import os
import time
from pathlib import Path

from app.config import EMBED_DIMS, EXTRACTOR_VERSION, SKIP_DIRS, is_safe_root
from app.db import (
    delete_missing_files,
    embedded_paths,
    get_conn,
    get_setting,
    record_scanned_root,
    replace_folders,
    set_setting,
    under_root,
    upsert_file,
    upsert_vector,
)
from app.embeddings import available_model, document_text, embed_texts, pack
from app.extractors import extract_excerpt, is_image
from app.ollama_client import list_models


def list_entries(root: Path):
    """Return selectable subfolders and files under a scanned root."""
    folders = []
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        folders.extend(Path(dirpath) / name for name in dirnames)
        for name in filenames:
            if name.startswith("."):
                continue
            files.append(Path(dirpath) / name)
    return folders, files


def _validate(root: Path) -> Path:
    root = root.resolve()
    if not is_safe_root(root):
        raise ValueError(f"{root} is not a selectable folder")
    if not root.is_dir():
        raise ValueError(f"{root} does not exist")
    return root


def _indexed_rows(root: Path) -> dict:
    """What the last scan of this folder recorded, keyed by path.

    Empty when the extractor has changed since that scan, which forces every
    file to be read again with the current one.
    """
    with get_conn() as conn:
        if get_setting(conn, "extractor_version") != EXTRACTOR_VERSION:
            return {}
        rows = conn.execute(
            r"SELECT path, mtime, size_bytes, excerpt FROM files WHERE path LIKE ? ESCAPE '\'",
            (under_root(str(root)),),
        ).fetchall()
    return {row["path"]: row for row in rows}


def _is_fresh(path: Path, cached, vision_model: str | None) -> bool:
    """True when the indexed row still matches the file on disk.

    Re-reading every file on every scan is the expensive part of scanning: a
    folder of 150 screenshots costs several minutes of vision-model time to
    learn nothing new. Size and mtime are enough to catch real edits, and a
    false negative only costs one wasted read.
    """
    if cached is None:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    if cached["mtime"] != stat.st_mtime or cached["size_bytes"] != stat.st_size:
        return False
    # A folder first scanned without the vision model has no text for its
    # images. Turning it on has to re-read them even though nothing changed.
    if vision_model and is_image(path) and not cached["excerpt"]:
        return False
    return True


def _record_for(path: Path, vision_model: str | None, cached=None) -> dict | None:
    """Build the row for `path`, reusing `cached`'s excerpt when it is current."""
    try:
        stat = path.stat()
    except OSError:
        return None
    excerpt = cached["excerpt"] if cached is not None else extract_excerpt(
        path, vision_model=vision_model
    )
    return {
        "path": str(path),
        "filename": path.name,
        "ext": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "ctime": stat.st_ctime,
        "excerpt": excerpt,
        "indexed_at": time.time(),
    }


# Records written per transaction. Scanning never keeps a connection open
# across a yield: reading a file (seconds each, with a vision model) happens
# with nothing held, so the database stays usable for the rest of the app for
# the length of a scan, and an abandoned scan can't strand a write
# transaction. Batching keeps that from costing a connection per file.
FLUSH_EVERY = 50


def scan_stream(root: Path, vision_model: str | None = None):
    """Index `root`, yielding progress events. Reading image contents with a
    vision model takes seconds per file, so the UI needs to show which file is
    being read and how far along it is."""
    root = _validate(root)
    folders, files = list_entries(root)
    total = len(files)
    indexed = _indexed_rows(root)
    embed_model = available_model(list_models())
    with get_conn() as conn:
        already_embedded = (
            embedded_paths(conn, embed_model, EMBED_DIMS) if embed_model else set()
        )
    # A file needs a vector when its text changed or it never had one for this
    # model. Tracked per file so a rescan of an unchanged folder does no
    # embedding work at all.
    needs_vector: set[str] = set()
    images = (
        sum(1 for f in files if is_image(f) and not _is_fresh(f, indexed.get(str(f)), vision_model))
        if vision_model
        else 0
    )
    yield {"type": "begin", "total": total, "images": images}

    seen: set[str] = set()
    count = 0
    reused = 0
    pending: list[dict] = []

    def flush() -> None:
        if not pending:
            return
        # Embedding a batch at a time rather than a file at a time: per-call
        # overhead dominates at this size. A failed batch returns nothing and
        # leaves those files searchable lexically, which is the same state the
        # app is in with no embedding model installed at all.
        stale = [r for r in pending if r["path"] in needs_vector]
        vectors = embed_texts(
            [document_text(r["filename"], r["excerpt"]) for r in stale], model=embed_model
        ) if embed_model and stale else []
        with get_conn() as conn:
            for record in pending:
                upsert_file(conn, record)
            for record, vector in zip(stale, vectors):
                upsert_vector(conn, record["path"], embed_model, len(vector), pack(vector))
        pending.clear()

    for i, path in enumerate(files, start=1):
        cached = indexed.get(str(path))
        fresh = _is_fresh(path, cached, vision_model)
        yield {
            "type": "reading",
            "i": i,
            "total": total,
            "filename": path.name,
            "vision": bool(vision_model) and is_image(path) and not fresh,
            "cached": fresh,
        }
        record = _record_for(path, vision_model, cached if fresh else None)
        if record is None:
            continue
        if not fresh or str(path) not in already_embedded:
            needs_vector.add(str(path))
        pending.append(record)
        seen.add(str(path))
        count += 1
        reused += fresh
        if len(pending) >= FLUSH_EVERY:
            flush()

    flush()
    with get_conn() as conn:
        delete_missing_files(conn, str(root), seen)
        replace_folders(conn, str(root), [str(folder) for folder in folders])
        record_scanned_root(conn, str(root), count)
        set_setting(conn, "extractor_version", EXTRACTOR_VERSION)
    yield {"type": "done", "indexed": count, "reused": reused, "root": str(root)}


def scan(root: Path, vision_model: str | None = None) -> int:
    """scan_stream() without the progress events."""
    count = 0
    for event in scan_stream(root, vision_model=vision_model):
        if event["type"] == "done":
            count = event["indexed"]
    return count


if __name__ == "__main__":
    from app.config import DEFAULT_ROOT
    from app.db import init_db

    init_db()
    n = scan(DEFAULT_ROOT)
    print(f"Indexed {n} files under {DEFAULT_ROOT}")

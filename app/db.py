import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    filename TEXT,
    ext TEXT,
    size_bytes INTEGER,
    mtime REAL,
    ctime REAL,
    excerpt TEXT,
    category TEXT,
    subcategory TEXT,
    indexed_at REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    path UNINDEXED,
    filename,
    excerpt
);

CREATE TABLE IF NOT EXISTS scanned_roots (
    root TEXT PRIMARY KEY,
    last_scanned REAL,
    file_count INTEGER
);

CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY,
    root TEXT,
    name TEXT,
    indexed_at REAL
);

CREATE TABLE IF NOT EXISTS file_vectors (
    path TEXT PRIMARY KEY,
    model TEXT,
    dims INTEGER,
    vec BLOB,
    embedded_at REAL
);

CREATE TABLE IF NOT EXISTS folder_profile (
    root TEXT,
    category TEXT,
    subcategory TEXT,
    count INTEGER,
    last_used REAL,
    PRIMARY KEY (root, category, subcategory)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_path TEXT,
    dst_path TEXT,
    batch_id TEXT,
    root TEXT,
    kind TEXT DEFAULT 'file',
    applied_at REAL,
    undone INTEGER DEFAULT 0
);
"""


@contextmanager
def get_conn():
    """A connection scoped to one unit of work, committed on clean exit.

    `check_same_thread=False` because the streaming endpoints run inside
    Starlette's worker-thread pool, which is free to resume a generator on a
    different thread than the one that started it. Callers still use a
    connection from one thread at a time; this only lifts sqlite3's assertion
    that it must be the *same* thread every time. Without it, a scan raises
    mid-write and the connection then can't even be closed (close() makes the
    same check), leaking an open write transaction that locks the database
    against every scan that follows.

    WAL keeps reads (browse, ask) working while a scan is writing, and
    `timeout` makes a second writer wait its turn rather than failing
    immediately with "database is locked".
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(moves)")}
        if "kind" not in columns:
            conn.execute("ALTER TABLE moves ADD COLUMN kind TEXT DEFAULT 'file'")


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, json.dumps(value)),
    )


# The bucket anything unrecognised falls back to, whatever a run's planning
# call decided to name the rest of its folders.
FALLBACK_CATEGORY = "Other"


def upsert_file(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        """
        INSERT INTO files (path, filename, ext, size_bytes, mtime, ctime, excerpt, indexed_at)
        VALUES (:path, :filename, :ext, :size_bytes, :mtime, :ctime, :excerpt, :indexed_at)
        ON CONFLICT(path) DO UPDATE SET
            filename=excluded.filename,
            ext=excluded.ext,
            size_bytes=excluded.size_bytes,
            mtime=excluded.mtime,
            ctime=excluded.ctime,
            excerpt=excluded.excerpt,
            indexed_at=excluded.indexed_at
        """,
        record,
    )
    conn.execute("DELETE FROM files_fts WHERE path = ?", (record["path"],))
    conn.execute(
        "INSERT INTO files_fts (path, filename, excerpt) VALUES (?, ?, ?)",
        (record["path"], record["filename"], record["excerpt"] or ""),
    )


def upsert_vector(conn: sqlite3.Connection, path: str, model: str, dims: int, vec: bytes) -> None:
    conn.execute(
        """
        INSERT INTO file_vectors (path, model, dims, vec, embedded_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            model=excluded.model, dims=excluded.dims, vec=excluded.vec,
            embedded_at=excluded.embedded_at
        """,
        (path, model, dims, vec, time.time()),
    )


def embedded_paths(conn: sqlite3.Connection, model: str, dims: int) -> set[str]:
    """Paths already embedded by this model at this size. Anything else needs
    re-embedding, since vectors from different models are not comparable."""
    rows = conn.execute(
        "SELECT path FROM file_vectors WHERE model = ? AND dims = ?", (model, dims)
    ).fetchall()
    return {row["path"] for row in rows}


def all_vectors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT path, vec FROM file_vectors").fetchall()


def under_root(root: str) -> str:
    r"""LIKE pattern matching every path inside `root`, for use with ESCAPE '\'.

    Two things a bare f"{root}%" gets wrong: `%` and `_` are LIKE wildcards, and
    folder names contain underscores often enough to matter ("my_folder" would
    also match "myXfolder"); and without a trailing separator, "~/test" matches
    "~/test2" as well, so scanning one folder can evict another's rows.
    """
    escaped = root.rstrip("/").replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
    return f"{escaped}/%"


def delete_missing_files(conn: sqlite3.Connection, root: str, seen_paths: set[str]) -> None:
    rows = conn.execute(
        r"SELECT path FROM files WHERE path LIKE ? ESCAPE '\'", (under_root(root),)
    ).fetchall()
    for row in rows:
        if row["path"] not in seen_paths:
            conn.execute("DELETE FROM files WHERE path = ?", (row["path"],))
            conn.execute("DELETE FROM files_fts WHERE path = ?", (row["path"],))
            conn.execute("DELETE FROM file_vectors WHERE path = ?", (row["path"],))


def replace_folders(conn: sqlite3.Connection, root: str, paths: list[str]) -> None:
    """Replace the directory-name context recorded by the latest scan."""
    conn.execute(
        r"DELETE FROM folders WHERE root = ? OR path LIKE ? ESCAPE '\'",
        (root, under_root(root)),
    )
    now = time.time()
    conn.executemany(
        "INSERT INTO folders(path, root, name, indexed_at) VALUES (?, ?, ?, ?)",
        [(path, root, path.rsplit("/", 1)[-1], now) for path in paths],
    )


# Common question words that would otherwise sink every match, since FTS5's
# default bareword query is an AND of every term in the string.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "where", "what", "when",
    "who", "which", "my", "me", "i", "of", "to", "for", "in", "on", "at",
    "that", "this", "with", "do", "does", "did", "find", "show", "tell",
    "about", "from", "any", "some", "there", "have", "has", "can", "you",
}


def _fts_query(text: str) -> str:
    words = re.findall(r"\w+", text.lower())
    terms = [w for w in words if w not in _STOPWORDS and len(w) > 1] or words
    return " OR ".join(f'"{t}"' for t in terms) if terms else text


# Matched terms come back wrapped in these, rather than in markup, so the API
# layer can HTML-escape the surrounding text before turning them into tags.
MATCH_OPEN = "\x02"
MATCH_CLOSE = "\x03"

# How deep each retriever goes before fusion. Wider than the final limit, so a
# file ranked poorly by one method can still be rescued by the other.
CANDIDATES = 40

# Reciprocal rank fusion: score a file 1/(RRF_K + rank) in each ranking it
# appears in, and add. It needs only the orderings, not the scores, which is
# what lets bm25 (unbounded, lower is better) and cosine similarity (0..1,
# higher is better) be combined without trying to calibrate them against each
# other. 60 is the constant from the original paper; it damps the influence of
# the top rank enough that agreement between the two rankings matters more than
# either one's first place.
RRF_K = 60


def _fts_ranked(conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[str, str]]:
    """Lexical hits as (path, snippet), best first."""
    try:
        rows = conn.execute(
            f"""
            SELECT files_fts.path AS path,
                   snippet(files_fts, 2, '{MATCH_OPEN}', '{MATCH_CLOSE}', '…', 24) AS snippet
            FROM files_fts
            WHERE files_fts MATCH ?
            ORDER BY bm25(files_fts)
            LIMIT ?
            """,
            (_fts_query(query), limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 chokes on bare punctuation-heavy queries; fall back to a LIKE
        # scan so the "ask" flow never hard-fails on a weird question.
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT path, substr(excerpt, 1, 200) AS snippet FROM files "
            "WHERE filename LIKE ? OR excerpt LIKE ? LIMIT ?",
            (like, like, limit),
        ).fetchall()
    return [(row["path"], row["snippet"] or "") for row in rows]


def _rows_by_path(conn: sqlite3.Connection, paths: list[str]) -> dict[str, dict]:
    """Full rows for `paths`, as plain dicts so callers can annotate them."""
    if not paths:
        return {}
    found = {}
    for i in range(0, len(paths), 500):  # SQLite caps bound variables per statement
        chunk = paths[i : i + 500]
        for row in conn.execute(
            f"SELECT * FROM files WHERE path IN ({','.join('?' * len(chunk))})", chunk
        ).fetchall():
            found[row["path"]] = dict(row)
    return found


def search_files(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    query_vector: list[float] | None = None,
    lexical_query: str | None = None,
) -> list[dict]:
    """Retrieve candidate files for a question, best first.

    Lexical alone misses anything phrased in different words than the document;
    semantic alone misses exact names, ids, and rare terms, which is most of
    what people actually search a folder for. Running both and fusing the
    rankings covers each one's failure mode.

    Rows come back as dicts carrying an extra "snippet": the matching span of
    the excerpt, so a result can show its evidence instead of just a filename.
    """
    lexical = _fts_ranked(conn, lexical_query or query, CANDIDATES)
    snippets = dict(lexical)

    scores: dict[str, float] = {}
    for position, (path, _) in enumerate(lexical):
        scores[path] = scores.get(path, 0.0) + 1.0 / (RRF_K + position)

    if query_vector:
        from app.embeddings import rank as rank_vectors

        semantic = rank_vectors(query_vector, all_vectors(conn))[:CANDIDATES]
        for position, (path, _) in enumerate(semantic):
            scores[path] = scores.get(path, 0.0) + 1.0 / (RRF_K + position)

    ordered = sorted(scores, key=lambda p: scores[p], reverse=True)[:limit]
    found = _rows_by_path(conn, ordered)

    results = []
    for path in ordered:
        row = found.get(path)
        if row is None:
            continue
        # A file found only by vector search has no matched span to highlight,
        # so it falls back to the opening of its excerpt.
        row["snippet"] = snippets.get(path) or (row.get("excerpt") or "")[:200]
        results.append(row)
    return results


def record_scanned_root(conn: sqlite3.Connection, root: str, file_count: int) -> None:
    conn.execute(
        """
        INSERT INTO scanned_roots (root, last_scanned, file_count) VALUES (?, ?, ?)
        ON CONFLICT(root) DO UPDATE SET last_scanned = excluded.last_scanned, file_count = excluded.file_count
        """,
        (root, time.time(), file_count),
    )


def forget_scanned_root(conn: sqlite3.Connection, root: str) -> None:
    """Drop a folder from Recent, along with everything indexed under it.

    Recent *is* the list of scanned roots, so keeping the indexed rows would
    leave a forgotten folder's files still answering "ask" queries.
    """
    pattern = under_root(root)
    conn.execute(r"DELETE FROM files_fts WHERE path LIKE ? ESCAPE '\'", (pattern,))
    conn.execute(r"DELETE FROM file_vectors WHERE path LIKE ? ESCAPE '\'", (pattern,))
    conn.execute(r"DELETE FROM files WHERE path LIKE ? ESCAPE '\'", (pattern,))
    conn.execute(r"DELETE FROM folders WHERE root = ? OR path LIKE ? ESCAPE '\'", (root, pattern))
    conn.execute("DELETE FROM scanned_roots WHERE root = ?", (root,))


def forget_all_scanned_roots(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM files_fts")
    conn.execute("DELETE FROM file_vectors")
    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM folders")
    conn.execute("DELETE FROM scanned_roots")


def has_scanned_root(conn: sqlite3.Connection, root: str) -> bool:
    return conn.execute("SELECT 1 FROM scanned_roots WHERE root = ?", (root,)).fetchone() is not None


def list_scanned_roots(conn: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT root, last_scanned, file_count FROM scanned_roots ORDER BY last_scanned DESC LIMIT ?",
        (limit,),
    ).fetchall()


def record_move(
    conn: sqlite3.Connection, src: str, dst: str, batch_id: str, root: str,
    kind: str = "file",
) -> None:
    conn.execute(
        "INSERT INTO moves (src_path, dst_path, batch_id, root, kind, applied_at, undone) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (src, dst, batch_id, root, kind, time.time()),
    )


def update_paths_after_folder_move(conn: sqlite3.Connection, src: str, dst: str) -> None:
    """Make every indexed descendant follow a directory rename."""
    pattern = under_root(src)
    for table in ("files", "files_fts", "file_vectors"):
        rows = conn.execute(
            rf"SELECT path FROM {table} WHERE path LIKE ? ESCAPE '\'", (pattern,)
        ).fetchall()
        for row in rows:
            old = row["path"]
            conn.execute(
                f"UPDATE {table} SET path = ? WHERE path = ?",
                (dst + old[len(src):], old),
            )

    rows = conn.execute(
        r"SELECT path, root FROM folders WHERE path = ? OR path LIKE ? ESCAPE '\'",
        (src, pattern),
    ).fetchall()
    for row in rows:
        old_path, old_root = row["path"], row["root"]
        new_path = dst + old_path[len(src):]
        if old_root == src or old_root.startswith(src + "/"):
            old_root = dst + old_root[len(src):]
        conn.execute(
            "UPDATE folders SET path = ?, root = ?, name = ? WHERE path = ?",
            (new_path, old_root, new_path.rsplit("/", 1)[-1], old_path),
        )

    roots = conn.execute(
        r"SELECT root FROM scanned_roots WHERE root = ? OR root LIKE ? ESCAPE '\'",
        (src, pattern),
    ).fetchall()
    for row in roots:
        old = row["root"]
        conn.execute(
            "UPDATE scanned_roots SET root = ? WHERE root = ?",
            (dst + old[len(src):], old),
        )


def update_file_after_move(conn: sqlite3.Connection, src: str, dst: str, category: str, subcategory: str | None) -> None:
    # Organizing renames as well as moves, so the indexed filename has to
    # follow. Without this, search still matches the name the file had before
    # the run and "ask" reports a name that no longer exists on disk.
    filename = dst.rsplit("/", 1)[-1]
    conn.execute(
        "UPDATE files SET path = ?, filename = ?, category = ?, subcategory = ? WHERE path = ?",
        (dst, filename, category, subcategory, src),
    )
    conn.execute(
        "UPDATE files_fts SET path = ?, filename = ? WHERE path = ?", (dst, filename, src)
    )
    conn.execute("UPDATE file_vectors SET path = ? WHERE path = ?", (dst, src))


def update_file_after_rename(conn: sqlite3.Connection, src: str, dst: str) -> None:
    """Follow an in-place rename without changing organization metadata."""
    filename = dst.rsplit("/", 1)[-1]
    conn.execute(
        "UPDATE files SET path = ?, filename = ? WHERE path = ?",
        (dst, filename, src),
    )
    conn.execute(
        "UPDATE files_fts SET path = ?, filename = ? WHERE path = ?", (dst, filename, src)
    )
    conn.execute("UPDATE file_vectors SET path = ? WHERE path = ?", (dst, src))


def revert_file_path(conn: sqlite3.Connection, dst: str, restored: str) -> None:
    """Point the index back at a file undo has just moved home."""
    filename = restored.rsplit("/", 1)[-1]
    conn.execute(
        "UPDATE files SET path = ?, filename = ?, category = NULL, subcategory = NULL WHERE path = ?",
        (restored, filename, dst),
    )
    conn.execute(
        "UPDATE files_fts SET path = ?, filename = ? WHERE path = ?", (restored, filename, dst)
    )
    conn.execute("UPDATE file_vectors SET path = ? WHERE path = ?", (restored, dst))


def record_folder_use(conn: sqlite3.Connection, root: str, category: str, subcategory: str | None) -> None:
    """Remember that this folder ended up using this category/subfolder pair.

    Recorded at apply time, which means it records the plan *after* the user
    edited it in the review table. The corrections are the valuable part: they
    are the only signal about how this person actually wants their files filed,
    and no amount of prompting recovers them.
    """
    conn.execute(
        """
        INSERT INTO folder_profile (root, category, subcategory, count, last_used)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(root, category, subcategory) DO UPDATE SET
            count = count + 1, last_used = excluded.last_used
        """,
        (root, category, subcategory or "", time.time()),
    )


def folder_profile(conn: sqlite3.Connection, root: str, limit: int = 10) -> list[dict]:
    """The subfolders this root has used before, most-used first."""
    rows = conn.execute(
        """
        SELECT category, subcategory, count FROM folder_profile
        WHERE root = ? AND subcategory != ''
        ORDER BY count DESC, last_used DESC
        LIMIT ?
        """,
        (root, limit),
    ).fetchall()
    return [
        {"name": r["subcategory"], "category": r["category"], "count": r["count"]}
        for r in rows
    ]


def forget_folder_profile(conn: sqlite3.Connection, root: str | None = None) -> None:
    if root is None:
        conn.execute("DELETE FROM folder_profile")
    else:
        conn.execute("DELETE FROM folder_profile WHERE root = ?", (root,))


def latest_batch_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT batch_id FROM moves WHERE undone = 0 ORDER BY applied_at DESC LIMIT 1"
    ).fetchone()
    return row["batch_id"] if row else None


def moves_for_batch(conn: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM moves WHERE batch_id = ? AND undone = 0", (batch_id,)
    ).fetchall()


def mark_batch_undone(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute("UPDATE moves SET undone = 1 WHERE batch_id = ?", (batch_id,))


def new_batch_id() -> str:
    return uuid.uuid4().hex[:12]

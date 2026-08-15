import asyncio
import base64
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import (
    DEFAULT_ROOT,
    HOME,
    OLLAMA_MODEL,
    SENSITIVE_HOME_SUBDIRS,
    SKIP_DIRS,
    VISION_MODEL,
    is_safe_root,
    is_embed_model,
    is_vision_model,
)
from app.db import (
    forget_all_scanned_roots,
    forget_folder_profile,
    forget_scanned_root,
    get_conn,
    has_scanned_root,
    init_db,
    list_scanned_roots,
    replace_folders,
    under_root,
)
from app import commands, indexer, organizer, search
from app.embeddings import available_model
from app.extractors import downscale_image, is_image
from app.ollama_client import list_models

app = FastAPI(title="Sift")

init_db()

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_ROOT = Path(__file__).resolve().parent.parent
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health", include_in_schema=False)
def api_health():
    """Readiness check used by both launchers.

    It names the app, process, and checkout that answered rather than just
    saying "ok", so a launcher can tell three cases apart that all used to
    look identical: our server is already up, some unrelated program owns the
    port, or a wedged process is holding the socket without serving. Adopting
    the second and third leaves the UI loading against a backend that never
    answers, which surfaces as "Failed to fetch" on the first real request.
    """
    return {
        "status": "ok",
        "app": "sift",
        "pid": os.getpid(),
        "root": str(APP_ROOT),
    }


# Closing the browser tab stops the local server, so the app does not outlive
# the window it was opened for. A reload fires the same close event as a real
# close, so a close arms a shutdown rather than performing one: any page that
# checks in during the grace window cancels it. That covers a reload, and it
# covers a second tab still being open, since every open page heartbeats.
SHUTDOWN_GRACE_SECONDS = float(os.environ.get("SIFT_SHUTDOWN_GRACE", "6"))
AUTO_SHUTDOWN = os.environ.get("SIFT_AUTO_SHUTDOWN", "1") != "0"
STOP_SCRIPT = APP_ROOT / "macos" / "stop_server.sh"

_last_heartbeat = 0.0


class _QuietHeartbeat(logging.Filter):
    """Keep heartbeats out of the access log.

    One line every few seconds per open tab would bury the requests worth
    reading, and this log is the first place to look when the app misbehaves.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/heartbeat" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_QuietHeartbeat())


@app.post("/api/heartbeat", include_in_schema=False)
def api_heartbeat():
    """Sent by every open page. Presence of one cancels a pending shutdown."""
    global _last_heartbeat
    _last_heartbeat = time.monotonic()
    return {"ok": True}


@app.post("/api/closing", include_in_schema=False)
async def api_closing():
    """Sent by a page being unloaded, via navigator.sendBeacon."""
    if not AUTO_SHUTDOWN:
        return {"ok": True, "armed": False}
    asyncio.create_task(_shutdown_unless_reclaimed(time.monotonic()))
    return {"ok": True, "armed": True}


async def _shutdown_unless_reclaimed(armed_at: float) -> None:
    await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)
    if _last_heartbeat > armed_at:
        return
    # Delegate to the same script the README documents, detached so it
    # survives this process, which it is about to stop. It stops by PID file
    # and then sweeps the port, so it also clears a --reload parent that
    # would otherwise be left holding the socket.
    subprocess.Popen(
        [str(STOP_SCRIPT)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/api/models")
def api_models():
    """Installed models, split into the two pickers the UI offers.

    Whatever is in `ollama list` shows up here, so pulling a model is all it
    takes to add one. Vision models are kept out of the chat picker since they
    have their own, but a few of them (gemma3) are capable chat models too, so
    an install with nothing else falls back to the unfiltered list rather than
    leaving the chat picker empty.

    Embedding models are filtered out of both: they cannot hold a conversation
    or read an image, so offering one in a picker only produces an error. They
    are selected automatically instead, since there is nothing to choose.
    """
    installed = list_models()
    models = [m for m in installed if not is_embed_model(m)]
    vision = [m for m in models if is_vision_model(m)]
    chat = [m for m in models if m not in set(vision)] or models

    default = OLLAMA_MODEL if OLLAMA_MODEL in chat else (chat[0] if chat else None)
    vision_default = VISION_MODEL if VISION_MODEL in vision else (vision[0] if vision else None)
    return {
        "models": chat,
        "default": default,
        "vision_models": vision,
        "vision_default": vision_default,
        # The UI says whether semantic search is on, since it changes what the
        # Find box can do and depends on a model the user may not have pulled.
        "embed_model": available_model(installed),
    }


@app.get("/api/browse")
def api_browse(path: str | None = None):
    target = Path(path).expanduser().resolve() if path else HOME
    if target != HOME:
        try:
            target.relative_to(HOME)
        except ValueError:
            raise HTTPException(status_code=400, detail="path is outside your home folder")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="not a directory")

    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in SKIP_DIRS or child.name in SENSITIVE_HOME_SUBDIRS:
                continue
            entries.append({"name": child.name, "path": str(child)})
    except PermissionError:
        pass

    with get_conn() as conn:
        recent = [dict(r) for r in list_scanned_roots(conn)]

    return {
        "path": str(target),
        "parent": str(target.parent) if target != HOME else None,
        "selectable": is_safe_root(target),
        "entries": entries,
        "recent_roots": recent,
        "quick_picks": [
            {"name": "Downloads", "path": str(DEFAULT_ROOT)},
            {"name": "Desktop", "path": str(HOME / "Desktop")},
            {"name": "Documents", "path": str(HOME / "Documents")},
        ],
    }


class ForgetRequest(BaseModel):
    root: str | None = None  # None forgets every scanned folder


@app.post("/api/recent/forget")
def api_recent_forget(req: ForgetRequest):
    with get_conn() as conn:
        if req.root is None:
            forget_all_scanned_roots(conn)
            forget_folder_profile(conn)
        else:
            # Only a folder that is actually in the list can be forgotten, so a
            # stray root can't turn into a prefix match that purges the index.
            if not has_scanned_root(conn, req.root):
                raise HTTPException(status_code=404, detail="not a recent folder")
            forget_scanned_root(conn, req.root)
            # Forgetting a folder means forgetting what was learned about it
            # too, otherwise "forget" leaves its filing habits behind.
            forget_folder_profile(conn, req.root)
        return {"recent_roots": [dict(r) for r in list_scanned_roots(conn)]}


class IndexRequest(BaseModel):
    root: str
    read_images: bool = False
    vision_model: str | None = None


def _vision_model_for(req: IndexRequest) -> str | None:
    return (req.vision_model or VISION_MODEL) if req.read_images else None


@app.post("/api/index")
def api_index(req: IndexRequest):
    root = Path(req.root).expanduser()
    try:
        count = indexer.scan(root, vision_model=_vision_model_for(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"indexed": count, "root": str(root.resolve())}


@app.post("/api/index/stream")
def api_index_stream(req: IndexRequest):
    root = Path(req.root).expanduser()
    if not is_safe_root(root.resolve()):
        raise HTTPException(status_code=400, detail=f"{root} is not a selectable folder")

    def events():
        try:
            for ev in indexer.scan_stream(root, vision_model=_vision_model_for(req)):
                yield f"data: {json.dumps(ev)}\n\n"
        except ValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class SuggestRequest(BaseModel):
    root: str
    model: str | None = None


@app.post("/api/organize/suggest")
def api_organize_suggest(req: SuggestRequest):
    root = Path(req.root).expanduser().resolve()
    if not is_safe_root(root):
        raise HTTPException(status_code=400, detail=f"{root} is not a selectable folder")
    with get_conn() as conn:
        rows = conn.execute(
            r"SELECT path FROM files WHERE path LIKE ? ESCAPE '\' AND category IS NULL",
            (under_root(str(root)),),
        ).fetchall()
    paths = [r["path"] for r in rows]
    if not paths:
        return {"suggestions": []}
    return {"suggestions": organizer.suggest(paths, model=req.model, root=root)}


@app.post("/api/organize/suggest/stream")
def api_organize_suggest_stream(req: SuggestRequest):
    """Server-sent events so the UI can show per-file progress: organizing
    makes one model call per file, which is slow enough to need feedback."""
    root = Path(req.root).expanduser().resolve()
    if not is_safe_root(root):
        raise HTTPException(status_code=400, detail=f"{root} is not a selectable folder")
    with get_conn() as conn:
        rows = conn.execute(
            r"SELECT path FROM files WHERE path LIKE ? ESCAPE '\' AND category IS NULL",
            (under_root(str(root)),),
        ).fetchall()
    paths = [r["path"] for r in rows]

    def events():
        if not paths:
            yield f"data: {json.dumps({'type': 'done', 'total': 0})}\n\n"
            return
        for ev in organizer.suggest_stream(paths, model=req.model, root=root):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class CommandRequest(BaseModel):
    root: str
    command: str
    model: str | None = None
    # Optional manual narrowing from the UI's filter chips, applied on top of
    # whatever the request itself implies.
    extensions: list[str] | None = None


@app.post("/api/command/stream")
def api_command_stream(req: CommandRequest):
    """Plan a natural-language file action.

    Produces the same suggestion shape as organizing, so the result is reviewed
    and applied through exactly the same path — including undo.
    """
    root = Path(req.root).expanduser().resolve()
    if not is_safe_root(root):
        raise HTTPException(status_code=400, detail=f"{root} is not a selectable folder")
    if not req.command.strip():
        raise HTTPException(status_code=400, detail="a request is required")

    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                r"SELECT * FROM files WHERE path LIKE ? ESCAPE '\'",
                (under_root(str(root)),),
            ).fetchall()
        ]
        folder_rows = [
            {
                **dict(r),
                "relative_path": str(Path(r["path"]).relative_to(root)),
            }
            for r in conn.execute(
                "SELECT path, name FROM folders WHERE root = ? ORDER BY path",
                (str(root),),
            ).fetchall()
        ]
        # An index created before folder context existed has no folder rows.
        # Backfill it on the first Do request so upgrading does not require the
        # user to remember to rescan before a folder rename can work.
        if not folder_rows:
            existing_folders, _ = indexer.list_entries(root)
            replace_folders(conn, str(root), [str(path) for path in existing_folders])
            folder_rows = [
                {
                    "path": str(path),
                    "name": path.name,
                    "relative_path": str(path.relative_to(root)),
                }
                for path in existing_folders
            ]

    if req.extensions:
        wanted = {e.lower() for e in req.extensions}
        rows = [r for r in rows if (r["ext"] or "").lower() in wanted]

    def events():
        if not rows and not folder_rows:
            yield f"data: {json.dumps({'type': 'error', 'detail': 'Nothing indexed in this folder yet.'})}\n\n"
            return
        for ev in commands.plan_stream(rows, req.command, model=req.model, folders=folder_rows):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ApplyRequest(BaseModel):
    root: str
    suggestions: list[dict]


@app.post("/api/organize/apply")
def api_organize_apply(req: ApplyRequest):
    try:
        batch_id, applied = organizer.apply(Path(req.root).expanduser(), req.suggestions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # `applied` is what moved, not what was asked for: reporting the request
    # count told the user a batch had succeeded even when every file was skipped.
    return {"batch_id": batch_id, "applied": applied}


class UndoRequest(BaseModel):
    batch_id: str | None = None


@app.post("/api/organize/undo")
def api_organize_undo(req: UndoRequest):
    reverted = organizer.undo(req.batch_id)
    return {"reverted": reverted}


class AskRequest(BaseModel):
    question: str
    model: str | None = None


@app.post("/api/ask")
def api_ask(req: AskRequest):
    return search.ask(req.question, model=req.model)


class RevealRequest(BaseModel):
    path: str


@app.post("/api/reveal")
def api_reveal(req: RevealRequest):
    p = Path(req.path)
    if not is_safe_root(p):
        raise HTTPException(status_code=400, detail="path is outside allowed folders")
    if not p.exists():
        raise HTTPException(status_code=404, detail="file not found")
    subprocess.run(["open", "-R", str(p)])
    return {"ok": True}


# Longest side of a result thumbnail, in CSS pixels times two for retina.
THUMB_PIXELS = 112


@app.get("/api/thumb")
def api_thumb(path: str):
    """A small JPEG preview of an indexed image.

    The only route that returns file contents, so it is the one place where a
    path traversal would leak data rather than just fail. Two independent
    checks: the path has to be inside a folder the app is allowed to read, and
    it has to be a file this app actually indexed — so even a bug in the first
    check cannot turn this into a general file server.
    """
    target = Path(path).expanduser()
    if not is_safe_root(target) or not is_image(target):
        raise HTTPException(status_code=400, detail="not a previewable path")
    with get_conn() as conn:
        known = conn.execute(
            "SELECT 1 FROM files WHERE path = ?", (str(target),)
        ).fetchone()
    if known is None:
        raise HTTPException(status_code=404, detail="not an indexed file")

    encoded = downscale_image(target, max_pixels=THUMB_PIXELS)
    if encoded is None:
        # Already smaller than the cap, so there is nothing to shrink.
        return FileResponse(target)
    return Response(
        content=base64.b64decode(encoded),
        media_type="image/jpeg",
        # The review table rebuilds its rows on every streamed suggestion, so
        # without this a single organize run refetches each thumbnail dozens of
        # times.
        headers={"Cache-Control": "private, max-age=3600"},
    )

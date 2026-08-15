"""UI-independent orchestration for both the FastAPI and terminal clients."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterator

from app import commands, indexer, organizer, search
from app.config import (
    OLLAMA_MODEL,
    VISION_MODEL,
    is_embed_model,
    is_safe_root,
    is_vision_model,
)
from app.db import get_conn, init_db, list_scanned_roots, replace_folders, under_root
from app.embeddings import available_model
from app.ollama_client import list_models
from app.plan import create_plan, split_current_items


class SiftService:
    def __init__(self) -> None:
        init_db()

    @staticmethod
    def root(path: str | Path | None) -> Path:
        root = Path(path or Path.cwd()).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"{root} does not exist or is not a directory")
        if not is_safe_root(root):
            raise ValueError(f"{root} is not a selectable folder")
        return root

    def models(self) -> dict:
        installed = list_models()
        usable = [name for name in installed if not is_embed_model(name)]
        vision = [name for name in usable if is_vision_model(name)]
        chat = [name for name in usable if name not in set(vision)] or usable
        return {
            "installed": installed,
            "chat_models": chat,
            "default": OLLAMA_MODEL if OLLAMA_MODEL in chat else (chat[0] if chat else None),
            "vision_models": vision,
            "vision_default": (
                VISION_MODEL if VISION_MODEL in vision else (vision[0] if vision else None)
            ),
            "embed_model": available_model(installed),
        }

    @staticmethod
    def require_model(model: str | None) -> str:
        installed = list_models()
        if not installed:
            raise ValueError(
                "No Ollama models are available. Start Ollama, then run "
                "`ollama pull qwen3:4b`."
            )
        selected = model or OLLAMA_MODEL
        if selected not in installed:
            raise ValueError(
                f"Ollama model {selected!r} is not installed. Run "
                f"`ollama pull {selected}` or choose one with /model."
            )
        return selected

    def status(self, root: str | Path | None) -> dict:
        selected = self.root(root)
        with get_conn() as conn:
            row = conn.execute(
                "SELECT root, last_scanned, file_count FROM scanned_roots WHERE root = ?",
                (str(selected),),
            ).fetchone()
        return {
            "root": str(selected),
            "indexed": bool(row),
            "file_count": row["file_count"] if row else 0,
            "last_scanned": row["last_scanned"] if row else None,
            **self.models(),
        }

    def recent(self) -> list[dict]:
        with get_conn() as conn:
            return [dict(row) for row in list_scanned_roots(conn)]

    @staticmethod
    def reveal(path: str | Path, *, root: str | Path | None = None) -> Path:
        """Show a safe local file in the platform's file manager."""
        target = Path(path).expanduser().resolve()
        if not target.exists() or not target.is_file():
            raise ValueError(f"{target} does not exist or is not a file")
        if not is_safe_root(target):
            raise ValueError(f"{target} is not a selectable file")
        if root is not None:
            selected = SiftService.root(root)
            try:
                target.relative_to(selected)
            except ValueError as exc:
                raise ValueError(f"{target} is outside the active folder") from exc
        if sys.platform == "darwin":
            command = ["open", "-R", str(target)]
        elif sys.platform == "win32":
            command = ["explorer", f"/select,{target}"]
        else:
            command = ["xdg-open", str(target.parent)]
        subprocess.run(command, check=True)
        return target

    def scan_events(
        self,
        root: str | Path | None,
        *,
        read_images: bool = False,
        vision_model: str | None = None,
    ) -> Iterator[dict]:
        selected = self.root(root)
        vision = self.require_model(vision_model or VISION_MODEL) if read_images else None
        yield from indexer.scan_stream(selected, vision_model=vision)

    def ask(
        self, question: str, *, model: str | None = None, root: str | Path | None = None
    ) -> dict:
        if not question.strip():
            raise ValueError("a question is required")
        selected = self.root(root) if root is not None else None
        return search.ask(
            question.strip(), model=self.require_model(model), root=selected
        )

    @staticmethod
    def _rows_and_folders(root: Path) -> tuple[list[dict], list[dict]]:
        with get_conn() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    r"SELECT * FROM files WHERE path LIKE ? ESCAPE '\'",
                    (under_root(str(root)),),
                ).fetchall()
            ]
            folders = [
                {**dict(row), "relative_path": str(Path(row["path"]).relative_to(root))}
                for row in conn.execute(
                    "SELECT path, name FROM folders WHERE root = ? ORDER BY path",
                    (str(root),),
                ).fetchall()
            ]
            if not folders:
                existing, _ = indexer.list_entries(root)
                replace_folders(conn, str(root), [str(path) for path in existing])
                folders = [
                    {"path": str(path), "name": path.name,
                     "relative_path": str(path.relative_to(root))}
                    for path in existing
                ]
        return rows, folders

    def command_events(
        self,
        root: str | Path | None,
        instruction: str,
        *,
        model: str | None = None,
        extensions: list[str] | None = None,
    ) -> Iterator[dict]:
        selected = self.root(root)
        if not instruction.strip():
            raise ValueError("an instruction is required")
        model = self.require_model(model)
        rows, folders = self._rows_and_folders(selected)
        if extensions:
            wanted = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}
            rows = [row for row in rows if (row.get("ext") or "").lower() in wanted]
        if not rows and not folders:
            yield {"type": "error", "detail": "Nothing indexed in this folder yet."}
            return
        yield from commands.plan_stream(rows, instruction, model=model, folders=folders)

    def organize_events(
        self, root: str | Path | None, *, model: str | None = None
    ) -> Iterator[dict]:
        selected = self.root(root)
        model = self.require_model(model)
        with get_conn() as conn:
            rows = conn.execute(
                r"SELECT path FROM files WHERE path LIKE ? ESCAPE '\' AND category IS NULL",
                (under_root(str(selected)),),
            ).fetchall()
        paths = [row["path"] for row in rows]
        if not paths:
            yield {"type": "done", "total": 0}
            return
        yield from organizer.suggest_stream(paths, model=model, root=selected)

    def build_plan(
        self,
        root: str | Path | None,
        suggestions: list[dict],
        *,
        instruction: str,
        kind: str,
        model: str | None,
    ) -> dict:
        return create_plan(
            self.root(root), suggestions, instruction=instruction, kind=kind, model=model
        )

    def apply_plan(self, plan: dict) -> dict:
        current, stale = split_current_items(plan)
        batch_id, applied = organizer.apply(Path(plan["root"]), current)
        return {
            "batch_id": batch_id,
            "requested": len(current) + len(stale),
            "applied": applied,
            "skipped": len(current) - applied + len(stale),
            "stale": stale,
        }

    @staticmethod
    def undo(batch_id: str | None = None) -> int:
        return organizer.undo(batch_id)

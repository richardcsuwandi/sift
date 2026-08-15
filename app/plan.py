"""Serializable, reviewable plans shared by Sift's terminal commands."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def _fingerprint(path: Path) -> dict[str, int | float | None]:
    try:
        stat = path.stat()
    except OSError:
        return {"source_size": None, "source_mtime": None}
    return {"source_size": stat.st_size, "source_mtime": stat.st_mtime}


def create_plan(
    root: Path,
    suggestions: list[dict],
    *,
    instruction: str,
    kind: str,
    model: str | None,
) -> dict[str, Any]:
    """Wrap domain suggestions in a stable file format for review/automation."""
    items = []
    for suggestion in suggestions:
        item = {**suggestion, "selected": suggestion.get("selected", True)}
        item.update(_fingerprint(Path(str(suggestion.get("path") or ""))))
        items.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": uuid.uuid4().hex,
        "created_at": time.time(),
        "root": str(root.resolve()),
        "instruction": instruction,
        "kind": kind,
        "model": model,
        "items": items,
    }


def validate_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("plan must be a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported plan schema version: {value.get('schema_version')!r}")
    if not isinstance(value.get("root"), str) or not value["root"]:
        raise ValueError("plan has no root folder")
    if not isinstance(value.get("items"), list):
        raise ValueError("plan items must be a list")
    if not all(isinstance(item, dict) and item.get("path") for item in value["items"]):
        raise ValueError("every plan item must name a source path")
    return value


def load_plan(path: Path) -> dict[str, Any]:
    try:
        return validate_plan(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def save_plan(plan: dict[str, Any], path: Path) -> Path:
    validate_plan(plan)
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path.resolve()


def selected_items(plan: dict[str, Any]) -> list[dict]:
    return [item for item in plan["items"] if item.get("selected", True)]


def split_current_items(plan: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Return (safe_to_apply, stale_or_missing) selected plan items."""
    current, stale = [], []
    for item in selected_items(validate_plan(plan)):
        path = Path(str(item["path"]))
        try:
            stat = path.stat()
        except OSError:
            stale.append({**item, "skip_reason": "source is missing"})
            continue
        expected_size = item.get("source_size")
        expected_mtime = item.get("source_mtime")
        if expected_size is not None and stat.st_size != expected_size:
            stale.append({**item, "skip_reason": "source size changed"})
        elif expected_mtime is not None and stat.st_mtime != expected_mtime:
            stale.append({**item, "skip_reason": "source modification time changed"})
        else:
            current.append(item)
    return current, stale

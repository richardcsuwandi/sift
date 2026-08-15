"""Natural-language file actions.

"Move 2025 receipts into Taxes", "rename these papers as author_year_title",
"trash the installers". Each one becomes an ordinary list of suggestions and
goes through organizer.apply, so the review tree, sanitising, collision
handling, logging, and undo are the same code that organizing already uses.
Only the way the plan is *produced* is new.

A command runs in three phases, for the same reason organizing does:

1. Interpret — one call turns the sentence into a structured intent.
2. Select — the scope is applied to the index with no model involved.
3. Plan — mechanical commands ("move all PDFs into Papers") need no further
   calls at all; only the ones needing per-file judgement pay per file.

That ordering is what keeps a command over a 250-file folder fast: the scope
usually removes most of the folder before anything expensive happens.
"""

from pathlib import Path

from collections import Counter
import re

from app.organizer import (
    _safe_component,
    _safe_filename,
    _sample_patterns,
    dedupe_filenames,
)
from app.ollama_client import chat_json

INTERPRET_SYSTEM_PROMPT = """You turn a request about a folder of files into a
structured plan. You are given the request and a sample of the filenames.

Choose one action:
- "move": put the matching files into a folder.
- "rename": give the matching files better names, leaving them where they are.
- "rename_folder": rename one existing folder, leaving its contents in it.
- "trash": move the matching files to the system Trash.

Describe which files it applies to in "scope":
- "extensions": lowercase, with the dot, e.g. [".pdf"]. Empty means any type.
- "name_contains": substrings that must appear in the filename. Empty means any.
- "keywords": words to look for in the file's name or contents, for requests
  that describe what a file is about ("receipts", "invoices") rather than what
  it is called. Empty when the request is purely mechanical.

Set "dest_folder" to the destination folder name for a "move", else null. Use a
plain name, never a path, never "..".

Set "rename_pattern" to the naming scheme the user asked for, in their own
words, else null.

For "rename_folder", set "folder_path" to the existing relative folder path
from the supplied folder list and "new_folder_name" to the requested new plain
name. For every other action set both to null.

Set "needs_per_file" to true only when deciding about a file requires reading
that file — renaming from contents, or picking out files by what they are
about. Set it to false when the filename alone decides it, such as "move all
PDFs", since that avoids one model call per file.

Respond with ONLY a JSON object matching the supplied schema."""

INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["move", "rename", "rename_folder", "trash"]},
        "scope": {
            "type": "object",
            "properties": {
                "extensions": {"type": "array", "items": {"type": "string"}},
                "name_contains": {"type": "array", "items": {"type": "string"}},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["extensions", "name_contains", "keywords"],
        },
        "dest_folder": {"type": ["string", "null"]},
        "rename_pattern": {"type": ["string", "null"]},
        "folder_path": {"type": ["string", "null"]},
        "new_folder_name": {"type": ["string", "null"]},
        "needs_per_file": {"type": "boolean"},
    },
    "required": ["action", "scope", "dest_folder", "rename_pattern", "folder_path", "new_folder_name", "needs_per_file"],
}

# Filenames shown to the interpreting call. It only needs to see the shape of
# the folder to pick extensions and patterns, not every file in it.
INTERPRET_SAMPLE = 40

# Type words in ordinary requests should become extension filters, not literal
# filename filters. Small models otherwise turn "the installers" into both
# extensions=[".dmg"] and name_contains=["installer"], which is an impossible
# AND for names such as AcmeSetup.dmg and printer-driver.pkg. Keep this mapping
# deterministic because selecting the wrong files is a filesystem-safety bug,
# not merely a prompting-quality issue.
INSTALLER_EXTENSIONS = [
    ".dmg", ".pkg", ".exe", ".msi", ".deb", ".rpm", ".appimage", ".apk",
]
_INSTALLER_NOUN = re.compile(r"\binstallers?\b", flags=re.IGNORECASE)
_INSTALLER_SCOPE_WORDS = {"installer", "installers", "installation", "setup"}

PER_FILE_SYSTEM_PROMPT = """You are applying one instruction to one file. You
are given the instruction, the filename, and a short excerpt of its contents.

- "include" is whether this file is one the instruction is about. Be strict: a
  file the user did not mean is worse than one missed.
- "new_name" is the filename to use. Keep the extension. Repeat the current
  name unchanged when the instruction does not ask for a rename.

Respond with ONLY a JSON object matching the supplied schema."""

PER_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "include": {"type": "boolean"},
        "new_name": {"type": "string"},
    },
    "required": ["include", "new_name"],
}

# Characters of a file's text shown to a per-file call.
PER_FILE_EXCERPT = 600


def interpret(command: str, rows, model: str | None = None, folders=None) -> dict | None:
    """Parse a request into a structured plan, or None if it can't be read."""
    explicit_folder = _explicit_folder_rename(command, folders or [])
    if explicit_folder:
        return explicit_folder
    # A flat sample is dominated by whatever the folder has most of — 141 of
    # 245 files here are screenshots — and a small model reads that as the
    # subject of the request, answering "rename my papers" with a scope that
    # selects screenshots. Going round-robin over filename patterns shows it
    # the folder's variety instead, and the histogram keeps the proportions.
    _, sample = _sample_patterns(rows)
    histogram = Counter((r["ext"] or "(none)").lower() for r in rows)
    listing = ", ".join(f"{ext} x{n}" for ext, n in histogram.most_common())
    names = ", ".join(f'"{r["filename"]}"' for r in sample[:INTERPRET_SAMPLE])
    command_lower = command.casefold()
    ordered_folders = sorted(
        folders or [],
        key=lambda folder: (
            folder["name"].casefold() not in command_lower,
            folder["relative_path"].casefold(),
        ),
    )
    folder_names = ", ".join(
        f'"{folder["relative_path"]}"' for folder in ordered_folders[:INTERPRET_SAMPLE]
    ) or "(none)"
    user = (
        f'request: "{command}"\n\n'
        f"this folder holds {len(rows)} files: {listing}\n\n"
        f"a sample of the filenames, one per naming pattern: {names}\n\n"
        f"existing folders, relative to the selected root: {folder_names}\n\n"
        "The sample shows what kinds of file exist. It does not say what the "
        "request is about: do not assume the request concerns the most common "
        "kind of file."
    )
    try:
        plan = chat_json(
            INTERPRET_SYSTEM_PROMPT, user, model=model, schema=INTERPRET_SCHEMA
        )
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None

    scope = plan.get("scope") if isinstance(plan.get("scope"), dict) else {}
    action = plan.get("action") if plan.get("action") in {"move", "rename", "rename_folder", "trash"} else "move"
    normalized_scope = _scope_for_command(command, {
        "extensions": _extensions(scope.get("extensions")),
        "name_contains": _strings(scope.get("name_contains")),
        "keywords": _strings(scope.get("keywords")),
    })
    return {
        "action": action,
        "scope": normalized_scope,
        # A destination is a single folder name, so a model answering with a
        # path or a traversal loses everything but its last usable component.
        "dest_folder": _safe_component(plan.get("dest_folder")) or None,
        "rename_pattern": (plan.get("rename_pattern") or None),
        "folder_path": _safe_relative_folder(plan.get("folder_path")),
        "new_folder_name": _safe_component(plan.get("new_folder_name")) or None,
        # A rename always reads the file: the new name has to come from what
        # the document actually is, and no scope filter can supply that. Asking
        # the model to decide and hoping it says yes just produces renames
        # derived from the old filename.
        "needs_per_file": action == "rename" or (
            action != "rename_folder" and bool(plan.get("needs_per_file"))
        ),
    }


def _scope_for_command(command: str, scope: dict) -> dict:
    """Correct generic file-type nouns the model can only approximate.

    An installer is identified by its file type, even when neither "installer"
    nor "setup" appears in its name. Once the request explicitly says
    installer, the complete known extension family is authoritative and the
    generic noun is removed from substring filters. Any real modifier the
    model found (for example "old") is preserved.
    """
    if not _INSTALLER_NOUN.search(command):
        return scope
    return {
        "extensions": list(INSTALLER_EXTENSIONS),
        "name_contains": [
            value for value in scope.get("name_contains", [])
            if value not in _INSTALLER_SCOPE_WORDS
        ],
        "keywords": [
            value for value in scope.get("keywords", [])
            if value not in _INSTALLER_SCOPE_WORDS
        ],
    }


def _explicit_folder_rename(command: str, folders) -> dict | None:
    """Handle an unambiguous folder rename mechanically and instantly."""
    match = re.fullmatch(
        r"\s*rename\s+(?:the\s+)?(?:folder|directory)\s+(.+?)\s+(?:to|as|with)\s+(.+?)\s*",
        command,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    old = _safe_relative_folder(match.group(1).strip("\"' "))
    new = _safe_component(match.group(2).strip("\"' ")) or None
    folder = _select_folder(folders, old)
    if not folder or not new:
        return None
    return {
        "action": "rename_folder",
        "scope": {"extensions": [], "name_contains": [], "keywords": []},
        "dest_folder": None,
        "rename_pattern": None,
        "folder_path": folder["relative_path"],
        "new_folder_name": new,
        "needs_per_file": False,
    }


def _safe_relative_folder(value) -> str | None:
    if not isinstance(value, str):
        return None
    parts = [_safe_component(part) for part in value.replace("\\", "/").split("/")]
    clean = [part for part in parts if part]
    return "/".join(clean) or None


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    seen = [str(v).strip().lower() for v in value if str(v).strip()]
    return list(dict.fromkeys(seen))


def effective_scope(plan: dict, rows=None) -> dict:
    """The scope actually used to select files.

    When a command needs per-file judgement, only the extension filter is kept.
    `name_contains` is a literal filename test, but a model asked about "the
    research papers" fills it with the words *describing* them — and no file is
    named "research", so a brittle prefilter throws away every file before the
    call that could have recognised one. The per-file "include" answer is the
    authority in that case; the extension filter stays because it is exact and
    it is what keeps the run from touching every file in the folder.
    """
    scope = plan["scope"]
    if plan["needs_per_file"]:
        narrowed = {
            "extensions": scope["extensions"],
            "name_contains": scope["name_contains"],
            "keywords": [],
        }
        # Keep a literal filename filter when it identifies a real candidate
        # ("rename CV"). Descriptive guesses such as "research papers" that
        # match no filename are still dropped so contents can decide.
        if rows is not None and scope["name_contains"] and select(rows, narrowed):
            return narrowed
        return {"extensions": scope["extensions"], "name_contains": [], "keywords": []}
    return scope


def _extensions(value) -> list[str]:
    """Extensions, always dotted. Models answer "png" as often as ".png", and
    an undotted one silently matches nothing."""
    return [e if e.startswith(".") else f".{e}" for e in _strings(value)]


def select(rows, scope: dict) -> list:
    """The files a scope covers, decided without the model.

    Extensions and filename substrings are exact tests. Keywords are a
    substring match over the name and the indexed excerpt: a cheap prefilter
    that narrows the batch before any per-file call, not a judgement about
    whether a file really is a receipt.
    """
    extensions = set(scope.get("extensions") or [])
    contains = scope.get("name_contains") or []
    keywords = scope.get("keywords") or []

    selected = []
    for row in rows:
        name = (row["filename"] or "").lower()
        if extensions and Path(name).suffix not in extensions:
            continue
        if contains and not any(c in name for c in contains):
            continue
        if keywords:
            haystack = f"{name}\n{(row['excerpt'] or '').lower()}"
            if not any(k in haystack for k in keywords):
                continue
        selected.append(row)
    return selected


def _suggestion(row, *, name=None, dest_folder=None, trash=False, rename_only=False) -> dict:
    """A plan row in the shape organizer.apply and the review tree expect."""
    return {
        "path": row["path"],
        # Kept so a command's rows render in the same review table as an
        # organize run's; dest_folder and trash take precedence in apply().
        "category": row["category"] or "Other",
        "subcategory": None,
        "suggested_filename": name or row["filename"],
        "confidence": 1.0,
        "dest_folder": dest_folder,
        "trash": trash,
        # Rename is deliberately distinct from organize: apply() must keep the
        # file beside its source instead of falling back to a category folder.
        "rename_only": rename_only,
    }


def _folder_suggestion(folder: dict, new_name: str) -> dict:
    return {
        "path": folder["path"],
        "category": "Other",
        "subcategory": None,
        "suggested_filename": new_name,
        "confidence": 1.0,
        "dest_folder": None,
        "trash": False,
        "rename_only": False,
        "folder_rename": True,
    }


def _select_folder(folders, relative_path: str | None):
    if not relative_path:
        return None
    exact = [f for f in folders if f["relative_path"].casefold() == relative_path.casefold()]
    if len(exact) == 1:
        return exact[0]
    by_name = [f for f in folders if f["name"].casefold() == relative_path.casefold()]
    return by_name[0] if len(by_name) == 1 else None


def _per_file(row, command: str, plan: dict, model: str | None) -> dict | None:
    """Ask about one file. None means leave it alone."""
    excerpt = (row["excerpt"] or "(no extractable text)")[:PER_FILE_EXCERPT]
    user = (
        f'instruction: "{command}"\n'
        f'filename: "{row["filename"]}"\n'
        f'excerpt: "{excerpt}"'
    )
    if plan.get("rename_pattern"):
        user += f'\nnaming scheme requested: "{plan["rename_pattern"]}"'
    try:
        reply = chat_json(PER_FILE_SYSTEM_PROMPT, user, model=model, schema=PER_FILE_SCHEMA)
    except Exception:
        return None
    if not isinstance(reply, dict) or not reply.get("include"):
        return None
    return _suggestion(
        row,
        name=_safe_filename(reply.get("new_name"), row["filename"]),
        dest_folder=plan["dest_folder"] if plan["action"] == "move" else None,
        trash=plan["action"] == "trash",
        rename_only=plan["action"] == "rename",
    )


def plan_stream(rows, command: str, model: str | None = None, folders=None):
    """Turn a request into reviewable suggestions, yielding progress events.

    Events match organizer.suggest_stream's, so the client renders a command's
    plan with the same before/after tree and review table.
    """
    folders = folders or []
    plan = interpret(command, rows, model, folders=folders)
    if plan is None:
        yield {"type": "error", "detail": "Couldn't understand that request. Try rephrasing it."}
        return

    if plan["action"] == "rename_folder":
        folder = _select_folder(folders, plan.get("folder_path"))
        new_name = plan.get("new_folder_name")
        suggestions = [_folder_suggestion(folder, new_name)] if folder and new_name else []
        yield {
            "type": "interpreted", "action": plan["action"],
            "matched": len(suggestions), "total": len(folders),
            "per_file": False, "target": "folders",
        }
        yield {"type": "revised", "suggestions": suggestions}
        yield {"type": "done", "total": len(suggestions)}
        return

    scoped = select(rows, effective_scope(plan, rows))
    yield {
        "type": "interpreted",
        "action": plan["action"],
        "dest_folder": plan["dest_folder"],
        "matched": len(scoped),
        "total": len(rows),
        "per_file": plan["needs_per_file"],
        "target": "files",
    }

    if not scoped:
        yield {"type": "revised", "suggestions": []}
        yield {"type": "done", "total": 0}
        return

    total = len(scoped)
    yield {"type": "begin", "total": total, "model": model}

    suggestions: list[dict] = []
    if plan["needs_per_file"]:
        for i, row in enumerate(scoped, start=1):
            yield {"type": "reading", "i": i, "total": total, "filename": row["filename"]}
            suggestion = _per_file(row, command, plan, model)
            if suggestion is None:
                continue
            suggestions.append(suggestion)
            yield {"type": "item", "i": i, "total": total, "suggestion": suggestion}
    else:
        # Nothing here needs the model: the scope already answered the question,
        # so a folder-wide move costs one call in total rather than one per file.
        for row in scoped:
            suggestions.append(
                _suggestion(
                    row,
                    dest_folder=plan["dest_folder"] if plan["action"] == "move" else None,
                    trash=plan["action"] == "trash",
                )
            )

    # Only in-batch name collisions need settling; the category merging passes
    # are about an organize run's invented folders, which a command has none of.
    yield {"type": "revised", "suggestions": dedupe_filenames(suggestions)}
    yield {"type": "done", "total": len(suggestions)}

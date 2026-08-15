import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from app.config import (
    FALLBACK_CATEGORIES,
    MAX_CATEGORIES,
    MAX_SUBCATEGORIES,
    MIN_FILES_PER_SUBCATEGORY,
    MIN_SHAPE_GROUP,
    SHAPE_MAJORITY,
    VOCAB_EXCERPT_CHARS,
    VOCAB_SAMPLE_FILES,
    is_safe_root,
)
from app.db import (
    FALLBACK_CATEGORY,
    folder_profile,
    get_conn,
    latest_batch_id,
    mark_batch_undone,
    moves_for_batch,
    new_batch_id,
    record_folder_use,
    record_move,
    revert_file_path,
    update_file_after_rename,
    update_file_after_move,
    update_paths_after_folder_move,
)
from app.ollama_client import chat_json


class Plan(NamedTuple):
    """The folder names one run is allowed to use, both levels of them."""

    categories: list[str]
    folders: list[dict]


def system_prompt(categories: list[str]) -> str:
    return f"""You are a file organizing assistant. Given a filename and an
optional content excerpt, pick exactly one category from the list this run is
using: {categories}.
Optionally suggest a short subcategory (1-2 words) and a cleaner filename if the
current one is unhelpful (e.g. "IMG_4821.jpg" -> keep as-is; "Untitled document.pdf"
with an excerpt about a March invoice -> "march_invoice.pdf"). Keep the file
extension unchanged in suggested_filename. If the message lists available
subfolders, the subcategory must be one of those exact names, or null when none
of them fit this file; never invent a new one. Respond with ONLY a JSON object of
the form: {{"category": string, "subcategory": string or null, "suggested_filename": string, "confidence": number between 0 and 1}}"""


# Constrains decoding so the model cannot emit a category outside the run's
# planned list or omit a required field — the difference between "asked nicely"
# and "structurally impossible", which is what makes small models usable here.
def suggestion_schema(categories: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": categories},
            "subcategory": {"type": ["string", "null"]},
            "suggested_filename": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["category", "suggested_filename", "confidence"],
    }


VOCAB_SYSTEM_PROMPT = f"""You are planning a folder structure. Given a list of
files, name the folders they should be sorted into: between 2 and
{MAX_CATEGORIES} top-level categories, and between 2 and {MAX_SUBCATEGORIES}
subfolders that together cover most of the files. Rules:
- Name the folders these files actually need. Do not use a generic set.
- Name groups, not files: every folder must fit several of the listed files.
- Category names must describe what the files are for. Do not use catch-all
  names such as "Data", "Documents", "Files", or "Assets" when the batch shows
  more specific groups such as financial records, research material, media,
  project notes, or software packages.
- A subfolder must fit at least two listed files. If only one file would use a
  proposed subfolder, make it part of a broader category instead or omit it.
- Files sharing an obvious naming pattern (e.g. "Screenshot 2026-...") are one
  group, however many of them there are.
- Prefer few broad folders over many narrow ones.
- Installer packages (.dmg, .pkg, .exe, .msi, .deb, .rpm) and software
  archives (.zip, .rar, .7z, .tar) belong together when several are present;
  never classify an installer as Data.
- Use short, plain names of 1-2 words, e.g. "Screenshots", "Invoices", "Slides".
- Every subfolder's category must be one of the categories you listed.
- Never reuse a category name as a subfolder name.
- Always include a category named "{FALLBACK_CATEGORY}" for files nothing else covers.
Respond with ONLY a JSON object of the form:
{{"categories": [string, ...], "folders": [{{"name": string, "category": string}}, ...]}}"""


VOCAB_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {"type": "array", "items": {"type": "string"}},
        "folders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["name", "category"],
            },
        },
    },
    "required": ["categories", "folders"],
}


# Characters that must never reach a path component we create. "/" is the
# POSIX separator, and ":" is the classic Mac one: the filesystem accepts it,
# but Finder renders it as "/", so a model that rewrites "00.36.48" as
# "00:36:48" produces a name that reads as a folder path on screen.
_UNSAFE_CHARS = re.compile(r"[/:\\\x00-\x1f\x7f]")

# These types identify themselves without needing model judgement. A batch can
# contain opaque installers with no extractable text, and small models have
# classified one as Data and another as Other. Only enforce the family when at
# least two such files exist, preserving the no-folder-per-file rule.
INSTALLER_ARCHIVE_EXTENSIONS = {
    ".dmg", ".pkg", ".exe", ".msi", ".deb", ".rpm", ".appimage", ".apk",
    ".zip", ".rar", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".xz",
}
INSTALLER_ARCHIVE_CATEGORY = "Installers & Archives"

# Longest path component we'll create. macOS caps at 255 bytes; staying well
# under leaves room for multi-byte characters and a " (1)" collision suffix.
MAX_COMPONENT_CHARS = 120


def _safe_component(name) -> str:
    """Reduce model-supplied text to one safe path component, or "" if nothing
    usable survives.

    Everything a model returns is untrusted input here: suggested filenames and
    subfolders are interpolated straight into a destination path, so an
    embedded separator, a leading "/", or a literal ".." would otherwise be
    enough to place a file outside the folder being organized.
    """
    name = _UNSAFE_CHARS.sub(" ", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip()
    # Leading dots hide the file; trailing dots and spaces are stripped by some
    # filesystems, which silently turns "report." and "report" into a
    # collision. This also reduces "." and ".." to nothing. Stripping both
    # characters from both ends, rather than dots then dots-and-spaces, is what
    # keeps "../../escape" from leaving " .. escape" behind once the separators
    # have become spaces.
    name = name.strip(". ")
    return name[:MAX_COMPONENT_CHARS].rstrip(". ")


def _safe_filename(suggested, original: str) -> str:
    """A filename we're willing to create, from the model's suggestion.

    Falls back to the original name when the suggestion doesn't survive
    sanitising, and always restores the original extension: the prompt asks the
    model to keep it, but asking isn't a guarantee, and a .docx renamed to .pdf
    stops opening in the right application.
    """
    suffix = _UNSAFE_CHARS.sub("", Path(original).suffix)[:20]
    # A model asked for a filename sometimes answers with a path. Reading it as
    # "the last segment" keeps the useful part of "photos/march_invoice.pdf"
    # while reducing "../../../etc/passwd" to a plain name.
    segments = [p for p in re.split(r"[/\\]", str(suggested or "")) if p.strip(". ")]
    stem = _safe_component(Path(_safe_component(segments[-1] if segments else "")).stem)
    if not stem:
        stem = _safe_component(Path(original).stem)
    # Leave room for the extension and a " (12)" collision suffix.
    stem = stem[: max(1, MAX_COMPONENT_CHARS - len(suffix) - 5)].rstrip(". ")
    return f"{stem or 'file'}{suffix}"


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


# Deleting is a move into the system Trash, never an unlink. That keeps one
# promise the app already makes — files are only ever moved — and gets undo for
# free, since a trashed file is logged exactly like any other move and Finder
# can restore it independently.
TRASH_DIR = Path.home() / ".Trash"


def _relative_folder(dest_folder) -> list[str]:
    """A model- or user-supplied destination, as safe path components.

    Each segment goes through the same sanitising as a subcategory, so a
    destination of "../../Library" becomes "Library" rather than an escape.
    """
    segments = re.split(r"[/\\]", str(dest_folder or ""))
    return [c for c in (_safe_component(seg) for seg in segments) if c]


def destination_dir(root: Path, suggestion: dict) -> Path | None:
    """Where a suggestion wants its file to go, or None if that isn't allowed.

    Three shapes share one apply path: an organize run's category/subcategory, a
    natural-language command's explicit folder, and a trash operation. Keeping
    them here means the review tree, collision handling, logging, and undo are
    identical for all three.
    """
    if suggestion.get("trash"):
        return TRASH_DIR

    folder = _relative_folder(suggestion.get("dest_folder"))
    if folder:
        target = root.joinpath(*folder)
    else:
        # A run names its own categories and the user can then edit them by
        # hand, so there is no list to check this against: it only has to
        # survive sanitising as a single path component.
        category = _safe_component(suggestion.get("category")) or FALLBACK_CATEGORY
        target = root / category / _safe_component(suggestion.get("subcategory"))

    return target if _is_within(target, root) else None


def _norm(name: str) -> str:
    """Canonical key for a folder name, so "Screenshots", "screenshot" and
    "Screen Shots" are recognised as one folder rather than three."""
    words = []
    for word in re.sub(r"[^a-z0-9]+", " ", name.lower()).split():
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return "".join(words)


def plan_vocabulary(
    rows,
    model: str | None = None,
    learned: list[dict] | None = None,
) -> Plan:
    """One call that sees the whole batch and names the folders it should use.

    This is the fix for the failure mode where N independent per-file calls
    invent N folders: they can't agree on a vocabulary they never see. Planning
    it once and enum-constraining the per-file calls to the result is what buys
    a stable tree without hardcoding the folder names — which couldn't be
    hardcoded well anyway, since a screenshots folder and a tax folder need
    different ones.

    Each subfolder is planned *with* its category, so "Screenshots" resolves to
    one path for the whole run instead of being re-decided per file and landing
    under two different categories. Falls back to FALLBACK_CATEGORIES with no
    subfolders if the model fails, so a run without a working model still sorts
    files somewhere sensible.
    """
    if not rows:
        return Plan(list(FALLBACK_CATEGORIES), [])
    patterns, sample = _sample_patterns(rows)
    # The extension histogram is over the whole batch, not just the sample, so
    # the planner can see that e.g. most of the folder is screenshots even when
    # it's only shown 60 of 600 files.
    histogram = Counter(Path(row["filename"]).suffix.lower() or "(none)" for row in rows)
    listing = f"{len(rows)} files: " + ", ".join(
        f"{ext} x{n}" for ext, n in histogram.most_common()
    )
    lines = []
    for row in sample:
        # Saying how many files share this one's naming pattern is what keeps a
        # stratified sample honest: the planner sees every *kind* of file, and
        # still knows which kinds are the bulk of the folder.
        siblings = patterns[_shape(row["filename"])] - 1
        alike = f" (+{siblings} more like it)" if siblings else ""
        excerpt = (row["excerpt"] or "(no extractable text)")[:VOCAB_EXCERPT_CHARS]
        lines.append(f'- "{row["filename"]}"{alike}: {excerpt}')
    listing += "\n\n" + "\n".join(lines)

    # Folders this user has actually approved here before. Offered as a
    # preference rather than a constraint: a folder that no longer fits the
    # files should not be revived, but one that does should keep its name
    # instead of being re-invented as a synonym every run.
    if learned:
        remembered = ", ".join(f'"{f["name"]}" (under {f["category"]})' for f in learned)
        listing += (
            f"\n\nThis folder has used these subfolders before: {remembered}."
            "\nReuse a previous name whenever it fits the files above, so the"
            " structure stays stable between runs. Only propose a new name for"
            " files none of them cover."
        )

    try:
        reply = chat_json(VOCAB_SYSTEM_PROMPT, listing, model=model, schema=VOCAB_SCHEMA)
    except Exception:
        return Plan(list(FALLBACK_CATEGORIES), [])

    categories = _planned_categories(reply.get("categories"))

    # A proposed subfolder that repeats a category name buys nothing and reads
    # badly as a path ("Documents/Documents"), so it's dropped here rather
    # than offered to the per-file calls.
    reserved = {_norm(c) for c in categories}
    folders: list[dict] = []
    seen: set[str] = set()
    for folder in reply.get("folders") or []:
        if not isinstance(folder, dict):
            continue
        name = _safe_component(folder.get("name"))
        key = _norm(name)
        if not key or key in seen or key in reserved:
            continue
        # The categories are free text now, so a subfolder can name one the
        # model left off its own list. Adopting it beats reassigning the folder
        # to "Other": the plan stays internally consistent either way, and this
        # way it keeps the structure the model actually described.
        category = _match_category(folder.get("category"), categories)
        if category is None:
            category = _safe_component(folder.get("category"))
            if not category or len(categories) >= MAX_CATEGORIES:
                category = FALLBACK_CATEGORY
            else:
                categories.append(category)
                reserved.add(_norm(category))
        seen.add(key)
        folders.append({"name": name, "category": category})
    return Plan(categories, folders[:MAX_SUBCATEGORIES])


def _planned_categories(raw) -> list[str]:
    """The run's top-level folders, from whatever the planning call returned."""
    categories: list[str] = []
    seen: set[str] = set()
    for value in raw or []:
        name = _safe_component(value)
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        categories.append(name)
        if len(categories) >= MAX_CATEGORIES:
            break
    if not categories:
        return list(FALLBACK_CATEGORIES)
    # apply() falls back to "Other" for anything it can't place, so the run has
    # to be able to name it even when the model didn't.
    if FALLBACK_CATEGORY not in categories:
        categories.append(FALLBACK_CATEGORY)
    return categories


def _match_category(value, categories: list[str]) -> str | None:
    """The planned category this name refers to, spelling differences aside."""
    key = _norm(_safe_component(value))
    if not key:
        return None
    return next((c for c in categories if _norm(c) == key), None)


def _sample_patterns(rows) -> tuple[Counter, list]:
    """Pick the files the planner is shown, one naming pattern at a time.

    The old stride sample took every Nth file, which sounds neutral but isn't:
    when 141 of 244 files are screenshots, most of the sample is screenshots,
    and the planner proposes a folder for them and nothing for the research
    PDFs it barely saw. Going round-robin over naming patterns (largest first,
    so the bulk of the folder is never missed) shows it the *variety* instead,
    which is what it's being asked to name folders for.
    """
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[_shape(row["filename"])].append(row)
    patterns = Counter({shape: len(members) for shape, members in groups.items()})
    # Stable: sorted() keeps insertion order among equal-sized groups, so the
    # same batch always shows the planner the same files.
    ordered = sorted(groups.values(), key=len, reverse=True)

    sample: list = []
    for i in range(max(len(g) for g in ordered)):
        for group in ordered:
            if i < len(group):
                sample.append(group[i])
                if len(sample) >= VOCAB_SAMPLE_FILES:
                    return patterns, sample
    return patterns, sample


def _schema_for(vocabulary: list[dict], categories: list[str]) -> dict:
    """The per-file schema, with subcategory pinned to the planned folders."""
    base = suggestion_schema(categories)
    if not vocabulary:
        return base
    return {
        **base,
        "properties": {
            **base["properties"],
            "subcategory": {"enum": [*(f["name"] for f in vocabulary), None]},
        },
    }


def _suggest_one(row, model: str | None, plan: Plan | None = None) -> dict:
    plan = plan or Plan(list(FALLBACK_CATEGORIES), [])
    vocabulary, categories = plan.folders, plan.categories
    user_prompt = (
        f'filename: "{row["filename"]}"\n'
        f'excerpt: "{row["excerpt"] or "(no extractable text)"}"'
    )
    if vocabulary:
        listing = ", ".join(f'"{f["name"]}"' for f in vocabulary)
        user_prompt += (
            f"\navailable subfolders: {listing}"
            "\nPut this file in one of them if it plausibly belongs; use null only"
            " when it clearly fits none."
        )

    # Falls back to the unconstrained schema before giving up on the file, so
    # a runtime that chokes on the enum costs a retry rather than turning
    # every single file in the batch into "Other".
    base = suggestion_schema(categories)
    schemas = [_schema_for(vocabulary, categories), base] if vocabulary else [base]
    suggestion = None
    for schema in schemas:
        try:
            suggestion = chat_json(
                system_prompt(categories), user_prompt, model=model, schema=schema
            )
            break
        except Exception:
            continue
    if suggestion is None:
        suggestion = {
            "category": FALLBACK_CATEGORY,
            "subcategory": None,
            "suggested_filename": row["filename"],
            "confidence": 0.0,
        }
    # The enum makes this near-impossible, but the retry above can run without
    # one, and then a near-miss spelling should still land in the planned
    # folder rather than in "Other".
    suggestion["category"] = _match_category(suggestion.get("category"), categories) or FALLBACK_CATEGORY
    # subcategory is optional in the schema, so a model is free to leave the
    # key out entirely; fill it in so every suggestion has the same shape.
    suggestion.setdefault("subcategory", None)

    # A planned folder owns its category. Taking it from the plan rather than
    # from this call is what keeps one folder at one path: the per-file call
    # decides *whether* the file belongs in "Screenshots", never where
    # "Screenshots" itself lives.
    planned = _planned_folder(suggestion.get("subcategory"), vocabulary)
    if planned:
        suggestion["subcategory"] = planned["name"]
        suggestion["category"] = planned["category"]

    # Sanitised here, not only in apply(), so the review table and the tree
    # preview show the name that will actually be created. apply() sanitises
    # again because the user can edit these fields by hand first.
    suggestion["subcategory"] = _safe_component(suggestion["subcategory"]) or None
    suggestion["suggested_filename"] = _safe_filename(
        suggestion.get("suggested_filename"), row["filename"]
    )
    suggestion["path"] = row["path"]
    return suggestion


def _planned_folder(subcategory, vocabulary: list[dict]) -> dict | None:
    key = _norm(str(subcategory or ""))
    if not key:
        return None
    return next((f for f in vocabulary if _norm(f["name"]) == key), None)


def merge_subcategories(suggestions: list[dict]) -> list[dict]:
    """Deterministic cleanup pass over a finished batch. No model involved.

    Planning helps but can't guarantee: an enum keeps the model to the agreed
    names, yet each call still picks its category alone, so the same folder can
    still end up under two of them ("Documents/Screenshots" *and*
    "Images/Screenshots"), and a planned folder can still attract a single
    file. Both are properties of the result set as a whole, which is why they
    get settled here, once every file has an answer.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in suggestions:
        subcategory = (s.get("subcategory") or "").strip()
        if subcategory:
            groups[_norm(subcategory)].append(s)

    for members in groups.values():
        if len(members) < MIN_FILES_PER_SUBCATEGORY:
            for s in members:
                s["subcategory"] = None
            continue
        # Ties resolve to whichever spelling/category was seen first, so the
        # same batch always produces the same tree.
        name = Counter(s["subcategory"].strip() for s in members).most_common(1)[0][0]
        category = Counter(
            _safe_component(s.get("category")) or FALLBACK_CATEGORY for s in members
        ).most_common(1)[0][0]
        # Free-text runs (no vocabulary) can still land on a category's own
        # name, which would nest it under itself as "Documents/Documents".
        if _norm(name) == _norm(category):
            name = None
        for s in members:
            s["subcategory"] = name
            s["category"] = category
    return suggestions


def _shape(filename: str) -> str:
    """Canonical key for a filename *pattern*, with the varying part removed.

    Every "Screenshot 2026-06-10 at 00.14.08.png" in a folder collapses to the
    same key regardless of its timestamp, which is what lets a batch notice that
    141 of its files are the same kind of thing.
    """
    path = Path(filename)
    stem = re.sub(r"\d+", "#", path.stem.lower())
    return f"{re.sub(r'[^a-z#]+', ' ', stem).strip()}|{path.suffix.lower()}"


def _format_sig(filename: str) -> str:
    """The punctuation/format skeleton of a name, with words and numbers erased.

    Compares *how* a name is written rather than what it says, so
    "march_invoice" and "april_invoice" match ("w_w") while
    "Screenshot_2026-06-10_00.14.08" and "Screenshot_2026-06-10_001448" don't
    ("w_#-#-#_#.#.#" vs "w_#-#-#_#").
    """
    return re.sub(r"\d+", "#", re.sub(r"[a-z]+", "w", Path(filename).stem.lower()))


def unify_by_shape(suggestions: list[dict]) -> list[dict]:
    """Make files that share a filename pattern agree with each other.

    The per-file call sees one file at a time, so on a batch of near-identical
    inputs it answers a fraction of them differently just from decoding noise:
    most screenshots get "Screenshots" and a handful get null, which is how a
    file ends up alone at the top of Images/ next to the folder it belongs in.
    Greedy decoding removes most of that, but "most" isn't a guarantee, and the
    evidence needed to settle it is a property of the batch rather than of any
    one file. So it's settled here, deterministically, with no model involved:
    a clear majority of files that look alike outvotes the stragglers.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in suggestions:
        groups[_shape(Path(str(s.get("path") or "")).name)].append(s)

    for members in groups.values():
        # Two lookalike files are a coincidence, so leave small groups alone;
        # there's no majority worth trusting in them anyway.
        if len(members) < MIN_SHAPE_GROUP:
            continue
        _unify_destination(members)
        _unify_renames(members)
    return suggestions


def _unify_destination(members: list[dict]) -> None:
    votes = Counter((s.get("subcategory") or "").strip() for s in members)
    votes.pop("", None)  # null is not a candidate, only the thing being outvoted
    if not votes:
        return
    name, count = votes.most_common(1)[0]
    if count <= len(members) * SHAPE_MAJORITY:
        return
    # The category comes from the files that picked this folder, so a straggler
    # can't drag the group somewhere else on its way in.
    category = Counter(
        s.get("category") for s in members if (s.get("subcategory") or "").strip() == name
    ).most_common(1)[0][0]
    for s in members:
        s["subcategory"] = name
        s["category"] = _safe_component(category) or FALLBACK_CATEGORY


def _unify_renames(members: list[dict]) -> None:
    """Drop the rename for a group the model rewrote inconsistently.

    One pattern came back as "Screenshot_2026-06-10_00.14.08.png",
    "..._001448.png", "..._00_40_10.png" and "..._01-28-32.png" in a single run.
    Files already sharing a naming convention have little to gain from being
    renamed and everything to lose from being renamed four different ways, so
    disagreement is read as "leave them alone". A group the model rewrote
    *consistently* keeps its new names: that's the case renaming is for.
    """
    if len({_format_sig(s.get("suggested_filename") or "") for s in members}) == 1:
        return
    for s in members:
        s["suggested_filename"] = Path(str(s.get("path") or "")).name


def dedupe_filenames(suggestions: list[dict]) -> list[dict]:
    """Give every file a distinct name within its destination folder.

    Flattening a tree brings files together that were only ever kept apart by
    living in different folders (two copies of "AI_curriculum.docx", two of a
    paper PDF). apply() already resolves that on disk, but silently and after
    the fact, so the preview promised a tree it wasn't going to produce.
    Deciding it here means the name in the review table is the name on disk.

    Comparison is case-insensitive because the destination usually is: on APFS
    and NTFS, "AI_curriculum.docx" and "ai_curriculum.docx" are one file, and
    two suggestions differing only in case would otherwise overwrite.
    """
    taken: dict[tuple, set[str]] = defaultdict(set)
    for s in suggestions:
        # Keyed on where the file actually lands, so a command that sends files
        # to an explicit folder gets the same in-batch collision handling that
        # an organize run does.
        if s.get("trash"):
            folder = ("\x00trash",)
        elif s.get("rename_only"):
            folder = ("\x00rename", str(Path(str(s.get("path") or "")).parent))
        elif s.get("dest_folder"):
            folder = tuple(_relative_folder(s.get("dest_folder")))
        else:
            folder = (s.get("category"), (s.get("subcategory") or "").strip())
        name = s.get("suggested_filename") or Path(str(s.get("path") or "")).name
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate, i = name, 1
        while candidate.lower() in taken[folder]:
            candidate = f"{stem} ({i}){suffix}"
            i += 1
        taken[folder].add(candidate.lower())
        s["suggested_filename"] = candidate
    return suggestions


def cohere_obvious_file_families(suggestions: list[dict]) -> list[dict]:
    """Keep extension-defined families together after model classification.

    This is deliberately narrow: it does not try to infer whether an arbitrary
    PDF is a paper or invoice. Installer and archive formats are unambiguous,
    however, and splitting them across Data/Other is never useful.
    """
    members = [
        suggestion for suggestion in suggestions
        if Path(str(suggestion.get("path") or "")).suffix.lower()
        in INSTALLER_ARCHIVE_EXTENSIONS
    ]
    if len(members) < MIN_FILES_PER_SUBCATEGORY:
        return suggestions
    for suggestion in members:
        suggestion["category"] = INSTALLER_ARCHIVE_CATEGORY
        suggestion["subcategory"] = None
    return suggestions


def finalize(suggestions: list[dict]) -> list[dict]:
    """The deterministic passes over a finished batch, in dependency order:
    lookalikes agree, then folders are merged and thinned, obvious file-type
    families cohere, then names are made unique within the final folders."""
    unified = unify_by_shape(suggestions)
    merged = merge_subcategories(unified)
    coherent = cohere_obvious_file_families(merged)
    return dedupe_filenames(coherent)


# SQLite caps how many variables one statement may bind (999 on older builds),
# and a large folder passes far more paths than that.
_SQL_VARS_PER_QUERY = 500


def _rows_for(paths: list[str]) -> list:
    if not paths:
        # "IN ()" is a syntax error, so the empty case can't reach SQLite.
        return []
    rows = {}
    with get_conn() as conn:
        for i in range(0, len(paths), _SQL_VARS_PER_QUERY):
            chunk = paths[i : i + _SQL_VARS_PER_QUERY]
            for row in conn.execute(
                f"SELECT * FROM files WHERE path IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall():
                rows[row["path"]] = row
    return [rows[p] for p in paths if p in rows]


def _learned_folders(root: Path | str | None) -> list[dict]:
    """Folders this root has been organized into before."""
    if not root:
        return []
    with get_conn() as conn:
        return folder_profile(conn, str(root))


def suggest(paths: list[str], model: str | None = None, root: Path | str | None = None) -> list[dict]:
    rows = _rows_for(paths)
    plan = plan_vocabulary(rows, model, _learned_folders(root))
    return finalize([_suggest_one(row, model, plan) for row in rows])


def suggest_stream(paths: list[str], model: str | None = None, root: Path | str | None = None):
    """Same as suggest(), but yields progress events so the UI can show which
    file is being read and a real percentage."""
    rows = _rows_for(paths)
    total = len(rows)
    learned = _learned_folders(root)
    yield {"type": "begin", "total": total, "model": model, "learned": len(learned)}

    yield {"type": "planning", "total": total, "learned": len(learned)}
    # Planned once for the whole run, so every file in a batch is sorted
    # against the same set of folder names.
    plan = plan_vocabulary(rows, model, learned)
    yield {"type": "vocabulary", "categories": plan.categories, "folders": plan.folders}

    suggestions = []
    for i, row in enumerate(rows, start=1):
        yield {"type": "reading", "i": i, "total": total, "filename": row["filename"]}
        suggestions.append(_suggest_one(row, model, plan))
        yield {"type": "item", "i": i, "total": total, "suggestion": suggestions[-1]}

    # These passes need the whole batch, so the client gets a corrected full set
    # at the end and replaces what it built up from the per-file events.
    yield {"type": "revised", "suggestions": finalize(suggestions)}
    yield {"type": "done", "total": total}


def apply(root: Path, suggestions: list[dict]) -> tuple[str, int]:
    """Move each suggestion into place. Returns the batch id and how many items
    actually moved, which is not the same as how many were requested: a
    suggestion can be skipped for being unsafe, missing, or already in place."""
    root = root.resolve()
    if not is_safe_root(root):
        raise ValueError(f"{root} is not a selectable folder")

    batch_id = new_batch_id()
    moved = 0
    with get_conn() as conn:
        for s in suggestions:
            src = Path(str(s.get("path") or "")).resolve()
            folder_rename = bool(s.get("folder_rename"))
            # Inside $HOME is not enough: the suggestions come from the client,
            # so without the second check a request could name any file in
            # $HOME and have this run pull it into the folder being organized.
            valid_source = src.is_dir() if folder_rename else src.is_file()
            if (
                not is_safe_root(src) or not _is_within(src, root) or
                not valid_source or (folder_rename and src == root)
            ):
                continue

            if folder_rename:
                name = _safe_component(s.get("suggested_filename"))
                if not name:
                    continue
                dst = src.parent / name
                if dst == src:
                    continue
                dst = _avoid_folder_collision(dst)
                record_move(conn, str(src), str(dst), batch_id, str(root), kind="folder")
                try:
                    shutil.move(str(src), str(dst))
                except OSError:
                    continue
                update_paths_after_folder_move(conn, str(src), str(dst))
                moved += 1
                continue

            # These arrive from the client, which got them from a model and
            # then let the user edit them by hand, so none of it is trusted.
            rename_only = bool(s.get("rename_only"))
            dst_dir = src.parent if rename_only else destination_dir(root, s)
            if dst_dir is None:
                continue
            filename = _safe_filename(s.get("suggested_filename"), src.name)
            dst = dst_dir / filename
            # Already exactly where it belongs. Without this, _avoid_collision
            # sees the file itself sitting at the destination and renames it to
            # "name (1).ext" for no reason.
            if dst == src:
                continue

            dst = _avoid_collision(dst)
            dst_dir.mkdir(parents=True, exist_ok=True)
            record_move(conn, str(src), str(dst), batch_id, str(root))
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                # One unmovable file (permissions, a full disk, a file that
                # vanished mid-batch) shouldn't abort the rest. The log row is
                # left behind deliberately: undo skips a dst that isn't there.
                continue
            # A trashed file keeps its move-log row, so undo restores it, but it
            # has no category and teaches the folder profile nothing.
            if rename_only:
                update_file_after_rename(conn, str(src), str(dst))
            elif s.get("trash"):
                update_file_after_move(conn, str(src), str(dst), None, None)
            else:
                relative = dst_dir.relative_to(root).parts
                category = relative[0] if relative else FALLBACK_CATEGORY
                subcategory = "/".join(relative[1:]) or None
                update_file_after_move(conn, str(src), str(dst), category, subcategory)
                record_folder_use(conn, str(root), category, subcategory)
            moved += 1
    return batch_id, moved


def undo(batch_id: str | None = None) -> int:
    with get_conn() as conn:
        target = batch_id or latest_batch_id(conn)
        if not target:
            return 0
        rows = moves_for_batch(conn, target)
        reverted = 0
        for row in rows:
            dst, src = Path(row["dst_path"]), Path(row["src_path"])
            root = Path(row["root"]) if row["root"] else None
            if dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                # Something else may have taken the original name since the
                # batch was applied. Undo restores a file, it never overwrites
                # one.
                is_folder = row["kind"] == "folder"
                restored = _avoid_folder_collision(src) if is_folder else _avoid_collision(src)
                try:
                    shutil.move(str(dst), str(restored))
                except OSError:
                    continue
                if is_folder:
                    update_paths_after_folder_move(conn, str(dst), str(restored))
                else:
                    revert_file_path(conn, str(dst), str(restored))
                reverted += 1
                _prune_empty_dirs(dst.parent, root)
        mark_batch_undone(conn, target)
    return reverted


def _prune_empty_dirs(directory: Path, root: Path | None) -> None:
    """Remove now-empty category folders left behind after an undo, stopping
    at the selected root so we never touch anything outside it."""
    directory = directory.resolve()
    stop_at = root.resolve() if root else None
    while is_safe_root(directory) and directory != stop_at:
        try:
            directory.rmdir()
        except OSError:
            break
        directory = directory.parent


def _avoid_collision(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem, suffix, parent = dst.stem, dst.suffix, dst.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _avoid_folder_collision(dst: Path) -> Path:
    if not dst.exists():
        return dst
    i = 1
    while True:
        candidate = dst.parent / f"{dst.name} ({i})"
        if not candidate.exists():
            return candidate
        i += 1

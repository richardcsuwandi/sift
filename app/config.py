import os
from pathlib import Path

from platformdirs import user_data_path

# Default model tag, used unless a request specifies a different one (see
# /api/models). Any Ollama-supported model works; qwen3:4b is the default
# because it benchmarked fastest and most consistent on the organize task
# (see README), not because anything here is tied to it.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")

# Vision model used to read image contents (OCR + short description) so that
# screenshots and photos become searchable by what's *in* them, not just their
# filename. Only used when image indexing is explicitly enabled — it's much
# slower than text extraction. This is the picker's initial selection; any
# installed vision model can be chosen at runtime instead.
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen2.5vl:3b")

# Substrings identifying vision-capable model tags. Used both to offer image
# reading only when such a model is installed, and to keep vision models out of
# the chat picker (they're selected separately, in the scan step).
#
# Any Ollama vision model works. One whose tag matches none of these is
# invisible to the app, so add it with a comma-separated
# EXTRA_VISION_HINTS="pixtral,internvl".
_BUILTIN_VISION_HINTS = ("vl", "llava", "vision", "moondream", "minicpm-v", "gemma3")

VISION_HINTS = _BUILTIN_VISION_HINTS + tuple(
    hint.strip().lower()
    for hint in os.environ.get("EXTRA_VISION_HINTS", "").split(",")
    if hint.strip()
)


def is_vision_model(tag: str) -> bool:
    """True if `tag` looks like a vision-capable Ollama model."""
    return any(hint in tag.lower() for hint in VISION_HINTS)


# Embedding model used for semantic search. Qwen3-Embedding is the default to
# keep the stack on one model family, but any embedding model on Ollama works;
# with none installed, search falls back to lexical matching alone.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen3-embedding:0.6b")

# Matryoshka truncation: the model natively emits 1024 dimensions and supports
# being cut short with little quality loss. Halving it halves both the stored
# index and the query-time arithmetic.
EMBED_DIMS = int(os.environ.get("EMBED_DIMS", "512"))

# How embedding models are recognised, both to pick a default and to keep them
# out of the chat and vision pickers, where they would only produce errors.
_BUILTIN_EMBED_HINTS = ("embed", "bge", "nomic", "minilm", "gte-", "e5-")

EMBED_HINTS = _BUILTIN_EMBED_HINTS + tuple(
    hint.strip().lower()
    for hint in os.environ.get("EXTRA_EMBED_HINTS", "").split(",")
    if hint.strip()
)


def is_embed_model(tag: str) -> bool:
    """True if `tag` looks like an embedding-only Ollama model."""
    return any(hint in tag.lower() for hint in EMBED_HINTS)


# Longest-side pixel cap for images handed to the vision model. Vision models
# charge per patch of image (Qwen2.5-VL, for one, roughly a token per 28x28
# block), so a raw retina screenshot costs ~7k image tokens while the same
# shot at 1024px costs ~900. Screenshot text stays legible well below the
# native resolution, so this is nearly free accuracy-wise and is by far the
# biggest lever on indexing speed.
VISION_MAX_PIXELS = int(os.environ.get("VISION_MAX_PIXELS", "1024"))

# Cap on tokens generated per image. Output is truncated to
# IMAGE_EXCERPT_MAX_CHARS anyway, so decoding past that is time spent on text
# that gets thrown away.
VISION_MAX_TOKENS = int(os.environ.get("VISION_MAX_TOKENS", "320"))

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".heic"}

HOME = Path.home()

# Convenience default for the directory picker's first suggestion.
DEFAULT_ROOT = HOME / "Downloads"

# Subdirectories directly under $HOME that are always off-limits, even
# though everything else under $HOME is selectable. Keeps the picker open
# to "any folder you'd plausibly want organized" while still fencing off
# credentials, app config, and OS-managed folders.
SENSITIVE_HOME_SUBDIRS = {
    "Library", ".ssh", ".gnupg", ".aws", ".config", ".docker",
    ".kube", ".npm", ".cache", ".Trash",
}

# Directories skipped during a recursive scan, even inside a safe root.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".Trash",
    ".DS_Store",
}

# Both folder levels are named per run by the planning call, since which
# folders are useful depends on what is actually in the folder being organized.
# This list is only the fallback for when that call fails, so a run without a
# working model still has somewhere to put things.
FALLBACK_CATEGORIES = [
    "Documents",
    "Images",
    "Invoices & Receipts",
    "Installers & Archives",
    "Code",
    "Media",
    "Other",
]

# Caps on what one planning call may name: top-level folders, and subfolders
# across the whole run. The per-file calls are then constrained to the result,
# which is what keeps a batch agreeing with itself.
MAX_CATEGORIES = 8
MAX_SUBCATEGORIES = 6

# How many files that planning call is shown. Enough to see the shape of a
# folder without pushing a small model past the context it handles well.
VOCAB_SAMPLE_FILES = 60

# Characters of excerpt shown per file when planning. The planner only needs
# to recognise kinds of files, not read them.
VOCAB_EXCERPT_CHARS = 160

# A subcategory holding fewer files than this is dropped after the run, since
# a folder per file is the thing this is all trying to avoid.
MIN_FILES_PER_SUBCATEGORY = 2

# How many files must share a filename pattern ("Screenshot <date> at <time>")
# before the run treats them as one group and makes them agree with each other.
# Two files looking alike is a coincidence; three is a convention.
MIN_SHAPE_GROUP = 3

# Share of a pattern group that must agree on a folder before the stragglers
# are moved to join them. A clear majority of identical-looking files is much
# better evidence of where they all belong than any single file's answer.
SHAPE_MAJORITY = 0.5

# How many characters of extracted text to keep per file for prompts/search.
# This is the ceiling on how much of a file the app can ever understand, so it
# bounds semantic search quality and snippet quality alike. At 500 it bound on
# essentially every text file in a real folder (measured: mean excerpt 461,
# max 500), truncating most documents inside their first page.
EXCERPT_MAX_CHARS = 2000

# Images get a larger budget: a slide or document screenshot carries far more
# searchable text than the first 500 chars of a .txt file.
IMAGE_EXCERPT_MAX_CHARS = 1200

# Bumped whenever extraction changes in a way that makes stored excerpts stale
# (a new file type, a larger budget, a better parser). Scanning reuses excerpts
# for files whose size and mtime are unchanged, which is what keeps a rescan
# cheap — but an improved extractor changes what a file *should* have produced
# without changing the file, so it needs this to invalidate the cache.
EXTRACTOR_VERSION = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_data_override = os.environ.get("SIFT_DATA_DIR")
# Source checkouts keep their existing local database, preserving the web
# app's development workflow. Installed wheels have no .git directory and use
# the OS-standard per-user application data location instead of attempting to
# write into site-packages.
DATA_DIR = (
    Path(_data_override).expanduser()
    if _data_override
    else PROJECT_ROOT / "data"
    if (PROJECT_ROOT / ".git").exists()
    else user_data_path("Sift", appauthor=False)
)
DB_PATH = DATA_DIR / "file_index.db"


def is_safe_root(path: Path) -> bool:
    """True if `path` is a directory the app is allowed to scan/organize.

    Anything under $HOME is fair game except $HOME itself (too broad — we
    never want to recursively reorganize someone's entire home folder) and
    a small set of sensitive subdirectories (credentials, app config, OS
    folders). Anything outside $HOME entirely is rejected.
    """
    path = path.resolve()
    if path == HOME:
        return False
    try:
        rel = path.relative_to(HOME)
    except ValueError:
        return False
    top = rel.parts[0] if rel.parts else None
    if top in SENSITIVE_HOME_SUBDIRS or (top and top.startswith(".")):
        return False
    return True

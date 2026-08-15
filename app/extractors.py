import re
from pathlib import Path

from app.config import EXCERPT_MAX_CHARS, IMAGE_EXCERPT_MAX_CHARS, IMAGE_EXTS

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh",
    ".log", ".ini", ".toml", ".cfg", ".tex", ".bib",
}


def _extract_txt(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:EXCERPT_MAX_CHARS]
    except OSError:
        return None


def _extract_pdf(path: Path) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        text = ""
        # The budget below stops this early on a dense page; the page cap only
        # matters for sparse ones (title pages, scanned covers), where two
        # pages of a paper is often just the title and the abstract.
        for page in reader.pages[:5]:
            text += page.extract_text() or ""
            if len(text) >= EXCERPT_MAX_CHARS:
                break
        return text[:EXCERPT_MAX_CHARS] or None
    except Exception:
        return None


def _extract_docx(path: Path) -> str | None:
    try:
        import docx

        document = docx.Document(str(path))
        text = "\n".join(p.text for p in document.paragraphs[:60])
        return text[:EXCERPT_MAX_CHARS] or None
    except Exception:
        return None


# Office formats are zip archives of XML, so their text is reachable with the
# standard library plus lxml (already present via python-docx). Adding
# openpyxl and python-pptx would pull in two dependencies to read text we can
# read here in a dozen lines — and an excerpt wants the text, not the cell
# grid or the slide geometry those libraries exist to model.
def _office_strings(path: Path, members, tag: str, limit: int, attr: str | None = None) -> list[str]:
    """Text from nodes named `tag`, in the named members of an OOXML zip.

    Reads each node's text content, or the value of `attr` when given — sheet
    names live in an attribute, cell and slide text in the element body.
    """
    import zipfile

    from lxml import etree

    out: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in members(archive.namelist()):
            try:
                tree = etree.fromstring(archive.read(name))
            except (etree.XMLSyntaxError, KeyError):
                continue
            for node in tree.iter():
                # Tags arrive namespaced as "{...}t"; compare the local name.
                if etree.QName(node).localname != tag:
                    continue
                value = node.get(attr) if attr else node.text
                if value and value.strip():
                    out.append(value.strip())
            if sum(len(s) for s in out) >= limit:
                break
    return out


def _slide_order(names: list[str]) -> list[str]:
    """Slide parts in deck order. The archive lists slide10 before slide2."""
    slides = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    return sorted(slides, key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)))


def _extract_pptx(path: Path) -> str | None:
    try:
        # "a:t" holds every run of text in a slide, titles included, in
        # document order — which is close enough to reading order for search.
        strings = _office_strings(path, _slide_order, "t", EXCERPT_MAX_CHARS)
    except Exception:
        return None
    return "\n".join(strings)[:EXCERPT_MAX_CHARS] or None


def _extract_xlsx(path: Path) -> str | None:
    try:
        # sharedStrings holds every distinct string in the workbook, which is
        # exactly its searchable text, without the numeric cell grid. Sheet
        # names come first because they are what people actually search for.
        sheets = _office_strings(
            path,
            lambda names: [n for n in names if n == "xl/workbook.xml"],
            "sheet",
            0,
            attr="name",
        )
        strings = _office_strings(
            path,
            lambda names: [n for n in names if n == "xl/sharedStrings.xml"],
            "t",
            EXCERPT_MAX_CHARS,
        )
    except Exception:
        return None
    return "\n".join([*sheets, *strings])[:EXCERPT_MAX_CHARS] or None


VISION_PROMPT = (
    "Transcribe all visible text in this image verbatim, including any title, "
    "headings, and body text. Then add one short line starting with 'Visual:' "
    "describing what the image is (e.g. a presentation slide, a code editor, a "
    "chat window, a photo of a receipt). Be concise and do not invent text that "
    "is not visible."
)


def downscale_image(path: Path, max_pixels: int | None = None) -> str | None:
    """Shrink an oversized image for the vision model, as base64 JPEG.

    Returns None when the image is already small enough or can't be read, in
    which case the caller should send the original file untouched.
    """
    import base64
    import io

    from PIL import Image

    from app.config import VISION_MAX_PIXELS

    cap = max_pixels or VISION_MAX_PIXELS
    try:
        with Image.open(path) as img:
            if max(img.size) <= cap:
                return None
            img = img.convert("RGB")
            img.thumbnail((cap, cap), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
    except Exception:
        return None
    return base64.b64encode(buf.getvalue()).decode()


def extract_image_text(path: Path, model: str | None = None) -> str | None:
    """OCR + describe an image with a local vision model.

    This is what makes screenshots searchable by content instead of filename.
    Much slower than text extraction, so callers opt in explicitly.
    """
    import ollama

    from app.config import VISION_MAX_TOKENS, VISION_MODEL
    from app.ollama_client import DECODING_OPTIONS

    image = downscale_image(path) or str(path)
    try:
        response = ollama.chat(
            model=model or VISION_MODEL,
            messages=[{"role": "user", "content": VISION_PROMPT, "images": [image]}],
            think=False,
            keep_alive="10m",
            # Greedy here too: the excerpt is the organizer's input, so a
            # description that changes between runs makes the folder it lands in
            # change with it.
            options={**DECODING_OPTIONS, "num_predict": VISION_MAX_TOKENS},
        )
    except Exception:
        return None
    text = (response["message"]["content"] or "").strip()
    return text[:IMAGE_EXCERPT_MAX_CHARS] or None


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def extract_excerpt(path: Path, vision_model: str | None = None) -> str | None:
    """Extract searchable text from a file.

    `vision_model` opts into reading image contents; without it images are
    indexed by filename only.
    """
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return _extract_txt(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".pptx":
        return _extract_pptx(path)
    if ext == ".xlsx":
        return _extract_xlsx(path)
    if vision_model and ext in IMAGE_EXTS:
        return extract_image_text(path, vision_model)
    return None

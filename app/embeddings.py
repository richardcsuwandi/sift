"""Local embeddings for semantic search.

FTS5 matches words. A question phrased in the user's vocabulary rather than the
document's ("the paper about tuning hyperparameters" for a file that says
"Bayesian optimization") produces no lexical overlap at all, so the model never
sees the right file no matter how good it is. Embeddings close that gap, and
fusing them with FTS5 keeps the exact-term matching that embeddings are bad at.

Everything here degrades to empty rather than raising: with no embedding model
installed, search quietly falls back to pure FTS5, the same way the app already
falls back when a model rejects `think=False`.
"""

import array
import math

from app.config import EMBED_DIMS, EMBED_MODEL, is_embed_model

# Qwen3-Embedding is trained with an instruction prefix on the query side only;
# documents are embedded bare. Skipping this costs real retrieval accuracy.
QUERY_INSTRUCTION = (
    "Instruct: Given a search query, retrieve the local file whose name or "
    "contents answer it\nQuery: "
)


def available_model(installed: list[str]) -> str | None:
    """The embedding model to use, or None if the user has not pulled one."""
    if EMBED_MODEL in installed:
        return EMBED_MODEL
    return next((m for m in installed if is_embed_model(m)), None)


def _normalize(vector: list[float]) -> list[float]:
    """Scale to unit length so cosine similarity is a plain dot product.

    Doing it once at write time means query-time scoring is one multiply-add per
    dimension, which is what makes a pure-Python scan fast enough to avoid a
    vector-index dependency.
    """
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed a batch, returning unit vectors. Empty list on any failure."""
    if not texts:
        return []
    import ollama

    try:
        response = ollama.embed(
            model=model or EMBED_MODEL,
            input=texts,
            dimensions=EMBED_DIMS,
            keep_alive="10m",
        )
    except Exception:
        return []
    return [_normalize(list(v)) for v in response.embeddings]


def embed_query(question: str, model: str | None = None) -> list[float] | None:
    vectors = embed_texts([QUERY_INSTRUCTION + question], model=model)
    return vectors[0] if vectors else None


def document_text(filename: str, excerpt: str | None) -> str:
    """What gets embedded for a file. The name carries real signal on its own,
    and is all there is for the file types nothing can extract."""
    return f"{filename}\n{excerpt}" if excerpt else filename


def pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack(blob: bytes) -> array.array:
    vector = array.array("f")
    vector.frombytes(blob)
    return vector


def rank(query: list[float], rows) -> list[tuple[str, float]]:
    """Score every stored vector against the query, best first.

    A linear scan, deliberately: at the scale a personal folder reaches, this is
    a few milliseconds, and an approximate index would mean a compiled
    dependency and a second thing that can fall out of sync with `files`.
    """
    scored = []
    for row in rows:
        vector = unpack(row["vec"])
        if len(vector) != len(query):
            # A different model or dimension wrote this row; it will be
            # rewritten on the next scan.
            continue
        scored.append((row["path"], sum(a * b for a, b in zip(query, vector))))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored

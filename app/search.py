import re
from pathlib import Path

from app.db import get_conn, search_files
from app.embeddings import available_model, embed_query
from app.ollama_client import chat_json, list_models

EXPAND_SYSTEM_PROMPT = """You turn a question about someone's files into search
terms for a keyword index.

Return the words most likely to appear in the file's name or text, including
obvious synonyms and both singular and plural forms — the index does no
stemming, so "invoice" and "invoices" are different terms. Expand abbreviations
and add the domain words an author would have used. Do not include words from
the question that carry no meaning on their own ("where", "file", "find").

Also list file extensions the answer is likely to have, lowercase and with the
dot, or an empty list when the question does not imply any.

Respond with ONLY a JSON object matching the supplied schema."""

EXPAND_MAX_TOKENS = 120

EXPAND_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": {"type": "string"}},
        "extensions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["terms", "extensions"],
    "additionalProperties": False,
}


def expand_query(question: str, model: str | None = None) -> str:
    """Rewrite a question as index terms, or return it unchanged on failure.

    FTS5 uses the default tokenizer, so it matches whole words and nothing else:
    no stemming, no synonyms. A question asked in the user's words rather than
    the document's therefore retrieves nothing at all. One small call recovers
    most of that, and costs about 0.3s with thinking off.
    """
    try:
        reply = chat_json(
            EXPAND_SYSTEM_PROMPT, question, model=model, schema=EXPAND_SCHEMA,
            # A dozen terms is plenty, and left uncapped the model pads the
            # list with near-duplicate phrasings that cost generation time and
            # add nothing to an OR query.
            num_predict=EXPAND_MAX_TOKENS,
        )
    except Exception:
        return question
    if not isinstance(reply, dict):
        return question
    terms = [str(t) for t in (reply.get("terms") or []) if str(t).strip()]
    # The original question stays in the query: the expansion is additive, and
    # a model that returns something unhelpful should not be able to make
    # retrieval worse than not asking it at all.
    return f"{question} {' '.join(terms)}" if terms else question

SYSTEM_PROMPT = """You find files in a user's local index. You will be given a
question and numbered candidate files (filename and a short content excerpt).

Return a final, user-facing result, never your reasoning, plan, or a description
of what you still need to do.

- Use only evidence in the candidates.
- Set status to "found" only when at least one candidate is a clear match.
- Cite files by their number, in "files". Cite only the ones that actually
  answer the question — usually one or two, never everything that looks
  related. Best match first.
- Keep "answer" to one or two sentences saying which file matches and why.
  Refer to files by their filename. The numbers are for the "files" list only:
  never write "file 3" or "[1]" in the answer, since the reader cannot see
  them.
- If the candidates are weak, ambiguous, or do not answer the question, set
  status to "not_found", use an empty "files" list, and plainly say that no
  relevant indexed file was found. Never guess.

Respond with ONLY a JSON object matching the supplied schema."""

NOT_FOUND_ANSWER = (
    "I couldn't find a relevant indexed file for that query. "
    "Try a filename, topic, or more specific phrase."
)

# Files are cited by number rather than by path. Two reasons: a path is ~30
# output tokens and citing ten of them was most of the answer's generation
# time, and a number outside the candidate range is trivially rejected, so
# there is no such thing as an invented path to defend against.
MAX_CITATIONS = 4

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["found", "not_found"]},
        "answer": {"type": "string"},
        "files": {
            "type": "array",
            "items": {"type": "integer"},
            "maxItems": MAX_CITATIONS,
        },
    },
    "required": ["status", "answer", "files"],
    "additionalProperties": False,
}


# Total excerpt characters to spend across all candidates. Split evenly, so
# a handful of strong matches each get shown in depth while a wide result set
# still fits in context. Image excerpts in particular carry real content, and
# truncating them too early hides the very text the question is about.
EXCERPT_BUDGET = 16000
MIN_PER_CANDIDATE = 200

# The budget is a latency trade, and prefill dominates: it is roughly 4k tokens
# of candidates, which fits ollama_client.NUM_CTX alongside the prompt. Fusing
# two retrievers is what makes a smaller budget affordable — the candidates are
# better ordered than a single bm25 ranking, so fewer of them are needed.
#
# How many files the answering model is shown. Retrieval returns more than this
# internally (see db.CANDIDATES) and fusion picks the best of them.
ASK_CANDIDATES = 12


# The candidates are numbered for the model's benefit, and a small model will
# refer to them by number in its prose no matter how firmly the prompt says not
# to. The reader never sees those numbers, so "the file [1] matches" reads as a
# broken reference. Asking is unreliable; removing them afterwards is not.
_CITATION_NOISE = re.compile(
    r"\s*\(\s*files?\s*#?\s*\d+\s*\)"   # "(file 3)"
    r"|\s*\[\s*\d+\s*\]"                 # "[1]"
    r"|\bfiles?\s+#?\d+\b"               # "file 3"
)


def _strip_citation_numbers(answer: str) -> str:
    return re.sub(r"\s{2,}", " ", _CITATION_NOISE.sub("", answer)).strip()


def ask(question: str, model: str | None = None, root: str | Path | None = None) -> dict:
    installed = list_models()
    embed_model = available_model(installed)
    # Both are best-effort: with no embedding model the search is lexical only,
    # and if expansion fails the raw question is used.
    query_vector = embed_query(question, model=embed_model) if embed_model else None
    lexical_query = expand_query(question, model=model)

    with get_conn() as conn:
        rows = search_files(
            conn, question, limit=200 if root else ASK_CANDIDATES,
            query_vector=query_vector, lexical_query=lexical_query,
        )

    if root:
        selected = Path(root).resolve()
        rows = [
            row for row in rows
            if Path(row["path"]).is_relative_to(selected)
        ][:ASK_CANDIDATES]

    if not rows:
        return {
            "answer": NOT_FOUND_ANSWER,
            "referenced_paths": [],
            "results": [],
        }

    per_candidate = max(MIN_PER_CANDIDATE, EXCERPT_BUDGET // len(rows))
    candidates = "\n".join(
        f'[{i}] filename: "{r["filename"]}", excerpt: "{(r["excerpt"] or "")[:per_candidate]}"'
        for i, r in enumerate(rows, start=1)
    )
    user_prompt = f'question: "{question}"\n\ncandidate files:\n{candidates}'

    try:
        result = chat_json(
            SYSTEM_PROMPT, user_prompt, model=model, schema=ANSWER_SCHEMA
        )
    except ValueError:
        return {
            "answer": "The model didn't return a usable answer. Try rephrasing the question.",
            "referenced_paths": [],
            "results": [],
        }
    # A small model may claim success without citing a file, or cite a number
    # that was never offered. Treat both as no result: the Find UI should never
    # display an unsupported or still-in-progress-sounding answer.
    cited = result.get("files")
    if not isinstance(cited, list):
        cited = []
    numbers = list(dict.fromkeys(
        n for n in cited if isinstance(n, int) and 1 <= n <= len(rows)
    ))[:MAX_CITATIONS]
    references = [rows[n - 1]["path"] for n in numbers]

    if result.get("status") != "found" or not references:
        return {"answer": NOT_FOUND_ANSWER, "referenced_paths": [], "results": []}

    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        answer = "I found a relevant indexed file."
    answer = _strip_citation_numbers(answer)
    # Evidence for each cited file, in the order the model cited them, so the
    # UI can show why a file was returned rather than asking the user to trust
    # the answer. referenced_paths stays for anything reading the old shape.
    results = [
        {
            "path": rows[n - 1]["path"],
            "filename": rows[n - 1]["filename"],
            "ext": rows[n - 1].get("ext") or "",
            "snippet": (rows[n - 1].get("snippet") or "").strip(),
        }
        for n in numbers
    ]
    return {"answer": answer.strip(), "referenced_paths": references, "results": results}

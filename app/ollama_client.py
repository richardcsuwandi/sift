import json
import os

import ollama

from app.config import OLLAMA_MODEL


KEEP_ALIVE = "10m"

# Ollama defaults to temperature 0.8 / top_p 0.9, which is tuned for
# conversation, not for classification. Organizing a folder asks the same
# question of hundreds of near-identical files ("Screenshot 2026-06-10 at
# 00.14.08.png" differs from its neighbour only in the digits), so sampled
# decoding answers a meaningful fraction of them differently for no reason: a
# tenth of a batch of screenshots lands outside the Screenshots folder, and the
# same filename pattern comes back rewritten five different ways. Greedy
# decoding makes identical inputs give identical answers, which is the whole
# job here. The seed only matters for runtimes that ignore temperature 0.
DECODING_OPTIONS = {"temperature": 0, "top_p": 1, "seed": 0}

# Context window, applied to every call rather than per call.
#
# Ollama defaults to 4096 tokens and silently drops the overflow, which is too
# small for the "ask" flow's candidate list. But it also keys its loaded-model
# cache on the context size: asking the same model for 4096 on one call and
# 8192 on the next unloads and reloads several GB between them. Varying it per
# call cost more than twenty seconds per question in reloads alone, which is
# why this is one number for the whole app instead of an argument.
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

# Models that rejected think=False once; don't keep retrying it for them.
_NO_THINK_SUPPORT: set[str] = set()


def _chat(
    model: str,
    messages: list[dict],
    schema: dict | None = None,
    num_predict: int | None = None,
):
    fmt = schema if schema else "json"
    options = {**DECODING_OPTIONS, "num_ctx": NUM_CTX}
    if num_predict:
        options["num_predict"] = num_predict
    if model not in _NO_THINK_SUPPORT:
        try:
            return ollama.chat(
                model=model,
                messages=messages,
                format=fmt,
                think=False,
                keep_alive=KEEP_ALIVE,
                options=options,
            )
        except Exception as exc:
            if "think" not in str(exc).lower():
                raise
            _NO_THINK_SUPPORT.add(model)
    return ollama.chat(
        model=model,
        messages=messages,
        format=fmt,
        keep_alive=KEEP_ALIVE,
        options=options,
    )


def list_models() -> list[str]:
    """Names of locally pulled Ollama models, for the UI's model picker."""
    try:
        response = ollama.list()
    except Exception:
        return []
    return sorted(m.model for m in response.models)


def chat_json(
    system: str,
    user: str,
    model: str | None = None,
    schema: dict | None = None,
    retries: int = 1,
    num_predict: int | None = None,
) -> dict:
    """Call a local Ollama model and parse a JSON object out of the reply.

    Passing a JSON `schema` uses Ollama's structured outputs, which constrains
    decoding to that shape. That matters most for small models: an `enum` on a
    field makes an invalid category impossible rather than merely discouraged.

    `think=False` matters enormously here: reasoning models (Qwen3, DeepSeek-R1,
    …) default to emitting a long chain-of-thought before every answer. For the
    short structured JSON we want, that's ~18x slower for no quality gain.
    Models without a thinking mode reject the option, so it's retried without.

    `keep_alive` holds the model in memory between calls, since organizing a
    folder makes one call per file back to back.

    `num_predict` caps generation for calls whose useful output is short; the
    context window is fixed for every call, see NUM_CTX.

    Retries once with a stricter reminder if the first reply isn't valid JSON,
    since small local models occasionally wrap JSON in prose or markdown fences.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        response = _chat(model or OLLAMA_MODEL, messages, schema, num_predict=num_predict)
        content = response["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last_error = exc
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": "That was not valid JSON. Reply with ONLY the JSON object, no other text.",
                }
            )
    raise ValueError(f"Model did not return valid JSON after {retries + 1} attempts") from last_error

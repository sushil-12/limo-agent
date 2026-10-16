"""Fit chat history into the model's context window.

Token counts are estimated, not exact. The estimate is deliberately conservative
(about 3 characters per token, while Llama averages nearer 4 for English), so
trimming happens a little early rather than Ollama silently truncating the prompt.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.llm.base import LLMMessage

CHARS_PER_TOKEN = 3
PER_MESSAGE_OVERHEAD_TOKENS = 8


def estimate_tokens(text: str) -> int:
    """Rough token estimate for one message, including role/template overhead."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) + PER_MESSAGE_OVERHEAD_TOKENS


def fit_history(
    system_prompt: str,
    history: Sequence[LLMMessage],
    *,
    num_ctx: int,
    reserved_for_reply: int,
) -> list[LLMMessage]:
    """Return the system message plus the newest history that fits.

    The oldest messages are dropped first. The latest message is always kept: it is
    at most 4,000 characters, which fits any supported ``num_ctx``.
    """
    budget = num_ctx - reserved_for_reply - estimate_tokens(system_prompt)
    kept: list[LLMMessage] = []
    for message in reversed(history):
        cost = estimate_tokens(message.content)
        if kept and cost > budget:
            break
        kept.append(message)
        budget -= cost
    kept.reverse()
    return [LLMMessage("system", system_prompt), *kept]

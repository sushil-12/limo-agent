"""Pre-check that keeps the assistant on 1800Limo topics and spots data questions.

A short, deterministic classification call (a couple of output tokens) runs before
the real reply. Off-topic messages get a fixed reply, so they never reach the full
generation. The system prompt repeats the rule as a second line of defence. For users
who may query data, the same call also answers DATA when a database lookup is needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from app.core.logging import get_logger
from app.llm.base import LLMMessage, LLMProvider
from app.services.prompts import ROUTER_PROMPT, TOPIC_CLASSIFIER_PROMPT, build_classifier_input

logger = get_logger(__name__)

_MAX_TOKENS = 3

Verdict = Literal["on", "off", "data"]


class TopicGuard:
    """Asks the model whether a message is in scope. Fails open on unclear answers.

    With ``block_off_topic=False`` the guard only routes data questions, and OFF is
    treated as ON.
    """

    def __init__(self, provider: LLMProvider, *, block_off_topic: bool = True) -> None:
        self._provider = provider
        self._block_off_topic = block_off_topic

    async def is_on_topic(self, message: str, previous_reply: str | None = None) -> bool:
        """True unless the model clearly answers OFF. Raises LLMError if the call fails."""
        return await self.classify(message, previous_reply) != "off"

    async def classify(
        self,
        message: str,
        previous_reply: str | None = None,
        *,
        allow_data: bool = False,
        earlier: Sequence[str] = (),
    ) -> Verdict:
        """Return "on", "off" or (only when ``allow_data``) "data". Raises LLMError if the call fails.

        ``earlier`` are the user's previous questions: a short follow-up ("how many in
        washington") is only clearly about 1800Limo next to them.
        """
        if not self._block_off_topic and not allow_data:
            return "on"
        content = build_classifier_input(message, previous_reply, allow_data=allow_data, earlier=earlier)
        result = await self._provider.chat(
            [
                LLMMessage("system", ROUTER_PROMPT if allow_data else TOPIC_CLASSIFIER_PROMPT),
                LLMMessage("user", content),
            ],
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
        )
        verdict = result.content.strip().upper()
        if allow_data and verdict.startswith("DATA"):
            return "data"
        if verdict.startswith("OFF"):
            return "off" if self._block_off_topic else "on"
        if not verdict.startswith("ON"):
            logger.warning("topic_guard_unclear", verdict=verdict[:20])
        return "on"

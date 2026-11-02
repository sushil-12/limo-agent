"""Masks personal details before text leaves for a hosted model.

Card numbers, phone numbers and email addresses are replaced with placeholders. Names and
addresses stay: the booking needs them read. Everything sent to a hosted provider passes
through ``redact``, so chat history, booking summaries and lookup results are all covered.
"""

from __future__ import annotations

import re

CARD = "[card number]"
PHONE = "[phone number]"
EMAIL = "[email]"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 13-19 digits, optionally grouped by spaces or dashes ("4242 4242 4242 4242").
_CARD = re.compile(r"(?<![\w.])\d(?:[ -]?\d){12,18}(?![\w.])")
# A run of digits with phone punctuation: "+1 (312) 555-0199", "312.555.0199", "9876543210".
_PHONE = re.compile(r"(?<![\w.])\+?\(?\d[\d ().-]{6,}\d(?![\w])")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}")
_DECIMAL = re.compile(r"\.\d{5,}")  # coordinates such as 41.878113


def redact(text: str) -> str:
    """``text`` with card numbers, phone numbers and emails masked."""
    text = _EMAIL.sub(EMAIL, text)
    text = _CARD.sub(CARD, text)
    return _PHONE.sub(_phone, text)


def _phone(match: re.Match[str]) -> str:
    value = match.group(0)
    digits = sum(c.isdigit() for c in value)
    if _DATE.search(value) or _DECIMAL.search(value):
        return value
    wanted = 8 if value.startswith("+") else 10
    return PHONE if wanted <= digits <= 15 else value

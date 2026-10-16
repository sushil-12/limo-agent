"""Who a chat turn is for: a signed-in Laravel user or an anonymous guest."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from dataclasses import dataclass
from typing import Any

from app.auth.models import CurrentUser

_NAME_UNSAFE = re.compile(r"[^\w\s'\-.]", re.UNICODE)
_MAX_NAME_LENGTH = 40
# "my name is Rahul" -> "Rahul". Guests answer the name question in free text.
_NAME_PREFIX = re.compile(
    r"^\s*(?:hi|hello|hey)?[\s,!.]*(?:my\s+name\s+is|my\s+name's|name\s*[:\-]|i\s+am|i'm|im|this\s+is|"
    r"it's|its|call\s+me)\s+",
    re.IGNORECASE,
)

# Per-process salt: guest keys correlate requests within a process without being
# reversible to an IP address in logs.
_GUEST_SALT = secrets.token_bytes(16)


def safe_display_name(raw: str | None) -> str | None:
    """Strip characters that could be used for prompt injection via a profile name."""
    if not raw:
        return None
    cleaned = " ".join(_NAME_UNSAFE.sub("", raw).split())[:_MAX_NAME_LENGTH]
    return cleaned or None


def guest_display_name(raw: str | None) -> str | None:
    """Turn a guest's free-text answer to "what's your name?" into a display name."""
    if not raw:
        return None
    return safe_display_name(_NAME_PREFIX.sub("", raw).strip(" .!,"))


def guest_key(client_ip: str | None) -> str:
    """Rate-limit and log key for a guest. IPv6 is grouped by /64, the usual per-customer block."""
    try:
        address = ipaddress.ip_address(client_ip or "")
    except ValueError:
        network = client_ip or "unknown"
    else:
        network = (
            str(ipaddress.ip_network(f"{address}/64", strict=False)) if address.version == 6 else str(address)
        )
    digest = hashlib.blake2b(network.encode(), key=_GUEST_SALT, digest_size=8).hexdigest()
    return f"g_{digest}"


@dataclass(frozen=True, slots=True)
class Requester:
    """The identity a reply is personalised and rate limited for."""

    role: str
    display_name: str | None
    user_id: int | None = None
    guest_id: str | None = None

    @property
    def is_guest(self) -> bool:
        return self.user_id is None

    @classmethod
    def from_user(cls, user: CurrentUser) -> Requester:
        name = safe_display_name(user.first_name) or safe_display_name((user.name or "").split(" ")[0])
        return cls(role=user.role, display_name=name, user_id=user.id)

    @classmethod
    def guest(cls, key: str, raw_name: str | None) -> Requester:
        return cls(role="guest", display_name=guest_display_name(raw_name), guest_id=key)

    def log_fields(self) -> dict[str, Any]:
        """Identity fields for structured logs (never the raw IP or name)."""
        if self.is_guest:
            return {"guest_id": self.guest_id, "role": self.role}
        return {"user_id": self.user_id, "role": self.role}

"""Wire format v1: one JSON envelope for every frame, the same on iOS, Android and web.

    {"v":1,"type":"…","id":"…","ts":<server ms>,"ride_id":"2327"|null,"seq":<int>|null,"data":{…}}

Durable ride events (status, chat, notices) carry a per-ride ``seq`` and can be replayed;
location frames have no seq (latest wins). Clients ignore unknown types and fields.
"""

from __future__ import annotations

import math
import secrets
import time
from typing import Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024

ErrorCode = Literal[
    "unauthorized",
    "forbidden_ride",
    "share_expired",
    "rate_limited",
    "bad_payload",
    "hello_required",
    "server_error",
]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    """Sortable-enough unique id: ms timestamp + random suffix."""
    return f"{now_ms():012x}{secrets.token_hex(5)}"


def envelope(
    type_: str,
    data: dict[str, Any] | None = None,
    *,
    ride_id: int | str | None = None,
    seq: int | None = None,
    ts: int | None = None,
) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": type_,
        "id": new_id(),
        "ts": ts if ts is not None else now_ms(),
        "ride_id": str(ride_id) if ride_id is not None else None,
        "seq": seq,
        "data": data or {},
    }


def dumps(message: dict[str, Any]) -> str:
    return orjson.dumps(message).decode()


def error_frame(code: ErrorCode, *, retry: bool = False, ride_id: int | None = None) -> str:
    return dumps(envelope("error", {"code": code, "retry": retry}, ride_id=ride_id))


class ProtocolError(Exception):
    """A frame the server can't accept; the connection gets an ``error`` frame."""

    def __init__(self, code: ErrorCode, *, retry: bool = False) -> None:
        super().__init__(code)
        self.code: ErrorCode = code
        self.retry = retry


# --- Inbound frames ---------------------------------------------------------------------


class _Frame(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Inbound(_Frame):
    v: int = PROTOCOL_VERSION
    type: str = Field(min_length=1, max_length=40)
    ride_id: int | None = None
    seq: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ride_id", mode="before")
    @classmethod
    def _ride_id_from_string(cls, value: object) -> object:
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value


class HelloData(_Frame):
    token: str = Field(min_length=10, max_length=8192)
    app: Literal["driver", "rider", "web", "admin"]
    platform: str = Field(default="unknown", max_length=16)
    app_version: str | None = Field(default=None, max_length=32)
    device_id: str | None = Field(default=None, max_length=128)


class SubscribeData(_Frame):
    last_seq: int = Field(default=0, ge=0)


class AppStateData(_Frame):
    state: Literal["foreground", "background"]


class LocationData(_Frame):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    acc_m: float | None = Field(default=None, ge=0, le=10_000)
    speed_mps: float | None = Field(default=None, ge=-1, le=120)
    heading: float | None = Field(default=None, ge=-1, le=360)
    gps_ts: int = Field(gt=0)  # driver phone clock, epoch ms
    epoch: str = Field(min_length=1, max_length=64)  # new value per app launch
    seq: int = Field(ge=0)
    src: Literal["nav_snapped", "raw"] = "raw"

    @field_validator("lat", "lng")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value) or value == 0.0:
            raise ValueError("coordinate must be a finite, non-zero number")
        return value


def parse_frame(raw: str | bytes) -> Inbound:
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("bad_payload")
    try:
        frame = Inbound.model_validate(orjson.loads(raw))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        raise ProtocolError("bad_payload") from exc
    if frame.v != PROTOCOL_VERSION:
        raise ProtocolError("bad_payload")
    return frame


def parse_data[T: BaseModel](model: type[T], data: dict[str, Any]) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ProtocolError("bad_payload") from exc

"""Firebase Cloud Messaging over the HTTP v1 API, without the Firebase SDK.

Auth is the service-account flow in ``google_auth``. Personal pushes go to device tokens;
topics are only for broadcasts. A token FCM reports as unregistered is returned as such,
so the caller can revoke it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from app.core.logging import get_logger
from app.realtime.google_auth import SCOPE_FCM, ServiceAccountTokens

logger = get_logger(__name__)

SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"


class SendOutcome(StrEnum):
    SENT = "sent"
    INVALID_TOKEN = "invalid_token"  # noqa: S105 - revoke it
    FAILED = "failed"  # transient; already retried
    DISABLED = "disabled"  # FCM not configured


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    data: dict[str, str]
    high_priority: bool = True
    collapse_key: str | None = None


class FcmClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        project_id: str | None,
        credentials: dict[str, str] | None,
    ) -> None:
        self._client = client
        self._project = project_id
        self._tokens = ServiceAccountTokens(client, credentials, SCOPE_FCM) if credentials else None

    @property
    def enabled(self) -> bool:
        return bool(self._project and self._tokens)

    async def send_to_token(self, token: str, message: PushMessage) -> SendOutcome:
        return await self._send({"token": token}, message)

    async def send_to_topic(self, topic: str, message: PushMessage) -> SendOutcome:
        return await self._send({"topic": topic}, message)

    async def _send(self, target: dict[str, str], message: PushMessage) -> SendOutcome:
        if self._tokens is None or not self._project:
            logger.info("rt_push_skipped_fcm_disabled")
            return SendOutcome.DISABLED
        body = {"message": {**target, **_platform_payload(message)}}
        url = SEND_URL.format(project=self._project)
        for attempt in range(3):
            try:
                access = await self._tokens.get()
                response = await self._client.post(
                    url, json=body, headers={"Authorization": f"Bearer {access}"}, timeout=10.0
                )
            except httpx.HTTPError as exc:
                logger.warning("rt_push_transport_error", attempt=attempt, error=type(exc).__name__)
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code == 200:
                return SendOutcome.SENT
            if response.status_code == 401:
                self._tokens.invalidate()  # expired early; refresh and retry
                continue
            error = _error_code(response)
            # INVALID_ARGUMENT also covers bad payloads; only a bad token may revoke it.
            bad_token = error == "UNREGISTERED" or (
                error == "INVALID_ARGUMENT" and "registration token" in response.text.lower()
            )
            if "token" in target and bad_token:
                return SendOutcome.INVALID_TOKEN
            if response.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            logger.error("rt_push_rejected", status=response.status_code, error=error)
            return SendOutcome.FAILED
        return SendOutcome.FAILED


def _platform_payload(message: PushMessage) -> dict[str, Any]:
    priority = "high" if message.high_priority else "normal"
    android: dict[str, Any] = {"priority": priority, "notification": {"sound": "default"}}
    apns_headers = {"apns-priority": "10" if message.high_priority else "5"}
    if message.collapse_key:
        android["collapse_key"] = message.collapse_key
        apns_headers["apns-collapse-id"] = message.collapse_key[:64]
    return {
        "notification": {"title": message.title, "body": message.body},
        "data": message.data,
        "android": android,
        "apns": {"headers": apns_headers, "payload": {"aps": {"sound": "default"}}},
    }


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return None
    for detail in error.get("details", []) or []:
        if isinstance(detail, dict) and detail.get("errorCode"):
            return str(detail["errorCode"])
    return error.get("status")

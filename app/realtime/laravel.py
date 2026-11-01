"""Outbound calls to Laravel (limoapi): SMS only. Twilio config and templates stay in PHP.

* ``POST sms/send``: existing endpoint, used for the capped SMS fallback
  (message_type ``ride_status``).
* ``POST internal/rides/{id}/completed-sms``: new internal endpoint (Laravel change C6)
  that dispatches ``SendReservationSms::dispatchRideCompleted``, which Laravel's own status
  endpoint did inline until now.

Both send the internal secret in ``x-secret``.
"""

from __future__ import annotations

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class LaravelClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        secret: str,
        ride_completed_path: str,
        timeout: float = 8.0,
    ) -> None:
        self._client = client
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        self._secret = secret
        self._completed_path = ride_completed_path
        self._timeout = timeout

    async def send_ride_sms(self, ride_id: int, text: str, customer_user_id: int | None) -> bool:
        body: dict[str, object] = {
            "booking_id": ride_id,
            "message_content": text,
            "message_type": "ride_status",
        }
        if customer_user_id:
            body["customer_id"] = customer_user_id
        return await self._post("sms/send", body, what="sms_fallback", ride_id=ride_id)

    async def ride_completed_sms(self, ride_id: int) -> bool:
        path = self._completed_path.format(ride_id=ride_id)
        return await self._post(path, {"reservation_id": ride_id}, what="ride_completed_sms", ride_id=ride_id)

    async def _post(self, path: str, body: dict[str, object], *, what: str, ride_id: int) -> bool:
        url = self._base + path.lstrip("/")
        for attempt in range(3):
            try:
                response = await self._client.post(
                    url,
                    json=body,
                    headers={"x-secret": self._secret, "Accept": "application/json"},
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    f"rt_laravel_{what}_transport", ride_id=ride_id, attempt=attempt, error=type(exc).__name__
                )
                continue
            if response.is_success:
                logger.info(f"rt_laravel_{what}_sent", ride_id=ride_id)
                return True
            if response.status_code < 500:
                logger.error(f"rt_laravel_{what}_rejected", ride_id=ride_id, status=response.status_code)
                return False
        logger.error(f"rt_laravel_{what}_failed", ride_id=ride_id)
        return False

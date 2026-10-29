"""The 1800limo Laravel API, called with the signed-in user's Passport token.

Paths are relative to LIMO_API_BASE_URL (the web app's ``environment.serverUrl``). The
token is sent as ``Authorization: Bearer`` exactly like the web app's interceptor and is
never logged or included in an error message.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)
# TEMP: numbers every Laravel API call this process makes (trace duplicate edit-reservation calls)
_request_counter = itertools.count(1)


class ApiError(Exception):
    """The API refused a request. ``errors`` is Laravel's per-field map (422 ``data``)."""

    def __init__(
        self, message: str, *, status: int | None = None, errors: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.errors = errors or {}

    def plain(self) -> str:
        """The error in words a user can act on."""
        if not self.errors:
            return self.message
        details = "; ".join(f"{_field_words(k)}: {v}" for k, v in self.errors.items())
        return details if self.message in ("Validation Error", "Error", "") else f"{self.message} ({details})"


class ApiUnreachableError(ApiError):
    """No response arrived, so whether the server acted is unknown."""


@dataclass(frozen=True, slots=True)
class RolePaths:
    """Endpoints per web portal (BOOKING_FLOW.md §2)."""

    mode: str  # admin | individual | travel_agent
    create_reservation: str
    add_credit_card: str
    get_reservation: str  # one booking, prefilled for editing (EDIT_BOOKING_FLOW.md §1), {id}
    edit_reservation: str
    edit_method: str  # the individual portal POSTs its edit; the others PUT
    accounts: str = ""  # list of customer accounts the role books for
    account: str = ""  # one account's details, {id}


ROLE_PATHS = {
    "admin": RolePaths(
        mode="admin",
        create_reservation="create-reservation",
        add_credit_card="add-credit-card",
        get_reservation="get-reservation/{id}/edit",
        edit_reservation="edit-reservation",
        edit_method="PUT",
        accounts="get-account-by-type/individual",
        account="get-an-account/{id}",
    ),
    "individual": RolePaths(
        mode="individual",
        create_reservation="individual/create-reservation",
        add_credit_card="individual/add-credit-card",
        get_reservation="individual/get-reservation/{id}/edit",
        edit_reservation="individual/edit-reservation",
        edit_method="POST",
        account="individual/get-account-details",
    ),
    "travel_agent": RolePaths(
        mode="travel_agent",
        create_reservation="travel-planner/create-reservation",
        add_credit_card="travel-planner/add-credit-card",
        get_reservation="travel-planner/get-reservation/{id}/edit",
        edit_reservation="travel-planner/edit-reservation",
        edit_method="PUT",
        accounts="travel-planner/get-account-by-type/individual",
        account="travel-planner/get-an-account/{id}",
    ),
}

# users.role names (AUTH_ALLOWED_ROLES) -> web portal. Other roles cannot book from the shell.
BOOKING_MODES = {
    "admin": "admin",
    "sub_admin": "admin",
    "rider": "individual",
    "travel_agent": "travel_agent",
    "sub_travel_agent": "travel_agent",
}


@dataclass
class LimoApi:
    base_url: str
    token: str = field(repr=False)
    client: httpx.AsyncClient = field(repr=False)
    timeout: float = 30.0

    async def get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        return await self._request("POST", path, body)

    async def put(self, path: str, body: dict[str, Any]) -> Any:
        return await self._request("PUT", path, body)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        call_no = next(_request_counter)
        logger.info("limo_api_request", call_no=call_no, method=method, path=path)
        started = time.monotonic()
        try:
            response = await self.client.request(
                method, url, json=body, headers=headers, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            logger.info("limo_api_unreachable", call_no=call_no, method=method, path=path, error=type(exc).__name__)
            raise ApiUnreachableError(f"no response from the booking API ({type(exc).__name__})") from None
        logger.info(
            "limo_api_response",
            call_no=call_no,
            method=method,
            path=path,
            status=response.status_code,
            ms=round((time.monotonic() - started) * 1000),
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("success") is False:
            raise _error(response.status_code, payload)
        return payload

    async def master_vehicle_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post("quote/master-vehicle-listing", payload)

    async def vehicle_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post("quote/vehicle-listing", payload)

    async def vehicle_type_ids(self) -> dict[str, int]:
        """``GET quote/filters``: vehicle class name -> vehicle-type filter id (embedded-quote.ts:73)."""
        data = (await self.get("quote/filters")).get("data") or {}
        return {t["name"]: t["id"] for t in data.get("vehicle-type") or [] if t.get("name")}

    async def booking_rates(self, booking_data: dict[str, Any]) -> dict[str, Any]:
        return (await self.post("admin/booking-rates-vehicle", booking_data)).get("data") or {}

    async def master_vehicle_info(self, vehicle_id: int) -> dict[str, Any]:
        return (await self.get(f"get-master-vehicle-info/{vehicle_id}")).get("data") or {}

    async def reservation_rates(self, reservation_id: int) -> dict[str, Any]:
        """The rate lines and totals saved on a booking (admin only, ``admin.service.ts:1250``)."""
        return (await self.get(f"admin/reservation-rates/{reservation_id}")).get("data") or {}

    async def common_data(self) -> dict[str, Any]:
        """Airlines and meet & greet choices (``createBookingGetData``)."""
        return (await self.get("amenities-languages-dresses")).get("data") or {}


def _error(status: int, payload: Any) -> ApiError:
    if not isinstance(payload, dict):
        return ApiError(f"the booking API answered HTTP {status}", status=status)
    message = str(payload.get("message") or f"HTTP {status}")
    data = payload.get("data")
    errors: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, list):
                value = value[0] if value else ""
            if isinstance(value, str | int | float):
                errors[str(key)] = str(value)
    return ApiError(message, status=status, errors=errors)


def _field_words(key: str) -> str:
    return key.replace(".", " ").replace("_", " ")

"""Google Places and Directions over REST, the server-side equivalents of the web app's calls.

The web app uses Places Autocomplete (``places:autocomplete`` v1) + place details for every
address field, and the Directions service with route alternatives, keeping the shortest
(booking.ts ``drawMap``). The key is GOOGLE_MAPS_API_KEY from limoagent's .env, never the
web app's browser key, and it is never printed.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
_DETAIL_FIELDS = "id,displayName,formattedAddress,location,addressComponents,types"


class MapsError(Exception):
    """Google could not answer; the message is safe to show (it never contains the key)."""


@dataclass(frozen=True, slots=True)
class Suggestion:
    place_id: str
    text: str
    types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlaceDetails:
    place_id: str
    name: str
    formatted_address: str
    lat: float
    lng: float
    types: tuple[str, ...]
    country_names: tuple[str, ...]  # long names of every address component (currency lookup)

    @property
    def is_airport(self) -> bool:
        return "airport" in self.types

    @property
    def airport_label(self) -> str:
        """``getAirportSelectionLabel`` (booking.ts:4583): "<name> - <address>"."""
        if not self.name:
            return self.formatted_address
        if self.formatted_address == self.name or self.formatted_address.startswith(f"{self.name} - "):
            return self.formatted_address
        return f"{self.name} - {self.formatted_address}"


@dataclass(frozen=True, slots=True)
class Route:
    meters: int
    seconds: int


class GoogleMaps:
    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._key = api_key
        self._client = client

    async def autocomplete(self, text: str) -> list[Suggestion]:
        data = await self._call(
            "POST", _AUTOCOMPLETE_URL, json={"input": text}, headers={"X-Goog-Api-Key": self._key}
        )
        found = []
        for item in data.get("suggestions") or []:
            prediction = item.get("placePrediction")
            if not prediction or not prediction.get("placeId"):
                continue
            found.append(
                Suggestion(
                    place_id=prediction["placeId"],
                    text=(prediction.get("text") or {}).get("text", ""),
                    types=tuple(prediction.get("types") or ()),
                )
            )
        return found

    async def details(self, place_id: str) -> PlaceDetails:
        data = await self._call(
            "GET",
            _DETAILS_URL.format(place_id=place_id),
            headers={"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": _DETAIL_FIELDS},
        )
        location = data.get("location") or {}
        if "latitude" not in location or "longitude" not in location:
            raise MapsError("Google returned that place without a location")
        return PlaceDetails(
            place_id=data.get("id", place_id),
            name=(data.get("displayName") or {}).get("text", ""),
            formatted_address=data.get("formattedAddress", ""),
            lat=float(location["latitude"]),
            lng=float(location["longitude"]),
            types=tuple(data.get("types") or ()),
            country_names=tuple(c.get("longText", "") for c in data.get("addressComponents") or ()),
        )

    async def route(self, points: list[tuple[float, float]]) -> Route:
        """Driving route through ``points`` in order; the shortest alternative, legs summed."""
        if len(points) < 2:
            raise MapsError("a route needs a start and an end")
        params = {
            "origin": _latlng(points[0]),
            "destination": _latlng(points[-1]),
            "alternatives": "true",
            "key": self._key,
        }
        if len(points) > 2:
            params["waypoints"] = "|".join(_latlng(p) for p in points[1:-1])
        data = await self._call("GET", _DIRECTIONS_URL, params=params)
        if data.get("status") != "OK" or not data.get("routes"):
            raise MapsError(f"no driving route found ({data.get('status', 'unknown status')})")
        totals = [
            (
                sum(leg["distance"]["value"] for leg in route["legs"]),
                sum(leg["duration"]["value"] for leg in route["legs"]),
            )
            for route in data["routes"]
        ]
        meters, seconds = min(totals, key=lambda total: total[0])
        return Route(meters=meters, seconds=seconds)

    async def _call(self, method: str, url: str, **kwargs: object) -> dict:
        try:
            response = await self._client.request(method, url, timeout=15.0, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise MapsError(f"Google Maps unreachable ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise MapsError(f"Google Maps answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise MapsError("Google Maps sent a response that isn't JSON") from None


def _latlng(point: tuple[float, float]) -> str:
    return f"{point[0]},{point[1]}"

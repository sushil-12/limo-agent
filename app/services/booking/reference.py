"""Static lookups the web app ships as assets: the airport list and the currency list.

data/airports.json and data/currencyOptions.json are copies of
1800-limo/src/assets/json/{airports,currencyOptions}.json. Copy them again when the web
app's lists change.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).parent / "data"
FBO_AIRPORT_ID = 3283  # "Fixed Base Operator (Private plane)": no coordinates, never matched by location


@dataclass(frozen=True, slots=True)
class Airport:
    id: int
    code: str
    name: str
    city: str
    country: str
    lat: float | None
    long: float | None

    @property
    def label(self) -> str:
        return f"{self.code} - {self.name}, {self.city}"


class AirportIndex:
    """The web app's ``BigData.airportsData``: internal airport ids, codes and coordinates."""

    def __init__(self, airports: Iterable[Airport]) -> None:
        self.airports = [a for a in airports if a.id != FBO_AIRPORT_ID]
        self._by_code = {a.code.upper(): a for a in self.airports if a.code}
        self._by_id = {a.id: a for a in self.airports}

    @classmethod
    def load(cls, path: Path = _DATA / "airports.json") -> AirportIndex:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            Airport(
                id=int(r["id"]),
                code=str(r.get("code") or ""),
                name=str(r.get("name") or ""),
                city=str(r.get("city") or ""),
                country=str(r.get("country") or ""),
                lat=_number(r.get("lat")),
                long=_number(r.get("long")),
            )
            for r in rows
        )

    def by_code(self, code: str) -> Airport | None:
        return self._by_code.get(code.strip().upper())

    def by_id(self, airport_id: Any) -> Airport | None:
        try:
            return self._by_id.get(int(airport_id))
        except (TypeError, ValueError):
            return None

    def in_city(self, city: str) -> list[Airport]:
        wanted = " ".join(city.lower().split())
        return [a for a in self.airports if a.city.lower() == wanted and a.lat is not None]

    def nearest(self, lat: float, lng: float) -> Airport | None:
        """``resolveInternalAirportRecord`` (booking.ts:4635): the airport closest to a point."""
        best: Airport | None = None
        best_km = math.inf
        for airport in self.airports:
            if airport.lat is None or airport.long is None:
                continue
            km = _haversine_km(lat, lng, airport.lat, airport.long)
            if km < best_km:
                best, best_km = airport, km
        return best


@lru_cache
def airport_index() -> AirportIndex:
    return AirportIndex.load()


@lru_cache
def _currencies() -> dict[str, dict[str, Any]]:
    return json.loads((_DATA / "currencyOptions.json").read_text(encoding="utf-8"))


def currency_for(country_names: Iterable[str]) -> dict[str, Any] | None:
    """``fillAddress`` / ``updateCurrencyFromAirportPlace``: the currency whose countryName is one of
    the place's address components. The web loop keeps the last match."""
    names = set(country_names)
    match = None
    for entry in _currencies().values():
        if entry.get("countryName") in names:
            match = entry
    return match


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def currency_symbol(code: Any) -> str | None:
    """The symbol for a saved booking's currency code (``tbl_order.currency``, e.g. ``usd``)."""
    wanted = str(code or "").upper()
    for entry in _currencies().values():
        if wanted and str(entry.get("currency") or "").upper() == wanted:
            return entry.get("symbol")
    return None

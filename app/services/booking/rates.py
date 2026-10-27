"""Totals and revenue shares, computed exactly as the web app does before it saves a booking.

The backend stores ``grand_total``, ``sub_total``, ``rateArray`` and ``shares_array`` as sent
(BOOKING_FLOW.md §5.2), so these must match the web app to the cent:

- line amounts and totals: rates-form.component.ts ``calculateAmount`` / ``calculateTotal`` /
  ``calculateGrandTotal`` (and the ``Return`` variants);
- shares: booking.component.ts ``createReservationShareArray`` /
  ``createReservationReturnShareArray``.

Quirks are kept on purpose (for example the Base_Rate total includes the admin share, and the
return leg's travel-agent admin share stays at 25 %). Anything the web app would crash on, or
silently price as zero, raises RatesError instead.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

GROUPS = ("all_inclusive_rates", "direct_taxes", "taxes", "amenities", "misc")
_FLAT_GROUPS = ("direct_taxes", "amenities", "taxes", "misc")
_SHARE_RATE_KEYS = ("ELH_Charges", "Stops", "Wait")
# calculateBaseRateShare: the amenities that count toward the admin / agent share
_SHARE_AMENITY_KEYS = (
    "Baby_Seat", "Baggage_Meet_(Dom)", "Baggage_Meet_(Int)", "Bike_Rack", "Booster_Seat", "Golf_Bags",
    "Lei_Greeting_–_Hawaii", "Luggage_Trailer", "Per_Diem", "Red_Carpet", "Security_/_Guard", "Skis",  # noqa: RUF001  (the web key has an en dash)
    "Tour_Guide", "Wedding_Package",
)  # fmt: skip
_TRAVEL_SHARE_PERCENT = 10


class RatesError(Exception):
    """The rates answer can't be priced the way the web app prices it."""


@dataclass(frozen=True, slots=True)
class ShareFlags:
    """The rates-form inputs that change shares (booking.ts ngOnInit / handleClientAccChange)."""

    is_travel_share: bool  # travel-agent portal, or an admin booking for a travel planner account
    is_created_by_admin: bool  # false in the travel-agent / individual portals
    is_farmout: bool = False  # affiliate portal only; the shell does not book as affiliate
    booking_created_from: str = "admin"

    @property
    def applies_travel_agent_share(self) -> bool:
        """``shouldApplyTravelAgentShare`` for a new booking (needs account_type travel_planner too)."""
        return not self.is_created_by_admin


@dataclass(frozen=True, slots=True)
class Rates:
    """One leg's rates form value: the line items plus its totals."""

    lines: dict[str, dict[str, dict[str, Any]]]  # group -> item -> rate line (amounts recomputed)
    totals: dict[str, float]  # item -> the total the rates form shows (Base_Rate includes the shares)
    sub_total: float
    grand_total: float
    min_rate_involved: bool

    def line_amount(self, group: str, item: str) -> float:
        return float(self.lines.get(group, {}).get(item, {}).get("amount") or 0)

    @property
    def base_rate(self) -> float:
        return _num(self.lines["all_inclusive_rates"]["Base_Rate"].get("baserate"))


def to_fixed2(value: float) -> float:
    """``Number(x.toFixed(2))``: half away from zero on the exact binary value, like JavaScript."""
    return float(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def charter_multiplier(hours: float) -> float:
    """``multiplyHours``: 24 hours or more is billed per day (hours / 24)."""
    return hours / 24 if hours >= 24 else hours


def outbound_rates(
    rate_array: dict[str, Any],
    *,
    service_type: str,
    hours: float,
    min_rate_involved: bool,
    flags: ShareFlags,
    vehicles: int = 1,
) -> Rates:
    lines = _lines(rate_array)
    charter = service_type == "charter_tour"
    multiply = charter_multiplier(hours)
    _amounts(lines, base_rate_hours=multiply if charter and hours != 0 and not min_rate_involved else None)

    base = lines["all_inclusive_rates"]["Base_Rate"]
    hourly = charter and not min_rate_involved
    share_base = _share_base(lines, multiply if hourly else None)
    admin_percent = (
        15 if (flags.is_travel_share and not flags.is_created_by_admin) or flags.is_farmout else 25
    )
    admin_share = share_base * admin_percent / 100 + _extra_gratuity(lines) * 0.25
    travel_share = (
        0.0
        if (not flags.is_travel_share and flags.is_created_by_admin)
        else (share_base * _TRAVEL_SHARE_PERCENT / 100)
    )
    base_total = _num(base.get("baserate")) * multiply if hourly else _num(base.get("baserate"))
    base_total += admin_share
    if flags.is_travel_share and not flags.is_created_by_admin:
        base_total += travel_share
    elif flags.is_farmout:
        base_total += share_base * 0.10
    return _totals(lines, base_total, vehicles, min_rate_involved)


def return_rates(
    rate_array: dict[str, Any],
    *,
    service_type: str,
    hours: float,
    flags: ShareFlags,
    vehicles: int = 1,
    created_by_role: str | None = None,
    return_affiliate_type: str = "affiliate",
) -> Rates:
    lines = _lines(rate_array)
    charter = service_type == "charter_tour"
    multiply = charter_multiplier(hours)
    _amounts(lines, base_rate_hours=None)  # the return form never multiplies by hours

    base = lines["all_inclusive_rates"]["Base_Rate"]
    share_base = _share_base(lines, multiply if charter else None)
    if (flags.is_travel_share and not flags.is_created_by_admin) or flags.is_farmout:
        admin_percent = 15
    elif flags.booking_created_from == "subscriber" or (
        created_by_role == "subscriber" and return_affiliate_type == "loose_affiliate"
    ):
        admin_percent = 0
    else:
        admin_percent = 25
    admin_share = share_base * admin_percent / 100 + _extra_gratuity(lines) * 0.25
    travel_share = (
        0.0
        if (not flags.is_travel_share and flags.is_created_by_admin)
        else (share_base * _TRAVEL_SHARE_PERCENT / 100)
    )
    base_total = _num(base.get("baserate")) * multiply if charter else _num(base.get("baserate"))
    base_total += admin_share
    if flags.is_travel_share and not flags.is_created_by_admin:
        base_total += travel_share
    elif flags.is_farmout:
        base_total += share_base * 0.10
    return _totals(lines, base_total, vehicles, min_rate_involved=False)


def share_array(
    rates: Rates,
    *,
    service_type: str,
    hours: float,
    account_type: str,
    flags: ShareFlags,
    vehicles: int = 1,
) -> dict[str, float]:
    """``createReservationShareArray`` (booking.ts:7698)."""
    aio = rates.lines["all_inclusive_rates"]
    if service_type == "charter_tour" and not rates.min_rate_involved:
        base = _num(aio["Base_Rate"].get("baserate")) * charter_multiplier(hours)
    else:
        base = _num(aio["Base_Rate"].get("baserate"))
    for key in _SHARE_RATE_KEYS:
        if key not in aio:
            raise RatesError(f"the rates answer has no {key} line")
        base += _num(aio[key].get("baserate"))
    for line in rates.lines.get("amenities", {}).values():
        base += _num(line.get("baserate"))
    if vehicles != 0:
        base *= vehicles
    grand = rates.grand_total
    stripe_fee = grand * 0.05 + 0.30
    admin_share = base * 25 / 100 + _extra_gratuity(rates.lines) * 0.25
    shares = {
        "baseRate": base,
        "grandTotal": grand,
        "stripeFee": stripe_fee,
        "adminShare": admin_share,
        "deducted_admin_share": admin_share - stripe_fee,
        "affiliateShare": grand - base * 0.25,
    }
    if account_type == "travel_planner" and flags.applies_travel_agent_share:
        shares["adminShare"] = base * 15 / 100
        shares["deducted_admin_share"] = shares["adminShare"] - stripe_fee
        shares["travelAgentShare"] = base * 0.10
    elif flags.is_farmout:
        shares["adminShare"] = base * 15 / 100
        shares["deducted_admin_share"] = shares["adminShare"] - stripe_fee
        shares["farmoutShare"] = base * 0.10
        shares["affiliateShare"] = grand - base * 0.25
    return shares


def return_share_array(
    rates: Rates, *, account_type: str, flags: ShareFlags, vehicles: int = 1
) -> dict[str, float]:
    """``createReservationReturnShareArray`` (booking.ts:7750). Its admin percent is reset to 25
    first, so the travel-agent branch keeps 25 % (unlike the outbound leg)."""
    base = sum(_num(line.get("baserate")) for line in rates.lines["all_inclusive_rates"].values())
    base += sum(_num(line.get("baserate")) for line in rates.lines.get("amenities", {}).values())
    if vehicles != 0:
        base *= vehicles
    grand = rates.grand_total
    stripe_fee = grand * 0.05 + 0.30
    admin_share = base * 25 / 100 + _extra_gratuity(rates.lines) * 0.25
    shares = {
        "baseRate": base,
        "returnGrandTotal": grand,
        "deducted_admin_share": admin_share - stripe_fee,
        "grandTotal": grand,
        "stripeFee": stripe_fee,
        "adminShare": admin_share,
        "affiliateShare": grand - admin_share,
    }
    if account_type == "travel_planner" and flags.applies_travel_agent_share:
        shares["adminShare"] = base * 25 / 100
        shares["deducted_admin_share"] = shares["adminShare"] - stripe_fee
        shares["travelAgentShare"] = base * 0.10
    elif flags.is_farmout:
        shares["adminShare"] = base * 15 / 100
        shares["deducted_admin_share"] = shares["adminShare"] - stripe_fee
        shares["farmoutShare"] = base * 0.10
    return shares


def return_rate_array(rates_answer: dict[str, Any]) -> dict[str, Any]:
    """``resolveReturnRateArray``: the API's ``retrunRateArray`` (sic) if it has lines, else a copy
    of the outbound ``rateArray``."""
    for key in ("retrunRateArray", "rateArray"):
        candidate = rates_answer.get(key)
        if isinstance(candidate, dict) and any(
            isinstance(candidate.get(g), dict) and candidate[g] for g in (*GROUPS,)
        ):
            return copy.deepcopy(candidate)
    return {}


def form_value(rates: Rates) -> dict[str, Any]:
    """The rates form value without totals: what the web app saves as ``rateArray``."""
    return copy.deepcopy(rates.lines)


def _lines(rate_array: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """``buildRatesForm``: the five groups, in the answer's key order, stopping at the first
    array-valued key like the web form does."""
    if not isinstance(rate_array, dict):
        raise RatesError("the rates answer has no rate lines")
    lines: dict[str, dict[str, dict[str, Any]]] = {group: {} for group in GROUPS}
    for key, items in rate_array.items():
        if isinstance(items, list):
            break
        if key not in lines or not isinstance(items, dict):
            continue
        for name, line in items.items():
            if not isinstance(line, dict) or "rate_label" not in line:
                raise RatesError(f"unexpected rate line {key}.{name}")
            for field in ("baserate", "amount"):
                if line.get(field) is not None and not isinstance(line[field], int | float):
                    raise RatesError(f"rate line {key}.{name} has a non-numeric {field}")
            lines[key][name] = dict(line)
    if "Base_Rate" not in lines["all_inclusive_rates"]:
        raise RatesError("the rates answer has no Base_Rate line")
    return lines


def _amounts(lines: dict[str, dict[str, dict[str, Any]]], *, base_rate_hours: float | None) -> None:
    for name, line in lines["all_inclusive_rates"].items():
        baserate = _num(line.get("baserate"))
        if name == "Base_Rate" and base_rate_hours is not None:
            line["amount"] = to_fixed2(base_rate_hours * baserate)
        else:
            line["amount"] = baserate
    km_rate = sum(_num(line["amount"]) for line in lines["all_inclusive_rates"].values())
    for group in _FLAT_GROUPS:
        for line in lines[group].values():
            baserate = _num(line.get("baserate"))
            if group == "taxes" and line.get("type") == "percent":
                line["amount"] = to_fixed2(baserate / 100 * km_rate)
            else:
                line["amount"] = baserate


def _totals(
    lines: dict[str, dict[str, dict[str, Any]]], base_total: float, vehicles: int, min_rate_involved: bool
) -> Rates:
    totals: dict[str, float] = {}
    for group in GROUPS:
        for name, line in lines[group].items():
            totals[name] = to_fixed2(_num(line["amount"]))
    totals["Base_Rate"] = to_fixed2(base_total)
    sub_total = 0.0
    for value in totals.values():
        sub_total = to_fixed2(sub_total) + to_fixed2(value)
    grand_total = to_fixed2(sub_total) * vehicles if vehicles != 0 else 0.0
    return Rates(
        lines=lines,
        totals=totals,
        sub_total=sub_total,
        grand_total=grand_total,
        min_rate_involved=min_rate_involved,
    )


def _share_base(lines: dict[str, dict[str, dict[str, Any]]], multiply: float | None) -> float:
    """``calculateBaseRateShare``."""
    aio = lines["all_inclusive_rates"]
    base = _num(aio["Base_Rate"].get("baserate"))
    if multiply is not None:
        base *= multiply
    base += sum(_num(aio[key].get("baserate")) for key in _SHARE_RATE_KEYS if key in aio)
    amenities = lines.get("amenities", {})
    base += sum(_num(amenities[key].get("baserate")) for key in _SHARE_AMENITY_KEYS if key in amenities)
    return base


def _extra_gratuity(lines: dict[str, dict[str, dict[str, Any]]]) -> float:
    line = lines.get("misc", {}).get("Extra_Gratuity")
    if line is None:
        # the web form throws here and saves a zero total; refuse instead of guessing a price
        raise RatesError("the rates answer has no misc.Extra_Gratuity line")
    return _num(line.get("amount"))


def _num(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0

"""Request bodies, built the way the web app builds them from its booking form.

``form_value`` is the shell's ``BookingForm.value``: every control from ``buildBookingForm``
(booking.ts:3069), filled from the draft. The other builders read from it the way the web
app reads from its form, so all requests agree with each other and with the saved booking.
"""

from __future__ import annotations

import html
import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.booking.draft import BookingDraft, Flight, Leg, Place
from app.services.booking.maps import Route
from app.services.booking.rates import ShareFlags, return_share_array, share_array
from app.services.booking.rates import form_value as rates_form_value
from app.services.booking.reference import currency_for

# booking.ts:3256-3295 and updateOutboundLegValidators / updateReturnLegValidators
CITY_INSTRUCTIONS = (
    "<ol><li>Driver - Text on location. Text the client a day before to confirm driver name , cell phone "
    "and booking details. Text client with ETA when en route</li></ol>"
)
CRUISE_INSTRUCTIONS = (
    "<ol><li>Pax - Text driver when docked.</li><li>Driver - Text the client a day before to confirm driver "
    "name , cell phone and booking details. Text client with ETA when en route. Text pax with pickup "
    "instructions when ship has arrived.</li></ol>"
)
AIRPORT_INSTRUCTIONS = (
    "<ol><li>Pax - Text driver when landing.</li><li>Driver - Text the client a day before to confirm driver "
    "name , cell phone and booking details. Text client with ETA when en route. Text pax with pickup "
    "instructions when plane has arrived.</li></ol>"
)


def default_instructions(pickup_kind: str) -> str:
    """The form's default booking_instructions for a leg starting at this kind of place."""
    return {"cruise": CRUISE_INSTRUCTIONS, "airport": AIRPORT_INSTRUCTIONS}.get(
        pickup_kind, CITY_INSTRUCTIONS
    )


MEET_GREET_CITY = (1, "Driver - Text/call when on location")
MEET_GREET_AIRPORT = (2, "Driver -  Airport - Text/call after plane lands with curbside meet location")


def form_value(d: BookingDraft, *, mode: str, vehicle_categories: list[dict[str, Any]] | None = None) -> dict:
    """``BookingForm.value`` for a new booking in the given portal mode."""
    v = _defaults(mode)
    _apply_trip(v, d)
    if d.vehicle is not None:
        _apply_vehicle(v, d, mode, vehicle_categories or [])
    _apply_customer(v, d)
    return v


def quote_payload(d: BookingDraft, v: dict) -> dict[str, Any]:
    """``buildQuotePayload`` (booking.ts:662): the master-vehicle-listing / vehicle-listing body."""
    pickup_type, _, dropoff_type = (v["transfer_type"] or "").partition("_to_")
    pickup_type, dropoff_type = pickup_type or "city", dropoff_type or "city"
    ret_pickup_type, _, ret_dropoff_type = (v["return_transfer_type"] or "").partition("_to_")
    ap, ad = pickup_type == "airport", dropoff_type == "airport"
    rt = v["service_type"] == "round_trip"
    rpa, rda = rt and ret_pickup_type == "airport", rt and ret_dropoff_type == "airport"

    location_info = []
    if d.outbound.route and d.outbound.route.meters > 0:
        location_info.append(_location_info(d.outbound.route))
    if rt and d.ret.route and d.ret.route.meters > 0:
        location_info.append(_location_info(d.ret.route))

    def stops(items: list[dict]) -> list[dict]:
        return [
            {
                "address": s["address"] or "",
                "latitude": s["latitude"] or "",
                "longitude": s["longitude"] or "",
            }
            for s in items
        ]

    return {
        "service_type": v["service_type"],
        "booking_hour": str(v["number_of_hours"] if v["number_of_hours"] is not None else "2"),
        "pickup_type": pickup_type,
        "dropoff_type": dropoff_type,
        "pickup_date": v["pickup_date"],
        "pickup_time": v["pickup_time"],
        "pickup_airport": (v["pickup_airport"] or None) if ap else None,
        "pickup_airport_name": (v["pickup_airport_option"] or v["pickup_airport_name"] or "") if ap else "",
        "pickup_airport_lat": (v["pickup_airport_latitude"] or None) if ap else None,
        "pickup_airport_long": (v["pickup_airport_longitude"] or None) if ap else None,
        "pickup_address": v["pickup"] or "",
        "pickup_address_lat": v["pickup_latitude"] or None,
        "pickup_address_long": v["pickup_longitude"] or None,
        "dropoff_airport": (v["dropoff_airport"] or None) if ad else None,
        "dropoff_airport_name": (v["dropoff_airport_option"] or v["dropoff_airport_name"] or "")
        if ad
        else "",
        "dropoff_airport_lat": (v["dropoff_airport_latitude"] or None) if ad else None,
        "dropoff_airport_long": (v["dropoff_airport_longitude"] or None) if ad else None,
        "dropoff_address": v["dropoff"] or "",
        "dropoff_address_lat": v["dropoff_latitude"] or None,
        "dropoff_address_long": v["dropoff_longitude"] or None,
        "return_pickup_date": v["return_pickup_date"] or v["pickup_date"],
        "return_pickup_time": v["return_pickup_time"],
        "return_pickup_airport": (v["return_pickup_airport"] or "") if rpa else "",
        "return_pickup_airport_name": (v["return_pickup_airport_option"] or "") if rpa else "",
        "return_pickup_airport_lat": (v["return_pickup_airport_latitude"] or "") if rpa else "",
        "return_pickup_airport_long": (v["return_pickup_airport_longitude"] or "") if rpa else "",
        "return_pickup_address": (v["return_pickup"] or "") if rt else "",
        "return_pickup_address_lat": (v["return_pickup_latitude"] or "") if rt else "",
        "return_pickup_address_long": (v["return_pickup_longitude"] or "") if rt else "",
        "return_dropoff_airport": (v["return_dropoff_airport"] or "") if rda else "",
        "return_dropoff_airport_name": (v["return_dropoff_airport_option"] or "") if rda else "",
        "return_dropoff_airport_lat": (v["return_dropoff_airport_latitude"] or "") if rda else "",
        "return_dropoff_airport_long": (v["return_dropoff_airport_longitude"] or "") if rda else "",
        "return_dropoff_address": (v["return_dropoff"] or "") if rt else "",
        "return_dropoff_address_lat": (v["return_dropoff_latitude"] or "") if rt else "",
        "return_dropoff_address_long": (v["return_dropoff_longitude"] or "") if rt else "",
        "no_of_passenger": v["total_passengers"] or 1,
        "no_of_luggage": v["luggage_count"] or 0,
        "return_no_of_passenger": v["total_passengers"] or 1,
        "return_no_of_luggage": v["luggage_count"] or 0,
        "location_info": location_info,
        "other_details": {
            "pickup_airport_name": (v["pickup_airport_option"] or v["pickup_airport_name"] or "")
            if ap
            else "",
            "dropoff_airport_name": (v["dropoff_airport_option"] or v["dropoff_airport_name"] or "")
            if ad
            else "",
            "return_pickup_airport_name": (
                v["return_pickup_airport_option"] or v["return_pickup_airport_name"] or ""
            )
            if rpa
            else "",
            "return_dropoff_airport_name": (
                v["return_dropoff_airport_option"] or v["return_dropoff_airport_name"] or ""
            )
            if rda
            else "",
        },
        "extra_stops": stops(v["extra_stops"]),
        "return_extra_stops": stops(v["return_extra_stops"]) if rt else [],
        "amenities": v["amenities"] or [],
        "chargedAmenities": v["chargedAmenities"] or [],
    }


def listing_payload(quote: dict[str, Any], vehicle_type_id: int | None, user_id: int) -> dict[str, Any]:
    """vehicle-listing body: embedded-quote ``selectClass`` + the signed-in ``user_id``."""
    filters = {"vehicle-type": [vehicle_type_id]} if vehicle_type_id is not None else {}
    return {
        **quote,
        "filters": filters,
        "selected_amenities": quote.get("amenities") or [],
        "user_id": user_id,
    }


def booking_data(d: BookingDraft, v: dict) -> dict[str, Any]:
    """``buildBookingData`` (booking.ts:595) as rates-form and ``fetchRatesByAffiliateVeh`` send it to
    ``admin/booking-rates-vehicle``."""
    vehicle_id = v["vehicle_id"]
    empty_vehicle = vehicle_id in (None, "")
    is_master_payload = vehicle_id in (0, "0") or empty_vehicle or d.is_master_vehicle
    picked_id = (d.vehicle or {}).get("id")
    effective_vehicle = vehicle_id or picked_id or v["vehicle_type"] or ""
    effective_return = v["return_vehicle_id"] or v["return_vehicle_type"] or effective_vehicle
    affiliate_id = v["affiliate_id"]
    data: dict[str, Any] = {
        "affiliate_id": affiliate_id,
        "return_affiliate_id": v["return_affiliate_id"] or affiliate_id,
        "vehicle_id": effective_vehicle,
        "return_vehicle_id": effective_return,
        "transfer_type": v["transfer_type"],
        "return_transfer_type": v["return_transfer_type"],
        "service_type": v["service_type"],
        "numberOfVehicles": 1,
        "distance": d.outbound.route.meters if d.outbound.route else 0,
        "return_distance": d.ret.route.meters if d.is_round_trip and d.ret.route else 0,
        "no_of_hours": 2 if v["number_of_hours"] == 0 else v["number_of_hours"],
        "is_master_vehicle": is_master_payload,
        "extra_stops": _strip_stops(v["extra_stops"]),
        "return_extra_stops": _strip_stops(v["return_extra_stops"]),
        "manual_change_aff_veh": False,
        "pickup_time": v["pickup_time"],
        "return_pickup_time": v["return_pickup_time"],
        "affiliate_type": v["affiliate_type"],
        "return_affiliate_type": v["return_affiliate_type"] or v["affiliate_type"],
        "rate_refresh_token": 1,  # bumped once by onQuoteVehicleSelected
    }
    # rates-form fetchRatesArrayByAffiliateVehicle + admin.service fetchRatesByAffiliateVeh
    has_affiliate = affiliate_id not in (None, "")
    data["is_master_vehicle"] = not has_affiliate and (data["is_master_vehicle"] is True or empty_vehicle)
    data["selected_amenities"] = []  # the shell offers no amenities (BOOKING_FLOW.md Q9)
    return data


def submit_payload(
    d: BookingDraft, *, mode: str, flags: ShareFlags, vehicle_categories: list | None = None
) -> dict:
    """``submitForm(true)`` (booking.ts:8072-8360) + ``createBookingByMode``."""
    if d.rates is None:
        raise ValueError("rates are required before submitting")
    v = form_value(d, mode=mode, vehicle_categories=vehicle_categories)
    _normalise_phones(v, d.is_round_trip)
    _mirror_airport_coordinates(v)
    rt = d.is_round_trip
    v["booking_created_from"] = flags.booking_created_from
    v["proceed"] = True
    currency = _currency(d)
    if currency is not None:  # JSON.stringify drops an undefined currency
        v["currency"] = currency
    v["platform_type"] = "web"
    v["is_master_vehicle"] = d.is_master_vehicle
    v["is_pending_affiliate"] = v["affiliate_type"] == "in_progress_affiliate"
    v["return_is_pending_affiliate"] = v["return_affiliate_type"] == "in_progress_affiliate"
    v["rateArray"] = rates_form_value(d.rates)
    v["grand_total"] = d.rates.grand_total
    v["sub_total"] = d.rates.sub_total
    v["min_rate_involved"] = d.rates.min_rate_involved
    v["shares_array"] = share_array(
        d.rates,
        service_type=v["service_type"],
        hours=float(v["number_of_hours"]),
        account_type=v["account_type"],
        flags=flags,
        vehicles=int(v["number_of_vehicles"] or 0),
    )
    if rt and d.return_rates is not None:
        v["returnRateArray"] = rates_form_value(d.return_rates)
        v["return_grand_total"] = d.return_rates.grand_total
        v["return_sub_total"] = d.return_rates.sub_total
        v["return_shares_array"] = return_share_array(
            d.return_rates,
            account_type=v["account_type"],
            flags=flags,
            vehicles=int(v["number_of_vehicles"] or 0),
        )
    if mode == "individual":
        v.pop("booking_created_from", None)  # only subscribers send it (booking.ts:8705)
    return v


def _defaults(mode: str) -> dict[str, Any]:
    today = date.today().isoformat()
    loose = {
        "first_name": "",
        "middle_name": "",
        "last_name": "",
        "phone": "",
        "phone_isd": "+1",
        "phone_country": "us",
        "email": "",
        "address": "",
        "country": "",
        "state": "",
        "city": "",
        "zipCode": "",
        "card_details": {"name": "", "card_number": "", "exp_month": "", "exp_year": "", "cvv": ""},
    }
    v: dict[str, Any] = {
        "service_type": "one_way",
        "transfer_type": "city_to_city",
        "return_transfer_type": "city_to_city",
        "number_of_hours": 2,
        "prevent_rate_override": False,
        "acc_id": "",
        "account_type": "travel_planner" if mode == "travel_agent" else "individual",
        "travel_client_id": "",
        "travel_client_acc": "travel_individual",
        "sub_account_type": "travel_agent",
        "sub_account_id": "",
        "change_individual_data": False,
        "loose_customer": loose,
        "passenger_name": "",
        "passenger_email": "",
        "passenger_cell": "",
        "passenger_cell_isd": "+1",
        "passenger_cell_country": "us",
        "total_passengers": 1,
        "luggage_count": 0,
        "booking_instructions": CITY_INSTRUCTIONS,
        "return_booking_instructions": CITY_INSTRUCTIONS,
        "affiliate_type": "affiliate",
        "affiliate_id": "",
        "return_affiliate_type": "affiliate",
        "return_affiliate_id": "",
        "loose_affiliate_id": "",
        "is_old_loose_affiliate": False,
        "return_loose_affiliate_id": "",
        "return_is_old_loose_affiliate": False,
    }
    for prefix in ("", "return_"):
        v.update(
            {
                f"{prefix}lose_affiliate_name": "",
                f"{prefix}lose_affiliate_phone": "",
                f"{prefix}lose_affiliate_phone_isd": "+1",
                f"{prefix}lose_affiliate_phone_country": "us",
                f"{prefix}lose_affiliate_email": "",
            }
        )
    v.update({"cancellation_hours": "24", "return_cancellation_hours": "24"})
    for prefix in ("", "return_"):
        v.update(
            {
                f"{prefix}vehicle_type": "",
                f"{prefix}vehicle_type_name": "",
                f"{prefix}vehicle_id": "",
                f"{prefix}vehicle_make": "",
                f"{prefix}vehicle_make_name": "",
                f"{prefix}vehicle_model": "",
                f"{prefix}vehicle_model_name": "",
                f"{prefix}vehicle_year": "",
                f"{prefix}vehicle_year_name": "",
                f"{prefix}vehicle_color": "",
                f"{prefix}vehicle_color_name": "",
                f"{prefix}vehicle_license_plate": "",
                f"{prefix}vehicle_seats": "4",
                f"{prefix}driver_id": "",
                f"{prefix}driver_name": "",
                f"{prefix}driver_gender": "",
                f"{prefix}driver_cell": "",
                f"{prefix}driver_cell_isd": "+1",
                f"{prefix}driver_cell_country": "us",
                f"{prefix}driver_email": "",
            }
        )
    v.update(
        {
            "driver_phone_type": "",
            "return_driver_phone_type": "",
            "driver_image_id": "",
            "vehicle_image_id": "",
            "meet_greet_choices": MEET_GREET_CITY[0],
            "meet_greet_choices_name": MEET_GREET_CITY[1],
            "number_of_vehicles": 1,
            "pickup_date": today,
            "pickup_time": "12:00 am",
            "extra_stops": [],
        }
    )
    for prefix in ("pickup", "dropoff"):
        v.update(_empty_end(prefix))
    v.update(
        {
            "pickup_airline_option": "",
            "pickup_airline": "",
            "pickup_airline_name": "",
            "pickup_flight": "",
            "origin_airport_city": "",
            "departing_airport_city": "",
            "cruise_port": "",
            "cruise_name": "",
            "cruise_time": "12:00 am",
            "dropoff_airline_option": "",
            "dropoff_airline": "",
            "dropoff_airline_name": "",
            "dropoff_flight": "",
            "return_meet_greet_choices": MEET_GREET_CITY[0],
            "return_meet_greet_choices_name": MEET_GREET_CITY[1],
            "return_pickup_date": today,
            "return_pickup_time": "12:00 pm",
            "return_extra_stops": [],
        }
    )
    for prefix in ("return_pickup", "return_dropoff"):
        v.update(_empty_end(prefix))
    v.update(
        {
            "return_pickup_airline_option": "",
            "return_pickup_airline": "",
            "return_pickup_airline_name": "",
            "return_pickup_flight": "",
            "return_cruise_port": "",
            "return_cruise_name": "",
            "return_cruise_time": "12:00 pm",
            "return_dropoff_airline_option": "",
            "return_dropoff_airline": "",
            "return_dropoff_airline_name": "",
            "return_dropoff_flight": "",
            "driver_languages": [],
            "driver_dresses": [],
            "amenities": [],
            "chargedAmenities": [],
            "journeyDistance": "",
            "journeyTime": "",
            "returnJourneyDistance": "",
            "returnJourneyTime": "",
            "reservation_id": "",
            "updateType": "create",
            "susbcriber_name": "",
            "return_susbcriber_name": "",
            "fbo_address": "",
            "return_fbo_address": "",
            "fbo_name": "",
            "return_fbo_name": "",
        }
    )
    return v


def _empty_end(prefix: str) -> dict[str, Any]:
    return {
        prefix: "",
        f"{prefix}_latitude": "",
        f"{prefix}_longitude": "",
        f"{prefix}_airport_option": "",
        f"{prefix}_airport": "",
        f"{prefix}_airport_name": "",
        f"{prefix}_airport_latitude": "",
        f"{prefix}_airport_longitude": "",
    }


def _apply_trip(v: dict, d: BookingDraft) -> None:
    out, ret, rt = d.outbound, d.ret, d.is_round_trip
    if d.service_type:
        v["service_type"] = d.service_type
    if out.transfer_type:
        v["transfer_type"] = out.transfer_type
    if rt and ret.transfer_type:
        v["return_transfer_type"] = ret.transfer_type
    v["number_of_hours"] = d.hours
    _apply_leg(v, out, "", "pickup", "dropoff")
    if rt:
        _apply_leg(v, ret, "return_", "return_pickup", "return_dropoff")
    v["total_passengers"] = d.pax if d.pax is not None else 1
    v["luggage_count"] = d.luggage if d.luggage is not None else 0
    if d.instructions:
        note = f"<p>{html.escape(d.instructions)}</p>"
        v["booking_instructions"] += note
        if rt:
            v["return_booking_instructions"] += note


def _apply_leg(v: dict, leg: Leg, prefix: str, pickup_key: str, dropoff_key: str) -> None:
    if leg.date is not None:
        v[f"{prefix}pickup_date"] = leg.date.isoformat()
    if leg.time is not None:
        v[f"{prefix}pickup_time"] = leg.time
    if leg.pickup is not None:
        v.update(_end_fields(pickup_key, leg.pickup))
    if leg.dropoff is not None:
        v.update(_end_fields(dropoff_key, leg.dropoff))
    v[f"{prefix}extra_stops"] = [_stop(s) for s in leg.stops]
    if leg.route is not None:
        distance_key, time_key = (
            ("returnJourneyDistance", "returnJourneyTime")
            if prefix
            else (
                "journeyDistance",
                "journeyTime",
            )
        )
        v[distance_key], v[time_key] = leg.route.meters, leg.route.seconds

    pickup_kind = leg.pickup.kind if leg.pickup else "city"
    dropoff_kind = leg.dropoff.kind if leg.dropoff else "city"
    v[f"{prefix}booking_instructions"] = default_instructions(pickup_kind)
    default_greet = MEET_GREET_CITY if pickup_kind == "city" else MEET_GREET_AIRPORT
    greet_id, greet_name = (
        (leg.meet_greet["id"], leg.meet_greet["message"]) if leg.meet_greet else default_greet
    )
    v[f"{prefix}meet_greet_choices"], v[f"{prefix}meet_greet_choices_name"] = greet_id, greet_name

    if pickup_kind == "airport":
        v.update(_airline_fields(f"{pickup_key}_airline", leg.pickup_flight))
        v[f"{pickup_key}_flight"] = leg.pickup_flight.number
        v["departing_airport_city" if prefix else "origin_airport_city"] = leg.pickup_flight.city
    if dropoff_kind == "airport":
        v.update(_airline_fields(f"{dropoff_key}_airline", leg.dropoff_flight))
        v[f"{dropoff_key}_flight"] = leg.dropoff_flight.number
    if "cruise" in (pickup_kind, dropoff_kind):
        v[f"{prefix}cruise_port"], v[f"{prefix}cruise_name"] = leg.cruise.port, leg.cruise.name
        v[f"{prefix}cruise_time"] = leg.cruise.time


def _end_fields(key: str, place: Place) -> dict[str, Any]:
    if place.kind != "airport":
        return {key: place.address, f"{key}_latitude": place.lat, f"{key}_longitude": place.lng}
    return {
        key: place.airport_label,
        f"{key}_airport_option": place.airport_label,
        f"{key}_airport": place.airport_id,
        f"{key}_airport_name": place.airport_label,
        f"{key}_airport_latitude": place.lat,
        f"{key}_airport_longitude": place.lng,
    }


def _airline_fields(key: str, flight: Flight) -> dict[str, Any]:
    airline = flight.airline
    if not airline:
        return {f"{key}_option": "", key: "", f"{key}_name": ""}
    return {f"{key}_option": airline, key: airline.get("id", ""), f"{key}_name": airline_label(airline)}


def airline_label(airline: dict[str, Any]) -> str:
    """admin.service.ts:39 ``formatted_name``."""
    return f"{airline.get('code', '')} - {airline.get('name', '')}, {airline.get('country', '')}"


def _stop(place: Place) -> dict[str, Any]:
    """An ``addExtraStop`` group; an airport stop also carries ``applyExtraStopAirportSelection``'s fields."""
    airport = place.kind == "airport"
    return {
        "address": place.address,
        "latitude": place.lat,
        "longitude": place.lng,
        "rate": "",
        "booking_instructions": "",
        "kind": "airport" if airport else "city",
        "airport": place.airport_id if airport else "",
        "airport_option": place.airport_label if airport else "",
        "airport_name": place.airport_label if airport else "",
        "airport_latitude": place.lat if airport else "",
        "airport_longitude": place.lng if airport else "",
    }


def _strip_stops(stops: list[dict]) -> list[dict]:
    """``stripInternalStopFields``."""
    keys = ("address", "latitude", "longitude", "rate", "booking_instructions")
    return [{k: s.get(k, "") if s.get(k) is not None else "" for k in keys} for s in stops]


def _apply_vehicle(v: dict, d: BookingDraft, mode: str, categories: list[dict[str, Any]]) -> None:
    """``onQuoteVehicleSelected`` -> ``prefillVehiclePreferencesFromMasterVehicle`` then
    ``populateDriverAndVehicleDetails``; master vehicles also take ``get-master-vehicle-info``."""
    vehicle = d.vehicle or {}
    rt = d.is_round_trip
    _prefill_vehicle(v, _normalise_master(vehicle, categories))
    if d.master_info:
        _prefill_vehicle(v, _normalise_master(d.master_info, categories))
    if not d.is_master_vehicle or not d.master_info:
        _populate_driver_and_vehicle(v, vehicle)
    if d.is_master_vehicle:
        affiliate_type = "loose_affiliate"
        v.update({"affiliate_id": "", "loose_affiliate_id": "", "affiliate_type": affiliate_type})
        if rt:
            v.update(
                {
                    "return_affiliate_id": "",
                    "return_loose_affiliate_id": "",
                    "return_affiliate_type": affiliate_type,
                }
            )
    else:
        v.update({"affiliate_id": vehicle.get("affiliate_id"), "affiliate_type": "affiliate"})
        if rt:
            v.update(
                {"return_affiliate_id": vehicle.get("affiliate_id"), "return_affiliate_type": "affiliate"}
            )
    if rt:  # syncMissingReturnVehicleFromOutbound: the return leg uses the same vehicle and driver
        for key in (
            "vehicle_type",
            "vehicle_type_name",
            "vehicle_id",
            "vehicle_make",
            "vehicle_make_name",
            "vehicle_model",
            "vehicle_model_name",
            "vehicle_year",
            "vehicle_year_name",
            "vehicle_color",
            "vehicle_color_name",
            "vehicle_license_plate",
            "vehicle_seats",
            "cancellation_hours",
            "driver_id",
            "driver_name",
            "driver_gender",
            "driver_cell",
            "driver_cell_isd",
            "driver_cell_country",
            "driver_email",
            "driver_phone_type",
        ):
            v[f"return_{key}"] = v[key]


def _normalise_master(vehicle: dict[str, Any], categories: list[dict[str, Any]]) -> dict[str, Any]:
    """``normalizeMasterVehicleForPrefill`` (booking.ts:6367)."""
    details = vehicle.get("vehicle_details") or {}

    def first(*values: Any) -> Any:
        return next((x for x in values if x is not None), "")

    type_name = first(
        vehicle.get("vehicleType"), vehicle.get("vehicleTypeName"), vehicle.get("name"), details.get("type")
    )
    type_id = first(
        vehicle.get("vehicleType_id"), vehicle.get("vehicle_type"), _category_id(categories, type_name)
    )
    return {
        "id": first(vehicle.get("ID"), vehicle.get("id"), vehicle.get("vehicle_id")),
        "vehicleType_id": type_id,
        "vehicleType": type_name,
        "make_id": first(vehicle.get("make_id"), vehicle.get("vehicle_make")),
        "make": first(vehicle.get("make"), vehicle.get("vehicleMake"), details.get("make")),
        "model_id": first(vehicle.get("model_id"), vehicle.get("vehicle_model")),
        "model": first(vehicle.get("model"), vehicle.get("vehicleModel"), details.get("model")),
        "year_id": first(vehicle.get("year_id"), vehicle.get("vehicle_year")),
        "year": first(vehicle.get("year"), vehicle.get("vehicleYear"), details.get("year")),
        "color_id": first(vehicle.get("color_id"), vehicle.get("vehicle_color")),
        "color": first(vehicle.get("color"), vehicle.get("vehicleColor"), details.get("color")),
        "licensePlate": first(
            vehicle.get("licensePlate"),
            vehicle.get("vehicle_license_plate"),
            details.get("licensePlate"),
            details.get("license_plate"),
        ),
        "seats": first(
            vehicle.get("seats"), details.get("seats"), vehicle.get("passenger"), details.get("passenger")
        ),
    }


def _category_id(categories: list[dict[str, Any]], name: Any) -> Any:
    """``resolveBigDataOptionId(BigData.vehicleCategories, [name])``."""
    wanted = str(name or "").strip().lower()
    for item in categories:
        if wanted and str(item.get("name") or item.get("formatted_name") or "").strip().lower() == wanted:
            return item.get("id")
    return None


def _prefill_vehicle(v: dict, normal: dict[str, Any]) -> None:
    """``prefillVehiclePreferencesFromMasterVehicle``: only non-empty values are written."""
    pairs = {
        "vehicle_id": normal["id"],
        "vehicle_type": normal["vehicleType_id"],
        "vehicle_type_name": normal["vehicleType"],
        "vehicle_make": normal["make_id"],
        "vehicle_make_name": normal["make"],
        "vehicle_model": normal["model_id"],
        "vehicle_model_name": normal["model"],
        "vehicle_year": normal["year_id"],
        "vehicle_year_name": normal["year"],
        "vehicle_color": normal["color_id"],
        "vehicle_color_name": normal["color"],
        "vehicle_license_plate": normal["licensePlate"],
        "vehicle_seats": normal["seats"],
    }
    for key, value in pairs.items():
        if value not in (None, ""):
            v[key] = value


def _populate_driver_and_vehicle(v: dict, vehicle: dict[str, Any]) -> None:
    """``populateDriverAndVehicleDetails`` quote-flow patch (booking.ts:921). The shell applies it
    for admins too and leaves driver assignment to this listing (BOOKING_FLOW.md Q12)."""
    driver = vehicle.get("driverInformation") or {}
    details = vehicle.get("vehicle_details") or {}
    isd = driver.get("cell_isd") or vehicle.get("driver_cell_isd")
    if isd and not str(isd).startswith("+"):
        isd = f"+{isd}"
    v.update(
        {
            "driver_id": driver.get("id") or vehicle.get("driver_id") or "",
            "driver_name": driver.get("name") or vehicle.get("driver_name") or "",
            "driver_email": driver.get("email") or vehicle.get("driver_email") or "",
            "driver_cell": driver.get("cell_number") or vehicle.get("driver_cell") or "",
            "driver_cell_isd": isd or "+1",
            "driver_cell_country": driver.get("cell_country") or vehicle.get("driver_cell_country") or "us",
            "driver_gender": driver.get("gender") or vehicle.get("driver_gender") or "",
            "vehicle_id": vehicle.get("id") or vehicle.get("vehicle_id") or "",
            "vehicle_type": vehicle.get("vehicle_type_id")
            or vehicle.get("vehicle_type")
            or v["vehicle_type"]
            or "",
            "vehicle_type_name": vehicle.get("name") or vehicle.get("vehicle_type_name") or "",
            "vehicle_make": details.get("make_id") or vehicle.get("vehicle_make") or "",
            "vehicle_make_name": details.get("make") or vehicle.get("vehicle_make_name") or "",
            "vehicle_model": details.get("model_id") or vehicle.get("vehicle_model") or "",
            "vehicle_model_name": details.get("model") or vehicle.get("vehicle_model_name") or "",
            "vehicle_year": details.get("year_id") or vehicle.get("vehicle_year") or "",
            "vehicle_year_name": details.get("year") or vehicle.get("vehicle_year_name") or "",
            "vehicle_seats": details.get("seats") or vehicle.get("vehicle_seats") or "4",
        }
    )


def _apply_customer(v: dict, d: BookingDraft) -> None:
    customer, passenger = d.customer, d.passenger
    if customer is not None:
        v["account_type"] = customer.account_type
        v["acc_id"] = customer.acc_id
        v["travel_client_acc"] = customer.travel_client_acc
        v["travel_client_id"] = customer.travel_client_id
        if customer.loose is not None:
            v["loose_customer"].update(customer.loose)
    if passenger is not None:
        v.update(
            {
                "passenger_name": passenger.name,
                "passenger_email": passenger.email,
                "passenger_cell": passenger.cell,
                "passenger_cell_isd": passenger.cell_isd,
                "passenger_cell_country": passenger.cell_country,
            }
        )


def _normalise_phones(v: dict, round_trip: bool) -> None:
    """booking.ts:8156-8270: '+' on the ISD codes, and the ISD prefix removed from numbers."""
    for key in ("driver_cell_isd", "lose_affiliate_phone_isd") + (
        ("return_driver_cell_isd", "return_lose_affiliate_phone_isd") if round_trip else ()
    ):
        v[key] = _plus(v[key])
    for number, isd in (
        ("return_driver_cell", "return_driver_cell_isd"),
        ("lose_affiliate_phone", "lose_affiliate_phone_isd"),
        ("passenger_cell", "passenger_cell_isd"),
        ("return_lose_affiliate_phone", "return_lose_affiliate_phone_isd"),
        ("driver_cell", "driver_cell_isd"),
    ):
        value, prefix = str(v[number] or ""), str(v[isd] or "")
        if value and prefix and value.startswith(prefix):
            v[number] = value[len(prefix) :]
    loose = v["loose_customer"]
    if loose["phone"] and loose["phone_isd"] and loose["phone"].startswith(loose["phone_isd"]):
        loose["phone"] = loose["phone"][len(loose["phone_isd"]) :]


def _plus(value: Any) -> Any:
    if value in (None, "") or str(value).startswith("+"):
        return value
    return f"+{value}"


def _mirror_airport_coordinates(v: dict) -> None:
    """``normalizeAirportCoordinatesInPayload``: an airport end's coordinates are copied onto the
    address coordinates the backend reads."""
    legs = (
        ("pickup", v["transfer_type"].partition("_to_")[0]),
        ("dropoff", v["transfer_type"].partition("_to_")[2]),
        ("return_pickup", v["return_transfer_type"].partition("_to_")[0]),
        ("return_dropoff", v["return_transfer_type"].partition("_to_")[2]),
    )
    for prefix, kind in legs:
        lat, lng = v[f"{prefix}_airport_latitude"], v[f"{prefix}_airport_longitude"]
        if kind == "airport" and lat not in ("", None) and lng not in ("", None):
            v[f"{prefix}_latitude"], v[f"{prefix}_longitude"] = lat, lng


def _currency(d: BookingDraft) -> str | None:
    """``currencyObj.currency``: set from the drop-off place's country, then the return drop-off's."""
    currency = None
    ends = [d.outbound.dropoff] + ([d.ret.dropoff] if d.is_round_trip else [])
    for place in ends:
        if place is not None and (match := currency_for(place.country_names)):
            currency = match["currency"]
    return currency


def _location_info(route: Route) -> dict[str, Any]:
    minutes = _js_round(route.seconds / 60)
    return {
        "distance": {"text": f"{_to_fixed1(route.meters / 1000)} km", "value": _js_round(route.meters)},
        "duration": {
            "text": f"{minutes} mins" if route.seconds > 0 else "0 mins",
            "value": _js_round(route.seconds),
        },
    }


def _js_round(value: float) -> int:
    """``Math.round``: halves go up."""
    return math.floor(value + 0.5)


def _to_fixed1(value: float) -> str:
    """``x.toFixed(1)``: ties on the exact binary value go up, unlike Python's format."""
    return str(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

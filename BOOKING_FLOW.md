# BOOKING_FLOW.md — how the web app books a ride (spec for the shell "book a ride" flow)

Source of truth: the shared Angular form `1800-limo/src/app/components/shared/booking/booking.component.{ts,html}`
(referred to below as `booking.ts` / `booking.html`), plus the services and child components it uses.
Backend references are in `limoapi/` and are only cited where they decide what the frontend must send.

All URLs below are relative to `serverUrl` = `https://api.1800limo.com/api/` (prod; `environment.prod.ts`).
Every authenticated call sends `Authorization: Bearer <Passport access token>` (added by
`interceptors/http-config.interceptor.ts:41` / `auth.interceptor.ts:29`). No other auth header is used.

> Status: Phase 2 implemented in the shell (`:book`, `app/services/booking/`) and the web chat (§11). §9 lists the open questions; §10
> records the answers and the defaults taken for the questions still open.

---

## 0. Important corrections to the task brief

These come from reading the code. They change how the flow has to be built.

1. **The web app has no step-by-step wizard.** It is a single long form (`<app-booking mode="…">`). The screens in
   the brief (customer → flight → amenities → payment → confirmation) are **sections of that one form**. After the
   form comes a **Preview** receipt and then **Save**. The order in §1 is the section order on the page.
2. **There is no payment step in the booking form.** No Stripe Elements, no charge. A new booking is saved unpaid
   and payment is taken later (finalize/payment screens, outside this form). The only card-related parts are:
   - a read-only list of the chosen account's **saved cards** (admin / travel-agent views), with an "Add credit card" form;
   - **raw card fields that the web form requires when a new customer ("loose customer") is created**
     (`loose_customer.card_details`). The backend sends those to Stripe as a token. See §6.
3. **Price is not taken from `vehicle-listing`.** Once a vehicle is chosen, the form calls a **third** API,
   `POST admin/booking-rates-vehicle`. The rate breakdown it returns (`rateArray`) plus totals computed on the
   client are what gets saved (§5.4). The `vehicle-listing` price is only shown as a "From" price on the card.
4. **`pickup_airport` is not always a Google `place_id`.** In the booking form, `pickup_airport` /
   `dropoff_airport` / `return_*_airport` hold the **internal airport id** (`BigData.airportsData[].id`). The id is
   picked as the airport nearest to the Google place's lat/lng (`computeAirportFieldsFromPlace`,
   `resolveInternalAirportRecord`, `booking.ts:4635-4766`). The sample payload in the brief (with a `ChIJ…` place
   id) comes from the standalone quotebot/home page, not from this form. See open question Q3.
5. **Address endpoints are typed `city`, not `address`.** `transfer_type` is `<pickup>_to_<dropoff>`, with each
   side one of `city | airport | cruise`. The backend rebuilds it the same way (`rideTransferType`,
   `limoapi/app/Http/Traits/Quotebot/VehicleFilters.php:1187`). **FBO is not a separate type.** It is an airport
   endpoint whose internal airport id is **`3283`**, and choosing it shows the `fbo_address` / `fbo_name` fields
   (`booking.html:1175,1221`).
6. **The category → vehicle filter id is not the master listing's `id`.** The embedded quote maps
   **category name → id** using `GET quote/filters` (`data['vehicle-type'][]`), then sends
   `filters: {"vehicle-type": [thatId]}` (`embedded-quote.component.ts:73-84,111-123`). The ids may happen to
   match the master `id`. See Q4.
7. **The web app does not send `user_id` to `vehicle-listing`.** The embedded quote sends
   `{...quotePayload, filters, selected_amenities}` only. The backend accepts an optional `user_id` and uses it
   only to apply a **business account's** pricing (`QuoteController.php:1877-1886`). See Q5.
8. **`PERSON_LOOKUP.md` does not exist in this repo.** Only `DATA_MODEL.md` exists. The disambiguation-menu rule
   the brief refers to has to be defined (or the file added) before Phase 2.

---

## 1. Step map (web booking, new booking, in page order)

| # | Section (`data-bksec`) | What happens | Code |
|---|---|---|---|
| 1 | **Client Accounts** | Choose who the booking is for. This depends on the role (§2). | `booking.html:136-700` |
| 2 | **Passenger Information** | `passenger_name`*, `passenger_cell`*, `passenger_cell_isd`, `passenger_cell_country`, `passenger_email`. Auto-filled from the chosen account, and editable. | `autofillData('passenger')` `booking.ts:6852` |
| 3 | **Booking Details** | `service_type`, `transfer_type` (pickup kind + dropoff kind), date and time, pickup/dropoff places, extra stops, `total_passengers`, `luggage_count`, `number_of_hours` (charter), meet & greet, flight/cruise/FBO fields, `cancellation_hours`, `booking_instructions`. | `buildBookingForm` `booking.ts:3069` |
| 4 | **Vehicle & Affiliate**: "Browse vehicles" | Embedded quote: **(a)** `POST quote/master-vehicle-listing` → category cards; **(b)** clicking a category → `POST quote/vehicle-listing` → vehicle cards; **(c)** "Select" → `onQuoteVehicleSelected` fills the vehicle/affiliate/driver fields. | `runEmbeddedQuote` `booking.ts:789`, `embedded-quote.component.ts` |
| 4' | Vehicle & Affiliate: "Manual" (admin/affiliate only) | Pick the affiliate, vehicle and driver by hand. | `booking.html:1920+` |
| 5 | **Transportation Details** (quote-flow roles) | Read-only card: driver photo, name, gender; vehicle type, make, model. | `booking.html:1827-1905` |
| 6 | **Return Booking** (round trip) | Return date, time, places, stops, flight/cruise, meet & greet. Return vehicle = outbound vehicle. | `booking.html` ~2900-4560 |
| 7 | **Rates** | `<app-rates-form>` calls `POST admin/booking-rates-vehicle` and shows the line items, sub total and grand total. | `rates-form.component.ts:772` |
| 8 | **Preview** button | `submitForm(false)`: client-side validation (§7), then the receipt preview modal. | `booking.ts:8072` |
| 9 | **Save** (in preview) | `submitForm(true)` → `createBookingByMode` → `POST …create-reservation` (§2). | `booking.ts:8703` |
| 10 | Confirm modal (edits only) | If the response has `data.is_confirm == false`, it shows `message` and re-submits with `proceed=false`. **For a new booking the backend only sends this on edit, so it does not apply.** | `booking.ts:8380` |
| 11 | Success | Navigates to the role's bookings list. **No booking id is shown in the UI** (the guest flow is the exception). | `navigatePostSave` `booking.ts:8729` |

There is also a **"Within 24 hours" warning** (non-admins only). If the outbound or return pickup is 0–24 h away, a
modal asks for confirmation before saving. It warns but does not block (`isBookingWithin24Hours`, `booking.ts:7942`).

---

## 2. Per role

Portal → mode (host components): admin & sub-admin → `/admin/new-booking-v2` (`mode="admin"`, guarded by
`SubAdminGuard`); travel agent → `travel-agent/create-new-booking-v2` (`mode="travel-agent"`); individual →
`individual/create-new-booking-v2` (`mode="individual"`); affiliate → `affiliate/create-new-booking-v2`
(`mode="affiliate"`); public quotebot → `mode="guest"`.

### 2.1 Submit endpoint per role (`createBookingByMode`, `booking.ts:8703`)

| Role (mode) | Method + URL | Service |
|---|---|---|
| admin, sub_admin | `POST create-reservation` | `admin.service.ts:958` |
| individual | `POST individual/create-reservation` | `individual.service.ts:66` (`booking_created_from` is deleted unless `currentUser.created_by_role=='subscriber'`) |
| travel_agent, sub_travel_agent | `POST travel-planner/create-reservation` | `travel-agent.service.ts:101` |
| affiliate (and sub_affiliate) | `POST affiliate/create-reservation` | `affiliate.service.ts:432` |
| guest (no login) | `POST quote/create-reservation` | `quotebot.service.ts:54` |

All of them send the same form-shaped JSON body (§5) with the Bearer token. Calls used before submit:

| Purpose | Call | Who |
|---|---|---|
| Lookups (airports, airlines, meet & greet list, vehicle categories…) | `bigData$` (`admin.service.ts:19-61`) | all |
| Category list | `POST quote/master-vehicle-listing` | all |
| Category name → filter id | `GET quote/filters` | all |
| Vehicles in category | `POST quote/vehicle-listing` | all |
| Price for the chosen vehicle | `POST admin/booking-rates-vehicle` (public route, `routes/api.php:165`) | all |
| Master vehicle details (category with `is_master_vehicle`) | `GET get-master-vehicle-info/{id}` | all |

### 2.2 Admin / sub_admin: choosing the customer

`account_type` radio: `individual | travel_planner | loose_customer` (subscriber admins get only
`individual | loose_customer`) (`booking.ts:178-179`, `booking.html:141-158`).

- **Existing individual** (`account_type='individual'`):
  - The list comes from `GET get-account-by-type/individual` (`fetchClientAccounts`, `booking.ts:4932`). It is
    loaded **once, in full**. The search box only re-sorts that list on the client by `name` (starts-with first)
    (`handleClientSearch`, `booking.ts:4978`). There is no server-side search by email/phone in this form.
    (The endpoint does accept `search, page, per_page` query params, `utils/affiliate-lookup.ts:119`. Q7.)
  - Picking one sets `acc_id = <tbl_account.id>`. `GET get-an-account/{id}` loads the details and auto-fills the
    passenger fields (`chooseUser`, `booking.ts:5048`). Saved cards are listed via `credit-cards/{id}`.
- **Travel agent's client** (`account_type='travel_planner'`): pick the agent (`acc_id`) from
  `get-account-by-type/travel`. That agent's clients are loaded with `GET get-account-for-travel-agent/{acc_id}`.
  Then either:
  - `travel_client_acc='travel_individual'` → choose `travel_client_id` (required), or
  - `travel_client_acc='travel_loose_customer'` → create a new client (same fields as a loose customer, below).
- **New customer** (`account_type='loose_customer'`): fill `loose_customer.{first_name*, last_name*, middle_name,
  email*, phone*, phone_isd, phone_country, address (optional; country/state/city/zipCode are set from the Google
  place)}`, **plus `card_details.{name*, card_number*(12–20 digits), exp_month*, exp_year*, cvv*(3–4)}` (required
  by the web form)** (`booking.ts:9754-9785`).
  - Backend (`Admin/ReservationsController@createReservation`): it checks that `loose_customer.phone` is unique
    ("Loose customer phone number is already registered…"). It creates a Stripe customer and **only attaches a
    card if `card_number` is non-empty**, so the backend does not require a card. It then creates the account and
    sets `acc_id` to the new account.
- Mapping to the payload: existing → `account_type`, `acc_id` (+ `travel_client_acc`, `travel_client_id` for travel
  planner). New → `account_type='loose_customer'`, `loose_customer{…}`, `acc_id=''`. Backend validation:
  `acc_id` is required unless it is a loose customer / travel loose customer.
- The passenger (`passenger_name`, `passenger_cell`) is separate from the account. It is pre-filled from the
  account and can be overwritten. That is how "booking for someone else" works.
- Admin-only extras: the "Manual" tab (any affiliate / loose affiliate / in-progress affiliate, vehicle, driver),
  `prevent_rate_override` (edits only), editable rate lines. **No past-date check** for admin, and no 24 h warning
  (`validatePastDateTime` returns early for admin, `booking.ts:7907`).

### 2.3 Travel agent / sub travel agent

- `account_type='travel_planner'` is fixed. Super agents choose `sub_account_type` = `travel_agent` (themselves)
  or `sub_travel_agent` + `sub_account_id`. A sub-agent never sees this choice (`booking.html:162-190`).
- Client: `travel_client_acc` = `travel_individual` (pick `travel_client_id` from
  `GET travel-planner/get-account-by-type/individual`; details from `GET travel-planner/get-an-account/{id}`
  auto-fill the passenger) **or** `travel_loose_customer` (new client: the same `loose_customer{…}` fields
  including card).
- `acc_id` is **set by the backend** from the token (`TpReservationsController.php:97-104`). The shell does not
  send it.
- The quote browse view hides the affiliate name/phone (`isTravelAgent`). Driver phone/email are hidden (§3.2).

### 2.4 Individual

- No account choice. `GET individual/get-account-details` loads the user's own account and auto-fills
  passenger name, email and cell (`loadIndividualAccount`, `booking.ts:5296`).
- The backend ignores the client's `acc_id` and uses the token's account (`Individual/ReservationController.php:150-155`).
- The passenger fields can still be edited (booking for someone else). No saved-card section.

### 2.5 Affiliate (and sub_affiliate)

- `account_type`: `individual | loose_customer`. Accounts come from `GET affiliate/get-account-by-type/individual`.
  `isFarmoutBooking=true`. The affiliate list is limited to `affiliate`.
- Not in the brief's role list, but the code supports it. Q8: should the shell support it?

---

## 3. Vehicle selection (what each step needs and shows)

### 3.1 master-vehicle-listing / vehicle-listing request (`buildQuotePayload`, `booking.ts:662-787`)

`pickup_type`/`dropoff_type` = the two halves of `transfer_type` (`city|airport|cruise`). For a city endpoint the
`*_airport*` fields are `null`/`''`. For an airport endpoint, the `*_address` fields **still carry the address
text** (`pickup_address = form.pickup`). The `return_*` fields are filled only for `round_trip` (otherwise `''`).
`return_no_of_passenger/luggage` = outbound values. `location_info` = `[{distance:{text:"x.x km", value:<m>},
duration:{text:"N mins", value:<s>}}]`, plus a second entry for the return leg on round trips. **The backend reads
`location_info[0]` and `[1]` by index, so a round trip must have both.** `other_details` carries the airport display
names. Extra stops are sent as `{address, latitude, longitude}`.

vehicle-listing = the same payload + `filters: {"vehicle-type": [<id from quote/filters for the category name>]}`
+ `selected_amenities: <payload.amenities>`.

If a category has `is_master_vehicle: true`, clicking it **selects it directly**: no vehicle-listing call and no
affiliate. The booking then has `affiliate_type='loose_affiliate'` (or `affiliate` in affiliate mode) and
`affiliate_id=''`, and `get-master-vehicle-info/{id}` fills the vehicle fields.

### 3.2 What each role is shown

| Field | admin | affiliate | individual / guest | travel agent |
|---|---|---|---|---|
| Category: name, pax, luggage, image, price (`rate_breakdown_<service_type>.grand_total`) | ✓ | ✓ | ✓ | ✓ |
| Vehicle: name, year/make/model, pax, luggage, "From <price>" | ✓ | ✓ | ✓ | ✓ |
| Affiliate name/company + affiliate phone (vehicle card) | ✓ | ✓ | ✗ | ✗ |
| Driver name, gender, photo; vehicle type/make/model (after select) | ✓ (manual section) | ✓ | ✓ | ✓ |
| Driver phone | ✓ | only if `canShowContactInfo(status)` | ✗ for a new booking* | ✗ for a new booking* |
| Driver email | ✓ | only if affiliate type + `canShowContactInfo` | ✗ | ✗ |

\* `canShowContactInfo(status)` = admin **or** a booking status that exists and is not
pending/rejected (`booking.ts:2986`). A new booking has no status, so non-admins see no driver phone or email.
The embedded quote shows **nothing else**: no distance-from-pickup, cancellation hours, languages, dress,
experience or amenity list. The brief's richer vehicle line goes beyond the web app. See Q6.

The currency symbol comes from `response.currency.symbol` (master listing). The saved `currency` comes from
`currencyObj`, which is matched from the **dropoff place's country** against `assets/json/currencyOptions.json`
(`updateCurrencyFromAirportPlace`, `booking.ts:4711`).

### 3.3 Amenities

`amenities` / `chargedAmenities` are form arrays sent in the quote payload, and `selected_amenities` is sent to the
listings. In the embedded flow they are **not editable**. Their values come from the quotebot's stored filters.
`booking-rates-vehicle` gets `selected_amenities` from `sessionStorage.filters.request.amenities`
(`admin.service.ts:1266`, overriding the rates-form value). Paid amenities then appear as lines in
`rateArray.amenities`. The form has **no UI to add a paid amenity such as a baby seat**. See Q9.

### 3.4 Price for the chosen vehicle: `POST admin/booking-rates-vehicle` (`rates-form.component.ts:772`, `admin.service.ts:1264`)

Body = `booking_data` (`buildBookingData`, `booking.ts:595`):
`affiliate_id, return_affiliate_id, vehicle_id (the vehicle-listing id; master id for master vehicles),
return_vehicle_id, transfer_type, return_transfer_type, service_type, numberOfVehicles:1, distance (m),
return_distance (m), no_of_hours (0→2), is_master_vehicle, extra_stops[{address,latitude,longitude,rate,booking_instructions}],
return_extra_stops, manual_change_aff_veh, pickup_time, return_pickup_time, affiliate_type, return_affiliate_type,
rate_refresh_token, selected_amenities`.
Response: `data.rateArray` (`all_inclusive_rates`, `direct_taxes`, `taxes`, `amenities`, `misc`; each line has
`baserate`, `amount`, `type`, `rate_label`…), `data.min_rate_involved`, and for round trips a return rate array.
The client then computes each line's `amount` (charter Base_Rate × hours; `percent` taxes = % of the
all-inclusive sum; others `amount = baserate`). `sub_total` = Σ amounts and `grand_total` = sub_total × number_of_vehicles
(`rates-form.component.ts:1137-1194,1278+`).

---

## 4. Per pickup/dropoff type: extra fields

`transfer_type` values (`booking.ts:177`): `city_to_city, city_to_airport, city_to_cruise, airport_to_city,
airport_to_airport, airport_to_cruise, cruise_to_city, cruise_to_airport`. There is **no `cruise_to_cruise`**.
Validators: `updateOutboundLegValidators` / `updateReturnLegValidators` (`booking.ts:9186-9335`). (* = required)

| Endpoint | Fields |
|---|---|
| **Pickup = city** | `pickup`* (address text), `pickup_latitude`, `pickup_longitude` |
| **Pickup = airport** (arrival) | `pickup_airport_option`* (display), `pickup_airport` (internal id), `pickup_airport_name`, `pickup_airport_latitude/longitude`, **`pickup_airline_option`*** (+`pickup_airline` id, `pickup_airline_name` from `BigData.airlinesData`), **`pickup_flight`***, **`origin_airport_city`*** (where the flight comes from), `meet_greet_choices` (id from `BigData.meetGreets`, default 2 "Driver - Airport - Text/call after plane lands with curbside meet location") + `meet_greet_choices_name` |
| **Pickup = FBO** | pickup airport with internal id `3283`, plus `fbo_address` (Google address) and `fbo_name`. Neither is marked required. |
| **Pickup = cruise** | `pickup`* address of the port, **`cruise_port`*, `cruise_name`***, `cruise_time` |
| **Dropoff = city** | `dropoff`*, `dropoff_latitude`, `dropoff_longitude` |
| **Dropoff = airport** (departure) | `dropoff_airport_option`*, `dropoff_airport`, `dropoff_airport_name`, lat/long, **`dropoff_airline_option`***, `dropoff_flight` (**optional**). There is no "departing city" on the outbound leg. |
| **Dropoff = cruise** | `dropoff`*, **`cruise_port`*, `cruise_name`*** (shared with pickup-cruise; one set per leg), `cruise_time` |
| Meet & greet default | `city_…` pickup → 1 "Driver - Text/call when on location"; other pickups → 2 (airport text above) |

**Return leg** (round trip): the same rules on `return_*` fields, with these differences. A return airport pickup requires
`return_pickup_flight`*, `return_pickup_airline_option`*, `return_pickup_airport_option`* and
**`departing_airport_city`*** (the return equivalent of `origin_airport_city`). Return cruise requires
`return_cruise_name`* and `return_cruise_port`*. Then `return_meet_greet_choices(_name)`, `return_fbo_address/name`.
The default return route is the reverse of the outbound (`syncReturnLegFromOutbound`, `booking.ts:7262`).

`booking_instructions` / `return_booking_instructions` are HTML with a default per transfer type (city / airport /
cruise text, `booking.ts:9196-9230`). Each extra stop also has its own `booking_instructions`.

**Backend required fields** (admin controller, lines 89-140): `service_type, transfer_type, pickup_date,
pickup_time, total_passengers, luggage_count, account_type, affiliate_type`. Plus `dropoff_airport` when the dropoff is
an airport, else `dropoff, dropoff_latitude, dropoff_longitude`. The same pattern applies to the return
pickup/dropoff on round trips. Plus `acc_id` (non-loose) or `loose_customer.{first_name,last_name,phone_isd,phone,email}`.

---

## 5. Service types and the full submit payload

### 5.1 Service types (`booking.html:42-50`)
`one_way` · `round_trip` · `charter_tour` (labelled "Charter/Tour").
- **Charter:** `number_of_hours` (sent as `booking_hour` to the listings and `no_of_hours` to rates). The
  **minimum is 2**: values below 2 are an error, and on blur they are forced to 2 (`booking.ts:3361-3427`, submit check `booking.ts:8285`).
  The same pickup and dropoff (and same-place stops) are allowed **only** for charter.
- **More than 2 outbound extra stops auto-converts the booking to `charter_tour`** (hours set to ≥2), and one-way and
  round-trip are then disabled (`charterTourStopThreshold=2`, `booking.ts:334,7420`).
- Round trip: the return pickup date defaults to the outbound date, and the return time defaults to `12:00 pm`.

### 5.2 Submit payload = `BookingForm.value` + additions (`submitForm`, `booking.ts:8330-8360`)

The body is the **entire form value**: every control from `buildBookingForm` (`booking.ts:3069-3260`), including the
empty ones. Source legend: **U** user input, **A** account lookup, **Q** quote/vehicle selection, **R** rates API,
**C** computed, **D** default.

| Group | Fields | Req? | Source |
|---|---|---|---|
| Trip | `service_type`, `transfer_type`, `return_transfer_type`, `number_of_hours` | ✓ | U |
| Date/time | `pickup_date` (YYYY-MM-DD), `pickup_time` (`HH:mm:ss`, from `constant_data.time_values`, 15-min steps), `return_pickup_date`, `return_pickup_time` | ✓ | U |
| Places | `pickup*`, `dropoff*`, `return_pickup*`, `return_dropoff*` (address + lat/long), `*_airport`, `*_airport_option`, `*_airport_name`, `*_airport_latitude/longitude` | per §4 | U + Google + C (internal airport id) |
| Flight/cruise/FBO | `*_airline_option`, `*_airline`, `*_airline_name`, `pickup_flight`, `dropoff_flight`, `return_*_flight`, `origin_airport_city`, `departing_airport_city`, `cruise_port/name/time`, `return_cruise_*`, `fbo_*`, `return_fbo_*` | per §4 | U |
| Stops | `extra_stops[]`, `return_extra_stops[]`, each `{address*, latitude, longitude, rate, booking_instructions, kind, airport…}` (internal keys go too; `buildBookingData` strips them only for the rates call) | | U |
| Pax | `total_passengers` (default 1), `luggage_count` (default 0) | ✓ | U |
| Customer | `account_type`, `acc_id`, `travel_client_id`, `travel_client_acc`, `sub_account_type`, `sub_account_id`, `change_individual_data`, `loose_customer{…, card_details{…}}` | per §2 | U/A |
| Passenger | `passenger_name`*, `passenger_cell`*, `passenger_cell_isd`, `passenger_cell_country`, `passenger_email` | ✓ | A, editable |
| Affiliate | `affiliate_type` ('affiliate'/'loose_affiliate'), `affiliate_id`, `return_affiliate_type/id`, `loose_affiliate_id`, `lose_affiliate_*`, `return_lose_affiliate_*`, `is_old_loose_affiliate`… | ✓ type | Q |
| Vehicle | `vehicle_id` (vehicle-listing `id`), `vehicle_type`* (`vehicle_type_id`), `vehicle_type_name`, `vehicle_make(_name)`, `vehicle_model(_name)`, `vehicle_year(_name)`, `vehicle_color(_name)`, `vehicle_license_plate`, `vehicle_seats`, the same with the `return_` prefix | ✓ type | Q |
| Driver | `driver_id`, `driver_name`, `driver_gender`, `driver_cell(_isd/_country)`, `driver_email`, `driver_phone_type`, `return_driver_*`, `driver_languages[]`, `driver_dresses[]` | | Q (`driverInformation`) |
| Prefs | `meet_greet_choices(_name)`, `return_meet_greet_choices(_name)`, `cancellation_hours`* (from vehicle `cancellation_policy`; options 2…672 h in `assets/json/charterOptions.json`), `return_cancellation_hours`, `booking_instructions`, `return_booking_instructions`, `number_of_vehicles` (D=1) | | Q/D/U |
| Amenities | `amenities[]`, `chargedAmenities[]` | | quotebot filters |
| Misc | `journeyDistance`, `journeyTime`, `returnJourneyDistance`, `returnJourneyTime` (m / s), `reservation_id:''`, `updateType:'create'`, `prevent_rate_override:false`, `susbcriber_name`, `driver_image_id`, `vehicle_image_id` | | C/D |
| **Added at submit** | `booking_created_from`, `proceed:true`, `currency` (`currencyObj.currency`), `platform_type:'web'`, `is_master_vehicle`, `is_pending_affiliate`, `return_is_pending_affiliate`, `reservation_type:'farmout'` (affiliate-created farm-out only) | | C |
| **Rates** | `rateArray` (rates form value without totals), `grand_total`, `sub_total`, `min_rate_involved`, `shares_array`; round trip: `returnRateArray`, `return_grand_total`, `return_sub_total`, `return_shares_array` | ✓ | R + C |

`shares_array` is computed on the client (`createReservationShareArray`, `booking.ts:7698`). `baseRate` = Base_Rate
(×hours for charter) + ELH/Stops/Wait + amenities, × vehicles. `stripeFee` = 5 % of grand total + 0.30.
`adminShare` = 25 % of base (+25 % of Extra_Gratuity). Travel-agent bookings use 15 % admin + 10 % agent share; farm-out
bookings use 15 % admin + 10 % farmout share. **The backend stores `grand_total`, `sub_total`, `rateArray` and
`share_array` as sent** (`ReservationsController.php:302,709-728`).

### 5.3 Phone normalisation done before submit
If a phone value starts with its ISD prefix, the prefix is stripped. All `*_isd` values are given a leading `+`
(`booking.ts:8200-8270`).

---

## 6. Payment

- **The booking form takes no payment.** The booking is saved unpaid (backend status `pending` or
  `driver_unassigned`). Charging happens later in the finalize/payment flows, which are outside this form.
- Saved cards (`credit-cards/{acc_id}` admin; `travel-planner/view-credit-card` and `client-credit-cards/{id}` agent)
  are **only displayed**. Their ids are **not** part of the payload. "Add credit card" uses `<app-new-card-form>`.
- **A new customer (loose / travel-loose) needs raw card number, expiry and CVV in the web form.** The backend sends
  them to Stripe `tokens.create`. **The shell will not collect raw card data.** Options (Q1):
  (a) create the new customer **without a card**. The backend allows this: the card is attached only when
  `card_number` is non-empty. This differs from the web app.
  (b) refuse new-customer creation in the shell ("create the customer in the web app first, then book here").
  (c) a payment/card link. No such endpoint exists today.
- Individual and existing-account bookings need no card input, so they are not affected.

---

## 7. Validations the web app enforces

| Rule | Who | Where |
|---|---|---|
| Required fields per §2 and §4 (+ `vehicle_type`, `cancellation_hours`, `passenger_name`, `passenger_cell` 4–15 digits) | all | form validators |
| Each place must be **picked from Google suggestions** (free text is rejected: "Please select an address from the suggestions list") | all | `validateAddressSuggestions` `booking.ts:2225` |
| Pickup date not before today; date+time not in the past (outbound and return) | **non-admin only** | `validatePastDateTime` `booking.ts:7905` |
| Pickup within the next 24 h → warning modal, must confirm | non-admin | `booking.ts:7942-8000` |
| Charter hours ≥ 2 | all | `booking.ts:8285` |
| Pickup, dropoff and stops must differ (except charter) | all | `getSameLocationError` |
| Route must have non-zero distance (except charter); Google Directions must resolve both legs | all | `booking.ts:8310`, `runEmbeddedQuote` |
| Rate `Base_Rate.baserate > 0` before save ("Base rate can not be empty.") | all | `booking.ts:8363` |
| Loose customer phone must be unique: the admin form pre-checks it with `POST admin/check-unique-user`, and every create controller checks it again | admin (pre-check); all (backend) | `checkUniqueness` `booking.ts:10747`, controllers |
| Paid bookings cannot be edited | edit only | `booking.ts:8762` |
| **Not enforced:** pax/luggage vs vehicle capacity, a minimum notice period beyond the 24 h warning, `vehicle_count=0`. The listing APIs decide which vehicles are returned. | | |

Distance and duration: Google **Directions** (JS SDK). Waypoints are the extra stops and
`provideRouteAlternatives=true`. The **shortest** alternative is used, with legs summed to metres and seconds
(`booking.ts:4380-4470`). Places: Google Places Autocomplete (`places:autocomplete` v1 REST + JS
`PlacesService.getDetails`). The key is read from the Maps `<script>` tag in `index.html` (a browser key; Q2).

---

## 8. Response handling

- Success (admin / TA / individual / affiliate): `{success:true, message:"Reservation has been created successfully.",
  data:{reservation_id, order_id, return_reservation_id?, return_order_id?}}`. `reservation_id` = `tbl_reservations.id`.
  The web app shows **no** id. It just routes to the bookings list (`/admin/daily-bookings-admin`,
  `/<role>/bookings`, `/affiliate/my-bookings`). Guest routes to a confirmation page with `reservation_id` /
  `confirmation_number`.
- Validation failure: HTTP 422 `{success:false, message:"Validation Error", data:{<field>: "<msg>"}}` (one message per
  field), shown to the user in the error dialog. I didn't trace how the dialog is wired for this error. Typical messages: phone already
  registered, card errors from Stripe, "The provided phone number is not valid…".
- `data.is_confirm == false` → confirmation modal → resubmit with `proceed=false`. **Edits only.**
- Side effects on success: booking emails and SMS go to the customer and affiliate (`SendReservationEmail`,
  `SendReservationSms`). **A test booking from the shell triggers real notifications** (Q10).

---

## 9. Open questions

1. **Payment for new customers.** The web app requires raw card details for a new (loose) customer. Which do you want:
   (a) create without a card (the backend allows it), (b) refuse and send them to the web app, (c) something else?
2. **Google APIs from a terminal.** The web key is a browser key (probably referrer-restricted). Is there a
   server-side Places + Directions key I should use (env var name?), or should the shell go through a backend
   proxy? I won't reuse the browser key from `index.html`.
3. **`pickup_airport` value.** The booking form saves the **internal airport id** (nearest `airportsData` record),
   while the quotebot sample sends the Google `place_id`. Which should the shell send to (a) the listing APIs and
   (b) `create-reservation`? (Proposal: internal id everywhere, matching the booking form.)
4. **Vehicle-type filter id.** Should I use the web app's `quote/filters` name→id mapping (proposal), or the master
   listing `id` as the brief says? They may be equal; I'd assert that at runtime.
5. **`user_id` on vehicle-listing.** The web app doesn't send it. The backend uses it only for business-account
   pricing. Send it (per the brief) or match the web app?
6. **Vehicle detail line.** The web app shows less than the brief (no distance, cancellation, languages, dress,
   experience, amenities, affiliate for TA/individual). Mirror the web app exactly, or show the brief's richer line
   (still hiding affiliate, phone and email from non-admins)?
7. **Admin customer search.** The web app loads all individuals and filters by name on the client. OK to call
   `get-account-by-type/individual?search=<q>` (server side, name/email/phone?) instead? I need to confirm which
   columns `search` matches.
8. **Affiliate role.** Should the shell support affiliate-created bookings (`affiliate/create-reservation`)?
   sub_travel_agent: its routes still point at the legacy `CreateBookingComponent`. Should it use the v2 behaviour
   (TA mode, no sub-advisor picker)?
9. **Paid amenities (baby seat…).** The booking form has no UI to add them, so they only arrive via quotebot filters.
   Should the shell offer them (send `amenities`/`chargedAmenities`/`selected_amenities` ids and re-call the
   listings + rates), or leave them out like the form?
10. **Environment.** Book against prod (`api.1800limo.com`) or staging (`1800limoapi.infodevbox.com`) while
    testing? Real bookings send emails and SMS.
11. **`:role` impersonation.** The shell's `:role` mode has no token, so it cannot book. Only `:login` sessions can
    book. Agreed?
12. **Admin driver assignment.** For an admin booking from a quote vehicle, the web app fills the vehicle and
    driver from the affiliate's lists (`fetchQBAffiliateVehicles`, `fetchAffiliateDrivers`). Should the shell send
    the vehicle-listing `driverInformation` as `driver_*`, as the quote-flow roles do, or leave the driver empty
    for the affiliate to assign?
13. **Rates.** I plan to call `admin/booking-rates-vehicle` and reproduce the rates-form totals and `shares_array`
    exactly as in §5.2–5.4, because the backend stores client totals verbatim. Confirm. Alternatively, can the
    backend recompute them?
14. **`PERSON_LOOKUP.md`** is not in the repo. Should I write the disambiguation rule (numbered menu, name/email/
    phone shown masked for non-admins) as part of Phase 2?

### Observed issues (not blocking; for your awareness)
- `create-reservation` trusts the client's `grand_total`, `sub_total`, `rateArray` and `shares_array`. A crafted
  request can set any price.
- `POST individual/create-reservation` sits **outside** the `auth:api` route group (`routes/api.php:729`). The
  controller calls `auth('api')->user()`, so it fails for a guest rather than being open, but the middleware should
  be there.
- `1800-limo/src/environments/*.ts` contain committed secrets (Mistral key, encryption key). They are not reproduced
  here; they should be rotated and moved out of the frontend bundle.

---

## 10. Decisions and the Phase 2 implementation

### 10.1 Answers (from review)

| Q | Decision | Implemented as |
|---|---|---|
| 1 Payment for new customers | Create the booking without a card, then add the card through a form opened from Python that calls the same add-card API | After an admin books for a **new customer**, the shell offers a card form. It runs on `127.0.0.1` with a random token in the URL, and the browser posts the card to the shell, which forwards it once to `POST add-credit-card` with `{id:'', acc_id, card_type:'personal', name, number, cvc, exp_month, exp_year}` (the web `new-card-form` body). `acc_id` comes from `tbl_reservations.acc_id` of the new booking. `:card` does the same for the signed-in user's own account (`individual/add-credit-card`, `travel-planner/add-credit-card`) or, for admins, for `:card ACC_ID`. The card never goes into the terminal, history, logs or the model. |
| 2 Google key | `GOOGLE_MAPS_API_KEY` from limoagent's `.env` | `app/services/booking/maps.py`: Places (New) `places:autocomplete` + place details, and the Directions API with alternatives (the shortest is kept, as the web app does). The key is never printed. |
| 3 Airport id | Internal airport id, from a copy of `airports.json` | `app/services/booking/data/airports.json` (copied from `1800-limo/src/assets/json/`). The id is the airport nearest to the Google place (`resolveInternalAirportRecord`). It is sent to the listings, rates and `create-reservation`. |
| 10 Environment | The backend handles it | `LIMO_API_BASE_URL` (default `https://api.1800limo.com/api/`, the web app's prod `serverUrl`). |
| 13 Rates | Call the rates API and copy the web app's totals math | `app/services/booking/rates.py`: `calculateAmount` / `calculateTotal` / `calculateGrandTotal` and both share arrays, quirks included (see the module docstring). JavaScript `toFixed` rounding is reproduced. A rates answer the web app would crash on or price at 0 (no `Base_Rate`, no `misc.Extra_Gratuity`) is refused instead. |

### 10.2 Defaults taken for the questions still open (change them if wrong)

| Q | Default | Why |
|---|---|---|
| 4 Filter id | The web app's `quote/filters` name → id map. If a name is missing, `filters` is `{}` (the web app then lists every vehicle; the shell says so) | Mirrors the web app |
| 5 `user_id` on vehicle-listing | Sent: the signed-in user's id | The brief asked for it; the backend uses it only for business-account pricing |
| 6 Vehicle line | Mirrors the web app: year/make/model, class, pax/bags, "from" price. Operator name and phone appear only for admins. Driver name and gender appear in the summary; driver phone and email only for admins | §3.2 visibility |
| 7 Admin customer search | Loads the list once (like the web app) and matches the typed text against `name` ("First Last (+isd phone)"), so a name or a phone works | No server-side search needed |
| 8 Roles | admin, sub_admin → admin portal; rider → individual; travel_agent, sub_travel_agent → travel-agent portal (no sub-advisor picker, `sub_account_type='travel_agent'`). Affiliate, corporate and subscriber accounts are told to use the web app | Scope |
| 9 Paid amenities | Not offered (`amenities`, `chargedAmenities`, `selected_amenities` are `[]`) | The booking form has no UI for them |
| 11 `:role` | Cannot book; `:login` is required | There is no token to call the API with |
| 12 Driver | For every role the vehicle-listing `driverInformation` and vehicle details are sent as `driver_*` / `vehicle_*` (the quote-flow patch), and `cancellation_hours` stays at the form default `'24'` | The admin web form instead loads the operator's vehicle/driver lists; the shell does not |
| 14 `PERSON_LOOKUP.md` | Not written. The shell's customer menu is numbered (10 at a time; type more of the name to narrow) | The menu is defined here |

### 10.3 What the shell does

`make shell` → `:login TOKEN` → `:book 5 am pickup on 14 nov from ord to mdw` (or type "book a ride from …" in chat
mode). The flow:

1. **Parses** English and Hinglish (`parse.py`, rule based): dates (`14 nov`, `2026-11-14`, `kal`, `parso`, weekdays),
   times (`5 am`, `17:30`, `subah 5 baje`, `shaam 7 baje`), `from X to Y` / `X se Y`, pax, bags, round trip, and
   charter hours. A relative date (`kal`, `tomorrow`, a weekday) is confirmed. "At 5" asks am or pm. Times must be
   on 15-minute steps.
2. **Asks only what is missing**, in this order: pickup, drop-off, date, time, service type (charter: hours ≥ 2),
   return date/time/route (the reverse, confirmed), passengers, bags. Extra stops are optional and are not asked:
   `add stop <address>`. More than 2 stops turns the booking into a charter, as in the web app.
3. **Resolves places.** An airport code goes to `airports.json`. "Chicago airport" with several airports shows a
   numbered menu (ORD / MDW). Anything else goes to Google autocomplete, and more than one suggestion gives a menu.
   An airport found via Google gets the nearest internal id. A place with "cruise" in it is a cruise end. FBO
   pickups are refused (book them in the web app).
4. **Checks** (non-admins only for time): no past date/time; the same place only for a charter; a driving
   distance > 0.
5. **Lists categories** (`master-vehicle-listing`) in API order. Categories with `vehicle_count = 0` are listed as
   "book now" and picking one books the category as the master vehicle (the web app's Book Now). Ones too small
   for the pax/bags are hidden and named. The price is `rate_breakdown_<service_type>.grand_total`, with the
   currency symbol from the API.
6. **Lists vehicles** (`vehicle-listing` with `filters.vehicle-type`, `user_id`, `selected_amenities: []`), cheapest
   first by `rate_breakdown_<service_type>.grand_total` (select-vehicle's default Low-High sort). A
   master category is selected directly (admin also calls `get-master-vehicle-info`).
7. **Prices** via `admin/booking-rates-vehicle` and the copied rates math. Base rate ≤ 0 → "Base rate can not be
   empty", pick another vehicle.
8. **Customer** per role (§2). Admin: existing customer / a travel agent's client / new customer (first, last,
   email, phone with country code). Travel agent: existing client / new client. Individual: their own account. The
   passenger is auto-filled and can be changed (`change passenger name to …`).
9. **Leg details** per §4: arrival airline, flight and origin city; departure airline and an optional flight
   number; cruise port, ship and time. Then meet & greet for **every** pickup kind (menu; `ok` keeps the default:
   1 for a city pickup, 2 otherwise). The same for the return leg. Last, the default driver instructions for the
   pickup kind are shown and special instructions are asked once (optional: `skip`). They are added to
   `booking_instructions` (and `return_booking_instructions`), and `change notes to …` still works.
10. **Summary**: trip, legs, flights, pax/bags, vehicle, operator (admin), driver, customer, passenger, the price
    lines and total (and a note if it differs from the listing price), payment (none now), and the 24-hour note for
    non-admins. Then **"Confirm booking? (yes/no)"**. Only an explicit yes submits.
11. **Submit once** to the role's `create-reservation` with the full form-shaped payload (§5.2). Success prints the
    reservation id and order id. A 422 is shown in plain words and nothing is resent until the user fixes it and
    says yes again. No response at all → the shell says the outcome is unknown and does not resend.

At any point: `change <field> to <value>` (trip changes clear routes and prices and call the listings again;
the previous category is reopened if it still fits), `back`, `vehicles`, `categories`, `summary`, `cancel`.

Files: `app/services/booking/{api,maps,reference,parse,draft,payload,rates,flow,card_form}.py`, `app/cli/sql_shell.py`
(`:book`, `:card`), settings `LIMO_API_BASE_URL` / `GOOGLE_MAPS_API_KEY`. Tests: `tests/test_booking.py` (the 11
brief scenarios, plus the parser, rates, payload, visibility and HTTP client).

### 10.4 Known gaps

- `platform_type` is sent as `'web'`, the only value the web app uses, so shell bookings look web-made.
- The admin pre-check `admin/check-unique-user` is not called. The backend still rejects a duplicate phone, and that
  error is shown.
- The rates form's "dedicated return fetch" (outbound loose affiliate with an affiliate return) never applies to the
  quote flow, so it is not reproduced.
- Subscriber admins (`booking_created_from='subscriber'`) are not handled.
- A travel agent's **new** client cannot get a card: there is no client add-card endpoint.
- Customer details typed into the shell (names, phones) go into the shell's readline history like any other input.
  Card numbers never do.

## 11. Booking from the web chat (`POST /v1/chat`)

The same `BookingFlow` also runs in the web chat (`app/services/booking/chat.py`, contract in
`docs/API.md` → "Booking a ride"). What differs from the shell:

- **Start:** a chat message matching the booking-request pattern ("book … ride/from/pickup …",
  "gaadi chahiye", …) from a signed-in rider, travel agent or admin. Guests are asked to sign in,
  other roles are told to use the website/app, and without `GOOGLE_MAPS_API_KEY` the chat says
  booking isn't available. `CHAT_BOOKING_ENABLED=false` turns it off.
- **State:** kept in the agent process, keyed by user id + `conversation_id`, dropped after
  `CHAT_BOOKING_TTL_SECONDS` idle (default 30 min). The web widget now sends a `conversation_id`
  (new per "New chat"). No LLM is involved in booking turns; the system prompt tells the model
  how to start a booking and that it can't change or cancel one.
- **Token:** the user's own bearer token from the chat request is passed to the 1800limo API
  (refreshed on every message), never logged.
- **Photos:** category and vehicle menus are sent as cards (`booking.cards`) with the listing
  photos: `cat_img` for `master-vehicle-listing` rows, `vehicle_images` for `vehicle-listing`
  rows (`QuoteController.php:1016` / `:2507`; master vehicles `:2685` / `:2863`). Only absolute
  http(s) URLs are passed on, at most 5 per card. The cards replace the numbered list in the text.
- **Buttons:** `booking.replies` suggests answers for the pending question (Yes/No, One way /
  Round trip / Charter, AM/PM, OK, Skip, Back, Cancel).
- **Disconnects:** a turn runs to completion even if the browser disconnects (it is shielded), so a
  submit can't be cut in half; what that turn said is sent with the next reply.
- **Cards for new customers:** the local card form can't open for a website user, so after an
  admin books a new customer the chat says to add the card from the web app.

Gaps: sessions are per process (several workers need sticky routing or a shared store); the
summary's aligned columns render as plain lines in the widget; numbered menus other than
categories/vehicles (airports, airlines, customers) stay as text.

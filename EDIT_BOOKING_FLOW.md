# EDIT_BOOKING_FLOW.md — how the web app edits a booking (spec for the shell / chat "edit booking" flow)

Companion to `BOOKING_FLOW.md`, which describes creating a booking. Editing uses the **same form**
(`1800-limo/src/app/components/shared/booking/booking.component.{ts,html}`, called `booking.ts` / `booking.html`
below), opened with `?bookingId=<id>&updateType=edit`. Only what is different from creating is described here.
Backend references are in `limoapi/`.

> Status: implemented in the shell (`:edit`) and the web chat (`app/services/booking/edit.py`). §7 records
> the decisions, §8 what was built and how it differs from the plan.

---

## 0. Key facts (they shape the design)

1. **Edit means resubmitting the whole form.** Nothing sends a partial update. The web app loads the booking,
   prefills every form control and then sends the **entire form value** again (the same body as create, §5.2 of
   `BOOKING_FLOW.md`), plus `reservation_id` and `updateType:'edit'`.
2. **Each leg is its own reservation.** A round trip is stored as two rows, both with `service_type='oneway'`. The
   return row has `related_to = <outbound id>` (`Admin/ReservationsController.php:1214-1216,1313`). Editing booking
   2141 edits **only that leg**. The prefill maps `oneway → one_way` (`booking.ts:3745`).
   **If an edit is submitted with `service_type='round_trip'`, the backend creates a new return reservation and
   order** (`ReservationsController.php:1307+,1724+`) and emails admin, affiliate and customer about it. So
   "make it a round trip" during an edit **adds** a leg. It does not edit the existing return leg.
3. **Paid bookings are locked.** `booking_status` `paid` or `paid_cash` → the web app refuses on load and again on
   submit (`isEditingPaidBooking`, `booking.ts:8762`, with the message *"This booking has already been paid and can no
   longer be edited."*). Every backend `editReservation` checks it again (422 *"This booking is already paid!"*).
   Nothing else is locked: cancelled, ended and finalized bookings can still be edited (see Q3).
4. **Ownership is checked by the backend** for individual and travel agent (`belongsToMe`, 422 *"This Booking Might
   not belongs to You"*) on both `get-reservation` and `edit-reservation`. Admin needs the `edit_booking`
   permission (route middleware); `view_booking` is enough to read.
5. **Prices are kept unless something forces a reprice.**
   - Admin: the form sets `prevent_rate_override=true` ("Prevent System Rate Override" checkbox, `booking.html:4615`).
     The saved rate lines are loaded (`GET admin/reservation-rates/{id}`, `admin.service.ts:1250`) and are **not** refetched when the trip
     changes. Only a `service_type` change forces a refetch (`rates-form.component.ts:855-866`).
     Unticking the box refetches `booking-rates-vehicle` at once.
   - Individual / travel agent: `prevent_rate_override=false`, so any trip change refetches
     `admin/booking-rates-vehicle` and reprices (same math as create, `rates.py`).
   - The backend stores whatever `rateArray`, `sub_total`, `grand_total` and `shares_array` it receives, and
     replaces the order lines (`tbl_order_details` deleted and re-added, `ReservationsController.php:1563-1620`).
6. **Changing the operator resets the booking.** If `affiliate_id` (or the loose affiliate) changes, or the booking
   was `driver_unassigned`, the backend cancels the old affiliate's email, sets `booking_status='pending'` and sends a
   new Accept/Reject email (`ReservationsController.php:938-1000,1720-1726`).
7. **The `is_confirm` dialog never fires.** The backend is meant to return `{is_confirm:false}` to warn about a
   change of affiliate, and the web app then resubmits with `proceed=false` (`booking.ts:8382`, `booking.html:4705`).
   But the condition is `!$old->booking_status == "driver_unassigned"`, which is always false (PHP's `!` binds
   first). The shell should still handle the response in case it gets fixed.

---

## 1. Endpoints per role

| Role | Load booking | Save edit | Service |
|---|---|---|---|
| admin, sub_admin | `GET get-reservation/{id}/edit` (perm `view_booking`) | `PUT edit-reservation` (perm `edit_booking`) | `admin.service.ts:949,968` |
| individual | `GET individual/get-reservation/{id}/edit` | **`POST`** `individual/edit-reservation` | `individual.service.ts:57,103` |
| travel_agent, sub_travel_agent | `GET travel-planner/get-reservation/{id}/edit` | `PUT travel-planner/edit-reservation` | `travel-agent.service.ts:93,159` |
| affiliate | `GET affiliate/get-reservation/{id}` | `PUT affiliate/edit-reservation` | `affiliate.service.ts:406,426` (out of scope, as for create) |
| guest | none, the web app redirects to the quote | none | `booking.ts:445` |

Note that individual uses **POST**, while the others use PUT.
`updateType` values `repeat` / `return` / `round` go to `duplicate-reservation` instead. Those are "book again"
actions, not edits, and are out of scope here.

**Load response** (`getReservation`, `ReservationsController.php:1779`): one flat object made from
`tbl_reservations.*` + `tbl_passengers.*` + `tbl_reservation_preference.*` + loose-affiliate columns +
`tbl_order.currency`, with `id = reservation_id = <id>`, airport lat/long added, `extra_stops` /
`return_extra_stops` / `share_array` possibly JSON strings, `service_type` in DB form (`oneway`, `chartertour`),
times as `HH:mm:ss`, `booking_instructions` HTML (the web app strips `<br />`), `meet_greet_choice_name` (the form
calls it `meet_greet_choices_name`), and `pickup_address` / `dropoff_address` (the form calls them `pickup` /
`dropoff`).

**Save response:** `{success:true, message:"Reservation detail has been updated successfully.", data:{reservation_id,
return_reservation_id?, return_order_id?}}`. Errors use the same 422 shape as create.

---

## 2. Web flow, step by step

| # | Step | Code |
|---|---|---|
| 1 | Entry: list page "Edit" link → `create-new-booking-v2?bookingId=…&updateType=edit`. Admin `daily-bookings` shows it when the status is not `paid`/`paid_cash`. Individual and TA reach it via "Cancel Booking" → "Edit / Change" (not shown for paid/cancelled). | `daily-bookings.component.html:88`, `bookings.component.ts:778` |
| 2 | Load: `get-reservation/{id}/edit`. Paid → error dialog, back to the list. | `prefillViaBookingID`, `booking.ts:3458` |
| 3 | Prefill: every scalar is copied into the form (numbers coerced), with the name fixes in §1. Airport/airline display options are rebuilt from `BigData`, plus the extra stops, amenities, languages, dresses, driver/vehicle images, `cancellation_hours` (kept, not reset from the vehicle), `pending affiliate` → `affiliate_type='in_progress_affiliate'`. Admin also loads the customer (`chooseUser`). The vehicle tab opens on **Manual** (admin) with the saved vehicle and driver. | `booking.ts:3530-3800` |
| 4 | The user changes any field. On edit, a time change re-checks past-time / 24 h and refetches rates unless they were typed by hand. | `booking.ts:9344` |
| 5 | Choosing the **same** vehicle again keeps the saved driver. A different vehicle takes its own driver. The cancellation hours are not reset on edit. | `handleSelectVehicleType`, `booking.ts:6215-6270` |
| 6 | Preview → Save. The same validations as create (§7 of `BOOKING_FLOW.md`), plus the paid check. Body = full form + `reservation_id`, `updateType:'edit'`, `proceed:true`. | `submitForm`, `booking.ts:8072-8390` |
| 7 | `is_confirm == false` → confirm modal → resubmit with `proceed=false`. Otherwise navigate to the list. | `booking.ts:8382` |

---

## 3. Shell / chat flow

Entry: `:edit 2141` in the shell, or in the chat "edit booking 2141", "change my booking #2141", "booking 2141 ka time
badalna hai". The intent pattern needs an edit verb (edit, change, modify, update, reschedule, badal…) together
with a booking word and an id. **No id** → "Which booking? Send its number (e.g. 2141)." Only a `:login` / signed-in
rider, travel agent or admin can edit, the same roles that can book.

1. **Load** `get-reservation/{id}/edit` for the role. Then:
   - 404 / empty / ownership 422 → "I couldn't find booking 2141 on your account." (One message for both, so the
     reply does not reveal that a booking exists.)
   - `paid` / `paid_cash` → "Booking 2141 is already paid, so it can't be changed. Please contact support." Stop.
   - Status rules for the others: see Q3.
2. **Build a draft** from the response (`draft_from_reservation`), the reverse of `payload.form_value`: legs, places
   (internal airport ids are kept), flights, cruise, pax/bags, stops, the customer (`acc_id`, account type, travel
   client), the passenger, the vehicle/affiliate/driver fields, cancellation hours, meet & greet, instructions, and the
   saved `rateArray` / totals / `share_array`. **The raw response is kept**, so fields the shell does not model are sent
   back unchanged (`original` overlaid by the shell's changes; see §4).
3. **Show the current booking** as the normal summary, headed "Booking 2141 (pending)". Then ask: "What would you
   like to change? e.g. `change time to 6 pm`, `change pickup to …`, `change passengers to 3`, `add stop …`."
4. **Changes** go through the existing `change <field> to <value>` handling (`flow.py:_change`), limited to the fields
   in §5. Each change re-runs the checks for that field (Google place pick, same-place, past-time and 24 h rule for
   non-admins, charter ≥ 2 h, 15-minute steps).
5. **Reprice** per §6. A changed price is shown as "Price: $120.00 → $135.50".
6. **Diff summary**: only the changed lines as `old → new`, then the new total, then the side effects that apply
   ("the operator will get a new accept request", "notifications are sent"). Then **"Save these changes to booking
   2141? (yes/no)"**. Only an explicit yes submits.
7. **Submit once**: the role's edit endpoint (§1) with the full payload (§4). Success: "Booking 2141 updated." A 422 is
   shown in plain words. `is_confirm:false` → show the backend `message` and ask yes/no, then resend with
   `proceed=false`. No response → "I couldn't confirm the update. Check the booking before trying again." Nothing is resent.
8. `cancel` at any point drops the edit without saving. `summary` shows the current state, `undo` reverts the last
   change.

---

## 4. Edit payload

`submit_payload` as for create, with these differences:

| Field | Value |
|---|---|
| Base | the loaded booking, mapped into form names (§1 fixes), then overlaid with `form_value(draft)` |
| `reservation_id` | the booking id |
| `updateType` | `'edit'` |
| `proceed` | `true` (`false` only on the `is_confirm` resend) |
| `service_type` | `one_way` or `charter_tour`. **Never `round_trip`** (that would create a new leg; Q2) |
| `prevent_rate_override` | admin: `true` unless the shell repriced; others `false` |
| `rateArray`, `sub_total`, `grand_total`, `min_rate_involved`, `shares_array` | the saved values when there is no reprice; the new values after a reprice |
| `acc_id`, `account_type`, `travel_client_*` | unchanged from the booking (the customer is not editable, §5) |
| `affiliate_type` / `affiliate_id` / vehicle / driver | unchanged unless the vehicle was changed; `is_pending_affiliate=1` → `in_progress_affiliate` |
| `cancellation_hours` | unchanged (as in the web app) |
| `currency`, `platform_type:'web'`, `booking_created_from` | as for create |

---

## 5. What can be changed

| Change | Allowed | Effect |
|---|---|---|
| Pickup date / time | ✓ | non-admin: no past time, 24 h warning; reprice (§6) |
| Pickup / drop-off place, extra stops | ✓ | new route + distance; reprice; the transfer type can change (city ↔ airport ↔ cruise) and then the flight/cruise fields for it are asked |
| Flight, airline, origin city, cruise details, meet & greet | ✓ | no reprice |
| Passengers / bags | ✓ | if they exceed the vehicle's seats/bags: "That vehicle seats N. Choose another vehicle?" |
| Passenger name / phone / email | ✓ | no reprice |
| Instructions (notes) | ✓ | no reprice |
| Charter hours | ✓ (charter only, ≥ 2) | reprice |
| One way ↔ charter | ✓ | reprice |
| Vehicle | ✓ via `vehicles` / `categories` | new listing for the current trip; the operator changes → status `pending` + new accept email (fact 6) |
| One way → round trip | Q2 | creates a new return booking |
| Customer (who the booking is for) | admin, travel agent (§9) | the split (`shares_array`) follows the new customer; the passenger is asked about |
| Operator / driver by hand, rate lines by hand | ✗ (admin web app only) | |

---

## 6. Repricing

- **Individual / travel agent** (matching the web app, whose rates form refetches as soon as an edit loads): every
  edit is priced again with `admin/booking-rates-vehicle` for the **same vehicle** and the copied rates math
  (`rates.py`), and the new total is saved. If the vehicle has no rate for the changed trip, the user is told to say
  `vehicles`, `undo` or `cancel`. These roles can't read the saved price (`admin/reservation-rates` is admin only),
  so the summary shows only the new one.
- **Admin** (matching `prevent_rate_override=true`): the saved price (`GET admin/reservation-rates/{id}`) is kept and
  sent back with the saved `share_array`, except when the service type or the vehicle changes. `reprice` switches to
  the system price (like unticking the box), and the summary then shows "Price: $old → $new".

---

## 7. Decisions

| Q | Decision |
|---|---|
| 1 Admin repricing | Keep the saved price by default, `reprice` for the system price |
| 2 One way → round trip | Refused: "book it as a new ride". `change return …` and `add return stop` are refused the same way, and `service_type` is never sent as `round_trip` |
| 3 Statuses | Refused for everyone: `paid`, `paid_cash`, `partial_paid`, `cancelled`, `cancellation_requested`, `ended`, `passenger_drop`, `finalized`, `comped`, `refund`, `partial_refund`, `non_payable`, and rides under way (`start_ride`, `en_route_pu`, `on_location`, `passenger_pick`, `en_route_do`). The list is `LOCKED_STATUSES` in `lookup.py` |
| 4 Edit window | As the web app: non-admins can't move a pickup into the past, and get the 24 h note |
| 5 Round-trip pairs | Warned: the other leg is found by `related_to`, and the change summary warns when the outbound moves after its return (or a return before its outbound) |
| 6 Notifications | Set aside. The summary only says that a new operator is asked to accept the ride |
| 7 No booking number | A numbered list of the user's upcoming, editable bookings (admin: all bookings; others: their own account's). The user can also type a booking number |
| 8 New customer's card | Entered in a card form in the chat widget, which sends it to the API itself (§9). Never through the agent |
| 9 Admin price, new customer | The saved total stays; `shares_array` is worked out again for the new customer |
| 10 Passenger with a new customer | Asked each time |
| 11 Riders | Only the passenger can change; the booking stays on their account |
| 12 Notifications | Whatever the API sends; the agent says nothing about emails |

---

## 8. What was built

- **`api.py`**: `get_reservation`, `edit_reservation` and `edit_method` per role in `RolePaths` (§1), `LimoApi.put` and
  `reservation_rates`.
- **`edit.py`**: `is_edit_request` / `booking_number`; `form_from_reservation` (the web prefill: the booking copied
  into the form's controls with the §1 name fixes) and `draft_from_reservation` (the booking as a `BookingDraft`); and
  `EditFlow`, a `BookingFlow` subclass. It loads the booking, refuses locked ones, shows the summary and asks what to
  change. Changes reuse the booking flow's `change … to …`, place lookups, checks, vehicle lists and rates. Each change
  is answered with an old → new list and "Save these changes to booking #N?". Commands: `vehicles`, `reprice`
  (admin), `undo`, `summary`, `save`, `cancel`.
- **Payload**: the loaded booking in form shape, with **only the fields the changes touched** replaced (the form
  built from the draft before and after the changes is compared key by key). A new vehicle replaces every operator,
  vehicle and driver field. The customer fields are never replaced. A note is added after the saved driver
  instructions, as `<p>…</p>`. Then `reservation_id`, `updateType:'edit'`, `proceed`, the rates (§6) and the phone and
  airport fixes the web app applies at submit. `is_confirm:false` asks "Save anyway?" and resends with `proceed=false`.
- **`lookup.py`**: `ReservationLookup.upcoming` and `.related`, read-only SQL on `tbl_reservations`.
- **Routing**: shell `:edit [ID]`, and an edit request typed in chat mode. In the web chat, `BookingSessions` starts an
  `EditFlow` for an edit request (checked before the booking pattern). When chat booking is off, `chat_service` answers
  an edit request with a fixed reply before the data lookup, so it can no longer time out there. The system prompt
  tells the model how a user starts an edit.
- **Follow-ups**: the chat remembers the booking each conversation last made or edited (for the session TTL), so
  "change the customer" or "update passenger details" right after an edit or a booking reaches the edit flow for that
  booking. Several changes in one message work: "change pax to 4 and bags to 2".
- **Model guard**: the history the language model gets (and the topic check and data lookup) has the flows' summaries,
  change lists and "Booking #N updated." replaced by a placeholder, and `card:` messages hidden. Before this, llama3.2
  copied them and wrote "Saving... Booking #2141 updated." for a change that was never sent (chat log, 28 Oct 08:00).
- **Tests**: `tests/test_booking_edit.py`.

### Gaps

- Not checked against the live API yet: the load → payload round trip is tested on a recorded-shape
  `get-reservation` response, not on real ones for every transfer type (cruise, charter, pending affiliate, FBO).
- FBO bookings load, but a new FBO place is refused, as in booking.
- `undo` keeps the history of one edit session only. A change already saved can't be undone.
- Without a saved flight or airline on an airport leg, the flow asks for it before saving (the web form requires it too).

---

## 9. Changing the customer

`change customer` (or "update the client account", …) for an admin or travel agent opens a menu. Riders are told
the booking stays on their account and how to change the passenger instead.

| Role, booking for | Options |
|---|---|
| admin, a travel agent's client | another client of the same agent · a new client of the same agent · a client of a different agent · an existing individual · a new customer |
| admin, an individual | another existing customer · a travel agent's client · a new customer |
| travel agent | another of your clients · a new client |

The searches and the new-customer questions are the booking flow's. Then "Make X the passenger too?" (1 / 2), the
change list (`Customer: old → new`) and yes/no. What is sent:

- existing customer: `account_type`, `acc_id`, `travel_client_acc`, `travel_client_id` from the pick (a travel agent
  keeps their own `acc_id`, which their edit endpoint requires);
- the split: with the saved price kept (admin), `shares_array` is computed again on the saved rate lines with the new
  customer's flags. As in the web app, an admin edit takes "created by admin" from the booking (`created_by == 1`,
  `booking.ts:3545`), so a travel agent's share appears only on bookings an admin didn't create;
- **a new customer or client**: the edit endpoints require the card with it (`loose_customer.card_details`,
  `ReservationsController.php:832-843`). In the web chat the yes answer returns `booking.card_request = {method,
  path, payload, card_holder}` instead of saving. The widget shows the card form (`app-new-card-form` with
  `collectOnly`), adds the card to `payload.loose_customer.card_details`, sends the request to the 1800limo API
  with the user's token (once more with `proceed:false` if it answers `is_confirm:false`), and posts `card:ok` or
  `card:error <reason>` to the chat. On `card:ok` the agent loads the booking again and only says it is updated when
  its customer really changed; on `card:error` it shows the reason and the form again. The shell doesn't offer new
  customers.

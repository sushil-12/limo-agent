# Limo Booking Chatbot — Data Model & Query Rules

> **Audience:** Claude Code / any LLM agent that writes SQL against the `limoapi` MySQL/MariaDB database to answer chatbot questions.
> **Status:** Confirmed with the product owner on 2026-10-25 against a sample dump. Items marked **UNCONFIRMED** must not be relied on without asking.
> **Golden rule:** Never invent a column, table, or relationship that is not written here. If a question needs something not covered, say so instead of guessing.

---

## 1. Big picture

`tbl_reservations` is the center of everything — one row = one booking (one leg of a trip). Every other table hangs off it:

```
                         tbl_travel_planner  (agency details, when booker is a travel agent)
                                 ▲ acc_id
                                 │
tbl_account (client) ◄── acc_id ─┤
tbl_account (passenger) ◄── travel_client_id
                                 │
                        ┌────────┴─────────┐
                        │ tbl_reservations │──── share_array (JSON: who gets how much)
                        └────────┬─────────┘
        affiliate_id ────────────┼──────────── lose_affiliate
              │                  │                    │
              ▼                  │ id                 ▼
   tbl_account (affiliate login) │           tbl_lose_affiliates
   tbl_affiliate_company         ▼
              │            tbl_order (grand_total, currency)
   driver_id  ▼                  │ id
   tbl_driver_setting            ▼
                           tbl_order_details (price buckets)
```

---

## 2. Tables and joins (all verified on data)

| Need | Join | Notes |
|---|---|---|
| Client who booked | `tbl_account.id = tbl_reservations.acc_id` | Always present. |
| Is the booker a travel agent? | `tbl_reservations.account_type = 'travel_planner'` | Values: `individual`, `travel_planner`, NULL (rare, legacy). |
| Travel agency details | `tbl_travel_planner.acc_id = tbl_reservations.acc_id` | ⚠️ Join on `tbl_travel_planner.acc_id`, **NOT** `tbl_travel_planner.id`. |
| Agent's passenger/client | `tbl_account.id = tbl_reservations.travel_client_id` | Filled for travel-agent bookings. |
| Affiliate (login account) | `tbl_account.id = tbl_reservations.affiliate_id` | The affiliate logs into the app with `tbl_account.user_id`. |
| Affiliate company details | `tbl_affiliate_company.acc_id = tbl_reservations.affiliate_id` | Company name, type, dispatch phone, onboarding/approval status. |
| Driver (of an affiliate) | `tbl_driver_setting.id = tbl_reservations.driver_id` | `tbl_driver_setting.acc_id` = the affiliate's account id. |
| Lose affiliate | `tbl_lose_affiliates.id = tbl_reservations.lose_affiliate` | A new lose-affiliate row is created per booking (`tbl_lose_affiliates.reservation_id = tbl_reservations.id`). |
| Order totals | `tbl_order.reservation_id = tbl_reservations.id` | One order per booking. ⚠️ `tbl_order.id` is **not** the booking number (they match for only ~677 of 2,129 orders). |
| Price breakdown | `tbl_order_details.order_id = tbl_order.id` | Buckets — see §6. Never compare `order_id` with a booking number. |

> The sample dump was intentionally partial (lose affiliates, order details), so missing matches in the sample are expected. In production these joins are complete.

### Two kinds of "driver side" on a booking
A booking is served by **either** an affiliate **or** a lose affiliate (normally only one of the two is filled):

- **Affiliate** (`affiliate_id`) — registered partner company with an app account. Has drivers in `tbl_driver_setting`.
  - Affiliate `acc_id = 4` is **SKYLIMO** (`fleet_operator`, dba `owner`) — the company's own in-house fleet. **Treat it like any other affiliate** (no special handling).
- **Lose affiliate** (`lose_affiliate`) — an external/one-off operator entered by admin. No app account, no `driver_id`.

---

## 3. Who is the driver? (resolution order)

Use the first rule that matches:

1. `driver_id` is set → driver = `tbl_driver_setting` row (`first_name`, `last_name`, `cell_isd` + `cell_number`).
2. `driver_id` is NULL **and** `is_pending_affiliate = 1` → affiliate hasn't finished onboarding; **show the affiliate company as the driver**: `tbl_affiliate_company.company_name`, phone `cell_isd` + `cell_number` (fallback `dispatchisd` + `dispatch`).
3. `affiliate_id` empty **and** `lose_affiliate` set → driver = `tbl_lose_affiliates` (`name`, `operator_name`, `phone_isd` + `phone`).
4. Nothing set → **unassigned**.

"Empty" always means `NULL` **or** `0` → use `COALESCE(col, 0) = 0`.

Legacy note: 11 bookings from 2023–2024 have `affiliate_id` set but no `driver_id` and no pending flag (old development bug). Ignore them; they are past and paid.

---

## 4. Roles & data scoping (MANDATORY on every query)

| Role | How they are identified | Bookings they may see |
|---|---|---|
| **Admin** | admin login | All bookings. |
| **Affiliate** | logs in with `tbl_account.user_id` → resolve to `tbl_account.id` | `tbl_reservations.affiliate_id = <their tbl_account.id>` |
| **Client (individual)** | their `tbl_account.id` | `tbl_reservations.acc_id = <their id>` **or** `travel_client_id = <their id>` (a travel agent booked for them). |
| **Travel agent** | their `tbl_account.id` (`account_type='travel_planner'`) | `tbl_reservations.acc_id = <their id>`; their passengers via `travel_client_id`. |
| Driver / Lose affiliate | **No login yet** | No chatbot access for now. |

Never return another user's rows. Scope filters are added by the backend from the session, never taken from the user's message.

---

## 5. Business rules for common questions

### 5.1 Admin: "How many bookings do I need to work on?"

**Main list — fully unassigned, active, upcoming:**
```sql
SELECT r.id, r.pickup_date, r.pickup_time,
       COALESCE(NULLIF(r.pickup,''),  r.pickup_address)  AS pickup,
       COALESCE(NULLIF(r.dropoff,''), r.dropoff_address) AS dropoff,
       r.booking_status, r.payment_status
FROM tbl_reservations r
WHERE COALESCE(r.affiliate_id, 0)   = 0
  AND COALESCE(r.lose_affiliate, 0) = 0
  AND r.booking_status NOT IN ('cancelled','rejected','refund','finalized','comped','non_payable')
  AND r.status = 'enable'
  AND r.pickup_date >= CURDATE()
ORDER BY r.pickup_date, r.pickup_time;
```
- Use **AND** between the two affiliate checks. Using OR returns almost every booking (each booking normally uses only one of the two fields) — wrong.
- Drop only the `pickup_date` filter if the admin explicitly asks to include past bookings.

**Separate line — pending-affiliate follow-ups (not part of the main count):**
```sql
SELECT r.id, r.pickup_date, r.pickup_time, ac.company_name, ac.account_approval
FROM tbl_reservations r
JOIN tbl_affiliate_company ac ON ac.acc_id = r.affiliate_id
WHERE r.is_pending_affiliate = 1
  AND r.booking_status NOT IN ('cancelled','rejected','refund','finalized','comped','non_payable')
  AND r.pickup_date >= CURDATE();
```
Answer format example:
> "0 bookings need assignment. 1 booking is with a pending affiliate and may need follow-up: #2096 (31 Oct 2026) — Debbie Jansen, onboarding in progress."

### 5.2 Affiliate approval / onboarding
- **Use `tbl_affiliate_company.account_approval`** as the source of truth. Values: `in-progress`, `completed`, `accepted`, `rejected`.
- **Ignore `tbl_affiliate_company.approved`** — it is only set for affiliates created by admin and is misleading (e.g. SKYLIMO has `approved='no'` but `account_approval='accepted'`).
- `step_completed` = JSON array of finished onboarding steps, **0 through 6** for affiliates (7 steps total; all 7 = fully onboarded).
  - ⚠️ Mixed types: elements can be strings or numbers (`["0","1"]`, `[0,1]`, `["0","1",2]`) and order can vary. Count elements / compare as text; do not assume sorted strings.
- Travel agents: `tbl_travel_planner.account_approval` (`accepted`, `pending`, …) and `step_completed` has steps **1–2** (also `step_completed_obj` like `{"step1":"completed","step2":"uncompleted"}`).
- Stripe status (`stripe_conncet_account_id`, `stripe_details_submitted`, `stripe_account_type`, etc.): **out of scope for now** — will be defined later. Any Stripe question (errors, payouts, connect status) gets "Stripe status information isn't available in the chatbot yet." `stripe_account_type` (`custom`/`standard`/`other`) is an account type, **not** an error status.

---

## 6. Money

**Always use stored values. Never recompute prices or markup.**

| Question | Source |
|---|---|
| Total price of booking | `tbl_order.grand_total` (or `share_array.grandTotal` — they match) |
| Currency | `UPPER(tbl_order.currency)` — data has both `USD` and `usd`, plus EUR, GBP, AUD, CAD, NZD, DOP, CZK, INR, ISK. Always show the booking's own currency; never sum different currencies together. |
| Who gets how much | `tbl_reservations.share_array` (JSON) — see below |
| Amount pre-charged to the customer | `tbl_reservations.charged_amount` (some bookings are pre-charged; this is that amount, not the total) |
| Price breakdown (base rate, stops, taxes…) | `tbl_order_details` |

### `share_array` (JSON in `tbl_reservations`)
```json
{"baseRate": 440, "stripeFee": 29.1, "adminShare": 110,
 "grandTotal": 576, "affiliateShare": 466, "deducted_admin_share": 80.9}
```
| Key | Meaning |
|---|---|
| `grandTotal` | Total charged to customer |
| `baseRate` | Base fare |
| `adminShare` | Platform/admin share = **admin profit** |
| `affiliateShare` | What the affiliate gets |
| `travelAgentShare` | What the travel agent gets (present on ~260 bookings) |
| `stripeFee` | Stripe processing fee |
| `deducted_admin_share` | Admin share after deductions |
| `farmoutShare`, `returnGrandTotal` | Ignore for now. |

**Never compute shares — only read and sum stored values.** The formulas once written here (`affiliateShare = grandTotal − adminShare`, `deducted_admin_share = adminShare − stripeFee`) hold for only ~1,227 of 1,500 bookings. A missing key = 0 for that booking (`COALESCE`).

Read with `JSON_UNQUOTE(JSON_EXTRACT(share_array, '$.affiliateShare'))`, cast to `DECIMAL(12,2)` when summing. The target is **MariaDB 10.4: the `->` / `->>` operators do not exist.** Can be NULL/empty on some bookings — filter `share_array LIKE '{%'` and say how many bookings were skipped. `reservation_shares` column is unused (always empty).

### Profit / income / expense — depends on who asks
| Asker | Words | Answer |
|---|---|---|
| Admin | profit, earnings, revenue | `SUM(adminShare)`, plus grandTotal, affiliateShare, travelAgentShare, stripeFee as a breakdown. For a named party ("profit for DG Limo") resolve the party first (person lookup) and filter by its booking column. |
| Affiliate | profit, income, earnings, payout | `SUM(affiliateShare)` over `affiliate_id = their acc_id`; no other shares. |
| Travel agent | profit, commission, earnings | `SUM(travelAgentShare)` over `acc_id = their acc_id`. |
| Individual customer | expense, spent, paid, "profit" | `SUM(tbl_order.grand_total)` over `acc_id = id OR travel_client_id = id`. |

Always one line per currency (`GROUP BY UPPER(tbl_order.currency)`), excluding `booking_status IN ('cancelled','rejected','refund')` (say so), and only the filters the user asked for.

### A named booking
"Rate / breakdown / charges / taxes / grand total of booking N": go through `tbl_order.reservation_id = N` and fetch the booking **whatever its status** (mention it if cancelled/refunded); no status filter, no `GROUP BY`. Taxes = `tbl_order_details` rows with `rate_category IN ('taxes','direct_taxes')` — never a made-up percentage.

### `tbl_order_details` buckets
`rate_category` values: `all_inclusive_rates` (Base Rate, Stops, Wait, Early AM / Late PM / Holiday Charge), `amenities` (Baby Seat, Red Carpet, …), `taxes` (State Tax, Tolls, …), `direct_taxes` (Airport Arrival/Departure Tax, …), `misc` (Parking, Extra Gratuity, …). Show only rows with `amount <> 0`, all of them (no `LIMIT 1`). Bucket amounts are pre-markup, so they **will not** add up to `grand_total` — that's expected; don't "explain" the gap with your own math.

---

## 7. Key reservation columns

| Column | Meaning / gotcha |
|---|---|
| `id` | **The only usable booking reference.** `conf_id` is always empty — never search by it. |
| `pickup_date`, `pickup_time` | Ride date/time. |
| `pickup` / `pickup_address` | For airport pickups `pickup` is blank and the airport text is in `pickup_address`. Always use `COALESCE(NULLIF(pickup,''), pickup_address)`. Same for `dropoff`. |
| `pickup_airport`, `pickup_airline` | **IDs**, not names (lookup tables not provided). The airport name is readable in `pickup_address` (e.g. "ORD - Chicago Ohare Intl, Chicago, United States"). |
| `pickup_flight` | Flight number (free text). |
| `transfer_type` | `airport_to_city`, `city_to_airport`, `city_to_city`, `airport_to_airport`, `city_to_cruise`, `cruise_to_city`, `cruise_to_airport`. |
| `service_type` | `oneway`, `chartertour` (`roundtrip` exists in enum but return legs are stored as separate rows). |
| `extra_stops` | JSON array: `[{"address": "...", "latitude": .., "longitude": ..}]`. |
| `total_passengers`, `luggage_count`, `baby_seat`, `booster_seat`, `child_certified`, `pet_friendly`, `handicap` | Ride requirements (`yes`/`no`). |
| `quote_amount` | **Always NULL — never use it** (totals are `tbl_order.grand_total`). |
| `booking_instructions` | Free-text notes from customer/admin. |
| `booking_status` | See §8. |
| `payment_status` | `paid`, `unpaid`, `partial_paid`, `refund`, `partial_refund`, `paid_cash`, `transfer_failed`. |
| `is_pending_affiliate` | `1` = assigned affiliate hasn't finished onboarding (see §3 rule 2). |
| `status` | `enable`/`disable` — always filter `status = 'enable'`. |
| `cancellation_reason` | Rarely filled. |
| `created_by_role` | `admin`, `individual`, `travel_agent`, `travel_planner`, `affiliate`. |
| `vehicle_type`, `vehicle_id`, `meet_greet_choices` | IDs into lookup tables **not provided** — don't print raw IDs as if they were names. |

---

## 8. `booking_status` → plain English

The enum mixes trip state and payment state. **UNCONFIRMED wording** — the English labels below are a proposed mapping; the closed-status list is what was used and agreed for the "needs work" rule.

| Value | Say |
|---|---|
| `pending` | Awaiting confirmation |
| `assigned` | Confirmed, operator assigned |
| `en_route_pu` / `start_ride` | Driver on the way to pickup |
| `on_location` | Driver at pickup |
| `passenger_pick` / `en_route_do` | Passenger on board |
| `passenger_drop` / `ended` | Ride completed |
| `finalized` | Completed & closed |
| `paid`, `paid_cash`, `partial_paid` | Completed & paid (cash / partially) |
| `comped` | Complimentary ride |
| `cancelled` | Cancelled |
| `cancellation_requested` | Cancellation requested |
| `rejected`, `rejection_requested` | Rejected / rejection requested |
| `refund`, `partial_refund` | Refunded / partially refunded |
| `driver_unassigned` | Driver removed — needs reassignment |
| `non_payable` | Non-payable |

**Closed statuses** (exclude from "active/upcoming/needs work"): `cancelled`, `rejected`, `refund`, `finalized`, `comped`, `non_payable`.

---

## 9. Never expose to users

`ride_otp`, `ride_otp_*`, `email_token`, `tbl_account.phone_otp`, `email_verificaiton_*`, `stripe_customer_id`, `stripe_conncet_account_id`, ID/licence images and numbers (`id_front_image`, `driver_license`, `licence_number`, …), exact lat/long, and any row outside the user's scope (§4). Admin may see contact phone/email of parties on a booking; clients see only their own booking's driver name/phone.

---

## 10. Open / UNCONFIRMED (ask before using)

- **`related_to` vs `booking_related_to`** — both link a booking to another booking (likely return leg / linked trip). 394 rows use `related_to`, 366 use `booking_related_to`. Exact difference not yet confirmed.
- **`passenger_id`** — target table not provided (passenger name/phone source unknown).
- **`sub_account_id`** (12 `sub_travel_agent` bookings) — assumed `tbl_account.id`, not confirmed.
- **Lookup tables** for `vehicle_type`, `pickup_airport`, `pickup_airline`, `meet_greet_choices` — not provided.
- **Stripe** questions — to be specified later.
- Old (pre-2024) `lose_affiliate` IDs may not resolve in `tbl_lose_affiliates` in partial dumps; in production they should.

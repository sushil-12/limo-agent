# limo-rt: the realtime service

Replaces limoRTService (Node + Socket.IO + MongoDB). It handles:

- live driver location;
- ride status writes, ported from Laravel's `POST /api/bookings/status`;
- chat;
- push notifications (FCM device tokens and broadcast topics);
- proximity alerts and the capped SMS fallback;
- share links for the web tracking page.

It lives in the limoagent repo but runs as **its own process** with **its own database
user**. The AI chatbot is unchanged and stays read-only.

```
apps ── wss /rt/v1/ws ──┐                    ┌── FCM HTTP v1
apps ── REST /rt/v1/* ──┤  limo-rt (uvicorn) ├── Laravel /api/sms/send (+ completed-sms)
Laravel ── /internal, /api/notifications ──┘  │
                              Redis (localhost)   MariaDB (limo_rt user)
```

The design, decisions and event contract are in the plan page ("1800LIMO Realtime
Migration"). This file covers running and deploying the service.

## Code map (`app/realtime/`)

| File | What |
|---|---|
| `main.py` | App factory and lifespan |
| `services.py` | The object graph |
| `ws.py` | WebSocket endpoint: hello, subscribe and replay, location, presence |
| `protocol.py` | v1 envelope and frame validation |
| `api.py` | REST: status, location fallback, share links, chat, devices, inbox, trail, flags, broadcast |
| `internal.py` | Laravel → limo-rt: ride events, and the Node-compatible `/api/notifications/*` |
| `status_writer.py` | Port of `recordStatusUpdate`: dedupe, stale check, stop columns, history |
| `events.py` | After-commit fan-out, and the rider/driver message wording |
| `notify.py`, `fcm.py`, `devices.py`, `laravel.py` | Push / SMS decisions and clients |
| `store.py`, `hub.py` | Redis state (streams, location, presence) and pub/sub fan-out |
| `rides.py` | Who is the driver or rider of a ride (cached) |
| `location.py`, `jobs.py` | Location hot path, trail sampling, flush, proximity, retention |
| `flags.py` | Kill switches in `tbl_rt_settings` |
| `tables.py`, `migrations/` | Owned `tbl_rt_*` tables (Alembic) and mirrors of the Laravel tables touched |
| `etl.py` | One-time Mongo → MySQL copy |

## Run locally

```bash
brew services start redis            # or: redis-server
cat docs/realtime/env.realtime.example >> .env   # then fill in the values
make rt-migrate                      # creates tbl_rt_* (needs RT_MIGRATE_DB_*)
make rt-dev                          # http://localhost:9100/rt/v1/health
make rt-test                         # 101 tests, no Redis or MySQL needed (fakeredis + SQLite)
```

## Tested so far

- **Unit and API tests (101).** These cover:
  - Laravel status parity: duplicates, stale updates, charter stop columns, ride-end SMS, and a check against the PHP constants.
  - WebSocket auth and ride access; ordering, replay and gaps.
  - Share-link filtering.
  - Push vs. ack, SMS cap and kill switch, dead-token revocation.
  - Internal API, trail sampling, proximity alerts.
  - The schema-ownership lint and the ETL parsing rules.
- **End-to-end on MariaDB 10.4.28 (XAMPP) with real Redis and uvicorn.**
  - Migration and foreign keys.
  - Grants enforced. The writer can't change prices or other tables, and the read-only user can't write.
  - 12/12 smoke checks passed. Location delivery: p50 4.2 ms on localhost. Status reaches the rider in 32 ms including the DB commit.
  - Replay after reconnect.
  - The replay log survived a hard Redis restart (AOF).

## Moving it to the server

Same host as limoagent (Ollama is no longer used there).

1. **Database accounts.** Edit and run `docs/realtime/GRANTS.sql` as a DB admin:
   - first the `limo_rt_migrate` and `limo_rt` users;
   - then, after step 4, the `tbl_rt_*` grants.

   Check with `SHOW GRANTS` that `limoagent_ro` still has SELECT only.
2. **Redis.**
   ```bash
   sudo apt install redis-server
   sudo mkdir -p /var/lib/redis/limo-rt && sudo chown redis:redis /var/lib/redis/limo-rt
   sudo cp deploy/redis-limo-rt.conf /etc/redis/limo-rt.conf   # set requirepass
   sudo systemctl enable --now redis-server@limo-rt   # or point the default unit at this file
   redis-cli -a '<password>' ping
   ```
   The unit is named `redis-limo-rt.service` in `deploy/limo-rt.service`; adjust `After=` if your distro names it differently.
3. **Code and dependencies.**
   - `git pull`
   - `.venv/bin/pip install -r requirements.txt`
   - Add the variables from `docs/realtime/env.realtime.example` to `/opt/limoagent/.env`.
   - Put the Firebase service-account JSON at `FCM_CREDENTIALS_PATH`, with mode 600 and owned by the service user.
4. **Tables.** Run `make rt-migrate`, then the `tbl_rt_*` grant block from step 1.
5. **Service.**
   ```bash
   sudo cp deploy/limo-rt.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now limo-rt
   curl -s localhost:9100/rt/v1/health   # {"status":"ok","redis":"ok","database":"ok"}
   ```
6. **Proxy and DNS.**
   - Point `rt.1800limo.com` at the host.
   - Install `deploy/nginx-limo-rt.conf` and set the `allow` lines to the Laravel host's private address.
   - Get a TLS certificate.
   - If behind an ALB, set the idle timeout to at least 120 s.
   - Test with `wscat -c wss://rt.1800limo.com/rt/v1/ws`. It should answer `hello_required` after 5 s.
7. **Logs.** `journalctl -u limo-rt -f` gives JSON lines. Ship them with the CloudWatch agent later; nothing to change in the app.

Nothing is live for users until the `rt.provider` flag is switched (see "Cutover").

### Apache instead of nginx

```apache
<VirtualHost *:443>
  ServerName rt.1800limo.com
  ProxyPreserveHost On
  ProxyTimeout 120
  RewriteEngine On
  RewriteCond %{HTTP:Upgrade} =websocket [NC]
  RewriteRule ^/rt/v1/ws$ ws://127.0.0.1:9100/rt/v1/ws [P,L]
  ProxyPass        /rt/v1/ http://127.0.0.1:9100/rt/v1/
  ProxyPassReverse /rt/v1/ http://127.0.0.1:9100/rt/v1/
  <LocationMatch "^/(internal|api)/">
    Require ip 127.0.0.1 10.0.0.0/8
    ProxyPass http://127.0.0.1:9100/
  </LocationMatch>
</VirtualHost>
```
(Needs `mod_proxy`, `mod_proxy_http`, `mod_proxy_wstunnel` and `mod_rewrite`.)

## Laravel side (limoapi)

**Done in code, not deployed.** Covered by `tests/Feature/RealtimeIntegrationTest.php` (8 tests).

| Change | Where |
|---|---|
| New `realtime.secret` middleware: accepts `x-secret` = `REALTIME_INTERNAL_SECRET`. With `:legacy` it also accepts Node's old booking secret during the transition | `app/Http/Middleware/RealtimeSecretAuth.php` |
| `POST /api/internal/rides/{id}/completed-sms` → `SendReservationSms::dispatchRideCompleted` | `app/Http/Controllers/API/Internal/RealtimeController.php`, `routes/api.php` |
| `POST /api/sms/send` now needs the secret. It was open to anyone | `routes/api.php` |
| `user/active-ride` uses the bearer token's user. `?userId=` only works with the secret (Node) | `UserController::getActiveRide` |
| `mobile/v1/config` returns `data.realtime = {provider, url, min_version}` from `tbl_rt_settings`, with the pilot allowlist applied when a bearer token is sent | `app/Services/Realtime/RealtimeConfig.php`, `routes/mobile.php` |
| The mobile notification client now sends `x-secret` and has 3 s / 5 s timeouts. **Node has been rejecting all its calls with 401;** they only reached users through the legacy client's fallback | `app/Services/Mobile/Notification/NotificationService.php` |
| The secret is no longer written to the log | `app/Services/NotificationService.php` |

**No per-controller ride-event calls.** Bookings are changed from ~30 places, several using query-builder updates. Instead, **limo-rt's reservation watcher** (`watcher.py`):
- every 3 s, reads rows whose `updated_at` moved;
- every 60 s, sweeps all open rides;
- emits cancelled / driver changed / updated events from a Redis snapshot.

`POST /internal/v1/ride-events` remains for explicit calls.

**Env to set at cutover (Laravel `.env`):**
```
REALTIME_INTERNAL_SECRET=<same as limo-rt RT_INTERNAL_SECRET>
NOTIFICATION_SERVICE_URL=http://<limoagent-private-ip>:9100
NODEJS_API_URL=http://<limoagent-private-ip>:9100/api
NOTIFICATION_SERVICE_SECRET=<same as RT_INTERNAL_SECRET>
```
Set `REALTIME_INTERNAL_SECRET` now; it is harmless before cutover. Change the other three only at cutover: until then they point at Node.

**After Node is switched off:**
- Remove `:legacy` from the `sms/send` route.
- Remove the secret path in `getActiveRide`.
- Delete `POST /api/bookings/status`.

## Cutover and rollback

- **Pilot.** Set `rt.allowlist_user_ids` to the internal test accounts. Drive real rides.
- **Go live.**
  1. Run the final `python -m app.realtime.etl ...`. It's idempotent, so this is a delta.
  2. `PUT /rt/v1/admin/flags/rt.provider {"value":"python"}`.
  3. Repoint Laravel (C6 step 3).
- **Rollback.** Set the flag back to `node` and revert the Laravel env. Apps switch on their next config fetch.
- **Known rollback gap.** Chat sent while on Python stays in MySQL, so Node won't show it.
- **Remove Node** after 14 clean days.

## Data migration (Mongo → MySQL)

```bash
.venv/bin/pip install -r requirements-migrate.txt
.venv/bin/python -m app.realtime.etl --mongo-uri "$MONGODB_URI" --mongo-db <db> --dry-run   # counts only
.venv/bin/python -m app.realtime.etl --mongo-uri "$MONGODB_URI" --mongo-db <db> --archive-dir /var/backups/limo-rt
```

- Rows for bookings or users that no longer exist are reported and skipped.
- Trails are downsampled to one point per 10 s or 25 m.
- Points older than 12 months go to `--archive-dir` as gzipped JSONL. Use an S3 bucket later.

## Licence scanning (`POST /rt/v1/scan-license`)

This replaces Node's `/api/scan-license`.

- **Input and output are unchanged.** Send a multipart `image` field or JSON `{base64, mimeType}`. The response is Node's JSON shape.
- **Auth:** a Passport token with the driver or admin role. The shared secret is no longer used.
- **Rate limit:** 10 scans per minute per user.
- **Size limit:** 5 MB images; 64 KB for every other route.
- **Code:** `ocr/normalizer.py` is a line-for-line port of `responseNormalizer.ts`. `ocr/documentai.py` calls the Document AI REST API, with no Google SDK. `ocr/images.py` checks file type from the content (magic bytes) and decodes base64.
- **Parity:** a differential run of both normalisers on 2,018 inputs found no differences once Node ran in UTC. Hand-written UK, EU, US AAMVA, NY, Louisiana, Indian and labelled documents, plus random documents.
- **One deliberate difference:** dates like "March 5, 1990" no longer shift a day when the server isn't on UTC.

The port keeps these bugs from the original, so nothing changes for drivers at cutover. Fix them separately with new tests:

| Input | Result today | Should be |
|---|---|---|
| California `LN SAMPLE` / `FN ALEXANDER JOSEPH` | name `LN SAMPLE` | `ALEXANDER JOSEPH SAMPLE` |
| AAMVA `4d DLN 99 999 999` | number `N 99 999 999` | `99 999 999` |
| Indian `Valid Till 14-08-2032` | expiry `2032-04-08` | `2032-08-14` |
| California, no AAMVA codes | address not found | `2570 24TH STREET, ANYTOWN, CA 95818` |

## Not built yet

- **Trail archive to S3.** Retention stays off (`RT_RETENTION_ENABLED=false`) until it exists.
- **Client apps.** iOS driver (`iOSlimodriverapp/1800LIMO/Core/Realtime/`) and iOS passenger (`ioslimouserapp/1800LimoUserApp/Network/Realtime/`): done in code, not yet built or run. Still to do: both Android apps, and web pages using share links.

Pushes also carry limoRTService's key names: `bookingId`, and `type`, which is the status for ride-status pushes and `chat_message` for chat. That way the apps' existing background push handlers understand them (`notify._add_legacy_push_keys`).

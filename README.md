# Limo AI Agent API

A backend-only Python service for the limo platform's AI assistant. The frontend
chat widget (web and mobile) calls it directly with the user's existing Laravel
login token.

- **FastAPI** on Uvicorn (uvloop + httptools), Pydantic v2, orjson, structlog JSON logs
- **Local Llama via Ollama** (native `/api/chat`, NDJSON streaming → SSE)
- **Existing Laravel MySQL**, accessed read-only with async SQLAlchemy 2.1 + aiomysql
- **Laravel Passport** bearer tokens, verified in-process (RS256 + DB checks)

The frontend contract is in **[docs/API.md](docs/API.md)**.

> Laravel owns the schema. This service never creates, alters or migrates tables:
> no `create_all`, no Alembic.

---

## Quick start (local)

Prerequisites: Python 3.12, [Ollama](https://ollama.com), and access to the
Laravel MySQL database.

```bash
make install                 # .venv + pinned dev deps
cp env.example .env          # then edit (see "Configuration from Laravel")
mkdir -p secrets && cp ../limoapi/storage/oauth-public.key secrets/
ollama pull llama3.1:8b      # or: make pull-model
make dev                     # http://127.0.0.1:9000  (docs at /docs outside production)
make test                    # no Ollama/MySQL needed
```

> The env template is named `env.example`. Rename it to `.env.example` if you prefer
> the conventional name.

## Configuration from Laravel

| Here | Copy from | Notes |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_DATABASE` | Laravel `.env` | Same variable names. From Docker use `host.docker.internal`. |
| `DB_USERNAME`, `DB_PASSWORD` | **a new read-only user** (below) | Don't reuse Laravel's account. Any special characters are fine (`URL.create`). |
| `PASSPORT_PUBLIC_KEY_PATH` | `limoapi/storage/oauth-public.key` | Or set `PASSPORT_PUBLIC_KEY` to the PEM (same as Laravel's env var). **Never** copy the private key. |
| `AUTH_ALLOWED_ROLES` | `tbl_user_roles` | Default `{"5":"rider","2":"driver"}`. |
| `DB_TIMEZONE` | `config/app.php` `'timezone' => 'UTC'` | `+00:00`. Change both together if Laravel ever changes. |

### How authentication works (Passport, not Sanctum)

`limoapi` uses **Laravel Passport 11** (league/oauth2-server 8.4, lcobucci/jwt 4).
Access tokens are RS256 JWTs. `app/auth/passport.py` does the same checks as
Passport's `TokenGuard`:

1. Verify the signature with the public key, plus `exp`/`nbf`/`iat` (5 s leeway).
   No database access, so forged or garbage tokens are rejected cheaply.
2. One parameterised query joins `oauth_access_tokens` (by `jti`), `oauth_clients`,
   `users` and `tbl_account`. Reject if:
   - the token is revoked or past `expires_at`;
   - the client is revoked, or its `provider` isn't empty or `users`;
   - `sub`/`aud` don't match the row;
   - `users.soft_delete = 1`;
   - `tbl_account.status = 'disable'` (same as Laravel's `userStatus` middleware);
   - the role isn't allowed.
3. Cache the result for 60 s, keyed by `sha256(token)`; the raw token is never stored or logged.

Every failure returns the same `401`. The reason is logged server-side
(`auth_rejected reason=...`), never returned. `last_used_at` is not updated: Passport
doesn't track it and this service is read-only (TODO in `passport.py`).

> **Known Laravel-side issue:** `TblDriverSetting` (the web `driver` guard) also gets
> Passport tokens (`API/AuthController::generateDriverToken`). If the personal access
> client has `provider = NULL`, the token's `user_id` is ambiguous between
> `users.id` and `tbl_driver_setting.id`. This service only accepts `users` tokens.
> Consider creating a separate personal access client with `provider = drivers` in
> Laravel.

### MySQL: dedicated read-only user

Grant only the columns the service reads. `users` contains `otp`/`password` and
`oauth_clients` contains `secret`; those stay invisible.

```sql
CREATE USER 'limo_agent'@'10.0.%' IDENTIFIED BY '<strong password>';   -- restrict the host

GRANT SELECT (id, user_id, client_id, revoked, expires_at) ON limpapidb.oauth_access_tokens TO 'limo_agent'@'10.0.%';
GRANT SELECT (id, provider, revoked)                       ON limpapidb.oauth_clients       TO 'limo_agent'@'10.0.%';
GRANT SELECT (id, name, email, role, soft_delete)          ON limpapidb.users               TO 'limo_agent'@'10.0.%';
GRANT SELECT (id, user_id, first_name, status, soft_delete) ON limpapidb.tbl_account        TO 'limo_agent'@'10.0.%';
-- Add more SELECT grants as RAG/tools need them. Writes come later (see "Next steps").
```

**Data questions (text-to-SQL)** need SELECT on the business tables they answer from.
The model only ever sees tables and columns this user is granted (read from
`information_schema`), so grants are the outer limit of what can be asked. Start with:

```sql
GRANT SELECT ON limpapidb.tbl_reservations       TO 'limo_agent'@'10.0.%';
GRANT SELECT ON limpapidb.tbl_vehicle_categories TO 'limo_agent'@'10.0.%';
GRANT SELECT ON limpapidb.tbl_invoices           TO 'limo_agent'@'10.0.%';
GRANT SELECT ON limpapidb.tbl_order              TO 'limo_agent'@'10.0.%';
GRANT SELECT ON limpapidb.tbl_driver_feedback    TO 'limo_agent'@'10.0.%';
GRANT SELECT ON limpapidb.tbl_vehicles           TO 'limo_agent'@'10.0.%';
GRANT SELECT (id, reservation_id, passenger_name, created_at) ON limpapidb.tbl_passengers TO 'limo_agent'@'10.0.%';
GRANT SELECT (id, acc_id, first_name, last_name, status, starRatingValue, created_at)
      ON limpapidb.tbl_driver_setting TO 'limo_agent'@'10.0.%';
-- Admin questions about customers/affiliates: grant the columns you want answerable, e.g.
-- GRANT SELECT (last_name, email, city, state, country, created_at) ON limpapidb.tbl_account ...;
-- GRANT SELECT (id, acc_id, company_name, approved, account_approval, created_at) ON limpapidb.tbl_affiliate_company ...;
```

How it works (`app/services/data/`): the topic-guard call also routes data questions
(`DATA`); the model gets a compact description of only the relevant tables
(`catalog.py`, live schema + curated notes) and writes one SELECT; `sql_guard.py`
parses it (sqlglot) and rejects anything but a plain read, then rewrites every table
reference into `(SELECT <allowed columns> FROM t WHERE <requester scope>)` per
`policy.py`; `runner.py` runs it with MySQL `max_execution_time`. Errors go back to the
model for up to `DATA_MAX_ATTEMPTS` tries. The answer is generated from the rows.
Row scoping never depends on the prompt.

Each connection also runs `SET time_zone = '+00:00'; SET SESSION TRANSACTION READ ONLY`
(`DB_READ_ONLY=true`), as a second guard. This form works on both MySQL and MariaDB
(e.g. XAMPP's MariaDB 10.4, which has no `transaction_read_only` variable). MySQL 8's default `caching_sha2_password`
plugin needs the `cryptography` package, which is already pinned.

### Laravel/MySQL conventions handled

- **Timestamps:** Laravel writes UTC (`config/app.php`). The session time zone is pinned
  to `+00:00`, so `TIMESTAMP` values come back in UTC. `DATETIME` columns (e.g.
  `oauth_access_tokens.expires_at`) hold UTC wall-clock values and are compared against
  naive UTC.
- **Soft deletes:** this schema uses a custom `soft_delete tinyint` on `users` and
  `tbl_account`, not Laravel's `deleted_at`. Both are checked. `deleted_at` exists only on
  `mobile_otps`, `mobile_sessions` and `device_tokens`.
- **Booleans** are `tinyint(1)`. Values are typed explicitly in queries (`Boolean()`).
- **JSON columns** (e.g. `users.driver_registration_state`) map to SQLAlchemy `JSON`
  in future mirror models (`app/db/base.py`).
- Laravel runs MySQL with `strict => false`. Keep that in mind once this service writes.
- `utf8mb4` charset on every connection. The engine uses `pool_pre_ping=True` and
  `pool_recycle=1800`; keep `DB_POOL_RECYCLE` below MySQL `wait_timeout`.

## curl examples

```bash
TOKEN='eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9...'   # access_token from the Laravel login API
API=http://127.0.0.1:9000

curl -s $API/health/live
curl -s $API/health/ready | jq
curl -s $API/v1/me -H "Authorization: Bearer $TOKEN" | jq

# Non-streaming
curl -s $API/v1/chat -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Do you do airport pickups?"}],"stream":false}' | jq

# Streaming (SSE); -N disables curl's buffering
curl -N $API/v1/chat -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Do you do airport pickups?"}]}'

# Debug routes (ENABLE_DEBUG_ROUTES=true, never in production)
curl -s $API/v1/debug/db/tables -H "X-Service-Key: $SERVICE_API_KEY" | jq
curl -s $API/v1/debug/db/tables/users -H "X-Service-Key: $SERVICE_API_KEY" | jq
```

## Docker

```bash
cp env.example .env            # set DB_HOST=host.docker.internal for MySQL on the host
mkdir -p secrets && cp ../limoapi/storage/oauth-public.key secrets/
make up                        # api + ollama
make pull-model                # pulls OLLAMA_MODEL into the ollama volume (once)
curl -s localhost:9000/health/ready
make down
```

- Multi-stage image on `python:3.12-slim`, runs as non-root uid 10001, with a
  `HEALTHCHECK` on `/health/live`.
- MySQL is **not** in compose; it's the existing Laravel database.
  `extra_hosts: host.docker.internal:host-gateway` makes the host reachable on Linux too.
- Models persist in the `ollama-models` volume. Ollama listens on `127.0.0.1:11434` only.
- **NVIDIA GPU:** install `nvidia-container-toolkit` on the host and uncomment the
  `deploy.resources` block in `docker-compose.yml`.
- **macOS:** Docker can't use the Apple GPU (~10 tokens/s on CPU vs ~26 on Metal for
  a 3B model on an M1). Run Ollama natively (Ollama.app) with `OLLAMA_MODEL` pulled
  there, and use the `docker-compose.mac.yml` override, which points the API at
  `host.docker.internal:11434` and skips the bundled Ollama. Setting `OLLAMA_BASE_URL`
  in `.env` is not enough: compose's `environment:` overrides it.

  ```bash
  # in .env, so plain `docker compose` / `make up` use the override:
  COMPOSE_FILE=docker-compose.yml:docker-compose.mac.yml
  ```

## Performance

- **Warm-up:** at startup the model is loaded in the background (empty `/api/chat` with
  `keep_alive` and the **same `num_ctx`**; Ollama reloads the model if `num_ctx` changes).
  Startup isn't blocked. `OLLAMA_KEEP_ALIVE=30m` keeps it in memory between chats.
- **Per-request metrics** are logged as `chat_completed`: `total_ms`, `load_ms`, `ttft_ms`,
  `tokens_per_sec`, token counts, message counts and lengths. Message content isn't logged
  (except at `DEBUG` outside production).
- **Ollama server settings** (set on the Ollama process, not here):
  - `OLLAMA_NUM_PARALLEL`: concurrent requests per loaded model (default 1 or 4,
    depending on memory). KV-cache memory grows with `num_parallel × num_ctx`. With
    `num_ctx=8192` and 4 parallel slots, budget for a 32k-token cache. Requests beyond
    the slots queue inside Ollama, which shows up as higher TTFT.
  - `OLLAMA_MAX_LOADED_MODELS`: keep at `1` for a single chat model, so a second model
    doesn't evict it.
  - `OLLAMA_KEEP_ALIVE`: server-wide default; this service also sends it per request.
- **GPU vs CPU** (rough numbers for an 8B Q4 model; measure with the `timings` fields):
  - NVIDIA datacentre/RTX GPU with ≥ 8 GB VRAM: roughly 40–100+ tokens/s, TTFT well under 1 s.
  - Apple Silicon (Metal): roughly 20–40 tokens/s. A 3B model measured ~30 tokens/s on the dev Mac.
  - CPU only: roughly 3–12 tokens/s, with multi-second TTFT on long prompts. Fine for
    development, too slow for production chat.
- **Uvicorn workers:** the service is async I/O-bound and Ollama is the bottleneck, so 1–2
  workers per container are enough (`WEB_CONCURRENCY`). **The rate limiter and the auth
  cache are in memory, per worker**: with N workers a user effectively gets N × the rate
  limit. Move both to Redis before scaling out horizontally.
- Shared pooled `httpx.AsyncClient` (connect 3 s, read `OLLAMA_TIMEOUT` between chunks);
  DB pool 10 + 10 overflow, 5 s checkout timeout; readiness checks run concurrently with
  2 s timeouts.

## Production checklist

- [ ] `APP_ENV=production`: JSON logs, `/docs` hidden, no readiness details.
- [ ] `ENABLE_DEBUG_ROUTES=false` (startup refuses `true` in production).
- [ ] `CORS_ORIGINS` set to the exact widget origins (no `*`).
- [ ] Read-only MySQL user with column grants. `DB_READ_ONLY=true`.
- [ ] Only `oauth-public.key` deployed; the private key stays with Laravel.
- [ ] `SERVICE_API_KEY` is ≥ 32 random chars, stored in the secret manager, never in a frontend.
- [ ] TLS terminated at the proxy. For SSE, Nginx needs `proxy_buffering off` (also sent as
      `X-Accel-Buffering: no`) and `proxy_read_timeout` ≥ `OLLAMA_TIMEOUT`.
- [ ] Set `FORWARDED_ALLOW_IPS` to the proxy's address.
- [ ] Ollama is not exposed publicly. The model is pulled, and `/health/ready` returns 200.
- [ ] GPU host sized for `OLLAMA_NUM_PARALLEL × OLLAMA_NUM_CTX`.
- [ ] One worker, or Redis-backed rate limiting/auth cache, before scaling out.
- [ ] Load balancer: liveness → `/health/live`, readiness → `/health/ready`.
- [ ] Laravel and the Passport tokens are both on UTC.

## Next steps: where things plug in

| Feature | Where |
|---|---|
| **RAG (FAQ answers)** | `ChatService._prepare` in `app/services/chat_service.py`: retrieve passages and add them to the system context. Put the retriever and embeddings (Ollama `/api/embed`) in a new `app/rag/` package. |
| **OpenAI provider** | New `app/llm/openai.py` implementing the `LLMProvider` protocol (`app/llm/base.py`); register it in `app/llm/factory.py`. |
| **Tool calling (bookings, driver registration)** | `ChatService`: route complex intents to the OpenAI provider with tools. Tool implementations go in `app/tools/`, calling `services/` that use `db/`. The SSE contract can add `tool` events without breaking clients. |
| **Server-side history (Redis)** | `ChatService`: load and save turns by `conversation_id`. The field is already in the contract. |
| **Redis rate limit + auth cache** | Implement `RateLimiter` (`app/core/rate_limit.py`) on Redis; move `PassportTokenVerifier`'s cache to Redis. |
| **DB writes** | Grant targeted INSERT/UPDATE, set `DB_READ_ONLY=false`, and add mirror models on `app/db/base.py` (reference: `limoapi/docs/ai-agent/sqlalchemy_models.py`). Follow `08-direct-db-access-guide.md` for side effects Laravel normally performs. |

## Project layout

```
app/
  main.py                     app factory, lifespan, middleware, handlers, routers
  core/      config.py logging.py errors.py rate_limit.py middleware.py responses.py
  auth/      passport.py dependencies.py models.py
  db/        session.py base.py
  llm/       base.py ollama.py factory.py
  services/  chat_service.py prompts.py context_window.py health_service.py schema_inspector.py
  api/       deps.py sse.py routes/{health,me,chat,debug_db}.py
  schemas/   chat.py health.py debug.py errors.py
tests/       conftest.py test_auth.py test_chat.py test_health.py test_debug_db.py test_units.py test_integration.py
docs/API.md  frontend contract
```

Routes stay thin and call `services/`. Services call `llm/` and `db/`. Run
`make lint test` before committing; `make test-integration` runs against real
Ollama and MySQL (set `INTEGRATION_BEARER_TOKEN` to also exercise auth and chat).

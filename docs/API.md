# Limo AI Agent API: frontend contract

This API backs the chat widget in the web and mobile apps. The widget calls it
directly with the user's existing Laravel login token. There is no UI in this service.

- **Base URL:** environment-specific (e.g. `https://agent.example.com`)
- **Versioning:** user-facing endpoints live under `/v1`. Breaking changes ship as `/v2`.
- **Content type:** JSON (`application/json; charset=utf-8`), except chat streaming (SSE).

---

## Authentication

Send the **same access token the Laravel API issued at login** (Laravel Passport):

```
Authorization: Bearer <access_token>
```

- This is the `access_token` the existing login/OTP endpoints return. It's a JWT
  that starts with `eyJ`.
- Tokens are accepted for **riders** (`users.role = 5`) and **drivers** (`users.role = 2`).
  Other roles, revoked or expired tokens, disabled accounts (`tbl_account.status = disable`),
  and deleted users get `401`.
- A token revoked in Laravel (logout) may keep working here for up to **60 seconds**
  (verification cache).
- Every auth failure returns the same `401` body. The response never says why a
  token was rejected.

Call `GET /v1/me` once after login to confirm the token works.

## Request IDs

Every response carries an `X-Request-ID` header, and error bodies repeat it as
`request_id`. You may send your own `X-Request-ID` (8–128 chars, `A-Z a-z 0-9 . _ -`)
to correlate logs. Include the id in bug reports.

## Errors

All errors use one shape:

```json
{
  "error": {
    "code": "validation_error",
    "message": "The request is invalid.",
    "request_id": "6f1c0a8e2b7d4c1e9a3f5b0d7e2c4a18"
  }
}
```

`validation_error` responses also include `details: [{"loc": [...], "message": "..."}]`.
Your message content is never echoed back.

| HTTP | `code` | Meaning / what the widget should do |
|---|---|---|
| 401 | `unauthorized` | Missing/invalid/expired token. Re-authenticate. |
| 404 | `not_found` | Unknown route. |
| 405 | `method_not_allowed` | Wrong HTTP method. |
| 413 | `payload_too_large` | Body over 512 KiB. |
| 422 | `validation_error` | Request breaks the schema or limits (see `details`). |
| 429 | `rate_limited` | Too many chat requests. Wait `Retry-After` seconds. |
| 500 | `internal_error` | Bug. Report the `request_id`. |
| 502 | `llm_error` | The model returned an error. Offer "try again". |
| 503 | `llm_unavailable` | Model server down or model not loaded. Offer "try again later". |
| 503 | `service_unavailable` | Database unreachable while verifying the token. Retry later. |
| 504 | `llm_timeout` | The model took too long. Offer "try again". |

## Rate limits

- `POST /v1/chat`: **20 requests per minute per user** (rolling window; configurable).
  Requests over the limit get `429` with a `Retry-After` header (seconds).
- Rejected requests don't count toward the limit.

## CORS

Only configured origins are allowed. Allowed request headers: `Authorization`,
`Content-Type`, `X-Request-ID`. Readable response headers: `X-Request-ID`,
`Retry-After`. Credentials (cookies) are **not** used; send the bearer token.

---

## Endpoints

### `GET /health/live`

No auth. Is the process up? Returns `200 {"status": "ok"}`.

### `GET /health/ready`

No auth. Checks the database and the model server. Returns `200`, or `503` if
anything is down.

```json
{
  "status": "ok",
  "checks": {
    "database": {"status": "ok", "latency_ms": 2.1, "detail": null},
    "llm": {"status": "ok", "latency_ms": 4.8, "detail": null, "model_available": true}
  }
}
```

`detail` is always `null` in production.

### `GET /v1/me`

Auth required. Returns the user the token belongs to.

```json
{
  "id": 1234,
  "role": "rider",
  "role_id": 5,
  "name": "Asha Rao",
  "first_name": "Asha",
  "email": "asha@example.com",
  "token_id": "8f9c…",
  "client_id": 1,
  "abilities": []
}
```

`role` is `"rider"` or `"driver"`. `abilities` holds the token's OAuth scopes.

### `POST /v1/chat`

Auth and rate limit apply.

**Request**

```json
{
  "messages": [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hello! How can I help?"},
    {"role": "user", "content": "Do you do airport pickups?"}
  ],
  "stream": true,
  "conversation_id": "c_2f8a1b"
}
```

| Field | Type | Rules |
|---|---|---|
| `messages` | array | 1–30 items. Oldest first. The **last item must be `role: "user"`**. |
| `messages[].role` | string | `"user"` or `"assistant"` only. **`"system"` is rejected (422)**; the server adds its own. |
| `messages[].content` | string | 1–4,000 characters, not blank. |
| `stream` | boolean | Default `true`. |
| `conversation_id` | string? | Optional, 1–64 chars of `A-Z a-z 0-9 _ -`. Echoed back. Generated if omitted. |

Unknown fields are rejected.

**The server keeps no history yet.** Send the recent conversation (up to 30
messages) on every request, including the assistant's earlier replies. If the
history is too long for the model's context window, the server drops the oldest
messages; the newest message is always kept. Reuse the `conversation_id` the first
response gave you. When the server starts storing history, the same field will be
used and the contract won't change.

#### Response with `stream: false`

`200 application/json`:

```json
{
  "conversation_id": "c_2f8a1b",
  "content": "Yes! We offer airport pickups at all major airports…",
  "model": "llama3.1:8b",
  "usage": {"prompt_tokens": 312, "completion_tokens": 58},
  "timings": {
    "total_ms": 1840.2,
    "load_ms": 12.4,
    "time_to_first_token_ms": 210.7,
    "tokens_per_sec": 41.3
  }
}
```

`load_ms`, `time_to_first_token_ms` and `tokens_per_sec` may be `null`.

`reply_type` is `"answer"` for a normal reply, `"data"` when the reply was generated
from a database lookup, and `"ask_name"` / `"off_topic"` for fixed replies.

#### Data answers (`reply_type: "data"`)

Signed-in admins, sub-admins, riders and drivers can ask about their data in plain
language ("How many pending bookings do I have?", "When is my next trip?", "Revenue
this month by vehicle type"). The server writes a read-only SQL query, runs it, and
answers from the result. Riders only ever see their own bookings; drivers see trips
assigned to them or their company; admins see platform-wide data. Credentials,
tokens, OTPs and payment instruments are never queryable.

The response then also carries `data`, so the UI can render a table next to the text:

```json
"data": {
  "status": "ok",
  "sql": "SELECT COUNT(*) AS pending FROM tbl_reservations WHERE booking_status = 'pending'",
  "columns": ["pending"],
  "rows": [[2]],
  "row_count": 1,
  "truncated": false,
  "elapsed_ms": 4.1
}
```

| Field | Meaning |
|---|---|
| `status` | `ok`; `no_access` (this account type can't look up data); `unanswerable` (not in the data); `failed`; `timeout` (narrow the question, e.g. a date range) |
| `sql` | The query, **admins only**; `null` for everyone else |
| `rows` | At most 200 rows (`DATA_MAX_ROWS`); `truncated: true` means more exist |

`data` is `null` for every other `reply_type`.

#### Response with `stream: true` (Server-Sent Events)

`200` with headers:

```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Accel-Buffering: no
```

Events arrive in this order:

| Event | `data` | When |
|---|---|---|
| `start` | `{"conversation_id": "...", "model": "..."}` | Immediately, once |
| `data` | Same object as `data` above | Once, before the first `delta`, only for data answers |
| `delta` | `{"content": "..."}` | Once per text chunk; concatenate them |
| `done` | `{"usage": {...}, "timings": {...}, "reply_type": "..."}` | Once, on success (same shapes as above) |
| `error` | `{"code": "...", "message": "..."}` | Instead of `done`, if generation fails |

A stream ends with exactly one `done` or one `error`. While the server waits for
the model (e.g. while it loads), it sends comment lines (`: ping`) every 15
seconds; ignore them. Errors before the stream starts (401, 422, 429) are normal
JSON error responses.

Example wire format:

```
event: start
data: {"conversation_id":"c_2f8a1b","model":"llama3.1:8b"}

: ping

event: delta
data: {"content":"Yes! We"}

event: delta
data: {"content":" offer airport pickups"}

event: done
data: {"usage":{"prompt_tokens":312,"completion_tokens":58},"timings":{"total_ms":1840.2,"load_ms":12.4,"time_to_first_token_ms":210.7,"tokens_per_sec":41.3}}

```

**Stopping:** abort the request (e.g. `AbortController`). The server notices the
disconnect and stops generating.

#### Reading the stream in the browser

`EventSource` can only send GET requests without custom headers, so it **cannot**
send the POST body or the `Authorization` header. Use `fetch()` with a
`ReadableStream` instead:

```js
/**
 * Stream a chat reply. Resolves with the full reply; calls onDelta per chunk.
 * Throws an Error with .code/.status/.requestId on failure.
 */
export async function streamChat({ apiBase, token, messages, conversationId, onDelta, signal }) {
  const res = await fetch(`${apiBase}/v1/chat`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ messages, stream: true, conversation_id: conversationId }),
    signal, // pass an AbortController's signal to stop generation
  });

  if (!res.ok) {
    const { error } = await res.json();
    throw Object.assign(new Error(error.message), {
      code: error.code,
      status: res.status,
      requestId: error.request_id,
      retryAfter: res.headers.get("Retry-After"),
    });
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  const result = { conversationId, content: "", usage: null, timings: null };
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;

    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (!frame || frame.startsWith(":")) continue; // keep-alive ping

      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      const payload = JSON.parse(data);

      if (event === "start") result.conversationId = payload.conversation_id;
      else if (event === "delta") {
        result.content += payload.content;
        onDelta?.(payload.content);
      } else if (event === "done") {
        result.usage = payload.usage;
        result.timings = payload.timings;
      } else if (event === "error") {
        throw Object.assign(new Error(payload.message), { code: payload.code, partial: result.content });
      }
    }
  }
  return result;
}

// Usage
const controller = new AbortController();
const reply = await streamChat({
  apiBase: "https://agent.example.com",
  token: localStorage.getItem("access_token"),
  messages: [{ role: "user", content: "Do you do airport pickups?" }],
  onDelta: (text) => appendToBubble(text),
  signal: controller.signal,
});
// controller.abort() stops generation early.
```

**Native mobile apps:** use the platform's streaming HTTP client (e.g. OkHttp
`EventSource`/`ResponseBody.source()` on Android, `URLSession.bytes(for:)` on iOS)
with the same POST body and header. Or use `stream: false` if streaming isn't
needed.

---

## Internal endpoints (not for frontends)

Mounted only when `ENABLE_DEBUG_ROUTES=true` (never in production). They require
the `X-Service-Key` header; user tokens are not accepted.

- `GET /v1/debug/db/tables`: tables with estimated row counts.
- `GET /v1/debug/db/tables/{name}`: columns, types, nullability, defaults, indexes,
  foreign keys. `404` for anything that isn't a real table.

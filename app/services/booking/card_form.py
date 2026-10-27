"""A one-off card form served on 127.0.0.1, so card numbers are never typed into the terminal.

The browser posts the card to this process, which forwards it once to the role's
``add-credit-card`` endpoint (the same call the web app's new-card form makes) and keeps
nothing: no logging, no history, no model. The URL carries a random token, and the server
stops after a card is added, on timeout, or when the shell is interrupted.
"""

from __future__ import annotations

import asyncio
import html
import re
import secrets
import threading
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from app.services.booking.api import ApiError

Submit = Callable[[dict[str, Any]], Awaitable[str]]  # card fields -> success message
_SUBMIT_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class CardResult:
    added: bool
    message: str


def validate_card(fields: dict[str, str], today: date) -> str | None:
    """The web new-card form's rules (new-card-form.component.ts:58-62, 119)."""
    if not fields.get("name", "").strip():
        return "Enter the name on the card."
    number = re.sub(r"[\s-]", "", fields.get("number", ""))
    if not number.isdigit() or not 12 <= len(number) <= 20:
        return "The card number must be 12 to 20 digits."
    cvc = fields.get("cvc", "").strip()
    if not cvc.isdigit() or len(cvc) > 4:
        return "The CVV must be up to 4 digits."
    try:
        month, year = int(fields.get("exp_month", "")), int(fields.get("exp_year", ""))
    except ValueError:
        return "Choose the expiry month and year."
    if not 1 <= month <= 12 or (year, month) < (today.year, today.month):
        return "The card has expired."
    return None


async def collect_card(
    submit: Submit,
    *,
    title: str,
    default_name: str = "",
    say: Callable[[str], None] = print,
    open_browser: Callable[[str], Any] = webbrowser.open,
    wait_seconds: float = 600.0,
) -> CardResult:
    loop = asyncio.get_running_loop()
    finished: asyncio.Future[CardResult] = loop.create_future()
    token = secrets.token_urlsafe(24)

    def finish(result: CardResult) -> None:
        if not finished.done():
            finished.set_result(result)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path != f"/{token}":
                self._send(404, "<p>Not found.</p>")
                return
            self._send(200, _page(title, default_name, error=None))

        def do_POST(self) -> None:
            if self.path != f"/{token}" or finished.done():
                self._send(404, "<p>This form has expired. Start again from the shell.</p>")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            error = validate_card(form, date.today())
            if error is None:
                fields = {
                    "card_type": "personal",
                    "name": form["name"].strip(),
                    "number": re.sub(r"[\s-]", "", form["number"]),
                    "cvc": form["cvc"].strip(),
                    "exp_month": f"{int(form['exp_month']):02d}",
                    "exp_year": int(form["exp_year"]),
                }
                try:
                    message = asyncio.run_coroutine_threadsafe(submit(fields), loop).result(_SUBMIT_TIMEOUT)
                except ApiError as exc:
                    error = f"The card was not added: {exc.plain()}"
                except Exception:
                    error = "The card could not be sent. Try again."
                else:
                    loop.call_soon_threadsafe(finish, CardResult(added=True, message=message))
                    self._send(200, f"<p>{html.escape(message)}</p><p>You can close this tab.</p>")
                    return
            self._send(200, _page(title, form.get("name", default_name), error=error))

        def _send(self, status: int, body: str) -> None:
            data = _document(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/{token}"
    say(f"  Opening a card form in your browser: {url}")
    say(f"  Waiting up to {int(wait_seconds // 60)} minutes (Ctrl-C to stop).")
    open_browser(url)
    try:
        return await asyncio.wait_for(finished, wait_seconds)
    except TimeoutError:
        return CardResult(added=False, message="The card form timed out; no card was added.")
    finally:
        server.shutdown()
        server.server_close()


def _page(title: str, name: str, *, error: str | None) -> str:
    this_year = date.today().year
    months = "".join(f'<option value="{m}">{m:02d}</option>' for m in range(1, 13))
    years = "".join(f'<option value="{y}">{y}</option>' for y in range(this_year, this_year + 16))
    problem = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<h1>{html.escape(title)}</h1>{problem}
<form method="post" autocomplete="off">
  <label>Name on card <input name="name" value="{html.escape(name)}" required></label>
  <label>Card number <input name="number" inputmode="numeric" autocomplete="off" required></label>
  <div class="row">
    <label>Month <select name="exp_month" required>{months}</select></label>
    <label>Year <select name="exp_year" required>{years}</select></label>
    <label>CVV <input name="cvc" inputmode="numeric" maxlength="4" autocomplete="off" required></label>
  </div>
  <button type="submit">Add card</button>
</form>"""


def _document(body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Add card</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem}}
body{{color:#222;background:#fff}} label{{display:block;margin:.75rem 0}}
input,select{{display:block;width:100%;padding:.5rem;font:inherit;box-sizing:border-box}}
.row{{display:flex;gap:.75rem}} .row label{{flex:1}} button{{padding:.6rem 1.2rem;font:inherit}}
.error{{color:#b00020}}
</style></head><body>{body}</body></html>"""

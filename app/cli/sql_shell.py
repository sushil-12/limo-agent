"""Interactive shell for chatting with the agent and teaching it the right SQL.

Runs the services in-process (no API server needed).

    make shell                                  # as admin
    make shell ARGS="--role rider --user-id 1771"
    make shell ARGS="--login"                   # asks for a Passport token first

Who you are:

    :login [TOKEN]  sign in with a Passport access token, exactly as the app does; without
                    TOKEN it is asked for hidden. LIMO_TOKEN in the environment signs in at start
    :logout         back to admin;  :whoami shows who you are chatting as
    :role ROLE [USER_ID]       act as any user without a token (admin, sub_admin, rider,
                    driver, affiliate_driver)

Two modes:

    chat (default)  the chatbot, as the API answers: replies to anything, and for a data
                    question also shows the tables picked, the SQL and the rows
    :sql            SQL only, faster: no topic check, no written reply, just SQL + rows
    :chat           back to chat mode;  :new starts a new conversation

Booking a ride (needs :login; admin, sub_admin, rider and travel agent accounts):

    :book [TEXT]    start booking, e.g. :book 5 am pickup on 14 nov from ord to mdw
                    (or just type "book a ride from ..." in chat mode). While booking, every
                    line goes to the booking: a number picks from the last list; 'change
                    passengers to 3', 'back', 'vehicles', 'summary', 'cancel' work any time.
                    Nothing is booked until you see the summary and answer yes.
    :card [ACC_ID]  add a payment card in a browser form on this machine (the number is never
                    typed here): your own account, or ACC_ID's account for admins

After a data answer:

    :good           save the last question + SQL to sql_examples.jsonl
    :fix SELECT ... run your SQL for the last question; if it works, save it as the example
    :bad            forget the learned SQL for the last question (the model writes it again)
    :tables [all | name ...]   what the model sees: last question's tables, all, or named ones
    :eval [FILE]    ask every question in FILE (default docs/sql_eval_questions.txt) without the
                    learned cache, to measure the model + examples
    :help, :quit

Saved examples are used from the next question on; edits to NOTES in catalog.py need a
restart. :good saves the question as typed, so train with full questions rather than
follow-ups like "and last month?".
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import os
import sys
import termios
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import text

from app.auth.models import CurrentUser
from app.auth.passport import PassportTokenVerifier
from app.core.config import Settings, get_settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import configure_logging
from app.db.session import create_engine
from app.llm.base import LLMError, LLMMessage, LLMProvider
from app.llm.factory import create_llm_provider, create_ollama_client
from app.schemas.chat import ChatMessageIn, ChatRequest
from app.services.booking.api import BOOKING_MODES, ROLE_PATHS, ApiError, LimoApi
from app.services.booking.card_form import collect_card
from app.services.booking.chat import is_booking_request
from app.services.booking.flow import BookingFlow, add_card
from app.services.booking.maps import GoogleMaps
from app.services.booking.reference import airport_index
from app.services.chat_service import ChatDelta, ChatFailed, ChatService
from app.services.data.agent import DataAgent, DataResult
from app.services.data.catalog import SchemaCatalog, render_tables
from app.services.data.examples import Audience, load_examples, save_example
from app.services.data.policy import DATA_ROLES, FULL_ACCESS_ROLES, PolicyResolver
from app.services.data.query_log import JsonlLog
from app.services.data.runner import QueryRunner
from app.services.data.sql_cache import SqlCache, cache_key
from app.services.requester import Requester
from app.services.topic_guard import TopicGuard

_HISTORY_FILE = Path.home() / ".limo_sql_shell_history"
_CHAT_MESSAGES = 20  # conversation turns sent with each chat message
_DEFAULT_CACHE = Path("data/learned_sql.jsonl")
_DEFAULT_EVAL = Path("docs/sql_eval_questions.txt")
_ADMIN_USER_ID = 1  # any id works for admins; scoped roles need a real users.id
_MAX_CELL = 40


class RecordingAgent(DataAgent):
    """A DataAgent that keeps its last result, so chat mode can show and save the SQL."""

    last: DataResult | None = None

    async def answer(self, requester: Requester, history: Sequence[LLMMessage]) -> DataResult:
        self.last = await super().answer(requester, history)
        return self.last


@dataclass
class Shell:
    agent: RecordingAgent
    eval_agent: DataAgent  # same pipeline without the learned cache
    chat: ChatService
    catalog: SchemaCatalog
    policies: PolicyResolver
    cache: SqlCache
    requester: Requester
    rows: int
    settings: Settings
    engine: Any
    user: CurrentUser | None = None  # set by :login
    verifier: PassportTokenVerifier | None = None
    mode: Literal["chat", "sql"] = "chat"
    question: str | None = None
    result: DataResult | None = None
    history: list[ChatMessageIn] = field(default_factory=list)
    http: httpx.AsyncClient | None = None  # booking API + Google calls
    token: str | None = field(default=None, repr=False)  # the :login token, kept in memory only
    booking: BookingFlow | None = None

    @property
    def audience(self) -> Audience:
        return "admin" if self.requester.role in FULL_ACCESS_ROLES else "scoped"

    @property
    def prompt(self) -> str:
        who = self.requester.role
        if self.user is not None:
            who = f"{self.requester.display_name or self.user.id} {who}"
        elif self.audience == "scoped":
            who += f":{self.requester.user_id}"
        if self.mode == "sql":
            who += " sql"
        if self.booking is not None:
            who += " booking"
        return f"limo[{who}]> "

    async def handle(self, line: str) -> bool:
        """Run one input line; False to quit."""
        if not line.startswith(":"):
            if self.booking is not None:
                await self.booking.handle(line)
                if self.booking.finished:
                    self.booking = None
            elif self.mode == "chat" and is_booking_request(line):
                await self.book(line)
            else:
                await (self.say(line) if self.mode == "chat" else self.ask(line))
            return True
        command, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        match command:
            case "q" | "quit" | "exit":
                return False
            case "h" | "help":
                print(__doc__)
            case "chat" | "sql":
                self.mode = command
                print(f"{command} mode")
            case "new":
                self.history.clear()
                print("new conversation")
            case "good":
                self.good()
            case "fix":
                await self.fix(rest)
            case "bad":
                await self.bad()
            case "tables":
                await self.tables(rest.split())
            case "role":
                await self.role(rest.split())
            case "login":
                if rest:
                    _forget_last_history_line()  # keep the token out of ~/.limo_sql_shell_history
                await self.login(rest or await asyncio.to_thread(_read_secret, "token: "))
            case "logout":
                await self.role(["admin"])
            case "whoami":
                self.whoami()
            case "book":
                await self.book(rest)
            case "card":
                await self.card(rest)
            case "eval":
                await self.evaluate(Path(rest) if rest else _DEFAULT_EVAL)
            case _:
                print(f"unknown command :{command} (try :help)")
        return True

    async def say(self, message: str) -> None:
        """One chat turn through ChatService, streamed, with the lookup shown under the reply."""
        self.history.append(ChatMessageIn(role="user", content=message))
        del self.history[:-_CHAT_MESSAGES]
        while self.history[0].role != "user":
            self.history.pop(0)
        self.agent.last = None
        started = time.perf_counter()
        reply: list[str] = []
        print("  ", end="", flush=True)
        async for event in self.chat.stream(self.requester, ChatRequest(messages=self.history)):
            if isinstance(event, ChatDelta):
                reply.append(event.content)
                print(event.content.replace("\n", "\n  "), end="", flush=True)
            elif isinstance(event, ChatFailed):
                print(f"[failed: {event.message}]", end="")
        print()
        took = time.perf_counter() - started
        if reply:
            self.history.append(ChatMessageIn(role="assistant", content="".join(reply)))
        else:
            self.history.pop()  # keep the conversation alternating after a failure
        if self.agent.last is None:
            print(f"  ({took:.1f}s, no lookup)")
            return
        self.question, self.result = message, self.agent.last
        print("  --- lookup")
        _print_result(self.result, took, self.rows)

    async def ask(self, question: str) -> None:
        started = time.perf_counter()
        result = await self.agent.answer(self.requester, [LLMMessage("user", question)])
        self.question, self.result = question, result
        _print_result(result, time.perf_counter() - started, self.rows)

    def good(self) -> None:
        if self.question is None or self.result is None or self.result.status != "ok" or not self.result.sql:
            print("nothing to save: ask a question that returns rows first")
            return
        self._save(self.question, self.result.sql)

    async def fix(self, sql: str) -> None:
        if self.question is None:
            print("ask the question first, then :fix SELECT ...")
            return
        if not sql:
            print("usage: :fix SELECT ...  (one line)")
            return
        # Run it the way a learned query runs: guarded and scoped to the current role.
        key = cache_key([LLMMessage("user", self.question)], 1)
        await self.cache.remember(self.requester.role, key, self.question, sql)
        if self.cache.get(self.requester.role, key) != sql:
            print("not saved: the SQL has a fixed date or year the question doesn't mention")
            return
        started = time.perf_counter()
        result = await self.agent.answer(self.requester, [LLMMessage("user", self.question)])
        self.result = result
        first = result.attempts[0] if result.attempts else None
        if first is None or first.error is not None:
            print(f"your SQL failed: {first.error if first else 'not run'}")
            print("the model's answer instead:")
            _print_result(result, time.perf_counter() - started, self.rows)
            return
        _print_result(result, time.perf_counter() - started, self.rows)
        self._save(self.question, sql)

    async def bad(self) -> None:
        if self.question is None:
            print("nothing to forget")
            return
        key = cache_key([LLMMessage("user", self.question)], 1)
        if self.cache.get(self.requester.role, key) is None:
            print("not learned; ask again to get a new query, or :fix SELECT ...")
            return
        await self.cache.forget(self.requester.role, key, "marked bad in sql_shell")
        print("forgotten; ask again for a new query, or :fix SELECT ...")
        if self.result is not None and self.result.sql:
            print("(if this SQL is also in sql_examples.jsonl, :fix it or edit the file)")

    async def tables(self, names: list[str]) -> None:
        policy = await self.policies.policy_for(self.requester)
        if policy is None:
            print("this role sees no data")
            return
        schema = await self.catalog.schema()
        visible = {
            table: cols
            for table, columns in schema.items()
            if (cols := policy.visible_columns(table, [c.name for c in columns]))
        }
        if names == ["all"] or (not names and self.result is None):
            for table in sorted(visible):
                print(f"  {table} ({len(visible[table])} columns)")
            print(f"{len(visible)} tables visible to {self.requester.role}")
            return
        wanted = names or (self.result.tables if self.result else [])
        missing = [t for t in wanted if t not in visible]
        if missing:
            print(f"not visible to {self.requester.role}: {', '.join(missing)}")
        shown = [t for t in wanted if t in visible]
        columns = {t: [c for c in schema[t] if c.name in set(visible[t])] for t in shown}
        print(render_tables(shown, columns))

    async def role(self, args: list[str]) -> None:
        if not args or args[0] not in DATA_ROLES:
            print(f"usage: :role ROLE [USER_ID]   roles: {', '.join(sorted(DATA_ROLES))}")
            return
        role = args[0]
        if role in FULL_ACCESS_ROLES:
            user_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else _ADMIN_USER_ID
        elif len(args) > 1 and args[1].isdigit():
            user_id = int(args[1])
        else:
            print(f"{role} needs a users.id: :role {role} 1771")
            return
        self.requester = Requester(role=role, display_name=None, user_id=user_id)
        self.user = self.token = self.booking = None
        self.question = self.result = None
        self.history.clear()
        print(f"now asking as {role} (user {user_id})")

    async def login(self, token: str) -> None:
        token = token.strip().removeprefix("Bearer ").strip()
        if not token:
            print("no token given")
            return
        if self.verifier is None:
            try:
                self.verifier = PassportTokenVerifier(
                    self.engine,
                    self.settings.passport_public_key_pem(),
                    allowed_roles=self.settings.auth_allowed_roles,
                    leeway_seconds=self.settings.passport_leeway_seconds,
                )
            except (OSError, ValueError) as exc:
                print(f"cannot check tokens: {exc}")
                return
        try:
            result = await self.verifier.authenticate(token)
        except ServiceUnavailableError:
            print("cannot check the token: the database is unreachable")
            return
        if isinstance(result, str):
            print(f"token rejected: {result}")
            if result == "role_not_allowed":
                print(f"  allowed roles (AUTH_ALLOWED_ROLES): {self.settings.auth_allowed_roles}")
            return
        self.user = result
        self.token, self.booking = token, None
        self.requester = Requester.from_user(result)
        self.question = self.result = None
        self.history.clear()
        self.whoami()
        if not self.agent.supports(self.requester):
            print("  this role cannot look up data; chat only")

    def _booking_api(self) -> tuple[LimoApi, str] | None:
        """The signed-in user's API client and portal mode, or None (with the reason printed)."""
        if self.user is None or self.token is None:
            print("booking needs a real login: :login TOKEN (:role has no token to call the API with)")
            return None
        mode = BOOKING_MODES.get(self.user.role)
        if mode is None:
            print(f"the shell can't book as {self.user.role}; use the web app")
            return None
        if self.http is None:
            self.http = httpx.AsyncClient()
        return LimoApi(self.settings.limo_api_base_url, self.token, self.http), mode

    async def book(self, request: str) -> None:
        if self.booking is not None:
            print("already booking; finish it or say 'cancel' first")
            return
        found = self._booking_api()
        if found is None:
            return
        api, mode = found
        key = self.settings.google_maps_api_key
        if key is None:
            print("booking needs GOOGLE_MAPS_API_KEY in .env (a server-side Places + Directions key)")
            return
        self.booking = BookingFlow(
            mode=mode,
            user_id=self.user.id if self.user else 0,
            api=api,
            maps=GoogleMaps(key.get_secret_value(), api.client),
            airports=airport_index(),
            account_for_reservation=self._reservation_account if mode == "admin" else None,
        )
        await self.booking.start(request)
        if self.booking.finished:
            self.booking = None

    async def _reservation_account(self, reservation_id: int) -> int | None:
        """tbl_reservations.acc_id of a booking this admin just created (for its new customer's card)."""
        async with self.engine.connect() as conn:
            row = await conn.execute(
                text("SELECT acc_id FROM tbl_reservations WHERE id = :id"), {"id": reservation_id}
            )
            value = row.scalar_one_or_none()
        return int(value) if value else None

    async def card(self, rest: str) -> None:
        found = self._booking_api()
        if found is None:
            return
        api, mode = found
        acc_id: int | None = None
        if mode == "admin":
            if not rest.strip().isdigit():
                print("usage: :card ACC_ID   (the tbl_account.id to add the card to)")
                return
            acc_id = int(rest)
        elif rest.strip():
            print(":card adds a card to your own account; it takes no account id for your role")
            return
        path = ROLE_PATHS[mode].add_credit_card
        try:
            result = await collect_card(
                lambda fields: add_card(api, path, fields, acc_id=acc_id),
                title="Add a payment card" + (f" to account {acc_id}" if acc_id else ""),
            )
        except ApiError as exc:
            print(f"card not added: {exc.plain()}")
            return
        print(f"  {result.message}")

    def whoami(self) -> None:
        if self.user is None:
            print(f"no token; acting as {self.requester.role} (user {self.requester.user_id})")
            return
        name = self.user.name or self.user.first_name or "-"
        print(f"signed in as {name} <{self.user.email or '-'}>  user {self.user.id}, role {self.user.role}")

    async def evaluate(self, path: Path) -> None:
        questions = await asyncio.to_thread(_read_questions, path)
        if questions is None:
            print(f"no such file: {path}")
            return
        statuses: Counter[str] = Counter()
        seconds: list[float] = []
        for number, question in enumerate(questions, start=1):
            started = time.perf_counter()
            result = await self.eval_agent.answer(self.requester, [LLMMessage("user", question)])
            took = time.perf_counter() - started
            statuses[result.status] += 1
            seconds.append(took)
            retries = len(result.attempts) - 1
            print(
                f"{number:>3}. {result.status:<11} {took:5.1f}s  rows={len(result.rows):<4}"
                f"{f' retries={retries}' if retries > 0 else ''}  {question}"
            )
            if result.sql:
                print(f"     {_one_line(result.sql)}")
            if result.status != "ok" and result.attempts and result.attempts[-1].error:
                print(f"     -> {result.attempts[-1].error}")
        if seconds:
            summary = ", ".join(f"{status} {count}" for status, count in statuses.most_common())
            print(f"\n{len(questions)} questions: {summary}; avg {sum(seconds) / len(seconds):.1f}s")
            print("review the SQL above; ask a wrong one again and :fix it to teach the right query")

    def _save(self, question: str, sql: str) -> None:
        example, replaced = save_example(self.audience, question, sql)
        self.agent.add_example(example)
        self.eval_agent.add_example(example)
        print(f"{'replaced' if replaced else 'saved'} {self.audience} example: {example.question}")


def _read_questions(path: Path) -> list[str] | None:
    """One question per line, # comments skipped; None if the file doesn't exist."""
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    return [q.strip() for q in lines if q.strip() and not q.lstrip().startswith("#")]


def _print_result(result: DataResult, seconds: float, max_rows: int) -> None:
    source = "learned" if result.cached else f"model, {len(result.attempts)} attempt(s)"
    print(f"  tables: {', '.join(result.tables) or '-'}")
    for index, attempt in enumerate(result.attempts, start=1):
        if attempt.error is not None:
            print(f"  [attempt {index} failed] {_one_line(attempt.sql or '(no SQL)')}")
            print(f"      -> {attempt.error}")
    if result.sql:
        print(f"  SQL:    {_one_line(result.sql)}")
    print(f"  {result.status} in {seconds:.1f}s ({source})")
    if result.status == "ok":
        _print_rows(result.columns, result.rows, max_rows, truncated=result.truncated)


def _print_rows(columns: list[str], rows: list[list[Any]], limit: int, *, truncated: bool) -> None:
    if not rows:
        print("  (no rows)")
        return
    shown = [[_cell(v) for v in row] for row in rows[:limit]]
    widths = [max(len(c), *(len(r[i]) for r in shown)) for i, c in enumerate(columns)]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in shown:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    more = len(rows) - len(shown)
    if more > 0 or truncated:
        print(f"  ... {more} more row(s){' (result capped by DATA_MAX_ROWS)' if truncated else ''}")


def _cell(value: Any) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 1] + "…"


def _one_line(sql: str) -> str:
    return " ".join(sql.split())


async def _read_line(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _warm_up(provider: LLMProvider) -> None:
    with contextlib.suppress(LLMError):
        await provider.warmup()


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging("WARNING", json_logs=False)
    if args.role not in DATA_ROLES:
        print(f"unknown role {args.role!r}; one of {', '.join(sorted(DATA_ROLES))}")
        return 2
    user_id = args.user_id or (_ADMIN_USER_ID if args.role in FULL_ACCESS_ROLES else None)
    if user_id is None:
        print(f"--user-id is required for role {args.role!r}")
        return 2

    engine = create_engine(settings)
    client = create_ollama_client(settings)
    provider = create_llm_provider(settings.llm_provider, settings, client)
    sql_provider = (
        create_llm_provider(settings.llm_provider, settings, client, model=settings.ollama_sql_model)
        if settings.ollama_sql_model
        else provider
    )
    catalog = SchemaCatalog(engine, ttl_seconds=settings.data_schema_cache_seconds)
    policies = PolicyResolver(engine)
    runner = QueryRunner(
        engine, timeout_seconds=settings.data_query_timeout_seconds, max_rows=settings.data_max_rows
    )
    cache = SqlCache(
        settings.data_sql_cache_path or _DEFAULT_CACHE, max_entries=settings.data_sql_cache_max_entries
    )
    query_log = JsonlLog(settings.data_query_log_path) if settings.data_query_log_path else None
    examples = load_examples()

    def build(kind: type[DataAgent], sql_cache: SqlCache | None) -> Any:
        return kind(
            sql_provider,
            catalog,
            policies,
            runner,
            max_rows=settings.data_max_rows,
            max_attempts=settings.data_max_attempts,
            examples=list(examples),
            query_log=query_log,
            sql_cache=sql_cache,
        )

    agent: RecordingAgent = build(RecordingAgent, cache)
    chat = ChatService(
        provider,
        num_ctx=settings.ollama_num_ctx,
        reserved_for_reply=settings.ollama_num_predict,
        topic_guard=TopicGuard(provider, block_off_topic=settings.topic_filter_enabled),
        data_agent=agent,
        chat_log=JsonlLog(settings.chat_log_path) if settings.chat_log_path else None,
    )
    shell = Shell(
        agent=agent,
        eval_agent=build(DataAgent, None),
        chat=chat,
        catalog=catalog,
        policies=policies,
        cache=cache,
        requester=Requester(role=args.role, display_name=None, user_id=user_id),
        rows=args.rows,
        settings=settings,
        engine=engine,
    )
    warmups = [
        asyncio.create_task(_warm_up(p))
        for p in {provider.model: provider, sql_provider.model: sql_provider}.values()
    ]
    models = provider.model if sql_provider is provider else f"{provider.model}, SQL {sql_provider.model}"
    print(f"model {models}, {len(examples)} examples, {len(cache)} learned queries. :help for commands")
    try:
        if token := os.environ.get("LIMO_TOKEN"):
            await shell.login(token)
        elif args.login:
            await shell.login(await asyncio.to_thread(_read_secret, "token: "))
        if shell.user is None:
            shell.whoami()
            print("  (:login TOKEN to chat as a real user, with their name and data)")
        while True:
            try:
                line = (await _read_line(shell.prompt)).strip()
            except EOFError:
                print()
                break
            if not line:
                continue
            try:
                if not await shell.handle(line):
                    break
            except LLMError as exc:
                print(f"model error: {exc}")
    finally:
        for warmup in warmups:
            warmup.cancel()
        await asyncio.gather(*warmups, return_exceptions=True)
        await client.aclose()
        if shell.http is not None:
            await shell.http.aclose()
        await engine.dispose()
    return 0


def _setup_history() -> None:
    try:
        import readline  # optional: arrow keys and history where available
    except ImportError:
        return
    with contextlib.suppress(OSError):
        readline.read_history_file(_HISTORY_FILE)
    readline.set_history_length(1000)
    atexit.register(lambda: _save_history(readline))


def _save_history(readline: Any) -> None:
    with contextlib.suppress(OSError):
        readline.write_history_file(_HISTORY_FILE)


def _read_secret(prompt: str) -> str:
    """Read a line without echoing it, like getpass, but with no length limit.

    A terminal in line mode drops input past 1024 bytes on macOS, Enter included, so a
    pasted Passport token (1000+ characters) never arrives. Reading key by key avoids that.
    """
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    raw = termios.tcgetattr(fd)
    raw[3] &= ~(termios.ICANON | termios.ECHO)  # Ctrl-C still interrupts (ISIG stays on)
    raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
    print(prompt, end="", flush=True)
    typed: list[str] = []
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, raw)
        while True:
            chunk = os.read(fd, 4096).decode(errors="ignore")
            chunk = chunk.replace("\x1b[200~", "").replace("\x1b[201~", "")  # bracketed paste
            for char in chunk:
                if char in "\r\n":
                    return "".join(typed)
                if char == "\x04" and not typed:  # Ctrl-D
                    return ""
                if char in "\x7f\b":
                    if typed:
                        typed.pop()
                elif char.isprintable():
                    typed.append(char)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        print()


def _forget_last_history_line() -> None:
    try:
        import readline
    except ImportError:
        return
    if (length := readline.get_current_history_length()) > 0:
        readline.remove_history_item(length - 1)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--role", default="admin", help="admin, sub_admin, rider, driver, affiliate_driver")
    parser.add_argument("--user-id", type=int, help="users.id to scope rider/driver data to")
    parser.add_argument("--rows", type=int, default=10, help="result rows to print")
    parser.add_argument("--login", action="store_true", help="ask for a Passport token at start")
    args = parser.parse_args(argv)
    _setup_history()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

"""Round-2 regressions on the real Laravel MariaDB configured in .env (no Ollama needed).

The questions here are answered by fixed SQL, so a fake model that is never called is
enough. Expected values are from the test dump of 2026-10-26.
Skipped by default. Run with ``pytest -m integration tests/test_integration_money.py -v``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.session import create_engine
from app.llm.base import ChatResult, LLMMessage
from app.services.data.agent import DataAgent, DataResult
from app.services.data.catalog import SchemaCatalog
from app.services.data.policy import PolicyResolver
from app.services.data.runner import QueryRunner
from app.services.requester import Requester

pytestmark = pytest.mark.integration

DG_LIMO = 847  # Dg Limo Service LLC, Darwin Guerra; also his own driver 249


class NoModel:
    model = "none"

    async def chat(self, messages: Sequence[LLMMessage], **_: Any) -> ChatResult:
        raise AssertionError("these questions must not need the model")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings(ollama_warmup=False))  # type: ignore[call-arg]  # reads .env
    yield engine
    await engine.dispose()


@pytest.fixture
def agent(engine: AsyncEngine) -> DataAgent:
    return DataAgent(
        NoModel(),  # type: ignore[arg-type]
        SchemaCatalog(engine, ttl_seconds=600),
        PolicyResolver(engine),
        QueryRunner(engine, timeout_seconds=10, max_rows=50),
        max_rows=50,
    )


async def ask(agent: DataAgent, *turns: str, role: str = "admin", user_id: int = 1) -> DataResult:
    history = [LLMMessage("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(turns)]
    return await agent.answer(Requester(role, None, user_id=user_id), history)


async def test_1_rate_for_booking_2109_comes_from_its_own_order(agent: DataAgent) -> None:
    result = await ask(agent, "tell me the rate for 2109 booking?")
    assert result.message == (
        "Price breakdown for booking #2109:\n- Base Rate: USD 123.79\n- Airport Departure Tax: USD 4.00"
    )


async def test_2_taxes_of_booking_2109(agent: DataAgent) -> None:
    result = await ask(agent, "how much taxes are there in booking 2109?")
    assert result.message == (
        "Taxes for booking #2109:\n- Airport Departure Tax: USD 4.00\nTotal taxes: USD 4.00"
    )


async def test_3_grand_total_of_booking_2109(agent: DataAgent) -> None:
    result = await ask(agent, "what is the grand total for 2109 booking?")
    assert result.message is not None
    assert result.message.startswith("The grand total for booking #2109 is USD 127.79.")


async def test_5_admin_profit_for_dg_limo(agent: DataAgent) -> None:
    result = await ask(agent, "how much profit for dg limo bookings")
    assert result.message is not None
    assert "Affiliate #847 (Dg Limo Service LLC)" in result.message
    assert "USD — 6 bookings" in result.message
    for line in (
        "- Admin profit (admin share): 273.00",
        "- Grand total: 1,825.00",
        "- Affiliate share: 1,460.00",
        "- Travel agent share: 92.00",
        "- Stripe fee: 93.05",
    ):
        assert line in result.message


async def test_6_dg_limo_logged_in_sees_only_their_income(agent: DataAgent, engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        user_id = (
            await conn.execute(text("SELECT user_id FROM tbl_account WHERE id = :id"), {"id": DG_LIMO})
        ).scalar_one()
    result = await ask(agent, "what's my income?", role="driver", user_id=user_id)
    assert result.message is not None
    assert "- USD: 1,460.00 (6 bookings)" in result.message
    assert "Admin" not in result.message
    assert "Travel agent" not in result.message


async def test_13_darwin_menu(agent: DataAgent) -> None:
    result = await ask(agent, "booking numbers for Darwin?")
    assert result.message is not None
    menu = result.message.splitlines()
    assert "1. Darwin Guerra — Affiliate #847 (Dg Limo Service LLC) & driver — 6 bookings" in menu
    assert not any(line.startswith(("Driver", "1. Driver")) or "— Driver for" in line for line in menu)
    lose = [line for line in menu if "Lose affiliate" in line]
    assert lose == [next(line for line in lose if line.endswith("— 1 booking"))]  # 4451 (0) dropped
    assert all(line.split(". ", 1)[1].split(" — ")[0] for line in menu[1:-1])  # every option has a name

"""Alembic environment for limo-rt. Only ``tbl_rt_*`` tables are ever touched.

Two guards on top of review:

* ``include_object`` hides every non-``tbl_rt_`` table from autogenerate, so a generated
  revision can never drop or alter a Laravel table;
* tests/realtime/test_migrations.py fails if any revision names another table.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import URL, create_engine, pool

from app.realtime.tables import RT_TABLE_PREFIX, rt_metadata

config = context.config


def _env() -> dict[str, str]:
    """.env (plain KEY=VALUE lines), overridden by the process environment."""
    values: dict[str, str] = {}
    env_file = Path(".env")
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    values.update(os.environ)
    return values


def _url() -> URL:
    env = _env()
    user = env.get("RT_MIGRATE_DB_USERNAME")
    password = env.get("RT_MIGRATE_DB_PASSWORD")
    if not user or password is None:
        raise SystemExit("RT_MIGRATE_DB_USERNAME and RT_MIGRATE_DB_PASSWORD must be set to run migrations")
    return URL.create(
        "mysql+pymysql",
        username=user,
        password=password,
        host=env.get("DB_HOST", "127.0.0.1"),
        port=int(env.get("DB_PORT", "3306")),
        database=env["DB_DATABASE"],
        query={"charset": "utf8mb4"},
    )


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        return bool(name and name.startswith(RT_TABLE_PREFIX))
    table = getattr(obj, "table", None)
    return table is None or str(table.name).startswith(RT_TABLE_PREFIX)


def run_migrations_offline() -> None:
    context.configure(
        url=_url().render_as_string(hide_password=False),
        target_metadata=rt_metadata,
        literal_binds=True,
        include_object=include_object,
        version_table=config.get_main_option("version_table") or "alembic_version_rt",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=rt_metadata,
            include_object=include_object,
            version_table=config.get_main_option("version_table") or "alembic_version_rt",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

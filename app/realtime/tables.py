"""SQLAlchemy Core tables used by the realtime service.

Two metadata objects, on purpose:

* ``rt_metadata``: tables this service owns (``tbl_rt_*``). Alembic creates them.
* ``laravel_metadata``: mirrors of Laravel tables, with only the columns read or written
  here. Laravel owns them. Never pass this metadata to ``create_all`` or Alembic.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
)

RT_TABLE_PREFIX = "tbl_rt_"

rt_metadata = MetaData()
laravel_metadata = MetaData()

# ---------------------------------------------------------------------------------------
# Owned by limo-rt (Alembic). Foreign keys are declared in the migration, which reads the
# referenced column types from the live database; they are not repeated here.
# ---------------------------------------------------------------------------------------

location_points = Table(
    "tbl_rt_location_points",
    rt_metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("reservation_id", BigInteger, nullable=False),
    Column("driver_user_id", BigInteger, nullable=True),
    Column("recorded_at", DateTime(timezone=False), nullable=False),  # UTC, ms precision in MySQL
    Column("lat", Numeric(10, 7), nullable=False),
    Column("lng", Numeric(10, 7), nullable=False),
    Column("accuracy_m", Numeric(7, 2), nullable=True),
    Column("speed_mps", Numeric(6, 2), nullable=True),
    Column("heading", Numeric(5, 1), nullable=True),
    Column("source", String(16), nullable=True),  # nav_snapped | raw | status | legacy
    Column("legacy_mongo_id", String(24), nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
)

chat_messages = Table(
    "tbl_rt_chat_messages",
    rt_metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("reservation_id", BigInteger, nullable=False),
    Column("sender_user_id", BigInteger, nullable=False),
    Column("receiver_user_id", BigInteger, nullable=True),
    Column("sender_role", String(16), nullable=False),  # driver | rider | admin
    Column("body", Text, nullable=False),
    Column("client_msg_id", String(64), nullable=True),
    Column("read_at", DateTime(timezone=False), nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("legacy_mongo_id", String(24), nullable=True, unique=True),
    UniqueConstraint("sender_user_id", "client_msg_id", name="uq_rt_chat_client_msg"),
)

user_devices = Table(
    "tbl_rt_user_devices",
    rt_metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("app", String(16), nullable=False),  # driver | rider
    Column("platform", String(16), nullable=False),  # ios | android | web
    Column("fcm_token", String(255), nullable=False, unique=True),
    Column("app_version", String(32), nullable=True),
    Column("locale", String(16), nullable=True),
    Column("last_seen_at", DateTime(timezone=False), nullable=False),
    Column("revoked_at", DateTime(timezone=False), nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
)

notifications = Table(
    "tbl_rt_notifications",
    rt_metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("title", String(255), nullable=False),
    Column("body", Text, nullable=False),
    Column("type", String(50), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("data", JSON, nullable=True),
    Column("reservation_id", BigInteger, nullable=True),
    Column("read_at", DateTime(timezone=False), nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("legacy_mongo_id", String(24), nullable=True, unique=True),
)

# Kill switches and rollout flags, edited from admin. Laravel's mobile/v1/config reads them.
settings = Table(
    "tbl_rt_settings",
    rt_metadata,
    Column("name", String(64), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_by_user_id", BigInteger, nullable=True),
    Column("updated_at", DateTime(timezone=False), nullable=False),
)

# ---------------------------------------------------------------------------------------
# Laravel-owned mirrors (never created here). Writes are limited by column-level grants.
# ---------------------------------------------------------------------------------------

reservations = Table(
    "tbl_reservations",
    laravel_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("acc_id", BigInteger),
    Column("travel_client_id", BigInteger),
    Column("affiliate_id", Integer),
    Column("driver_id", Integer),
    Column("booking_status", String(32)),
    Column("status_change_flag", String(8)),
    Column("service_type", String(32)),
    Column("extra_stops", Text),
    Column("pickup_latitude", Numeric(11, 8)),
    Column("pickup_longitude", Numeric(11, 8)),
    Column("dropoff_latitude", Numeric(11, 8)),
    Column("dropoff_longitude", Numeric(11, 8)),
    Column("current_stop_index", SmallInteger),
    Column("current_stop_status", String(32)),
    Column("total_stops", SmallInteger),
    Column("stop_status_updated_at", DateTime(timezone=False)),
    Column("updated_at", DateTime(timezone=False)),
)

accounts = Table(
    "tbl_account",
    laravel_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger),
    Column("first_name", String(255)),
)

users = Table(
    "users",
    laravel_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("role", Integer),
)

status_history = Table(
    "tbl_booking_status_history",
    laravel_metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("reservation_id", BigInteger, nullable=False),
    Column("previous_status", String(32)),
    Column("booking_status", String(32), nullable=False),
    Column("stop_index", SmallInteger),
    Column("stop_number", SmallInteger),
    Column("total_stops", SmallInteger),
    Column("stop_address", String(512)),
    Column("stop_latitude", Numeric(10, 7)),
    Column("stop_longitude", Numeric(10, 7)),
    Column("driver_id", BigInteger),
    Column("customer_id", BigInteger),
    Column("latitude", Numeric(10, 7)),
    Column("longitude", Numeric(10, 7)),
    Column("event_at", DateTime(timezone=False)),
    Column("source", String(32)),
    Column("correlation_id", String(128)),
    Column("idempotency_key", String(128)),
    Column("metadata", JSON),
    Column("created_at", DateTime(timezone=False)),
    Column("updated_at", DateTime(timezone=False)),
)

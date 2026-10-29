"""SQLAlchemy engine, session factory, and shared column helpers.

Models are written to run on both SQLite (local dev) and Postgres (production):
JSON becomes JSONB on Postgres, enums are stored as strings, IDs are strings.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum
from functools import lru_cache
from pathlib import Path

from sqlalchemy import JSON, DateTime, Engine, Enum, Numeric, TypeDecorator, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

JSONType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(10, 2)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend.

    SQLite drops tzinfo, so values read back would be naive and fail to compare
    with `utcnow()`. This normalises to UTC on write and re-attaches UTC on read.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime passed to UTCDateTime; use timezone-aware values")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


def str_enum(enum_cls: type[PyEnum]) -> Enum:
    """Store a Python enum by its value as a VARCHAR (portable, readable in SQL)."""
    return Enum(
        enum_cls,
        native_enum=False,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
        length=32,
    )


def make_engine(url: str, echo: bool) -> Engine:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, echo=echo)

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _enable_sqlite_fks(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


@lru_cache
def get_engine() -> Engine:
    s = get_settings()
    return make_engine(s.database_url, s.db_echo)


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)

"""SQLite state: tracked flights, status snapshots, change log, API usage."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    inspect,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Flight(Base):
    """A flight the user asked us to watch."""

    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Normalised flight number, e.g. "KL1234"
    flight_number: Mapped[str] = mapped_column(String(16), index=True)
    # Departure date (local to the origin airport), ISO yyyy-mm-dd
    flight_date: Mapped[str] = mapped_column(String(10), index=True)
    label: Mapped[str] = mapped_column(String(200), default="")

    # Lifecycle: active | completed | abandoned
    tracking_state: Mapped[str] = mapped_column(String(16), default="active", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    next_poll_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=utcnow, index=True
    )
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    last_error: Mapped[str] = mapped_column(Text, default="")
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)

    # --- Latest known status (the diff baseline) ---
    status: Mapped[str] = mapped_column(String(32), default="")
    callsign: Mapped[str] = mapped_column(String(16), default="")
    airline: Mapped[str] = mapped_column(String(80), default="")
    airline_icao: Mapped[str] = mapped_column(String(8), default="")
    aircraft_reg: Mapped[str] = mapped_column(String(16), default="")
    aircraft_model: Mapped[str] = mapped_column(String(80), default="")

    dep_iata: Mapped[str] = mapped_column(String(8), default="")
    dep_name: Mapped[str] = mapped_column(String(120), default="")
    dep_terminal: Mapped[str] = mapped_column(String(16), default="")
    dep_gate: Mapped[str] = mapped_column(String(16), default="")
    dep_scheduled_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    dep_scheduled_local: Mapped[str] = mapped_column(String(40), default="")
    dep_actual_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    dep_actual_local: Mapped[str] = mapped_column(String(40), default="")
    dep_lat: Mapped[Optional[float]] = mapped_column(Float, default=None)
    dep_lon: Mapped[Optional[float]] = mapped_column(Float, default=None)

    arr_iata: Mapped[str] = mapped_column(String(8), default="")
    arr_name: Mapped[str] = mapped_column(String(120), default="")
    arr_terminal: Mapped[str] = mapped_column(String(16), default="")
    arr_gate: Mapped[str] = mapped_column(String(16), default="")
    arr_baggage_belt: Mapped[str] = mapped_column(String(16), default="")
    arr_scheduled_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    arr_scheduled_local: Mapped[str] = mapped_column(String(40), default="")
    arr_actual_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    arr_actual_local: Mapped[str] = mapped_column(String(40), default="")
    arr_lat: Mapped[Optional[float]] = mapped_column(Float, default=None)
    arr_lon: Mapped[Optional[float]] = mapped_column(Float, default=None)

    # --- Phase 2: live position tracking ---
    position_source: Mapped[str] = mapped_column(String(16), default="")
    last_position_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    next_position_poll_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=None, index=True
    )
    position_error: Mapped[str] = mapped_column(Text, default="")

    raw_json: Mapped[str] = mapped_column(Text, default="")

    positions: Mapped[list["FlightPosition"]] = relationship(
        back_populates="flight",
        cascade="all, delete-orphan",
        order_by="FlightPosition.recorded_at",
    )

    events: Mapped[list["FlightEvent"]] = relationship(
        back_populates="flight",
        cascade="all, delete-orphan",
        order_by="FlightEvent.created_at.desc()",
    )

    @property
    def route(self) -> str:
        if self.dep_iata and self.arr_iata:
            return f"{self.dep_iata} → {self.arr_iata}"
        return "—"


class FlightEvent(Base):
    """One logged change (or note) for a flight — the alert history."""

    __tablename__ = "flight_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # registered | change | error | info
    kind: Mapped[str] = mapped_column(String(16), default="change")
    summary: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    flight: Mapped[Flight] = relationship(back_populates="events")


class FlightPosition(Base):
    """One ADS-B fix — the flight trail is these rows in time order."""

    __tablename__ = "flight_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    altitude_ft: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    ground_speed_kt: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    track_deg: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    on_ground: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="adsb.lol")

    flight: Mapped[Flight] = relationship(back_populates="positions")


class ApiCall(Base):
    """Every upstream API call, so we can enforce the free-tier budget."""

    __tablename__ = "api_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="aerodatabox", index=True)
    endpoint: Mapped[str] = mapped_column(String(200), default="")
    units: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(300), default="")


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix) :]
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# Columns added after v0.1.0. SQLAlchemy's create_all() only creates missing
# *tables*, so an existing flights.db needs these bolted on explicitly.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "flights": [
        ("airline_icao", "VARCHAR(8) DEFAULT ''"),
        ("dep_lat", "FLOAT"),
        ("dep_lon", "FLOAT"),
        ("arr_lat", "FLOAT"),
        ("arr_lon", "FLOAT"),
        ("position_source", "VARCHAR(16) DEFAULT ''"),
        ("last_position_at", "DATETIME"),
        ("next_position_poll_at", "DATETIME"),
        ("position_error", "TEXT DEFAULT ''"),
    ],
}


def _migrate() -> list[str]:
    """Idempotently add columns introduced after the first release.

    Safe to run on every startup: existing rows keep their data, and the new
    columns arrive empty (which every code path already treats as 'unknown').
    """
    applied: list[str] = []
    if not settings.database_url.startswith("sqlite"):
        return applied

    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in inspector.get_table_names():
                continue  # create_all() will build it complete
            existing = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in existing:
                    continue
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                )
                applied.append(f"{table}.{name}")
    return applied


def init_db() -> list[str]:
    """Create anything missing, then migrate. Returns the columns it added."""
    migrated = _migrate()
    Base.metadata.create_all(engine)
    return migrated


def units_used_this_month(session, provider: str = "aerodatabox") -> int:
    """Units consumed since the 1st of the current UTC month."""
    now = utcnow()
    month_start = datetime(now.year, now.month, 1)
    total = (
        session.query(func.coalesce(func.sum(ApiCall.units), 0))
        .filter(
            ApiCall.provider == provider,
            ApiCall.created_at >= month_start,
        )
        .scalar()
    )
    return int(total or 0)

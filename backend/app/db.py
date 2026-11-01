"""Persistence: farmer profiles and saved advisories (SQLite by default, any SQLAlchemy URL via DATABASE_URL)."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Optional

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ROOT = Path(__file__).resolve().parents[1]


def database_url() -> str:
    """DATABASE_URL (e.g. Supabase Postgres), else SQLite. Serverless hosts (Vercel) only allow writes in /tmp."""
    url = os.getenv("DATABASE_URL", "").strip()
    password = os.getenv("DATABASE_PASSWORD", "")
    if url and password:
        # Keep the secret out of the URL setting: DATABASE_URL may hold Supabase's "[YOUR-PASSWORD]" placeholder.
        url = url.replace("[YOUR-PASSWORD]", quote(password, safe=""))
    if not url:
        folder = Path("/tmp") if os.getenv("VERCEL") else ROOT
        return f"sqlite:///{folder / 'krishimitra.db'}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]  # psycopg 3 driver
    return url


DB_URL = database_url()
if DB_URL.startswith("sqlite"):
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
else:
    # Serverless-friendly: no pool kept between invocations; prepare_threshold=None works with
    # Supabase's transaction pooler (port 6543), which does not support prepared statements.
    engine = create_engine(DB_URL, poolclass=NullPool, pool_pre_ping=True, connect_args={"prepare_threshold": None})
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Farmer(Base):
    __tablename__ = "farmers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(5), default="hi")
    state: Mapped[str] = mapped_column(String(80), default="Bihar")
    district: Mapped[str] = mapped_column(String(80), default="Patna")
    village: Mapped[str] = mapped_column(String(100), default="")
    crop: Mapped[str] = mapped_column(String(30), default="wheat")
    sowing_date: Mapped[date] = mapped_column(Date)
    irrigation: Mapped[str] = mapped_column(String(40), default="available")
    farm_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class MandiCache(Base):
    """Last successful data.gov.in response per state and crop ("Bihar:wheat"), shared by every serverless instance."""
    __tablename__ = "mandi_cache"
    state: Mapped[str] = mapped_column(String(80), primary_key=True)
    records_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Advisory(Base):
    __tablename__ = "advisories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farmer_id: Mapped[int] = mapped_column(Integer, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db() -> str | None:
    """Create tables if missing. Returns an error message instead of crashing when the database is unreachable."""
    try:
        Base.metadata.create_all(engine)
        return None
    except Exception as exc:  # e.g. wrong password or network: the API still starts and reports it
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"


DB_INIT_ERROR = init_db()


def crop_stage(sowing: date, today: date | None = None) -> str:
    days = max(0, ((today or date.today()) - sowing).days)
    if days < 21:
        return "establishment"
    if days < 60:
        return "vegetative"
    if days < 100:
        return "reproductive"
    return "maturity"


def days_after_sowing(sowing: date, today: date | None = None) -> int:
    return max(0, ((today or date.today()) - sowing).days)


def farmer_json(f: Farmer) -> dict:
    return {
        "id": f.id, "name": f.name, "language": f.language, "state": f.state, "district": f.district,
        "village": f.village, "crop": f.crop, "sowing_date": f.sowing_date.isoformat(), "irrigation": f.irrigation,
        "farm_size": f.farm_size, "stage": crop_stage(f.sowing_date), "days_after_sowing": days_after_sowing(f.sowing_date),
    }

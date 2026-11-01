"""Live mandi (market) prices from the Government of India Open Government Data platform (data.gov.in).

Dataset: "Current Daily Price of Various Commodities from Various Markets (Mandi)", AGMARKNET.
Requires a free API key from https://data.gov.in (set DATA_GOV_IN_API_KEY).

data.gov.in is often slow (10-30 s), so:
- every successful response is saved in the database (table mandi_cache) and reused for CACHE_SECONDS,
  shared by all serverless instances;
- the dashboard never waits on data.gov.in (allow_fetch=False); the site loads prices separately;
- when data.gov.in fails, the last real response (up to STALE_SECONDS old) is shown and labelled with its
  fetch time. Prices are never invented: with nothing real to show, the status says so.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

from .db import MandiCache, Session
from .weather import get_json

RESOURCE_ID = os.getenv("MANDI_RESOURCE_ID", "9ef84268-d588-465a-a308-a864a43d0070")
URL = f"https://api.data.gov.in/resource/{RESOURCE_ID}"
SOURCE = "AGMARKNET via data.gov.in (Current Daily Price of Various Commodities from Various Markets)"
CACHE_SECONDS = 3 * 60 * 60
STALE_SECONDS = 72 * 60 * 60  # last real prices, shown with their date
FAILURE_BACKOFF_SECONDS = 5 * 60
DEFAULT_BUDGET_SECONDS = float(os.getenv("MANDI_TIMEOUT_SECONDS", "55"))  # Vercel functions may run 60 s
COMMODITY_PATTERNS = {"wheat": r"^wheat", "paddy": r"paddy|dhan"}

_memory: dict[str, tuple[float, list[dict]]] = {}
_failed_at: dict[str, tuple[float, str]] = {}


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _load_cached(cache_key: str) -> tuple[float, list[dict]] | None:
    if cache_key in _memory:
        return _memory[cache_key]
    try:
        with Session() as db:
            row = db.get(MandiCache, cache_key)
            if row:
                fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=timezone.utc)
                _memory[cache_key] = (fetched.timestamp(), json.loads(row.records_json))
                return _memory[cache_key]
    except Exception:
        return None  # database trouble must not break prices
    return None


def _save_cached(cache_key: str, records: list[dict]) -> None:
    now = time.time()
    _memory[cache_key] = (now, records)
    try:
        with Session() as db:
            db.merge(MandiCache(state=cache_key, records_json=json.dumps(records, ensure_ascii=False),
                                fetched_at=datetime.fromtimestamp(now, timezone.utc)))
            db.commit()
    except Exception:
        pass


# AGMARKNET commodity names, so data.gov.in filters on its side and returns a few rows instead of hundreds.
COMMODITY_NAMES = {"wheat": ["Wheat"], "paddy": ["Paddy(Dhan)(Common)", "Paddy(Dhan)(Basmati)"]}


async def _fetch_records(state: str, crop: str, key: str, timeout: float) -> list[dict]:
    base = {"api-key": key, "format": "json", "limit": 100, "filters[state.keyword]": state}
    names = COMMODITY_NAMES.get(crop)
    if names:
        results = await asyncio.gather(*(get_json(URL, {**base, "filters[commodity]": n}, timeout=timeout, retries=False) for n in names),
                                       return_exceptions=True)
        records = [r for res in results if isinstance(res, dict) for r in (res.get("records") or [])]
        if records or all(isinstance(res, dict) for res in results):
            return records
    # unknown crop name, or the filtered request failed: whole state (slower)
    data = await get_json(URL, {**base, "limit": 500}, timeout=timeout, retries=False)
    return data.get("records") or []


def _build(records: list[dict], crop: str, district: str, state: str, base: dict, fetched_ts: float, stale_reason: str | None) -> dict:
    pattern = re.compile(COMMODITY_PATTERNS.get(crop, re.escape(crop)), re.I)
    rows = [
        {"market": r.get("market"), "district": r.get("district"), "commodity": r.get("commodity"),
         "variety": r.get("variety"), "arrival_date": r.get("arrival_date"),
         "min_price": _num(r.get("min_price")), "max_price": _num(r.get("max_price")), "modal_price": _num(r.get("modal_price")),
         "unit": "INR/quintal"}
        for r in records if pattern.search(r.get("commodity") or "")
    ]
    rows = [r for r in rows if r["modal_price"] is not None and r["min_price"] is not None and r["max_price"] is not None]
    info = {**base, "fetched_at": datetime.fromtimestamp(fetched_ts, timezone.utc).isoformat(), "stale": bool(stale_reason)}
    if stale_reason:
        info["stale_reason"] = stale_reason
    if not rows:
        return {**info, "status": "no_records", "records": [],
                "message": f"No {state} market reported {crop} prices in the latest AGMARKNET data."}
    local = [r for r in rows if (r["district"] or "").lower() == district.lower()]
    rows = sorted(local, key=lambda r: r["market"] or "") + sorted([r for r in rows if r not in local], key=lambda r: r["market"] or "")
    return {**info, "status": "ok", "records": rows[:8], "district_match": bool(local)}


async def mandi_prices(crop: str, district: str, state: str = "Bihar", allow_fetch: bool = True, budget: float | None = None,
                       force: bool = False) -> dict:
    key = os.getenv("DATA_GOV_IN_API_KEY", "").strip()
    base = {"crop": crop, "district": district, "state": state, "source": SOURCE}
    if not key:
        return {**base, "status": "not_configured", "records": [], "message": "not configured (data.gov.in API key missing)"}

    cache_key = f"{state}:{crop}"
    cached = _load_cached(cache_key)
    age = time.time() - cached[0] if cached else None
    if cached and age < CACHE_SECONDS and not force:
        return _build(cached[1], crop, district, state, base, cached[0], None)

    failed = _failed_at.get(cache_key)
    recently_failed = failed and time.time() - failed[0] < FAILURE_BACKOFF_SECONDS and not force
    if not allow_fetch or recently_failed:
        if cached and age < STALE_SECONDS:
            return _build(cached[1], crop, district, state, base, cached[0], "refreshing" if not recently_failed else failed[1])
        if recently_failed:
            return {**base, "status": "unavailable", "records": [], "message": failed[1]}
        return {**base, "status": "pending", "records": [], "message": "loading today's prices from data.gov.in"}

    budget = budget or DEFAULT_BUDGET_SECONDS
    try:
        records = await asyncio.wait_for(_fetch_records(state, crop, key, budget), timeout=budget)
    except Exception as exc:
        reason = "too slow to respond" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else f"unreachable ({type(exc).__name__})"
        message = f"data.gov.in {reason}; will retry in a few minutes"
        _failed_at[cache_key] = (time.time(), message)
        if cached and age < STALE_SECONDS:
            return _build(cached[1], crop, district, state, base, cached[0], message)
        return {**base, "status": "unavailable", "records": [], "message": message}
    _failed_at.pop(cache_key, None)
    _save_cached(cache_key, records)
    return _build(records, crop, district, state, base, time.time(), None)


async def refresh_all(state: str = "Bihar", budget: float | None = None) -> dict:
    """Daily job: refresh saved prices for every supported crop (called by Vercel Cron)."""
    results = await asyncio.gather(*(mandi_prices(c, "", state, budget=budget, force=True) for c in COMMODITY_NAMES))
    return {c: {"status": r["status"], "rows": len(r["records"]), "message": r.get("message")} for c, r in zip(COMMODITY_NAMES, results)}

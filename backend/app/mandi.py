"""Live mandi (market) prices from the Government of India Open Government Data platform (data.gov.in).

Dataset: "Current Daily Price of Various Commodities from Various Markets (Mandi)", AGMARKNET.
Requires a free API key from https://data.gov.in (set DATA_GOV_IN_API_KEY). Without a key, or when no market
reported today, the response says so explicitly; prices are never invented.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

from .weather import get_json

RESOURCE_ID = os.getenv("MANDI_RESOURCE_ID", "9ef84268-d588-465a-a308-a864a43d0070")
URL = f"https://api.data.gov.in/resource/{RESOURCE_ID}"
SOURCE = "AGMARKNET via data.gov.in (Current Daily Price of Various Commodities from Various Markets)"
CACHE_SECONDS = 30 * 60
COMMODITY_PATTERNS = {"wheat": r"^wheat", "paddy": r"paddy|dhan"}
_cache: dict[str, tuple[float, list[dict]]] = {}


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


async def _state_records(state: str, key: str) -> list[dict]:
    if state in _cache and time.time() - _cache[state][0] < CACHE_SECONDS:
        return _cache[state][1]
    base = {"api-key": key, "format": "json", "limit": 1000}
    last_exc: Exception | None = None
    for filter_name in ("filters[state.keyword]", "filters[state]"):
        try:
            data = await get_json(URL, {**base, filter_name: state}, timeout=12)
        except Exception as exc:
            last_exc = exc
            continue
        records = data.get("records") or []
        if records:
            _cache[state] = (time.time(), records)
            return records
    if last_exc:
        raise last_exc
    _cache[state] = (time.time(), [])
    return []


async def mandi_prices(crop: str, district: str, state: str = "Bihar") -> dict:
    key = os.getenv("DATA_GOV_IN_API_KEY", "").strip()
    base = {"crop": crop, "district": district, "state": state, "source": SOURCE,
            "fetched_at": datetime.now(timezone.utc).isoformat()}
    if not key:
        return {**base, "status": "not_configured", "records": [],
                "message": "not configured (data.gov.in API key missing)"}
    try:
        records = await _state_records(state, key)
    except Exception as exc:
        return {**base, "status": "unavailable", "records": [], "message": f"data.gov.in unreachable ({type(exc).__name__})"}
    pattern = re.compile(COMMODITY_PATTERNS.get(crop, re.escape(crop)), re.I)
    rows = [
        {"market": r.get("market"), "district": r.get("district"), "commodity": r.get("commodity"),
         "variety": r.get("variety"), "arrival_date": r.get("arrival_date"),
         "min_price": _num(r.get("min_price")), "max_price": _num(r.get("max_price")), "modal_price": _num(r.get("modal_price")),
         "unit": "INR/quintal"}
        for r in records if pattern.search(r.get("commodity") or "")
    ]
    rows = [r for r in rows if r["modal_price"] is not None and r["min_price"] is not None and r["max_price"] is not None]
    if not rows:
        return {**base, "status": "no_records", "records": [],
                "message": f"No {state} market reported {crop} prices in today's AGMARKNET data."}
    local = [r for r in rows if (r["district"] or "").lower() == district.lower()]
    rows = sorted(local, key=lambda r: r["market"] or "") + sorted([r for r in rows if r not in local], key=lambda r: r["market"] or "")
    return {**base, "status": "ok", "records": rows[:8], "district_match": bool(local)}

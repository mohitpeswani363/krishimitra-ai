"""Backend tests. External APIs are stubbed with response shapes copied from Open-Meteo and data.gov.in,
so these check our logic; the live integration is exercised by running the app."""
import asyncio
import re
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.mandi as mandi_mod
import app.weather as w
from app.advisor import classify_intent, compose_answer, critical_stage, evaluate_risks
from app.agent import invoke_agent
from app.knowledge import retrieve_knowledge, validate_citations

client = TestClient(m.app)


def forecast_payload(rain_hourly=0.0, prob=5, soil=0.30, t_max=28, t_min=15, wind=8):
    times = [f"2026-11-01T{h:02d}:00" for h in range(24)] + [f"2026-11-02T{h:02d}:00" for h in range(24)]
    n = len(times)
    return {
        "current": {"time": "2026-11-01T10:00", "temperature_2m": 24.0, "relative_humidity_2m": 60, "precipitation": 0, "wind_speed_10m": wind},
        "hourly": {"time": times, "precipitation": [rain_hourly] * n, "precipitation_probability": [prob] * n,
                   "soil_moisture_0_to_1cm": [soil] * n, "soil_moisture_1_to_3cm": [soil] * n, "soil_moisture_3_to_9cm": [soil] * n,
                   "soil_moisture_9_to_27cm": [soil] * n, "soil_temperature_6cm": [22.0] * n},
        "daily": {"time": ["2026-11-01", "2026-11-02", "2026-11-03"], "temperature_2m_max": [t_max] * 3,
                  "temperature_2m_min": [t_min] * 3, "precipitation_sum": [rain_hourly * 24] * 3, "precipitation_probability_max": [prob] * 3},
    }


@pytest.fixture
def live_apis(monkeypatch):
    """Stub the HTTP layer: geocoding knows Patna/Gaya, forecast returns the configured payload."""
    state = {"forecast": forecast_payload(), "fail": False}

    async def fake_get_json(url, params, timeout=8, retries=True):
        if state["fail"]:
            raise httpx.ConnectError("network down")
        if "geocoding" in url:
            name = params["name"].lower()
            if name in ("patna", "gaya"):
                return {"results": [{"name": name.title(), "latitude": 25.6, "longitude": 85.1, "country_code": "IN", "admin1": "Bihar"}]}
            return {"results": []}
        return state["forecast"]

    monkeypatch.setattr(w, "get_json", fake_get_json)
    monkeypatch.setattr(mandi_mod, "get_json", fake_get_json)
    w._geo_cache.clear()
    w._forecast_cache.clear()
    return state


def make_farmer(language="hi", district="Patna", das=24, crop="wheat", irrigation="available"):
    r = client.post("/api/farmers", json={"name": "Sita Devi", "language": language, "district": district, "crop": crop,
                                          "sowing_date": (date.today() - timedelta(days=das)).isoformat(), "irrigation": irrigation})
    assert r.status_code == 200, r.text
    return r.json()


def test_profile_and_hindi_live_advisory_is_persisted_and_cited(live_apis):
    f = make_farmer()
    r = client.post(f"/api/advisories/{f['id']}", json={"question": "मेरे गेहूं में कल सिंचाई करूं?"})
    assert r.status_code == 200 and "charset=utf-8" in r.headers["content-type"].lower()
    d = r.json()
    assert d["weather"]["mode"] == "live" and d["intent"] == "irrigation"
    assert any("ऀ" <= c <= "ॿ" for c in d["answer"]) and "à¤" not in d["answer"]
    assert "डेमो" not in d["answer"] and "simulated" not in d["weather"]["label"].lower()
    assert d["citations_verified"] and all(e["page"] > 0 for e in d["evidence"])
    assert [s["node"] for s in d["steps"]][:3] == ["intent_classification", "farmer_context", "weather_tool"]
    assert all(s["status"] == "ok" for s in d["steps"])
    history = client.get(f"/api/dashboard/{f['id']}").json()["advisories"]
    assert history[0]["question"] == "मेरे गेहूं में कल सिंचाई करूं?"


def test_answer_numbers_come_from_live_forecast(live_apis):
    live_apis["forecast"] = forecast_payload(rain_hourly=0.5, prob=80, soil=0.25)
    f = make_farmer(language="en")
    d = client.post(f"/api/advisories/{f['id']}", json={"question": "Should I irrigate tomorrow?"}).json()
    assert "12.0 mm" in d["answer"] and "80%" in d["answer"] and "hold irrigation" in d["answer"]


def test_weather_outage_degrades_gracefully_without_fake_data(live_apis):
    f = make_farmer()
    live_apis["fail"] = True
    w._forecast_cache.clear()
    dash = client.get(f"/api/dashboard/{f['id']}").json()
    assert dash["weather"] is None and "unreachable" in dash["weather_error"]
    d = client.post(f"/api/advisories/{f['id']}", json={"question": "कल सिंचाई करूं?"}).json()
    assert d["weather"] is None and any(e["node"] == "weather_tool" for e in d["errors"])
    assert "उपलब्ध नहीं" in d["answer"] and "°C" not in d["answer"]


def test_stale_real_forecast_is_served_and_labelled(live_apis):
    f = make_farmer()
    assert client.get(f"/api/dashboard/{f['id']}").json()["weather"]["cache"] == "miss"
    for k in list(w._forecast_cache):
        ts, data = w._forecast_cache[k]
        w._forecast_cache[k] = (ts - w.FRESH_SECONDS - 60, data)
    live_apis["fail"] = True
    weather = client.get(f"/api/dashboard/{f['id']}").json()["weather"]
    assert weather["cache"] == "stale" and weather["stale_minutes"] >= 15


def test_retry_recovers_from_transient_429(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] == 1 else 200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(w.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(w, "RETRY_DELAYS", (0, 0))
    assert asyncio.run(w.get_json("https://example.org", {})) == {"ok": True} and calls["n"] == 2


def test_input_validation(live_apis):
    base = {"name": "A", "district": "Patna", "crop": "wheat", "sowing_date": date.today().isoformat()}
    assert client.post("/api/farmers", json={**base, "district": "Xyzqwe"}).status_code == 422
    assert client.post("/api/farmers", json={**base, "sowing_date": (date.today() + timedelta(days=3)).isoformat()}).status_code == 422
    assert client.post("/api/farmers", json={**base, "crop": "mango"}).status_code == 422
    assert client.post("/api/farmers", json={**base, "name": ""}).status_code == 422
    assert client.get("/api/dashboard/99999").status_code == 404
    f = make_farmer()
    assert client.post(f"/api/advisories/{f['id']}", json={"question": "a"}).status_code == 422


def test_risk_rules():
    def weather(**kw):
        return w.parse_openmeteo(forecast_payload(**kw))
    assert [r["type"] for r in evaluate_risks(weather(rain_hourly=0.5, prob=80), "wheat")] == ["rain-and-field-work"]
    assert "heavy-rain" in [r["type"] for r in evaluate_risks(weather(rain_hourly=2, prob=90), "wheat")]
    assert "heat-stress" in [r["type"] for r in evaluate_risks(weather(t_max=36), "wheat", "reproductive")]
    assert "heat-stress" not in [r["type"] for r in evaluate_risks(weather(t_max=36), "wheat", "vegetative")]
    assert "cold-frost" in [r["type"] for r in evaluate_risks(weather(t_min=3), "wheat")]
    assert "dry-soil" in [r["type"] for r in evaluate_risks(weather(soil=0.12), "wheat")]
    assert "waterlogging" in [r["type"] for r in evaluate_risks(weather(soil=0.45), "wheat")]
    assert "wind-lodging" in [r["type"] for r in evaluate_risks(weather(wind=45), "wheat")]
    assert evaluate_risks(None, "wheat") == []


def test_parser_uses_next_24_hours_from_now():
    p = forecast_payload()
    p["hourly"]["precipitation"] = [0.0] * 10 + [1.0] * 38  # rain only from 10:00, the current hour
    parsed = w.parse_openmeteo(p)
    assert parsed["rainfall_mm_next_24h"] == 24.0 and parsed["soil"]["moisture_topsoil_m3m3"] == 0.3
    assert len(parsed["daily"]) == 3


def test_critical_stage_and_intent():
    assert critical_stage(22)[0] == "now" and critical_stage(15)[0] == "in 5 days" and critical_stage(50) is None
    assert classify_intent("गेहूं का मंडी भाव क्या है?") == "market_price"
    assert classify_intent("Should I irrigate?") == "irrigation"
    assert classify_intent("पाला पड़ेगा क्या?") == "weather_risk"


def test_mandi_without_key_is_explicit_and_has_no_numbers(live_apis):
    f = make_farmer()
    mandi = client.get(f"/api/dashboard/{f['id']}").json()["mandi"]
    assert mandi["status"] == "not_configured" and mandi["records"] == []


def test_mandi_live_records_are_filtered_by_crop_and_district(live_apis, monkeypatch):
    monkeypatch.setenv("DATA_GOV_IN_API_KEY", "test-key")
    mandi_mod._memory.clear()
    mandi_mod._failed_at.clear()

    async def fake(url, params, timeout=8, retries=True):
        assert params["api-key"] == "test-key" and params["filters[commodity]"] == "Wheat"
        return {"records": [
            {"state": "Bihar", "district": "Gaya", "market": "Gaya", "commodity": "Wheat", "variety": "Dara", "arrival_date": "01/11/2026", "min_price": "2400", "max_price": "2550", "modal_price": "2480"},
            {"state": "Bihar", "district": "Patna", "market": "Masaurhi", "commodity": "Wheat", "variety": "Other", "arrival_date": "01/11/2026", "min_price": "2420", "max_price": "2600", "modal_price": "2500"},
            {"state": "Bihar", "district": "Patna", "market": "Patna", "commodity": "Onion", "variety": "Red", "arrival_date": "01/11/2026", "min_price": "1800", "max_price": "2200", "modal_price": "2000"},
        ]}
    monkeypatch.setattr(mandi_mod, "get_json", fake)
    result = asyncio.run(mandi_mod.mandi_prices("wheat", "Patna"))
    assert result["status"] == "ok" and result["district_match"]
    assert [r["market"] for r in result["records"]] == ["Masaurhi", "Gaya"]
    farmer = {"crop": "wheat", "language": "en", "days_after_sowing": 50, "district": "Patna"}
    text = compose_answer("wheat price today?", farmer, None, [], result, [], "market_price")
    assert "Masaurhi" in text and "₹2500" in text


def test_llm_guardrail_rejects_uncited_dosage(live_apis, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    async def fake_llm(state):
        return "Spray 2 ml/l chlorpyrifos and irrigate."
    monkeypatch.setattr(m, "openai_synthesis", fake_llm)
    f = make_farmer(language="en")
    d = client.post(f"/api/advisories/{f['id']}", json={"question": "Should I irrigate?"}).json()
    assert d["provider"] == "rule-based" and "rejected" in d["safety"] and "chlorpyrifos" not in d["answer"]


def test_llm_answer_used_when_safe(live_apis, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    async def fake_llm(state):
        return "Soil is moist; check again tomorrow [ICAR p.12]."
    monkeypatch.setattr(m, "openai_synthesis", fake_llm)
    f = make_farmer(language="en")
    d = client.post(f"/api/advisories/{f['id']}", json={"question": "Should I irrigate?"}).json()
    assert d["provider"].startswith("llm:") and d["answer"].startswith("Soil is moist")


def test_llm_failure_falls_back_to_rule_answer(live_apis, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    async def broken(state):
        raise httpx.ConnectError("llm down")
    monkeypatch.setattr(m, "openai_synthesis", broken)
    f = make_farmer(language="en")
    d = client.post(f"/api/advisories/{f['id']}", json={"question": "Should I irrigate?"}).json()
    assert d["provider"] == "rule-based" and any(e["node"] == "llm_synthesis" for e in d["errors"])


def test_retrieval_returns_real_page_cited_wheat_evidence():
    evidence = retrieve_knowledge("wheat irrigation", "wheat")
    assert evidence and all(x["publisher"] == "Indian Council of Agricultural Research" and x["page"] > 0 for x in evidence)
    assert validate_citations(evidence)


def test_citation_tampering_rejected():
    evidence = retrieve_knowledge("irrigation wheat", "wheat")
    assert not validate_citations([])
    assert not validate_citations([dict(evidence[0], text="fabricated excerpt")])
    assert not validate_citations([dict(evidence[0], page=evidence[0]["page"] + 1)])


def test_retrieval_filters_explicitly_unrelated_state():
    evidence = retrieve_knowledge("irrigation wheat Gujarat", "wheat", "Bihar")
    assert evidence and all(not re.search(r"\bGujarat\b", x["text"], re.I) for x in evidence)
    focused = retrieve_knowledge("मेरे गेहूं में सिंचाई", "wheat", "Bihar")
    assert "Bihar" in focused[0]["text"]
    assert all(not re.search(r"fertili[sz]er|NPK|herbicide|pesticide", x["text"], re.I) for x in focused)


def test_cors_preflight():
    r = client.options("/api/farmers", headers={"Origin": "http://localhost:3002", "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:3002"


def test_langgraph_runs_nodes_in_order_and_isolates_failures():
    seen = []

    def node(name, fail=False):
        async def run(state):
            seen.append(name)
            if fail:
                raise ValueError("boom")
            return {}
        return run
    names = ["a", "b", "c"]
    result = asyncio.run(invoke_agent({"question": "hello"}, {"a": node("a"), "b": node("b", fail=True), "c": node("c")}))
    assert seen == names and result["trace"] == names
    assert [s["status"] for s in result["steps"]] == ["ok", "error", "ok"] and result["errors"][0]["node"] == "b"


def test_slow_mandi_api_is_cut_off_and_not_retried_every_request(monkeypatch):
    monkeypatch.setenv("DATA_GOV_IN_API_KEY", "test-key")
    monkeypatch.setattr(mandi_mod, "DEFAULT_BUDGET_SECONDS", 0.2)
    mandi_mod._memory.clear()
    mandi_mod._failed_at.clear()
    from app.db import MandiCache, Session
    with Session() as db:
        db.query(MandiCache).delete()
        db.commit()
    calls = {"n": 0}

    async def slow(url, params, timeout=8, retries=True):
        calls["n"] += 1
        await asyncio.sleep(5)
    monkeypatch.setattr(mandi_mod, "get_json", slow)
    first = asyncio.run(mandi_mod.mandi_prices("wheat", "Patna"))
    second = asyncio.run(mandi_mod.mandi_prices("wheat", "Patna"))
    assert first["status"] == second["status"] == "unavailable" and "too slow" in first["message"]
    assert calls["n"] == 1 and first["records"] == []


def test_mandi_cache_is_shared_via_database_and_serves_stale_real_data(monkeypatch):
    import time as _time
    from app.db import MandiCache, Session
    monkeypatch.setenv("DATA_GOV_IN_API_KEY", "test-key")
    mandi_mod._memory.clear()
    mandi_mod._failed_at.clear()
    with Session() as db:
        db.query(MandiCache).delete()
        db.commit()
    record = {"state": "Bihar", "district": "Patna", "market": "Patna", "commodity": "Wheat", "variety": "Dara",
              "arrival_date": "01/11/2026", "min_price": "2400", "max_price": "2600", "modal_price": "2500"}

    async def ok(url, params, timeout=8, retries=True):
        return {"records": [record]}
    monkeypatch.setattr(mandi_mod, "get_json", ok)
    # the dashboard path never fetches: nothing cached yet -> pending
    assert asyncio.run(mandi_mod.mandi_prices("wheat", "Patna", allow_fetch=False))["status"] == "pending"
    assert asyncio.run(mandi_mod.mandi_prices("wheat", "Patna"))["status"] == "ok"
    mandi_mod._memory.clear()  # a different serverless instance: reads the database copy
    cached = asyncio.run(mandi_mod.mandi_prices("wheat", "Patna", allow_fetch=False))
    assert cached["status"] == "ok" and not cached["stale"] and cached["records"][0]["modal_price"] == 2500

    # cache expired and data.gov.in down: the last real prices are shown, labelled stale
    ts, recs = mandi_mod._memory["Bihar:wheat"]
    mandi_mod._memory["Bihar:wheat"] = (ts - mandi_mod.CACHE_SECONDS - 60, recs)

    async def down(url, params, timeout=8, retries=True):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(mandi_mod, "get_json", down)
    stale = asyncio.run(mandi_mod.mandi_prices("wheat", "Patna"))
    assert stale["status"] == "ok" and stale["stale"] and "unreachable" in stale["stale_reason"]


def test_daily_refresh_queries_each_crop_by_commodity_name(monkeypatch):
    monkeypatch.setenv("DATA_GOV_IN_API_KEY", "test-key")
    mandi_mod._memory.clear()
    mandi_mod._failed_at.clear()
    asked = []

    async def fake(url, params, timeout=8, retries=True):
        asked.append(params.get("filters[commodity]"))
        name = params.get("filters[commodity]")
        return {"records": [{"state": "Bihar", "district": "Gaya", "market": "Gaya", "commodity": name, "variety": "x",
                             "arrival_date": "01/11/2026", "min_price": "2000", "max_price": "2300", "modal_price": "2150"}]}
    monkeypatch.setattr(mandi_mod, "get_json", fake)
    r = client.get("/api/cron/mandi").json()
    assert r["wheat"]["status"] == r["paddy"]["status"] == "ok" and r["paddy"]["rows"] == 2
    assert sorted(asked) == ["Paddy(Dhan)(Basmati)", "Paddy(Dhan)(Common)", "Wheat"]

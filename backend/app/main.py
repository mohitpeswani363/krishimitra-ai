"""KrishiMitra AI API: farmer profiles, live dashboard, and the advisory agent. Also serves the built website."""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from .advisor import classify_intent, compose_answer, evaluate_risks
from .agent import invoke_agent, openai_synthesis
from .db import DB_URL, Advisory, Farmer, Session, farmer_json, init_db
from . import db as db_module
from .knowledge import retrieve_knowledge, validate_citations
from .mandi import mandi_prices
from .weather import LocationNotFound, WeatherUnavailable, geocode_district, weather_for_district

app = FastAPI(title="KrishiMitra AI", version="0.2.0", description="Bilingual agricultural advisory using live weather, soil and mandi data")
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001,http://localhost:3002,http://127.0.0.1:3002").split(",") if x.strip()]
# Deployed site origins: set CORS_ORIGINS to the Vercel site URL, or match by regex (default: this project's *.vercel.app URLs).
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", r"https://krishimitra[a-z0-9-]*\.vercel\.app")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_origin_regex=CORS_ORIGIN_REGEX or None, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    if db_module.DB_INIT_ERROR:  # tables may not exist yet: retry once the database is reachable
        db_module.DB_INIT_ERROR = init_db()
    return JSONResponse(status_code=503, content={"detail": "The database is temporarily unavailable. Please try again in a moment."})


@app.middleware("http")
async def explicit_json_utf8(request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("application/json"):
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


class FarmerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    language: str = "hi"
    state: str = "Bihar"
    district: str = Field(default="Patna", min_length=2, max_length=80)
    village: str = ""
    crop: str = "wheat"
    sowing_date: date
    irrigation: str = "available"
    farm_size: Optional[float] = Field(default=None, ge=0, le=1000)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


DOSAGE = re.compile(r"\d+(\.\d+)?\s*(kg|g|ml|l|litre|liter)\s*/\s*(ha|hectare|acre|l|litre|liter)", re.I)


def get_farmer_or_404(db, farmer_id: int) -> Farmer:
    f = db.get(Farmer, farmer_id)
    if not f:
        raise HTTPException(404, "Farmer not found. Create a profile first.")
    return f


async def live_weather_or_none(f: Farmer) -> tuple[dict | None, str | None]:
    try:
        return await weather_for_district(f.district, f.state), None
    except (WeatherUnavailable, LocationNotFound) as exc:
        return None, str(exc)


@app.get("/api/health")
def health():
    if db_module.DB_INIT_ERROR:
        db_module.DB_INIT_ERROR = init_db()
    return {"status": "ok" if not db_module.DB_INIT_ERROR else "degraded", "database_error": db_module.DB_INIT_ERROR, "database": DB_URL.split(":", 1)[0].split("+")[0],
            "mandi_configured": bool(os.getenv("DATA_GOV_IN_API_KEY")), "llm_configured": bool(os.getenv("OPENAI_API_KEY"))}


@app.post("/api/farmers")
async def create_farmer(payload: FarmerIn):
    if payload.language not in ("hi", "en"):
        raise HTTPException(422, "language must be hi or en")
    if payload.crop not in ("wheat", "paddy"):
        raise HTTPException(422, "crop must be wheat or paddy")
    if payload.irrigation not in ("available", "limited"):
        raise HTTPException(422, "irrigation must be available or limited")
    if payload.sowing_date > date.today():
        raise HTTPException(422, "Sowing date cannot be in the future")
    if (date.today() - payload.sowing_date).days > 240:
        raise HTTPException(422, "Sowing date is more than 240 days ago; enter the current season's sowing date")
    try:
        await geocode_district(payload.district, payload.state)
    except LocationNotFound as exc:
        raise HTTPException(422, str(exc))
    except WeatherUnavailable:
        pass  # location service down: save anyway, the dashboard will retry
    with Session() as db:
        farmer = Farmer(**{**payload.model_dump(), "name": payload.name.strip(), "district": payload.district.strip()})
        db.add(farmer)
        db.commit()
        db.refresh(farmer)
        return farmer_json(farmer)


@app.get("/api/farmers/{farmer_id}")
def get_farmer(farmer_id: int):
    with Session() as db:
        return farmer_json(get_farmer_or_404(db, farmer_id))


@app.get("/api/dashboard/{farmer_id}")
async def dashboard(farmer_id: int):
    with Session() as db:
        f = get_farmer_or_404(db, farmer_id)
        fj = farmer_json(f)
        weather, weather_error = await live_weather_or_none(f)
        history = db.scalars(select(Advisory).where(Advisory.farmer_id == farmer_id).order_by(Advisory.id.desc()).limit(10))
        return {
            "farmer": fj, "weather": weather, "weather_error": weather_error,
            "risks": evaluate_risks(weather, f.crop, fj["stage"]),
            "mandi": await mandi_prices(f.crop, f.district, f.state),
            "advisories": [{"id": a.id, "question": a.question, "answer": a.answer, "created_at": a.created_at.isoformat()} for a in history],
        }


@app.post("/api/advisories/{farmer_id}")
async def advise(farmer_id: int, payload: AskIn):
    with Session() as db:
        f = get_farmer_or_404(db, farmer_id)
        question = payload.question.strip()

        async def classify(s):
            intent = classify_intent(s["question"])
            return {"intent": intent, "_detail": intent}

        async def context(s):
            return {"farmer": farmer_json(f), "_detail": f"{f.crop}, {farmer_json(f)['days_after_sowing']} days after sowing"}

        async def weather_tool(s):
            weather = await weather_for_district(f.district, f.state)
            return {"weather": weather, "_detail": f"{weather['location']}: {weather['rainfall_mm_next_24h']} mm/24h, cache {weather.get('cache')}"}

        async def mandi_tool(s):
            m = await mandi_prices(f.crop, f.district, f.state)
            return {"mandi": m, "_detail": f"{m['status']} ({len(m['records'])} markets)"}

        async def rag_tool(s):
            ev = retrieve_knowledge(s["question"], f.crop, f.state)
            return {"evidence": ev, "_detail": f"{len(ev)} ICAR passages"}

        async def risk_tool(s):
            risks = evaluate_risks(s.get("weather"), f.crop, s["farmer"]["stage"])
            return {"risks": risks, "_detail": ", ".join(r["type"] for r in risks) or "no active risks"}

        async def rule_synthesis(s):
            text = compose_answer(s["question"], s["farmer"], s.get("weather"), s.get("risks", []), s.get("mandi"), s.get("evidence", []), s.get("intent", "general_crop_advice"))
            return {"answer": text, "provider": "rule-based", "_detail": "answer drafted from live facts"}

        async def llm_synthesis(s):
            if not os.getenv("OPENAI_API_KEY"):
                return {"_detail": "skipped (no LLM key configured); rule-based answer kept"}
            text = await openai_synthesis(s)
            return {"answer_llm": text, "_detail": "LLM rewrite produced"}

        async def safety_validation(s):
            llm = s.get("answer_llm")
            if not llm:
                return {"safety": "rule-based answer only", "_detail": "no LLM output to check"}
            allowed = " ".join(e["text"] for e in s.get("evidence", []))
            bad = [m.group(0) for m in DOSAGE.finditer(llm) if m.group(0) not in allowed]
            if bad:
                return {"safety": f"LLM answer rejected: uncited dosage {bad}", "_detail": "LLM rejected, rule-based answer kept"}
            return {"answer": llm, "provider": f"llm:{os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}", "safety": "passed", "_detail": "LLM answer passed checks"}

        async def citations(s):
            ok = validate_citations(s.get("evidence", []))
            return {"citations_valid": ok, "_detail": "all citations match the ICAR index" if ok else "no verifiable citations"}

        state = {"question": question, "errors": [], "steps": []}
        nodes = {"intent_classification": classify, "farmer_context": context, "weather_tool": weather_tool, "mandi_tool": mandi_tool,
                 "agricultural_rag": rag_tool, "crop_risk": risk_tool, "rule_synthesis": rule_synthesis, "llm_synthesis": llm_synthesis,
                 "safety_validation": safety_validation, "citation_validation": citations}
        try:
            state = await invoke_agent(state, nodes)
        except RuntimeError as exc:  # LangGraph missing: run the same steps without it, and say so
            for fn in nodes.values():
                try:
                    state.update({k: v for k, v in (await fn(state)).items() if k != "_detail"})
                except Exception as err:
                    state["errors"].append({"node": fn.__name__, "error": str(err)})
            state["provider"] = state.get("provider", "rule-based") + " (LangGraph unavailable)"
            state["error"] = str(exc)
        if not state.get("answer"):
            raise HTTPException(500, "Advisory could not be generated; please try again.")

        item = Advisory(farmer_id=farmer_id, question=question, answer=state["answer"], mode="live")
        db.add(item)
        db.commit()
        db.refresh(item)
        return {
            "id": item.id, "answer": state["answer"], "intent": state.get("intent"), "provider": state.get("provider"),
            "weather": state.get("weather"), "risks": state.get("risks", []), "mandi": state.get("mandi"),
            "evidence": state.get("evidence", []), "citations_verified": state.get("citations_valid", False),
            "steps": state.get("steps", []), "errors": state.get("errors", []), "safety": state.get("safety"),
            "created_at": item.created_at.isoformat(),
        }


# Serve the exported Next.js site (frontend/out) from the same server when present.
STATIC_DIR = Path(os.getenv("STATIC_DIR", Path(__file__).resolve().parents[2] / "frontend" / "out"))
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")

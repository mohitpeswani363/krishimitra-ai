"""Advisory agent: a compiled LangGraph that runs each tool step in order and records what happened.

Every node is wrapped so a failing tool (e.g. weather or mandi API down) is recorded in state["errors"] and
state["steps"] instead of crashing the request; later nodes adapt to the missing input (see advisor.py).
"""
from __future__ import annotations

import os
import time
from typing import Any, TypedDict

import httpx


class AdvisoryState(TypedDict, total=False):
    question: str
    farmer: dict
    weather: Any
    mandi: Any
    evidence: list[dict]
    risks: list[dict]
    intent: str
    safety: str
    answer: str
    answer_llm: str
    provider: str
    error: str
    errors: list[dict]
    steps: list[dict]
    citations_valid: bool
    trace: list[str]


async def openai_synthesis(state: AdvisoryState) -> str:
    """Optional LLM step. Works with any OpenAI-compatible API (OpenAI, Groq, Together...) via OPENAI_BASE_URL."""
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    lang = "Hindi" if any("ऀ" <= c <= "ॿ" for c in state["question"]) else "the farmer's language"
    evidence = [{"id": e["id"], "page": e["page"], "text": e["text"]} for e in state.get("evidence", [])]
    prompt = (
        f"You are an agricultural advisor for small farmers in Bihar. Answer in {lang}, in under 120 words.\n"
        "Use ONLY the facts below (live weather, soil moisture, risks, mandi prices, ICAR passages). "
        "Never invent numbers, pesticide doses or diagnoses. If a fact is missing, say it is unavailable. "
        "Cite ICAR passages as [ICAR p.<page>]. End with: verify in the field / with your KVK.\n"
        f"Farmer={state['farmer']}\nWeather={state.get('weather')}\nRisks={state.get('risks')}\n"
        f"Mandi={state.get('mandi')}\nEvidence={evidence}\nRule-based draft={state.get('answer')}\nQuestion={state['question']}"
    )
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def build_graph(nodes: dict[str, Any]):
    """Compile a sequential LangGraph; each node is traced, timed and isolated from failures."""
    from langgraph.graph import END, StateGraph

    graph = StateGraph(AdvisoryState)
    for name, fn in nodes.items():
        async def traced(state, _fn=fn, _name=name):
            started = time.perf_counter()
            try:
                update = await _fn(state) or {}
                status, detail = "ok", update.pop("_detail", "")
            except Exception as exc:  # record and continue: the next nodes handle the missing input
                update, status, detail = {}, "error", f"{type(exc).__name__}: {exc}"
            step = {"node": _name, "status": status, "detail": detail, "ms": round((time.perf_counter() - started) * 1000)}
            out = {**update, "trace": state.get("trace", []) + [_name], "steps": state.get("steps", []) + [step]}
            if status == "error":
                out["errors"] = state.get("errors", []) + [{"node": _name, "error": detail}]
            return out
        graph.add_node(name, traced)
    order = list(nodes)
    graph.set_entry_point(order[0])
    for a, b in zip(order, order[1:]):
        graph.add_edge(a, b)
    graph.add_edge(order[-1], END)
    return graph.compile()


async def invoke_agent(state: AdvisoryState, nodes: dict[str, Any]):
    try:
        graph = build_graph(nodes)
    except ImportError:
        raise RuntimeError("LangGraph dependency is unavailable; install backend requirements")
    return await graph.ainvoke(state)

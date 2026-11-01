"""Retrieval over the page-cited ICAR Rabi Agro-Advisory 2021-22 index (data/index/*.jsonl, built from data/sources/*.pdf).

Ranking: term overlap + irrigation-topic score + geographic relevance to the farmer's state, with filters that
drop passages about other states and (for irrigation questions) chemical/variety passages. Every returned
passage carries its page number and is checked against the index by validate_citations().
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

INDEX_PATH = Path(__file__).resolve().parents[1] / "data" / "index" / "icar_rabi_2021_22.jsonl"
STATES = ["Gujarat", "Punjab", "Haryana", "Rajasthan", "Maharashtra", "Uttar Pradesh", "Bihar", "West Bengal", "Karnataka",
          "Tamil Nadu", "Andhra Pradesh", "Telangana", "Madhya Pradesh", "Himachal Pradesh", "Uttarakhand", "Odisha", "Assam"]
TOKEN = r"[a-zA-Z]{3,}|[ऀ-ॿ]{2,}"
IRRIGATION_QUERY = r"irrig|rain|water|सिंचाई|बारिश"
SCOPE = "Historical general crop guidance; not a current weather-specific instruction."


@lru_cache(maxsize=1)
def load_index() -> tuple[dict, ...]:
    if not INDEX_PATH.exists():
        return ()
    return tuple(json.loads(line) for line in INDEX_PATH.read_text(encoding="utf-8").splitlines() if line.strip())


def retrieve_knowledge(query: str, crop: str, state: str = "Bihar") -> list[dict]:
    docs = load_index()
    terms = set(re.findall(TOKEN, query.lower()))
    query_irrigation = bool(re.search(IRRIGATION_QUERY, query, re.I))
    ranked = []
    for d in docs:
        if d.get("crop") not in (crop, "all"):
            continue
        text = d.get("text", "")
        named = [x for x in STATES if re.search(rf"\b{x}\b", text, re.I)]
        if named and state not in named and not re.search(r"all[- ]India|across India|national", text, re.I):
            continue
        irrigation_score = len(re.findall(r"irrigat|irrigation|water management|crown root|jointing|heading|milking", text, re.I))
        if query_irrigation and irrigation_score == 0:
            continue
        if query_irrigation and re.search(r"fertili[sz]er|NPK|herbicide|pesticide|variet(y|ies)|chemical", text, re.I):
            continue
        overlap = len(terms & set(re.findall(TOKEN, text.lower())))
        geographic = 3 if re.search(rf"\b{re.escape(state)}\b", text, re.I) else (1 if not named else 0)
        score = overlap + irrigation_score * 2 + geographic * 3
        if score:
            ranked.append((score, d))
    if not ranked and crop == "wheat":
        ranked = [(len(re.findall(r"irrigat|crown root|jointing|sowing|heading|milking", d.get("text", ""), re.I)), d)
                  for d in docs if d.get("crop") == crop
                  and not re.search(r"\b(Gujarat|Punjab|Haryana|Rajasthan|Maharashtra)\b", d.get("text", ""), re.I)
                  and re.search(r"irrigat|crown root|jointing", d.get("text", ""), re.I)]
    chosen = [dict(d) for _, d in sorted(ranked, key=lambda x: x[0], reverse=True)[:5]]
    for item in chosen:
        item["guidance_scope"] = SCOPE
    return chosen


def validate_citations(evidence: list[dict]) -> bool:
    """True only if every passage exists verbatim in the index with the same title, URL and page."""
    if not evidence:
        return False
    indexed = {d.get("id"): d for d in load_index()}
    return all(
        x.get("id") in indexed
        and x.get("title") == indexed[x["id"]].get("title")
        and x.get("publisher") == "Indian Council of Agricultural Research"
        and x.get("url") == indexed[x["id"]].get("url")
        and x.get("text") == indexed[x["id"]].get("text")
        and x.get("page") == indexed[x["id"]].get("page")
        and isinstance(x.get("page"), int) and x["page"] > 0
        and not x.get("simulated", False)
        for x in evidence
    )

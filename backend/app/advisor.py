"""Crop-weather risk rules and the rule-based bilingual advisory writer.

All numbers in an answer come from the live inputs passed in (Open-Meteo forecast and soil model, AGMARKNET
prices, the farmer's sowing date) or from the cited ICAR passages. Thresholds are stated here so they can be
reviewed by an agronomist.
"""
from __future__ import annotations

import re

# Volumetric soil water content (m3/m3) thresholds for loamy Indo-Gangetic soils (approximate).
SOIL_DRY = 0.18
SOIL_WATERLOGGED = 0.40
RAIN_PROB = 60
RAIN_MM = 5
HEAVY_RAIN_MM = 30
WHEAT_TERMINAL_HEAT_C = 35
EXTREME_HEAT_C = 38
WHEAT_COLD_C = 4
WIND_LODGING_KMH = 40

# Critical irrigation windows for wheat, days after sowing (ICAR Rabi Agro-Advisory 2021-22, Bihar section).
WHEAT_CRITICAL_STAGES = [
    (20, 25, "crown root initiation (CRI)", "शीर्ष जड़ निकलना (CRI)"),
    (40, 45, "late jointing", "गाँठ बनना"),
    (65, 70, "heading", "बाली निकलना"),
    (90, 100, "milking", "दूधिया अवस्था"),
]

INTENTS = {
    "market_price": r"price|rate|mandi|market|sell|मंडी|भाव|दाम|कीमत|बेच",
    "irrigation": r"irrig|water|rain|सिंचाई|पानी|बारिश",
    "weather_risk": r"weather|heat|cold|frost|wind|storm|मौसम|गर्मी|ठंड|पाला|आंधी|हवा",
}


def classify_intent(question: str) -> str:
    for intent, pattern in INTENTS.items():
        if re.search(pattern, question, re.I):
            return intent
    return "general_crop_advice"


def wants_hindi(question: str, farmer: dict) -> bool:
    if re.search(r"[ऀ-ॿ]", question):
        return True
    if re.search(r"[a-zA-Z]{3,}", question):
        return False
    return farmer.get("language") == "hi"


def _risk(kind, severity, conditions, en_title, hi_title, en_action, hi_action, weather):
    return {"type": kind, "severity": severity, "conditions": conditions, "title_en": en_title, "title_hi": hi_title,
            "action": en_action, "action_hi": hi_action, "valid_for": "next 72 hours", "source": weather.get("source")}


def evaluate_risks(weather: dict | None, crop: str, stage: str = "vegetative") -> list[dict]:
    if not weather:
        return []
    risks = []
    prob, rain = weather.get("precipitation_probability_pct", 0) or 0, weather.get("rainfall_mm_next_24h", 0) or 0
    daily = weather.get("daily") or []
    t_max = max([d["t_max"] for d in daily if d.get("t_max") is not None] or [weather.get("temperature_c", 0)])
    t_min = min([d["t_min"] for d in daily if d.get("t_min") is not None] or [weather.get("temperature_c", 99)])
    heavy = max([d["rain_mm"] for d in daily if d.get("rain_mm") is not None] or [0])
    soil = (weather.get("soil") or {}).get("moisture_topsoil_m3m3")
    if prob >= RAIN_PROB and rain >= RAIN_MM:
        risks.append(_risk("rain-and-field-work", "moderate", f"{prob}% rain probability; {rain} mm forecast in 24 h",
                           "Rain expected: hold irrigation", "बारिश की संभावना: सिंचाई रोकें",
                           "Check the field before irrigating; expected rain may meet the crop's water need.",
                           "सिंचाई से पहले खेत देखें; आने वाली बारिश से फसल की पानी की ज़रूरत पूरी हो सकती है।", weather))
    if heavy >= HEAVY_RAIN_MM:
        risks.append(_risk("heavy-rain", "high", f"up to {heavy} mm rain in a day over the next 3 days",
                           "Heavy rain: clear field drainage", "भारी बारिश: खेत से पानी निकासी करें",
                           "Open drainage channels and postpone fertiliser or spraying until after the rain.",
                           "नाली खोलें, और खाद या छिड़काव बारिश के बाद तक टालें।", weather))
    if t_max >= EXTREME_HEAT_C or (crop == "wheat" and stage in ("reproductive", "maturity") and t_max >= WHEAT_TERMINAL_HEAT_C):
        risks.append(_risk("heat-stress", "high" if t_max >= EXTREME_HEAT_C else "moderate", f"max temperature {t_max} °C in the next 3 days",
                           "Heat stress risk", "गर्मी से फसल पर तनाव का खतरा",
                           "Avoid water stress during grain filling; irrigate in the evening if soil is dry.",
                           "दाना भरने के समय पानी की कमी न होने दें; मिट्टी सूखी हो तो शाम को सिंचाई करें।", weather))
    if crop == "wheat" and t_min <= WHEAT_COLD_C:
        risks.append(_risk("cold-frost", "moderate", f"min temperature {t_min} °C in the next 3 days",
                           "Cold / frost risk", "ठंड / पाला का खतरा",
                           "Watch for frost on clear, calm nights; consult your KVK on protective light irrigation.",
                           "साफ़ और शांत रातों में पाले पर नज़र रखें; हल्की सिंचाई के लिए अपने KVK से सलाह लें।", weather))
    if (weather.get("wind_kmh") or 0) >= WIND_LODGING_KMH:
        risks.append(_risk("wind-lodging", "moderate", f"wind {weather['wind_kmh']} km/h",
                           "Strong wind: lodging risk", "तेज़ हवा: फसल गिरने का खतरा",
                           "Avoid irrigating just before strong wind; wet soil increases lodging.",
                           "तेज़ हवा से ठीक पहले सिंचाई न करें; गीली मिट्टी में फसल ज़्यादा गिरती है।", weather))
    if soil is not None and soil < SOIL_DRY and not (prob >= RAIN_PROB and rain >= RAIN_MM):
        risks.append(_risk("dry-soil", "moderate", f"topsoil moisture {soil} m³/m³ (dry below {SOIL_DRY})",
                           "Dry topsoil: water stress likely", "ऊपरी मिट्टी सूखी: पानी की कमी संभव",
                           "Confirm by hand (soil crumbles when squeezed) and irrigate if confirmed.",
                           "हाथ से जाँचें (मुट्ठी में मिट्टी बिखर जाए) और पुष्टि हो तो सिंचाई करें।", weather))
    if soil is not None and soil >= SOIL_WATERLOGGED:
        risks.append(_risk("waterlogging", "moderate", f"topsoil moisture {soil} m³/m³ (saturated above {SOIL_WATERLOGGED})",
                           "Waterlogging risk", "जलभराव का खतरा",
                           "Do not irrigate; drain standing water.", "सिंचाई न करें; खड़ा पानी निकालें।", weather))
    return risks


def critical_stage(das: int) -> tuple[str, str, str] | None:
    """Current or next wheat critical irrigation window: (status, english name, hindi name)."""
    for start, end, en, hi in WHEAT_CRITICAL_STAGES:
        if start <= das <= end:
            return ("now", en, hi)
        if das < start and start - das <= 10:
            return (f"in {start - das} days", en, hi)
    return None


def compose_answer(question: str, farmer: dict, weather: dict | None, risks: list[dict], mandi: dict | None,
                   evidence: list[dict], intent: str) -> str:
    hi = wants_hindi(question, farmer)
    L = (lambda en, h: h) if hi else (lambda en, h: en)
    lines: list[str] = []
    das, crop = farmer.get("days_after_sowing", 0), farmer.get("crop")
    crop_name = L(crop, {"wheat": "गेहूं", "paddy": "धान"}.get(crop, crop))

    if intent == "market_price":
        lines += _price_lines(mandi, L)

    if weather:
        prob, rain = weather.get("precipitation_probability_pct", 0), weather.get("rainfall_mm_next_24h", 0)
        soil = (weather.get("soil") or {}).get("moisture_topsoil_m3m3")
        place = weather.get("location", farmer.get("district"))
        lines.append(L(f"Live forecast for {place}: {weather['temperature_c']} °C now, {rain} mm rain expected in 24 h ({prob}% chance).",
                       f"{place} का ताज़ा मौसम: अभी {weather['temperature_c']} °C, अगले 24 घंटे में {rain} मिमी बारिश ({prob}% संभावना)।"))
        if soil is not None:
            state = L("dry", "सूखी") if soil < SOIL_DRY else (L("saturated", "बहुत गीली") if soil >= SOIL_WATERLOGGED else L("moist", "नम"))
            lines.append(L(f"Modelled topsoil moisture: {soil} m³/m³ ({state}).", f"अनुमानित ऊपरी मिट्टी की नमी: {soil} m³/m³ ({state})।"))
        if intent in ("irrigation", "general_crop_advice", "weather_risk"):
            lines.append(_irrigation_decision(farmer, prob, rain, soil, L, critical_stage(das) if crop == "wheat" else None))
        if weather.get("cache") == "stale":
            lines.append(L(f"Note: weather service is unreachable; this uses the last real forecast ({weather.get('stale_minutes')} min old).",
                           f"ध्यान दें: मौसम सेवा अभी उपलब्ध नहीं; {weather.get('stale_minutes')} मिनट पुराना असली पूर्वानुमान उपयोग हुआ है।"))
    else:
        lines.append(L("Live weather is unavailable right now, so this advice does not use a forecast. Check soil moisture by hand before irrigating.",
                       "अभी ताज़ा मौसम उपलब्ध नहीं है, इसलिए यह सलाह पूर्वानुमान के बिना है। सिंचाई से पहले हाथ से मिट्टी की नमी जाँचें।"))

    for r in risks:
        if r["type"] != "rain-and-field-work" and r["type"] != "dry-soil":
            lines.append(L(f"⚠ {r['title_en']} ({r['conditions']}): {r['action']}", f"⚠ {r['title_hi']} ({r['conditions']}): {r['action_hi']}"))

    if crop == "wheat":
        cs = critical_stage(das)
        if cs:
            when = L(cs[0], "अभी" if cs[0] == "now" else cs[0].replace("in ", "").replace(" days", " दिन में"))
            lines.append(L(f"Your wheat is {das} days after sowing; the {cs[1]} irrigation window is {when} (ICAR, cited below).",
                           f"आपका {crop_name} बुवाई के {das} दिन बाद है; {cs[2]} की सिंचाई का समय {when} है (ICAR, नीचे स्रोत)।"))
    if intent != "market_price" and mandi and mandi.get("status") == "ok":
        lines += _price_lines(mandi, L)[:1]
    if not evidence:
        lines.append(L(f"No cited ICAR guidance for {crop_name} is indexed yet; confirm with your local KVK.",
                       f"{crop_name} के लिए ICAR का स्रोत अभी उपलब्ध नहीं; अपने स्थानीय KVK से पुष्टि करें।"))
    lines.append(L("Rule-based advisory from live data; verify in the field before acting.",
                   "यह ताज़ा डेटा पर आधारित नियम-आधारित सलाह है; कदम उठाने से पहले खेत में जाँच लें।"))
    return " ".join(lines)


def _irrigation_decision(farmer: dict, prob, rain, soil, L, stage=None) -> str:
    if prob >= RAIN_PROB and rain >= RAIN_MM:
        return L("Decision: hold irrigation for now; rain is likely. Re-check after the rain.",
                 "निर्णय: अभी सिंचाई रोकें, बारिश की संभावना है। बारिश के बाद फिर जाँचें।")
    if soil is not None and soil >= SOIL_WATERLOGGED:
        return L("Decision: do not irrigate; soil is already saturated.", "निर्णय: सिंचाई न करें, मिट्टी पहले से बहुत गीली है।")
    if soil is not None and soil < SOIL_DRY:
        if farmer.get("irrigation") == "limited":
            return L("Decision: soil looks dry and rain is unlikely; use your limited water at the next critical crop stage.",
                     "निर्णय: मिट्टी सूखी लग रही है और बारिश की संभावना कम है; सीमित पानी अगली महत्वपूर्ण अवस्था पर दें।")
        return L("Decision: soil looks dry and rain is unlikely; irrigate in the next 1–2 days after a hand check.",
                 "निर्णय: मिट्टी सूखी लग रही है और बारिश की संभावना कम है; हाथ से जाँच कर 1–2 दिन में सिंचाई करें।")
    if stage and stage[0] == "now":
        return L(f"Decision: soil is moist today, but this is the critical {stage[1]} window; irrigate within this window "
                 "as soon as the topsoil starts drying. Check the field again tomorrow.",
                 f"निर्णय: आज मिट्टी नम है, पर यह {stage[2]} की महत्वपूर्ण अवधि है; ऊपरी मिट्टी सूखने लगे तो इसी अवधि में "
                 "सिंचाई करें। कल फिर खेत जाँचें।")
    return L("Decision: soil moisture looks adequate; no irrigation needed today. Re-check in 2–3 days.",
             "निर्णय: मिट्टी में पर्याप्त नमी दिख रही है; आज सिंचाई की ज़रूरत नहीं। 2–3 दिन बाद फिर जाँचें।")


def _price_lines(mandi: dict | None, L) -> list[str]:
    if not mandi or mandi.get("status") != "ok":
        msg = (mandi or {}).get("message", "unavailable")
        return [L(f"Mandi prices: {msg}", f"मंडी भाव: अभी उपलब्ध नहीं ({msg})")]
    r = mandi["records"][0]
    return [L(f"Today's mandi price at {r['market']} ({r['district']}): ₹{r['modal_price']:.0f}/quintal modal "
              f"(₹{r['min_price']:.0f}–{r['max_price']:.0f}), reported {r['arrival_date']}.",
              f"आज {r['market']} ({r['district']}) मंडी भाव: ₹{r['modal_price']:.0f}/क्विंटल (₹{r['min_price']:.0f}–{r['max_price']:.0f}), "
              f"तारीख {r['arrival_date']}।")]

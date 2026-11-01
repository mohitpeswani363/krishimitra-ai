# How it works: notes for the team before judging

Every team member should be able to answer these questions without the code open.

**Where does each number on screen come from?**
Temperature, rain, rain chance, humidity, wind, the 3-day forecast, soil moisture and soil temperature come from one Open-Meteo forecast call (`weather.py → live_weather`), made for the coordinates of the farmer's district (`geocode_district`). Mandi prices come from the data.gov.in AGMARKNET resource `9ef84268-d588-465a-a308-a864a43d0070` (`mandi.py`). Days after sowing come from the profile's sowing date (`db.py → days_after_sowing`). Nothing is hardcoded.

**What happens if Open-Meteo is down?**
`get_json` retries twice (0.5 s, then 1.5 s) on network errors, 429 and 5xx. If it still fails, `live_weather` returns the last real forecast for that location if it is less than 6 hours old, marked `cache: "stale"` with its age, and the UI and answer both say so. With nothing cached, the dashboard shows the error with a Retry button, and the agent records the `weather_tool` failure and answers without forecast numbers.

**How is the irrigation decision made?** (`advisor.py → _irrigation_decision`)
1. Rain chance ≥ 60% and ≥ 5 mm in 24 h → hold irrigation.
2. Topsoil ≥ 0.40 m³/m³ → saturated, do not irrigate.
3. Topsoil < 0.18 → dry; irrigate within 1–2 days after a hand check (with limited water, save it for the next critical stage).
4. Otherwise adequate, unless the wheat is inside an ICAR critical window (CRI 20–25, jointing 40–45, heading 65–70, milking 90–100 days after sowing). Then: irrigate within the window once the topsoil starts drying.

**Why is soil moisture "modelled"?**
Open-Meteo's soil moisture comes from a land-surface model for that location, not a sensor in the field. That is why every answer asks the farmer to check by hand.

**How does retrieval work, and how do you stop hallucinated sources?**
`knowledge.py` scores each of the 434 ICAR passages: words shared with the question, irrigation-topic terms (×2), and geographic relevance (+9 for passages naming the farmer's state). It drops passages that name a different state, and for irrigation questions drops fertiliser and pesticide passages. `validate_citations` then checks every passage shown against the index (same id, title, URL, page and exact text); a test proves a tampered excerpt or wrong page is rejected.

**Where is the AI / agent?**
`agent.py` compiles a LangGraph `StateGraph` of 10 nodes. Each node is a tool or reasoning step; the wrapper times it, records it in `steps`, and catches failures into `errors` so later nodes adapt (for example, the answer writer handles missing weather). The optional LLM node only gets the facts gathered by earlier nodes, and the safety node rejects its output if it contains a chemical dose not found in a cited passage.

**Why not just use ChatGPT?**
A general chatbot doesn't know today's forecast for Gaya, the farmer's sowing date, or today's Bihar mandi prices, and it can invent doses and sources. KrishiMitra grounds every number in a live source, ties advice to the crop's exact stage, verifies its citations, and works in Hindi.

**What would you build next?**
IMD district forecasts alongside Open-Meteo; Kharif (paddy) ICAR advisories in the index; voice input and output in Hindi and Bhojpuri; WhatsApp/SMS alerts when a risk appears for a saved farmer; field soil sensors where available.

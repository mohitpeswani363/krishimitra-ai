# KrishiMitra AI: hackathon submission

## 1. Problem statement

**Target issue and context.** Bihar's wheat and paddy farmers make irrigation and selling decisions with little timely information. Weather in the Indo-Gangetic plain is shifting: unseasonal rain, late-season heat that shrivels wheat grain, and cold waves. Irrigating just before rain wastes scarce diesel and groundwater and can waterlog the crop. Missing the crown-root irrigation (about 21 days after sowing) cuts wheat yield. Most advisories are in English, are generic to a whole state, and are not matched to the farmer's own crop stage.

**Target audience.** Small and marginal farmers in Bihar (most of the state's holdings are under 1 hectare), many more comfortable in Hindi than English, plus the extension workers and Krishi Vigyan Kendra staff who support them.

## 2. Solution description

**High-level overview.** KrishiMitra is a bilingual advisory agent. The farmer saves a profile (district, crop, sowing date, irrigation access) and asks a question in Hindi or English. A 10-step LangGraph agent:

1. classifies the question (irrigation, weather risk, market price, general),
2. loads the farmer's crop and its age in days,
3. fetches the live forecast and modelled soil moisture for the district,
4. fetches today's mandi prices,
5. retrieves page-cited ICAR guidance,
6. runs crop-specific risk rules (rain, heavy rain, heat, frost, wind, dry soil, waterlogging),
7. drafts a decision from those facts ("hold irrigation, 12 mm rain likely" or "soil is moist but the CRI window is now"),
8. optionally rewrites it with an LLM restricted to those facts,
9. rejects any LLM output containing an uncited chemical dose,
10. verifies every citation against the ICAR index.

If a tool fails, the agent records it and carries on. A weather outage serves the last real forecast, clearly labelled with its age. With no forecast at all, the answer says the forecast is unavailable instead of guessing. The site shows each step the agent took, with timings.

**Technical architecture.** Next.js (static export) and FastAPI in one Docker container; LangGraph for the agent; SQLAlchemy with SQLite (or PostgreSQL) for profiles and history; httpx with retries and caching for external APIs.
Data: Open-Meteo forecast and soil-moisture model (live), AGMARKNET daily mandi prices via the data.gov.in API (live), and the ICAR Rabi Agro-Advisory 2021-22 (434 passages indexed by page).
Retrieval: lexical plus topic and geographic ranking that filters out other states' passages, with exact-match citation validation.
Optional LLM: any OpenAI-compatible model.

## 3. Prototype link

`https://huggingface.co/spaces/<your-username>/krishimitra-ai` (fill in after deploying; see README → Deploy)

## 4. Demo video

`<link to the uploaded video>` (`krishimitra-demo.mp4`, a recording of the live app)

## 5. GitHub repository

`https://github.com/<your-username>/krishimitra-ai`

## 6. Pitch deck

`<link to the deck>`

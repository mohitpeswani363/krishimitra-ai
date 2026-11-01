'use client';
import { useEffect, useState } from 'react';

// Empty = same origin (production: FastAPI serves this site). Set NEXT_PUBLIC_API_URL for `npm run dev`.
const API = process.env.NEXT_PUBLIC_API_URL || '';

type Farmer = { id: number; name: string; language: string; district: string; crop: string; sowing_date: string; stage: string; days_after_sowing: number; irrigation: string };
type Day = { date: string; t_max: number; t_min: number; rain_mm: number; rain_prob_pct: number };
type Weather = {
  label: string; location: string; temperature_c: number; rainfall_mm_next_24h: number; precipitation_probability_pct: number;
  humidity_pct: number; wind_kmh: number; forecast_time: string; attribution: string; cache?: string; stale_minutes?: number;
  soil: { moisture_topsoil_m3m3: number | null; moisture_rootzone_m3m3: number | null; temperature_6cm_c: number | null; source: string };
  daily: Day[];
};
type Risk = { type: string; severity: string; conditions: string; title_en: string; title_hi: string; action: string; action_hi: string };
type MandiRow = { market: string; district: string; commodity: string; variety: string; arrival_date: string; min_price: number; max_price: number; modal_price: number };
type Mandi = { status: 'ok' | 'not_configured' | 'unavailable' | 'no_records'; records: MandiRow[]; message?: string; source: string; district_match?: boolean };
type Dash = { farmer: Farmer; weather: Weather | null; weather_error: string | null; risks: Risk[]; mandi: Mandi; advisories: { id: number; question: string; answer: string }[] };
type Step = { node: string; status: string; detail: string; ms: number };
type Evidence = { id: string; title: string; page: number; url: string; text: string; guidance_scope: string };
type Result = { answer: string; provider: string; steps: Step[]; evidence: Evidence[]; citations_verified: boolean; intent: string };

const SAVED = 'krishimitra_farmer_id';
const store = {
  get: () => { try { return localStorage.getItem(SAVED); } catch { return null; } },
  set: (v: string) => { try { localStorage.setItem(SAVED, v); } catch { /* storage blocked: profile still works this session */ } },
  clear: () => { try { localStorage.removeItem(SAVED); } catch { /* ignore */ } },
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`${API}${path}`, init);
  } catch {
    throw new Error('Cannot reach the KrishiMitra server. Check your internet connection and try again.');
  }
  const text = await r.text();
  let data: any = null;
  try { data = JSON.parse(text); } catch { /* non-JSON error page */ }
  if (!r.ok) {
    const detail = data?.detail;
    const msg = Array.isArray(detail) ? detail.map((d: any) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join('; ') : detail || text || r.statusText;
    throw Object.assign(new Error(msg), { status: r.status });
  }
  return data as T;
}

const daysAgo = (n: number) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);

export default function Home() {
  const [hi, setHi] = useState(true);
  const [farmer, setFarmer] = useState<Farmer | null>(null);
  const [editing, setEditing] = useState(false);
  const [dash, setDash] = useState<Dash | null>(null);
  const [question, setQuestion] = useState('मेरे गेहूं के खेत में कल सिंचाई करनी चाहिए या बारिश होने वाली है?');
  const [asked, setAsked] = useState('');
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const t = (en: string, h: string) => (hi ? h : en);

  async function load(id: number) {
    const d = await api<Dash>(`/api/dashboard/${id}`);
    setDash(d); setFarmer(d.farmer); store.set(String(id));
  }

  useEffect(() => {
    (async () => {
      const saved = store.get();
      if (saved) {
        try { await load(Number(saved)); }
        catch (e: any) { if (e.status === 404) store.clear(); else setError(e.message); }
      }
      setLoading(false);
    })();
  }, []);

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault(); setBusy(true); setError('');
    const x = new FormData(e.currentTarget);
    try {
      const f = await api<Farmer>('/api/farmers', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: x.get('name'), language: hi ? 'hi' : 'en', state: 'Bihar', district: x.get('district'), village: '', crop: x.get('crop'), sowing_date: x.get('sowing_date'), irrigation: x.get('irrigation'), farm_size: null }),
      });
      await load(f.id); // load the new farm's live data before leaving the form, so the old dashboard never flashes
      setResult(null); setAsked(''); setEditing(false);
    } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  }

  async function ask(q: string) {
    if (!farmer || q.trim().length < 2) return;
    setBusy(true); setError(''); setAsked(q);
    try {
      const d = await api<Result>(`/api/advisories/${farmer.id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: q }) });
      setResult(d);
      load(farmer.id).catch(() => { /* history refresh is best-effort */ });
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }

  async function retry() {
    if (!farmer) return;
    setBusy(true); setError('');
    try { await load(farmer.id); } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  }

  const w = dash?.weather;
  const soil = w?.soil.moisture_topsoil_m3m3;
  const soilState = soil == null ? '' : soil < 0.18 ? t('Dry', 'सूखी') : soil >= 0.4 ? t('Saturated', 'बहुत गीली') : t('Moist', 'नम');
  const stageName = (s: string) => t(s, ({ establishment: 'अंकुरण', vegetative: 'बढ़वार', reproductive: 'बाली/फूल', maturity: 'पकाव' } as Record<string, string>)[s] || s);
  const cropName = (c: string) => (c === 'wheat' ? t('wheat', 'गेहूं') : t('paddy', 'धान'));
  const suggestions = farmer?.crop === 'paddy'
    ? [t('Will heavy rain affect my paddy?', 'क्या भारी बारिश से धान को नुकसान होगा?'), t('What is the paddy mandi price today?', 'आज धान का मंडी भाव क्या है?')]
    : [t('Should I irrigate my wheat tomorrow?', 'क्या कल गेहूं में सिंचाई करूँ?'), t('Is there frost or heat risk this week?', 'क्या इस हफ्ते पाला या गर्मी का खतरा है?'), t('What is the wheat mandi price today?', 'आज गेहूं का मंडी भाव क्या है?')];

  const profileForm = (
    <form className="form" onSubmit={create}>
      <label>{t('Your name', 'आपका नाम')}<input id="name" name="name" required maxLength={100} placeholder="Sita Devi" defaultValue={farmer?.name} /></label>
      <label>{t('District (Bihar)', 'ज़िला (बिहार)')}<input id="district" name="district" required minLength={2} defaultValue={farmer?.district || 'Patna'} /></label>
      <label>{t('Crop', 'फसल')}<select id="crop" name="crop" defaultValue={farmer?.crop || 'wheat'}><option value="wheat">{t('Wheat', 'गेहूं')}</option><option value="paddy">{t('Paddy', 'धान')}</option></select></label>
      <label>{t('Sowing date', 'बुवाई की तारीख')}<input id="sowing_date" name="sowing_date" type="date" required max={daysAgo(0)} min={daysAgo(240)} defaultValue={farmer?.sowing_date || daysAgo(24)} /></label>
      <label>{t('Irrigation', 'सिंचाई')}<select id="irrigation" name="irrigation" defaultValue={farmer?.irrigation || 'available'}><option value="available">{t('Available', 'उपलब्ध')}</option><option value="limited">{t('Limited', 'सीमित')}</option></select></label>
      <div className="row" style={{ alignSelf: 'end' }}>
        <button className="primary" disabled={busy}>{busy ? '…' : t('Save profile', 'प्रोफ़ाइल सहेजें')}</button>
        {farmer && <button type="button" className="secondary" onClick={() => setEditing(false)}>{t('Cancel', 'रद्द करें')}</button>}
      </div>
    </form>
  );

  return (
    <main className="shell">
      <header className="top">
        <div className="brand">🌱 Krishi<span>Mitra</span> AI</div>
        <div className="row">
          <span className="badge live">● {t('LIVE DATA', 'लाइव डेटा')}</span>
          <button className="secondary" onClick={() => setHi(!hi)} aria-label="Switch language">{hi ? 'हिंदी' : 'English'} ⇄</button>
        </div>
      </header>

      <section className="hero">
        <div>
          <h1>{t('Your farm, understood.', 'आपकी खेती, आपकी भाषा में।')}</h1>
          <div className="muted">{t('Live weather, soil moisture and mandi prices turned into crop-stage advice for Bihar farmers', 'बिहार के किसानों के लिए ताज़ा मौसम, मिट्टी की नमी और मंडी भाव पर आधारित फसल सलाह')}</div>
        </div>
        <div className="badge">{t('AGENT · OPEN-METEO · AGMARKNET · ICAR', 'एजेंट · Open-Meteo · AGMARKNET · ICAR')}</div>
      </section>

      {error && <div className="risk sev-high error-box" role="alert"><span>{error}</span>{farmer && <button className="secondary" onClick={retry}>{t('Retry', 'फिर कोशिश करें')}</button>}</div>}

      {loading ? <p className="muted">{t('Loading…', 'लोड हो रहा है…')}</p> : !farmer || editing ? (
        <section className="card" style={{ maxWidth: 720 }}>
          <h2>{t('Create your farm profile', 'अपनी खेती की प्रोफ़ाइल बनाएं')}</h2>
          <p className="muted" style={{ marginTop: -8 }}>{t('Advice is calculated from your district’s live forecast and your crop’s age.', 'सलाह आपके ज़िले के ताज़ा मौसम और फसल की उम्र से बनती है।')}</p>
          {profileForm}
        </section>
      ) : (
        <div className="layout">
          <div className="stack">
            <section className="card">
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <div>
                  <div style={{ fontSize: 23, fontWeight: 700 }}>{t('Namaste', 'नमस्ते')}, {farmer.name} 👋</div>
                  <p className="muted" style={{ margin: '6px 0 0' }}>{farmer.district}, Bihar · {cropName(farmer.crop)} · {t(`${farmer.days_after_sowing} days after sowing`, `बुवाई के ${farmer.days_after_sowing} दिन`)} · {stageName(farmer.stage)}</p>
                </div>
                <button className="secondary" onClick={() => setEditing(true)}>{t('Edit profile', 'प्रोफ़ाइल बदलें')}</button>
              </div>
              <hr style={{ border: 0, borderTop: '1px solid #edf0e8', margin: '18px 0' }} />
              <h2>{t('Live weather & soil', 'ताज़ा मौसम और मिट्टी')}</h2>
              {!w ? (
                <div className="note error-box"><span>{t('Live weather is unavailable right now: ', 'अभी ताज़ा मौसम उपलब्ध नहीं: ')}{dash?.weather_error}</span><button className="secondary" onClick={retry} disabled={busy}>{t('Retry', 'फिर कोशिश करें')}</button></div>
              ) : (
                <>
                  <div className="weather">
                    <div style={{ fontSize: 28 }}>🌦️</div>
                    <div className="temp">{w.temperature_c}°</div>
                    <div><b>{w.location}</b><div className="muted">{w.label} · {w.forecast_time.replace('T', ' ')}</div></div>
                  </div>
                  <div className="metrics" style={{ marginTop: 14 }}>
                    <span>🌧 {w.rainfall_mm_next_24h} mm / 24h</span><span>☔ {w.precipitation_probability_pct}%</span>
                    <span>💧 {w.humidity_pct}% {t('humidity', 'नमी')}</span><span>💨 {w.wind_kmh} km/h</span>
                  </div>
                  {soil != null && (
                    <div style={{ marginTop: 16 }}>
                      <div className="row" style={{ justifyContent: 'space-between', fontSize: 13 }}>
                        <b>{t('Topsoil moisture (0–9 cm)', 'ऊपरी मिट्टी की नमी (0–9 सेमी)')}: {soil} m³/m³ · {soilState}</b>
                        {w.soil.temperature_6cm_c != null && <span className="muted">{t('Soil temp', 'मिट्टी तापमान')} {w.soil.temperature_6cm_c}°C</span>}
                      </div>
                      <div className="soilbar" aria-hidden="true"><i style={{ left: `calc(${Math.min(100, Math.max(0, soil / 0.5 * 100))}% - 2px)` }} /></div>
                      <div className="soilscale"><span>{t('dry', 'सूखी')} &lt;0.18</span><span>{t('moist', 'नम')}</span><span>{t('saturated', 'गीली')} ≥0.40</span></div>
                    </div>
                  )}
                  <div className="days">
                    {w.daily.map(d => (
                      <div className="day" key={d.date}><b>{new Date(d.date + 'T00:00').toLocaleDateString(hi ? 'hi-IN' : 'en-IN', { weekday: 'short', day: 'numeric', month: 'short' })}</b>{d.t_min}° / {d.t_max}°<br />🌧 {d.rain_mm} mm · {d.rain_prob_pct}%</div>
                    ))}
                  </div>
                  {dash!.risks.length === 0 && <p className="muted" style={{ fontSize: 13 }}>✓ {t('No weather risks for your crop in the next 3 days.', 'अगले 3 दिनों में आपकी फसल के लिए मौसम का कोई खतरा नहीं।')}</p>}
                  {dash!.risks.map(r => (
                    <div className={`risk ${r.severity === 'high' ? 'sev-high' : ''}`} key={r.type}>
                      <b>⚠ {t(r.title_en, r.title_hi)}</b> <span className="muted">({r.conditions})</span>
                      <p style={{ marginBottom: 0 }}>{t(r.action, r.action_hi)}</p>
                    </div>
                  ))}
                  <p className="muted" style={{ fontSize: 11, marginBottom: 0 }}>{w.attribution}{w.cache === 'stale' ? ` · ${t(`last real forecast, ${w.stale_minutes} min old`, `${w.stale_minutes} मिनट पुराना असली पूर्वानुमान`)}` : ''}</p>
                </>
              )}
            </section>

            <section className="card">
              <h2>📈 {t(`Today's ${farmer.crop} mandi prices`, `आज के ${cropName(farmer.crop)} मंडी भाव`)}</h2>
              {dash?.mandi.status === 'ok' ? (
                <div style={{ overflowX: 'auto' }}>
                  <table className="prices">
                    <thead><tr><th>{t('Market', 'मंडी')}</th><th>{t('District', 'ज़िला')}</th><th>{t('Modal ₹/qtl', 'औसत ₹/क्विंटल')}</th><th>{t('Range', 'दायरा')}</th><th>{t('Date', 'तारीख')}</th></tr></thead>
                    <tbody>{dash.mandi.records.map((r, i) => <tr key={i}><td>{r.market}</td><td>{r.district}</td><td><b>₹{r.modal_price}</b></td><td>₹{r.min_price}–{r.max_price}</td><td>{r.arrival_date}</td></tr>)}</tbody>
                  </table>
                </div>
              ) : <div className="note">{t('Mandi prices: ', 'मंडी भाव: ')}{dash?.mandi.message}</div>}
              <p className="muted" style={{ fontSize: 11, marginBottom: 0 }}>{dash?.mandi.source}</p>
            </section>
          </div>

          <section className="card">
            <h2>🤖 {t('Ask KrishiMitra', 'कृषिमित्र से पूछें')}</h2>
            <p className="muted">{t('An agent checks live weather, soil, mandi prices and ICAR guidance before answering.', 'जवाब से पहले एजेंट ताज़ा मौसम, मिट्टी, मंडी भाव और ICAR सलाह जाँचता है।')}</p>
            <div className="chips">{suggestions.map(s => <button key={s} className="chip" disabled={busy} onClick={() => { setQuestion(s); ask(s); }}>{s}</button>)}</div>
            <div className="chatbox" aria-live="polite">
              {asked && <div className="bubble question">{asked}</div>}
              {busy && asked && <div className="bubble muted">{t('Checking live weather, soil, mandi and ICAR sources…', 'ताज़ा मौसम, मिट्टी, मंडी और ICAR स्रोत जाँचे जा रहे हैं…')}</div>}
              {!busy && result && <div className="bubble">{result.answer}</div>}
              {!asked && <div className="bubble muted">{t('Tap a question above or type your own, in Hindi or English.', 'ऊपर कोई सवाल चुनें या हिंदी/अंग्रेज़ी में अपना सवाल लिखें।')}</div>}
            </div>
            <form className="chatrow" onSubmit={e => { e.preventDefault(); ask(question); }}>
              <input id="question" value={question} onChange={e => setQuestion(e.target.value)} aria-label="Question" maxLength={2000} />
              <button className="primary" disabled={busy || question.trim().length < 2}>{busy ? '…' : t('Ask', 'पूछें')}</button>
            </form>

            {result && !busy && (
              <>
                <div style={{ marginTop: 18 }}>
                  <b>{t('How the agent answered', 'एजेंट ने कैसे जवाब बनाया')}</b> <span className="muted" style={{ fontSize: 12 }}>· {result.provider}</span>
                  <ul className="steps">
                    {result.steps.map(s => <li key={s.node}><span className={s.status}>{s.status === 'ok' ? '✓' : '✗'}</span><span>{s.node}</span><span className="muted">{s.detail}</span><span className="muted">{s.ms} ms</span></li>)}
                  </ul>
                </div>
                <div style={{ marginTop: 18 }}>
                  <b>{t('Sources', 'स्रोत')}</b> {result.citations_verified && <span className="badge live" style={{ padding: '3px 8px', fontSize: 11 }}>✓ {t('citations verified', 'स्रोत सत्यापित')}</span>}
                  {result.evidence.length === 0 && <p className="muted" style={{ fontSize: 12 }}>{t('No indexed ICAR passage matched this question.', 'इस सवाल से मेल खाता ICAR अंश नहीं मिला।')}</p>}
                  {result.evidence.map(x => (
                    <p key={x.id} className="src"><b>{x.title}, p.{x.page}</b> · <a href={`${x.url}#page=${x.page}`} target="_blank" rel="noreferrer">PDF</a><br /><em className="muted">{x.guidance_scope}</em><br />{x.text}</p>
                  ))}
                </div>
              </>
            )}
          </section>

          <section className="card full">
            <h2>🧾 {t('Recent advisory history', 'हाल की सलाह')}</h2>
            {dash?.advisories.length ? dash.advisories.map(a => <div className="history" key={a.id}><span>{a.question}</span><span>✓ {t('Saved', 'सहेजा गया')}</span></div>) : <div className="muted">{t('Your saved advisories will appear here.', 'आपकी सहेजी सलाह यहां दिखेगी।')}</div>}
          </section>
        </div>
      )}
      <footer className="footer">{t('Weather & soil: Open-Meteo · Prices: AGMARKNET / data.gov.in · Guidance: ICAR Rabi Agro-Advisory 2021-22. Verify in your field or with your local KVK before acting.', 'मौसम व मिट्टी: Open-Meteo · भाव: AGMARKNET / data.gov.in · सलाह: ICAR रबी कृषि परामर्श 2021-22। कदम उठाने से पहले खेत में या स्थानीय KVK से पुष्टि करें।')}</footer>
    </main>
  );
}

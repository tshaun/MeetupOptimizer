import React, { useEffect, useMemo, useRef, useState } from "react";
import { MapContainer, TileLayer, Marker, Polyline, Popup } from "react-leaflet";
import "leaflet/dist/leaflet.css";

const SINGAPORE = [1.3521, 103.8198];

function useDebouncedValue(value, delay = 400) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return v;
}

export default function RoutePlanner() {
  // ------- STATE (unchanged logic) -------
  const [budget, setBudget] = useState(80);
  const [timeLimit, setTimeLimit] = useState(240);
  const [groupSize, setGroupSize] = useState(3);
  const [categories, setCategories] = useState([]);
  const [fairness, setFairness] = useState(0.5);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [refresh, setRefresh] = useState(0);

  const debounced = {
    budget: useDebouncedValue(budget),
    timeLimit: useDebouncedValue(timeLimit),
    groupSize: useDebouncedValue(groupSize),
    categories: useDebouncedValue(categories),
    fairness: useDebouncedValue(fairness),
    start: useDebouncedValue(start),
    end: useDebouncedValue(end),
  };

  const catList = ["cafe", "restaurant", "museum", "park", "thrift", "bar", "gallery"];
  const defaultPrefs = Object.fromEntries(catList.map(c => [c, 1.0]));
  const [prefsList, setPrefsList] = useState(() =>
    Array.from({ length: Math.max(1, groupSize) }, () => ({ ...defaultPrefs }))
  );

  useEffect(() => {
    setPrefsList(prev => {
      const n = Math.max(1, groupSize);
      if (prev.length === n) return prev;
      if (prev.length < n) {
        return [...prev, ...Array.from({ length: n - prev.length }, () => ({ ...defaultPrefs }))];
      }
      return prev.slice(0, n);
    });
  }, [groupSize]);

  const [selectedPerson, setSelectedPerson] = useState(0);
  useEffect(() => {
    setSelectedPerson(s => Math.min(s, Math.max(0, prefsList.length - 1)));
  }, [prefsList.length]);

  const [routeData, setRouteData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const qs = useMemo(() => {
    const p = new URLSearchParams();
    p.set("budget", String(debounced.budget));
    p.set("time_limit", String(debounced.timeLimit));
    p.set("group_size", String(debounced.groupSize));
    p.set("fairness", String(debounced.fairness));
    if (debounced.categories?.length) p.set("categories", debounced.categories.join(","));
    if (debounced.start) p.set("start", debounced.start);
    if (debounced.end) p.set("end", debounced.end);
    try { p.set("prefs", JSON.stringify(prefsList)); } catch {}
    if (refresh) p.set("refresh", String(refresh));
    return p.toString();
  }, [debounced, refresh, prefsList]);

  useEffect(() => {
    const url = new URL(window.location.href);
    url.search = qs;
    window.history.replaceState({}, "", url.toString());
  }, [qs]);

  useEffect(() => {
    let cancel = false;
    (async () => {
      setLoading(true);
      setErr("");
      try {
        const res = await fetch(`http://localhost:8000/plan?${qs}`);
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        if (!cancel) setRouteData(data);
      } catch (e) {
        if (!cancel) {
          setErr(e.message || "Failed to fetch route");
          setRouteData(null);
        }
      } finally {
        if (!cancel) setLoading(false);
      }
    })();
    return () => { cancel = true; };
  }, [qs]);

  // ------- INGESTION -------
  const [uploadBusy, setUploadBusy] = useState(false);
  const [taskId, setTaskId] = useState(null);
  const pollRef = useRef(null);

  async function handleUpload(file) {
    setUploadBusy(true);
    setErr("");
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("http://localhost:8000/venues/upload", { method: "POST", body: form });
      if (!res.ok) throw new Error(await res.text());
      const { task_id } = await res.json();
      setTaskId(task_id);

      pollRef.current = setInterval(async () => {
        const s = await fetch(`http://localhost:8000/venues/status/${task_id}`);
        if (!s.ok) return;
        const j = await s.json();
        if (j.status === "ready" || j.status === "error") {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setUploadBusy(false);
          if (j.status === "error") {
            setErr(j.message || "Ingestion failed");
            setRouteData(null);
          } else {
            setRefresh(v => v + 1);
            setTaskId(null);
          }
        }
      }, 1000);
    } catch (e) {
      setUploadBusy(false);
      setErr(e.message || "Upload failed");
    }
  }

  // ------- STYLES (self-contained) -------
  const styles = `
  :root{
    --bg:#0b1220; --panel:#0f172a; --panel2:#0c1426;
    --text:#e5e7eb; --muted:#9aa3b2; --border:#1f2a44; --blue:#3b82f6; --green:#22c55e; --red:#ef4444;
  }
  .rp-root{position:fixed; inset:0; background:var(--bg); color:var(--text); font:14px/1.35 system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;}
  .topbar{height:48px; display:flex; align-items:center; justify-content:space-between; padding:0 16px; border-bottom:1px solid var(--border); background:rgba(11,18,32,.9); backdrop-filter:saturate(140%) blur(6px);}
  .main{height:calc(100vh - 48px); display:grid; grid-template-columns:360px 1fr;}
  .side{border-right:1px solid var(--border); height:100%; overflow:auto; padding:16px; gap:16px; display:flex; flex-direction:column;}
  .panel{background:linear-gradient(180deg, var(--panel), var(--panel2)); border:1px solid var(--border); border-radius:14px; padding:16px; box-shadow:0 6px 20px rgba(0,0,0,.25);}
  .h2{font-weight:600; margin:0 0 8px;}
  .labelrow{display:flex; justify-content:space-between; align-items:center; gap:8px; color:var(--muted);}
  .input{width:100%; background:#0a1326; color:var(--text); border:1px solid var(--border); border-radius:10px; padding:8px 10px; outline:none}
  .input:focus{border-color:var(--blue); box-shadow:0 0 0 2px rgba(59,130,246,.35)}
  .range{width:100%;}
  .chips{display:flex; flex-wrap:wrap; gap:8px}
  .chip{font-size:12px; padding:6px 10px; border-radius:999px; border:1px solid var(--border); background:#0a1326; color:#c9d2e3; cursor:pointer}
  .chip.on{background:rgba(59,130,246,.12); border-color:#2866c9; color:#e6eeff}
  .tabs{display:flex; gap:6px; overflow:auto; padding-bottom:4px}
  .tab{font-size:12px; padding:6px 10px; border-radius:8px; border:1px solid var(--border); background:#0a1326; cursor:pointer}
  .tab.on{background:#2866c9; border-color:#2866c9; color:white}
  .btn{font-size:12px; padding:6px 10px; border-radius:8px; border:1px solid var(--border); background:#0a1326; color:#c9d2e3; cursor:pointer}
  .btn:hover{border-color:#3d4a6b}
  .right{height:100%; padding:16px; overflow:hidden}
  .vsplit{height:100%; display:grid; grid-template-rows:minmax(260px,1fr) minmax(220px,40%); gap:16px}
  .card{background:linear-gradient(180deg, var(--panel), var(--panel2)); border:1px solid var(--border); border-radius:14px; overflow:hidden; box-shadow:0 6px 20px rgba(0,0,0,.25);}
  .card-head{display:flex; justify-content:space-between; align-items:center; padding:10px 14px; border-bottom:1px solid var(--border)}
  .card-body{height:calc(100% - 41px); position:relative}
  .results{height:calc(100% - 0px); padding:12px 14px; overflow:auto}
  .muted{color:var(--muted)}
  .kpi{display:flex; gap:12px; font-size:12px; color:var(--muted)}
  .kpi b{color:var(--text)}
  .list{list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:8px}
  .row{display:flex; gap:8px}
  .bullet{color:var(--muted); margin-top:2px}
  /* upload pill */
  .upload{display:flex; align-items:center; gap:8px; padding:8px 12px; border-radius:12px; border:1px solid var(--border); background:#0a1326; cursor:pointer; font-size:12px}
  .upload:hover{border-color:#3d4a6b}
  `;

  // ------- RENDER -------
  return (
    <div className="rp-root">
      <style>{styles}</style>

      {/* Top bar */}
      <div className="topbar">
        <div style={{display:"flex", alignItems:"center", gap:8}}>
          <span role="img" aria-label="map">🗺️</span>
          <strong>Smart Route Planner</strong>
          {loading && <span className="muted" style={{marginLeft:8}}>Recomputing…</span>}
          {taskId && (
            <span className="muted" style={{marginLeft:8, fontSize:12}}>
              Ingestion: {taskId}
            </span>
          )}
        </div>

        <label className="upload">
          <span>📂</span>
          <span>{uploadBusy ? "Ingesting venues…" : "Upload venues (CSV/JSON)"}</span>
          <input
            type="file"
            accept=".csv,application/json,text/csv,application/vnd.ms-excel"
            style={{display:"none"}}
            disabled={uploadBusy}
            onChange={e => e.target.files?.[0] && handleUpload(e.target.files[0])}
          />
        </label>
      </div>

      {/* Main: left controls | right content */}
      <div className="main">
        {/* LEFT SIDEBAR */}
        <aside className="side">
          <section className="panel">
            <h2 className="h2">Constraints</h2>
            <div style={{display:"grid", gap:12}}>
              <div>
                <div className="labelrow"><span>Budget per person ($)</span><span className="muted">{budget}</span></div>
                <input className="input" type="number" value={budget} min={0} onChange={e=>setBudget(Number(e.target.value))}/>
              </div>
              <div>
                <div className="labelrow"><span>Time limit (minutes)</span><span className="muted">{timeLimit}</span></div>
                <input className="input" type="number" value={timeLimit} min={0} onChange={e=>setTimeLimit(Number(e.target.value))}/>
              </div>
              <div>
                <div className="labelrow"><span>Group size</span><span className="muted">{groupSize}</span></div>
                <input className="input" type="number" value={groupSize} min={1} onChange={e=>setGroupSize(Number(e.target.value))}/>
              </div>
              <div>
                <div className="labelrow"><span>Fairness vs total fun</span><span className="muted">{fairness.toFixed(2)}</span></div>
                <input className="range" type="range" min="0" max="1" step="0.05" value={fairness} onChange={e=>setFairness(Number(e.target.value))}/>
                <div className="labelrow" style={{fontSize:12}}>
                  <span>Max total utility</span><span>Balance across friends</span>
                </div>
              </div>
              <div>
                <div className="labelrow" style={{justifyContent:"flex-start"}}><span>Start (lat, lon)</span></div>
                <input className="input" placeholder="1.3000,103.8000" value={start} onChange={e=>setStart(e.target.value)}/>
              </div>
              <div>
                <div className="labelrow" style={{justifyContent:"flex-start"}}><span>End (lat, lon)</span></div>
                <input className="input" placeholder="(optional)" value={end} onChange={e=>setEnd(e.target.value)}/>
              </div>
            </div>
          </section>

          <section className="panel">
            <h2 className="h2">Categories</h2>
            <div className="chips" style={{marginBottom:12}}>
              {catList.map(c=>{
                const on = categories.includes(c);
                return (
                  <button key={c}
                          className={`chip ${on ? "on":""}`}
                          onClick={()=>setCategories(prev => on ? prev.filter(x=>x!==c) : [...prev, c])}>
                    {c}
                  </button>
                );
              })}
            </div>

            <div style={{borderTop:`1px solid var(--border)`, paddingTop:12, display:"grid", gap:12}}>
              <div className="labelrow"><strong>Preferences</strong><span className="muted">Per-person weights</span></div>
              <div className="tabs">
                {prefsList.map((_, idx)=>(
                  <button key={idx} className={`tab ${selectedPerson===idx?"on":""}`} onClick={()=>setSelectedPerson(idx)}>
                    P{idx+1}
                  </button>
                ))}
              </div>

              <div style={{border:`1px solid var(--border)`, borderRadius:12, padding:12, background:"#0a1326"}}>
                <div style={{maxHeight:220, overflow:"auto", display:"grid", gap:8}}>
                  {catList.map(c=>(
                    <div key={c} className="row">
                      <div style={{width:90}} className="muted">{c}</div>
                      <input className="range" type="range" min="0" max="3" step="0.1"
                        value={prefsList[selectedPerson]?.[c] ?? 1.0}
                        onChange={e=>{
                          setPrefsList(prev=>{
                            const copy = prev.map(x=>({...x}));
                            copy[selectedPerson] = {...copy[selectedPerson], [c]: parseFloat(e.target.value)};
                            return copy;
                          });
                        }}/>
                      <div style={{width:36, textAlign:"right"}} className="muted">
                        {(prefsList[selectedPerson]?.[c] ?? 1.0).toFixed(1)}
                      </div>
                    </div>
                  ))}
                </div>

                <div style={{display:"flex", gap:8, marginTop:12}}>
                  <button className="btn" onClick={()=>setPrefsList(prev=>prev.map((p,i)=>i===selectedPerson?{...defaultPrefs}:p))}>Reset person</button>
                  <button className="btn" onClick={()=>setPrefsList(prev=>prev.map(()=>({...prefsList[selectedPerson]})))}>Copy to all</button>
                  <button className="btn" onClick={()=>setPrefsList(Array.from({length:Math.max(1,groupSize)},()=>({...defaultPrefs})))}>Reset all</button>
                </div>
              </div>
            </div>
          </section>
        </aside>

        {/* RIGHT PANE */}
        <div className="right">
          <div className="vsplit">
            {/* MAP */}
            <section className="card">
              <div className="card-head">
                <div><strong>Map</strong></div>
                {loading && <div className="muted" style={{fontSize:12}}>Recomputing…</div>}
              </div>
              <div className="card-body">
                {loading && (
                  <div style={{position:"absolute", inset:0, background:"rgba(11,18,32,.6)", display:"flex", alignItems:"center", justifyContent:"center", fontSize:12}} className="muted">
                    Recomputing route…
                  </div>
                )}
                <MapContainer center={SINGAPORE} zoom={12} style={{ height: "100%", width: "100%" }}>
                  <TileLayer
                    attribution="&copy; OpenStreetMap contributors"
                    url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                  />
                  {routeData && routeData.route?.map((v, idx) => (
                    <Marker key={v.id ?? `${v.lat},${v.lon},${idx}`} position={[v.lat, v.lon]}>
                      <Popup>
                        <div style={{fontSize:12}}>
                          <b>{idx + 1}. {v.name}</b><br />
                          {v.category ?? "—"}, ${v.cost_estimate_per_person ?? "?"}<br />
                          {v.service_time_min ?? "?"} min
                        </div>
                      </Popup>
                    </Marker>
                  ))}
                  {routeData && routeData.route?.length > 1 && (
                    <Polyline positions={routeData.route.map(v => [v.lat, v.lon])} weight={3} opacity={0.9} />
                  )}
                </MapContainer>
              </div>
            </section>

            {/* RESULTS */}
            <section className="card">
              <div className="card-head">
                <strong>Suggested route</strong>
                <div className="kpi">
                  <span>⏱ <b>{routeData?.total_time ?? "—"}</b> min</span>
                  <span>💸 <b>${routeData?.total_cost ?? "—"}</b></span>
                  <span>🛑 <b>{routeData?.route?.length ?? 0}</b></span>
                </div>
              </div>
              <div className="results">
                {err && <div style={{color:"var(--red)", marginBottom:8, fontSize:12}}>{err}</div>}
                {routeData && routeData.route?.length ? (
                  <ul className="list">
                    {routeData.route.map((v, i) => (
                      <li key={v.id ?? `${v.lat},${v.lon},${i}`} className="row">
                        <div className="bullet">{i + 1}.</div>
                        <div>
                          <div style={{fontWeight:600}}>{v.name}</div>
                          <div className="muted" style={{fontSize:12}}>
                            {v.category} · {v.service_time_min} min
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="muted">{
                    loading ? "Computing route…" : "No route found. Adjust constraints or categories."
                  }</div>
                )}
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}

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
  // Query params (drive everything)
  const [budget, setBudget] = useState(80);
  const [timeLimit, setTimeLimit] = useState(240);
  const [groupSize, setGroupSize] = useState(3);
  const [categories, setCategories] = useState(["cafe", "museum", "park"]);
  const [fairness, setFairness] = useState(0.5); // 0 maximize sum, 1 maximize balance
  const [start, setStart] = useState("");        // optional "lat,lon"
  const [end, setEnd] = useState("");            // optional "lat,lon"

  const debounced = {
    budget: useDebouncedValue(budget),
    timeLimit: useDebouncedValue(timeLimit),
    groupSize: useDebouncedValue(groupSize),
    categories: useDebouncedValue(categories),
    fairness: useDebouncedValue(fairness),
    start: useDebouncedValue(start),
    end: useDebouncedValue(end),
  };

  const [routeData, setRouteData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  // Build query string from state
  const qs = useMemo(() => {
    const p = new URLSearchParams();
    p.set("budget", String(debounced.budget));
    p.set("time_limit", String(debounced.timeLimit));
    p.set("group_size", String(debounced.groupSize));
    p.set("fairness", String(debounced.fairness));
    if (debounced.categories?.length) p.set("categories", debounced.categories.join(","));
    if (debounced.start) p.set("start", debounced.start);
    if (debounced.end) p.set("end", debounced.end);
    return p.toString();
  }, [debounced]);

  // Keep URL in sync for shareability
  useEffect(() => {
    const url = new URL(window.location.href);
    url.search = qs;
    window.history.replaceState({}, "", url.toString());
  }, [qs]);

  // Fetch on change
  useEffect(() => {
    let cancel = false;
    async function run() {
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
    }
    run();
    return () => { cancel = true; };
  }, [qs]);

  // Simple ingestion flow
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

      // Poll status
      pollRef.current = setInterval(async () => {
        const s = await fetch(`http://localhost:8000/venues/status/${task_id}`);
        if (!s.ok) return; // keep polling
        const j = await s.json();
        if (j.status === "ready" || j.status === "error") {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setUploadBusy(false);
          if (j.status === "error") setErr(j.message || "Ingestion failed");
          // Trigger fresh route after data refresh
          // (our effect on qs will refetch automatically)
        }
      }, 1000);
    } catch (e) {
      setUploadBusy(false);
      setErr(e.message || "Upload failed");
    }
  }

  // UI helpers
  const catList = ["cafe", "restaurant", "museum", "park", "thrift", "bar", "gallery"];

  return (
    <div className="p-4 space-y-4">
      <header className="flex flex-wrap items-end gap-3 justify-between">
        <h1 className="text-2xl font-bold">🗺️ Smart Route Planner</h1>

        <label className="text-sm bg-gray-100 px-3 py-2 rounded-lg cursor-pointer">
          {uploadBusy ? "Ingesting…" : "Upload venues (CSV/JSON)"}
          <input
            type="file"
            accept=".csv,application/json,text/csv,application/vnd.ms-excel"
            className="hidden"
            disabled={uploadBusy}
            onChange={e => e.target.files?.[0] && handleUpload(e.target.files[0])}
          />
        </label>
      </header>

      <section className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="bg-white p-3 rounded-xl shadow space-y-3">
          <h2 className="font-semibold">Constraints</h2>
          <div className="flex items-center justify-between">
            <label className="text-sm">Budget</label>
            <input type="number" className="border rounded px-2 py-1 w-24"
              value={budget} onChange={e => setBudget(Number(e.target.value))}/>
          </div>
          <div className="flex items-center justify-between">
            <label className="text-sm">Time limit (min)</label>
            <input type="number" className="border rounded px-2 py-1 w-24"
              value={timeLimit} onChange={e => setTimeLimit(Number(e.target.value))}/>
          </div>
          <div className="flex items-center justify-between">
            <label className="text-sm">Group size</label>
            <input type="number" className="border rounded px-2 py-1 w-20"
              value={groupSize} onChange={e => setGroupSize(Number(e.target.value))}/>
          </div>
          <div>
            <label className="text-sm">Fairness</label>
            <input type="range" min="0" max="1" step="0.05" value={fairness}
              onChange={e => setFairness(Number(e.target.value))} className="w-full"/>
            <div className="text-xs text-gray-600 flex justify-between">
              <span>Max total utility</span><span>Balance across friends</span>
            </div>
          </div>
          <div className="space-y-1">
            <label className="text-sm">Start (lat,lon)</label>
            <input className="border rounded px-2 py-1 w-full" placeholder="1.3000,103.8000"
              value={start} onChange={e => setStart(e.target.value)}/>
          </div>
          <div className="space-y-1">
            <label className="text-sm">End (lat,lon)</label>
            <input className="border rounded px-2 py-1 w-full" placeholder=""
              value={end} onChange={e => setEnd(e.target.value)}/>
          </div>
        </div>

        <div className="bg-white p-3 rounded-xl shadow space-y-2 md:col-span-2">
          <h2 className="font-semibold">Categories</h2>
          <div className="flex flex-wrap gap-2">
            {catList.map(c => {
              const active = categories.includes(c);
              return (
                <button key={c}
                  onClick={() =>
                    setCategories(prev => active ? prev.filter(x => x !== c) : [...prev, c])
                  }
                  className={`px-3 py-1 rounded-full border text-sm ${active ? "bg-blue-600 text-white border-blue-600" : "bg-white"}`}>
                  {c}
                </button>
              );
            })}
          </div>
        </div>
      </section>

      <div className="h-[560px] w-full rounded-xl overflow-hidden shadow relative">
        {loading && <div className="absolute inset-0 bg-white/50 backdrop-blur-sm flex items-center justify-center text-sm">Recomputing route…</div>}
        <MapContainer center={SINGAPORE} zoom={12} style={{ height: "100%", width: "100%" }}>
          <TileLayer
            attribution='&copy; OpenStreetMap contributors'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          {routeData && routeData.route?.map((v, idx) => (
            <Marker key={v.id ?? `${v.lat},${v.lon},${idx}`} position={[v.lat, v.lon]}>
              <Popup>
                <div className="text-sm">
                  <b>{idx + 1}. {v.name}</b><br />
                  {v.category ?? "—"}, ${v.cost_estimate_per_person ?? "?"}<br />
                  {v.service_time_min ?? "?"} min
                </div>
              </Popup>
            </Marker>
          ))}
          {routeData && routeData.route?.length > 1 && (
            <Polyline
              positions={routeData.route.map(v => [v.lat, v.lon])}
              weight={3}
              opacity={0.8}
            />
          )}
        </MapContainer>
      </div>

      <div className="bg-gray-50 p-4 rounded-xl shadow">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Suggested Route</h2>
          {taskId && <span className="text-xs px-2 py-1 rounded bg-gray-200">Ingestion task: {taskId}</span>}
        </div>
        {err && <div className="text-sm text-red-600 mt-1">{err}</div>}
        {routeData ? (
          <>
            <ul className="space-y-1 text-sm mt-2">
              {routeData.route?.map((v, i) => (
                <li key={v.id ?? `${v.lat},${v.lon},${i}`}>
                  {i + 1}. {v.name} ({v.category}, {v.service_time_min} min)
                </li>
              ))}
            </ul>
            <div className="mt-2 text-sm text-gray-700">
              Total Time: {routeData.total_time} min · Total Cost: ${routeData.total_cost}
            </div>
          </>
        ) : (
          <div className="text-sm text-gray-500">No route yet. Tweak the filters.</div>
        )}
      </div>
    </div>
  );
}

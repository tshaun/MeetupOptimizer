from fastapi import APIRouter, UploadFile, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Dict, List, Any
from pathlib import Path
import csv, json, uuid, io

from backend.utils.matrix import compute_matrix_minutes
from backend.routers import route_router  # for cache invalidation

router = APIRouter(prefix="/venues", tags=["venues"])

TASKS: Dict[str, Dict] = {}  # {task_id: {"status": "pending|ready|error", "message": ""}}

class Venue(BaseModel):
  id: str
  name: str
  lat: float
  lon: float
  category: str
  cost_estimate_per_person: float = 0
  service_time_min: int = 60
  opening_hours: Dict[str, str] | None = None

def _coerce_float(v: Any) -> float:
  if v is None or v == "":
    raise ValueError("missing float value")
  try:
    return float(v)
  except Exception:
    raise ValueError(f"invalid float: {v!r}")

def _coerce_int(v: Any) -> int:
  if v is None or v == "":
    raise ValueError("missing int value")
  try:
    return int(float(v))
  except Exception:
    raise ValueError(f"invalid int: {v!r}")

def _normalize_venue_dict(item: Dict[str, Any]) -> Dict[str, Any]:
  """Map common schema variants to our expected fields and coerce types.
  Accepts keys like lng/longitude -> lon, latitude -> lat, service_time -> service_time_min,
  cost/cost_per_person/price -> cost_estimate_per_person. Auto-generate id if missing.
  """
  d = {k.strip(): v for k, v in item.items()} if isinstance(item, dict) else {}
  # Key synonyms
  if "lon" not in d:
    for k in ("lng", "longitude", "long"):
      if k in d:
        d["lon"] = d[k]
        break
  if "lat" not in d and "latitude" in d:
    d["lat"] = d["latitude"]
  if "service_time_min" not in d:
    for k in ("service_time", "duration_min"):
      if k in d:
        d["service_time_min"] = d[k]
        break
  if "cost_estimate_per_person" not in d:
    for k in ("cost", "price", "cost_per_person"):
      if k in d:
        d["cost_estimate_per_person"] = d[k]
        break
  if "category" not in d:
    for k in ("type", "tag"):
      if k in d:
        d["category"] = d[k]
        break

  # Required fields
  if "name" not in d:
    raise ValueError("venue is missing required 'name'")
  if "lat" not in d or "lon" not in d:
    raise ValueError("venue is missing required 'lat'/'lon'")

  # Type coercion
  d.setdefault("id", str(uuid.uuid4()))
  d["lat"] = _coerce_float(d["lat"])
  d["lon"] = _coerce_float(d["lon"])
  d["category"] = str(d.get("category", "unknown"))
  if "cost_estimate_per_person" in d:
    d["cost_estimate_per_person"] = _coerce_float(d["cost_estimate_per_person"])
  else:
    d["cost_estimate_per_person"] = 0.0
  if "service_time_min" in d:
    d["service_time_min"] = _coerce_int(d["service_time_min"])
  else:
    d["service_time_min"] = 60
  return d

def parse_file_bytes(data: bytes, filename: str):
  text = data.decode("utf-8", errors="ignore")
  if filename.lower().endswith(".json"):
    payload = json.loads(text)
    items: List[Dict[str, Any]]
    if isinstance(payload, dict) and "venues" in payload:
      items = payload["venues"]
    else:
      items = payload
    if not isinstance(items, list):
      raise ValueError("expected a JSON array of venues or an object with 'venues' array")
    norm = [_normalize_venue_dict(obj) for obj in items]
    return [Venue(**obj) for obj in norm]
  # CSV fallback
  reader = csv.DictReader(io.StringIO(text))
  out: List[Venue] = []
  for row in reader:
    # Normalize CSV keys (allow lng/longitude, service_time, etc.)
    r = {k.strip(): v for k, v in row.items()}
    if "lon" not in r:
      for k in ("lng", "longitude", "long"):
        if k in r:
          r["lon"] = r[k]
          break
    if "service_time_min" not in r and "service_time" in r:
      r["service_time_min"] = r["service_time"]
    if "cost_estimate_per_person" not in r:
      for k in ("cost", "price", "cost_per_person"):
        if k in r:
          r["cost_estimate_per_person"] = r[k]
          break

    out.append(Venue(
      id=r.get("id") or str(uuid.uuid4()),
      name=r["name"],
      lat=_coerce_float(r.get("lat")),
      lon=_coerce_float(r.get("lon")),
      category=str(r.get("category", "unknown")),
      cost_estimate_per_person=_coerce_float(r.get("cost_estimate_per_person", 0)),
      service_time_min=_coerce_int(r.get("service_time_min", 60)),
    ))
  return out

def _data_root() -> Path:
  # project root / src (to align with route_router's current loader)
  return Path(__file__).resolve().parents[2] / "src"

def upsert_to_db(venues: List[Venue]):
  # For now, persist to JSON files the planner reads.
  root = _data_root()
  root.mkdir(parents=True, exist_ok=True)

  # Write venues.json
  venues_payload = [v.model_dump() for v in venues]
  (root / "venues.json").write_text(json.dumps(venues_payload, indent=2))

  # Regenerate travel_matrix.json using a simple haversine-based estimate.
  matrix_min = compute_matrix_minutes(venues_payload)
  ids = [v.get("id") for v in venues_payload]
  tm = {"matrix_min": matrix_min, "ids": ids}
  (root / "travel_matrix.json").write_text(json.dumps(tm, indent=2))

def ingest_task(task_id: str, blob: bytes, filename: str):
  try:
    TASKS[task_id]["status"] = "processing"
    venues = parse_file_bytes(blob, filename)
    upsert_to_db(venues)
    # Invalidate planner cache so new data is picked up on next /plan request
    try:
      route_router.clear_cache()
    except Exception:
      pass
    TASKS[task_id]["status"] = "ready"
  except Exception as e:
    TASKS[task_id]["status"] = "error"
    TASKS[task_id]["message"] = str(e)

@router.post("/upload")
async def upload(file: UploadFile, bg: BackgroundTasks):
  task_id = str(uuid.uuid4())[:8]
  TASKS[task_id] = {"status": "pending", "message": ""}
  blob = await file.read()
  bg.add_task(ingest_task, task_id, blob, file.filename)
  return {"task_id": task_id}

@router.get("/status/{task_id}")
def status(task_id: str):
  t = TASKS.get(task_id)
  if not t:
    raise HTTPException(404, "task not found")
  return t

# venues_router.py
from fastapi import APIRouter, UploadFile, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Dict
import csv, json, uuid, io

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

def parse_file_bytes(data: bytes, filename: str):
  text = data.decode("utf-8", errors="ignore")
  if filename.lower().endswith(".json"):
    items = json.loads(text)
    return [Venue(**item) for item in items]
  # CSV fallback
  reader = csv.DictReader(io.StringIO(text))
  out = []
  for row in reader:
    out.append(Venue(
      id=row.get("id") or str(uuid.uuid4()),
      name=row["name"],
      lat=float(row["lat"]),
      lon=float(row["lon"]),
      category=row.get("category","unknown"),
      cost_estimate_per_person=float(row.get("cost_estimate_per_person", 0)),
      service_time_min=int(row.get("service_time_min", 60)),
    ))
  return out

def upsert_to_db(venues: list[Venue]):
  # TODO: upsert into your DB
  # For demo, write to a local JSON cache or call your repo layer
  pass

def ingest_task(task_id: str, blob: bytes, filename: str):
  try:
    TASKS[task_id]["status"] = "processing"
    venues = parse_file_bytes(blob, filename)
    upsert_to_db(venues)
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

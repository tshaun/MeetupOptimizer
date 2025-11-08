from fastapi import APIRouter, HTTPException, Query
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
import json
import math
import time

# Your solver
from backend.planner import plan_route  # updated import

router = APIRouter(prefix="/plan", tags=["planner"])

# -----------------------------
# Simple file cache to avoid re-reading on every request
# -----------------------------
_DATA_CACHE: Dict[str, Any] = {
    "venues": None,
    "tm": None,
    "venues_mtime": 0.0,
    "tm_mtime": 0.0,
    # semantic embeddings and model cache
    "embeddings": None,    # list[list[float]] aligned with full venues
    "emb_mtime": 0.0,      # mirrors venues_mtime used for embeddings
    "st_model": None,      # SentenceTransformer instance
}

def clear_cache() -> None:
    """Clear cached data so subsequent requests reload updated files."""
    _DATA_CACHE["venues"] = None
    _DATA_CACHE["tm"] = None
    _DATA_CACHE["embeddings"] = None
    _DATA_CACHE["st_model"] = None

def _data_root() -> Path:
    # when this module is in backend/routers, parents[2] points to project root
    return Path(__file__).resolve().parents[2] / "src"

def _load_json_cached(path: Path, cache_key: str, mtime_key: str):
    st = path.stat()
    if _DATA_CACHE.get(cache_key) is not None and _DATA_CACHE.get(mtime_key) == st.st_mtime:
        return _DATA_CACHE[cache_key]
    data = json.loads(path.read_text())
    _DATA_CACHE[cache_key] = data
    _DATA_CACHE[mtime_key] = st.st_mtime
    return data

def _load_base_data() -> Tuple[List[dict], List[List[float]]]:
    root = _data_root()
    venues = _load_json_cached(root / "venues.json", "venues", "venues_mtime")
    tm_raw = _load_json_cached(root / "travel_matrix.json", "tm", "tm_mtime")
    tm = tm_raw["matrix_min"]
    if len(tm) != len(venues):
        raise RuntimeError("travel_matrix.json size does not match venues.json")
    return venues, tm

# -----------------------------
# Embeddings (SentenceTransformer)
# -----------------------------
def _get_st_model():
    # lazy import and instantiate model, cache it
    if _DATA_CACHE.get("st_model") is not None:
        return _DATA_CACHE["st_model"]
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        _DATA_CACHE["st_model"] = model
        return model
    except Exception:
        _DATA_CACHE["st_model"] = None
        return None

def _venue_texts(venues: List[dict]) -> List[str]:
    texts: List[str] = []
    for v in venues:
        name = str(v.get("name", "")).strip()
        cat = str(v.get("category", "")).strip()
        # Build compact text combining key fields
        if cat and name:
            texts.append(f"{name} | {cat}")
        elif name:
            texts.append(name)
        else:
            texts.append(cat or "")
    return texts

def _compute_embeddings_for_full_venues(full_venues: List[dict]) -> Optional[List[List[float]]]:
    model = _get_st_model()
    if model is None:
        return None
    try:
        texts = _venue_texts(full_venues)
        # encode returns numpy array if convert_to_numpy=True; we convert to Python lists for JSON-ability
        embs = model.encode(texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
        # convert to nested lists
        return [list(map(float, row)) for row in embs]
    except Exception:
        return None

def _ensure_embeddings_up_to_date(full_venues: List[dict]) -> Optional[List[List[float]]]:
    # If we already have embeddings and the venues timestamp hasn't changed, reuse
    if (
        _DATA_CACHE.get("embeddings") is not None
        and _DATA_CACHE.get("emb_mtime") == _DATA_CACHE.get("venues_mtime")
        and isinstance(_DATA_CACHE.get("embeddings"), list)
        and len(_DATA_CACHE["embeddings"]) == len(full_venues)
    ):
        return _DATA_CACHE["embeddings"]
    # Otherwise recompute
    embs = _compute_embeddings_for_full_venues(full_venues)
    if embs is not None and len(embs) == len(full_venues):
        _DATA_CACHE["embeddings"] = embs
        _DATA_CACHE["emb_mtime"] = _DATA_CACHE.get("venues_mtime", 0.0)
        return embs
    # fallback: no embeddings
    _DATA_CACHE["embeddings"] = None
    return None

# -----------------------------
# Helpers
# -----------------------------
def _parse_categories(categories: Optional[str]) -> Optional[List[str]]:
    if not categories:
        return None
    # Normalize to lowercase for robust matching against venue categories
    cats = [c.strip().lower() for c in categories.split(",") if c.strip()]
    return cats or None

def _slice_by_categories(venues: List[dict], tm: List[List[float]], cats: Optional[List[str]]) -> Tuple[List[dict], List[List[float]], List[int]]:
    if not cats:
        # no filtering
        return venues, tm, list(range(len(venues)))
    keep_idx = []
    for i, v in enumerate(venues):
        cat_val = str(v.get("category", "")).strip().lower()
        if cat_val in cats:
            keep_idx.append(i)
    if not keep_idx:
        return [], [[]], []
    # slice matrix
    sliced = [[tm[i][j] for j in keep_idx] for i in keep_idx]
    sliced_venues = [venues[i] for i in keep_idx]
    return sliced_venues, sliced, keep_idx

def _slice_embeddings(embeddings: Optional[List[List[float]]], keep_idx: Optional[List[int]]) -> Optional[List[List[float]]]:
    if embeddings is None:
        return None
    if not keep_idx:
        return embeddings
    return [embeddings[i] for i in keep_idx]

def _parse_latlon(s: Optional[str]) -> Optional[Tuple[float, float]]:
    if not s:
        return None
    try:
        lat_s, lon_s = s.split(",", 1)
        return float(lat_s.strip()), float(lon_s.strip())
    except Exception:
        return None

def _is_int_token(t: str) -> bool:
    try:
        int(t)
        return True
    except Exception:
        return False

def _resolve_order(
    order: str,
    venues_filtered: List[dict],
    filtered_to_original: List[int],
) -> List[int]:
    """
    Accepts comma-separated tokens. Each token can be:
      - an integer index into the FILTERED list, or
      - a venue 'id', or
      - a venue 'name' (last resort, case-sensitive)
    Returns a list of ORIGINAL indices (against the full venues array).
    """
    tokens = [t.strip() for t in order.split(",") if t.strip()]
    if not tokens:
        return []
    # quick maps
    id_to_fidx = {}
    name_to_fidx = {}
    for fidx, v in enumerate(venues_filtered):
        vid = v.get("id")
        if isinstance(vid, str):
            id_to_fidx.setdefault(vid, fidx)
        name = v.get("name")
        if isinstance(name, str):
            name_to_fidx.setdefault(name, fidx)

    out_orig_idx: List[int] = []
    for t in tokens:
        fidx = None
        if _is_int_token(t):
            i = int(t)
            if 0 <= i < len(venues_filtered):
                fidx = i
        if fidx is None and t in id_to_fidx:
            fidx = id_to_fidx[t]
        if fidx is None and t in name_to_fidx:
            fidx = name_to_fidx[t]
        if fidx is None:
            # skip unknown token
            continue
        out_orig_idx.append(filtered_to_original[fidx])
    # remove duplicates while preserving order
    seen = set()
    dedup = []
    for i in out_orig_idx:
        if i not in seen:
            seen.add(i)
            dedup.append(i)
    return dedup

def _compute_totals(route_indices: List[int], venues: List[dict], tm: List[List[float]], group_size: int) -> Tuple[int, float]:
    """
    Returns (total_time_min, total_cost)
    time = sum(service_time_min) + sum(travel legs)
    cost = sum(cost_estimate_per_person) * group_size
    """
    if not route_indices:
        return 0, 0.0
    # service time + costs
    total_service = 0
    total_cost_pp = 0.0
    for i in route_indices:
        v = venues[i]
        total_service += int(v.get("service_time_min", 60) or 0)
        total_cost_pp += float(v.get("cost_estimate_per_person", 0.0) or 0.0)
    # travel time between consecutive stops
    travel = 0.0
    for a, b in zip(route_indices, route_indices[1:]):
        travel += float(tm[a][b] if (0 <= a < len(tm) and 0 <= b < len(tm)) else 0.0)
    total_time = int(round(total_service + travel))
    total_cost = round(total_cost_pp * max(group_size, 1), 2)
    return total_time, total_cost

# -----------------------------
# Route endpoint
# -----------------------------
@router.get("")
def get_plan(
    budget: int = Query(90, ge=0, description="Budget per person unless you already pre-multiply"),
    time_limit: int = Query(360, ge=0, description="Total time limit in minutes"),
    group_size: int = Query(1, ge=1),
    categories: Optional[str] = Query(None, description="Comma-separated categories to include"),
    fairness: float = Query(0.0, ge=0.0, le=1.0, description="0 = maximize sum, 1 = balance; passed through for now"),
    start: Optional[str] = Query(None, description="Optional 'lat,lon' start (not used by solver)"),
    end: Optional[str] = Query(None, description="Optional 'lat,lon' end (not used by solver)"),
    lock_order: bool = Query(False, description="If true and 'order' provided, respect that order instead of solving"),
    order: Optional[str] = Query(None, description="Comma-separated indices/ids/names when lock_order=true"),
    prefs: Optional[str] = Query(None, description="Optional JSON-encoded category weights, e.g. '{\"cafe\":1.5}'"),
    enable_embeddings: bool = Query(True, description="Use semantic diversity model if available"),
    sim_threshold: float = Query(0.8, ge=-1.0, le=1.0, description="Cosine similarity threshold for semantic penalty"),
    semantic_penalty: float = Query(0.3, ge=0.0, le=1.0, description="Multiplier applied when similarity exceeds threshold"),
    fairness_profile: Optional[str] = Query(None, description="Optional preset: 'balanced' or 'strict' (overrides slider)"),
    meet_date: Optional[str] = Query(None, description="Meetup date YYYY-MM-DD"),
    meet_time: Optional[str] = Query(None, description="Meetup start time HH:MM (24h)"),
    meet_tz: Optional[str] = Query("+08:00", description="Timezone offset like +08:00; default Asia/Singapore"),
    # Default inflated walking multiplier to counter optimistic straight-line estimates.
    walk_multiplier: float = Query(1.6, ge=0.25, le=5.0, description="Scale all travel minutes (default 1.6 to deflate optimistic walking times)"),
    transfer_buffer_min: float = Query(0.0, ge=0.0, le=15.0, description="Optional per-leg buffer minutes added before rounding"),
    travel_rounding: str = Query("ceil", description="ceil | round | none for schedule/feasibility travel rounding")
):
    try:
        full_venues, full_tm = _load_base_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load base data: {e}")

    cats = _parse_categories(categories)
    venues_flt, tm_flt, keep_map = _slice_by_categories(full_venues, full_tm, cats)

    # Prepare embeddings aligned to full venues (then slice if filtered)
    embeddings_full: Optional[List[List[float]]] = None
    if enable_embeddings:
        embeddings_full = _ensure_embeddings_up_to_date(full_venues)

    if cats and not keep_map:
        # no venues left after filtering
        return {
            "route": [],
            "route_indices": [],
            "total_time": 0,
            "total_cost": 0.0,
            "filters": {"categories": cats},
            "meta": {
                "group_size": group_size,
                "fairness": fairness,
                "start": _parse_latlon(start),
                "end": _parse_latlon(end),
                "source": "empty_after_filter",
            },
        }

    # If user wants to lock the order, bypass solver and compute totals
    if lock_order and order:
        orig_indices = _resolve_order(order, venues_flt or full_venues, keep_map or list(range(len(full_venues))))
        if not orig_indices:
            raise HTTPException(status_code=400, detail="Could not resolve any stops from 'order'.")
        total_time, total_cost = _compute_totals(orig_indices, full_venues, full_tm, group_size)
        route_payload = [full_venues[i] for i in orig_indices]
        return {
            "route": route_payload,
            "route_indices": orig_indices,
            "total_time": total_time,
            "total_cost": total_cost,
            "filters": {"categories": cats},
            "meta": {
                "group_size": group_size,
                "fairness": fairness,
                "start": _parse_latlon(start),
                "end": _parse_latlon(end),
                "source": "locked_order",
            },
        }

    # Otherwise, call the solver on the filtered set
    # Multiply budget by group size so per-person budget scales
    effective_budget = int(budget)

    try:
        # Solve on the filtered problem if we filtered; else on the full set
        v_for_solver = venues_flt or full_venues
        tm_for_solver = tm_flt if venues_flt is not None and keep_map else full_tm
        # Apply walking multiplier to travel matrix without mutating cache
        if walk_multiplier and abs(walk_multiplier - 1.0) > 1e-6:
            tm_for_solver = [
                [float(x) * float(walk_multiplier) for x in row]
                for row in tm_for_solver
            ]
        embeddings_for_solver = _slice_embeddings(embeddings_full, keep_map) if embeddings_full is not None else None

        # parse inline prefs JSON if provided
        prefs_map = None
        if prefs:
            try:
                parsed = json.loads(prefs)
                # parsed may be a dict or a list of dicts
                if isinstance(parsed, dict):
                    prefs_map = {str(k).strip().lower(): float(v) for k, v in parsed.items()}
                elif isinstance(parsed, list):
                    # keep as list of normalized dicts for per-person weights
                    parsed_list = []
                    for item in parsed:
                        if isinstance(item, dict):
                            parsed_list.append({str(k).strip().lower(): float(v) for k, v in item.items()})
                    if parsed_list:
                        prefs_map = parsed_list
                    else:
                        prefs_map = None
                else:
                    prefs_map = None
            except Exception:
                prefs_map = None

        # Map simple frontend fairness slider (0..1) to planner fairness knobs.
        # If an explicit fairness_profile is provided, prefer it.
        plan_kwargs = {}
        if fairness_profile:
            plan_kwargs['fairness_profile'] = fairness_profile
        else:
            # slider -> preset/alpha mapping
            try:
                fval = float(fairness)
            except Exception:
                fval = 0.0
            if fval >= 0.8:
                # request strict preset
                plan_kwargs['fairness_profile'] = 'strict'
            elif fval <= 0.2:
                # balanced / default behavior
                plan_kwargs['fairness_profile'] = None
            else:
                # intermediate: scale fairness_alpha between 1..10 and keep Nash marginal
                plan_kwargs['fairness_alpha'] = 1.0 + fval * 9.0
                plan_kwargs['fairness_mode'] = 'marginal'
                plan_kwargs['fairness_group_aggregator'] = 'nash'

        # Build absolute start timestamp if provided
        meet_start_iso = None
        if meet_date and meet_time:
            # basic sanitize
            d = meet_date.strip()
            t = meet_time.strip()
            tz = (meet_tz or "+08:00").strip()
            if len(t) == 5:  # HH:MM
                t = f"{t}:00"
            if tz and (tz.startswith("+") or tz.startswith("-")) and len(tz) in (6, 9):
                meet_start_iso = f"{d}T{t}{tz}"
            else:
                meet_start_iso = f"{d}T{t}"

        result = plan_route(
            v_for_solver,
            tm_for_solver,
            budget=effective_budget,
            time_limit=time_limit,
            prefs=prefs_map,
            embeddings=embeddings_for_solver,
            sim_threshold=sim_threshold,
            semantic_penalty=semantic_penalty,
            transfer_buffer_min=transfer_buffer_min,
            travel_rounding=travel_rounding,
            meet_start_iso=meet_start_iso,
            **plan_kwargs,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Planner failed: {e}")

    # result is expected to contain 'route_indices' against the filtered set
    route_indices_filtered: List[int] = result.get("route_indices", [])
    if not isinstance(route_indices_filtered, list):
        raise HTTPException(status_code=500, detail="Planner returned invalid 'route_indices'.")

    # Map filtered indices back to original indices
    if keep_map:
        route_indices_original = [keep_map[i] for i in route_indices_filtered]
    else:
        route_indices_original = route_indices_filtered

    # Build payload and recompute totals so numbers are consistent even if solver omitted costs
    total_time, total_cost = _compute_totals(route_indices_original, full_venues, full_tm, group_size)
    route_payload = [full_venues[i] for i in route_indices_original]

    # Merge in anything else the solver produced
    out = {
        "route": route_payload,
        "route_indices": route_indices_original,
        "total_time": total_time,
        "total_cost": total_cost,
        "filters": {"categories": cats},
        "meta": {
            "group_size": group_size,
            "fairness": fairness,
            "start": _parse_latlon(start),
            "end": _parse_latlon(end),
            "source": "solver",
            "walk_multiplier": walk_multiplier,
            "transfer_buffer_min": transfer_buffer_min,
            "travel_rounding": travel_rounding,
        },
    }
    # surface any extra keys from the solver except ones we overwrite
    for k, v in result.items():
        if k not in out:
            out[k] = v
    return out


@router.get("/categories")
def list_categories():
    """Return sorted list of unique categories (lowercased) from venues.json."""
    try:
        venues, _ = _load_base_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load base data: {e}")
    cats = {
        str(v.get("category", "unknown")).strip().lower()
        for v in venues if v.get("category") is not None
    }
    out = sorted(c for c in cats if c)
    return out

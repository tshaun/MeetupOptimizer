# route_router.py
from fastapi import APIRouter, HTTPException, Query
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
import json
import math
import time

# Your solver
from src.planner import plan_route, smooth_route  # must accept (venues, tm, budget, time_limit)

router = APIRouter(prefix="/plan", tags=["planner"])
# -----------------------------
# Simple file cache to avoid re-reading on every request
# -----------------------------
_DATA_CACHE: Dict[str, Any] = {
    "venues": None,
    "tm": None,
    "venues_mtime": 0.0,
    "tm_mtime": 0.0,
}

def _data_root() -> Path:
    # points to src/
    return Path(__file__).resolve().parents[1]

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
# Helpers
# -----------------------------
def _parse_categories(categories: Optional[str]) -> Optional[List[str]]:
    if not categories:
        return None
    # accept some common synonyms from the frontend (make filter robust)
    synonym_map = {
        "restaurant": "food",
        "restaurants": "food",
        "gallery": "activity",
        "galleries": "activity",
    }
    cats = [c.strip() for c in categories.split(",") if c.strip()]
    cats = [synonym_map.get(c.lower(), c).lower() for c in cats]
    return cats or None

def _slice_by_categories(venues: List[dict], tm: List[List[float]], cats: Optional[List[str]]) -> Tuple[List[dict], List[List[float]], List[int]]:
    if not cats:
        # no filtering
        return venues, tm, list(range(len(venues)))
    keep_idx = [i for i, v in enumerate(venues) if (v.get("category") in cats)]
    if not keep_idx:
        return [], [[]], []
    # slice matrix
    sliced = [[tm[i][j] for j in keep_idx] for i in keep_idx]
    sliced_venues = [venues[i] for i in keep_idx]
    return sliced_venues, sliced, keep_idx

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
    prefs: Optional[str] = Query(None, description="Optional JSON-encoded preference weights, e.g. '{\"cafe\":1.5}'"),
    group_size: int = Query(1, ge=1),
    categories: Optional[str] = Query(None, description="Comma-separated categories to include"),
    fairness: float = Query(0.0, ge=0.0, le=1.0, description="0 = maximize sum, 1 = balance; passed through for now"),
    start: Optional[str] = Query(None, description="Optional 'lat,lon' start (not used by solver)"),
    end: Optional[str] = Query(None, description="Optional 'lat,lon' end (not used by solver)"),
    lock_order: bool = Query(False, description="If true and 'order' provided, respect that order instead of solving"),
    order: Optional[str] = Query(None, description="Comma-separated indices/ids/names when lock_order=true"),
):
    # diagnostic container for pipeline stages/timings
    t_start_request = time.time()
    debug = {"stages": {}}
    try:
        t0 = time.time()
        full_venues, full_tm = _load_base_data()
        debug["stages"]["data_load"] = {"ok": True, "duration_s": round(time.time() - t0, 3), "n_venues": len(full_venues)}
        # log immediately to terminal (print only; avoid logger to prevent file export)
        print("data_load:", debug["stages"]["data_load"])
    except Exception as e:
        debug["stages"]["data_load"] = {"ok": False, "error": str(e)}
        raise HTTPException(status_code=500, detail=f"Failed to load base data: {e}")

    # try to load or compute semantic embeddings for venues (optional)
    root = _data_root()
    full_embs = None
    try:
        emb_path = root / "venues_embs.json"
        t0 = time.time()
        if emb_path.exists():
            try:
                full_embs = json.loads(emb_path.read_text())
                debug["stages"]["embeddings"] = {"source": "file", "ok": True, "duration_s": round(time.time() - t0, 3)}
                print("embeddings (file):", debug["stages"]["embeddings"])
            except Exception as e:
                full_embs = None
                debug["stages"]["embeddings"] = {"source": "file", "ok": False, "error": str(e)}
                print("embeddings (file) failed:", debug["stages"]["embeddings"])
        else:
            # attempt to compute embeddings if sentence-transformers is installed
            try:
                t1 = time.time()
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
                def venue_text(v):
                    return f"{v.get('name','')}. {v.get('note','')}. Category: {v.get('category','')}._"
                texts = [venue_text(v) for v in full_venues]
                embs = model.encode(texts, normalize_embeddings=True)
                # convert to Python lists for JSON-compatibility if callers want to persist
                try:
                    full_embs = embs.tolist()
                except Exception:
                    # embs may already be list-like
                    full_embs = list(map(list, embs))
                debug["stages"]["embeddings"] = {"source": "computed", "ok": True, "duration_s": round(time.time() - t1, 3)}
                print("embeddings (computed):", debug["stages"]["embeddings"])
            except Exception as e:
                full_embs = None
                debug["stages"]["embeddings"] = {"source": "none", "ok": False, "error": str(e)}
                print("embeddings: none", debug["stages"]["embeddings"])
    except Exception:
        full_embs = None

    cats = _parse_categories(categories)
    debug["stages"]["parsed_categories"] = {"raw": categories, "parsed": cats}
    print("categories parsed:", debug["stages"]["parsed_categories"])
    venues_flt, tm_flt, keep_map = _slice_by_categories(full_venues, full_tm, cats)
    debug["stages"]["filtering"] = {"kept": len(keep_map) if keep_map else 0}
    print("filtering:", debug["stages"]["filtering"])

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

        # parse prefs JSON if provided
        prefs_obj = None
        if prefs:
            try:
                prefs_obj = json.loads(prefs)
            except Exception:
                # ignore malformed prefs and fall back to defaults
                prefs_obj = None
        debug["stages"]["prefs"] = {"raw": prefs, "parsed": prefs_obj}
        print("prefs:", debug["stages"]["prefs"])

        # Defensive mapping: map common frontend keys to backend category keys
        if prefs_obj is not None:
            key_map = {
                "restaurant": "food",
                "restaurants": "food",
                "gallery": "activity",
                "galleries": "activity",
            }
            def _remap_dict(d):
                out = {}
                for k, v in d.items():
                    nk = key_map.get(k.lower(), k).lower()
                    try:
                        out[nk] = float(v)
                    except Exception:
                        out[nk] = v
                return out

            if isinstance(prefs_obj, dict):
                prefs_obj = _remap_dict(prefs_obj)
            elif isinstance(prefs_obj, list):
                prefs_obj = [_remap_dict(x) if isinstance(x, dict) else x for x in prefs_obj]

        # slice embeddings to match filtered venues if we have them
        emb_for_solver = None
        try:
            if full_embs is not None:
                if keep_map:
                    emb_for_solver = [full_embs[i] for i in keep_map]
                else:
                    emb_for_solver = full_embs
        except Exception:
            emb_for_solver = None
        debug["stages"]["embeddings_for_solver"] = {"ok": bool(emb_for_solver), "n": len(emb_for_solver) if emb_for_solver else 0}
        print("embeddings_for_solver:", debug["stages"]["embeddings_for_solver"])

        t_solver = time.time()
        result = plan_route(
            v_for_solver,
            tm_for_solver,
            budget=effective_budget,
            time_limit=time_limit,
            prefs=prefs_obj,
            embeddings=emb_for_solver,
        )
        debug["stages"]["solver"] = {"duration_s": round(time.time() - t_solver, 3), "ok": True}
        print("solver:", debug["stages"]["solver"])
        # post-process ordering to improve coherence (non-destructive; best-effort)
        try:
            if isinstance(result, dict) and isinstance(result.get("route_indices"), list) and result.get("route_indices"):
                # smooth on the filtered problem (indices are relative to v_for_solver)
                t_smooth = time.time()
                sm = smooth_route(result["route_indices"], v_for_solver, tm_for_solver, time_limit=time_limit)
                result["route_indices"] = sm
                debug["stages"]["smoothing"] = {"ok": True, "duration_s": round(time.time() - t_smooth, 3)}
                print("smoothing:", debug["stages"]["smoothing"])
        except Exception as e:
            # don't fail the request for smoothing issues
            debug["stages"]["smoothing"] = {"ok": False, "error": str(e)}
            print("smoothing failed:", debug["stages"]["smoothing"])
            pass
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
    # finalize debug timings
    debug["stages"]["totals_computed"] = {"total_time": total_time, "total_cost": total_cost}
    debug["stages"]["completed"] = {"duration_s": round(time.time() - t_start_request, 3)}
    # final log summary
    print("plan request stages:")
    print(json.dumps(debug, indent=2))

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
            "debug": debug,
        },
    }
    # surface any extra keys from the solver except ones we overwrite
    for k, v in result.items():
        if k not in out:
            out[k] = v
    return out

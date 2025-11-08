from math import inf, log, exp, ceil
from collections import Counter

def plan_route(
    venues,
    tm,
    *,
    time_limit=360,
    budget=80,
    start_idx: int | None = None,
    end_idx: int | None = None,
    meet_start_iso: str | None = None,  # ISO timestamp with timezone, e.g. "2025-10-29T18:00:00+08:00"
    transfer_buffer_min: float = 0.0,    # minimum handoff buffer in minutes (applied to raw travel); 0 = none
    travel_rounding: str = "ceil",       # how to round travel for schedule/feasibility: 'ceil' | 'round' | 'none'
    prefs=None,               # backward compat: single or list aggregated into people_prefs
    people_prefs=None,        # new: list[dict[str,float]] per-person preferences (lowercased keys)
    base_reward=10.0,
    diversity_penalty=0.6,
    cost_beta=0.4,
    fatigue_decay=0.03,   # deprecated (kept for backward compat); fatigue now tied to remaining time
    variety_penalty=0.2,
    apply_smoothing=True,
    enable_category_boosts=True,
    travel_divisor_k=1.0,
    embeddings=None,
    sim_threshold=0.8,
    semantic_penalty=0.3,
    utility_aggregator="nash",  # 'nash' (geometric mean) or 'utilitarian' (sum)
    fairness_mode="marginal",   # 'marginal' (delta) or 'absolute' (prospective)
    fairness_alpha=1.0,          # scales fairness utility before other penalties
    fairness_group_aggregator="nash",  # 'nash' or 'maxmin' for group welfare when not utilitarian
    fairness_profile=None,              # None | 'balanced' | 'strict' overrides key fairness params
):
    """
    Greedy planner with hard time/budget caps.
    Returns dict with route indices, names, total time, total cost.
    """
    # optional absolute start time support
    meet_start_dt = None
    try:
        if meet_start_iso:
            # Prefer fromisoformat; if it fails, leave as None
            from datetime import datetime
            meet_start_dt = datetime.fromisoformat(meet_start_iso)
    except Exception:
        meet_start_dt = None

    # --- defaults ---
    default_prefs = {
        "food": 1.0,
        "cafe": 1.8,
        "bar": 1.4,
        "museum": 0.9,
        "activity": 1.6,
        "thrift": 1.2,
        "park": 0.8,
    }

    # -------- helpers (fairness) --------
    def _normalize_people(p_in, legacy):
        """Return list of per-person preference dicts (lowercased keys).

        Precedence:
        - If people_prefs explicitly provided and is list of dicts, use it.
        - Else if legacy prefs is list, treat as people_prefs.
        - Else if legacy prefs is dict, wrap as single person.
        - Else use one default person (default_prefs).
        Fill missing categories with default weight (from default_prefs or 1.0 if absent).
        """
        if p_in is not None and isinstance(p_in, list) and all(isinstance(x, dict) for x in p_in):
            raw = p_in
        elif legacy is not None:
            if isinstance(legacy, list) and all(isinstance(x, dict) for x in legacy):
                raw = legacy
            elif isinstance(legacy, dict):
                raw = [legacy]
            else:
                raw = [default_prefs]
        else:
            raw = [default_prefs]

        norm_list = []
        for d in raw:
            nd = {}
            for k, v in d.items():
                try:
                    nd[str(k).strip().lower()] = float(v)
                except (TypeError, ValueError):
                    continue
            # fill defaults for missing known categories
            for k_def, v_def in default_prefs.items():
                if k_def not in nd:
                    nd[k_def] = v_def
            norm_list.append(nd)

        # ensure venue categories also present
        all_cats = set()
        for v in venues:
            all_cats.add(str(v.get("category", "unknown")).strip().lower())
        for nd in norm_list:
            for c in all_cats:
                if c not in nd:
                    nd[c] = 1.0
        return norm_list

    # Fairness profile presets override selected parameters for reproducibility
    if fairness_profile == 'strict':
        enable_category_boosts = False
        fairness_group_aggregator = 'maxmin'
        fairness_mode = 'absolute'
        fairness_alpha = 10.0
        travel_divisor_k = 8.0
        cost_beta = 0.1
        diversity_penalty = 0.9
        variety_penalty = 0.3

    people_list = _normalize_people(people_prefs, prefs)
    num_people = len(people_list)
    # clamp variety penalty to a safe range
    try:
        variety_penalty = float(variety_penalty)
    except Exception:
        variety_penalty = 0.2
    variety_penalty = max(0.0, min(variety_penalty, 0.9))
    def venue_cost(i):
        return int(venues[i].get("cost_estimate_per_person", 0) or 0)

    def route_cost(route_indices):
        return sum(venue_cost(i) for i in route_indices)

    def feasible_cost(current_cost, next_idx):
        return current_cost + venue_cost(next_idx) <= budget

    # Per-person cumulative utilities during construction (marginal fairness scoring)
    person_cum_utils = [0.0] * num_people

    def _per_person_venue_utils(cat: str):
        vals = []
        for pref_d in people_list:
            w_i = pref_d.get(cat, 1.0)
            vals.append(base_reward * (w_i ** 2))
        return vals

    def _aggregate(vals, mode):
        if mode == "utilitarian":
            return sum(vals)
        # Nash geometric mean
        sum_logs = 0.0
        for v in vals:
            sum_logs += log(max(v, 1e-6))
        return exp(sum_logs / max(len(vals), 1))

    def _group_aggregate_from_sums(person_sums, agg_name):
        """Aggregate per-person cumulative utilities to a single group score.

        agg_name: 'nash' | 'maxmin' | 'utilitarian'
        """
        if agg_name == "utilitarian":
            return sum(person_sums)
        if agg_name == "maxmin":
            return min(person_sums) if person_sums else 0.0
        # default 'nash'
        sum_logs = 0.0
        for u in person_sums:
            sum_logs += log(max(u, 1e-6))
        return exp(sum_logs / max(len(person_sums), 1))

    def fairness_score_for_cat(venue_cat: str, route_indices):
        """Return fairness utility for adding a venue of given category now.

        - marginal: uses delta of aggregated welfare (new_agg - curr_agg, floored at 0)
        - absolute: uses new_agg only (prospective aggregate), stronger push to balancing early
        Then applies diversity penalty post aggregation and scales by fairness_alpha.
        """
        # Choose group aggregator: utilitarian is used only when explicitly requested
        agg_name = "utilitarian" if utility_aggregator == "utilitarian" else fairness_group_aggregator
        curr_agg = _group_aggregate_from_sums(person_cum_utils, agg_name)
        add_vals = _per_person_venue_utils(venue_cat)
        new_sums = [person_cum_utils[i] + add_vals[i] for i in range(num_people)]
        new_agg = _group_aggregate_from_sums(new_sums, agg_name)
        if fairness_mode == "absolute":
            base = new_agg
        else:
            base = max(new_agg - curr_agg, 0.0)
        cats_so_far_lc = [str(venues[i].get("category", "unknown")).strip().lower() for i in route_indices]
        count_same = Counter(cats_so_far_lc)[venue_cat]
        diversity_factor = 1.0 / (1.0 + diversity_penalty * count_same)
        return fairness_alpha * base * diversity_factor

    def cost_penalty(i):
        c = venue_cost(i)
        frac = c / max(budget, 1)
        return 1.0 / (1.0 + cost_beta * frac)

    def within_time(cum_time, travel, stay):
        return cum_time + travel + stay <= time_limit

    # --- time window helpers (absolute time) ---
    def _parse_open_windows(v):
        """Return list of (start_dt, end_dt) or empty list if none/invalid.
        Expects v.get('open_windows') as list of [start_iso, end_iso] pairs.
        """
        wins = v.get("open_windows") or v.get("opening_hours")  # allow future/backward field names
        if not wins:
            return []
        pairs = []
        from datetime import datetime
        for item in wins:
            try:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    a = datetime.fromisoformat(str(item[0]))
                    b = datetime.fromisoformat(str(item[1]))
                    if b > a:
                        pairs.append((a, b))
            except Exception:
                continue
        return pairs

    def _is_within_window(v, arrival_min: float, leave_min: float) -> bool:
        """Check if arrival/leave (mins offset from meet_start) fit some open window.
        If no windows or no meet_start specified, treat as always open.
        """
        if meet_start_dt is None:
            return True
        wins = _parse_open_windows(v)
        if not wins:
            return True
        from datetime import timedelta
        arr_dt = meet_start_dt + timedelta(minutes=float(arrival_min))
        leave_dt = meet_start_dt + timedelta(minutes=float(leave_min))
        for a, b in wins:
            if a <= arr_dt and leave_dt <= b:
                return True
        return False

    # --- travel helper applying minimum/ceil for schedule feasibility ---
    def _travel_minutes(a: int, b: int) -> float:
        if not (0 <= a < len(tm) and 0 <= b < len(tm)):
            base = 0.0
        try:
            raw = float(tm[a][b])
        except Exception:
            raw = 0.0
        base = max(raw, float(transfer_buffer_min))
        mode = (travel_rounding or "ceil").lower()
        if mode == "ceil":
            return float(ceil(base))
        if mode == "round":
            return float(round(base))
        # 'none' or unknown
        return float(base)

    # -------- prefilter: nuke impossible single stops --------
    feasible_indices = [
        i for i, v in enumerate(venues)
        if int(v.get("service_time_min", 60) or 60) <= time_limit and venue_cost(i) <= budget
    ]
    if not feasible_indices:
        return {"route_indices": [], "route_names": [], "total_time": 0, "total_cost": 0}

    # -------- choose seed (explicit start anchor or best feasible) --------
    n = len(venues)
    route = []
    visited = set()
    time_spent = 0
    current = None

    def _service_time(i: int) -> int:
        return int(venues[i].get("service_time_min", 60) or 60)

    explicit_start = None
    if start_idx is not None and 0 <= int(start_idx) < n:
        explicit_start = int(start_idx)
        stay = _service_time(explicit_start)
        if stay > time_limit or venue_cost(explicit_start) > budget:
            # explicit start is infeasible -> return empty
            return {"route_indices": [], "route_names": [], "total_time": 0, "total_cost": 0}
        if meet_start_dt is not None and not _is_within_window(venues[explicit_start], 0.0, stay):
            return {"route_indices": [], "route_names": [], "total_time": 0, "total_cost": 0}
        route = [explicit_start]
        visited = {explicit_start}
        time_spent = stay
        current = explicit_start
    else:
        # fallback to best feasible seed (no travel yet)
        best_seed, best_seed_score = None, -inf
        for j in feasible_indices:
            stay = _service_time(j)
            # seed must fit time & budget on its own
            if stay > time_limit or venue_cost(j) > budget:
                continue
            # absolute time: seed arrival=0, leave=stay must satisfy any window
            if meet_start_dt is not None:
                if not _is_within_window(venues[j], 0.0, stay):
                    continue
            # seed score: reward adjusted by price and "time pain"
            cat = str(venues[j].get("category", "unknown")).strip().lower()
            reward = fairness_score_for_cat(cat, [])
            price_factor = cost_penalty(j)
            # prefer shorter stays when time is tight
            time_factor = 1.0 / (1.0 + stay / max(time_limit, 1))
            score = (reward * price_factor * time_factor) / (stay + 1)
            if score > best_seed_score or (score == best_seed_score and (best_seed is None or j < best_seed)):
                best_seed_score = score
                best_seed = j

        if best_seed is None:
            return {"route_indices": [], "route_names": [], "total_time": 0, "total_cost": 0}
        route = [best_seed]
        visited = {best_seed}
        time_spent = _service_time(best_seed)
        current = best_seed

    # Precompute similarity matrix if embeddings provided. This is done once to
    # avoid repeated O(D) loops during candidate scoring. We attempt a safe
    # normalization; if anything goes wrong we fall back to skipping semantic
    # penalty.
    sim_matrix = None
    if embeddings is not None:
        try:
            n_emb = len(embeddings)
            norms = [0.0] * n_emb
            for i, v in enumerate(embeddings):
                s = 0.0
                for x in v:
                    s += x * x
                norms[i] = (s ** 0.5) if s > 0.0 else 0.0

            sim_matrix = [[0.0] * n_emb for _ in range(n_emb)]
            for i in range(n_emb):
                for j in range(n_emb):
                    ni = norms[i]
                    nj = norms[j]
                    if ni > 0 and nj > 0:
                        s = 0.0
                        vi = embeddings[i]
                        vj = embeddings[j]
                        for a, b in zip(vi, vj):
                            s += a * b
                        sim_matrix[i][j] = s / (ni * nj)
                    else:
                        sim_matrix[i][j] = 0.0
        except Exception:
            sim_matrix = None

    # -------- greedy expansion with hard caps --------
    while True:
        best_next = None
        best_score = -inf
        current_cost = route_cost(route)

        for j in range(n):
            if j in visited:
                continue
            # if an explicit end is provided, keep it for the final position only
            if end_idx is not None and int(end_idx) == j:
                continue
            stay = int(venues[j].get("service_time_min", 60) or 60)
            # quick reject if single venue can't fit even from a fresh start
            if stay > time_limit:
                continue
            # travel from current (raw for scoring; effective for timing)
            travel = float(tm[current][j]) if 0 <= current < n and 0 <= j < n else inf
            travel_eff = _travel_minutes(current, j)
            if travel == inf:
                continue

            # hard caps
            if not feasible_cost(current_cost, j):
                continue
            # ensure we can still reach the end anchor afterward (if provided)
            extra_end_time = 0.0
            if end_idx is not None and 0 <= int(end_idx) < n:
                extra_end_time = _travel_minutes(j, int(end_idx)) + _service_time(int(end_idx))
            if not within_time(time_spent, travel_eff, stay):
                continue
            if end_idx is not None and not within_time(time_spent + travel_eff + stay, extra_end_time, 0):
                continue
            # budget with end anchor cost reserved once (if not already counted by being in route)
            extra_end_cost = 0
            if end_idx is not None and int(end_idx) not in visited:
                extra_end_cost = venue_cost(int(end_idx))
            if current_cost + venue_cost(j) + extra_end_cost > budget:
                continue
            # absolute time feasibility: arrival/leave must be within a window if provided
            if meet_start_dt is not None:
                arrival = time_spent + travel_eff
                leave = arrival + stay
                if not _is_within_window(venues[j], arrival, leave):
                    continue

            # scoring
            cat = str(venues[j].get("category", "unknown")).strip().lower()
            reward = fairness_score_for_cat(cat, route)
            # optional category-specific boosts
            remaining = max(time_limit - time_spent, 0)
            if enable_category_boosts:
                # boost bars toward the end of the itinerary so they naturally sit later
                if str(venues[j].get("category", "")).strip().lower() == "bar" and remaining < 360:
                    reward *= 2.0
            # semantic diversity penalty using precomputed embeddings (if provided)
            if sim_matrix is not None and 0 <= j < len(sim_matrix):
                try:
                    # find max similarity between candidate j and any visited stop
                    max_sim = 0.0
                    for vi in route:
                        if 0 <= vi < len(sim_matrix[j]):
                            s = sim_matrix[j][vi]
                            if s > max_sim:
                                max_sim = s
                    if max_sim > sim_threshold:
                        reward *= semantic_penalty
                except Exception:
                    pass
            price_factor = cost_penalty(j)
            fatigue_factor = max(0.3, min(1.0, remaining / max(time_limit, 1)))

            last_cat = str(venues[route[-1]].get("category", "")).strip().lower()
            this_cat = str(venues[j].get("category", "")).strip().lower()
            same_cat_penalty = variety_penalty if last_cat == this_cat else 0.0
            variety_factor = 1.0 - same_cat_penalty

            denom = (travel + max(0.0, float(travel_divisor_k)))
            if denom <= 0.0:
                denom = 1.0
            score = (reward * price_factor * fatigue_factor * variety_factor) / denom

            if score > best_score or (score == best_score and (best_next is None or j < best_next)):
                best_score = score
                best_next = j

        if best_next is None:
            break

        travel_time = _travel_minutes(current, best_next)
        stay_time = int(venues[best_next].get("service_time_min", 60) or 60)

        if not within_time(time_spent, travel_time, stay_time):
            break
        if meet_start_dt is not None:
            arrival = time_spent + travel_time
            leave = arrival + stay_time
            if not _is_within_window(venues[best_next], arrival, leave):
                break

        visited.add(best_next)
        route.append(best_next)
        time_spent += int(round(travel_time + stay_time))
        current = best_next
        # update cumulative utilities with chosen venue
        chosen_cat = str(venues[best_next].get("category", "unknown")).strip().lower()
        add_vals = _per_person_venue_utils(chosen_cat)
        for i in range(num_people):
            person_cum_utils[i] += add_vals[i]
        # budget feasibility is already enforced before choosing best_next.
        # Continue expanding until no feasible candidates remain.

    # compute totals / telemetry helpers
    def _compute_total_time(order):
        total = 0.0
        for i, idx in enumerate(order):
            stay = int(venues[idx].get("service_time_min", 60) or 60)
            if i > 0:
                prev = order[i - 1]
                travel = _travel_minutes(prev, idx)
                total += travel
            total += stay
        return int(round(total))

    def _total_travel(order):
        dist = 0.0
        for i in range(1, len(order)):
            a = order[i - 1]
            b = order[i]
            dist += float(tm[a][b] if 0 <= a < len(tm) and 0 <= b < len(tm) else 0.0)
        return dist

    # Append end anchor if provided and not already last
    final_route = list(route)
    if end_idx is not None and 0 <= int(end_idx) < n:
        e = int(end_idx)
        if not final_route or final_route[-1] != e:
            final_route.append(e)

    # optionally smooth the route (hill-climb reorder); keep smoothing optional
    if apply_smoothing and len(final_route) >= 3:
        try:
            # smooth_route is defined below in this file
            keep_last = end_idx is not None and 0 <= int(end_idx) < n
            final_route = smooth_route(final_route, venues, tm, time_limit=time_limit, fixed_first=True, fixed_last=keep_last)
        except Exception:
            # if smoothing fails for any reason, fall back to original order
            final_route = list(route)

    # Ensure end anchor remains last if specified
    if end_idx is not None and 0 <= int(end_idx) < n and final_route and final_route[-1] != int(end_idx):
        # move all occurrences of end_idx to the end (keep relative order of others)
        e = int(end_idx)
        final_route = [x for x in final_route if x != e] + [e]

    # If absolute time is enabled, verify smoothed route still meets windows; else fallback
    if meet_start_dt is not None and len(final_route) >= 2:
        def _route_valid(order):
            t = 0.0
            for i, idx in enumerate(order):
                stay = int(venues[idx].get("service_time_min", 60) or 60)
                if i > 0:
                    prev = order[i-1]
                    t += _travel_minutes(prev, idx)
                arr = t
                lea = t + stay
                if not _is_within_window(venues[idx], arr, lea):
                    return False
                t = lea
            return True
        if not _route_valid(final_route) and _route_valid(route):
            final_route = list(route)

    total_time = _compute_total_time(final_route)
    total_cost = route_cost(final_route)
    total_travel = _total_travel(final_route)
    per_leg = total_travel / max(1, len(final_route) - 1)

    # per-person cumulative utilities over final route (sum of u_i for chosen venues)
    # person_cum_utils already tracked incrementally (may differ if smoothing changed order
    # after construction; if smoothing applied, recompute utilities in new order)
    if apply_smoothing and len(final_route) >= 3:
        # recompute to reflect new order (order doesn't matter for sums, but keep consistent logic)
        person_cum_utils = [0.0] * num_people
        for idx in final_route:
            cat = str(venues[idx].get("category", "unknown")).strip().lower()
            add_vals = _per_person_venue_utils(cat)
            for i in range(num_people):
                person_cum_utils[i] += add_vals[i]

    # Nash welfare over cumulative sums (always compute for telemetry)
    sum_logs = 0.0
    for u in person_cum_utils:
        sum_logs += log(max(u, 1e-6))
    nash_utility = exp(sum_logs / max(len(person_cum_utils), 1))
    final_aggregated = _aggregate(person_cum_utils, utility_aggregator)

    # Build optional schedule with real clock times if meet_start is provided
    solver_start_time = None
    solver_end_time = None
    route_schedule = None
    if meet_start_dt is not None:
        from datetime import timedelta
        t = 0.0
        sched = []
        per_leg_travel = []
        for i, idx in enumerate(final_route):
            stay = int(venues[idx].get("service_time_min", 60) or 60)
            if i > 0:
                prev = final_route[i-1]
                leg = _travel_minutes(prev, idx)
                per_leg_travel.append(float(leg))
                t += leg
            else:
                per_leg_travel.append(0.0)
            arr_dt = meet_start_dt + timedelta(minutes=float(t))
            lea_dt = arr_dt + timedelta(minutes=float(stay))
            sched.append({
                "index": idx,
                "id": venues[idx].get("id"),
                "name": venues[idx].get("name"),
                "arrival_time": arr_dt.isoformat(),
                "leave_time": lea_dt.isoformat(),
            })
            t += stay
        route_schedule = sched
        solver_start_time = meet_start_dt.isoformat()
        if sched:
            solver_end_time = sched[-1]["leave_time"]
    else:
        per_leg_travel = None

    return {
        "route_indices": final_route,
        "route_names": [venues[i]["name"] for i in final_route],
        "total_time": int(total_time),
        "total_cost": int(total_cost),
        # absolute time outputs
        "solver_start_time": solver_start_time,
        "solver_end_time": solver_end_time,
        "route_schedule": route_schedule,
        "route_travel_minutes": per_leg_travel,
        "travel_mode": "walking",
        # telemetry
        "solver_total_travel": float(total_travel),
        "solver_per_leg_travel": float(per_leg),
        "solver_person_utilities": person_cum_utils,
        "solver_nash_utility": float(nash_utility),
        "solver_aggregated_utility": float(final_aggregated),
    }


def smooth_route(route_indices, venues, tm, *, time_limit=None, max_iters=50, lambda_dist=0.02, fixed_first=True, fixed_last=False):
    """
    Simple hill-climb re-ordering to improve route coherence.

    - Keeps the first stop fixed (seed) and reorders the rest by trying pairwise swaps
      that improve a route-level coherence score.
    - Uses a phase preference (where categories should appear along the route)
      and transition bonuses (which category-to-category transitions are nicer).

    Returns a new list of indices (same elements, reordered).
    """
    if not route_indices or len(route_indices) < 3:
        return list(route_indices)

    def compute_total_time(order):
        # compute total time = sum(stays) + sum(travel between consecutive stops)
        total = 0.0
        for i, idx in enumerate(order):
            stay = int(venues[idx].get("service_time_min", 60) or 60)
            if i > 0:
                prev = order[i - 1]
                travel = float(tm[prev][idx] if 0 <= prev < len(tm) and 0 <= idx < len(tm) else 0.0)
                total += travel
            total += stay
        return int(round(total))

    def total_travel(order):
        dist = 0.0
        for i in range(1, len(order)):
            a = order[i - 1]
            b = order[i]
            dist += float(tm[a][b] if 0 <= a < len(tm) and 0 <= b < len(tm) else 0.0)
        return dist

    def route_coherence_score(route):
        score = 0.0
        n = len(route)
        # desired phase (0=early ... 1=late)
        desired_phase = {
            "museum": 0.2,
            "park": 0.2,
            "cafe": 0.35,
            "thrift": 0.4,
            "food": 0.5,
            "activity": 0.6,
            "bar": 0.95,
        }
        phase_weight = {
            "museum": 1.0,
            "park": 0.6,
            "cafe": 0.9,
            "thrift": 0.8,
            "food": 1.0,
            "activity": 0.8,
            "bar": 1.2,
        }

        # transition bonuses for nice sequences
        transition_bonus = {
            ("thrift", "food"): 1.2,
            ("food", "bar"): 1.4,
            ("thrift", "bar"): 0.6,
            ("cafe", "bar"): 0.8,
            ("museum", "cafe"): 0.4,
        }

        for i, idx in enumerate(route):
            cat = venues[idx].get("category", "unknown")
            cat = str(cat).strip().lower()
            phase = i / max(n - 1, 1)
            desired = desired_phase.get(cat, 0.5)
            pw = phase_weight.get(cat, 0.8)
            score += (1.0 - abs(phase - desired)) * pw

            if i > 0:
                prev_cat = venues[route[i - 1]].get("category", "unknown")
                prev_cat = str(prev_cat).strip().lower()
                score += transition_bonus.get((prev_cat, cat), 0.0)
                # small penalty for repeating same category
                if prev_cat == cat:
                    score -= 0.6

        # subtract travel penalty so more compact orders are preferred
        dist = total_travel(route)
        score -= lambda_dist * dist

        return score

    # hill-climb by pairwise swaps (keep anchors fixed when requested)
    best_route = list(route_indices)
    best_score = route_coherence_score(best_route)
    n = len(best_route)
    iters = 0
    improved = True
    while improved and iters < max_iters:
        improved = False
        iters += 1
        # try all pairwise swaps for positions 1..n-1
        local_best_score = best_score
        local_best_route = None
        start_i = 1 if fixed_first else 0
        end_j_limit = n - 1 if not fixed_last else n - 1  # last index is n-1; we'll avoid selecting it when fixed
        for i in range(start_i, n - 1):
            # skip if i is last and fixed_last
            if fixed_last and i == n - 1:
                continue
            j_start = i + 1
            j_end = n - 1 if not fixed_last else n - 1
            for j in range(j_start, j_end):
                cand = best_route[:]
                cand[i], cand[j] = cand[j], cand[i]
                # reject candidate if it violates time_limit (when provided)
                if time_limit is not None:
                    cand_time = compute_total_time(cand)
                    if cand_time > time_limit:
                        continue
                s = route_coherence_score(cand)
                if s > local_best_score + 1e-6:
                    local_best_score = s
                    local_best_route = cand

        if local_best_route is not None:
            best_route = local_best_route
            best_score = local_best_score
            improved = True

    return best_route

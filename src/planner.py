import json
from math import inf
from collections import Counter

def plan_route(
    venues,
    tm,
    *,
    time_limit=360,
    budget=80,
    prefs=None,
    base_reward=10.0,
    diversity_penalty=0.6,
    cost_beta=0.4,
    fatigue_decay=0.03,   # kept for backward compat; fatigue now tied to remaining time
    variety_penalty=0.2,
    embeddings=None,
    sim_threshold=0.8,
    semantic_penalty=0.3,
):
    """
    Greedy planner with hard time/budget caps.
    Returns dict with route indices, names, total time, total cost.
    """

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

    # -------- helpers --------
    def _normalize_prefs(p):
        # Case 1: nothing passed in → defaults
        if p is None:
            return default_prefs.copy()

        # Case 2: single dict {cat: weight}
        if isinstance(p, dict):
            merged = default_prefs.copy()
            for k, v in p.items():
                try:
                    merged[k] = float(v)
                except (TypeError, ValueError):
                    pass
            return merged

        # Case 3: list of per-person dicts
        if isinstance(p, list):
            merged = default_prefs.copy()
            for cat in default_prefs.keys():
                vals = []
                for item in p:
                    if not isinstance(item, dict):
                        continue
                    if cat in item:
                        try:
                            vals.append(float(item[cat]))
                        except (TypeError, ValueError):
                            pass
                if vals:
                    merged[cat] = sum(vals) / len(vals)
            return merged

        # Anything weird → just fall back
        return default_prefs.copy()

    prefs_dict = _normalize_prefs(prefs)
    def venue_cost(i):
        return int(venues[i].get("cost_estimate_per_person", 0) or 0)

    def route_cost(route_indices):
        return sum(venue_cost(i) for i in route_indices)

    def feasible_cost(current_cost, next_idx):
        return current_cost + venue_cost(next_idx) <= budget

    def venue_reward(venue, route_indices):
        cat = venue.get("category", "unknown")
        # use normalized prefs (category weights) when computing reward
        w = prefs_dict.get(cat, 1.0)
        cats_so_far = [venues[i].get("category", "unknown") for i in route_indices]
        count_same = Counter(cats_so_far)[cat]
        diversity_factor = 1.0 / (1.0 + diversity_penalty * count_same)
        return base_reward * (w ** 2) * diversity_factor

    def cost_penalty(i):
        c = venue_cost(i)
        frac = c / max(budget, 1)
        return 1.0 / (1.0 + cost_beta * frac)

    def within_time(cum_time, travel, stay):
        return cum_time + travel + stay <= time_limit

    # -------- prefilter: nuke impossible single stops --------
    feasible_indices = [
        i for i, v in enumerate(venues)
        if int(v.get("service_time_min", 60) or 60) <= time_limit and venue_cost(i) <= budget
    ]
    if not feasible_indices:
        return {"route_indices": [], "route_names": [], "total_time": 0, "total_cost": 0}

    # -------- choose best feasible seed (no travel yet) --------
    best_seed, best_seed_score = None, -inf
    for j in feasible_indices:
        stay = int(venues[j].get("service_time_min", 60) or 60)
        # seed must fit time & budget on its own
        if stay > time_limit or venue_cost(j) > budget:
            continue
        # seed score: reward adjusted by price and "time pain"
        reward = venue_reward(venues[j], [])
        price_factor = cost_penalty(j)
        # prefer shorter stays when time is tight
        time_factor = 1.0 / (1.0 + stay / max(time_limit, 1))
        score = (reward * price_factor * time_factor) / (stay + 1)
        if score > best_seed_score:
            best_seed_score = score
            best_seed = j

    if best_seed is None:
        return {"route_indices": [], "route_names": [], "total_time": 0, "total_cost": 0}

    # initialize route with the chosen seed
    route = [best_seed]
    visited = {best_seed}
    time_spent = int(venues[best_seed].get("service_time_min", 60) or 60)
    current = best_seed

    # -------- greedy expansion with hard caps --------
    n = len(venues)
    while True:
        best_next = None
        best_score = -inf
        current_cost = route_cost(route)

        for j in range(n):
            if j in visited:
                continue
            stay = int(venues[j].get("service_time_min", 60) or 60)
            # quick reject if single venue can't fit even from a fresh start
            if stay > time_limit:
                continue
            # travel from current
            travel = float(tm[current][j]) if 0 <= current < n and 0 <= j < n else inf
            if travel == inf:
                continue

            # hard caps
            if not feasible_cost(current_cost, j):
                continue
            if not within_time(time_spent, travel, stay):
                continue

            # scoring
            reward = venue_reward(venues[j], route)
            # boost bars toward the end of the itinerary so they naturally sit later
            # remaining is minutes left (time_limit - time_spent)
            remaining = max(time_limit - time_spent, 0)
            if venues[j].get("category") == "bar" and remaining < 360:
                reward *= 2.0
            # semantic diversity penalty using precomputed embeddings (if provided)
            if embeddings is not None:
                try:
                    # embeddings assumed as list/array-like of vectors; use dot product for cosine if normalized
                    vec_j = embeddings[j]
                    # compute max similarity to any visited stop
                    max_sim = 0.0
                    for vi in route:
                        vvec = embeddings[vi]
                        # dot product
                        s = 0.0
                        # assume both are sequences of equal length
                        for a, b in zip(vec_j, vvec):
                            s += a * b
                        if s > max_sim:
                            max_sim = s
                    if max_sim > sim_threshold:
                        reward *= semantic_penalty
                except Exception:
                    # if embeddings malformed, ignore semantic penalty
                    pass
            price_factor = cost_penalty(j)
            fatigue_factor = max(0.3, min(1.0, remaining / max(time_limit, 1)))

            last_cat = venues[route[-1]].get("category", "")
            same_cat_penalty = variety_penalty if last_cat == venues[j].get("category", "") else 0.0
            variety_factor = 1.0 - same_cat_penalty

            score = (reward * price_factor * fatigue_factor * variety_factor) / (travel + 1.0)

            if score > best_score:
                best_score = score
                best_next = j

        if best_next is None:
            break

        travel_time = float(tm[current][best_next])
        stay_time = int(venues[best_next].get("service_time_min", 60) or 60)

        if not within_time(time_spent, travel_time, stay_time):
            break

        visited.add(best_next)
        route.append(best_next)
        time_spent += int(round(travel_time + stay_time))
        current = best_next

        if route_cost(route) > budget:
            route.pop()
            break

    total_cost = route_cost(route)
    return {
        "route_indices": route,
        "route_names": [venues[i]["name"] for i in route],
        "total_time": int(time_spent),
        "total_cost": int(total_cost),
    }


def smooth_route(route_indices, venues, tm, *, time_limit=None, max_iters=50, lambda_dist=0.02):
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
            phase = i / max(n - 1, 1)
            desired = desired_phase.get(cat, 0.5)
            pw = phase_weight.get(cat, 0.8)
            score += (1.0 - abs(phase - desired)) * pw

            if i > 0:
                prev_cat = venues[route[i - 1]].get("category", "unknown")
                score += transition_bonus.get((prev_cat, cat), 0.0)
                # small penalty for repeating same category
                if prev_cat == cat:
                    score -= 0.6

        # subtract travel penalty so more compact orders are preferred
        dist = total_travel(route)
        score -= lambda_dist * dist

        return score

    # hill-climb by pairwise swaps (keep first index fixed)
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
        for i in range(1, n - 1):
            for j in range(i + 1, n):
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

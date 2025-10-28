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
    cost_beta=0.8,
    fatigue_decay=0.03,   # kept for backward compat; fatigue now tied to remaining time
    variety_penalty=0.4
):
    """
    Greedy planner with hard time/budget caps.
    Returns dict with route indices, names, total time, total cost.
    """

    # --- defaults ---
    if prefs is None:
        prefs = {
            "food": 1.0,
            "cafe": 1.8,
            "bar": 1.4,
            "museum": 0.9,
            "activity": 1.6,
            "thrift": 1.2,
            "park": 0.8,
        }

    # -------- helpers --------
    def venue_cost(i):
        return int(venues[i].get("cost_estimate_per_person", 0) or 0)

    def route_cost(route_indices):
        return sum(venue_cost(i) for i in route_indices)

    def feasible_cost(current_cost, next_idx):
        return current_cost + venue_cost(next_idx) <= budget

    def venue_reward(venue, route_indices):
        cat = venue.get("category", "unknown")
        w = prefs.get(cat, 1.0)
        cats_so_far = [venues[i].get("category", "unknown") for i in route_indices]
        count_same = Counter(cats_so_far)[cat]
        diversity_factor = 1.0 / (1.0 + diversity_penalty * count_same)
        return base_reward * w * diversity_factor

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
            price_factor = cost_penalty(j)

            remaining = max(time_limit - time_spent, 0)
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

        # commit next
        travel_time = float(tm[current][best_next])
        stay_time = int(venues[best_next].get("service_time_min", 60) or 60)

        # final guard (paranoid but safe)
        if not within_time(time_spent, travel_time, stay_time):
            break

        visited.add(best_next)
        route.append(best_next)
        time_spent += int(round(travel_time + stay_time))
        current = best_next

        if route_cost(route) > budget:
            # remove the last venue and break out — budget blown
            route.pop()
            break

    total_cost = route_cost(route)
    return {
        "route_indices": route,
        "route_names": [venues[i]["name"] for i in route],
        "total_time": int(time_spent),
        "total_cost": int(total_cost),
    }

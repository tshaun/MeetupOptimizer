import argparse
import json
import random
import statistics
from typing import List, Dict, Tuple

from backend.planner import plan_route

VENUES_PATH = 'src/venues.json'
TM_PATH = 'src/travel_matrix_v2.json'


def load_data() -> Tuple[List[dict], List[List[float]], List[str]]:
    with open(VENUES_PATH, 'r', encoding='utf-8') as f:
        venues = json.load(f)
    with open(TM_PATH, 'r', encoding='utf-8') as f:
        tm_raw = json.load(f)
    if not isinstance(tm_raw, dict) or ('matrix_min' not in tm_raw and 'matrix' not in tm_raw):
        raise RuntimeError('travel_matrix_v2.json must contain matrix_min or matrix')
    tm = tm_raw.get('matrix_min') or tm_raw.get('matrix')
    if len(tm) != len(venues):
        raise RuntimeError(f'Travel matrix size {len(tm)} must match venues size {len(venues)}')
    # unique categories from venues (lowercased)
    cats = sorted({str(v.get('category', 'unknown')).strip().lower() for v in venues})
    return venues, tm, cats


def gen_people_prefs(cats: List[str], num_people: int = 2,
                     focus_weight: float = 3.0, base_weight: float = 0.7) -> List[Dict[str, float]]:
    """Generate per-person preference dicts with stronger contrast.

    Each person gets one distinct focus category (sampled without replacement),
    assigned weight=focus_weight; all other categories assigned base_weight.
    """
    chosen = random.sample(cats, k=min(num_people, len(cats)))
    prefs = []
    for i in range(num_people):
        focus = chosen[i % len(chosen)]
        d = {c: base_weight for c in cats}
        d[focus] = focus_weight
        prefs.append(d)
    return prefs


def run_trials(venues, tm, cats, trials: int, seed: int,
               time_limit: int,
               budget: int,
               diversity_penalty: float,
               variety_penalty: float,
               cost_beta: float,
               travel_divisor_k: float,
               fairness_alpha: float = 1.0,
               fairness_mode: str = 'marginal',
               fairness_group_aggregator: str = 'nash',
               focus_weight: float = 3.0,
               base_weight: float = 0.7) -> dict:
    rng = random.Random(seed)
    wins = 0
    fair_mins = []
    util_mins = []

    for t in range(trials):
        # use independent RNG for category sampling per trial
        random.seed(rng.randint(0, 2**31-1))
        people = gen_people_prefs(cats, num_people=2, focus_weight=focus_weight, base_weight=base_weight)

        fair = plan_route(
            venues, tm,
            time_limit=time_limit,
            budget=budget,
            people_prefs=people,
            apply_smoothing=False,
            enable_category_boosts=False,
            diversity_penalty=diversity_penalty,
            variety_penalty=variety_penalty,
            cost_beta=cost_beta,
            travel_divisor_k=travel_divisor_k,
            utility_aggregator='nash',
            fairness_alpha=fairness_alpha,
            fairness_mode=fairness_mode,
            fairness_group_aggregator=fairness_group_aggregator,
        )
        util = plan_route(
            venues, tm,
            time_limit=time_limit,
            budget=budget,
            people_prefs=people,
            apply_smoothing=False,
            enable_category_boosts=False,
            diversity_penalty=diversity_penalty,
            variety_penalty=variety_penalty,
            cost_beta=cost_beta,
            travel_divisor_k=travel_divisor_k,
            utility_aggregator='utilitarian',
            fairness_alpha=fairness_alpha,
            fairness_mode=fairness_mode,
            fairness_group_aggregator=fairness_group_aggregator,
        )

        fair_min = min(fair.get('solver_person_utilities', [0.0]))
        util_min = min(util.get('solver_person_utilities', [0.0]))
        fair_mins.append(fair_min)
        util_mins.append(util_min)
        if fair_min > util_min:
            wins += 1

    win_rate = wins / float(trials)
    avg_fair_min = statistics.mean(fair_mins) if fair_mins else 0.0
    avg_util_min = statistics.mean(util_mins) if util_mins else 0.0
    med_fair_min = statistics.median(fair_mins) if fair_mins else 0.0
    med_util_min = statistics.median(util_mins) if util_mins else 0.0

    return {
        'win_rate': win_rate,
        'avg_fair_min': avg_fair_min,
        'avg_util_min': avg_util_min,
        'med_fair_min': med_fair_min,
        'med_util_min': med_util_min,
    }


def main():
    ap = argparse.ArgumentParser(description='Benchmark fairness (Nash) vs utilitarian on min per-person utility.')
    ap.add_argument('--trials', type=int, default=100)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--time-limit', type=int, default=180)
    ap.add_argument('--budget', type=int, default=100)
    # sweep ranges
    ap.add_argument('--diversity', nargs='+', type=float, default=[0.8, 0.9, 1.0, 1.1])
    ap.add_argument('--variety', nargs='+', type=float, default=[0.2, 0.3, 0.4])
    ap.add_argument('--cost-beta', nargs='+', type=float, default=[0.1, 0.2, 0.275, 0.35])
    ap.add_argument('--travel-k', nargs='+', type=float, default=[1.0, 2.0, 3.0, 5.0, 8.0, 10.0])
    ap.add_argument('--fairness-alpha', nargs='+', type=float, default=[1.0, 3.0, 5.0, 8.0, 10.0])
    ap.add_argument('--fairness-mode', nargs='+', type=str, default=['marginal','absolute'])
    ap.add_argument('--fairness-group', nargs='+', type=str, default=['nash','maxmin'])
    ap.add_argument('--focus-weight', type=float, default=3.0)
    ap.add_argument('--base-weight', type=float, default=0.7)

    args = ap.parse_args()

    venues, tm, cats = load_data()

    print(f'Dataset: venues={len(venues)}, tm={len(tm)}x{len(tm[0])}')
    print(f'Trials={args.trials}, seed={args.seed}')

    best = None
    for dp in args.diversity:
        for vp in args.variety:
            for cb in args.cost_beta:
                for tk in args.travel_k:
                    for fa in args.fairness_alpha:
                        for fm in args.fairness_mode:
                            for fg in args.fairness_group:
                                res = run_trials(venues, tm, cats, args.trials, args.seed,
                                                 args.time_limit, args.budget,
                                                 dp, vp, cb, tk,
                                                 fairness_alpha=fa, fairness_mode=fm,
                                                 fairness_group_aggregator=fg,
                                                 focus_weight=args.focus_weight,
                                                 base_weight=args.base_weight)
                                print(f'Params: group={fg}, mode={fm}, diversity={dp:.3f}, variety={vp:.3f}, cost_beta={cb:.3f}, travel_k={tk:.1f}, '
                                      f'fairness_alpha={fa:.1f}, focus={args.focus_weight:.1f}, base={args.base_weight:.1f} | '
                                      f'win_rate={res["win_rate"]*100:.1f}% | '
                                      f'avg_min: nash={res["avg_fair_min"]:.2f}, util={res["avg_util_min"]:.2f} | '
                                      f'med_min: nash={res["med_fair_min"]:.2f}, util={res["med_util_min"]:.2f}')
                                if best is None or res['win_rate'] > best['win_rate']:
                                    best = {**res, 'params': (fg, fm, dp, vp, cb, tk, fa, args.focus_weight, args.base_weight)}

    if best:
        fg, fm, dp, vp, cb, tk, fa, focus_w, base_w = best['params']
        print('\nBest combo: group={}, mode={}, diversity={:.3f}, variety={:.3f}, cost_beta={:.3f}, travel_k={:.1f}, fairness_alpha={:.1f}, focus_w={:.1f}, base_w={:.1f}'.format(
            fg, fm, dp, vp, cb, tk, fa, focus_w, base_w))
        print('Best win rate: {:.1f}%'.format(best['win_rate'] * 100))
        print('Avg min utilities: Nash {:.2f} vs Util {:.2f}'.format(best['avg_fair_min'], best['avg_util_min']))
        print('Med min utilities: Nash {:.2f} vs Util {:.2f}'.format(best['med_fair_min'], best['med_util_min']))
        if best['win_rate'] >= 0.7:
            print('Target achieved (>=70% win rate).')
        else:
            print('Target not achieved; consider expanding sweeps or tuning preferences generation.')


if __name__ == '__main__':
    main()

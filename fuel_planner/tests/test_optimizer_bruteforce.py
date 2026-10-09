"""
Independent validation of the DP optimizer against exhaustive search.

The brute-force solver below shares no code with the optimizer. It drives
the route station by station and tries every integer purchase quantity
(0..capacity) at every station.

Why integer search is exact here: generated station positions are multiples
of mpg miles, and capacity and starting fuel are whole gallons, so every fuel
level is a whole number of gallons. The purchase LP has interval
(consecutive-ones) constraints, which makes its matrix totally unimodular.
With integral data it therefore has an integral optimal solution, so the
integer minimum equals the continuous minimum the DP computes.
"""
import itertools
import random
from decimal import Decimal
from fractions import Fraction

import pytest

from fuel_planner.services.optimizer import FuelRouteOptimizer, StationCandidate


def brute_force_min_cost(total_miles, stations, capacity, mpg, start_fuel):
    """stations: list of (position_miles, price). Returns min cost (Fraction) or None."""
    stations = sorted(stations)
    best = None
    for buys in itertools.product(range(capacity + 1), repeat=len(stations)):
        fuel, prev, cost, ok = Fraction(start_fuel), 0, Fraction(0), True
        for (p, price), q in zip(stations, buys):
            fuel -= Fraction(p - prev, mpg)
            if fuel < 0:
                ok = False
                break
            fuel += q
            if fuel > capacity:
                ok = False
                break
            cost += q * Fraction(str(price))
            prev = p
        if not ok:
            continue
        fuel -= Fraction(total_miles - prev, mpg)
        if fuel < 0:
            continue
        if best is None or cost < best:
            best = cost
    return best


def simulate(plan_stops, total_miles, mpg, capacity, start_fuel):
    """Independently re-drive the returned plan and check every invariant."""
    fuel, prev = Fraction(str(start_fuel)), Fraction(0)
    for s in plan_stops:
        pos = Fraction(str(s.route_position_miles))
        assert pos > prev
        fuel -= (pos - prev) / mpg
        assert fuel >= 0
        assert abs(float(fuel) - s.arrival_fuel_gallons) < 1e-3
        fuel += Fraction(str(s.gallons_to_purchase))
        assert fuel <= capacity + Fraction(1, 1000)
        prev = pos
    fuel -= (Fraction(str(total_miles)) - prev) / mpg
    assert fuel >= -Fraction(1, 1000)


CASES = []
rng = random.Random(20261009)
for _ in range(250):
    capacity = rng.choice([3, 4, 5, 6])
    mpg = 10
    n = rng.randint(1, 4)
    total = rng.randint(capacity + 1, capacity * (n + 1)) * mpg
    positions = sorted(rng.sample(range(1, total // mpg), k=min(n, total // mpg - 1)))
    stations = [(p * mpg, round(rng.uniform(2.5, 4.5), 2)) for p in positions]
    start = rng.randint(0, capacity)
    CASES.append((total, stations, capacity, mpg, start))


@pytest.mark.parametrize("total,stations,capacity,mpg,start", CASES)
def test_dp_matches_exhaustive_search(total, stations, capacity, mpg, start):
    expected = brute_force_min_cost(total, stations, capacity, mpg, start)
    opt = FuelRouteOptimizer(tank_capacity_gallons=capacity, fuel_economy_mpg=mpg)
    cands = [StationCandidate(f"s{i}", f"S{i}", 0, 0, Decimal(str(pr)), float(p)) for i, (p, pr) in enumerate(stations)]
    res = opt.optimize(float(total), cands, starting_fuel_gallons=float(start))

    if expected is None:
        assert res.is_feasible is False
        return

    assert res.is_feasible is True, res.error_message
    # Exact cost from unrounded quantities equals brute-force optimum.
    exact = sum(Fraction(str(s.gallons_to_purchase)) * Fraction(str(s.price_per_gallon)) for s in res.fuel_stops)
    assert exact == expected
    # Reported total equals the sum of per-stop charges, to the cent.
    assert res.total_fuel_cost_usd == sum((s.purchase_cost_usd for s in res.fuel_stops), Decimal("0.00"))
    assert abs(res.total_fuel_cost_usd - Decimal(expected.numerator) / Decimal(expected.denominator)) <= Decimal("0.01") * max(1, len(res.fuel_stops))
    simulate(res.fuel_stops, total, Fraction(mpg), capacity, start)


def test_access_distance_is_charged():
    """An off-route station costs extra fuel to reach; a cheaper far station can lose to an on-route one."""
    opt = FuelRouteOptimizer(tank_capacity_gallons=50, fuel_economy_mpg=10)
    on_route = StationCandidate("on", "On", 0, 0, Decimal("3.20"), 300.0)
    off_route = StationCandidate("off", "Off", 0, 0, Decimal("3.15"), 300.0,
                                 detour_distance_miles=40.0, access_miles_one_way=20.0)
    res = opt.optimize(600.0, [on_route, off_route], starting_fuel_gallons=50.0)
    # On-route: arrive 20, buy 10 -> $32.00.  Off-route: arrive 18, buy 14 -> $44.10.
    assert [s.station_id for s in res.fuel_stops] == ["on"]
    assert res.total_fuel_cost_usd == Decimal("32.00")

    # Off-route alone: fuel consumed must include the 40 extra miles.
    res2 = opt.optimize(600.0, [off_route], starting_fuel_gallons=50.0)
    assert res2.fuel_stops[0].arrival_fuel_gallons == 18.0
    assert res2.fuel_stops[0].gallons_to_purchase == 14.0
    assert res2.total_fuel_consumed_gallons == 64.0


def test_greedy_counterexample_start_choice():
    """
    The old greedy went to the cheapest station reachable from the start.
    Here that is worse: the cheap station is close, but the next stretch
    forces an expensive purchase. The DP must beat greedy.
    """
    opt = FuelRouteOptimizer(tank_capacity_gallons=5, fuel_economy_mpg=10)
    stations = [(10, 3.00), (50, 3.50), (90, 4.50)]
    cands = [StationCandidate(f"s{i}", "", 0, 0, Decimal(str(p)), float(x)) for i, (x, p) in enumerate(stations)]
    res = opt.optimize(130.0, cands, starting_fuel_gallons=5.0)
    expected = brute_force_min_cost(130, stations, 5, 10, 5)
    exact = sum(Fraction(str(s.gallons_to_purchase)) * Fraction(str(s.price_per_gallon)) for s in res.fuel_stops)
    assert exact == expected

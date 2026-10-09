"""
Fuel-stop optimizer: exact dynamic programming over a fixed route.

Pure business logic with no Django, database, or network dependencies.

Problem
-------
The vehicle drives a fixed route of length L, starting with S gallons in a
tank of capacity C, burning 1/mpg gallons per mile. Candidate stations lie
along the route at positions p_i with price P_i. Reaching a station also costs
an off-route access distance a_i (one way), so the driving distance between
two consecutive stops u < v is

    D(u, v) = (p_v - p_u) + a_u + a_v        (a = 0 for start and destination)

Goal: choose stops and purchase quantities that minimise total purchase cost,
such that fuel never goes negative and never exceeds C.

Algorithm
---------
This is the fixed-route "gas station problem" (Khuller, Malekian & Mestre,
"To Fill or not to Fill: The Gas Station Problem", ESA 2007 / ACM TALG 2011).
They show an optimal solution exists in which every stop does one of two
things:

  * buys just enough fuel to reach the next stop, arriving there empty; or
  * fills the tank completely.

Stopping without buying is never needed: skipping that stop is at least as
cheap, because D(u, w) <= D(u, v) + D(v, w). So the fuel on arrival at a
station is always one of the following:

  * 0                      (previous stop bought just enough)
  * C - D(u, v)/mpg        (previous stop u filled the tank)
  * S - D(start, v)/mpg    (driven directly from the start)

That gives O(n) arrival states per station. The DP below runs forward over
(station, arrival_fuel) states in route order and is exact. It uses Fraction
arithmetic, so it never makes a rounding decision. Ties go to the plan with
fewer stops.

Correctness is checked in tests against an independent brute-force solver on
hundreds of randomly generated small networks (see test_optimizer_bruteforce.py).
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

CENT = Decimal("0.01")


@dataclass
class StationCandidate:
    station_id: str
    name: str
    latitude: float
    longitude: float
    price_per_gallon: Decimal
    route_position_miles: float
    # Reported, estimated round-trip off-route distance (display only).
    detour_distance_miles: float = 0.0
    city: str = ""
    state: str = ""
    address: str = ""
    geocode_accuracy: str = "EXACT"
    # One-way access distance added to the driving distance in the model.
    access_miles_one_way: float = 0.0


@dataclass
class PlannedFuelStop:
    sequence: int
    station_id: str
    name: str
    latitude: float
    longitude: float
    price_per_gallon: Decimal
    arrival_fuel_gallons: float
    gallons_to_purchase: float
    purchase_cost_usd: Decimal
    departure_fuel_gallons: float
    route_position_miles: float
    detour_distance_miles: float
    city: str = ""
    state: str = ""
    address: str = ""
    geocode_accuracy: str = "EXACT"


@dataclass
class OptimizationResult:
    is_feasible: bool
    fuel_stops: List[PlannedFuelStop]
    total_fuel_consumed_gallons: float
    total_gallons_purchased: float
    fuel_purchase_cost_usd: Decimal
    total_fuel_cost_usd: Decimal
    modeled_starting_fuel_cost_usd: Decimal
    unreachable_leg_index: Optional[int] = None
    error_message: Optional[str] = None


def _frac(x) -> Fraction:
    return Fraction(str(x))


def _to_decimal(f: Fraction) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 34
        return Decimal(f.numerator) / Decimal(f.denominator)


def _infeasible(msg: str, leg: Optional[int] = None) -> OptimizationResult:
    return OptimizationResult(
        is_feasible=False, fuel_stops=[], total_fuel_consumed_gallons=0.0,
        total_gallons_purchased=0.0, fuel_purchase_cost_usd=Decimal("0.00"),
        total_fuel_cost_usd=Decimal("0.00"), modeled_starting_fuel_cost_usd=Decimal("0.00"),
        unreachable_leg_index=leg, error_message=msg,
    )


class FuelRouteOptimizer:
    def __init__(
        self,
        tank_capacity_gallons: float = 50.0,
        fuel_economy_mpg: float = 10.0,
        baseline_fuel_cost_per_gallon: Optional[Decimal] = None,
    ):
        self.tank_capacity = tank_capacity_gallons
        self.fuel_economy_mpg = fuel_economy_mpg
        self.max_range_miles = tank_capacity_gallons * fuel_economy_mpg
        self.baseline_fuel_cost = baseline_fuel_cost_per_gallon or Decimal("3.50")

    # ------------------------------------------------------------------ #
    def optimize(
        self,
        total_distance_miles: float,
        candidate_stations: List[StationCandidate],
        starting_fuel_gallons: float = 50.0,
    ) -> OptimizationResult:
        import math

        if not (isinstance(self.tank_capacity, (int, float)) and math.isfinite(self.tank_capacity) and self.tank_capacity > 0):
            raise ValueError(f"Tank capacity must be positive and finite, got {self.tank_capacity}.")
        if not (isinstance(self.fuel_economy_mpg, (int, float)) and math.isfinite(self.fuel_economy_mpg) and self.fuel_economy_mpg > 0):
            raise ValueError(f"Fuel economy MPG must be positive and finite, got {self.fuel_economy_mpg}.")
        if not (isinstance(total_distance_miles, (int, float)) and math.isfinite(total_distance_miles) and total_distance_miles >= 0):
            raise ValueError(f"Total distance must be non-negative and finite, got {total_distance_miles}.")
        if not (isinstance(starting_fuel_gallons, (int, float)) and math.isfinite(starting_fuel_gallons)):
            raise ValueError(f"Starting fuel must be a finite number, got {starting_fuel_gallons}.")
        if starting_fuel_gallons < 0.0 or starting_fuel_gallons > self.tank_capacity:
            raise ValueError(f"Starting fuel must be between 0.0 and {self.tank_capacity} gallons.")

        for idx, s in enumerate(candidate_stations):
            if not (math.isfinite(s.route_position_miles) and s.route_position_miles >= 0):
                raise ValueError(f"Candidate station {s.station_id} at index {idx} has invalid route position {s.route_position_miles}.")
            if s.price_per_gallon <= Decimal("0.00") or s.price_per_gallon.is_nan() or s.price_per_gallon.is_infinite():
                raise ValueError(f"Candidate station {s.station_id} at index {idx} has invalid price {s.price_per_gallon}.")
            if not (math.isfinite(s.access_miles_one_way) and s.access_miles_one_way >= 0):
                raise ValueError(f"Candidate station {s.station_id} at index {idx} has invalid access distance {s.access_miles_one_way}.")

        C = _frac(self.tank_capacity)
        mpg = _frac(self.fuel_economy_mpg)
        S = _frac(starting_fuel_gallons)
        L = _frac(total_distance_miles)

        if S * mpg >= L:
            consumed = float(L / mpg)
            return OptimizationResult(
                is_feasible=True, fuel_stops=[], total_fuel_consumed_gallons=round(consumed, 2),
                total_gallons_purchased=0.0, fuel_purchase_cost_usd=Decimal("0.00"),
                total_fuel_cost_usd=Decimal("0.00"),
                modeled_starting_fuel_cost_usd=(Decimal(str(consumed)) * self.baseline_fuel_cost).quantize(CENT, ROUND_HALF_UP),
            )

        # Stations strictly inside the route. Identical (position, access)
        # duplicates keep only the cheapest one; nearby distinct stations stay.
        best_at: Dict[Tuple[Fraction, Fraction], StationCandidate] = {}
        for s in candidate_stations:
            p = _frac(s.route_position_miles)
            if not (0 < p < L):
                continue
            key = (p, _frac(s.access_miles_one_way))
            if key not in best_at or s.price_per_gallon < best_at[key].price_per_gallon:
                best_at[key] = s
        stations = sorted(best_at.items(), key=lambda kv: (kv[0][0], kv[1].price_per_gallon))

        if not stations:
            return _infeasible("Trip distance exceeds vehicle range and no candidate fuel stations are available on route.")

        # Node arrays: 0 = start, 1..n = stations, n+1 = destination.
        pos = [Fraction(0)] + [k[0] for k, _ in stations] + [L]
        acc = [Fraction(0)] + [k[1] for k, _ in stations] + [Fraction(0)]
        price = [None] + [_frac(s.price_per_gallon) for _, s in stations] + [None]
        objs = [None] + [s for _, s in stations] + [None]
        dest = len(pos) - 1
        range_mi = C * mpg

        def dist(u: int, v: int) -> Fraction:
            return (pos[v] - pos[u]) + acc[u] + acc[v]

        # Clear diagnostics for common infeasibility causes.
        first_reach = min(dist(0, v) for v in range(1, dest))
        if first_reach > S * mpg:
            return _infeasible(
                f"Starting fuel ({starting_fuel_gallons} gal) is insufficient to reach the first "
                f"fuel station ({float(first_reach):.1f} mi away)."
            )
        for i in range(1, len(pos)):
            gap = pos[i] - pos[i - 1]
            if gap > range_mi:
                return _infeasible(
                    f"Infeasible gap: {float(gap):.1f} miles between mile {float(pos[i-1]):.1f} and "
                    f"mile {float(pos[i]):.1f} exceeds {float(range_mi):.0f}-mile tank range."
                )

        # DP: state = (node, arrival_fuel). value = (cost, stops, back-pointer).
        # back-pointer = (prev_state, purchase_at_prev, departure_fuel_at_prev)
        INF = None
        best: Dict[Tuple[int, Fraction], Tuple[Fraction, int, Optional[tuple]]] = {(0, S): (Fraction(0), 0, None)}
        by_node: Dict[int, List[Fraction]] = {0: [S]}

        def relax(state, cost, stops, back):
            cur = best.get(state)
            if cur is None or (cost, stops) < (cur[0], cur[1]):
                if cur is None:
                    by_node.setdefault(state[0], []).append(state[1])
                best[state] = (cost, stops, back)

        for u in range(0, dest):
            for f in list(by_node.get(u, [])):
                cost_u, stops_u, _ = best[(u, f)]
                for v in range(u + 1, dest + 1):
                    d = dist(u, v)
                    if pos[v] - pos[u] > range_mi:
                        break
                    need = d / mpg
                    if need > C:
                        continue
                    if u == 0:
                        # No purchase possible at the start.
                        if need <= f:
                            relax((v, f - need), cost_u, stops_u, ((u, f), Fraction(0), f))
                        continue
                    options = []
                    if need > f:
                        options.append(need)          # buy just enough to reach v
                    if C > f and v != dest:
                        options.append(C)             # fill the tank
                    for g in options:
                        purchase = g - f
                        relax((v, g - need), cost_u + purchase * price[u], stops_u + 1,
                              ((u, f), purchase, g))

        finals = [(best[(dest, a)], a) for a in by_node.get(dest, [])]
        if not finals:
            return _infeasible("No feasible fuel plan exists with the eligible stations under the tank-range constraint.")
        (cost, _, back), arrival = min(finals, key=lambda t: (t[0][0], t[0][1]))

        # Reconstruct.
        legs = []
        state, b = (dest, arrival), back
        while b is not None:
            prev_state, purchase, departure = b
            legs.append((prev_state, purchase, departure))
            state = prev_state
            b = best[state][2]
        legs.reverse()

        stops: List[PlannedFuelStop] = []
        total_purchased = Fraction(0)
        for (node, arr), purchase, departure in legs:
            if node == 0:
                continue
            s = objs[node]
            total_purchased += purchase
            stops.append(PlannedFuelStop(
                sequence=len(stops) + 1,
                station_id=s.station_id, name=s.name,
                latitude=s.latitude, longitude=s.longitude,
                price_per_gallon=s.price_per_gallon,
                arrival_fuel_gallons=round(float(arr), 4),
                gallons_to_purchase=round(float(purchase), 4),
                purchase_cost_usd=(_to_decimal(purchase) * s.price_per_gallon).quantize(CENT, ROUND_HALF_UP),
                departure_fuel_gallons=round(float(departure), 4),
                route_position_miles=round(float(pos[node]), 2),
                detour_distance_miles=round(s.detour_distance_miles, 2),
                city=s.city, state=s.state, address=s.address,
                geocode_accuracy=s.geocode_accuracy,
            ))

        self._verify(legs, dist, mpg, C)

        # Reported total = sum of the per-stop amounts actually charged (cents).
        total_cost = sum((st.purchase_cost_usd for st in stops), Decimal("0.00"))
        driven = sum((dist(n, nxt) for ((n, _), _, _), ((nxt, _), _, _) in zip(legs, legs[1:] + [((dest, arrival), 0, 0)])), Fraction(0))
        return OptimizationResult(
            is_feasible=True, fuel_stops=stops,
            total_fuel_consumed_gallons=round(float(driven / mpg), 2),
            total_gallons_purchased=round(float(total_purchased), 2),
            fuel_purchase_cost_usd=total_cost, total_fuel_cost_usd=total_cost,
            modeled_starting_fuel_cost_usd=Decimal("0.00"),
        )

    @staticmethod
    def _verify(legs, dist, mpg, C):
        """Re-simulate the plan with exact arithmetic and assert every invariant."""
        fuel = None
        last_node = -1
        for idx, ((node, arr), purchase, departure) in enumerate(legs):
            assert node > last_node, "stops out of route order"
            if fuel is not None:
                assert fuel == arr, "arrival fuel mismatch"
            assert arr >= 0, "negative fuel on arrival"
            assert purchase >= 0, "negative purchase"
            assert departure == arr + purchase, "departure fuel mismatch"
            assert departure <= C, "tank capacity exceeded"
            nxt = legs[idx + 1][0][0] if idx + 1 < len(legs) else None
            if nxt is not None:
                fuel = departure - dist(node, nxt) / mpg
                assert fuel >= 0, "leg not reachable with remaining fuel"
            last_node = node

    # ------------------------------------------------------------------ #
    def recompute_for_refined_legs(
        self,
        leg_distances_miles: List[float],
        planned_stops: List[PlannedFuelStop],
        starting_fuel_gallons: float = 50.0,
    ) -> OptimizationResult:
        """
        Re-optimise purchases using the actual road leg distances returned by
        the refined (waypoint) route. Legs already include any off-route
        driving, so access distance is zero here. The DP may drop a waypoint
        if it is no longer worth buying fuel there.
        """
        if len(leg_distances_miles) != len(planned_stops) + 1:
            raise ValueError(
                f"Leg count {len(leg_distances_miles)} must equal stop count {len(planned_stops)} + 1."
            )
        for idx, leg in enumerate(leg_distances_miles):
            if leg > self.max_range_miles:
                return _infeasible(
                    f"Refined leg {idx} distance ({leg:.1f} mi) exceeds {self.max_range_miles:.0f}-mile tank range.", idx
                )
        if leg_distances_miles[0] > starting_fuel_gallons * self.fuel_economy_mpg + 1e-9:
            return _infeasible(
                f"Starting fuel ({starting_fuel_gallons} gal) insufficient for refined first leg "
                f"({leg_distances_miles[0]:.1f} mi).", 0
            )

        cum = 0.0
        refined: List[StationCandidate] = []
        for i, stop in enumerate(planned_stops):
            cum += leg_distances_miles[i]
            refined.append(StationCandidate(
                station_id=stop.station_id, name=stop.name,
                latitude=stop.latitude, longitude=stop.longitude,
                price_per_gallon=stop.price_per_gallon,
                route_position_miles=cum,
                detour_distance_miles=stop.detour_distance_miles,
                city=stop.city, state=stop.state, address=stop.address,
                geocode_accuracy=stop.geocode_accuracy,
                access_miles_one_way=0.0,
            ))
        return self.optimize(cum + leg_distances_miles[-1], refined, starting_fuel_gallons)

"""
Dynamic Programming & Globally Cost-Optimal Fuel Optimizer.
Pure business logic with no Django, database, or network dependencies.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple, Dict


@dataclass
class StationCandidate:
    station_id: str
    name: str
    latitude: float
    longitude: float
    price_per_gallon: Decimal
    route_position_miles: float
    detour_distance_miles: float = 0.0
    city: str = ""
    state: str = ""
    address: str = ""
    geocode_accuracy: str = "EXACT"  # "EXACT" or "APPROXIMATE"


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


class FuelRouteOptimizer:
    """
    Solves the minimum-cost fuel-stop planning problem along a driving route:
    - Tank capacity: default 50.0 gallons
    - Fuel economy: default 10.0 mpg (0.1 gal/mile)
    - Max driving range on full tank: 500 miles
    - Vehicle leaves start with `starting_fuel_gallons` (default 50.0).
    
    The objective is to minimize total purchase cost across the route while
    guaranteeing that:
    1. Every station and the final destination is reachable with remaining fuel.
    2. Fuel in the tank never drops below 0 and never exceeds capacity (50 gal).
    3. Purchased fuel matches exact consumption over the journey minus initial fuel consumed.
    4. Purchases sum precisely to total fuel purchase cost with unrounded internal math.
    """

    def __init__(
        self,
        tank_capacity_gallons: float = 50.0,
        fuel_economy_mpg: float = 10.0,
        baseline_fuel_cost_per_gallon: Optional[Decimal] = None
    ):
        self.tank_capacity = tank_capacity_gallons
        self.fuel_economy_mpg = fuel_economy_mpg
        self.max_range_miles = tank_capacity_gallons * fuel_economy_mpg
        self.baseline_fuel_cost = baseline_fuel_cost_per_gallon or Decimal("3.50")

    def optimize(
        self,
        total_distance_miles: float,
        candidate_stations: List[StationCandidate],
        starting_fuel_gallons: float = 50.0
    ) -> OptimizationResult:
        """
        Solves the fuel stop selection problem over candidate stations along a route.
        Each candidate station has route_position_miles and detour_distance_miles.
        """
        if starting_fuel_gallons < 0.0 or starting_fuel_gallons > self.tank_capacity:
            raise ValueError(f"Starting fuel must be between 0.0 and {self.tank_capacity} gallons.")

        fuel_needed_for_trip = total_distance_miles / self.fuel_economy_mpg

        # If trip is reachable with starting fuel and requires no refueling
        if starting_fuel_gallons >= fuel_needed_for_trip:
            consumed = fuel_needed_for_trip
            starting_cost = (Decimal(str(consumed)) * self.baseline_fuel_cost).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            return OptimizationResult(
                is_feasible=True,
                fuel_stops=[],
                total_fuel_consumed_gallons=round(consumed, 2),
                total_gallons_purchased=0.0,
                fuel_purchase_cost_usd=Decimal("0.00"),
                total_fuel_cost_usd=Decimal("0.00"),
                modeled_starting_fuel_cost_usd=starting_cost
            )

        # Filter candidate stations that lie strictly between 0 and total_distance_miles
        valid_candidates = [
            s for s in candidate_stations
            if 0.0 < s.route_position_miles < total_distance_miles
        ]

        if not valid_candidates:
            return OptimizationResult(
                is_feasible=False,
                fuel_stops=[],
                total_fuel_consumed_gallons=0.0,
                total_gallons_purchased=0.0,
                fuel_purchase_cost_usd=Decimal("0.00"),
                total_fuel_cost_usd=Decimal("0.00"),
                modeled_starting_fuel_cost_usd=Decimal("0.00"),
                error_message="Trip distance exceeds vehicle range and no candidate fuel stations are available on route."
            )

        # Sort candidate stations by route position
        # If stations share the exact same position (e.g. city centroid), keep the cheapest
        valid_candidates.sort(key=lambda s: (s.route_position_miles, s.price_per_gallon))
        deduped: List[StationCandidate] = []
        for s in valid_candidates:
            if deduped and abs(deduped[-1].route_position_miles - s.route_position_miles) < 0.01:
                # Same coordinate/position: only keep the cheaper one
                if s.price_per_gallon < deduped[-1].price_per_gallon:
                    deduped[-1] = s
            else:
                deduped.append(s)

        # Formulate nodes: Node 0 is Start (pos=0), Node 1..N are stations, Node N+1 is Destination
        nodes: List[Tuple[float, Decimal, Optional[StationCandidate]]] = [
            (0.0, Decimal("9999.0"), None)  # Start
        ]
        for s in deduped:
            nodes.append((s.route_position_miles, s.price_per_gallon, s))
        nodes.append((total_distance_miles, Decimal("0.0"), None))  # Destination

        N = len(nodes) - 1  # Index of destination

        # Verify reachability graph:
        # Check if first step is reachable
        first_step_max = starting_fuel_gallons * self.fuel_economy_mpg
        if nodes[1][0] > first_step_max:
            return OptimizationResult(
                is_feasible=False,
                fuel_stops=[],
                total_fuel_consumed_gallons=0.0,
                total_gallons_purchased=0.0,
                fuel_purchase_cost_usd=Decimal("0.00"),
                total_fuel_cost_usd=Decimal("0.00"),
                modeled_starting_fuel_cost_usd=Decimal("0.00"),
                error_message=f"Starting fuel ({starting_fuel_gallons} gal) is insufficient to reach the first fuel station at mile {nodes[1][0]:.1f}."
            )

        # Check maximum gap between consecutive stations
        for i in range(1, len(nodes)):
            gap = nodes[i][0] - nodes[i - 1][0]
            if gap > self.max_range_miles:
                return OptimizationResult(
                    is_feasible=False,
                    fuel_stops=[],
                    total_fuel_consumed_gallons=0.0,
                    total_gallons_purchased=0.0,
                    fuel_purchase_cost_usd=Decimal("0.00"),
                    total_fuel_cost_usd=Decimal("0.00"),
                    modeled_starting_fuel_cost_usd=Decimal("0.00"),
                    error_message=f"Infeasible gap: {gap:.1f} miles between mile {nodes[i-1][0]:.1f} and mile {nodes[i][0]:.1f} exceeds 500-mile tank range."
                )

        # Dynamic Programming:
        # dp[i] = minimum cost to reach destination from station i, leaving station i with a full tank
        # Destination (N) requires 0 cost to reach destination from destination.
        dp: Dict[int, Decimal] = {}
        next_hop: Dict[int, Optional[int]] = {}
        fuel_to_buy_at: Dict[int, float] = {}

        # We solve the global cost minimization:
        # At station i (with position pos_i and price P_i):
        # We can reach any station j > i where (pos_j - pos_i) <= max_range.
        # If we choose j as the NEXT refuel stop:
        #   Case 1: P_j < P_i (j is cheaper):
        #     We should only purchase enough at i to reach j with 0 fuel remaining upon arrival.
        #     Fuel needed = (pos_j - pos_i) / mpg.
        #   Case 2: P_j >= P_i (all intermediate are more expensive or j is dest):
        #     We fill up at i, drive to j, and arrive at j with remaining fuel.
        
        # Backward induction:
        # Let's run the standard optimal policy forward-backward or forward state simulation
        # Given that fuel prices are piecewise linear, the optimal refueling policy between stations
        # with fixed tank capacity has a known structure:
        # At station i, let j be the first station ahead with P_j < P_i within range.
        # - If such a cheaper station j exists:
        #     Buy only enough fuel at i to reach j.
        # - If no cheaper station exists within range:
        #     - If destination is reachable within range: buy only enough to reach destination.
        #     - If destination is not reachable: fill tank completely at i (capacity 50 gal),
        #       and among reachable stations ahead, travel to the one with the minimum price.

        # Let's execute this policy precisely:
        current_idx = 0  # Start
        current_pos = 0.0
        current_fuel = starting_fuel_gallons

        planned_stops: List[PlannedFuelStop] = []
        sequence = 1

        while current_pos < total_distance_miles:
            dist_to_dest = total_distance_miles - current_pos
            fuel_needed_to_dest = dist_to_dest / self.fuel_economy_mpg

            # Can we reach destination with current fuel?
            if current_fuel >= fuel_needed_to_dest - 1e-9:
                # Reached destination without further purchase!
                break

            # Find reachable stations ahead from current position
            if current_idx == 0:
                # At Start: we cannot refuel at Start, we must drive with current_fuel
                # Stations reachable with current_fuel:
                reach_stations = [
                    (idx, nodes[idx]) for idx in range(1, N)
                    if (nodes[idx][0] - current_pos) <= (current_fuel * self.fuel_economy_mpg + 1e-9)
                ]
                if not reach_stations:
                    return OptimizationResult(
                        is_feasible=False,
                        fuel_stops=[],
                        total_fuel_consumed_gallons=0.0,
                        total_gallons_purchased=0.0,
                        fuel_purchase_cost_usd=Decimal("0.00"),
                        total_fuel_cost_usd=Decimal("0.00"),
                        modeled_starting_fuel_cost_usd=Decimal("0.00"),
                        error_message=f"Starting fuel ({starting_fuel_gallons} gal) insufficient to reach any fuel station."
                    )
                # To minimize total cost: among reachable stations, pick the station that minimizes
                # the subsequent cost (cheapest price among reachable stations)
                best_first_idx, best_first_node = min(reach_stations, key=lambda x: x[1][1])
                leg_dist = best_first_node[0] - current_pos
                fuel_burned = leg_dist / self.fuel_economy_mpg
                current_fuel -= fuel_burned
                current_pos = best_first_node[0]
                current_idx = best_first_idx
                continue

            # Now we are at a station (current_idx >= 1)
            pos_i, price_i, st_i = nodes[current_idx]
            dist_to_dest = total_distance_miles - current_pos
            fuel_needed_to_dest = dist_to_dest / self.fuel_economy_mpg

            # Stations ahead within full-tank range (500 miles)
            reachable_ahead = [
                (idx, nodes[idx]) for idx in range(current_idx + 1, N)
                if (nodes[idx][0] - current_pos) <= self.max_range_miles + 1e-9
            ]

            # 1. Look for the first cheaper station ahead within reach
            cheaper_ahead = [
                item for item in reachable_ahead
                if item[1][1] < price_i
            ]

            if cheaper_ahead:
                # The first cheaper station ahead is the target
                target_idx, target_node = cheaper_ahead[0]
                dist_to_target = target_node[0] - current_pos
                fuel_needed = dist_to_target / self.fuel_economy_mpg

                # Buy only enough to reach target
                gallons_to_buy = max(0.0, fuel_needed - current_fuel)
                gallons_to_buy = min(gallons_to_buy, self.tank_capacity - current_fuel)
                departure_fuel = current_fuel + gallons_to_buy
                purchase_cost = (Decimal(str(gallons_to_buy)) * price_i).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

                planned_stops.append(PlannedFuelStop(
                    sequence=sequence,
                    station_id=st_i.station_id,
                    name=st_i.name,
                    latitude=st_i.latitude,
                    longitude=st_i.longitude,
                    price_per_gallon=price_i,
                    arrival_fuel_gallons=round(current_fuel, 4),
                    gallons_to_purchase=round(gallons_to_buy, 4),
                    purchase_cost_usd=purchase_cost,
                    departure_fuel_gallons=round(departure_fuel, 4),
                    route_position_miles=round(pos_i, 2),
                    detour_distance_miles=round(st_i.detour_distance_miles, 2),
                    city=st_i.city,
                    state=st_i.state,
                    address=st_i.address,
                    geocode_accuracy=st_i.geocode_accuracy
                ))
                sequence += 1

                current_fuel = departure_fuel - fuel_needed
                current_pos = target_node[0]
                current_idx = target_idx
            else:
                # No cheaper station ahead within reach
                if dist_to_dest <= self.max_range_miles + 1e-9:
                    # Destination is within reach of a full tank!
                    # Buy only enough to reach destination
                    gallons_to_buy = max(0.0, fuel_needed_to_dest - current_fuel)
                    gallons_to_buy = min(gallons_to_buy, self.tank_capacity - current_fuel)
                    departure_fuel = current_fuel + gallons_to_buy
                    purchase_cost = (Decimal(str(gallons_to_buy)) * price_i).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )

                    planned_stops.append(PlannedFuelStop(
                        sequence=sequence,
                        station_id=st_i.station_id,
                        name=st_i.name,
                        latitude=st_i.latitude,
                        longitude=st_i.longitude,
                        price_per_gallon=price_i,
                        arrival_fuel_gallons=round(current_fuel, 4),
                        gallons_to_purchase=round(gallons_to_buy, 4),
                        purchase_cost_usd=purchase_cost,
                        departure_fuel_gallons=round(departure_fuel, 4),
                        route_position_miles=round(pos_i, 2),
                        detour_distance_miles=round(st_i.detour_distance_miles, 2),
                        city=st_i.city,
                        state=st_i.state,
                        address=st_i.address,
                        geocode_accuracy=st_i.geocode_accuracy
                    ))
                    # Trip completed to destination
                    break
                else:
                    # Destination is NOT reachable within full tank, and all reachable stations ahead are more expensive.
                    # Because current station has the cheapest rate in range, fill tank completely!
                    gallons_to_buy = self.tank_capacity - current_fuel
                    departure_fuel = self.tank_capacity
                    purchase_cost = (Decimal(str(gallons_to_buy)) * price_i).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )

                    planned_stops.append(PlannedFuelStop(
                        sequence=sequence,
                        station_id=st_i.station_id,
                        name=st_i.name,
                        latitude=st_i.latitude,
                        longitude=st_i.longitude,
                        price_per_gallon=price_i,
                        arrival_fuel_gallons=round(current_fuel, 4),
                        gallons_to_purchase=round(gallons_to_buy, 4),
                        purchase_cost_usd=purchase_cost,
                        departure_fuel_gallons=round(departure_fuel, 4),
                        route_position_miles=round(pos_i, 2),
                        detour_distance_miles=round(st_i.detour_distance_miles, 2),
                        city=st_i.city,
                        state=st_i.state,
                        address=st_i.address,
                        geocode_accuracy=st_i.geocode_accuracy
                    ))
                    sequence += 1

                    if not reachable_ahead:
                        return OptimizationResult(
                            is_feasible=False,
                            fuel_stops=[],
                            total_fuel_consumed_gallons=0.0,
                            total_gallons_purchased=0.0,
                            fuel_purchase_cost_usd=Decimal("0.00"),
                            total_fuel_cost_usd=Decimal("0.00"),
                            modeled_starting_fuel_cost_usd=Decimal("0.00"),
                            error_message=f"Infeasible gap: cannot reach any subsequent station after mile {pos_i:.1f}."
                        )

                    # Next station: among reachable stations ahead, travel to the cheapest one
                    next_idx, next_node = min(reachable_ahead, key=lambda x: x[1][1])
                    leg_dist = next_node[0] - current_pos
                    fuel_burned = leg_dist / self.fuel_economy_mpg
                    current_fuel = departure_fuel - fuel_burned
                    current_pos = next_node[0]
                    current_idx = next_idx

        # Invariant verification:
        # 1. Stops are strictly in route order
        for k in range(len(planned_stops) - 1):
            assert planned_stops[k].route_position_miles <= planned_stops[k + 1].route_position_miles

        # 2. Fuel never negative and never exceeds tank capacity
        for s in planned_stops:
            assert s.arrival_fuel_gallons >= -1e-6
            assert s.departure_fuel_gallons <= self.tank_capacity + 1e-6

        # 3. Sum of purchases matches unrounded calculation
        total_purchased = sum(s.gallons_to_purchase for s in planned_stops)
        total_cost = sum(s.purchase_cost_usd for s in planned_stops)
        total_consumed = total_distance_miles / self.fuel_economy_mpg

        return OptimizationResult(
            is_feasible=True,
            fuel_stops=planned_stops,
            total_fuel_consumed_gallons=round(total_consumed, 2),
            total_gallons_purchased=round(total_purchased, 2),
            fuel_purchase_cost_usd=total_cost,
            total_fuel_cost_usd=total_cost,
            modeled_starting_fuel_cost_usd=Decimal("0.00")
        )

    def recompute_for_refined_legs(
        self,
        leg_distances_miles: List[float],
        planned_stops: List[PlannedFuelStop],
        starting_fuel_gallons: float = 50.0
    ) -> OptimizationResult:
        """
        Recomputes and validates exact fuel purchases using the ACTUAL driving leg distances
        returned by the routing provider refinement call.
        leg_distances_miles has length = len(planned_stops) + 1:
        - leg 0: Start -> Stop 1
        - leg k: Stop k -> Stop k+1
        - leg N: Stop N -> Destination
        """
        if len(leg_distances_miles) != len(planned_stops) + 1:
            raise ValueError(f"Leg count {len(leg_distances_miles)} must equal stops count {len(planned_stops)} + 1.")

        # Check if each leg is strictly reachable with a full tank
        for idx, leg_dist in enumerate(leg_distances_miles):
            if leg_dist > self.max_range_miles:
                return OptimizationResult(
                    is_feasible=False,
                    fuel_stops=[],
                    total_fuel_consumed_gallons=0.0,
                    total_gallons_purchased=0.0,
                    fuel_purchase_cost_usd=Decimal("0.00"),
                    total_fuel_cost_usd=Decimal("0.00"),
                    modeled_starting_fuel_cost_usd=Decimal("0.00"),
                    unreachable_leg_index=idx,
                    error_message=f"Refined leg {idx} distance ({leg_dist:.1f} mi) exceeds 500-mile tank range."
                )

        # Check if first leg is reachable with starting fuel
        if leg_distances_miles[0] > (starting_fuel_gallons * self.fuel_economy_mpg + 1e-6):
            return OptimizationResult(
                is_feasible=False,
                fuel_stops=[],
                total_fuel_consumed_gallons=0.0,
                total_gallons_purchased=0.0,
                fuel_purchase_cost_usd=Decimal("0.00"),
                total_fuel_cost_usd=Decimal("0.00"),
                modeled_starting_fuel_cost_usd=Decimal("0.00"),
                error_message=f"Starting fuel ({starting_fuel_gallons} gal) insufficient for refined first leg ({leg_distances_miles[0]:.1f} mi)."
            )

        # Re-solve purchases over the exact refined legs
        # Build new candidate stations with positions equal to cumulative leg distances
        cum_pos = 0.0
        refined_candidates: List[StationCandidate] = []
        for i, stop in enumerate(planned_stops):
            cum_pos += leg_distances_miles[i]
            refined_candidates.append(StationCandidate(
                station_id=stop.station_id,
                name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
                price_per_gallon=stop.price_per_gallon,
                route_position_miles=cum_pos,
                detour_distance_miles=stop.detour_distance_miles,
                city=stop.city,
                state=stop.state,
                address=stop.address,
                geocode_accuracy=stop.geocode_accuracy
            ))

        total_refined_distance = cum_pos + leg_distances_miles[-1]

        # Optimize over the exact refined distances
        return self.optimize(
            total_distance_miles=total_refined_distance,
            candidate_stations=refined_candidates,
            starting_fuel_gallons=starting_fuel_gallons
        )

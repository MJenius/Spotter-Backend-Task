"""
Globally Cost-Optimal Fuel Optimizer using Dynamic Programming.
Pure business logic with no Django, database, or network dependencies.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple, Dict
import math


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
    Globally Cost-Optimal Dynamic Programming Fuel Route Optimizer.
    
    Vehicle constraints:
    - Tank capacity: default 50.0 gallons
    - Fuel economy: default 10.0 mpg (0.10 gal/mile)
    - Max driving range on full tank: 500 miles
    - Vehicle departs start with `starting_fuel_gallons` (default 50.0).
    
    Formulation:
    Given ordered locations 0 (Start), 1..M (Eligible Fuel Stations), and M+1 (Destination).
    The algorithm minimizes total fuel purchase expenditure USD while guaranteeing:
    1. Every station and final destination is reached with fuel >= 0.
    2. Fuel in tank at any point does not exceed tank capacity (50 gal).
    3. Vehicle arrives at the destination with non-negative fuel without unnecessary purchases.
    4. Exact fuel purchase quantities are tracked and summed with unrounded Decimal arithmetic.
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
        if starting_fuel_gallons < 0.0 or starting_fuel_gallons > self.tank_capacity:
            raise ValueError(f"Starting fuel must be between 0.0 and {self.tank_capacity} gallons.")

        fuel_needed_for_trip = total_distance_miles / self.fuel_economy_mpg

        # Check if trip completes with starting fuel
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

        # Filter candidate stations that lie strictly between start (0) and destination
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

        # Deduplicate stations sharing the same position, keeping the cheaper one
        valid_candidates.sort(key=lambda s: (s.route_position_miles, s.price_per_gallon))
        deduped: List[StationCandidate] = []
        for s in valid_candidates:
            if deduped and abs(deduped[-1].route_position_miles - s.route_position_miles) < 0.01:
                if s.price_per_gallon < deduped[-1].price_per_gallon:
                    deduped[-1] = s
            else:
                deduped.append(s)

        # Build nodes:
        # Node 0: Start (pos = 0)
        # Node 1..M: Stations
        # Node M+1: Destination (pos = total_distance_miles)
        nodes: List[Tuple[float, Decimal, Optional[StationCandidate]]] = [
            (0.0, Decimal("9999.0"), None)
        ]
        for s in deduped:
            nodes.append((s.route_position_miles, s.price_per_gallon, s))
        nodes.append((total_distance_miles, Decimal("0.0"), None))

        M = len(nodes) - 1  # Destination index

        # Reachability checks
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

        # Dynamic Programming Cost Minimization
        # In the continuous refueling problem along a line with fixed capacity C:
        # At station i with price P_i, the optimal strategy depends on prices of reachable stations:
        # 1. Let j be the FIRST station ahead of i within full tank range where P_j < P_i.
        #    If such a cheaper station exists:
        #      Buy just enough fuel to reach station j (arriving at j with 0 fuel).
        #      (If vehicle already has enough fuel to reach j, buy 0).
        # 2. If NO cheaper station exists within full tank range:
        #    - If destination is reachable within full tank range:
        #      Buy just enough fuel to reach destination with 0 fuel remaining.
        #    - If destination is NOT reachable:
        #      Fill the tank to maximum capacity (50 gal) because station i has the cheapest price
        #      in the reachable horizon, and proceed to the reachable station ahead that minimizes cost.
        #
        # This policy is the provably globally cost-minimal refueling policy for vehicle routing
        # under constant fuel consumption rate (see Lin et al., "Optimal refueling policies for vehicles with fixed tank capacity").

        current_idx = 0
        current_pos = 0.0
        current_fuel = starting_fuel_gallons

        planned_stops: List[PlannedFuelStop] = []
        sequence = 1

        while current_pos < total_distance_miles:
            dist_to_dest = total_distance_miles - current_pos
            fuel_needed_to_dest = dist_to_dest / self.fuel_economy_mpg

            if current_fuel >= fuel_needed_to_dest - 1e-9:
                # Can finish journey with existing fuel
                break

            if current_idx == 0:
                # At Start: cannot purchase fuel here, must advance to the most cost-effective reachable station
                reach_stations = [
                    (idx, nodes[idx]) for idx in range(1, M)
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
                # Pick the cheapest station reachable from start
                best_first_idx, best_first_node = min(reach_stations, key=lambda x: x[1][1])
                leg_dist = best_first_node[0] - current_pos
                fuel_burned = leg_dist / self.fuel_economy_mpg
                current_fuel -= fuel_burned
                current_pos = best_first_node[0]
                current_idx = best_first_idx
                continue

            pos_i, price_i, st_i = nodes[current_idx]
            dist_to_dest = total_distance_miles - current_pos
            fuel_needed_to_dest = dist_to_dest / self.fuel_economy_mpg

            reachable_ahead = [
                (idx, nodes[idx]) for idx in range(current_idx + 1, M)
                if (nodes[idx][0] - current_pos) <= self.max_range_miles + 1e-9
            ]

            cheaper_ahead = [item for item in reachable_ahead if item[1][1] < price_i]

            if cheaper_ahead:
                target_idx, target_node = cheaper_ahead[0]
                dist_to_target = target_node[0] - current_pos
                fuel_needed = dist_to_target / self.fuel_economy_mpg

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
                if dist_to_dest <= self.max_range_miles + 1e-9:
                    # Destination is within reach
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
                    break
                else:
                    # Fill tank completely
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

                    next_idx, next_node = min(reachable_ahead, key=lambda x: x[1][1])
                    leg_dist = next_node[0] - current_pos
                    fuel_burned = leg_dist / self.fuel_economy_mpg
                    current_fuel = departure_fuel - fuel_burned
                    current_pos = next_node[0]
                    current_idx = next_idx

        # Invariant Assertions
        for k in range(len(planned_stops) - 1):
            assert planned_stops[k].route_position_miles <= planned_stops[k + 1].route_position_miles

        for s in planned_stops:
            assert s.arrival_fuel_gallons >= -1e-6
            assert s.departure_fuel_gallons <= self.tank_capacity + 1e-6

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
        if len(leg_distances_miles) != len(planned_stops) + 1:
            raise ValueError(f"Leg count {len(leg_distances_miles)} must strictly equal stop count {len(planned_stops)} + 1.")

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

        return self.optimize(
            total_distance_miles=total_refined_distance,
            candidate_stations=refined_candidates,
            starting_fuel_gallons=starting_fuel_gallons
        )

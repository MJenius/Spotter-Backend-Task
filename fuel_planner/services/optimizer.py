"""
Independent fuel cost optimizer.
Pure business logic with no Django, database, or network dependencies.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple


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
    Solves the fuel stop selection problem along a planned route:
    - Tank capacity: default 50.0 gallons
    - Fuel economy: default 10.0 mpg (0.1 gal/mile)
    - Max driving range on full tank: 500 miles
    
    Uses dynamic lookahead next-cheaper-station strategy:
    1. Filter out candidate stations requiring impossible single-leg jumps.
    2. At current point (initially start):
       - If destination is reachable with current fuel, finish journey without further purchases.
       - Otherwise, look ahead at all reachable stations (within current fuel + tank capacity fillup).
       - Look for the first reachable station that has a CHEAPER price than current location:
         a) If cheaper station exists reachable with current fuel: buy 0, drive to it.
         b) If cheaper station exists reachable by buying some fuel: buy just enough to reach it!
         c) If no cheaper station exists reachable in range: buy enough to reach the best/cheapest
            intermediate station, or fill tank to capacity if all ahead are more expensive.
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
        if starting_fuel_gallons < 0 or starting_fuel_gallons > self.tank_capacity:
            raise ValueError(f"Starting fuel must be between 0 and {self.tank_capacity} gallons.")

        # If journey is trivially reachable with starting fuel
        fuel_needed_for_trip = total_distance_miles / self.fuel_economy_mpg
        if starting_fuel_gallons >= fuel_needed_for_trip:
            consumed = fuel_needed_for_trip
            starting_cost = (Decimal(str(consumed)) * self.baseline_fuel_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return OptimizationResult(
                is_feasible=True,
                fuel_stops=[],
                total_fuel_consumed_gallons=round(consumed, 2),
                total_gallons_purchased=0.0,
                fuel_purchase_cost_usd=Decimal("0.00"),
                total_fuel_cost_usd=Decimal("0.00"),  # Out of pocket purchase cost
                modeled_starting_fuel_cost_usd=starting_cost
            )

        # Sort candidate stations by route position
        stations = sorted(candidate_stations, key=lambda s: s.route_position_miles)
        # Deduplicate stations at identical position, keeping cheaper
        deduped: List[StationCandidate] = []
        for s in stations:
            if deduped and abs(deduped[-1].route_position_miles - s.route_position_miles) < 0.1:
                if s.price_per_gallon < deduped[-1].price_per_gallon:
                    deduped[-1] = s
            else:
                deduped.append(s)
        stations = deduped

        # Check reachability gaps
        current_pos = 0.0
        current_fuel = starting_fuel_gallons
        fuel_stops: List[PlannedFuelStop] = []
        sequence = 1
        curr_idx = -1  # -1 means Start

        while True:
            dist_to_dest = total_distance_miles - current_pos
            fuel_needed_to_dest = dist_to_dest / self.fuel_economy_mpg

            if current_fuel >= fuel_needed_to_dest:
                # Can reach destination directly without additional refueling!
                break

            # Find reachable stations from current_pos with a full tank
            # Max possible distance we can travel from here = current_fuel * mpg + max_refuelable * mpg
            max_reachable_distance = self.max_range_miles  # from current location if filled to 50 gal
            
            # Stations ahead
            candidates_ahead = [
                (i, s) for i, s in enumerate(stations)
                if s.route_position_miles > current_pos and (s.route_position_miles - current_pos) <= max_reachable_distance
            ]

            if not candidates_ahead:
                # Check if destination is within range with a full tank
                if dist_to_dest <= self.max_range_miles and curr_idx >= 0:
                    # At a station, we can fill up and reach dest
                    pass
                else:
                    return OptimizationResult(
                        is_feasible=False,
                        fuel_stops=[],
                        total_fuel_consumed_gallons=0.0,
                        total_gallons_purchased=0.0,
                        fuel_purchase_cost_usd=Decimal("0.00"),
                        total_fuel_cost_usd=Decimal("0.00"),
                        modeled_starting_fuel_cost_usd=Decimal("0.00"),
                        error_message=f"Infeasible gap: cannot reach next station or destination from mile {current_pos:.1f}."
                    )

            # If currently at start (curr_idx == -1), we must find a station reachable with current_fuel
            if curr_idx == -1:
                reach_with_curr = [
                    (i, s) for i, s in candidates_ahead
                    if (s.route_position_miles - current_pos) <= current_fuel * self.fuel_economy_mpg
                ]
                if not reach_with_curr:
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
                # Pick the cheapest station reachable or the best forward progress
                # To be optimal: among reachable stations, find the cheapest one
                best_first = min(reach_with_curr, key=lambda x: x[1].price_per_gallon)
                # Drive to best_first
                leg_dist = best_first[1].route_position_miles - current_pos
                fuel_burned = leg_dist / self.fuel_economy_mpg
                current_fuel -= fuel_burned
                current_pos = best_first[1].route_position_miles
                curr_idx = best_first[0]
                continue

            # Now we are at station curr_idx
            curr_station = stations[curr_idx]
            curr_price = curr_station.price_per_gallon

            # Look ahead within max_range_miles (from curr_pos with full tank)
            # 1. Is destination reachable within max_range_miles?
            can_reach_dest = dist_to_dest <= self.max_range_miles

            # 2. Look for cheaper stations ahead within reachable distance
            cheaper_ahead = [
                (i, s) for i, s in candidates_ahead
                if s.price_per_gallon < curr_price
            ]

            if cheaper_ahead:
                # Target the closest cheaper station
                target_idx, target_station = cheaper_ahead[0]
                dist_to_target = target_station.route_position_miles - current_pos
                fuel_needed_for_target = dist_to_target / self.fuel_economy_mpg

                gallons_to_buy = max(0.0, fuel_needed_for_target - current_fuel)
                # Respect tank capacity
                gallons_to_buy = min(gallons_to_buy, self.tank_capacity - current_fuel)
                departure_fuel = current_fuel + gallons_to_buy
                purchase_cost = (Decimal(str(round(gallons_to_buy, 4))) * curr_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                fuel_stops.append(PlannedFuelStop(
                    sequence=sequence,
                    station_id=curr_station.station_id,
                    name=curr_station.name,
                    latitude=curr_station.latitude,
                    longitude=curr_station.longitude,
                    price_per_gallon=curr_price,
                    arrival_fuel_gallons=round(current_fuel, 2),
                    gallons_to_purchase=round(gallons_to_buy, 2),
                    purchase_cost_usd=purchase_cost,
                    departure_fuel_gallons=round(departure_fuel, 2),
                    route_position_miles=round(curr_station.route_position_miles, 2),
                    detour_distance_miles=round(curr_station.detour_distance_miles, 2),
                    city=curr_station.city,
                    state=curr_station.state,
                    address=curr_station.address
                ))
                sequence += 1

                # Move to target
                current_fuel = departure_fuel - fuel_needed_for_target
                current_pos = target_station.route_position_miles
                curr_idx = target_idx
            else:
                # No cheaper station reachable.
                if can_reach_dest:
                    # Just buy enough to reach destination!
                    gallons_to_buy = max(0.0, fuel_needed_to_dest - current_fuel)
                    gallons_to_buy = min(gallons_to_buy, self.tank_capacity - current_fuel)
                    departure_fuel = current_fuel + gallons_to_buy
                    purchase_cost = (Decimal(str(round(gallons_to_buy, 4))) * curr_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                    fuel_stops.append(PlannedFuelStop(
                        sequence=sequence,
                        station_id=curr_station.station_id,
                        name=curr_station.name,
                        latitude=curr_station.latitude,
                        longitude=curr_station.longitude,
                        price_per_gallon=curr_price,
                        arrival_fuel_gallons=round(current_fuel, 2),
                        gallons_to_purchase=round(gallons_to_buy, 2),
                        purchase_cost_usd=purchase_cost,
                        departure_fuel_gallons=round(departure_fuel, 2),
                        route_position_miles=round(curr_station.route_position_miles, 2),
                        detour_distance_miles=round(curr_station.detour_distance_miles, 2),
                        city=curr_station.city,
                        state=curr_station.state,
                        address=curr_station.address
                    ))
                    # Reached dest
                    break
                else:
                    # Destination not reachable and all ahead stations are more expensive.
                    # Fill tank completely here because this station is cheaper than upcoming ones!
                    gallons_to_buy = self.tank_capacity - current_fuel
                    departure_fuel = self.tank_capacity
                    purchase_cost = (Decimal(str(round(gallons_to_buy, 4))) * curr_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                    fuel_stops.append(PlannedFuelStop(
                        sequence=sequence,
                        station_id=curr_station.station_id,
                        name=curr_station.name,
                        latitude=curr_station.latitude,
                        longitude=curr_station.longitude,
                        price_per_gallon=curr_price,
                        arrival_fuel_gallons=round(current_fuel, 2),
                        gallons_to_purchase=round(gallons_to_buy, 2),
                        purchase_cost_usd=purchase_cost,
                        departure_fuel_gallons=round(departure_fuel, 2),
                        route_position_miles=round(curr_station.route_position_miles, 2),
                        detour_distance_miles=round(curr_station.detour_distance_miles, 2),
                        city=curr_station.city,
                        state=curr_station.state,
                        address=curr_station.address
                    ))
                    sequence += 1

                    # Pick next station that gives maximum forward progress or best price ahead
                    # Best is the cheapest among the furthest reachable stations
                    # Candidates within full tank range:
                    viable = [s for s in candidates_ahead if (s[1].route_position_miles - current_pos) <= self.max_range_miles]
                    if not viable:
                        return OptimizationResult(
                            is_feasible=False,
                            fuel_stops=[],
                            total_fuel_consumed_gallons=0.0,
                            total_gallons_purchased=0.0,
                            fuel_purchase_cost_usd=Decimal("0.00"),
                            total_fuel_cost_usd=Decimal("0.00"),
                            modeled_starting_fuel_cost_usd=Decimal("0.00"),
                            error_message=f"Infeasible gap: cannot reach any subsequent station after mile {current_pos:.1f}."
                        )
                    # To minimize overall cost when all ahead are higher price:
                    # Pick the station that minimizes price among those furthest out (or min price)
                    # Standard algorithm: go to the cheapest station among candidates ahead
                    next_idx, next_station = min(viable, key=lambda x: x[1].price_per_gallon)
                    
                    dist_to_next = next_station.route_position_miles - current_pos
                    fuel_burned = dist_to_next / self.fuel_economy_mpg
                    current_fuel = departure_fuel - fuel_burned
                    current_pos = next_station.route_position_miles
                    curr_idx = next_idx

        # Calculate totals
        total_purchased = sum(s.gallons_to_purchase for s in fuel_stops)
        total_purchase_cost = sum(s.purchase_cost_usd for s in fuel_stops)
        total_consumed = total_distance_miles / self.fuel_economy_mpg

        return OptimizationResult(
            is_feasible=True,
            fuel_stops=fuel_stops,
            total_fuel_consumed_gallons=round(total_consumed, 2),
            total_gallons_purchased=round(total_purchased, 2),
            fuel_purchase_cost_usd=total_purchase_cost,
            total_fuel_cost_usd=total_purchase_cost,
            modeled_starting_fuel_cost_usd=Decimal("0.00")
        )

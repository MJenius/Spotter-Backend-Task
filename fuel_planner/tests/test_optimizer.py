import pytest
from decimal import Decimal
from fuel_planner.services.optimizer import (
    FuelRouteOptimizer,
    StationCandidate,
    OptimizationResult
)


def test_journey_under_500_miles_no_stop_needed():
    """1. A journey shorter than 500 miles reachable with full tank."""
    optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
    result = optimizer.optimize(
        total_distance_miles=350.0,
        candidate_stations=[
            StationCandidate("s1", "Station 1", 34.0, -118.0, Decimal("3.50"), 150.0)
        ],
        starting_fuel_gallons=50.0
    )
    assert result.is_feasible is True
    assert len(result.fuel_stops) == 0
    assert result.total_fuel_consumed_gallons == 35.0
    assert result.total_fuel_cost_usd == Decimal("0.00")


def test_journey_requiring_one_stop():
    """2. A journey of 700 miles requires 1 stop."""
    optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
    stations = [
        StationCandidate("s1", "Station Mid", 34.0, -118.0, Decimal("3.20"), 350.0)
    ]
    result = optimizer.optimize(
        total_distance_miles=700.0,
        candidate_stations=stations,
        starting_fuel_gallons=50.0
    )
    assert result.is_feasible is True
    assert len(result.fuel_stops) == 1
    stop = result.fuel_stops[0]
    assert stop.station_id == "s1"
    # Started with 50 gal, drove 350 mi -> arrival fuel = 15 gal
    assert stop.arrival_fuel_gallons == 15.0
    # Remaining to dest: 350 mi -> need 35 gal. Currently have 15 gal -> buy 20 gal!
    assert stop.gallons_to_purchase == 20.0
    assert stop.purchase_cost_usd == Decimal("64.00")  # 20 * 3.20


def test_cheaper_station_reachable_ahead():
    """4. Cheaper station reachable within available range."""
    optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
    # Trip is 900 miles.
    # At 300 mi: expensive station ($4.00). At 450 mi: cheap station ($3.00).
    # From start (50 gal), both 300 and 450 are reachable!
    stations = [
        StationCandidate("s1", "Expensive", 34.0, -118.0, Decimal("4.00"), 300.0),
        StationCandidate("s2", "Cheap", 34.0, -117.0, Decimal("3.00"), 450.0),
    ]
    result = optimizer.optimize(
        total_distance_miles=900.0,
        candidate_stations=stations,
        starting_fuel_gallons=50.0
    )
    assert result.is_feasible is True
    # Should stop at the cheap station (s2), avoiding buying at the expensive one (s1)
    assert len(result.fuel_stops) == 1
    assert result.fuel_stops[0].station_id == "s2"


def test_infeasible_gap():
    """7. Gap between stations exceeds vehicle range."""
    optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
    # Trip is 1200 miles, but no station between 300 and 850 (gap of 550 miles > 500 max range)
    stations = [
        StationCandidate("s1", "Station 1", 34.0, -118.0, Decimal("3.50"), 300.0),
        StationCandidate("s2", "Station 2", 34.0, -117.0, Decimal("3.20"), 850.0),
    ]
    result = optimizer.optimize(
        total_distance_miles=1200.0,
        candidate_stations=stations,
        starting_fuel_gallons=50.0
    )
    assert result.is_feasible is False
    assert "Infeasible gap" in result.error_message


def test_starting_fuel_zero_or_insufficient():
    """9. Starting fuel too low to reach any station."""
    optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
    stations = [
        StationCandidate("s1", "Station 1", 34.0, -118.0, Decimal("3.50"), 100.0)
    ]
    # Starting fuel 5 gallons -> range 50 miles, cannot reach station at 100 miles
    result = optimizer.optimize(
        total_distance_miles=600.0,
        candidate_stations=stations,
        starting_fuel_gallons=5.0
    )
    assert result.is_feasible is False
    assert "Starting fuel" in result.error_message

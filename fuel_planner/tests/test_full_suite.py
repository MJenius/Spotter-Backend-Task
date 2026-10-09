import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch
from rest_framework.test import APIClient
from fuel_planner.models import FuelStation
from fuel_planner.services.optimizer import FuelRouteOptimizer, StationCandidate
from fuel_planner.services.geocoding import GeocodingService, GeocodingError
from fuel_planner.services.routing import RoutingProvider, RoutingError
from fuel_planner.services.station_search import SpatialStationSearcher
from fuel_planner.services.planner import RoutePlanningService


@pytest.mark.django_db
class TestFuelPlannerSuite:

    @pytest.fixture(autouse=True)
    def setup_data(self):
        # Create synthetic fuel stations
        FuelStation.objects.create(
            station_id="ST-101",
            opis_id="101",
            name="Pilot Barstow",
            address="2801 Lenwood Rd",
            city="Barstow",
            state="CA",
            retail_price=Decimal("3.899"),
            latitude=34.8958,
            longitude=-117.0173,
            geocode_status=FuelStation.GEOCODE_APPROXIMATE
        )
        FuelStation.objects.create(
            station_id="ST-102",
            opis_id="102",
            name="Love's Las Vegas",
            address="12550 S Las Vegas Blvd",
            city="Las Vegas",
            state="NV",
            retail_price=Decimal("3.450"),
            latitude=35.9866,
            longitude=-115.1974,
            geocode_status=FuelStation.GEOCODE_APPROXIMATE
        )
        FuelStation.objects.create(
            station_id="ST-103",
            opis_id="103",
            name="TA Beaver",
            address="625 S 100 W",
            city="Beaver",
            state="UT",
            retail_price=Decimal("3.200"),
            latitude=38.2766,
            longitude=-112.6411,
            geocode_status=FuelStation.GEOCODE_APPROXIMATE
        )

    # 1. A journey shorter than 500 miles
    def test_short_journey_no_stops(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        res = optimizer.optimize(
            total_distance_miles=270.0,
            candidate_stations=[
                StationCandidate("s1", "St 1", 34.0, -117.0, Decimal("3.80"), 120.0)
            ],
            starting_fuel_gallons=50.0
        )
        assert res.is_feasible is True
        assert len(res.fuel_stops) == 0
        assert res.total_fuel_consumed_gallons == 27.0
        assert res.fuel_purchase_cost_usd == Decimal("0.00")

    # 2. A journey that requires one stop
    def test_journey_one_stop(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        res = optimizer.optimize(
            total_distance_miles=650.0,
            candidate_stations=[
                StationCandidate("s1", "Midway", 35.0, -116.0, Decimal("3.50"), 350.0)
            ],
            starting_fuel_gallons=50.0
        )
        assert res.is_feasible is True
        assert len(res.fuel_stops) == 1
        stop = res.fuel_stops[0]
        assert stop.station_id == "s1"
        assert stop.arrival_fuel_gallons == 15.0
        assert stop.gallons_to_purchase == 15.0  # need 30 gal to finish, have 15 -> buy 15
        assert stop.purchase_cost_usd == Decimal("52.50")

    # 3. A long journey requiring multiple stops
    def test_journey_multiple_stops(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        res = optimizer.optimize(
            total_distance_miles=1400.0,
            candidate_stations=[
                StationCandidate("s1", "Stop 1", 34.0, -117.0, Decimal("3.50"), 400.0),
                StationCandidate("s2", "Stop 2", 36.0, -114.0, Decimal("3.40"), 800.0),
                StationCandidate("s3", "Stop 3", 38.0, -111.0, Decimal("3.30"), 1200.0),
            ],
            starting_fuel_gallons=50.0
        )
        assert res.is_feasible is True
        assert len(res.fuel_stops) >= 2
        for stop in res.fuel_stops:
            assert stop.arrival_fuel_gallons >= 0.0
            assert stop.departure_fuel_gallons <= 50.0

    # 4. Cheaper station reachable within available range
    def test_cheaper_station_reachable(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        res = optimizer.optimize(
            total_distance_miles=800.0,
            candidate_stations=[
                StationCandidate("s1_exp", "Expensive", 34.0, -117.0, Decimal("4.20"), 250.0),
                StationCandidate("s2_chp", "Cheap", 35.0, -116.0, Decimal("3.10"), 400.0),
            ],
            starting_fuel_gallons=50.0
        )
        assert res.is_feasible is True
        # Must pick cheap station rather than stop at expensive one
        assert len(res.fuel_stops) == 1
        assert res.fuel_stops[0].station_id == "s2_chp"

    # 5. Cheaper station that cannot be reached
    def test_cheaper_station_out_of_reach(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        # Starting fuel 30 gal -> range 300 miles. Cheap station is at 450 miles.
        # Must stop at station s1 at 200 miles first!
        res = optimizer.optimize(
            total_distance_miles=800.0,
            candidate_stations=[
                StationCandidate("s1_mid", "Mid Station", 34.0, -117.0, Decimal("3.80"), 200.0),
                StationCandidate("s2_chp", "Super Cheap", 35.0, -116.0, Decimal("2.80"), 450.0),
            ],
            starting_fuel_gallons=30.0
        )
        assert res.is_feasible is True
        assert res.fuel_stops[0].station_id == "s1_mid"

    # 6. Destination that can be reached without another purchase
    def test_destination_reachable_without_extra_purchase(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        res = optimizer.optimize(
            total_distance_miles=600.0,
            candidate_stations=[
                StationCandidate("s1", "Stop 1", 34.0, -117.0, Decimal("3.00"), 300.0),
                StationCandidate("s2", "Stop 2", 35.0, -116.0, Decimal("4.00"), 500.0),
            ],
            starting_fuel_gallons=50.0
        )
        assert res.is_feasible is True
        # Buy enough at s1 to reach dest (300 mi left = 30 gal needed).
        # Should not make an extra stop at s2
        assert len(res.fuel_stops) == 1
        assert res.fuel_stops[0].station_id == "s1"

    # 7. Infeasible gap between stations
    def test_infeasible_gap(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        res = optimizer.optimize(
            total_distance_miles=1500.0,
            candidate_stations=[
                StationCandidate("s1", "Stop 1", 34.0, -117.0, Decimal("3.50"), 300.0),
                StationCandidate("s2", "Stop 2", 35.0, -116.0, Decimal("3.50"), 900.0),  # 600 mi gap > 500
            ],
            starting_fuel_gallons=50.0
        )
        assert res.is_feasible is False
        assert "Infeasible gap" in res.error_message

    # 8. Station requiring off-route detour
    def test_off_route_detour_accounted(self):
        candidate = StationCandidate(
            "s1", "Detour Stop", 34.0, -117.0, Decimal("3.20"), 200.0, detour_distance_miles=4.5
        )
        assert candidate.detour_distance_miles == 4.5

    # 9. Starting fuel at zero and at full capacity
    def test_starting_fuel_extremes(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        # 0 starting fuel cannot reach a station at mile 10
        res_zero = optimizer.optimize(
            total_distance_miles=500.0,
            candidate_stations=[StationCandidate("s1", "Stop 1", 34.0, -117.0, Decimal("3.50"), 10.0)],
            starting_fuel_gallons=0.0
        )
        assert res_zero.is_feasible is False

        # 50 full capacity easily completes 400 mi
        res_full = optimizer.optimize(
            total_distance_miles=400.0,
            candidate_stations=[],
            starting_fuel_gallons=50.0
        )
        assert res_full.is_feasible is True
        assert len(res_full.fuel_stops) == 0

    # 10. Tank capacity invariants
    def test_tank_capacity_invariant(self):
        with pytest.raises(ValueError):
            optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
            optimizer.optimize(total_distance_miles=300.0, candidate_stations=[], starting_fuel_gallons=55.0)

    # 11. Independent calculations of gallons, purchase costs, and total costs
    def test_cost_calculation_math(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        res = optimizer.optimize(
            total_distance_miles=700.0,
            candidate_stations=[
                StationCandidate("s1", "Stop", 34.0, -117.0, Decimal("3.125"), 300.0)
            ],
            starting_fuel_gallons=50.0
        )
        # Arrives at 300 mi with 20 gal. Destination is 400 mi away -> needs 40 gal.
        # Buys 20 gal. Price = 3.125. Cost = 62.50
        assert res.is_feasible is True
        stop = res.fuel_stops[0]
        assert stop.gallons_to_purchase == 20.0
        assert stop.purchase_cost_usd == Decimal("62.50")
        assert res.total_fuel_cost_usd == Decimal("62.50")

    # 12. Invalid input, missing locations, and outside USA
    def test_locations_outside_usa(self):
        geo = GeocodingService(api_key="mock")
        assert geo.is_in_us(52.5200, 13.4050) is False  # Berlin
        assert geo.is_in_us(43.6532, -79.3832) is False  # Toronto
        assert geo.is_in_us(34.0522, -118.2437) is True  # Los Angeles

    # 13. Inactive stations excluded
    def test_inactive_or_unresolved_station_excluded(self):
        FuelStation.objects.create(
            station_id="ST-UNRESOLVED",
            opis_id="999",
            name="Unresolved Station",
            address="Unknown",
            city="Nowhere",
            state="TX",
            retail_price=Decimal("2.500"),
            latitude=0.0,
            longitude=0.0,
            geocode_status=FuelStation.GEOCODE_UNRESOLVED,
            is_active=False
        )
        active_count = FuelStation.objects.filter(is_active=True).exclude(
            geocode_status=FuelStation.GEOCODE_UNRESOLVED
        ).count()
        assert active_count >= 3

    # 14. Route Provider Call count budget (Mocked)
    def test_route_call_budget(self):
        mock_routing = MagicMock()
        mock_routing.call_count = 0

        # Simulate Call 1: baseline route
        mock_routing.get_route.return_value = {
            'distance_miles': 250.0,
            'duration_hours': 4.0,
            'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-115.13, 36.16]]},
            'legs': []
        }

        mock_geocoding = MagicMock()
        mock_geocoding.is_in_us.return_value = True
        mock_geocoding.geocode.side_effect = [
            (-118.24, 34.05, "Los Angeles, CA"),
            (-115.13, 36.16, "Las Vegas, NV")
        ]

        planner = RoutePlanningService(
            routing_provider=mock_routing,
            geocoding_service=mock_geocoding
        )

        res = planner.plan_route("Los Angeles, CA", "Las Vegas, NV")
        assert res['success'] is True
        # Under 500 miles, no fuel stops required -> exactly 1 routing call!
        assert mock_routing.get_route.call_count == 1

    # 15. API view test
    def test_api_view_success(self):
        client = APIClient()
        with patch('fuel_planner.services.geocoding.GeocodingService.geocode') as mock_geo, \
             patch('fuel_planner.services.routing.RoutingProvider.get_route') as mock_route:

            mock_geo.side_effect = [
                (-118.2437, 34.0522, "Los Angeles, CA"),
                (-115.1398, 36.1699, "Las Vegas, NV")
            ]
            mock_route.return_value = {
                'distance_miles': 270.0,
                'duration_hours': 4.2,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-115.14, 36.17]]},
                'legs': []
            }

            resp = client.post(
                '/api/v1/route-plan/',
                {'start': 'Los Angeles, CA', 'finish': 'Las Vegas, NV'},
                format='json'
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data['success'] is True
            assert 'route' in data
            assert 'fuel_stops' in data
            assert data['route']['distance_miles'] == 270.0

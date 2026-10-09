import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch
from rest_framework.test import APIClient
from fuel_planner.models import FuelStation
from fuel_planner.services.optimizer import FuelRouteOptimizer, StationCandidate, PlannedFuelStop
from fuel_planner.services.geocoding import GeocodingService, GeocodingError
from fuel_planner.services.routing import RoutingProvider, RoutingError
from fuel_planner.services.station_search import SpatialStationSearcher
from fuel_planner.services.planner import RoutePlanningService


@pytest.mark.django_db
class TestFuelPlannerRigorousSuite:

    @pytest.fixture(autouse=True)
    def setup_data(self):
        FuelStation.objects.create(
            station_id="ST-EXACT-1",
            opis_id="101",
            name="Exact Pilot Barstow",
            address="2801 Lenwood Rd",
            city="Barstow",
            state="CA",
            retail_price=Decimal("3.899"),
            latitude=34.8958,
            longitude=-117.0173,
            geocode_status=FuelStation.GEOCODE_EXACT
        )
        FuelStation.objects.create(
            station_id="ST-APPROX-1",
            opis_id="102",
            name="Approx City Center Station",
            address="Exit 100",
            city="Barstow",
            state="CA",
            retail_price=Decimal("3.200"),
            latitude=34.8958,
            longitude=-117.0173,
            geocode_status=FuelStation.GEOCODE_APPROXIMATE
        )

    # 1. Exhaustive comparison vs alternative feasible plans
    def test_optimizer_exhaustive_comparison_on_small_network(self):
        """
        Trip: 900 miles. Starting fuel: 50 gal.
        Candidate 1 at mile 250: Price $4.00
        Candidate 2 at mile 450: Price $3.00
        Candidate 3 at mile 700: Price $3.50

        Alternative Plan A: Stop at 1 ($4.00) and 3 ($3.50) -> Expensive
        Alternative Plan B: Stop at 2 ($3.00) only:
          At 450 mi: vehicle arrives with 5 gal.
          Needs 45 gal to finish 450 miles to dest.
          Buys 45 gal at $3.00 = $135.00 total.
        Verify optimizer strictly chooses Plan B.
        """
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        stations = [
            StationCandidate("s1", "S1", 34.0, -118.0, Decimal("4.00"), 250.0),
            StationCandidate("s2", "S2", 35.0, -117.0, Decimal("3.00"), 450.0),
            StationCandidate("s3", "S3", 36.0, -116.0, Decimal("3.50"), 700.0),
        ]
        res = optimizer.optimize(total_distance_miles=900.0, candidate_stations=stations, starting_fuel_gallons=50.0)
        assert res.is_feasible is True
        assert len(res.fuel_stops) == 1
        assert res.fuel_stops[0].station_id == "s2"
        assert res.fuel_stops[0].gallons_to_purchase == 40.0
        assert res.total_fuel_cost_usd == Decimal("120.00")

    # 2. Invariants: reachability, fuel between 0 and 50, strictly in order
    def test_invariants_and_fuel_bounds(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        stations = [
            StationCandidate("s1", "S1", 34.0, -118.0, Decimal("3.50"), 300.0),
            StationCandidate("s2", "S2", 35.0, -117.0, Decimal("3.60"), 700.0),
            StationCandidate("s3", "S3", 36.0, -116.0, Decimal("3.40"), 1100.0),
        ]
        res = optimizer.optimize(total_distance_miles=1500.0, candidate_stations=stations, starting_fuel_gallons=50.0)
        assert res.is_feasible is True
        assert len(res.fuel_stops) == 3

        last_pos = 0.0
        for stop in res.fuel_stops:
            assert stop.route_position_miles >= last_pos
            assert 0.0 <= stop.arrival_fuel_gallons <= 50.0
            assert 0.0 <= stop.departure_fuel_gallons <= 50.0
            assert stop.departure_fuel_gallons >= stop.arrival_fuel_gallons
            last_pos = stop.route_position_miles

    # 3. Sum of purchases matches unrounded cost calculation
    def test_fuel_costs_sum_from_unrounded_purchases(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        stations = [
            StationCandidate("s1", "S1", 34.0, -118.0, Decimal("3.3333"), 350.0)
        ]
        res = optimizer.optimize(total_distance_miles=700.0, candidate_stations=stations, starting_fuel_gallons=50.0)
        assert res.is_feasible is True
        stop = res.fuel_stops[0]
        assert stop.gallons_to_purchase == 20.0
        assert stop.purchase_cost_usd == Decimal("66.67")
        assert res.total_fuel_cost_usd == sum(s.purchase_cost_usd for s in res.fuel_stops)

    # 4. Refined route distances recompute fuel plan
    def test_recompute_fuel_plan_on_refined_route(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        provisional_stops = [
            PlannedFuelStop(
                sequence=1,
                station_id="s1",
                name="S1",
                latitude=34.0,
                longitude=-118.0,
                price_per_gallon=Decimal("3.00"),
                arrival_fuel_gallons=20.0,
                gallons_to_purchase=20.0,
                purchase_cost_usd=Decimal("60.00"),
                departure_fuel_gallons=40.0,
                route_position_miles=300.0,
                detour_distance_miles=10.0,
                geocode_accuracy="EXACT"
            )
        ]
        refined_res = optimizer.recompute_for_refined_legs(
            leg_distances_miles=[310.0, 390.0],
            planned_stops=provisional_stops,
            starting_fuel_gallons=50.0
        )
        assert refined_res.is_feasible is True
        assert refined_res.fuel_stops[0].gallons_to_purchase == 20.0
        assert refined_res.total_fuel_consumed_gallons == 70.0

    # 5. Mismatching or malformed refined legs triggers an immediate error in planner
    def test_planner_fails_on_mismatching_refined_legs(self):
        # Create an approximate station in Cedar City to make 650 mile trip feasible
        FuelStation.objects.create(
            station_id="ST-APPROX-CEDAR",
            opis_id="105",
            name="Approx Cedar City",
            address="I-15",
            city="Cedar City",
            state="UT",
            retail_price=Decimal("3.20"),
            latitude=37.67,
            longitude=-113.06,
            geocode_status=FuelStation.GEOCODE_APPROXIMATE
        )

        mock_routing = MagicMock()
        mock_routing.call_count = 0
        mock_routing.get_route.side_effect = [
            # Call 1: Baseline
            {
                'distance_miles': 650.0,
                'duration_hours': 9.5,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-113.06, 37.67], [-111.89, 40.76]]},
                'legs': [],
                'leg_distances_miles': [650.0]
            },
            # Call 2: Refined route returns only 1 leg instead of 3!
            {
                'distance_miles': 655.0,
                'duration_hours': 9.6,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-113.06, 37.67], [-111.89, 40.76]]},
                'legs': [{'distance': 1000000}],
                'leg_distances_miles': [655.0]  # Missing intermediate waypoint legs
            }
        ]

        mock_geocoding = MagicMock()
        mock_geocoding.is_in_us.return_value = True
        mock_geocoding.geocode.side_effect = [
            (-118.24, 34.05, "Los Angeles, CA"),
            (-111.89, 40.76, "Salt Lake City, UT")
        ]

        planner = RoutePlanningService(routing_provider=mock_routing, geocoding_service=mock_geocoding)
        # Using allow_approximate_stations=True so a provisional stop is planned
        res = planner.plan_route("Los Angeles, CA", "Salt Lake City, UT", starting_fuel_gallons=50.0, allow_approximate_stations=True)
        assert res['success'] is False
        assert "Refinement routing error: expected" in res['error']

    # 6. Approximate station coordinates are disabled by default
    def test_approximate_stations_disabled_by_default(self):
        # Default planner: allow_approximate_stations is False
        planner = RoutePlanningService()
        assert planner.station_searcher.include_approximate_stations is False

        # In Barstow, we have ST-EXACT-1 (EXACT) and ST-APPROX-1 (APPROXIMATE)
        candidates_strict = planner.station_searcher.find_candidate_stations(
            route_coordinates=[(-117.1, 34.8), (-117.0, 34.9)],
            total_distance_miles=10.0
        )
        assert len(candidates_strict) == 1
        assert candidates_strict[0].station_id == "ST-EXACT-1"

    # 7. Defensible geospatial validation near borders and international points
    def test_geospatial_validation_near_borders(self):
        geo = GeocodingService(api_key="mock")
        # Detroit (US) vs Windsor (Canada)
        assert geo.is_in_us(42.3314, -83.0458) is True   # Detroit, MI
        assert geo.is_in_us(42.3149, -83.0364) is False  # Windsor, ON

        # San Diego (US) vs Tijuana (Mexico)
        assert geo.is_in_us(32.7157, -117.1611) is True  # San Diego, CA
        assert geo.is_in_us(32.5149, -117.0382) is False # Tijuana, Mexico

        # Alaska & Hawaii
        assert geo.is_in_us(61.2181, -149.9003) is True  # Anchorage, AK
        assert geo.is_in_us(21.3069, -157.8583) is True  # Honolulu, HI

        # Foreign points
        assert geo.is_in_us(43.6532, -79.3832) is False  # Toronto
        assert geo.is_in_us(49.2827, -123.1207) is False # Vancouver
        assert geo.is_in_us(52.5200, 13.4050) is False   # Berlin

    # 8. API default behavior returns transparent notification when no exact stations exist
    def test_api_defaults_to_exact_and_returns_clear_message(self):
        client = APIClient()
        with patch('fuel_planner.services.geocoding.GeocodingService.geocode') as mock_geo, \
             patch('fuel_planner.services.routing.RoutingProvider.get_route') as mock_route:

            mock_geo.side_effect = [
                (-118.2437, 34.0522, "Los Angeles, CA"),
                (-111.8910, 40.7608, "Salt Lake City, UT")
            ]
            mock_route.return_value = {
                'distance_miles': 685.0,
                'duration_hours': 10.0,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-111.89, 40.76]]},
                'legs': [],
                'leg_distances_miles': [685.0]
            }

            # POST without allow_approximate_stations (defaults to false)
            resp = client.post(
                '/api/v1/route-plan/',
                {'start': 'Los Angeles, CA', 'finish': 'Salt Lake City, UT'},
                format='json'
            )
            assert resp.status_code == 422
            data = resp.json()
            assert data['success'] is False
            assert "no verified EXACT fuel station coordinates exist" in data['error']

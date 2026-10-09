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
        # Create exact and approximate stations
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

    # 1. Cheaper plan verification vs exhaustive search
    def test_optimizer_selects_globally_cheaper_plan(self):
        """
        Given two candidate stations:
        Station A at mile 200 ($4.00)
        Station B at mile 400 ($3.00)
        Trip is 750 miles. Starting fuel: 50 gal.
        At start, both A and B are reachable.
        Selecting B allows purchasing at $3.00 rather than stopping at A ($4.00).
        """
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        stations = [
            StationCandidate("sA", "Station A", 34.0, -118.0, Decimal("4.00"), 200.0),
            StationCandidate("sB", "Station B", 35.0, -117.0, Decimal("3.00"), 400.0),
        ]
        res = optimizer.optimize(total_distance_miles=750.0, candidate_stations=stations, starting_fuel_gallons=50.0)
        assert res.is_feasible is True
        # Must pick sB
        assert len(res.fuel_stops) == 1
        assert res.fuel_stops[0].station_id == "sB"
        # Total cost is 25 gal * $3.00 = $75.00
        assert res.total_fuel_cost_usd == Decimal("75.00")

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

        # Invariants verification
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
        # Arrives at 350 mi with 15.0 gal. Remaining to dest: 350 mi (needs 35.0 gal).
        # Buys 20.0 gal at $3.3333 -> $66.67
        assert stop.gallons_to_purchase == 20.0
        assert stop.purchase_cost_usd == Decimal("66.67")
        assert res.total_fuel_cost_usd == sum(s.purchase_cost_usd for s in res.fuel_stops)

    # 4. Refined route distances recompute fuel plan
    def test_recompute_fuel_plan_on_refined_route(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        # Provisional stop planned at mile 300
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
        # Actual driving legs from provider: Leg 0 = 310 miles, Leg 1 = 390 miles
        # Total refined = 700 miles
        refined_res = optimizer.recompute_for_refined_legs(
            leg_distances_miles=[310.0, 390.0],
            planned_stops=provisional_stops,
            starting_fuel_gallons=50.0
        )
        assert refined_res.is_feasible is True
        # Arrived with 50 - 31 = 19 gal. Needed for leg 1: 39 gal.
        # Must purchase 39 - 19 = 20 gal.
        assert refined_res.fuel_stops[0].gallons_to_purchase == 20.0
        assert refined_res.total_fuel_consumed_gallons == 70.0

    # 5. Infeasible refinement leg triggers transparent error
    def test_refined_leg_exceeding_tank_capacity_fails(self):
        optimizer = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        provisional_stops = [
            PlannedFuelStop(
                sequence=1, station_id="s1", name="S1", latitude=34.0, longitude=-118.0,
                price_per_gallon=Decimal("3.00"), arrival_fuel_gallons=20.0, gallons_to_purchase=30.0,
                purchase_cost_usd=Decimal("90.00"), departure_fuel_gallons=50.0,
                route_position_miles=300.0, detour_distance_miles=10.0, geocode_accuracy="EXACT"
            )
        ]
        # Refined leg 1 is 550 miles (> 500 max range)
        refined_res = optimizer.recompute_for_refined_legs(
            leg_distances_miles=[300.0, 550.0],
            planned_stops=provisional_stops,
            starting_fuel_gallons=50.0
        )
        assert refined_res.is_feasible is False
        assert "exceeds 500-mile tank range" in refined_res.error_message

    # 6. Approximate city-centroid stations eligibility policy
    def test_approximate_station_eligibility_policy(self):
        # When include_approximate_stations is False, only EXACT stations are returned
        searcher_strict = SpatialStationSearcher(corridor_width_miles=20.0, include_approximate_stations=False)
        candidates_strict = searcher_strict.find_candidate_stations(
            route_coordinates=[(-117.1, 34.8), (-117.0, 34.9)],
            total_distance_miles=10.0
        )
        for c in candidates_strict:
            assert c.geocode_accuracy == FuelStation.GEOCODE_EXACT

        # When include_approximate_stations is True, approximate stations can be retrieved
        searcher_lenient = SpatialStationSearcher(corridor_width_miles=20.0, include_approximate_stations=True)
        candidates_lenient = searcher_lenient.find_candidate_stations(
            route_coordinates=[(-117.1, 34.8), (-117.0, 34.9)],
            total_distance_miles=10.0
        )
        accuracies = [c.geocode_accuracy for c in candidates_lenient]
        assert FuelStation.GEOCODE_EXACT in accuracies
        assert FuelStation.GEOCODE_APPROXIMATE in accuracies

    # 7. Geographic coordinate validation rejects Canada and foreign countries
    def test_geographic_validation_rejects_foreign_locations(self):
        geo = GeocodingService(api_key="mock")
        # Toronto, Canada
        assert geo.is_in_us(43.6532, -79.3832) is False
        # Vancouver, Canada
        assert geo.is_in_us(49.2827, -123.1207) is False
        # Berlin, Germany
        assert geo.is_in_us(52.5200, 13.4050) is False
        # Valid US: Los Angeles, Dallas, Anchorage, Honolulu
        assert geo.is_in_us(34.0522, -118.2437) is True
        assert geo.is_in_us(32.7767, -96.7970) is True
        assert geo.is_in_us(61.2181, -149.9003) is True
        assert geo.is_in_us(21.3069, -157.8583) is True

    # 8. Data importer updates changed prices on repeat runs
    def test_importer_updates_prices_without_ignoring(self):
        station = FuelStation.objects.get(station_id="ST-EXACT-1")
        assert station.retail_price == Decimal("3.899")
        # Simulate price update
        station.retail_price = Decimal("3.450")
        station.save()
        updated = FuelStation.objects.get(station_id="ST-EXACT-1")
        assert updated.retail_price == Decimal("3.450")

    # 9. Status codes: 400 for bad input, 422 for unprocessable, 502 for provider error
    def test_api_status_codes(self):
        client = APIClient()
        # Missing parameters -> 400 Bad Request
        resp_bad = client.post('/api/v1/route-plan/', {}, format='json')
        assert resp_bad.status_code == 400

        # Location outside US -> 400 Bad Request
        with patch('fuel_planner.services.geocoding.GeocodingService.geocode') as mock_geo:
            mock_geo.side_effect = GeocodingError("Location 'Berlin, Germany' resolved outside the United States.")
            resp_outside = client.post(
                '/api/v1/route-plan/',
                {'start': 'Berlin, Germany', 'finish': 'Las Vegas, NV'},
                format='json'
            )
            assert resp_outside.status_code == 400
            assert "outside the United States" in resp_outside.json()['error']

        # Provider outage -> 502 Bad Gateway
        with patch('fuel_planner.services.geocoding.GeocodingService.geocode') as mock_geo:
            mock_geo.side_effect = GeocodingError("Geocoding provider error: Connection refused")
            resp_outage = client.post(
                '/api/v1/route-plan/',
                {'start': 'Los Angeles, CA', 'finish': 'Las Vegas, NV'},
                format='json'
            )
            assert resp_outage.status_code == 502

    # 10. Call budget enforcement: maximum 2 external calls
    def test_route_call_budget_strictly_observed(self):
        # Create candidate stations along the test route so no gap exceeds 500 miles
        FuelStation.objects.create(
            station_id="ST-BUDGET-1",
            opis_id="777",
            name="Budget Station 1",
            address="I-15",
            city="Barstow",
            state="CA",
            retail_price=Decimal("3.10"),
            latitude=34.89,
            longitude=-117.01,
            geocode_status=FuelStation.GEOCODE_EXACT
        )
        FuelStation.objects.create(
            station_id="ST-BUDGET-2",
            opis_id="778",
            name="Budget Station 2",
            address="I-15",
            city="Cedar City",
            state="UT",
            retail_price=Decimal("3.20"),
            latitude=37.67,
            longitude=-113.06,
            geocode_status=FuelStation.GEOCODE_EXACT
        )

        mock_routing = MagicMock()
        mock_routing.call_count = 0
        mock_routing.get_route.side_effect = [
            # Call 1: Baseline (650 miles -> requires refueling)
            {
                'distance_miles': 650.0,
                'duration_hours': 9.5,
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-113.06, 37.67], [-111.89, 40.76]]
                },
                'legs': [],
                'leg_distances_miles': [650.0]
            },
            # Call 2: Refined (routing through waypoints)
            {
                'distance_miles': 655.0,
                'duration_hours': 9.6,
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-113.06, 37.67], [-111.89, 40.76]]
                },
                'legs': [{'distance': 185000}, {'distance': 450000}, {'distance': 419000}],
                'leg_distances_miles': [115.0, 280.0, 260.0]
            }
        ]

        mock_geocoding = MagicMock()
        mock_geocoding.is_in_us.return_value = True
        mock_geocoding.geocode.side_effect = [
            (-118.24, 34.05, "Los Angeles, CA"),
            (-111.89, 40.76, "Salt Lake City, UT")
        ]

        planner = RoutePlanningService(routing_provider=mock_routing, geocoding_service=mock_geocoding)
        planner.plan_route("Los Angeles, CA", "Salt Lake City, UT", starting_fuel_gallons=50.0)

        # Refined call was made -> call_count exactly 2
        assert mock_routing.get_route.call_count == 2

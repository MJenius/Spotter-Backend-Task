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

    # 1. Exhaustive comparison vs brute-force on small network
    def test_optimizer_exhaustive_comparison_on_small_network(self):
        """
        Trip: 900 miles. Starting fuel: 50 gal.
        Candidate 1 at mile 250: Price $4.00
        Candidate 2 at mile 450: Price $3.00
        Candidate 3 at mile 700: Price $3.50

        Brute-force check:
        Plan A: Stop at 1 ($4.00), fill to reach 3 or dest. Cost > $130.
        Plan B: Stop at 2 ($3.00) only.
          Reach 450 with 5 gal remaining. Buy 40 gal at $3.00 = $120.00.
          Departs with 45 gal, finishes remaining 450 miles with 0 gal left.
        Optimal plan cost: exactly $120.00.
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
            {
                'distance_miles': 650.0,
                'duration_hours': 9.5,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-113.06, 37.67], [-111.89, 40.76]]},
                'legs': [],
                'leg_distances_miles': [650.0]
            },
            {
                'distance_miles': 655.0,
                'duration_hours': 9.6,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-113.06, 37.67], [-111.89, 40.76]]},
                'legs': [{'distance': 1000000}],
                'leg_distances_miles': [655.0]
            }
        ]

        mock_geocoding = MagicMock()
        mock_geocoding.is_in_us.return_value = True
        mock_geocoding.geocode.side_effect = [
            (-118.24, 34.05, "Los Angeles, CA"),
            (-111.89, 40.76, "Salt Lake City, UT")
        ]

        planner = RoutePlanningService(routing_provider=mock_routing, geocoding_service=mock_geocoding)
        res = planner.plan_route("Los Angeles, CA", "Salt Lake City, UT", starting_fuel_gallons=50.0, allow_approximate_stations=True)
        assert res['success'] is False
        assert "Refinement routing error: expected" in res['error']

    # 6. Approximate station coordinates are disabled by default
    def test_approximate_stations_disabled_by_default(self):
        planner = RoutePlanningService()
        assert planner.station_searcher.include_approximate_stations is False

        candidates_strict = planner.station_searcher.find_candidate_stations(
            route_coordinates=[(-117.1, 34.8), (-117.0, 34.9)],
            total_distance_miles=10.0
        )
        assert len(candidates_strict) == 1
        assert candidates_strict[0].station_id == "ST-EXACT-1"

    # 7. Defensible geospatial validation using boundary GeoJSON
    def test_geospatial_validation_near_borders(self):
        geo = GeocodingService(api_key="mock")
        # Detroit (US) vs Windsor (Canada)
        assert geo.is_in_us(42.3314, -83.0458) is True
        assert geo.is_in_us(42.3149, -83.0364) is False

        # Buffalo (US) vs Fort Erie (Canada)
        assert geo.is_in_us(42.8864, -78.8784) is True
        assert geo.is_in_us(42.9022, -78.9328) is False

        # San Diego (US) vs Tijuana (Mexico)
        assert geo.is_in_us(32.7157, -117.1611) is True
        assert geo.is_in_us(32.5149, -117.0382) is False

        # Alaska & Hawaii
        assert geo.is_in_us(61.2181, -149.9003) is True
        assert geo.is_in_us(21.3069, -157.8583) is True

        # Foreign locations
        assert geo.is_in_us(43.6532, -79.3832) is False
        assert geo.is_in_us(49.2827, -123.1207) is False
        assert geo.is_in_us(52.5200, 13.4050) is False
        # Yukon, Canada (previously misclassified by coarse Alaska bounding box)
        assert geo.is_in_us(62.0, -135.0) is False
        # Mid-Pacific Ocean (previously misclassified by coarse Hawaii bounding box)
        assert geo.is_in_us(20.0, -159.0) is False

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

            resp = client.post(
                '/api/v1/route-plan/',
                {'start': 'Los Angeles, CA', 'finish': 'Salt Lake City, UT'},
                format='json'
            )
            assert resp.status_code == 422
            data = resp.json()
            assert data['success'] is False
            assert "no verified EXACT fuel station coordinates exist" in data['error']

    # 9. Test station search populates access_miles_one_way
    def test_station_search_populates_access_distance(self):
        searcher = SpatialStationSearcher(corridor_width_miles=15.0, max_off_route_distance_miles=10.0, include_approximate_stations=True)
        candidates = searcher.find_candidate_stations(
            route_coordinates=[(-117.1, 34.8), (-117.0, 34.9)],
            total_distance_miles=10.0
        )
        assert len(candidates) > 0
        for cand in candidates:
            assert cand.access_miles_one_way >= 0.0
            assert abs(cand.detour_distance_miles - cand.access_miles_one_way * 2.0) < 1e-3

    # 10. Test zero starting fuel stays zero
    def test_zero_starting_fuel_stays_zero(self):
        client = APIClient()
        with patch('fuel_planner.services.geocoding.GeocodingService.geocode') as mock_geo, \
             patch('fuel_planner.services.routing.RoutingProvider.get_route') as mock_route:

            mock_geo.side_effect = [
                (-118.2437, 34.0522, "Los Angeles, CA"),
                (-115.1485, 36.1672, "Las Vegas, NV")
            ]
            mock_route.return_value = {
                'distance_miles': 270.0,
                'duration_hours': 4.0,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-115.14, 36.16]]},
                'legs': [],
                'leg_distances_miles': [270.0]
            }

            resp = client.post(
                '/api/v1/route-plan/',
                {'start': 'Los Angeles, CA', 'finish': 'Las Vegas, NV', 'starting_fuel_gallons': 0.0},
                format='json'
            )
            assert resp.status_code == 422
            data = resp.json()
            assert data['success'] is False
            assert "Starting fuel (0.0 gal) is insufficient" in data['error']

    # 11. Test route refinement reconciliation error when recomputed stops alter provisional sequence
    def test_reconciliation_error_when_stops_sequence_altered(self):
        planner = RoutePlanningService()
        planned_stops = [
            PlannedFuelStop(
                sequence=1,
                station_id="s1",
                name="S1",
                latitude=0.0,
                longitude=0.0,
                price_per_gallon=Decimal("3.50"),
                arrival_fuel_gallons=20.0,
                gallons_to_purchase=30.0,
                purchase_cost_usd=Decimal("105.00"),
                departure_fuel_gallons=50.0,
                route_position_miles=300.0,
                detour_distance_miles=2.0
            )
        ]
        # In this scenario, leg distances are 100 miles and 200 miles (total 300 miles)
        # Starting with 50 gal, the entire 300-mile trip is reachable without purchasing fuel.
        # So recompute_for_refined_legs will drop stop s1, producing 0 stops.
        recomputed = planner.optimizer.recompute_for_refined_legs(
            leg_distances_miles=[100.0, 200.0],
            planned_stops=planned_stops,
            starting_fuel_gallons=50.0
        )
        assert recomputed.is_feasible is True
        assert len(recomputed.fuel_stops) == 0  # Provisional stop dropped!

        # Now test that RoutePlanningService detects this discrepancy and returns an explicit reconciliation error
        mock_routing = MagicMock()
        mock_routing.call_count = 1
        mock_routing.get_route.side_effect = [
            # Baseline route (600 miles -> requires refueling with starting fuel 40 gal)
            {
                'distance_miles': 600.0,
                'duration_hours': 9.0,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-111.89, 40.76]]},
                'legs': [],
                'leg_distances_miles': [600.0]
            },
            # Refined route through s1: provider returns short legs [50.0, 100.0] where fuel purchase is no longer needed
            {
                'distance_miles': 150.0,
                'duration_hours': 2.0,
                'geometry': {'type': 'LineString', 'coordinates': [[-118.24, 34.05], [-117.01, 34.89], [-111.89, 40.76]]},
                'legs': [{'distance': 80467}, {'distance': 160934}],
                'leg_distances_miles': [50.0, 100.0]
            }
        ]

        mock_geocoding = MagicMock()
        mock_geocoding.is_in_us.return_value = True
        mock_geocoding.geocode.side_effect = [
            (-118.24, 34.05, "Los Angeles, CA"),
            (-111.89, 40.76, "Salt Lake City, UT")
        ]

        planner_service = RoutePlanningService(routing_provider=mock_routing, geocoding_service=mock_geocoding)
        with patch.object(planner_service.station_searcher, 'find_candidate_stations') as mock_search:
            mock_search.return_value = [
                StationCandidate(
                    station_id="ST-MOCK-1",
                    name="Mock Station",
                    latitude=35.0,
                    longitude=-116.0,
                    price_per_gallon=Decimal("3.00"),
                    route_position_miles=250.0,
                    detour_distance_miles=0.0,
                    access_miles_one_way=0.0
                )
            ]
            res = planner_service.plan_route(
                "Los Angeles, CA", "Salt Lake City, UT",
                starting_fuel_gallons=30.0,
                allow_approximate_stations=True
            )
            assert res['success'] is False
            assert "Route refinement discrepancy" in res['error']

    # 10. Test CSV Import schema validation & transaction rollback on failure
    def test_import_fuel_stations_atomic_rollback(self, tmp_path):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        # Create a valid CSV with 1 station
        csv_file = tmp_path / "stations.csv"
        csv_file.write_text(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "99999,Atomic Test Stop,123 Main St,Barstow,CA,1,3.500\n",
            encoding="utf-8"
        )

        initial_count = FuelStation.objects.count()
        assert initial_count > 0

        # Simulate failure during save/bulk_create inside the command
        with patch('fuel_planner.models.FuelStation.objects.bulk_create', side_effect=RuntimeError("Simulated DB Crash")):
            with pytest.raises(RuntimeError):
                call_command('import_fuel_stations', csv_path=str(csv_file), clear=True)

        # Confirm rollback: stations should NOT have been cleared because the transaction aborted
        assert FuelStation.objects.count() == initial_count

    # 11. Test CSV Import rejects missing headers
    def test_import_fuel_stations_missing_headers(self, tmp_path):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("ID,Name,Price\n1,Bad,3.00\n", encoding="utf-8")

        with pytest.raises(CommandError) as exc_info:
            call_command('import_fuel_stations', csv_path=str(bad_csv))
        assert "missing mandatory columns" in str(exc_info.value).lower()

    # 12. Test Routing Response Schema Validation (segments sum, missing summary, bad linestring)
    def test_routing_provider_schema_validation(self):
        from fuel_planner.services.routing import RoutingSchemaError

        provider = RoutingProvider(api_key="test-key")

        # Missing summary distance
        bad_response = {
            'features': [{
                'type': 'Feature',
                'geometry': {'type': 'LineString', 'coordinates': [[-118.0, 34.0], [-117.0, 34.0]]},
                'properties': {
                    'summary': {'duration': 100.0},  # missing 'distance'
                    'segments': [{'distance': 1000.0}]
                }
            }]
        }
        with patch('requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = bad_response
            with pytest.raises(RoutingSchemaError):
                provider.get_route([(-118.0, 34.0), (-117.0, 34.0)], use_cache=False)

        # Segments distance sum mismatch vs total summary distance
        bad_sum_response = {
            'features': [{
                'type': 'Feature',
                'geometry': {'type': 'LineString', 'coordinates': [[-118.0, 34.0], [-117.0, 34.0]]},
                'properties': {
                    'summary': {'distance': 10000.0, 'duration': 100.0},
                    'segments': [{'distance': 1000.0}]  # 1000 != 10000
                }
            }]
        }
        with patch('requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = bad_sum_response
            with pytest.raises(RoutingSchemaError):
                provider.get_route([(-118.0, 34.0), (-117.0, 34.0)], use_cache=False)

    # 13. Test Routing cache key uniqueness for close coordinates
    def test_routing_cache_key_precision(self):
        # Two routes with coordinates differing only beyond 4 decimal places
        coords1 = [(-118.243681, 34.052235), (-117.017311, 34.895811)]
        coords2 = [(-118.243689, 34.052239), (-117.017311, 34.895811)]
        k1 = RoutingProvider.compute_cache_key(coords1)
        k2 = RoutingProvider.compute_cache_key(coords2)
        assert k1 != k2

    # 14. Test Optimizer strict numeric input validation
    def test_optimizer_numeric_validation(self):
        opt = FuelRouteOptimizer()
        with pytest.raises(ValueError):
            opt.optimize(-100.0, [])
        with pytest.raises(ValueError):
            opt.optimize(float('nan'), [])
        with pytest.raises(ValueError):
            opt.optimize(100.0, [], starting_fuel_gallons=float('inf'))
        with pytest.raises(ValueError):
            bad_candidate = StationCandidate("s1", "S1", 34.0, -118.0, Decimal("-1.00"), 50.0)
            opt.optimize(100.0, [bad_candidate])

    # 15. Test API typed error mapping for rate limits and auth failures
    def test_api_view_typed_error_handling(self):
        from fuel_planner.services.geocoding import GeocodingRateLimitError, GeocodingAuthError
        from fuel_planner.services.routing import RoutingRateLimitError, RoutingAuthError

        client = APIClient()

        # Geocoding Rate Limit -> 429
        with patch.object(RoutePlanningService, 'plan_route', side_effect=GeocodingRateLimitError("429 limit")):
            res = client.post('/api/v1/route-plan/', {'start': 'Los Angeles, CA', 'finish': 'Las Vegas, NV'}, format='json')
            assert res.status_code == 429

        # Geocoding Auth Error -> 502
        with patch.object(RoutePlanningService, 'plan_route', side_effect=GeocodingAuthError("401 auth")):
            res = client.post('/api/v1/route-plan/', {'start': 'Los Angeles, CA', 'finish': 'Las Vegas, NV'}, format='json')
            assert res.status_code == 502

        # Routing Rate Limit -> 429
        with patch.object(RoutePlanningService, 'plan_route', side_effect=RoutingRateLimitError("429 routing")):
            res = client.post('/api/v1/route-plan/', {'start': 'Los Angeles, CA', 'finish': 'Las Vegas, NV'}, format='json')
            assert res.status_code == 429

        # Routing Auth Error -> 502
        with patch.object(RoutePlanningService, 'plan_route', side_effect=RoutingAuthError("401 routing auth")):
            res = client.post('/api/v1/route-plan/', {'start': 'Los Angeles, CA', 'finish': 'Las Vegas, NV'}, format='json')
            assert res.status_code == 502

    # 16. Test geometry coordinate validation & summary > 0 requirement
    def test_routing_provider_geometry_and_summary_bounds(self):
        from fuel_planner.services.routing import RoutingSchemaError

        provider = RoutingProvider(api_key="test-key")

        # Zero summary distance
        bad_zero_dist = {
            'features': [{
                'type': 'Feature',
                'geometry': {'type': 'LineString', 'coordinates': [[-118.0, 34.0], [-117.0, 34.0]]},
                'properties': {
                    'summary': {'distance': 0.0, 'duration': 100.0},
                    'segments': [{'distance': 0.0}]
                }
            }]
        }
        with patch('requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = bad_zero_dist
            with pytest.raises(RoutingSchemaError) as exc:
                provider.get_route([(-118.0, 34.0), (-117.0, 34.0)], use_cache=False)
            assert "positive finite number" in str(exc.value)

        # Invalid geometry coordinates (out of bounds)
        bad_coords_resp = {
            'features': [{
                'type': 'Feature',
                'geometry': {'type': 'LineString', 'coordinates': [[-195.0, 34.0], [-117.0, 34.0]]},
                'properties': {
                    'summary': {'distance': 1000.0, 'duration': 100.0},
                    'segments': [{'distance': 1000.0}]
                }
            }]
        }
        with patch('requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = bad_coords_resp
            with pytest.raises(RoutingSchemaError) as exc:
                provider.get_route([(-118.0, 34.0), (-117.0, 34.0)], use_cache=False)
            assert "Invalid longitude" in str(exc.value)

    # 17. Test geocoding is_in_us coordinate bounds check
    def test_geocoding_is_in_us_bounds(self):
        geo = GeocodingService(api_key="test")
        assert geo.is_in_us(95.0, -118.0) is False  # Lat > 90
        assert geo.is_in_us(-95.0, -118.0) is False  # Lat < -90
        assert geo.is_in_us(34.0, 185.0) is False  # Lon > 180
        assert geo.is_in_us(34.0, -185.0) is False  # Lon < -180
        assert geo.is_in_us(float('nan'), -118.0) is False
        assert geo.is_in_us(34.0, float('inf')) is False

    # 18. Test optimizer station candidate coordinate bounds
    def test_optimizer_candidate_bounds(self):
        opt = FuelRouteOptimizer()
        with pytest.raises(ValueError):
            opt.optimize(100.0, [StationCandidate("s1", "S1", 95.0, -118.0, Decimal("3.50"), 50.0)])
        with pytest.raises(ValueError):
            opt.optimize(100.0, [StationCandidate("s2", "S2", 34.0, 200.0, Decimal("3.50"), 50.0)])

    # 19. Test coastal metric tolerance with projected coordinates
    def test_geocoding_coastal_metric_tolerance(self):
        geo = GeocodingService(api_key="test")
        # Point right off Santa Monica Pier (approx 500m off coastline, in Pacific Ocean)
        # Should be within 3,200m coastal tolerance and closer to US than MEX/CAN
        assert geo.is_in_us(34.008, -118.502) is True

        # Point 10 miles (16,000m) off coastline into the ocean
        # Should be rejected because distance exceeds 3,200m
        assert geo.is_in_us(33.85, -118.70) is False

    # 20. Test display-rounded fuel gallons vs charged cost reconciliation
    def test_optimizer_display_quantities_and_cost_reconciliation(self):
        """
        Verify that even when gallons_to_purchase is rounded to 4 decimals for display,
        purchase_cost_usd is computed from the exact unrounded Fraction and matches the
        total charged fuel cost to the penny.
        """
        opt = FuelRouteOptimizer(tank_capacity_gallons=50.0, fuel_economy_mpg=10.0)
        # 700-mile trip: starts with 50 gal, needs to buy at mile 300 and 500
        stations = [
            StationCandidate("s1", "Station 1", 34.0, -117.0, Decimal("3.1234"), 300.0),
            StationCandidate("s2", "Station 2", 35.0, -115.0, Decimal("3.5678"), 500.0),
        ]
        res = opt.optimize(700.0, stations, starting_fuel_gallons=50.0)
        assert res.is_feasible is True
        assert len(res.fuel_stops) > 0

        # Sum of per-stop purchase costs equals total_fuel_cost_usd exactly
        sum_stop_costs = sum((s.purchase_cost_usd for s in res.fuel_stops), Decimal("0.00"))
        assert res.total_fuel_cost_usd == sum_stop_costs
        assert res.fuel_purchase_cost_usd == sum_stop_costs

        for s in res.fuel_stops:
            # Displayed gallons is a float rounded to 4 decimals
            assert round(s.gallons_to_purchase, 4) == s.gallons_to_purchase
            # Check difference between displayed product (gallons * price) and actual charged cost is < $0.01
            naive_display_cost = Decimal(str(round(s.gallons_to_purchase * float(s.price_per_gallon), 2)))
            assert abs(s.purchase_cost_usd - naive_display_cost) <= Decimal("0.01")

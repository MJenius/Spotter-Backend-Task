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

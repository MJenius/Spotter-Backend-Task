import logging
from typing import Dict, Any, Optional, List, Tuple
from decimal import Decimal
from django.conf import settings
from fuel_planner.services.geocoding import GeocodingService, GeocodingError
from fuel_planner.services.routing import RoutingProvider, RoutingError
from fuel_planner.services.station_search import SpatialStationSearcher
from fuel_planner.services.optimizer import FuelRouteOptimizer, StationCandidate, OptimizationResult

logger = logging.getLogger(__name__)


class RoutePlanningService:
    """
    Orchestrates:
    1. Geocoding start and finish (if given as text) or parsing coords.
    2. Route Provider Call 1: Baseline route & geometry.
    3. Spatial Corridor Search: Finding candidate fuel stations.
    4. Optimizer: Optimal stop selection & purchase plan.
    5. Route Provider Call 2 (Refinement): Route with waypoints to reflect actual driving detours.
    6. Response assembly with strict API call tracking and cost accounting.
    """

    def __init__(
        self,
        routing_provider: Optional[RoutingProvider] = None,
        geocoding_service: Optional[GeocodingService] = None,
        station_searcher: Optional[SpatialStationSearcher] = None,
        optimizer: Optional[FuelRouteOptimizer] = None
    ):
        self.routing_provider = routing_provider or RoutingProvider()
        self.geocoding_service = geocoding_service or GeocodingService()
        self.station_searcher = station_searcher or SpatialStationSearcher(
            corridor_width_miles=10.0,
            max_off_route_distance_miles=getattr(settings, 'MAX_OFF_ROUTE_DISTANCE_MILES', 5.0)
        )
        self.optimizer = optimizer or FuelRouteOptimizer(
            tank_capacity_gallons=getattr(settings, 'TANK_CAPACITY_GALLONS', 50.0),
            fuel_economy_mpg=getattr(settings, 'FUEL_ECONOMY_MPG', 10.0)
        )

    def plan_route(
        self,
        start_input: Any,
        finish_input: Any,
        starting_fuel_gallons: Optional[float] = None,
        max_off_route_distance: Optional[float] = None
    ) -> Dict[str, Any]:
        starting_fuel = (
            starting_fuel_gallons
            if starting_fuel_gallons is not None
            else getattr(settings, 'DEFAULT_STARTING_FUEL_GALLONS', 50.0)
        )

        if max_off_route_distance is not None:
            self.station_searcher.max_off_route_distance_miles = max_off_route_distance

        # Step 1: Resolve Coordinates
        start_lon, start_lat, start_label = self._resolve_location(start_input, "start")
        finish_lon, finish_lat, finish_label = self._resolve_location(finish_input, "finish")

        # Step 2: Route Provider Call 1 (Baseline route)
        baseline_coords = [(start_lon, start_lat), (finish_lon, finish_lat)]
        baseline_route = self.routing_provider.get_route(baseline_coords)
        baseline_distance = baseline_route['distance_miles']
        route_linestring_coords = baseline_route['geometry'].get('coordinates', [])

        # Step 3: Spatial Corridor Station Search
        candidates = self.station_searcher.find_candidate_stations(
            route_coordinates=route_linestring_coords,
            total_distance_miles=baseline_distance
        )

        # Step 4: Optimization
        opt_result: OptimizationResult = self.optimizer.optimize(
            total_distance_miles=baseline_distance,
            candidate_stations=candidates,
            starting_fuel_gallons=starting_fuel
        )

        if not opt_result.is_feasible:
            return {
                'success': False,
                'error': opt_result.error_message or "Unable to find a feasible fuel route under vehicle constraints.",
                'routing_provider_calls': self.routing_provider.call_count
            }

        # Step 5: Route Refinement (Call 2 if stops exist)
        final_route = baseline_route
        if opt_result.fuel_stops:
            waypoint_coords = [(start_lon, start_lat)]
            for stop in opt_result.fuel_stops:
                waypoint_coords.append((stop.longitude, stop.latitude))
            waypoint_coords.append((finish_lon, finish_lat))

            try:
                # Call 2: Refined route visiting actual station locations
                refined_route = self.routing_provider.get_route(waypoint_coords)
                final_route = refined_route
            except Exception as e:
                logger.warning(f"Refinement route failed ({e}), falling back to baseline route with estimated detours.")

        # Serialize fuel stops
        fuel_stops_data = [
            {
                'sequence': s.sequence,
                'station_id': s.station_id,
                'name': s.name,
                'address': s.address,
                'city': s.city,
                'state': s.state,
                'latitude': s.latitude,
                'longitude': s.longitude,
                'price_per_gallon': float(s.price_per_gallon),
                'arrival_fuel_gallons': s.arrival_fuel_gallons,
                'gallons_to_purchase': s.gallons_to_purchase,
                'purchase_cost_usd': float(s.purchase_cost_usd),
                'departure_fuel_gallons': s.departure_fuel_gallons,
                'route_position_miles': s.route_position_miles,
                'detour_distance_miles': s.detour_distance_miles,
            }
            for s in opt_result.fuel_stops
        ]

        total_distance = final_route['distance_miles']
        total_fuel_consumed = round(total_distance / self.optimizer.fuel_economy_mpg, 2)

        return {
            'success': True,
            'start': {'label': start_label, 'coordinates': [start_lon, start_lat]},
            'finish': {'label': finish_label, 'coordinates': [finish_lon, finish_lat]},
            'route': {
                'distance_miles': total_distance,
                'duration_hours': final_route.get('duration_hours', 0.0),
                'geometry': final_route['geometry'],
                'legs': final_route.get('legs', []),
            },
            'fuel_stops': fuel_stops_data,
            'fuel_consumed_gallons': total_fuel_consumed,
            'fuel_purchased_gallons': opt_result.total_gallons_purchased,
            'fuel_purchase_cost_usd': float(opt_result.fuel_purchase_cost_usd),
            'total_fuel_cost_usd': float(opt_result.total_fuel_cost_usd),
            'cost_accounting_note': (
                "total_fuel_cost_usd represents the actual out-of-pocket expenditure for fuel purchases planned at recommended stations along this journey. "
                f"Assumes departure with {starting_fuel} gallons pre-existing in the tank."
            ),
            'summary': {
                'number_of_stops': len(fuel_stops_data),
                'maximum_range_miles': self.optimizer.max_range_miles,
                'fuel_economy_mpg': self.optimizer.fuel_economy_mpg,
                'tank_capacity_gallons': self.optimizer.tank_capacity,
                'starting_fuel_gallons': starting_fuel,
                'routing_provider_calls': self.routing_provider.call_count
            },
            'data_quality': {
                'coordinate_provenance': 'us_cities_census_enriched',
                'stations_evaluated_in_corridor': len(candidates),
            }
        }

    def _resolve_location(self, loc: Any, label_prefix: str) -> Tuple[float, float, str]:
        if isinstance(loc, (list, tuple)) and len(loc) == 2:
            lon, lat = float(loc[0]), float(loc[1])
            if not self.geocoding_service.is_in_us(lat, lon):
                raise GeocodingError(f"{label_prefix} coordinates ({lat}, {lon}) are outside the United States.")
            return lon, lat, f"{label_prefix.title()} ({lat:.4f}, {lon:.4f})"

        if isinstance(loc, dict) and 'lat' in loc and 'lon' in loc:
            lat, lon = float(loc['lat']), float(loc['lon'])
            if not self.geocoding_service.is_in_us(lat, lon):
                raise GeocodingError(f"{label_prefix} coordinates ({lat}, {lon}) are outside the United States.")
            return lon, lat, f"{label_prefix.title()} ({lat:.4f}, {lon:.4f})"

        if isinstance(loc, str):
            # Check if format is "lat, lon"
            if ',' in loc:
                parts = [p.strip() for p in loc.split(',')]
                if len(parts) == 2:
                    try:
                        lat, lon = float(parts[0]), float(parts[1])
                        if self.geocoding_service.is_in_us(lat, lon):
                            return lon, lat, f"{label_prefix.title()} ({lat:.4f}, {lon:.4f})"
                    except ValueError:
                        pass
            return self.geocoding_service.geocode(loc)

        raise GeocodingError(f"Invalid {label_prefix} location format: {loc}")

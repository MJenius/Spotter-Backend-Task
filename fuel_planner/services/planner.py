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
    Coordinates end-to-end fuel-route optimization:
    1. Geocodes Start & Finish or validates explicit coordinates.
    2. Call 1 (Baseline): Obtains driving geometry & baseline distance.
    3. Spatial Corridor Search: Filters candidate stations with exact vs approximate policy.
    4. Optimizer (Provisional Plan): Solves lookahead cost-minimal refueling sequence.
    5. Call 2 (Refinement): Reroutes through exact selected station waypoints.
    6. Recomputes & verifies fuel purchases against ACTUAL refined driving legs.
       Returns an error if the refined itinerary becomes infeasible.
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
            max_off_route_distance_miles=getattr(settings, 'MAX_OFF_ROUTE_DISTANCE_MILES', 5.0),
            include_approximate_stations=True  # Can be configured per request
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
        max_off_route_distance: Optional[float] = None,
        allow_approximate_stations: bool = True
    ) -> Dict[str, Any]:
        starting_fuel = (
            starting_fuel_gallons
            if starting_fuel_gallons is not None
            else getattr(settings, 'DEFAULT_STARTING_FUEL_GALLONS', 50.0)
        )

        if max_off_route_distance is not None:
            self.station_searcher.max_off_route_distance_miles = max_off_route_distance
        self.station_searcher.include_approximate_stations = allow_approximate_stations

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

        if not candidates and baseline_distance > (starting_fuel * self.optimizer.fuel_economy_mpg):
            return {
                'success': False,
                'error': (
                    "No eligible fuel stations found along the route corridor. "
                    "If approximate city-level stations are excluded, none have verified exact coordinates."
                ),
                'routing_provider_calls': self.routing_provider.call_count
            }

        # Step 4: Initial Optimization Plan
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

        # Step 5: Route Refinement & Recomputation (Call 2 if stops exist)
        final_route = baseline_route
        final_opt_result = opt_result

        if opt_result.fuel_stops:
            waypoint_coords = [(start_lon, start_lat)]
            for stop in opt_result.fuel_stops:
                waypoint_coords.append((stop.longitude, stop.latitude))
            waypoint_coords.append((finish_lon, finish_lat))

            try:
                refined_route = self.routing_provider.get_route(waypoint_coords)
                final_route = refined_route

                # Critical Fix: Recompute and validate fuel purchases on ACTUAL refined legs!
                leg_distances = refined_route.get('leg_distances_miles', [])
                if len(leg_distances) == len(opt_result.fuel_stops) + 1:
                    recomputed_result = self.optimizer.recompute_for_refined_legs(
                        leg_distances_miles=leg_distances,
                        planned_stops=opt_result.fuel_stops,
                        starting_fuel_gallons=starting_fuel
                    )
                    if not recomputed_result.is_feasible:
                        return {
                            'success': False,
                            'error': (
                                f"Refined route through recommended stops became infeasible: "
                                f"{recomputed_result.error_message}"
                            ),
                            'routing_provider_calls': self.routing_provider.call_count
                        }
                    final_opt_result = recomputed_result
            except RoutingError as e:
                logger.warning(f"Refinement routing call failed: {e}. Aborting with error.")
                return {
                    'success': False,
                    'error': f"Failed to calculate refined driving route through waypoints: {str(e)}",
                    'routing_provider_calls': self.routing_provider.call_count
                }

        # Serialize verified fuel stops
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
                'arrival_fuel_gallons': round(s.arrival_fuel_gallons, 2),
                'gallons_to_purchase': round(s.gallons_to_purchase, 2),
                'purchase_cost_usd': float(s.purchase_cost_usd),
                'departure_fuel_gallons': round(s.departure_fuel_gallons, 2),
                'route_position_miles': round(s.route_position_miles, 2),
                'detour_distance_miles': round(s.detour_distance_miles, 2),
                'geocode_accuracy': s.geocode_accuracy
            }
            for s in final_opt_result.fuel_stops
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
            'fuel_purchased_gallons': final_opt_result.total_gallons_purchased,
            'fuel_purchase_cost_usd': float(final_opt_result.fuel_purchase_cost_usd),
            'total_fuel_cost_usd': float(final_opt_result.total_fuel_cost_usd),
            'cost_accounting_note': (
                "total_fuel_cost_usd reflects the exact sum of fuel purchases planned at recommended stations along the refined itinerary. "
                f"Assumes vehicle departs origin with {starting_fuel} gallons."
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
                'candidate_stations_in_corridor': len(candidates),
                'approximate_stations_permitted': allow_approximate_stations,
                'note': "Fuel prices taken directly from OPIS CSV dataset. Station locations are based on city/highway enrichment."
            }
        }

    def _resolve_location(self, loc: Any, label_prefix: str) -> Tuple[float, float, str]:
        if isinstance(loc, (list, tuple)) and len(loc) == 2:
            lon, lat = float(loc[0]), float(loc[1])
            if not self.geocoding_service.is_in_us(lat, lon):
                raise GeocodingError(f"{label_prefix.title()} coordinates ({lat}, {lon}) are outside the United States.")
            return lon, lat, f"{label_prefix.title()} ({lat:.4f}, {lon:.4f})"

        if isinstance(loc, dict) and 'lat' in loc and 'lon' in loc:
            lat, lon = float(loc['lat']), float(loc['lon'])
            if not self.geocoding_service.is_in_us(lat, lon):
                raise GeocodingError(f"{label_prefix.title()} coordinates ({lat}, {lon}) are outside the United States.")
            return lon, lat, f"{label_prefix.title()} ({lat:.4f}, {lon:.4f})"

        if isinstance(loc, str):
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

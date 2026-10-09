import logging
import requests
from typing import List, Tuple, Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

METERS_TO_MILES = 0.000621371


class RoutingError(Exception):
    pass


class RoutingProvider:
    """
    Integrates with HeiGIT / OpenRouteService v2 directions API.
    Enforces call-budget tracking, caching, timeouts, and graceful error handling.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or getattr(settings, 'HEIGIT_API_KEY', '')
        self.call_count = 0
        self.headers = {
            'Authorization': self.api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json, application/geo+json',
            'User-Agent': 'SpotterFuelPlanner/1.0'
        }

    def get_route(
        self,
        coordinates: List[Tuple[float, float]],
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """
        coordinates: list of [longitude, latitude] points in order.
        Returns GeoJSON FeatureCollection dictionary with distance_miles and geometry LineString.
        """
        if len(coordinates) < 2:
            raise RoutingError("At least two coordinates (start and finish) are required.")

        # Cache key based on rounded coordinates
        cache_key = f"route_{'_'.join([f'{c[0]:.4f},{c[1]:.4f}' for c in coordinates])}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached:
                return cached

        url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
        payload = {
            'coordinates': coordinates,
            'radiuses': [-1] * len(coordinates),  # default search radius
            'instructions': False,
            'elevation': False
        }

        self.call_count += 1
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=12.0)
            if resp.status_code == 429:
                raise RoutingError("Routing provider rate limit exceeded (HTTP 429). Please retry in a moment.")
            if resp.status_code == 404:
                raise RoutingError("Routing endpoint not found or unsupported route geometry.")
            resp.raise_for_status()

            data = resp.json()
            features = data.get('features', [])
            if not features:
                raise RoutingError("No driving route found between the specified points.")

            feature = features[0]
            summary = feature['properties']['summary']
            distance_meters = summary.get('distance', 0.0)
            duration_seconds = summary.get('duration', 0.0)
            distance_miles = distance_meters * METERS_TO_MILES

            geometry = feature.get('geometry', {})  # GeoJSON LineString
            legs = feature['properties'].get('segments', [])

            result = {
                'distance_miles': round(distance_miles, 2),
                'duration_hours': round(duration_seconds / 3600.0, 2),
                'geometry': geometry,
                'legs': legs,
                'raw_feature': feature
            }

            if use_cache:
                cache.set(cache_key, result, timeout=86400)  # cache 24 hours

            return result
        except requests.RequestException as e:
            logger.error(f"Routing provider error: {e}")
            raise RoutingError(f"Driving route calculation failed: {str(e)}")

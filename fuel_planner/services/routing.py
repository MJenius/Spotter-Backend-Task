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
    Integrates with official HeiGIT API v2 endpoints:
    https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson
    Enforces call-budget tracking, caching, timeouts, and graceful error handling.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or getattr(settings, 'HEIGIT_API_KEY', '')
        self.call_count = 0
        self.headers = {
            'Authorization': self.api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json, application/geo+json',
            'User-Agent': 'SpotterFuelPlanner/2.0'
        }
        self.endpoint_url = "https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson"

    def get_route(
        self,
        coordinates: List[Tuple[float, float]],
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """
        coordinates: list of [longitude, latitude] points in order.
        Returns GeoJSON FeatureCollection dictionary with distance_miles, legs, and geometry LineString.
        """
        if len(coordinates) < 2:
            raise RoutingError("At least two coordinates (start and finish) are required.")

        cache_key = f"heigit_route_{'_'.join([f'{c[0]:.4f},{c[1]:.4f}' for c in coordinates])}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached:
                return cached

        payload = {
            'coordinates': coordinates,
            'radiuses': [-1] * len(coordinates),
            'instructions': True,
            'elevation': False
        }

        self.call_count += 1
        try:
            resp = requests.post(self.endpoint_url, json=payload, headers=self.headers, timeout=12.0)
            if resp.status_code == 429:
                raise RoutingError("Routing provider rate limit exceeded (HTTP 429). Please retry in a moment.")
            if resp.status_code in (401, 403):
                raise RoutingError("Routing provider authentication failed. Please check the HeiGIT API key.")
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

            geometry = feature.get('geometry', {})
            legs_raw = feature['properties'].get('segments', [])

            # Extract full-precision leg distances in miles
            leg_distances = [
                seg['distance'] * METERS_TO_MILES
                for seg in legs_raw
            ]

            result = {
                'distance_miles': distance_miles,
                'duration_hours': duration_seconds / 3600.0,
                'geometry': geometry,
                'legs': legs_raw,
                'leg_distances_miles': leg_distances,
                'raw_feature': feature
            }

            if use_cache:
                cache.set(cache_key, result, timeout=86400)

            return result
        except requests.RequestException as e:
            logger.error(f"HeiGIT routing request error: {e}")
            raise RoutingError(f"Driving route calculation failed: {str(e)}")

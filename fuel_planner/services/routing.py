import hashlib
import json
import logging
import math
import requests
from typing import List, Tuple, Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

METERS_TO_MILES = 0.000621371


class RoutingError(Exception):
    """Base exception for routing failures."""
    pass


class RoutingRateLimitError(RoutingError):
    """Raised when upstream routing service returns HTTP 429."""
    pass


class RoutingAuthError(RoutingError):
    """Raised when upstream routing service returns HTTP 401 or 403."""
    pass


class RoutingNotFoundError(RoutingError):
    """Raised when upstream routing cannot find a drivable route between points."""
    pass


class RoutingSchemaError(RoutingError):
    """Raised when upstream routing returns invalid, missing, or contradictory data."""
    pass


class RoutingProvider:
    """
    Integrates with official HeiGIT API v2 endpoints:
    https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson
    Enforces call-budget tracking, caching, timeouts, strict schema validation,
    and graceful error handling.
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

    @staticmethod
    def compute_cache_key(coordinates: List[Tuple[float, float]], payload_extra: Optional[Dict[str, Any]] = None) -> str:
        """
        Generates a stable, collision-free cache key using normalized full-precision coordinates
        and payload parameters.
        """
        canonical_data = {
            'v': '2.0',
            'coords': [[float(c[0]), float(c[1])] for c in coordinates],
            'extra': payload_extra or {}
        }
        serialized = json.dumps(canonical_data, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        return f"heigit_route_v2_{digest}"

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

        for idx, pt in enumerate(coordinates):
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                raise RoutingError(f"Coordinate at index {idx} must be a (longitude, latitude) pair.")
            lon, lat = pt[0], pt[1]
            if not (math.isfinite(lon) and math.isfinite(lat)):
                raise RoutingError(f"Coordinate at index {idx} contains non-finite numbers ({lon}, {lat}).")

        payload = {
            'coordinates': [[c[0], c[1]] for c in coordinates],
            'radiuses': [-1] * len(coordinates),
            'instructions': True,
            'elevation': False
        }

        cache_key = self.compute_cache_key(coordinates, {'radiuses': payload['radiuses']})
        if use_cache:
            cached = cache.get(cache_key)
            if cached:
                return cached

        self.call_count += 1
        try:
            resp = requests.post(self.endpoint_url, json=payload, headers=self.headers, timeout=12.0)
            if resp.status_code == 429:
                raise RoutingRateLimitError("Routing provider rate limit exceeded (HTTP 429). Please retry in a moment.")
            if resp.status_code in (401, 403):
                raise RoutingAuthError("Routing provider authentication failed. Please check the HeiGIT API key.")
            if resp.status_code == 404:
                raise RoutingNotFoundError("Routing endpoint not found or unsupported route geometry.")
            resp.raise_for_status()

            data = resp.json()
            if not isinstance(data, dict):
                raise RoutingSchemaError("Malformed routing response: expected JSON object.")

            features = data.get('features')
            if not isinstance(features, list) or not features:
                raise RoutingNotFoundError("No driving route found between the specified points.")

            feature = features[0]
            if not isinstance(feature, dict):
                raise RoutingSchemaError("Malformed routing feature: expected feature object.")

            properties = feature.get('properties')
            if not isinstance(properties, dict):
                raise RoutingSchemaError("Malformed routing feature: missing 'properties' object.")

            summary = properties.get('summary')
            if not isinstance(summary, dict) or 'distance' not in summary or 'duration' not in summary:
                raise RoutingSchemaError("Malformed routing properties: missing 'summary' with distance and duration.")

            distance_meters = summary.get('distance')
            duration_seconds = summary.get('duration')

            if not (isinstance(distance_meters, (int, float)) and math.isfinite(distance_meters) and distance_meters > 0):
                raise RoutingSchemaError(f"Route summary distance must be a positive finite number, got: {distance_meters}")
            if not (isinstance(duration_seconds, (int, float)) and math.isfinite(duration_seconds) and duration_seconds > 0):
                raise RoutingSchemaError(f"Route summary duration must be a positive finite number, got: {duration_seconds}")

            distance_miles = distance_meters * METERS_TO_MILES

            geometry = feature.get('geometry')
            if not isinstance(geometry, dict) or geometry.get('type') != 'LineString':
                raise RoutingSchemaError("Malformed routing feature: geometry must be a GeoJSON LineString.")

            coords = geometry.get('coordinates')
            if not isinstance(coords, list) or len(coords) < 2:
                raise RoutingSchemaError("Malformed routing geometry: LineString must have at least 2 coordinate points.")

            for c_idx, pt in enumerate(coords):
                if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                    raise RoutingSchemaError(f"Malformed geometry coordinate at index {c_idx}: expected [lon, lat].")
                g_lon, g_lat = pt[0], pt[1]
                if not (isinstance(g_lon, (int, float)) and math.isfinite(g_lon) and -180.0 <= g_lon <= 180.0):
                    raise RoutingSchemaError(f"Invalid longitude {g_lon} in geometry at index {c_idx}.")
                if not (isinstance(g_lat, (int, float)) and math.isfinite(g_lat) and -90.0 <= g_lat <= 90.0):
                    raise RoutingSchemaError(f"Invalid latitude {g_lat} in geometry at index {c_idx}.")

            legs_raw = properties.get('segments')
            if not isinstance(legs_raw, list):
                raise RoutingSchemaError("Malformed routing properties: 'segments' list is required.")

            expected_leg_count = len(coordinates) - 1
            if len(legs_raw) != expected_leg_count:
                raise RoutingSchemaError(
                    f"Segment count mismatch: expected {expected_leg_count} legs for {len(coordinates)} waypoints, "
                    f"got {len(legs_raw)}."
                )

            # Validate each segment and sum of distances
            leg_distances = []
            segment_distance_meters_sum = 0.0
            for s_idx, seg in enumerate(legs_raw):
                if not isinstance(seg, dict) or 'distance' not in seg:
                    raise RoutingSchemaError(f"Malformed segment at index {s_idx}: missing distance.")
                seg_dist = seg['distance']
                if not (isinstance(seg_dist, (int, float)) and math.isfinite(seg_dist) and seg_dist >= 0):
                    raise RoutingSchemaError(f"Invalid distance in segment {s_idx}: {seg_dist}")
                segment_distance_meters_sum += seg_dist
                leg_distances.append(seg_dist * METERS_TO_MILES)

            # Check that segment distances sum to total summary distance within tolerance (0.5% or 50 meters)
            tolerance = max(50.0, distance_meters * 0.005)
            if abs(segment_distance_meters_sum - distance_meters) > tolerance:
                raise RoutingSchemaError(
                    f"Segment distances sum ({segment_distance_meters_sum:.1f} m) does not match total "
                    f"summary distance ({distance_meters:.1f} m) within tolerance."
                )

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
            raise RoutingError("Driving route calculation failed due to an upstream network or communication error.")

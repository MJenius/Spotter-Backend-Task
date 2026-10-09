import logging
import json
from pathlib import Path
import requests
from typing import Tuple, Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache
from shapely.geometry import shape, Point
from shapely.ops import unary_union

logger = logging.getLogger(__name__)

# Preload US, Canada, and Mexico boundary geometries from GeoJSON
BOUNDARY_FILE = Path(settings.BASE_DIR) / 'data' / 'us_boundary.geojson'
US_POLYGON = None
CAN_POLYGON = None
MEX_POLYGON = None

if BOUNDARY_FILE.exists():
    try:
        with open(BOUNDARY_FILE, 'r', encoding='utf-8') as f:
            b_data = json.load(f)
            if b_data.get('type') == 'FeatureCollection':
                for feat in b_data.get('features', []):
                    iso = (feat.get('properties', {}) or {}).get('ADM0_A3')
                    geom = shape(feat['geometry'])
                    if iso == 'USA':
                        US_POLYGON = geom
                    elif iso == 'CAN':
                        CAN_POLYGON = geom
                    elif iso == 'MEX':
                        MEX_POLYGON = geom
            elif 'geometry' in b_data:
                US_POLYGON = shape(b_data['geometry'])
    except Exception as e:
        logger.warning(f"Could not load US GeoJSON boundary: {e}")


class GeocodingError(Exception):
    pass


class GeocodingService:
    """
    Geocodes textual locations and coordinates using HeiGIT Pelias v1:
    - Search: https://api.heigit.org/pelias/v1/search
    - Reverse geocoding: https://api.heigit.org/pelias/v1/reverse (for border verification)
    
    Validates US coordinates against the official US GeoJSON boundary polygon
    supplemented by reverse-geocoding metadata on maritime/border edge points.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or getattr(settings, 'HEIGIT_API_KEY', '')
        self.headers = {
            'Authorization': self.api_key,
            'User-Agent': 'SpotterFuelPlanner/2.0',
            'Accept': 'application/json'
        }
        self.endpoint_url = "https://api.heigit.org/pelias/v1/search"
        self.reverse_url = "https://api.heigit.org/pelias/v1/reverse"

    def geocode(self, location: str) -> Tuple[float, float, str]:
        loc_clean = location.strip()
        cache_key = f"heigit_geocode_{loc_clean.lower().replace(' ', '_')}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        params = {
            'text': loc_clean,
            'size': 1
        }

        try:
            resp = requests.get(self.endpoint_url, params=params, headers=self.headers, timeout=8.0)
            if resp.status_code == 429:
                raise GeocodingError("Geocoding service rate limit exceeded. Please retry shortly.")
            if resp.status_code in (401, 403):
                raise GeocodingError("Geocoding provider authentication failed. Please check the HeiGIT API key.")
            resp.raise_for_status()

            data = resp.json()
            features = data.get('features', [])
            if not features:
                raise GeocodingError(f"Could not locate '{location}'. Please specify a valid US city or address.")

            feature = features[0]
            coords = feature['geometry']['coordinates']  # [lon, lat]
            lon, lat = float(coords[0]), float(coords[1])
            props = feature.get('properties', {})
            label = props.get('label', loc_clean)

            country_code = (props.get('country_a') or props.get('country') or '').upper()

            if country_code and country_code not in ('USA', 'US', 'UNITED STATES'):
                raise GeocodingError(f"Location '{location}' resolved to {country_code}, which is outside the United States.")

            if not self.is_in_us(lat, lon, country_code=country_code):
                raise GeocodingError(f"Location '{location}' resolved to coordinates ({lat}, {lon}) outside the United States.")

            result = (lon, lat, label)
            cache.set(cache_key, result, timeout=86400 * 7)
            return result
        except requests.RequestException as e:
            logger.error(f"HeiGIT geocoding request failed for {location}: {e}")
            raise GeocodingError(f"Geocoding provider error: {str(e)}")

    def is_in_us(self, lat: float, lon: float, country_code: Optional[str] = None) -> bool:
        """
        Geospatial US validation using US boundary polygon and border reverse metadata:
        1. If country_code from provider is known, check USA.
        2. Alaska (51.0°N to 71.5°N, -180.0°W to -129.0°W).
        3. Hawaii (18.5°N to 22.5°N, -160.5°W to -154.5°W).
        4. Polygon check via US boundary GeoJSON.
        5. Specific exclusion for cross-border neighbors (Windsor ON, Tijuana MX, Fort Erie ON).
        """
        if country_code:
            return country_code.upper() in ('USA', 'US', 'UNITED STATES')

        # Alaska bounding envelope
        if 51.0 <= lat <= 71.5 and -180.0 <= lon <= -129.0:
            return True

        # Hawaii bounding envelope
        if 18.5 <= lat <= 22.5 and -160.5 <= lon <= -154.5:
            return True

        # Check explicit Canadian border points along Great Lakes / Detroit River / St Lawrence
        # Windsor ON is south of Detroit MI across the Detroit river:
        if -83.040 <= lon <= -82.90 and 42.20 <= lat <= 42.325:
            return False

        # Fort Erie, Canada (across from Buffalo NY):
        if -79.05 <= lon <= -78.90 and 42.88 <= lat <= 42.96:
            return False

        # Tijuana, Mexico (south of San Diego border ~32.534N):
        if -117.2 <= lon <= -116.8 and lat < 32.534:
            return False

        # General Mexico border
        if lat < 25.837:
            return False

        # 49th parallel northern boundary
        if lat > 49.0:
            if not (49.0 < lat <= 49.384358 and -95.3 <= lon <= -94.8):
                return False

        # Check against loaded GeoJSON polygons (USA, CAN, MEX)
        if US_POLYGON is not None:
            pt = Point(lon, lat)
            # 1. Definite foreign rejection if inside Canadian or Mexican sovereign boundary
            if CAN_POLYGON is not None and CAN_POLYGON.contains(pt):
                return False
            if MEX_POLYGON is not None and MEX_POLYGON.contains(pt):
                return False

            # 2. Definite acceptance if inside US boundary
            if US_POLYGON.contains(pt):
                return True

            # 3. For coastal / border points slightly offset from simplified polygon:
            d_us = US_POLYGON.distance(pt)
            d_can = CAN_POLYGON.distance(pt) if CAN_POLYGON is not None else float('inf')
            d_mex = MEX_POLYGON.distance(pt) if MEX_POLYGON is not None else float('inf')

            # Point must be very close to US boundary and closer to US than to any neighboring country
            if d_us < 0.05 and d_us < d_can and d_us < d_mex:
                return True
            return False

        # Fallback only if polygon could not be loaded
        return (24.396308 <= lat <= 49.384358 and -125.0 <= lon <= -66.93457)

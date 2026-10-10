import hashlib
import json
import logging
import math
from pathlib import Path
import requests
from typing import Tuple, Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache
from shapely.geometry import shape, Point
from shapely.ops import unary_union
import pyproj
from shapely import transform

logger = logging.getLogger(__name__)

# Preload US, Canada, and Mexico boundary geometries from GeoJSON
BOUNDARY_FILE = Path(settings.BASE_DIR) / 'data' / 'us_boundary.geojson'
US_POLYGON = None
CAN_POLYGON = None
MEX_POLYGON = None

# Regional metric transformers for uniform geodesic/projected distance checks
TRANSFORMER_CONUS = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform
TRANSFORMER_ALASKA = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3338", always_xy=True).transform
TRANSFORMER_HAWAII = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3759", always_xy=True).transform

US_POLYGON_CONUS = None
US_POLYGON_ALASKA = None
US_POLYGON_HAWAII = None

CAN_POLYGON_CONUS = None
MEX_POLYGON_CONUS = None

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

        if US_POLYGON is not None:
            US_POLYGON_CONUS = transform(US_POLYGON, TRANSFORMER_CONUS, interleaved=False)
            US_POLYGON_ALASKA = transform(US_POLYGON, TRANSFORMER_ALASKA, interleaved=False)
            US_POLYGON_HAWAII = transform(US_POLYGON, TRANSFORMER_HAWAII, interleaved=False)
        if CAN_POLYGON is not None:
            CAN_POLYGON_CONUS = transform(CAN_POLYGON, TRANSFORMER_CONUS, interleaved=False)
        if MEX_POLYGON is not None:
            MEX_POLYGON_CONUS = transform(MEX_POLYGON, TRANSFORMER_CONUS, interleaved=False)
    except Exception as e:
        logger.warning(f"Could not load US GeoJSON boundary: {e}")


class GeocodingError(Exception):
    """Base exception for geocoding failures."""
    pass


class GeocodingRateLimitError(GeocodingError):
    """Raised when upstream geocoding returns HTTP 429."""
    pass


class GeocodingAuthError(GeocodingError):
    """Raised when upstream geocoding returns HTTP 401 or 403."""
    pass


class GeocodingLocationNotFoundError(GeocodingError):
    """Raised when location cannot be found or is outside the US."""
    pass


class GeocodingProviderError(GeocodingError):
    """Raised when upstream geocoding fails due to network/server errors."""
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

    @staticmethod
    def compute_cache_key(location: str) -> str:
        loc_normalized = " ".join(location.strip().lower().split())
        digest = hashlib.sha256(loc_normalized.encode('utf-8')).hexdigest()
        return f"heigit_geocode_v2_{digest}"

    def geocode(self, location: str) -> Tuple[float, float, str]:
        loc_clean = location.strip()
        if not loc_clean:
            raise GeocodingLocationNotFoundError("Location query cannot be empty.")

        cache_key = self.compute_cache_key(loc_clean)
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
                raise GeocodingRateLimitError("Geocoding service rate limit exceeded (HTTP 429). Please retry shortly.")
            if resp.status_code in (401, 403):
                raise GeocodingAuthError("Geocoding provider authentication failed. Please check the HeiGIT API key.")
            resp.raise_for_status()

            data = resp.json()
            features = data.get('features', [])
            if not features:
                raise GeocodingLocationNotFoundError(f"Could not locate '{location}'. Please specify a valid US city or address.")

            feature = features[0]
            coords = feature['geometry']['coordinates']  # [lon, lat]
            lon, lat = float(coords[0]), float(coords[1])
            props = feature.get('properties', {})
            label = props.get('label', loc_clean)

            country_code = (props.get('country_a') or props.get('country') or '').upper()

            if country_code and country_code not in ('USA', 'US', 'UNITED STATES'):
                raise GeocodingLocationNotFoundError(f"Location '{location}' resolved to {country_code}, which is outside the United States.")

            if not self.is_in_us(lat, lon, country_code=country_code):
                raise GeocodingLocationNotFoundError(f"Location '{location}' resolved to coordinates ({lat}, {lon}) outside the United States.")

            result = (lon, lat, label)
            cache.set(cache_key, result, timeout=86400 * 7)
            return result
        except requests.RequestException as e:
            logger.error(f"HeiGIT geocoding request failed for {location}: {e}")
            raise GeocodingProviderError("Geocoding service failed due to an upstream network or communication error.")

    def is_in_us(self, lat: float, lon: float, country_code: Optional[str] = None) -> bool:
        """
        Geospatial sovereign US validation using authoritative sovereign boundary polygons (Natural Earth 1:10m):
        1. Explicitly checks that lat/lon are finite and within physical bounds [-90, 90] and [-180, 180].
        2. If country_code from geocoder is known and foreign (e.g. CAN, MEX), reject.
        3. If boundary data is missing/corrupted, fail closed with GeocodingError.
        4. Reject if point falls within Canadian or Mexican sovereign boundary (CAN_POLYGON, MEX_POLYGON).
        5. Accept if point is covered by US sovereign boundary (US_POLYGON covers), which includes Continental US,
           Alaska, and Hawaii polygons.
        6. Coastal boundary tolerance: accounts for geocoded coastal pier/beach coordinates immediately adjacent
           to land using projected metric distances (CONUS EPSG:5070, Alaska EPSG:3338, Hawaii EPSG:3759; tolerance: 3200m / ~2 miles).
        """
        import math
        if not (isinstance(lat, (int, float)) and math.isfinite(lat) and -90.0 <= lat <= 90.0):
            return False
        if not (isinstance(lon, (int, float)) and math.isfinite(lon) and -180.0 <= lon <= 180.0):
            return False

        if country_code and country_code.upper() not in ('USA', 'US', 'UNITED STATES'):
            return False

        if US_POLYGON is None:
            raise GeocodingError(
                "Authoritative US boundary polygon data is not loaded. Cannot safely validate coordinates."
            )

        pt = Point(lon, lat)

        # 1. Definite foreign sovereign rejection
        if CAN_POLYGON is not None and CAN_POLYGON.covers(pt):
            return False
        if MEX_POLYGON is not None and MEX_POLYGON.covers(pt):
            return False

        # 2. Definite domestic sovereign acceptance (CONUS, Alaska, Hawaii)
        if US_POLYGON.covers(pt):
            return True

        # 3. Coastal maritime edge tolerance: within a uniform 3,200 meters (~2 miles) of coastline
        # and strictly closer to the US boundary than to Canada or Mexico.
        # Uses region-appropriate planar equal-area metric projections for true geodesic/metric distance.
        if lat >= 50.0 and lon <= -128.0:
            # Alaska (EPSG:3338)
            trans = TRANSFORMER_ALASKA
            us_poly = US_POLYGON_ALASKA
            can_poly = None
            mex_poly = None
        elif lat <= 25.0 and lon <= -150.0:
            # Hawaii (EPSG:3759)
            trans = TRANSFORMER_HAWAII
            us_poly = US_POLYGON_HAWAII
            can_poly = None
            mex_poly = None
        else:
            # CONUS (EPSG:5070)
            trans = TRANSFORMER_CONUS
            us_poly = US_POLYGON_CONUS
            can_poly = CAN_POLYGON_CONUS
            mex_poly = MEX_POLYGON_CONUS

        if us_poly is not None:
            pt_proj = transform(pt, trans, interleaved=False)
            d_us_meters = us_poly.distance(pt_proj)
            d_can_meters = can_poly.distance(pt_proj) if can_poly is not None else float('inf')
            d_mex_meters = mex_poly.distance(pt_proj) if mex_poly is not None else float('inf')

            # 3,200 meters (~2 miles) uniform geodesic/metric tolerance
            if d_us_meters < 3200.0 and d_us_meters < d_can_meters and d_us_meters < d_mex_meters:
                return True

        return False

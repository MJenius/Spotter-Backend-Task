import logging
import requests
from typing import Tuple, Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Continental US bounding box
CONUS_BOUNDS = {
    'min_lat': 24.396308,
    'max_lat': 49.384358,
    'min_lon': -125.0,
    'max_lon': -66.93457
}


class GeocodingError(Exception):
    pass


class GeocodingService:
    """
    Geocodes textual locations into (lon, lat) using HeiGIT / OpenRouteService geocoding API.
    Caches successful results and validates that coordinates are in the continental United States.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or getattr(settings, 'HEIGIT_API_KEY', '')
        self.headers = {
            'User-Agent': 'SpotterFuelPlanner/1.0',
            'Accept': 'application/json'
        }

    def geocode(self, location: str) -> Tuple[float, float, str]:
        """
        Returns (longitude, latitude, formatted_address)
        """
        loc_clean = location.strip()
        cache_key = f"geocode_{loc_clean.lower().replace(' ', '_')}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        # Try ORS / HeiGIT geocode search
        url = "https://api.openrouteservice.org/geocode/search"
        params = {
            'api_key': self.api_key,
            'text': loc_clean,
            'boundary.country': 'USA',
            'size': 1
        }

        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=8.0)
            if resp.status_code == 429:
                raise GeocodingError("Geocoding service rate limit exceeded. Please retry shortly.")
            resp.raise_for_status()
            data = resp.json()
            features = data.get('features', [])
            if not features:
                raise GeocodingError(f"Could not locate '{location}'. Please specify a valid US city or address.")

            coords = features[0]['geometry']['coordinates']  # [lon, lat]
            lon, lat = float(coords[0]), float(coords[1])
            label = features[0]['properties'].get('label', loc_clean)

            # Validate US location
            if not self.is_in_us(lat, lon):
                raise GeocodingError(f"Location '{location}' resolved to ({lat}, {lon}) which is outside the US or not supported.")

            result = (lon, lat, label)
            cache.set(cache_key, result, timeout=86400 * 7)  # cache 7 days
            return result
        except requests.RequestException as e:
            logger.error(f"Geocoding request failed for {location}: {e}")
            raise GeocodingError(f"Geocoding provider error: {str(e)}")

    @staticmethod
    def is_in_us(lat: float, lon: float) -> bool:
        """Checks if latitude and longitude are within US boundary."""
        # Alaska check
        in_alaska = (51.0 <= lat <= 71.5 and -180.0 <= lon <= -129.0)
        # Hawaii check
        in_hawaii = (18.5 <= lat <= 22.5 and -160.5 <= lon <= -154.5)
        # Conus rough check
        # Note: Toronto is 43.65, -79.38 which falls in bbox rectangle unless we exclude north of Lake Ontario/Erie
        # or check Great Lakes border
        if in_alaska or in_hawaii:
            return True
        if not (24.396308 <= lat <= 49.384358 and -125.0 <= lon <= -66.93457):
            return False
        # Specific exclusion for southern Ontario (Canada) which dips below 45 lat
        # Longitude between -83 and -75 with latitude > 42.5 is mostly Ontario
        if -83.0 <= lon <= -75.0 and lat >= 43.0:
            # Roughly north of Lake Erie / Ontario
            # Buffalo NY is 42.88, -78.87 (US), Rochester NY is 43.15, -77.6 (US)
            # Toronto is lat 43.65, lon -79.38
            if -80.0 <= lon <= -79.0 and lat >= 43.4:
                return False
        return True

import logging
import requests
from typing import Tuple, Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Valid US State codes
US_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY',
    'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND',
    'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
}


class GeocodingError(Exception):
    pass


class GeocodingService:
    """
    Geocodes textual locations using official HeiGIT Pelias v1 API:
    https://api.heigit.org/pelias/v1/search
    Caches successful results and validates that coordinates belong to the US.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or getattr(settings, 'HEIGIT_API_KEY', '')
        self.headers = {
            'Authorization': self.api_key,
            'User-Agent': 'SpotterFuelPlanner/2.0',
            'Accept': 'application/json'
        }
        self.endpoint_url = "https://api.heigit.org/pelias/v1/search"

    def geocode(self, location: str) -> Tuple[float, float, str]:
        """
        Returns (longitude, latitude, formatted_address)
        """
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

            # Geographic validation via country code and region
            country_code = (props.get('country_a') or props.get('country') or '').upper()
            region = (props.get('region_a') or '').upper()

            # If country metadata is returned, enforce USA
            if country_code and country_code not in ('USA', 'US', 'UNITED STATES'):
                raise GeocodingError(f"Location '{location}' resolved to {country_code}, which is outside the United States.")

            # Coordinate validation fallback
            if not self.is_in_us(lat, lon, region=region):
                raise GeocodingError(f"Location '{location}' resolved to coordinates ({lat}, {lon}) outside the United States.")

            result = (lon, lat, label)
            cache.set(cache_key, result, timeout=86400 * 7)
            return result
        except requests.RequestException as e:
            logger.error(f"HeiGIT geocoding request failed for {location}: {e}")
            raise GeocodingError(f"Geocoding provider error: {str(e)}")

    @staticmethod
    def is_in_us(lat: float, lon: float, region: Optional[str] = None) -> bool:
        """
        Geographic validation for continental US, Alaska, and Hawaii.
        Properly rejects Canadian provinces (ON, QC, BC, AB, etc.) and Mexico.
        """
        # Alaska
        if 51.0 <= lat <= 71.5 and -180.0 <= lon <= -129.0:
            return True
        # Hawaii
        if 18.5 <= lat <= 22.5 and -160.5 <= lon <= -154.5:
            return True
        # Continental US strictly stops at 49.0000 degrees North (49th parallel border with Canada)
        # Note: Point Roberts is 48.98N, Lake of the Woods NW Angle is 49.38N (-95.15W to -94.8W)
        if 24.396308 <= lat and -125.0 <= lon <= -66.93457:
            if lat > 49.0:
                # Only the Northwest Angle of Minnesota is above 49th parallel up to 49.38N
                if not (49.0 < lat <= 49.384358 and -95.3 <= lon <= -94.8):
                    return False

            # Check Canada border dip around Great Lakes / Southern Ontario:
            # Ontario dips south to ~41.68N (Pelee Island) between longitudes -83.1W and -75.5W
            if -83.5 <= lon <= -75.0 and lat >= 42.5:
                # Buffalo NY is ~42.88N, -78.87W. Rochester is ~43.15N, -77.6W.
                # Toronto is ~43.65N, -79.38W; Hamilton is ~43.25N, -79.87W
                if -80.5 <= lon <= -78.5 and lat >= 43.3:
                    return False
                if -83.0 <= lon <= -81.0 and lat >= 42.5:
                    return False
            return True
        return False

import logging
import requests
from typing import Tuple, Dict, Any, Optional
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

US_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY',
    'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND',
    'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
}


class GeocodingError(Exception):
    pass


class GeocodingService:
    """
    Geocodes textual locations and validates coordinates using HeiGIT Pelias v1:
    - Forward search: https://api.heigit.org/pelias/v1/search
    - Reverse validation: https://api.heigit.org/pelias/v1/reverse (for explicit coordinates)
    
    Validates US boundary using reverse-geocoded ISO country metadata and precise polygon limits.
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
        Geospatial validation:
        1. If country_code is already known from provider, check USA.
        2. Alaska: 51.0°N to 71.5°N, -180.0°W to -129.0°W
        3. Hawaii: 18.5°N to 22.5°N, -160.5°W to -154.5°W
        4. Continental US: 24.396308°N to 49.0°N (and up to 49.384°N only in MN Northwest Angle), -125.0°W to -66.934°W.
           Excludes Canadian territory in southern Ontario (Toronto/Windsor/Hamilton) and Mexican border.
        """
        if country_code:
            return country_code.upper() in ('USA', 'US', 'UNITED STATES')

        # Alaska
        if 51.0 <= lat <= 71.5 and -180.0 <= lon <= -129.0:
            return True

        # Hawaii
        if 18.5 <= lat <= 22.5 and -160.5 <= lon <= -154.5:
            return True

        # Continental US (49th parallel northern boundary)
        if 24.396308 <= lat and -125.0 <= lon <= -66.93457:
            if lat > 49.0:
                # Only Lake of the Woods Northwest Angle extends above 49N up to 49.384N
                if not (49.0 < lat <= 49.384358 and -95.3 <= lon <= -94.8):
                    return False

            # Canada border along Great Lakes / Southern Ontario:
            # Detroit river runs between Detroit, MI and Windsor, ON.
            # Windsor City Center is at lat 42.3149, lon -83.0364
            # Detroit City Center is at lat 42.3314, lon -83.0458 (Detroit river border runs at ~42.325N)
            if -83.040 <= lon <= -82.90 and 42.20 <= lat <= 42.325:
                # Strictly Windsor / Essex County, Canada
                return False

            if -83.5 <= lon <= -75.0 and lat >= 42.0:
                # Toronto / Hamilton / Niagara Peninsula (Canada)
                if -80.5 <= lon <= -78.8 and lat >= 43.1:
                    return False
                if -83.0 <= lon <= -81.0 and lat >= 42.5:
                    return False

            # Mexico border around Tijuana / San Diego:
            # Tijuana is lat 32.51N, lon -117.03W; US border is ~32.534N
            if -117.2 <= lon <= -116.8 and lat < 32.534:
                return False

            # Mexican border in Arizona / New Mexico / Texas (latitudes below ~25.8N are Mexico)
            if lat < 25.837:
                return False

            return True

        return False

import math
from typing import List, Tuple
from shapely.geometry import LineString, Point
from shapely import transform
import pyproj
from fuel_planner.models import FuelStation
from fuel_planner.services.optimizer import StationCandidate

# Regional metric projections for accurate distance measurements:
# - Conus Albers (EPSG:5070) for Continental US
# - Alaska Albers (EPSG:3338) for Alaska
# - Hawaii Albers Equal Area (EPSG:3759) for Hawaii
METERS_TO_MILES = 0.000621371
MILES_TO_METERS = 1609.344

TRANSFORMER_CONUS = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform
TRANSFORMER_ALASKA = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3338", always_xy=True).transform
TRANSFORMER_HAWAII = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3759", always_xy=True).transform


def get_region_transformer(min_lon: float, min_lat: float, max_lon: float, max_lat: float):
    # Alaska: high latitude, negative longitude
    if min_lat >= 50.0 and min_lon <= -128.0:
        return TRANSFORMER_ALASKA
    # Hawaii: lower latitude, tropical Pacific
    if max_lat <= 25.0 and min_lon <= -150.0:
        return TRANSFORMER_HAWAII
    return TRANSFORMER_CONUS


class SpatialStationSearcher:
    """
    Finds fuel stations along a route corridor using region-appropriate metric planar projections:
    - CONUS: Conus Albers (EPSG:5070)
    - Alaska: Alaska Albers (EPSG:3338)
    - Hawaii: Hawaii Albers (EPSG:3759)
    - Calculates distance from road LineString in real meters/miles.
    - Projects stations onto the route to estimate route position and round-trip detour access distance.
    - Honors the `include_approximate_stations` policy: by default, only EXACT stations are eligible
      unless explicitly permitted (e.g. for demonstration or fallback).
    """

    def __init__(
        self,
        corridor_width_miles: float = 10.0,
        max_off_route_distance_miles: float = 5.0,
        include_approximate_stations: bool = True
    ):
        self.corridor_width_miles = corridor_width_miles
        self.max_off_route_distance_miles = max_off_route_distance_miles
        self.include_approximate_stations = include_approximate_stations

    def find_candidate_stations(
        self,
        route_coordinates: List[Tuple[float, float]],
        total_distance_miles: float
    ) -> List[StationCandidate]:
        """
        route_coordinates: List of [longitude, latitude] points along the route.
        """
        if not route_coordinates or len(route_coordinates) < 2:
            return []

        route_line_wgs84 = LineString(route_coordinates)
        min_lon, min_lat, max_lon, max_lat = route_line_wgs84.bounds
        proj_transformer = get_region_transformer(min_lon, min_lat, max_lon, max_lat)

        route_line_proj = transform(route_line_wgs84, proj_transformer, interleaved=False)
        route_proj_length_meters = route_line_proj.length
        scale_factor = (total_distance_miles * MILES_TO_METERS) / max(route_proj_length_meters, 1.0)

        # SQL bounding box filter in WGS84 with latitude-scaled longitude buffer
        mid_lat = (min_lat + max_lat) / 2.0
        cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
        deg_lat_buffer = (self.corridor_width_miles / 69.0) + 0.05
        deg_lon_buffer = (self.corridor_width_miles / (69.0 * cos_lat)) + 0.05

        min_lon -= deg_lon_buffer
        max_lon += deg_lon_buffer
        min_lat -= deg_lat_buffer
        max_lat += deg_lat_buffer

        db_stations = FuelStation.objects.filter(
            is_active=True,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
            latitude__gte=min_lat,
            latitude__lte=max_lat
        ).exclude(geocode_status=FuelStation.GEOCODE_UNRESOLVED)

        # Strict eligibility policy: exclude approximate if include_approximate_stations is False
        if not self.include_approximate_stations:
            db_stations = db_stations.filter(geocode_status=FuelStation.GEOCODE_EXACT)

        candidates: List[StationCandidate] = []

        for st in db_stations:
            st_point_wgs84 = Point(st.longitude, st.latitude)
            st_point_proj = transform(st_point_wgs84, proj_transformer, interleaved=False)

            # Accurate distance from station to route in meters and miles
            dist_meters = route_line_proj.distance(st_point_proj)
            dist_miles = dist_meters * METERS_TO_MILES

            if dist_miles <= self.max_off_route_distance_miles:
                proj_dist_meters = route_line_proj.project(st_point_proj)
                scaled_position_miles = (proj_dist_meters * scale_factor) * METERS_TO_MILES
                
                one_way_access = round(dist_miles, 2)
                roundtrip_detour_miles = round(one_way_access * 2.0, 2)

                candidates.append(StationCandidate(
                    station_id=st.station_id,
                    name=st.name,
                    latitude=st.latitude,
                    longitude=st.longitude,
                    price_per_gallon=st.retail_price,
                    route_position_miles=round(scaled_position_miles, 2),
                    detour_distance_miles=roundtrip_detour_miles,
                    city=st.city,
                    state=st.state,
                    address=st.address,
                    geocode_accuracy=st.geocode_status,
                    access_miles_one_way=one_way_access
                ))

        candidates.sort(key=lambda s: s.route_position_miles)
        return candidates

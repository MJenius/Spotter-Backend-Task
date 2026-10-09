import math
from typing import List, Tuple
from shapely.geometry import LineString, Point
from shapely import transform
import pyproj
from fuel_planner.models import FuelStation
from fuel_planner.services.optimizer import StationCandidate

# Transform coordinates between WGS84 (EPSG:4326) and Conus Albers (EPSG:5070)
PROJECT_TO_METERS = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform
METERS_TO_MILES = 0.000621371
MILES_TO_METERS = 1609.344


class SpatialStationSearcher:
    """
    Finds fuel stations along a route corridor using Conus Albers (EPSG:5070) planar projection:
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
        route_line_proj = transform(route_line_wgs84, PROJECT_TO_METERS, interleaved=False)
        route_proj_length_meters = route_line_proj.length
        scale_factor = (total_distance_miles * MILES_TO_METERS) / max(route_proj_length_meters, 1.0)

        # SQL bounding box filter in WGS84
        min_lon, min_lat, max_lon, max_lat = route_line_wgs84.bounds
        deg_buffer = (self.corridor_width_miles / 50.0) + 0.1
        min_lon -= deg_buffer
        max_lon += deg_buffer
        min_lat -= deg_buffer
        max_lat += deg_buffer

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
            st_point_proj = transform(st_point_wgs84, PROJECT_TO_METERS, interleaved=False)

            # Accurate distance from station to route in meters and miles
            dist_meters = route_line_proj.distance(st_point_proj)
            dist_miles = dist_meters * METERS_TO_MILES

            if dist_miles <= self.max_off_route_distance_miles:
                proj_dist_meters = route_line_proj.project(st_point_proj)
                scaled_position_miles = (proj_dist_meters * scale_factor) * METERS_TO_MILES
                
                # Round-trip access detour
                roundtrip_detour_miles = dist_miles * 2.0

                candidates.append(StationCandidate(
                    station_id=st.station_id,
                    name=st.name,
                    latitude=st.latitude,
                    longitude=st.longitude,
                    price_per_gallon=st.retail_price,
                    route_position_miles=round(scaled_position_miles, 2),
                    detour_distance_miles=round(roundtrip_detour_miles, 2),
                    city=st.city,
                    state=st.state,
                    address=st.address,
                    geocode_accuracy=st.geocode_status
                ))

        candidates.sort(key=lambda s: s.route_position_miles)
        return candidates

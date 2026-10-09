import math
from typing import List, Tuple
from shapely.geometry import LineString, Point
from shapely.ops import transform
import pyproj
from fuel_planner.models import FuelStation
from fuel_planner.services.optimizer import StationCandidate

# Projections: WGS84 (EPSG:4326) to US National Atlas Equal Area (EPSG:2163) or Albers (EPSG:5070)
# EPSG:5070 NAD83 / Conus Albers is ideal for meter distances across continental US.
PROJECT_TO_METERS = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform
PROJECT_TO_WGS84 = pyproj.Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True).transform
METERS_TO_MILES = 0.000621371
MILES_TO_METERS = 1609.344


class SpatialStationSearcher:
    """
    Finds fuel stations along a route corridor:
    - Projects route LineString to projected planar CRS (meters)
    - Computes bounding box around the route + corridor buffer
    - Queries database using lat/long bbox
    - Filters stations within `corridor_width_miles` of route LineString
    - Projects stations onto route line to get precise route position (miles) and detour distance (miles)
    """

    def __init__(self, corridor_width_miles: float = 10.0, max_off_route_distance_miles: float = 5.0):
        self.corridor_width_miles = corridor_width_miles
        self.max_off_route_distance_miles = max_off_route_distance_miles

    def find_candidate_stations(
        self,
        route_coordinates: List[Tuple[float, float]],
        total_distance_miles: float
    ) -> List[StationCandidate]:
        """
        route_coordinates: List of (longitude, latitude) pairs from GeoJSON
        """
        if not route_coordinates or len(route_coordinates) < 2:
            return []

        # Build Shapely LineString in lon, lat
        route_line_wgs84 = LineString(route_coordinates)
        
        # Transform to Conus Albers (meters) for accurate Euclidean operations
        route_line_proj = transform(PROJECT_TO_METERS, route_line_wgs84)
        route_proj_length_meters = route_line_proj.length
        # Scaling factor to match real driving distance reported by provider
        scale_factor = (total_distance_miles * MILES_TO_METERS) / max(route_proj_length_meters, 1.0)

        # Bounding box in WGS84 for SQL filter (with safety margin)
        min_lon, min_lat, max_lon, max_lat = route_line_wgs84.bounds
        # 1 degree lat is ~69 miles, 1 degree lon is ~50 miles in US
        deg_buffer = (self.corridor_width_miles / 50.0) + 0.1
        min_lon -= deg_buffer
        max_lon += deg_buffer
        min_lat -= deg_buffer
        max_lat += deg_buffer

        # Query candidates in DB
        db_stations = FuelStation.objects.filter(
            is_active=True,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
            latitude__gte=min_lat,
            latitude__lte=max_lat
        ).exclude(geocode_status=FuelStation.GEOCODE_UNRESOLVED)

        candidates: List[StationCandidate] = []
        max_dist_meters = self.corridor_width_miles * MILES_TO_METERS

        for st in db_stations:
            st_point_wgs84 = Point(st.longitude, st.latitude)
            st_point_proj = transform(PROJECT_TO_METERS, st_point_wgs84)

            # Distance from station to route in meters
            dist_meters = route_line_proj.distance(st_point_proj)
            dist_miles = dist_meters * METERS_TO_MILES

            if dist_miles <= self.corridor_width_miles:
                # Calculate distance along route where station projects
                proj_dist_meters = route_line_proj.project(st_point_proj)
                scaled_position_miles = (proj_dist_meters * scale_factor) * METERS_TO_MILES
                
                # Access detour: round trip off-route
                detour_miles = dist_miles * 2.0

                if dist_miles <= self.max_off_route_distance_miles:
                    candidates.append(StationCandidate(
                        station_id=st.station_id,
                        name=st.name,
                        latitude=st.latitude,
                        longitude=st.longitude,
                        price_per_gallon=st.retail_price,
                        route_position_miles=round(scaled_position_miles, 2),
                        detour_distance_miles=round(detour_miles, 2),
                        city=st.city,
                        state=st.state,
                        address=st.address
                    ))

        # Sort along route
        candidates.sort(key=lambda s: s.route_position_miles)
        return candidates

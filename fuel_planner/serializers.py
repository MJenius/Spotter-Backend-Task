from rest_framework import serializers


class RoutePlanRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        required=True,
        help_text="Start location text (e.g. 'Los Angeles, CA') or coordinate string 'lat, lon'"
    )
    finish = serializers.CharField(
        required=True,
        help_text="Destination location text (e.g. 'Las Vegas, NV') or coordinate string 'lat, lon'"
    )
    starting_fuel_gallons = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=50.0,
        default=50.0,
        help_text="Fuel in vehicle tank at departure in gallons (0.0 to 50.0). Default is 50.0."
    )
    max_off_route_distance_miles = serializers.FloatField(
        required=False,
        min_value=0.5,
        max_value=25.0,
        default=5.0,
        help_text="Maximum one-way detour distance to consider fuel stations (miles). Default is 5.0."
    )
    allow_approximate_stations = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Allow stations enriched with approximate city-centroid coordinates. Defaults to FALSE for physical location accuracy."
    )

import math
from rest_framework import serializers


class RoutePlanRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        required=True,
        max_length=255,
        trim_whitespace=True,
        help_text="Start location text (e.g. 'Los Angeles, CA') or coordinate string 'lat, lon'"
    )
    finish = serializers.CharField(
        required=True,
        max_length=255,
        trim_whitespace=True,
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

    def validate_start(self, value):
        v = value.strip()
        if not v or len(v) < 2:
            raise serializers.ValidationError("Start location must be at least 2 characters.")
        return v

    def validate_finish(self, value):
        v = value.strip()
        if not v or len(v) < 2:
            raise serializers.ValidationError("Finish location must be at least 2 characters.")
        return v

    def validate_starting_fuel_gallons(self, value):
        if value is not None and (math.isnan(value) or math.isinf(value)):
            raise serializers.ValidationError("Starting fuel must be a finite number.")
        return value

    def validate_max_off_route_distance_miles(self, value):
        if value is not None and (math.isnan(value) or math.isinf(value)):
            raise serializers.ValidationError("Max detour distance must be a finite number.")
        return value

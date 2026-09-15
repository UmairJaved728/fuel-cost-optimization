"""API request/response serializers."""

from rest_framework import serializers


class PlanLocationSerializer(serializers.Serializer):
    """A single location: either coordinates or an address (never both)."""

    address = serializers.CharField(
        max_length=300, allow_blank=False, required=False, trim_whitespace=True
    )
    latitude = serializers.FloatField(required=False, min_value=-90.0, max_value=90.0)
    longitude = serializers.FloatField(required=False, min_value=-180.0, max_value=180.0)

    def validate(self, attrs):
        has_address = bool(attrs.get("address"))
        has_lat = attrs.get("latitude") is not None
        has_lon = attrs.get("longitude") is not None

        if has_address and (has_lat or has_lon):
            raise serializers.ValidationError(
                "A location must contain exactly one of an address or coordinates."
            )
        if not has_address and not (has_lat and has_lon):
            raise serializers.ValidationError(
                "A location must contain either an address or both latitude and longitude."
            )
        if has_lat != has_lon:
            raise serializers.ValidationError(
                "latitude and longitude must be supplied together."
            )
        return attrs


class PlanRequestSerializer(serializers.Serializer):
    """POST /api/v1/routes/plan/ payload."""

    start = PlanLocationSerializer(required=True)
    finish = PlanLocationSerializer(required=True)


class StationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


class FuelStopSerializer(serializers.Serializer):
    stop_number = serializers.IntegerField()
    station = StationSerializer()
    route_position_miles = serializers.FloatField()
    distance_from_previous_stop_miles = serializers.FloatField()
    retail_price_per_gallon = serializers.DecimalField(max_digits=12, decimal_places=3)
    fuel_before_stop_gallons = serializers.DecimalField(max_digits=12, decimal_places=2)
    fuel_purchased_gallons = serializers.DecimalField(max_digits=12, decimal_places=2)
    fuel_after_stop_gallons = serializers.DecimalField(max_digits=12, decimal_places=2)
    estimated_cost = serializers.DecimalField(max_digits=12, decimal_places=2)


class RouteGeometrySerializer(serializers.Serializer):
    type = serializers.CharField()
    coordinates = serializers.ListField(child=serializers.ListField())


class VehicleSerializer(serializers.Serializer):
    mpg = serializers.IntegerField()
    max_range_miles = serializers.IntegerField()
    tank_capacity_gallons = serializers.DecimalField(max_digits=6, decimal_places=2)
    starting_fuel_gallons = serializers.DecimalField(max_digits=6, decimal_places=2)


class FuelPlanSerializer(serializers.Serializer):
    total_fuel_purchased_gallons = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_fuel_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()


class PlanResponseSerializer(serializers.Serializer):
    route = serializers.SerializerMethodField()
    vehicle = VehicleSerializer()
    fuel_plan = FuelPlanSerializer()
    fuel_stops = FuelStopSerializer(many=True)

    @staticmethod
    def get_route(obj):
        return obj["route"]
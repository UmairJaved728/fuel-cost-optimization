from django.contrib.gis.db import models
from django.utils import timezone


class Station(models.Model):
    """A truck stop loaded from the OPIS fuel-price CSV for offline use.

    The OPIS truckstop ID is intentionally NOT globally unique, because the
    source data can contain several rows that share an OPIS ID but represent
    the same physical station (e.g. "PILOT TRAVEL CENTER #1243" vs
    "PILOT #1243").  Station identity is decided at import time using a
    combination of OPIS ID, normalized name, address, city and state.
    """

    opis_id = models.CharField(max_length=50, blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    state = models.CharField(max_length=2, blank=True, default="", db_index=True)
    rack_id = models.CharField(max_length=50, blank=True, default="")

    # Full CSV precision is preserved internally (e.g. 3.00733333).
    retail_price = models.DecimalField(max_digits=12, decimal_places=8, default=0, db_index=True)

    latitude = models.FloatField(default=0.0)
    longitude = models.FloatField(default=0.0)
    location = models.PointField(srid=4326, geography=True, spatial_index=True, null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Station"
        verbose_name_plural = "Stations"

    def __str__(self):
        return self.name or f"Station {self.id}"

    def update_location(self) -> None:
        """Keep the latitude/longitude columns and the PostGIS point in sync."""
        self.location = type(self).make_point(self.latitude, self.longitude)

    @staticmethod
    def make_point(latitude: float, longitude: float):
        from django.contrib.gis.geos import Point

        return Point(longitude, latitude, srid=4326)
from django.db import models


class FuelStation(models.Model):
    GEOCODE_EXACT = 'EXACT'
    GEOCODE_APPROXIMATE = 'APPROXIMATE'
    GEOCODE_UNRESOLVED = 'UNRESOLVED'

    GEOCODE_STATUS_CHOICES = [
        (GEOCODE_EXACT, 'Exact Address/Exit Match'),
        (GEOCODE_APPROXIMATE, 'Approximate City Match'),
        (GEOCODE_UNRESOLVED, 'Unresolved'),
    ]

    station_id = models.CharField(max_length=64, unique=True, db_index=True)
    opis_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128, db_index=True)
    state = models.CharField(max_length=8, db_index=True)
    rack_id = models.CharField(max_length=64, blank=True, null=True)
    retail_price = models.DecimalField(max_digits=8, decimal_places=4, db_index=True)
    latitude = models.FloatField(db_index=True)
    longitude = models.FloatField(db_index=True)
    geocode_status = models.CharField(
        max_length=16,
        choices=GEOCODE_STATUS_CHOICES,
        default=GEOCODE_APPROXIMATE,
        db_index=True
    )
    provenance = models.CharField(max_length=255, default='us_cities_census_enriched')
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['latitude', 'longitude']),
            models.Index(fields=['state', 'retail_price']),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) - ${self.retail_price}/gal"

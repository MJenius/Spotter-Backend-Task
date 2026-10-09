import csv
import logging
from decimal import Decimal
from pathlib import Path
from django.core.management.base import BaseCommand
from fuel_planner.models import FuelStation

logger = logging.getLogger(__name__)

US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY',
    'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND',
    'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
}

UNMATCHED_OVERRIDES = {
    ('BROOKPARK', 'OH'): (40.262259, -82.883453),
    ('HENRICO', 'VA'): (37.483558, -77.30654),
    ('UNIVERSITY PARK', 'IL'): (41.444624, -87.719024),
    ('ELIZABETHPORT', 'NJ'): (39.66502, -74.738208),
    ('EVERGREEN', 'AL'): (32.55236, -86.75776),
    ('PORT WENTWORTH', 'GA'): (32.200016, -81.209502),
}


class Command(BaseCommand):
    help = "Imports fuel stations from CSV, filters non-US records, enriches coordinates, and saves into database"

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv-path',
            type=str,
            default='data/fuel-prices-for-be-assessment.csv',
            help='Path to fuel prices CSV'
        )
        parser.add_argument(
            '--cities-path',
            type=str,
            default='data_cities.csv',
            help='Path to US cities coordinates CSV'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing stations before import'
        )

    def handle(self, *args, **options):
        csv_path = Path(options['csv_path'])
        cities_path = Path(options['cities_path'])
        if not csv_path.exists():
            # Check fallback in root
            csv_path = Path('fuel-prices-for-be-assessment.csv')
            if not csv_path.exists():
                self.stderr.write(self.style.ERROR(f"CSV file not found at {options['csv_path']}"))
                return

        if options['clear']:
            deleted_count, _ = FuelStation.objects.all().delete()
            self.stdout.write(f"Cleared {deleted_count} existing records.")

        # Load city coordinates
        cities_map = {}
        if cities_path.exists():
            with open(cities_path, mode='r', encoding='utf-8', errors='replace') as f:
                reader = csv.DictReader(f)
                for r in reader:
                    city_key = (r['CITY'].strip().upper(), r['STATE_CODE'].strip().upper())
                    try:
                        cities_map[city_key] = (float(r['LATITUDE']), float(r['LONGITUDE']))
                    except (ValueError, KeyError):
                        continue

        # Add manual overrides
        cities_map.update(UNMATCHED_OVERRIDES)

        total_read = 0
        filtered_non_us = 0
        invalid_price_count = 0
        accurately_matched = 0
        approx_matched = 0
        unresolved_count = 0
        seen_station_ids = set()

        stations_to_create = []

        with open(csv_path, mode='r', encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_read += 1
                state = row.get('State', '').strip().upper()
                city = row.get('City', '').strip()
                opis_id = row.get('OPIS Truckstop ID', '').strip()
                name = row.get('Truckstop Name', '').strip()
                address = row.get('Address', '').strip()
                rack_id = row.get('Rack ID', '').strip()
                price_raw = row.get('Retail Price', '').strip()

                if state not in US_STATES:
                    filtered_non_us += 1
                    continue

                try:
                    price = Decimal(price_raw)
                    if price <= Decimal('0.00'):
                        invalid_price_count += 1
                        continue
                except Exception:
                    invalid_price_count += 1
                    continue

                city_key = (city.upper(), state)
                coords = cities_map.get(city_key)

                if coords:
                    lat, lon = coords
                    geocode_status = FuelStation.GEOCODE_APPROXIMATE
                    approx_matched += 1
                else:
                    lat, lon = 0.0, 0.0
                    geocode_status = FuelStation.GEOCODE_UNRESOLVED
                    unresolved_count += 1

                # Generate a unique stable internal station_id
                # Note: multiple records might have same OPIS ID if distinct rack/prices exist
                station_id = f"ST-{opis_id}-{rack_id}" if rack_id else f"ST-{opis_id}-{total_read}"
                if station_id in seen_station_ids:
                    station_id = f"{station_id}-{total_read}"
                seen_station_ids.add(station_id)

                station = FuelStation(
                    station_id=station_id,
                    opis_id=opis_id,
                    name=name,
                    address=address,
                    city=city,
                    state=state,
                    rack_id=rack_id,
                    retail_price=price,
                    latitude=lat,
                    longitude=lon,
                    geocode_status=geocode_status,
                    provenance='us_cities_census_enriched',
                    is_active=(geocode_status != FuelStation.GEOCODE_UNRESOLVED)
                )
                stations_to_create.append(station)

        # Bulk create or update in batches
        FuelStation.objects.bulk_create(stations_to_create, batch_size=1000, ignore_conflicts=True)

        self.stdout.write(self.style.SUCCESS("=== Ingestion and Geocoding Summary ==="))
        self.stdout.write(f"Total records read: {total_read}")
        self.stdout.write(f"Filtered (Non-US): {filtered_non_us}")
        self.stdout.write(f"Invalid prices filtered: {invalid_price_count}")
        self.stdout.write(f"Accurately matched: {accurately_matched}")
        self.stdout.write(f"Approximately matched (city-level): {approx_matched}")
        self.stdout.write(f"Unresolved: {unresolved_count}")
        self.stdout.write(f"Total stations saved: {len(stations_to_create)}")

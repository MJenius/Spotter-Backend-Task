import csv
import logging
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.core.management.base import BaseCommand, CommandError
from fuel_planner.models import FuelStation

logger = logging.getLogger(__name__)

US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY',
    'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND',
    'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
}


class Command(BaseCommand):
    help = "Imports fuel stations from CSV, validates US territory, enriches coordinates, and updates prices safely on reruns."

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv-path',
            type=str,
            default=None,
            help='Path to fuel prices CSV (defaults to BASE_DIR/data/fuel-prices-for-be-assessment.csv)'
        )
        parser.add_argument(
            '--cities-path',
            type=str,
            default=None,
            help='Path to US cities coordinates CSV (defaults to BASE_DIR/data_cities.csv)'
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing stations before import'
        )

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)

        raw_csv = options.get('csv_path')
        if raw_csv:
            csv_path = Path(raw_csv) if Path(raw_csv).is_absolute() else base_dir / raw_csv
        else:
            csv_path = base_dir / 'data' / 'fuel-prices-for-be-assessment.csv'
            if not csv_path.exists():
                csv_path = base_dir / 'fuel-prices-for-be-assessment.csv'

        raw_cities = options.get('cities_path')
        if raw_cities:
            cities_path = Path(raw_cities) if Path(raw_cities).is_absolute() else base_dir / raw_cities
        else:
            cities_path = base_dir / 'data_cities.csv'

        if not csv_path.exists():
            raise CommandError(f"Fuel prices CSV file not found at: {csv_path}")

        # Pre-validate CSV headers and schema upfront before touching database
        required_fuel_headers = {'State', 'City', 'OPIS Truckstop ID', 'Truckstop Name', 'Address', 'Retail Price'}
        with open(csv_path, mode='r', encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f)
            fieldnames = set(reader.fieldnames or [])
            missing_headers = required_fuel_headers - fieldnames
            if missing_headers:
                raise CommandError(f"Fuel prices CSV is missing mandatory columns: {sorted(list(missing_headers))}")

        # Load and validate city coordinates
        cities_map = {}
        if cities_path.exists():
            with open(cities_path, mode='r', encoding='utf-8', errors='replace') as f:
                reader = csv.DictReader(f)
                c_fieldnames = set(reader.fieldnames or [])
                if not {'CITY', 'STATE_CODE', 'LATITUDE', 'LONGITUDE'}.issubset(c_fieldnames):
                    raise CommandError("Cities CSV is missing required columns: CITY, STATE_CODE, LATITUDE, LONGITUDE")
                for r in reader:
                    city_key = (r['CITY'].strip().upper(), r['STATE_CODE'].strip().upper())
                    try:
                        cities_map[city_key] = (float(r['LATITUDE']), float(r['LONGITUDE']))
                    except (ValueError, KeyError):
                        continue

        with transaction.atomic():
            if options['clear']:
                deleted_count, _ = FuelStation.objects.all().delete()
                self.stdout.write(f"Cleared {deleted_count} existing records.")

            total_read = 0
            filtered_non_us = 0
            invalid_price_count = 0
            exact_matched = 0
            approx_matched = 0
            unresolved_count = 0
            seen_station_ids = set()

            created_count = 0
            updated_count = 0

            # Existing stations map for safe repeatable updates
            existing_stations = {s.station_id: s for s in FuelStation.objects.all()}

            stations_to_create = []
            stations_to_update = []

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

                    # Honest coordinate provenance:
                    # City coordinates from Census/SimpleMaps are marked as APPROXIMATE city centroids.
                    if coords:
                        lat, lon = coords
                        geocode_status = FuelStation.GEOCODE_APPROXIMATE
                        provenance = 'us_cities_database_centroid'
                        approx_matched += 1
                    else:
                        lat, lon = 0.0, 0.0
                        geocode_status = FuelStation.GEOCODE_UNRESOLVED
                        provenance = 'unresolved'
                        unresolved_count += 1

                    # Generate a stable station_id
                    station_id = f"ST-{opis_id}-{rack_id}" if rack_id else f"ST-{opis_id}"
                    if station_id in seen_station_ids:
                        station_id = f"{station_id}-{total_read}"
                    seen_station_ids.add(station_id)

                    if station_id in existing_stations:
                        # Update price and attributes safely
                        st = existing_stations[station_id]
                        st.retail_price = price
                        st.name = name
                        st.address = address
                        st.city = city
                        st.state = state
                        st.rack_id = rack_id
                        st.latitude = lat
                        st.longitude = lon
                        st.geocode_status = geocode_status
                        st.provenance = provenance
                        st.is_active = (geocode_status != FuelStation.GEOCODE_UNRESOLVED)
                        st.updated_at = timezone.now()
                        stations_to_update.append(st)
                        updated_count += 1
                    else:
                        new_st = FuelStation(
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
                            provenance=provenance,
                            is_active=(geocode_status != FuelStation.GEOCODE_UNRESOLVED)
                        )
                        stations_to_create.append(new_st)
                        created_count += 1

            if stations_to_create:
                FuelStation.objects.bulk_create(stations_to_create, batch_size=1000)
            if stations_to_update:
                FuelStation.objects.bulk_update(
                    stations_to_update,
                    fields=['retail_price', 'name', 'address', 'city', 'state', 'rack_id', 'latitude', 'longitude', 'geocode_status', 'provenance', 'is_active', 'updated_at'],
                    batch_size=1000
                )

        self.stdout.write(self.style.SUCCESS("=== Ingestion and Geocoding Summary ==="))
        self.stdout.write(f"Total records read: {total_read}")
        self.stdout.write(f"Filtered (Non-US): {filtered_non_us}")
        self.stdout.write(f"Invalid prices filtered: {invalid_price_count}")
        self.stdout.write(f"Accurately matched: {exact_matched}")
        self.stdout.write(f"Approximately matched (city centroid): {approx_matched}")
        self.stdout.write(f"Unresolved: {unresolved_count}")
        self.stdout.write(f"Created: {created_count}, Updated: {updated_count}")

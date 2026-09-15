"""Retry geocoding for the stations recorded in the failure CSV.

python manage.py retry_station_geocoding --csv path/to/fuel_prices.csv
                         [--failures-csv data/geocoding_failures.csv]

Why this command exists
-----------------------
``import_stations`` records stations it could not geocode in
``data/geocoding_failures.csv`` instead of dropping them.  Failures are often
transient (the public Nominatim instance rate-limits / returns empty results
during long imports), so most of them succeed on a later, slower retry.

Retry strategy (stronger than the initial import):

1. Try the Photon geocoder (https://photon.komoot.io/) first with progressively
   looser query variants (combined name+address, address, name, then
   city/state) and a short per-request timeout.
2. If every variant fails, retry with the regular Nominatim client using
   the same variants and a polite delay.
3. If both free-text services fail, fall back to the Open-Meteo Geocoding API
   (https://geocoding-api.open-meteo.com/) with the town/state pair.
4. A coordinate is accepted when the provider reports a US country code and
   the point lies inside a generous continental hull (the detail polygon is too
   coarse for genuine US coastal/border towns).  When the provider is silent
   about the country, the stricter detail polygon still applies.

Photon and Open-Meteo are tried before Nominatim because they are independent
no-key services that do not accumulate the rate-limit debt of a shared
Nominatim instance during a long import.

The original fuel-price CSV is re-read so that recovered rows get the correct
price/rack ID (identity-preserving dedup rule).  Stations already in the
database are skipped.  Stations that still cannot be geocoded are written back
to the failure CSV so no information is lost.
"""

from __future__ import annotations

import csv
import time
from collections import Counter
from pathlib import Path

import requests
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError

from routes.management.commands.import_stations import Command as ImportCommand
from routes.models import Station
from routes.services.geocoding_client import GeocodingClient
from routes.services.usa_boundary import is_within_usa

DEFAULT_NOMINATIM_DELAY = 1.2
DEFAULT_PHOTON_DELAY = 0.5
DEFAULT_OPENMETEO_DELAY = 0.4

PHOTON_ENDPOINT = "https://photon.komoot.io/api/"
PHOTON_TIMEOUT_SECONDS = 8

OPENMETEO_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
OPENMETEO_TIMEOUT_SECONDS = 8

_FAILURE_HEADERS = ["opis_id", "name", "address", "city", "state", "reason"]

# jiterco-compatible country codes that Photon/Nominatim may report for the USA.
_US_COUNTRY_CODES = {"us", "usa", "united states"}

# Generous continental hull used to sanity-check coordinates that carry an
# authoritative US country code (from Nominatim/Photon/Open-Meteo).  It spans
# the whole country (incl. Alaska, Hawaii and the Keys) but still catches
# absurd geocodes.  The detail polygon is too coarse for genuine US coastal and
# border towns, so it is only trusted when the provider is silent about the
# country.
_US_HULL = (17.0, -179.0, 72.5, -50.0)  # min_lat, min_lon, max_lat, max_lon


class Command(BaseCommand):
    help = "Retry geocoding for stations recorded in the failure CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            required=True,
            help="Original fuel-price CSV (re-read for prices / rack IDs).",
        )
        parser.add_argument(
            "--failures-csv",
            default=None,
            help="Failures CSV to read and rewrite (default data/geocoding_failures.csv).",
        )
        parser.add_argument(
            "--nominatim-delay",
            type=float,
            default=DEFAULT_NOMINATIM_DELAY,
            help=f"Seconds between Nominatim requests (default {DEFAULT_NOMINATIM_DELAY}).",
        )
        parser.add_argument(
            "--photon-delay",
            type=float,
            default=DEFAULT_PHOTON_DELAY,
            help=f"Seconds between Photon requests (default {DEFAULT_PHOTON_DELAY}).",
        )
        parser.add_argument(
            "--openmeteo-delay",
            type=float,
            default=DEFAULT_OPENMETEO_DELAY,
            help=f"Seconds between Open-Meteo requests (default {DEFAULT_OPENMETEO_DELAY}).",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"])
        failures_path = Path(
            options["failures_csv"]
            or (Path(__file__).resolve().parents[3] / "data" / "geocoding_failures.csv")
        )
        nominatim_delay = max(0.0, options["nominatim_delay"])
        photon_delay = max(0.0, options["photon_delay"])
        openmeteo_delay = max(0.0, options["openmeteo_delay"])

        if not csv_path.is_file():
            raise CommandError(f"CSV file not found: {csv_path}")
        if not failures_path.is_file():
            raise CommandError(f"Failures CSV not found: {failures_path}")

        base = ImportCommand()
        records = base._read_records(csv_path)
        base._validate_fields(records)
        stats = Counter(
            raw_records=len(records),
            us_records=0,
            duplicates_removed=0,
            successfully_geocoded=0,
            failed_geocoding=0,
            imported_stations=0,
            malformed_prices=0,
            non_us_records=0,
        )
        unique = base._deduplicate(records, stats)
        by_identity = {row["_identity"]: row for row in unique}

        failed_identities = self._read_failed_identities(failures_path, base)
        existing_identities = base._load_existing_identities()

        self.stdout.write(
            f"Retrying {len(failed_identities)} failed stations from {failures_path} ..."
        )

        nominatim = GeocodingClient()
        imported = 0
        skipped = 0
        missing_rows = 0
        still_failed = []

        for identity in sorted(failed_identities):
            if identity in existing_identities:
                skipped += 1
                continue

            row = by_identity.get(identity)
            if row is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"  identity not found in CSV after dedup: {identity}"
                    )
                )
                missing_rows += 1
                self._record_still_failed(still_failed, row or self._row_from_identity(identity))
                continue

            coordinates = self._geocode_station(
                row, nominatim, nominatim_delay, photon_delay, openmeteo_delay
            )
            if coordinates is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"  still ungeocodable: {row['Truckstop Name']}, {row['City']}, {row['State']}"
                    )
                )
                self._record_still_failed(still_failed, row)
                continue

            latitude, longitude = coordinates
            Station.objects.create(
                opis_id=row["OPIS Truckstop ID"],
                name=row["Truckstop Name"],
                address=row["Address"],
                city=row["City"],
                state=row["State"],
                rack_id=row["Rack ID"],
                retail_price=row["_price"],
                latitude=latitude,
                longitude=longitude,
                location=Point(longitude, latitude, srid=4326),
            )
            imported += 1
            existing_identities.add(identity)

        self._write_failures(failures_path, still_failed)

        self.stdout.write(self.style.SUCCESS("\nRetry report"))
        self.stdout.write("-" * 40)
        self.stdout.write(f"failed identities examined:   {len(failed_identities)}")
        self.stdout.write(f"skipped (already in DB):      {skipped}")
        self.stdout.write(f"missing from deduped CSV:     {missing_rows}")
        self.stdout.write(f"successfully geocoded now:    {imported}")
        self.stdout.write(f"still ungeocodable:           {len(still_failed)}")
        self.stdout.write("-" * 40)
        self.stdout.write(f"rewritten failures file:      {failures_path}")

    # ------------------------------------------------------------------ #
    # Geocoding
    # ------------------------------------------------------------------ #
    def _geocode_station(self, row: dict, nominatim: GeocodingClient,
                         nominatim_delay: float, photon_delay: float,
                         openmeteo_delay: float):
        """Resolve one station (Photon, then Nominatim, then Open-Meteo)."""
        variants = self._query_variants(row)

        for query in variants:
            try:
                longitude, latitude, country_code = self._photon_geocode(query)
            except Exception:
                time.sleep(photon_delay)
                continue
            time.sleep(photon_delay)
            if self._acceptable(country_code, latitude, longitude):
                return latitude, longitude

        for query in variants:
            try:
                result = nominatim.geocode(query)
            except Exception:
                time.sleep(nominatim_delay)
                continue
            time.sleep(nominatim_delay)
            if self._acceptable(result.country_code, result.latitude, result.longitude):
                return result.latitude, result.longitude

        for query in variants[-1:]:
            try:
                longitude, latitude, country_code = self._openmeteo_geocode(query)
            except Exception:
                time.sleep(openmeteo_delay)
                continue
            if self._acceptable(country_code, latitude, longitude):
                return latitude, longitude

        return None

    @staticmethod
    def _query_variants(row: dict) -> list[str]:
        name = row["Truckstop Name"]
        address = row["Address"]
        city = row["City"]
        state = row["State"]

        variants = []
        if address:
            variants.append(f"{address}, {city}, {state}, USA")
            if name:
                variants.append(f"{name} {address}, {city}, {state}, USA")
        if name:
            variants.append(f"{name}, {city}, {state}, USA")
        variants.append(f"{city}, {state}, USA")

        # The price CSV occasionally drops apostrophes from town names
        # (e.g. Odonnell for O'Donnell), or keeps them when the map does not.
        # Retry with both spellings when they differ.
        alternate_cities = []
        city_stripped = city.replace("'", "")
        if city_stripped != city:
            alternate_cities.append(city_stripped)
        if "'" not in city and len(city) > 2:
            alternate_cities.append(city[0] + "'" + city[1:])
        for alt in alternate_cities:
            if address:
                variants.append(f"{address}, {alt}, {state}, USA")
            if name:
                variants.append(f"{name}, {alt}, {state}, USA")
            variants.append(f"{alt}, {state}, USA")
        return variants

    @staticmethod
    def _acceptable(country_code: str, latitude: float, longitude: float) -> bool:
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            return False
        code = (country_code or "").lower()
        if code and code not in _US_COUNTRY_CODES:
            return False
        if code in _US_COUNTRY_CODES:
            min_lat, min_lon, max_lat, max_lon = _US_HULL
            return min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon
        return is_within_usa(latitude, longitude)

    @staticmethod
    def _photon_geocode(query: str):
        """Photon single-hit lookup.  Returns (lon, lat, country_code) or raises."""
        resp = requests.get(
            PHOTON_ENDPOINT,
            params={"q": query, "limit": 1, "lang": "en"},
            timeout=PHOTON_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise ValueError(f"Photon returned HTTP {resp.status_code}")
        data = resp.json()
        features = data.get("features") or []
        if not features:
            raise ValueError("Photon returned no match")
        feature = features[0]
        longitude, latitude = (float(v) for v in feature["geometry"]["coordinates"][:2])
        properties = feature.get("properties") or {}
        country_code = (properties.get("countrycode") or "").lower()
        return longitude, latitude, country_code

    @staticmethod
    def _openmeteo_geocode(query: str):
        """Open-Meteo town lookup.  Returns (lon, lat, country_code) or raises.

        ``query`` should be the trailing city/state variant (the API searches
        place names, not free-text addresses).
        """
        city, _, state = query.partition(",")
        resp = requests.get(
            OPENMETEO_ENDPOINT,
            params={
                "name": city.strip(),
                "count": 1,
                "language": "en",
                "format": "json",
                "countryCode": "US",
            },
            timeout=OPENMETEO_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise ValueError(f"Open-Meteo returned HTTP {resp.status_code}")
        data = resp.json()
        results = data.get("results") or []
        if not results:
            raise ValueError("Open-Meteo returned no match")
        result = results[0]
        return (
            float(result["longitude"]),
            float(result["latitude"]),
            (result.get("country_code") or "").lower(),
        )

    # ------------------------------------------------------------------ #
    # Failures bookkeeping
    # ------------------------------------------------------------------ #
    def _read_failed_identities(self, path: Path, base: ImportCommand) -> set[tuple]:
        identities = set()
        with path.open("r", newline="", encoding="utf-8-sig") as fh:
            for record in csv.DictReader(fh):
                identities.add(
                    (
                        base._normalize(record.get("opis_id", "")),
                        base._normalize(record.get("address", "")),
                        base._normalize(record.get("city", "")),
                        base._normalize(record.get("state", "")).upper(),
                    )
                )
        return identities

    def _record_still_failed(self, output: list[dict], row: dict) -> None:
        output.append(
            {
                "opis_id": row.get("OPIS Truckstop ID", ""),
                "name": row.get("Truckstop Name", ""),
                "address": row.get("Address", ""),
                "city": row.get("City", ""),
                "state": row.get("State", ""),
                "reason": "still ungeocodable (Photon, Nominatim, Open-Meteo)",
            }
        )

    @staticmethod
    def _row_from_identity(identity: tuple) -> dict:
        opis_id, address, city, state = identity
        return {
            "OPIS Truckstop ID": opis_id,
            "Truckstop Name": "",
            "Address": address,
            "City": city,
            "State": state,
            "Rack ID": "",
            "_price": None,
        }

    @staticmethod
    def _write_failures(path: Path, rows: list[dict]) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FAILURE_HEADERS)
            writer.writeheader()
            writer.writerows(rows)
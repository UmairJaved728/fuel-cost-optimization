"""Offline station preprocessing / import management command.

python manage.py import_stations --csv path/to/fuel_prices.csv

This is a ONE-TIME offline step that must be run before the API can produce
useful fuel plans.  It reads the OPIS fuel-price CSV, deduplicates records,
geocodes the unique stations (the only place station geocoding happens), and
bulk-loads them into PostgreSQL/PostGIS.

The command is safe to re-run: stations that were already imported (matched by
the documented station identity) are skipped, and failed geocoding attempts are
recorded in ``data/geocoding_failures.csv`` rather than silently dropped.
"""

from __future__ import annotations

import csv
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from routes.models import Station
from routes.services.geocoding_client import GeocodingClient
from routes.services.usa_boundary import is_within_usa

VALID_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

REQUIRED_HEADERS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]

BULK_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Preprocess and import the fuel-price CSV into PostgreSQL/PostGIS."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="Path to the fuel-prices CSV.")
        parser.add_argument(
            "--geocode-delay",
            type=float,
            default=1.1,
            help="Seconds to wait between station geocoding requests (default 1.1).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Only process this many NEW stations per run (useful for resumable runs).",
        )
        parser.add_argument(
            "--failures-csv",
            default=None,
            help="Where to write geocoding failures (default: data/geocoding_failures.csv).",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"])
        failures_path = Path(
            options["failures_csv"] or (Path(__file__).resolve().parents[3] / "data" / "geocoding_failures.csv")
        )
        delay = max(0.0, options["geocode_delay"])
        limit = options.get("limit")

        if not csv_path.is_file():
            raise CommandError(f"CSV file not found: {csv_path}")

        records = self._read_records(csv_path)
        self._validate_fields(records)

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

        unique_rows = self._deduplicate(records, stats)

        failures_path.parent.mkdir(parents=True, exist_ok=True)
        existing_identities = self._load_existing_identities()

        geocoder = GeocodingClient()
        new_stations = []
        # ``known`` starts from everything already in the database and grows as
        # batches are committed, so a crash mid-run loses at most the current
        # batch and stations that were geocoded so far stay visible in the DB.
        known_identities = set(existing_identities)

        for row in unique_rows:
            identity = row["_identity"]
            if identity in known_identities:
                continue
            if limit is not None and len(new_stations) >= limit:
                break

            station = self._geocode_and_build(row, geocoder, delay)
            if station is None:
                self._record_failure(failures_path, row)
                stats["failed_geocoding"] += 1
                continue

            stats["successfully_geocoded"] += 1
            new_stations.append(station)
            known_identities.add(identity)

            if len(new_stations) >= BULK_BATCH_SIZE:
                self._bulk_insert(new_stations)
                stats["imported_stations"] += len(new_stations)
                new_stations = []

        self._bulk_insert(new_stations)
        stats["imported_stations"] += len(new_stations)

        self._print_stats(stats, len(unique_rows))

    # ------------------------------------------------------------------ #
    # CSV parsing
    # ------------------------------------------------------------------ #
    def _read_records(self, csv_path: Path) -> list[dict]:
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None:
                    raise CommandError("CSV is empty or has no header row.")
                header = [h.strip() for h in reader.fieldnames]
                missing = [h for h in REQUIRED_HEADERS if h not in header]
                if missing:
                    raise CommandError(
                        f"CSV is missing required headers: {', '.join(missing)}"
                    )
                return [dict(row) for row in reader]
        except (OSError, UnicodeDecodeError) as exc:
            raise CommandError(f"Could not read CSV {csv_path}: {exc}") from exc

    def _validate_fields(self, records: list[dict]) -> None:
        """Normalize text fields; malformed prices and non-US states are skipped later."""
        for row in records:
            row["Truckstop Name"] = self._normalize(row.get("Truckstop Name"))
            row["Address"] = self._normalize(row.get("Address"))
            row["City"] = self._normalize(row.get("City"))
            row["State"] = self._normalize(row.get("State")).upper()
            row["OPIS Truckstop ID"] = self._normalize(row.get("OPIS Truckstop ID"))
            row["Rack ID"] = self._normalize(row.get("Rack ID"))

    @staticmethod
    def _normalize(raw) -> str:
        if raw is None:
            return ""
        return " ".join(str(raw).strip().split())

    # ------------------------------------------------------------------ #
    # Deduplication
    # ------------------------------------------------------------------ #
    def _deduplicate(self, records: list[dict], stats: Counter) -> list[dict]:
        """Apply the documented deduplication rule.

        Rule (see README):

        * a row is kept only when it is a US row with a parseable price;
        * the physical station identity is the tuple
          (OPIS ID, normalized address, city, state);
        * two rows sharing an identity represent the same physical station;
          we keep the row with the lowest retail price (ties -> first row),
          because two rows at the same physical location almost always differ
          only by station-name spelling (e.g. "PILOT TRAVEL CENTER #1243" vs
          "PILOT #1243").
        """
        best: dict[tuple, dict] = {}

        for row in records:
            state = row["State"]
            if state not in VALID_US_STATES:
                stats["non_us_records"] += 1
                continue
            stats["us_records"] += 1

            try:
                price = Decimal(row.get("Retail Price", "").strip())
            except (InvalidOperation, ValueError):
                stats["malformed_prices"] += 1
                continue

            identity = (
                row["OPIS Truckstop ID"],
                row["Address"],
                row["City"],
                row["State"],
            )
            row["_identity"] = identity
            row["_price"] = price

            existing = best.get(identity)
            if existing is None:
                best[identity] = row
                continue
            stats["duplicates_removed"] += 1
            if price < existing["_price"]:
                best[identity] = row

        return list(best.values())

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    def _load_existing_identities(self) -> set[tuple]:
        identities = set()
        for opis_id, address, city, state in (
            Station.objects.filter(location__isnull=True)
            .values_list("opis_id", "address", "city", "state")
        ):
            identities.add(
                (
                    self._normalize(opis_id),
                    self._normalize(address),
                    self._normalize(city),
                    self._normalize(state).upper(),
                )
            )
        for opis_id, address, city, state in (
            Station.objects.exclude(location__isnull=True)
            .values_list("opis_id", "address", "city", "state")
        ):
            identities.add(
                (
                    self._normalize(opis_id),
                    self._normalize(address),
                    self._normalize(city),
                    self._normalize(state).upper(),
                )
            )
        return identities

    def _geocode_and_build(self, row: dict, geocoder: GeocodingClient, delay: float):
        """Geocode one station identity and build an unsaved Station object."""
        coordinates = self._geocode_station(row, geocoder, delay)
        if coordinates is None:
            return None

        latitude, longitude = coordinates
        station = Station(
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
        return station

    def _geocode_station(self, row: dict, geocoder: GeocodingClient, delay: float):
        """Try several progressively looser queries; return (lat, lon) or None."""
        queries = []
        if row["Address"]:
            queries.append(f"{row['Address']}, {row['City']}, {row['State']}, USA")
        if row["Truckstop Name"]:
            queries.append(f"{row['Truckstop Name']}, {row['City']}, {row['State']}, USA")
        queries.append(f"{row['City']}, {row['State']}, USA")

        for query in queries:
            try:
                result = geocoder.geocode(query)
            except Exception as exc:  # noqa: BLE001 - offline tool, keep going
                self.stdout.write(
                    self.style.WARNING(f"  geocode error for {query!r}: {exc}")
                )
                time.sleep(delay)
                continue

            time.sleep(delay)
            if not GeocodingClient.is_us_result(result):
                continue
            if not is_within_usa(result.latitude, result.longitude):
                continue
            if not (
                -90.0 <= result.latitude <= 90.0
                and -180.0 <= result.longitude <= 180.0
            ):
                continue
            return result.latitude, result.longitude

        return None

    def _bulk_insert(self, stations: list[Station]) -> None:
        if not stations:
            return
        with transaction.atomic():
            for start in range(0, len(stations), BULK_BATCH_SIZE):
                batch = stations[start : start + BULK_BATCH_SIZE]
                # ignore_conflicts keeps re-runs idempotent if a batch straddles
                # two runs (the in-memory identity set already prevents most).
                Station.objects.bulk_create(
                    batch,
                    batch_size=BULK_BATCH_SIZE,
                    ignore_conflicts=True,
                )

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def _record_failure(self, failures_path: Path, row: dict) -> None:
        write_header = not failures_path.exists() or failures_path.stat().st_size == 0
        with failures_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "opis_id",
                    "name",
                    "address",
                    "city",
                    "state",
                    "reason",
                ],
            )
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "opis_id": row["OPIS Truckstop ID"],
                    "name": row["Truckstop Name"],
                    "address": row["Address"],
                    "city": row["City"],
                    "state": row["State"],
                    "reason": "geocoding failed for all query variants",
                }
            )

    def _print_stats(self, stats: Counter, unique_rows: int) -> None:
        self.stdout.write(self.style.SUCCESS("\nImport report"))
        self.stdout.write("-" * 40)
        self.stdout.write(f"raw records:           {stats['raw_records']}")
        self.stdout.write(f"US records:            {stats['us_records']}")
        self.stdout.write(f"duplicates removed:    {stats['duplicates_removed']}")
        self.stdout.write(f"successfully geocoded: {stats['successfully_geocoded']}")
        self.stdout.write(f"failed geocoding:      {stats['failed_geocoding']}")
        self.stdout.write(f"imported stations:     {stats['imported_stations']}")
        self.stdout.write("-" * 40)
        if stats["non_us_records"]:
            self.stdout.write(
                self.style.WARNING(f"non-US records skipped: {stats['non_us_records']}")
            )
        if stats["malformed_prices"]:
            self.stdout.write(
                self.style.WARNING(f"malformed price rows skipped: {stats['malformed_prices']}")
            )
        self.stdout.write(
            f"unique physical stations after dedup: {unique_rows}"
        )
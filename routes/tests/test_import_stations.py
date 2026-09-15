"""Importer tests.  Geocoding is mocked; nothing leaves the box."""

import io
from collections import Counter
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.management import call_command

from routes.management.commands.import_stations import Command
from routes.models import Station

pytestmark = pytest.mark.django_db

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price"


def _write_csv(path, rows):
    lines = [HEADER]
    lines += rows
    path.write_text("\n".join(lines), encoding="utf-8")


def _us_result(lat, lon, name="Test City, TX, USA"):
    return SimpleNamespace(
        latitude=lat,
        longitude=lon,
        display_name=name,
        country_code="us",
    )


def _patch_geocoder(mapping=None, side_effect=None):
    if side_effect is None:
        def _geocode(query, _mapping=mapping or {}):
            for key, result in _mapping.items():
                if key in query:
                    return result
            raise Exception(f"No map for {query!r}")

        side_effect = _geocode
    return mock.patch(
        "routes.management.commands.import_stations.GeocodingClient.geocode",
        side_effect=side_effect,
    )


class TestDeduplicate:
    def test_keeps_lowest_price_for_same_identity(self):
        command = Command()
        records = [
            {"OPIS Truckstop ID": "100", "Truckstop Name": "PILOT TRAVEL CENTER #1",
             "Address": "I-35 EXIT 271", "City": "Austin", "State": "TX",
             "Rack ID": "", "Retail Price": "3.09"},
            {"OPIS Truckstop ID": "100", "Truckstop Name": "PILOT #1",
             "Address": "I-35 EXIT 271", "City": "Austin", "State": "TX",
             "Rack ID": "", "Retail Price": "2.98"},
            {"OPIS Truckstop ID": "101", "Truckstop Name": "OTHER",
             "Address": "Main St", "City": "Dallas", "State": "TX",
             "Rack ID": "", "Retail Price": "3.20"},
        ]
        for r in records:
            command._validate_fields([r])

        stats = Counter(raw_records=len(records), us_records=0,
                        duplicates_removed=0, non_us_records=0,
                        malformed_prices=0)
        unique = command._deduplicate(records, stats)
        assert len(unique) == 2
        assert stats["duplicates_removed"] == 1
        by_id = {r["OPIS Truckstop ID"]: r for r in unique}
        assert by_id["100"]["_price"] == Decimal("2.98")  # cheapest kept

    def test_non_us_state_skipped(self):
        command = Command()
        records = [{"OPIS Truckstop ID": "1", "Truckstop Name": "X",
                    "Address": "A", "City": "Toronto", "State": "ON",
                    "Rack ID": "", "Retail Price": "3.00"}]
        command._validate_fields([records[0]])

        stats = Counter(raw_records=1, us_records=0,
                        duplicates_removed=0, non_us_records=0,
                        malformed_prices=0)
        unique = command._deduplicate(records, stats)
        assert unique == []
        assert stats["non_us_records"] == 1

    def test_malformed_price_skipped(self):
        command = Command()
        records = [{"OPIS Truckstop ID": "1", "Truckstop Name": "X",
                    "Address": "A", "City": "B", "State": "TX",
                    "Rack ID": "", "Retail Price": "not-a-price"}]
        command._validate_fields([records[0]])

        stats = Counter(raw_records=1, us_records=0,
                        duplicates_removed=0, non_us_records=0,
                        malformed_prices=0)
        unique = command._deduplicate(records, stats)
        assert unique == []
        assert stats["malformed_prices"] == 1


class TestImportCommand:
    def test_full_import_reports_and_inserts(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, [
            "1,PILOT #1,I-35 EXIT 271,Austin,TX,,3.09",
            "2,LOVES #1,Main St,Dallas,TX,,3.20",
        ])
        mapping = {
            "Austin": _us_result(30.27, -97.74, "Austin, TX, USA"),
            "Dallas": _us_result(32.78, -96.80, "Dallas, TX, USA"),
        }
        with _patch_geocoder(mapping):
            out = io.StringIO()
            call_command(
                "import_stations", csv=str(csv_path),
                geocode_delay=0, failures_csv=str(failures),
                stdout=out,
            )
        assert Station.objects.count() == 2
        report = out.getvalue()
        assert "raw records:" in report and "imported stations:" in report

    def test_duplicate_rows_produce_single_station(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, [
            "100,PILOT TRAVEL CENTER #1,I-35 EXIT 271,Austin,TX,,3.09",
            "100,PILOT #1,I-35 EXIT 271,Austin,TX,,2.98",
            "100,PILOT #1   ,I-35 EXIT 271  ,Austin,TX,,2.98",
        ])
        mapping = {"Austin": _us_result(30.27, -97.74)}
        with _patch_geocoder(mapping):
            call_command(
                "import_stations", csv=str(csv_path),
                geocode_delay=0, failures_csv=str(failures),
            )
        assert Station.objects.count() == 1
        station = Station.objects.get()
        assert float(station.retail_price) == 2.98

    def test_reimport_is_idempotent(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        mapping = {"Austin": _us_result(30.27, -97.74)}
        with _patch_geocoder(mapping):
            call_command("import_stations", csv=str(csv_path), geocode_delay=0, failures_csv=str(failures))
            call_command("import_stations", csv=str(csv_path), geocode_delay=0, failures_csv=str(failures))
        assert Station.objects.count() == 1

    def test_geocoding_failure_is_recorded(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])

        def _boom(query):
            raise Exception("geocoder down")

        with _patch_geocoder(side_effect=_boom):
            call_command("import_stations", csv=str(csv_path), geocode_delay=0, failures_csv=str(failures))

        assert Station.objects.count() == 0
        assert failures.exists()
        text = failures.read_text(encoding="utf-8")
        assert "PILOT" in text
        assert "Austin" in text

    def test_geocoder_result_outside_usa_rejected(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        mapping = {"Austin": SimpleNamespace(latitude=49.2, longitude=-123.1,
                                             display_name="Vancouver, BC, Canada",
                                             country_code="ca")}
        with _patch_geocoder(mapping):
            call_command("import_stations", csv=str(csv_path), geocode_delay=0, failures_csv=str(failures))
        assert Station.objects.count() == 0
        assert failures.exists()

    def test_missing_header_raises_command_error(self, tmp_path):
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text("OPIS Truckstop ID,Name\n1,PILOT", encoding="utf-8")
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command("import_stations", csv=str(csv_path), geocode_delay=0)
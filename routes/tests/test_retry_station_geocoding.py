"""Retry-command tests.  Both geocoders are mocked; nothing leaves the box."""

import io
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.management.base import CommandError
from django.core.management import call_command

from routes.models import Station

pytestmark = pytest.mark.django_db

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price"
FAILURE_HEADER = "opis_id,name,address,city,state,reason"


def _write_csv(path, rows):
    path.write_text("\n".join([HEADER] + rows), encoding="utf-8")


def _write_failures(path, rows):
    path.write_text("\n".join([FAILURE_HEADER] + rows), encoding="utf-8")


def _us_result(lat, lon, name="Austin, TX, USA"):
    return SimpleNamespace(
        latitude=lat,
        longitude=lon,
        display_name=name,
        country_code="us",
    )


class _Combined:
    """Stack several mock.patch context managers into a single ``with``."""

    def __init__(self, *patchers):
        self._patchers = patchers
        self._stack = ExitStack()

    def __enter__(self):
        self._stack = ExitStack()
        for patcher in self._patchers:
            self._stack.enter_context(patcher)
        return self._stack

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _photon_response(features):
    class FakeResp:
        status_code = 200

        def __init__(self, features):
            self._features = features

        def json(self):
            return {"features": self._features or []}

    return FakeResp(features)


def _openmeteo_response(results):
    class FakeResp:
        status_code = 200

        def __init__(self, results):
            self._results = results

        def json(self):
            return {"results": self._results or []}

    return FakeResp(results)


def _patch_requests(photon=lambda q: None, openmeteo=lambda q: None):
    """Patch requests.get, dispatching Photon vs Open-Meteo by URL."""

    def _get(url, params=None, timeout=None):
        query = params.get("q", "") if url != "https://geocoding-api.open-meteo.com/v1/search" else params.get("name", "")
        if "photon" in url:
            return _photon_response(photon(query))
        return _openmeteo_response(openmeteo(query))

    return mock.patch(
        "routes.management.commands.retry_station_geocoding.requests.get",
        side_effect=_get,
    )


def _patch_nominatim(mapping=None, side_effect=None):
    if side_effect is None:
        def _geocode(query, _mapping=mapping or {}):
            for key, result in _mapping.items():
                if key in query:
                    return result
            raise Exception(f"No map for {query!r}")
        side_effect = _geocode
    geocode_patch = mock.patch(
        "routes.management.commands.retry_station_geocoding.GeocodingClient.geocode",
        side_effect=side_effect,
    )
    return _Combined(geocode_patch, _patch_requests(photon=lambda q: None, openmeteo=lambda q: None))


def _feature(lon, lat, country_code):
    return {
        "geometry": {"coordinates": [lon, lat]},
        "properties": {"countrycode": country_code},
    }


def _run(command=("retry_station_geocoding",), **kwargs):
    out = io.StringIO()
    call_command(*command, stdout=out, **kwargs)
    return out.getvalue()


class TestRetryCommand:
    def test_nominatim_success_imports_and_clears_failures(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        _write_failures(failures, ["1,PILOT #1,I-35,Austin,TX,geocoding failed for all query variants"])

        mapping = {"Austin": _us_result(30.27, -97.74)}
        with _patch_nominatim(mapping):
            report = _run(csv=str(csv_path), failures_csv=str(failures),
                          nominatim_delay=0, photon_delay=0)

        assert Station.objects.count() == 1
        assert "successfully geocoded now:" in report
        assert "still ungeocodable:" in report
        text = failures.read_text(encoding="utf-8")
        assert text.strip().splitlines() == [FAILURE_HEADER]

    def test_photon_fallback_imports_when_nominatim_fails(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        _write_failures(failures, ["1,PILOT #1,I-35,Austin,TX,geocoding failed for all query variants"])

        with _patch_nominatim(side_effect=lambda q: (_ for _ in ()).throw(Exception("down"))):
            with _patch_requests(
                photon=lambda q: [_feature(-97.74, 30.27, "US")],
                openmeteo=lambda q: None,
            ):
                report = _run(csv=str(csv_path), failures_csv=str(failures),
                              nominatim_delay=0, photon_delay=0)

        station = Station.objects.get()
        assert station.city == "Austin"
        assert station.latitude == 30.27 and station.longitude == -97.74
        assert failures.read_text(encoding="utf-8").strip().splitlines() == [FAILURE_HEADER]
        assert "successfully geocoded now:" in report

    def test_recovered_row_keeps_csv_price(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["9,PILOT #9,I-35,Austin,TX,R1,3.00733333"])
        _write_failures(failures, ["9,PILOT #9,I-35,Austin,TX,geocoding failed for all query variants"])

        with _patch_nominatim({"Austin": _us_result(30.27, -97.74)}):
            _run(csv=str(csv_path), failures_csv=str(failures),
                 nominatim_delay=0, photon_delay=0)

        station = Station.objects.get()
        assert str(station.retail_price) == "3.00733333"
        assert station.rack_id == "R1"

    def test_identity_already_in_db_is_skipped(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        _write_failures(failures, ["1,PILOT #1,I-35,Austin,TX,geocoding failed for all query variants"])
        Station.objects.create(
            opis_id="1", name="PILOT #1", address="I-35", city="Austin",
            state="TX", retail_price="3.00", latitude=30.27, longitude=-97.74,
        )

        geocode = mock.MagicMock()
        with _patch_nominatim(side_effect=geocode):
            report = _run(csv=str(csv_path), failures_csv=str(failures),
                          nominatim_delay=0, photon_delay=0)

        assert Station.objects.count() == 1
        geocode.assert_not_called()
        assert "skipped (already in DB):" in report

    def test_both_providers_fail_kept_in_failures(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        _write_failures(failures, ["1,PILOT #1,I-35,Austin,TX,geocoding failed for all query variants"])

        with _patch_nominatim(side_effect=lambda q: (_ for _ in ()).throw(Exception("down"))):
            with _patch_requests(photon=lambda q: None, openmeteo=lambda q: None):
                report = _run(csv=str(csv_path), failures_csv=str(failures),
                              nominatim_delay=0, photon_delay=0)

        assert Station.objects.count() == 0
        text = failures.read_text(encoding="utf-8").strip().splitlines()
        assert text[0] == FAILURE_HEADER
        assert text[1] == '1,PILOT #1,I-35,Austin,TX,"still ungeocodable (Photon, Nominatim, Open-Meteo)"'
        assert "still ungeocodable:" in report

    def test_openmeteo_fallback_imports_when_others_fail(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        _write_failures(failures, ["1,PILOT #1,I-35,Austin,TX,geocoding failed for all query variants"])

        with _patch_nominatim(side_effect=lambda q: (_ for _ in ()).throw(Exception("down"))):
            with _patch_requests(
                photon=lambda q: None,
                openmeteo=lambda q: [{"longitude": -97.74, "latitude": 30.27, "country_code": "US"}],
            ):
                report = _run(csv=str(csv_path), failures_csv=str(failures),
                              nominatim_delay=0, photon_delay=0)

        station = Station.objects.get()
        assert station.city == "Austin"
        assert station.latitude == 30.27 and station.longitude == -97.74
        assert failures.read_text(encoding="utf-8").strip().splitlines() == [FAILURE_HEADER]
        assert "successfully geocoded now:" in report

    def test_non_us_photon_result_is_rejected(self, tmp_path):
        csv_path = tmp_path / "prices.csv"
        failures = tmp_path / "failures.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        _write_failures(failures, ["1,PILOT #1,I-35,Austin,TX,geocoding failed for all query variants"])

        with _patch_nominatim(side_effect=lambda q: (_ for _ in ()).throw(Exception("down"))):
            with _patch_requests(
                photon=lambda q: [_feature(-97.74, 30.27, "MX")],
                openmeteo=lambda q: None,
            ):
                _run(csv=str(csv_path), failures_csv=str(failures),
                     nominatim_delay=0, photon_delay=0)

        assert Station.objects.count() == 0
        assert "still ungeocodable" in failures.read_text(encoding="utf-8")

    def test_missing_files_raise_command_error(self, tmp_path):
        with pytest.raises(CommandError):
            _run(csv=str(tmp_path / "missing.csv"), failures_csv=str(tmp_path / "f.csv"))
        csv_path = tmp_path / "prices.csv"
        _write_csv(csv_path, ["1,PILOT #1,I-35,Austin,TX,,3.09"])
        with pytest.raises(CommandError):
            _run(csv=str(csv_path), failures_csv=str(tmp_path / "missing.csv"))
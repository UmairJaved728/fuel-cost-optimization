"""Isolated geocoding client.

Only this module knows that Nominatim (OpenStreetMap) is the geocoding
provider.  Everywhere else in the application works with the plain
:class:`GeocodingResult` value and never needs to know the provider.

Geocoding is used in two contexts:

* user start/finish addresses (at request time, cached),
* station records (only during offline preprocessing).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

from routes.exceptions import GeocodingFailedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeocodingResult:
    latitude: float
    longitude: float
    display_name: str
    country_code: str


class GeocodingClient:
    """Resolves a free-text address to coordinates using an HTTP geocoder."""

    _US_COUNTRY_CODES = {"us", "usa", "united states"}

    def __init__(self) -> None:
        self._base_url = settings.GEOCODER_BASE_URL.rstrip("/")
        self._timeout = settings.GEOCODING_TIMEOUT_SECONDS
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": settings.GEOCODER_USER_AGENT,
                "Referer": settings.GEOCODER_USER_AGENT,
            }
        )

    def geocode(self, address: str) -> GeocodingResult:
        """Geocode ``address`` and return the best US result.

        Unraises nothing on network/provider failure, timeouts are enforced
        and there is no unbounded retry loop.

        Raises
        ------
        GeocodingFailedError
            If the provider is unreachable, the response is unusable, or no USA
            result matches.
        """
        logger.info("geocoding address=%r", address)

        params = {
            "q": address,
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "us",
            "addressdetails": 1,
        }

        try:
            resp = self._session.get(
                self._base_url + "/search",
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise GeocodingFailedError(
                f"Geocoding service unreachable for {address!r}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise GeocodingFailedError(
                f"Geocoding service returned HTTP {resp.status_code} for {address!r}."
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise GeocodingFailedError(
                f"Geocoding service returned malformed JSON for {address!r}."
            ) from exc

        if not data:
            raise GeocodingFailedError(f"No geocoding match found for {address!r}.")

        try:
            feature = data[0]
            latitude = float(feature["lat"])
            longitude = float(feature["lon"])
            display_name = feature.get("display_name", address)
        except (KeyError, TypeError, ValueError) as exc:
            raise GeocodingFailedError(
                f"Geocoding service returned an unusable result for {address!r}."
            ) from exc

        country_code = self._extract_country_code(feature)
        result = GeocodingResult(
            latitude=latitude,
            longitude=longitude,
            display_name=display_name,
            country_code=country_code,
        )
        logger.info(
            "geocoding result address=%r country_code=%r lat=%.5f lon=%.5f",
            address,
            result.country_code,
            result.latitude,
            result.longitude,
        )
        return result

    @staticmethod
    def _extract_country_code(feature: dict) -> str:
        address = feature.get("address") or {}
        country_code = address.get("country_code", "")
        if country_code:
            return country_code.lower()
        # Fall back to parsing the display name.
        display_name = feature.get("display_name", "").lower()
        if "united states" in display_name or "united states of america" in display_name:
            return "us"
        return country_code

    @classmethod
    def is_us_result(cls, result: GeocodingResult) -> bool:
        return result.country_code.lower() in cls._US_COUNTRY_CODES
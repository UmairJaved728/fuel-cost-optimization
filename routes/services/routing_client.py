"""Thin wrapper around the public OSRM routing API.

The public demo server (https://router.project-osrm.org) accepts unauthenticated
requests and supports the *driving* profile.  This client makes one call per
plan invocation, using the exact parameters required by the assignment, and
returns a normalised dataclass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from routes.exceptions import NoRouteFoundError, RoutingServiceError

logger = logging.getLogger(__name__)

METERS_TO_MILES = 1609.344


@dataclass(frozen=True)
class RouteInfo:
    distance_meters: float
    duration_seconds: float
    geometry: dict[str, Any]  # GeoJSON LineString


class RoutingClient:
    """Builds and executes an OSRM route request."""

    # Fixed per assignment requirement 5
    _PARAMS: dict[str, str] = {
        "alternatives": "false",
        "steps": "false",
        "geometries": "geojson",
        "overview": "full",
    }

    def __init__(self) -> None:
        self._base_url = settings.OSRM_BASE_URL.rstrip("/")
        self._timeout = settings.ROUTING_CLIENT_TIMEOUT_SECONDS
        self._session = self._build_session()

    def get_route(
        self,
        start_lat: float,
        start_lon: float,
        finish_lat: float,
        finish_lon: float,
    ) -> RouteInfo:
        """Return the fastest driving route between two coordinate pairs.

        Raises
        ------
        NoRouteFoundError
            If OSRM reports that no route exists between the supplied points.
        RoutingServiceError
            On HTTP / timeout / malformed-response errors.
        """
        url = (
            f"{self._base_url}/route/v1/driving/"
            f"{start_lon},{start_lat};{finish_lon},{finish_lat}"
        )

        logger.info(
            "OSRM request url=%s params=%s",
            url,
            self._PARAMS,
        )

        try:
            resp = self._session.get(url, params=self._PARAMS, timeout=self._timeout)
        except requests.RequestException as exc:
            raise RoutingServiceError(
                f"Failed to reach the OSRM routing service: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise RoutingServiceError(
                f"OSRM returned HTTP {resp.status_code}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise RoutingServiceError(
                "OSRM returned a non-JSON response."
            ) from exc

        if body.get("code") != "Ok":
            raise NoRouteFoundError(
                f"OSRM could not find a route: {body.get('message', 'Unknown')}"
            )

        try:
            route = body["routes"][0]
        except (KeyError, IndexError) as exc:
            raise NoRouteFoundError("OSRM response contained no route.") from exc

        geometry = route.get("geometry")
        if not geometry or geometry.get("type") != "LineString":
            raise RoutingServiceError(
                "OSRM response geometry was not a valid LineString."
            )

        result = RouteInfo(
            distance_meters=route["distance"],
            duration_seconds=route["duration"],
            geometry=geometry,
        )

        logger.info(
            "OSRM response distance=%.1fm duration=%.1fs",
            result.distance_meters,
            result.duration_seconds,
        )

        return result

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=settings.ROUTING_CLIENT_RETRIES,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
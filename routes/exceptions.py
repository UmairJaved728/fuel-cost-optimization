"""Application error types and a DRF exception handler.

All API errors share one JSON shape:

    {"error": {"code": "...", "message": "..."}}

The handler deliberately never exposes stack traces.
"""

import logging

from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Base class for controlled application errors."""

    code = "INTERNAL_ERROR"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidRequestError(APIError):
    code = "INVALID_REQUEST"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "The request is invalid."


class InvalidLocationError(APIError):
    code = "INVALID_LOCATION"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "The supplied location is invalid."


class LocationOutsideUSAError(APIError):
    code = "LOCATION_OUTSIDE_USA"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "Both locations must be inside the USA."


class GeocodingFailedError(APIError):
    code = "GEOCODING_FAILED"
    http_status = status.HTTP_502_BAD_GATEWAY
    default_message = "The geocoding service could not resolve the address."


class NoRouteFoundError(APIError):
    code = "NO_ROUTE_FOUND"
    http_status = status.HTTP_404_NOT_FOUND
    default_message = "No driving route could be found between the requested locations."


class RoutingServiceError(APIError):
    code = "ROUTING_SERVICE_ERROR"
    http_status = status.HTTP_502_BAD_GATEWAY
    default_message = "The routing service returned an error."


class NoFeasibleFuelPlanError(APIError):
    code = "NO_FEASIBLE_FUEL_PLAN"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_message = (
        "No feasible fuel plan exists for the selected route with the "
        "available stations and a 500 mile maximum vehicle range."
    )


def api_exception_handler(exc, context):
    """DRF exception handler that returns the unified error envelope."""
    if isinstance(exc, APIError):
        return Response(
            {"error": {"code": exc.code, "message": exc.message}},
            status=exc.http_status,
        )

    if isinstance(exc, ValidationError):
        return Response(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": _flatten_validation_error(exc.detail),
                }
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, Http404):
        return Response(
            {"error": {"code": "NO_ROUTE_FOUND", "message": NoRouteFoundError.default_message}},
            status=status.HTTP_404_NOT_FOUND,
        )

    response = drf_exception_handler(exc, context)
    if response is not None:
        try:
            detail = str(response.data.get("detail", ""))
        except Exception:  # noqa: BLE001
            detail = ""
        return Response(
            {"error": {"code": "INVALID_REQUEST", "message": detail or "Request failed."}},
            status=response.status_code,
        )

    logger.exception("Unhandled exception in request", exc_info=exc)
    return Response(
        {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected internal error occurred."}},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _flatten_validation_error(detail) -> str:
    if isinstance(detail, dict):
        return "; ".join(
            f"{key}: {_flatten_validation_error(value)}" for key, value in detail.items()
        )
    if isinstance(detail, (list, tuple)):
        return "; ".join(_flatten_validation_error(item) for item in detail)
    return str(detail)
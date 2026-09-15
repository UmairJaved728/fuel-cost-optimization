"""Thin API views.

Business logic lives in ``routes/services``; views only validate input via the
serializers and delegate to the route service.
"""

import logging

from django.views.generic import TemplateView
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routes.serializers import (
    PlanRequestSerializer,
    PlanResponseSerializer,
)
from routes.services.route_service import RouteService

logger = logging.getLogger(__name__)


class PlanView(APIView):
    """POST /api/v1/routes/plan/"""

    serializer_class = PlanRequestSerializer

    @extend_schema(
        request=PlanRequestSerializer,
        responses={
            200: PlanResponseSerializer,
            400: OpenApiResponse(description="Invalid request or location."),
            404: OpenApiResponse(description="No route found."),
            422: OpenApiResponse(description="No feasible fuel plan."),
            502: OpenApiResponse(description="Geocoding or routing service error."),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = PlanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = RouteService().plan(**serializer.validated_data)
        return Response(result, status=status.HTTP_200_OK)


class DemoTemplateView(TemplateView):
    template_name = "demo.html"
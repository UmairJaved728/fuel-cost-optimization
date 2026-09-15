from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from routes.views import DemoTemplateView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("routes.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
    path("demo/", DemoTemplateView.as_view(), name="demo"),
]
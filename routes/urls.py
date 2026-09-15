from django.urls import path

from routes.views import PlanView

urlpatterns = [
    path("routes/plan/", PlanView.as_view(), name="route-plan"),
]
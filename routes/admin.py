from django.contrib import admin

from .models import Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "city",
        "state",
        "retail_price",
        "latitude",
        "longitude",
    )
    list_filter = ("state",)
    search_fields = ("name", "city", "address")
    list_select_related = False
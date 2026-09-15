"""Pragmatic contiguous-USA boundary validation.

This is deliberately *not* a single giant rectangle.  The continental US is
approximated by a coarse polygon (with a separate box for Alaska and Hawaii)
so that obviously non-US inputs such as Toronto, Montreal or Paris are
rejected.  It is a permissive approximation: near-coastal and border areas
close to the actual boundary may be accepted or rejected either way without
affecting the purpose of the check, which is to reject *obvious* non-USA
requests.

The check is a modeling approximation used during input validation only.
"""

from __future__ import annotations

from django.contrib.gis.geos import GEOSGeometry

# Approximate outline of the contiguous United States.  Vertices are
# (longitude, latitude) in WKT order.  The border region is traced with a few
# extra vertices near the real international boundary so that legitimate US
# locations in border areas (the lower Rio Grande, the Florida Keys, the
# Washington 49th parallel) are not rejected.
_CONUS_OUTLINE = [
    (-67.00, 45.00),  # eastern Maine
    (-68.90, 47.30),  # NE Maine / Canada
    (-71.00, 45.30),  # northern New Hampshire / Vermont
    (-73.00, 45.00),  # northern New York (Lake Champlain)
    (-75.40, 44.10),  # St. Lawrence / Lake Ontario
    (-79.00, 43.60),  # Niagara, NY-ON border
    (-82.50, 43.30),  # western Lake Erie
    (-83.40, 44.40),  # Saginaw Bay, Michigan
    (-84.40, 46.00),  # Straits of Mackinac
    (-84.00, 47.30),  # northern Lake Huron
    (-89.30, 48.10),  # western Lake Superior
    (-95.10, 49.00),  # north-west Minnesota
    (-97.00, 49.00),  # North Dakota
    (-104.00, 49.00),  # Montana
    (-108.00, 49.00),
    (-111.00, 49.00),
    (-114.10, 49.00),
    (-116.04, 48.99),  # Idaho panhandle
    (-117.10, 48.99),  # NE Washington
    (-120.00, 48.99),  # northern Washington
    (-123.30, 49.00),  # Washington 49th parallel (Blaine / Point Roberts)
    (-124.70, 48.30),  # Cape Flattery (north-west corner)
    (-124.30, 46.10),  # Washington/Oregon coast
    (-124.10, 42.00),  # Oregon/California coast
    (-122.00, 36.90),  # central California
    (-120.00, 34.40),  # southern California
    (-117.10, 32.50),  # San Diego / Mexico border
    (-114.80, 32.70),  # California/Arizona border
    (-113.00, 31.30),  # Arizona/Mexico
    (-111.00, 31.30),
    (-108.20, 31.30),  # New Mexico/Mexico
    (-106.49, 31.72),  # El Paso (city sits just south of the true bend)
    (-104.40, 29.60),  # Rio Grande valley (Presidio)
    (-102.70, 29.40),  # Rio Grande valley
    (-100.20, 29.70),  # Del Rio area
    (-99.60, 28.90),  # above Laredo
    (-99.50, 27.50),  # Laredo
    (-98.20, 26.20),  # McAllen / lower Rio Grande
    (-97.45, 25.90),  # Brownsville
    (-94.00, 29.00),  # Louisiana coast
    (-89.00, 30.00),
    (-88.00, 30.20),
    (-87.00, 30.30),
    (-84.30, 29.80),  # north-west Florida
    (-82.00, 28.00),  # Florida gulf coast
    (-81.80, 24.55),  # Key West (southernmost point)
    (-81.10, 24.71),  # Marathon, Florida Keys
    (-80.30, 26.50),  # Florida east coast (Keys -> up)
    (-80.00, 27.20),  # Miami
    (-81.30, 30.70),  # Jacksonville
    (-80.80, 32.00),  # Georgia/South Carolina coast
    (-77.60, 34.70),  # North Carolina coast
    (-75.60, 35.10),
    (-75.30, 38.50),  # Virginia / Maryland
    (-74.00, 40.50),  # New York / New Jersey
    (-70.10, 41.60),  # Cape Cod
    (-69.90, 44.00),  # Maine coast
]

_ALASKA_BOX = (-179.10, 51.00, -129.90, 71.50)  # lon_min, lat_min, lon_max, lat_max
_HAWAII_BOX = (-160.20, 18.90, -154.80, 28.40)

# A few regions where the coarse outline cannot hug the real boundary closely
# enough (the polygon's straight edges sweep away legitimate land).  These
# boxes are small and entirely within the USA, so adding them never admits
# foreign points.
_EXTRA_BOXES = [
    (-99.80, 25.80, -97.30, 27.70),  # Lower Rio Grande valley (Laredo-McAllen-Brownsville)
    (-82.10, 24.40, -80.30, 25.40),  # Florida Keys through the southern tip
    (-82.90, 26.30, -82.30, 29.00),  # west-central Florida gulf coast (Tampa/Bradenton)
    (-73.60, 44.80, -73.30, 45.10),  # Lake Champlain border valley (Champlain/Plattsburgh)
]


def _polygon() -> GEOSGeometry:
    ring = list(_CONUS_OUTLINE)
    ring.append(ring[0])
    coords = ", ".join(f"{lon} {lat}" for lon, lat in ring)
    return GEOSGeometry(f"POLYGON(({coords}))", srid=4326)


_CONUS = None
_ALASKA = None
_HAWAII = None


def _get_geometries():
    global _CONUS, _ALASKA, _HAWAII
    if _CONUS is None:
        _CONUS = _polygon()
        _ALASKA = GEOSGeometry(
            f"POLYGON(({_ALASKA_BOX[0]} {_ALASKA_BOX[1]}, "
            f"{_ALASKA_BOX[2]} {_ALASKA_BOX[1]}, "
            f"{_ALASKA_BOX[2]} {_ALASKA_BOX[3]}, "
            f"{_ALASKA_BOX[0]} {_ALASKA_BOX[3]}, "
            f"{_ALASKA_BOX[0]} {_ALASKA_BOX[1]}))",
            srid=4326,
        )
        _HAWAII = GEOSGeometry(
            f"POLYGON(({_HAWAII_BOX[0]} {_HAWAII_BOX[1]}, "
            f"{_HAWAII_BOX[2]} {_HAWAII_BOX[1]}, "
            f"{_HAWAII_BOX[2]} {_HAWAII_BOX[3]}, "
            f"{_HAWAII_BOX[0]} {_HAWAII_BOX[3]}, "
            f"{_HAWAII_BOX[0]} {_HAWAII_BOX[1]}))",
            srid=4326,
        )
    return _CONUS, _ALASKA, _HAWAII


def is_within_usa(latitude: float, longitude: float) -> bool:
    """Return True if the coordinate lies inside a coarse USA boundary."""
    conus, alaska, hawaii = _get_geometries()
    point = GEOSGeometry(f"POINT({longitude} {latitude})", srid=4326)
    if conus.contains(point) or alaska.contains(point) or hawaii.contains(point):
        return True
    for lon_min, lat_min, lon_max, lat_max in _EXTRA_BOXES:
        if lon_min <= longitude <= lon_max and lat_min <= latitude <= lat_max:
            return True
    return False
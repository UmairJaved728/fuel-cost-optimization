"""Shared test helpers.

``dense_line`` interpolates extra vertices along a straight (flat) lon/lat
segment so the PostGIS ``geography`` interpretation is a good approximation of
the same straight line.  Real OSRM polylines in production carry thousands of
intermediate vertices, so the corridor test in the live system never depends
on this; only the sparse synthetic geometries in the tests do.
"""

from django.contrib.gis.geos import LineString


def dense_line(coordinates, steps_per_segment=8):
    """Return a LineString with ``steps_per_segment`` interpolated points per
    segment between each pair of ``(lon, lat)`` coordinates (SRID 4326)."""
    points = []
    for (x1, y1), (x2, y2) in zip(coordinates, coordinates[1:]):
        for k in range(steps_per_segment):
            t = k / steps_per_segment
            points.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    points.append(coordinates[-1])
    return LineString(points, srid=4326)
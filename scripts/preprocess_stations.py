#!/usr/bin/env python
"""Convenience wrapper around the offline station preprocessing.

This is equivalent to running:

    python manage.py import_stations --csv path/to/fuel_prices.csv

Usage:

    python scripts/preprocess_stations.py path/to/fuel_prices.csv [--limit N]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/preprocess_stations.py <csv_path> [options]")
        return 2

    csv_path = sys.argv[1]
    args = ["--csv", csv_path]
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        args += ["--limit", sys.argv[idx + 1]]
    if "--geocode-delay" in sys.argv:
        idx = sys.argv.index("--geocode-delay")
        args += ["--geocode-delay", sys.argv[idx + 1]]

    call_command("import_stations", *args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
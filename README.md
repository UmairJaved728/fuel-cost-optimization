# Fuel Route Optimizer

A Django + PostgreSQL/PostGIS API that computes a **minimum-cost fuel purchase
plan** for a fixed driving route across the continental USA.

Given a start and finish location, the service:

1. geocodes the locations (when addresses are supplied) — cache-aware,
2. requests the fastest driving route from the free public [OSRM](https://router.project-osrm.org)
   demo server,
3. selects fuel stations inside a **10 mile corridor** around the route using the
   PostGIS spatial index,
4. computes the **minimum fuel purchase cost** for that fixed route with a
   dynamic-programming-optimal greedy algorithm, and
5. returns the route geometry, the fuel stops, and the total cost as JSON.

---

## 1. Assignment requirements

* Route two locations in the US (addresses *or* coordinates).
* Maximize the chance of completing the route off USA highways by using truck-stop
  retail fuel prices from the supplied OPIS CSV.
* One external routing API call per unique route (cached).
* Free public routing API only — no API keys.
* Offline station preprocessing: geocode stations once, store them, never geocode
  from the API request path.

Deliverables implemented:

| Area | Where |
| --- | --- |
| Django project + app | `config/`, `routes/` |
| PostGIS Station model + migration | `routes/models.py`, `routes/migrations/0001_initial.py` |
| Offline importer | `routes/management/commands/import_stations.py` |
| Routing client (OSRM) | `routes/services/routing_client.py` |
| Geocoding client (Nominatim) | `routes/services/geocoding_client.py` |
| Spatial station service | `routes/services/station_service.py` |
| Pure fuel optimizer | `routes/services/fuel_optimizer.py` |
| Orchestration + caching | `routes/services/route_service.py`, `routes/services/cache_service.py` |
| API (DRF) | `routes/views.py`, `routes/serializers.py`, `routes/urls.py` |
| OpenAPI schema + Swagger UI | `/api/schema/`, `/api/docs/` |
| Interactive demo | `/demo/` |
| Tests (offline, no internet) | `routes/tests/` (106 tests) |

---

## 2. Architecture

```
HTTP request
   └─ PlanView (DRF) ─ validates input serializer
        └─ RouteService.plan()
             ├─ _resolve_location()   GeocodingClient (address only) ─ DB cache
             ├─ _get_route_info()     RoutingClient (OSRM) ────────── DB cache
             ├─ geojson_to_line()     GeoJSON → PostGIS LineString
             ├─ find_candidate_stations()  PostGIS ST_DWithin corridor query
             │                            + ST_LineLocatePoint projection
             ├─ optimize()            pure greedy/DP fuel optimizer
             └─ _build_response()     presentation rounding only
```

The API layer is deliberately thin.  All business logic lives in
`routes/services/`, and the fuel optimizer (`fuel_optimizer.py`) is a pure Python
module with **no Django imports** so it can be unit-tested exhaustively.

---

## 3. Technology choices

| Concern | Choice | Why |
| --- | --- | --- |
| Web framework | Django 6.1 + Django REST Framework | Latest stable, batteries included, serializers + schema tooling |
| Database | PostgreSQL 16 + PostGIS 3.6 | Spatial index & SQL-side corridor search |
| Routing | OSRM public server | Free, no API key, standard `/route/v1/driving` |
| Geocoding | Nominatim public server (`localhost:3000` demo server) | Free, no API key, geocoding happens **offline** only |
| Caching | Django database cache (`django_cache` table) | PostgreSQL-backed, no Redis, survives restarts |
| Tests | pytest + pytest-django | 106 offline tests, no network access |

### Why OSRM
OSRM's public demo server returns route geometry, distance and duration with one
unauthenticated request, no API key, and generous usage terms for light usage.
The route is cached by the service so each unique start/finish triggers **at most
one** OSRM call.

### Why PostGIS
The corridor "stations near this line" query is a textbook
`ST_DWithin(geography, geography, meters)` call backed by a GiST spatial index.
PostGIS keeps the relevance filtering in the database, so thousands of stations
never leave SQL.

### Why station preprocessing is offline
Geocoding thousands of OPIS stations on the live request path would be slow,
rate-limited, and fragile.  Instead the importer geocodes each unique station
once (with pacing, fallback queries, and a failures file), stores
latitude/longitude + a PostGIS point, and the API only ever *queries* the DB.

---

## 4. Request lifecycle

1. **Input validation** — each of `start` / `finish` must contain exactly one of
   an *address* or *latitude/longitude* pair.  Bad input → `400`.
2. **Geocoding** (address only) — cache hit returns instantly; a miss calls
   Nominatim once and stores the result.  Non-USA results are rejected.
3. **Coordinate validation** — both resolved points must fall inside a coarse
   contiguous-USA, Alaska or Hawaii boundary → otherwise `400`.
4. **Routing** — cache hit returns the stored OSRM response; a miss calls OSRM
   once and stores distance/duration/geometry for 24 h (configurable).
5. **Station selection** — PostGIS returns every station within the corridor
   (default 10 miles) as a geography distance.
6. **Station positioning** — `ST_LineLocatePoint` projects each station onto the
   route causing its along-route position in miles.
7. **Optimization** — the greedy algorithm decides, per station, whether to buy
   fuel and how much.
8. **Response** — a validated plan; every internal consistency check (fuel
   never negative, fuel never above the tank, forward-ordered stops, totals
   matching individual stops) must pass before `200` is returned.

---

## 5. The fuel algorithm

The truck travels at fixed speed along a fixed route.  Fuel is the only resource.

* At each station the planner starts from the fuel *actually on board* after
  driving to it.
* If the destination is reachable with that fuel, buy **nothing**.
* Otherwise, scan **future** stations within the vehicle's maximum range:
  * if a **cheaper** station is reachable, buy **only the fuel needed to reach
    it** (never over-buy at a more expensive stop),
  * else, if the destination is within max range, buy **only the fuel needed to
    reach the destination**,
  * else, **fill the tank**.
* Money and fuel are handled with Python `Decimal` right up to presentation;
  totals are rounded to cents *only* in the JSON response.

All station candidates are sorted by route position; the optimizer's state never
moves backward along the route (invariant enforced and checked).

### Why the greedy algorithm is optimal
This is the standard optimal policy for the "gas station problem" on a fixed
path.  The proof is a straightforward exchange argument:

* An optimal plan never does more than one partial fill, and never fills up at a
  station while a strictly cheaper station is reachable.
* At a station where the next cheaper reachable station is `d` miles away, buying
  fewer gallons than needed to reach it is never better (you would pay the same
  or higher price later), and buying more is never better (a cheaper price is
  available within range).
* Therefore the greedy choice — buy exactly enough to reach the next cheaper
  station, else exactly enough to reach the destination, else fill — matches an
  optimal plan at every decision point.

This is *also* verified empirically: the test suite contains an exhaustive
brute-force comparison that enumerates every feasible purchase strategy on a
fine gallon grid (0.1 gal steps) for randomized station layouts and running
tanks, and asserts the greedy total cost equals the brute-force minimum.

**The guarantee applies to the fixed-route model.**  It optimizes fuel purchase
*for a given driving route*; it does not claim global optimization of the road
route itself.

---

## 6. Vehicle & planning assumptions

| Assumption | Value | Note |
| --- | --- | --- |
| MPG | 10 | `FUEL_MPG` |
| Fuel tank | 50 gallons | `TANK_CAPACITY_GALLONS` |
| Maximum range | 500 miles | `MAX_RANGE_MILES` (tank × mpg) |
| Starting fuel | 50 gallons (full) | `STARTING_FUEL_GALLONS` |
| Corridor width | 10 miles around route | `ROUTE_CORRIDOR_MILES` |
| Currency | USD | fuel costs in US dollars |
| Refueling | continuous | any amount 0–tank may be purchased |

### Station detour limitation
Station access and re-entry road-network detours are **not individually
modeled**.  Stations are selected geographically near the route and represented
by their projected position along that route.  This avoids per-station routing
requests and keeps the solution within the assignment's external API limits.

The fuel optimization guarantee applies to the fixed-route model and therefore
does not include detour driving (see section 5).  A station that is geographically
inside the corridor is assumed to require negligible detour time/fuel.

---

## 7. API documentation

Interactive Swagger UI: `GET /api/docs/`
OpenAPI (JSON/YAML): `GET /api/schema/`

### POST /api/v1/routes/plan/

Request body — `start` and `finish` each contain **either** `address` **or**
`latitude` + `longitude`:

```json
{
  "start":  {"address": "Kansas City, MO"},
  "finish": {"latitude": 35.0844, "longitude": -106.6504}
}
```

### Example response (abridged)

```json
{
  "route": {
    "distance_miles": 792.40,
    "duration_minutes": 735.20,
    "geometry": {
      "type": "LineString",
      "coordinates": [[-94.58, 39.09], [-94.57, 39.08], "..."]}
  },
  "vehicle": {
    "mpg": 10,
    "max_range_miles": 500,
    "tank_capacity_gallons": 50.00,
    "starting_fuel_gallons": 50.00
  },
  "fuel_plan": {
    "total_fuel_purchased_gallons": 70.00,
    "total_fuel_cost": 236.25,
    "currency": "USD"
  },
  "fuel_stops": [
    {
      "stop_number": 1,
      "station": {"id": 4301, "name": "LOVES #242",
                  "address": "2415 W HWY 76", "city": "BRANSON",
                  "state": "MO", "latitude": 36.63, "longitude": -93.26},
      "route_position_miles": 292.10,
      "distance_from_previous_stop_miles": 292.10,
      "retail_price_per_gallon": 3.007,
      "fuel_before_stop_gallons": 20.80,
      "fuel_purchased_gallons": 49.20,
      "fuel_after_stop_gallons": 70.00,
      "estimated_cost": 147.94
    }
  ]
}
```

### Errors

All errors share one envelope: `{"error": {"code": "...", "message": "..."}}`

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `INVALID_REQUEST` | Malformed JSON, missing/invalid fields |
| 400 | `INVALID_LOCATION` | Location missing address+coords, bad values, out-of-range coordinates |
| 400 | `LOCATION_OUTSIDE_USA` | Either point outside the USA boundary |
| 404 | `NO_ROUTE_FOUND` | OSRM could not find a route |
| 422 | `NO_FEASIBLE_FUEL_PLAN` | No combination of stations can cover the route |
| 502 | `GEOCODING_FAILED` | Nominatim could not resolve an address |
| 502 | `ROUTING_SERVICE_ERROR` | OSRM unavailable / malformed response |
| 500 | `INTERNAL_ERROR` | Unexpected failure (never leaks stack traces) |

---

## 8. Local Windows setup

### 1. Prerequisites
* Python 3.12+
* PostgreSQL 16 with **PostGIS 3.4+** enabled
* Git

### 2. PostgreSQL / PostGIS on Windows (no Docker)
1. Download the Windows installer from https://www.postgresql.org/download/windows/
   e.g. `postgresql-16.x-windows-x64.exe` and install PostGIS as a **stack** or
   component (the installer offers PostGIS as part of the bundle).
2. Ensure the PostgreSQL `bin` folder (containing `gdal`/`geos` DLLs) is on
   `PATH`, or point `GDAL_LIBRARY_PATH` / `GEOS_LIBRARY_PATH` at
   `C:\Program Files\PostgreSQL\16\bin\libgdal-35.dll` (the settings file
   auto-discovers them).
3. Create the database and extension:

```
createdb -h localhost -p 5432 -U postgres fuel_route_optimizer
psql  -h localhost -p 5432 -U postgres -d fuel_route_optimizer -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

> PostGIS is also enabled by the migration (`routes/migrations/0001_initial.py`
> runs `CreateExtension("postgis")`), so step 3 is belt-and-braces.

### 3. Project setup

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

Then edit `.env` so `DATABASE_URL` matches your local Postgres:

```
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/fuel_route_optimizer
```

### 4. Migrate

```
python manage.py migrate
```

Migration `routes.0002_create_cache_table` also creates the `django_cache`
table — no separate `createcachetable` step.

### 5. Import the stations (offline preprocessing)

```
python manage.py import_stations --csv fuel-prices-for-be-assessment.csv
```

The importer:

* validates headers and required fields;
* filters to US states, rejects malformed prices (never coerced to zero);
* deduplicates by station identity `(OPIS ID, normalized address, city, state)`
  keeping the row with the **lowest** price when several rows share an identity
  (they usually differ only by name spelling, e.g. `PILOT TRAVEL CENTER #1243` vs
  `PILOT #1243`);
* geocodes each unique station against Nominatim with pacing
  (`--geocode-delay`, default 1.1 s), trying address → name → `city, state`
  fallbacks, and validates results are real US coordinates;
* bulk-loads in batches (`Station.objects.bulk_create`);
* prints a report: raw records, US records, duplicates removed, successfully
  geocoded, failed geocoding, imported stations;
* writes failures to `data/geocoding_failures.csv` (never silently drops them).

Re-runs are safe and resumable — already-imported identities are skipped.  For a
slow connection run in chunks:

```
python manage.py import_stations --csv fuel-prices-for-be-assessment.csv --limit 500
```

A helper wrapper exists at `scripts/preprocess_stations.py`.

### 6. Run the tests (no internet required)

```
.\.venv\Scripts\python.exe -m pytest
```

All external services (OSRM, Nominatim) are mocked in the tests; the suite runs
entirely offline against a disposable `test_fuel_route_optimizer` PostGIS
database (the real `fuel_route_optimizer` database is never touched).

### 7. Run the server

```
python manage.py runserver
```

* API: `POST http://localhost:8000/api/v1/routes/plan/`
* Interactive demo: `http://localhost:8000/demo/`
* Swagger UI: `http://localhost:8000/api/docs/`
* OpenAPI schema: `http://localhost:8000/api/schema/`

---

## 9. Caching

* **Route cache** — OSRM responses keyed on start latitude/longitude, finish
  latitude/longitude and routing profile.  Coordinates are normalized before
  keying (e.g. `41.878100` and `41.8781` produce the same key).  Example key:
  `route:driving:41.8781,-87.6298:40.7128,-74.006`.  TTL 86400 s (default,
  configurable via `CACHE_TTL_SECONDS`).
* **Geocode cache** — address → coordinates results cached (TTL same setting).
* Backend: Django's database cache (the `django_cache` table), PostgreSQL-backed
  — no Redis.

Verified in tests: identical repeated requests call OSRM once; different start or
finish produce a new key; a cached address never calls the geocoder again.

---

## 10. Performance considerations

* Corridor query runs in PostgreSQL with a GiST index on `location`
  (geography point) — only stations within the corridor ever cross the wire.
* Route-relative position via a single `ST_LineLocatePoint` projection in the
  same query.
* Bulk station import in batches of 500.
* Money/fuel arithmetic in `Decimal`; only the final response rounds.

---

## 11. External API dependencies

* **OSRM public demo server** (`https://router.project-osrm.org`) — routing.
  Free and unauthenticated; suitable for light usage.  The service caches every
  unique request, so the steady-state call rate is low.
* **Nominatim public server** — geocoding.  Used **offline** (station import) and
  for the one-time request-time address lookup.  Paced and User-Agent-tagged
  per Nominatim usage policy.

If either service is unreachable the API degrades with explicit codes:
`ROUTING_SERVICE_ERROR`, `GEOCODING_FAILED`, `NO_ROUTE_FOUND`.

---

## 12. Limitations

* **Fixed-route model** — the fuel plan is optimal *for the chosen OSRM route*;
  the route itself is computed by a generic driving-route service, not a fuel-aware
  router.
* **No detour modeling** — stations are represented by their projected route
  position; access/exit detour miles are not modeled (see section 6).
* **Route corridor is geographic** — a station 9 miles from the route as the crow
  flies is included even if the road detour to reach it is long.
* **Coarse USA boundary** — near-coastal and border points may be accepted or
  rejected either way; it rejects *obvious* non-USA inputs, not survey-grade
  borders.
* **Single starting fuel assumption** — the truck starts with a full tank
  (50 gal).
* The public OSRM/Nominatim servers are shared infrastructure — response latency
  depends on them.

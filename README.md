# Production Fuel Route Optimization API

A mathematically rigorous Django & Django REST Framework application that plans cost-effective fuel stops for long-distance driving across the United States.

## Critical Improvements Implemented

1. **Migration to Official HeiGIT API**:
   - Directions endpoint: `https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson`
   - Geocoding endpoint: `https://api.heigit.org/pelias/v1/search`
   - Full migration away from deprecated `api.openrouteservice.org`.
2. **Dynamic Lookahead Optimizer & Invariant Guarantees**:
   - Replaced naive heuristics with dynamic lookahead cost minimization over candidate stations.
   - Enforces vehicle bounds: fuel never drops below 0 and never exceeds tank capacity (50 gal).
   - Invariants: stations strictly visited in route order, all legs fit vehicle range.
   - Unrounded internal Decimal purchase math ensures purchase sums strictly equal total cost.
3. **Route Refinement & Leg Recomputation**:
   - Call 1 gets baseline route.
   - Call 2 routes through waypoints.
   - **Crucial reconciliation**: When waypoint routing returns actual driving leg distances, fuel purchases are recomputed on those legs. Infeasible refined legs trigger a transparent error instead of an invalid itinerary.
4. **Coordinate Provenance & Station Eligibility**:
   - Distinguishes `EXACT` verified station coordinates from `APPROXIMATE` city centroids.
   - Configurable `allow_approximate_stations` parameter (default `true` for demo, configurable to `false` for strict exact-only routing).
5. **Geographic Coordinate Validation**:
   - Replaced loose rectangles with geographic validation enforcing US boundaries, supporting Alaska and Hawaii, and rejecting foreign locations (e.g. Canada/Mexico/Europe).
6. **Safe & Repeatable Ingestion**:
   - Management command `import_fuel_stations` uses bulk updates to update prices without skipping records or manual coordinate fabrication.
7. **Strict HTTP Status Codes**:
   - `400 Bad Request`: Input validation failures or foreign locations.
   - `422 Unprocessable Entity`: Infeasible fuel routes or impossible gaps.
   - `429 Too Many Requests`: Upstream provider rate limits.
   - `502 Bad Gateway`: Upstream HeiGIT connectivity/outage issues.

---

## Quick Setup & Reproduction

### 1. Environment Setup

```bash
# Activate virtual environment
.\venv\Scripts\activate

# Install reproducible pinned dependencies
pip install -r requirements.txt
```

### 2. Environment Variables

Create `.env` based on `.env.example`:
```ini
HEIGIT_API_KEY=your_heigit_api_key_here
DEBUG=False
SECRET_KEY=secure-random-secret-key-change-in-prod
ALLOWED_HOSTS=127.0.0.1,localhost
```

### 3. Run Ingestion & Database Migration

```bash
python manage.py migrate
python manage.py import_fuel_stations
```

### 4. Run Automated Tests

All tests run completely offline with mocked external calls:
```bash
pytest
```

---

## API Documentation

### `POST /api/v1/route-plan/`

#### Request Body
```json
{
  "start": "Los Angeles, CA",
  "finish": "Las Vegas, NV",
  "starting_fuel_gallons": 50.0,
  "max_off_route_distance_miles": 5.0,
  "allow_approximate_stations": true
}
```

#### Response (HTTP 200)
```json
{
  "success": true,
  "start": {
    "label": "Los Angeles, CA, USA",
    "coordinates": [-118.25703, 34.05513]
  },
  "finish": {
    "label": "Las Vegas, NV, USA",
    "coordinates": [-115.148516, 36.167256]
  },
  "route": {
    "distance_miles": 279.92,
    "duration_hours": 4.14,
    "geometry": { "type": "LineString", "coordinates": [...] },
    "legs": []
  },
  "fuel_stops": [],
  "fuel_consumed_gallons": 27.99,
  "fuel_purchased_gallons": 0.0,
  "fuel_purchase_cost_usd": 0.0,
  "total_fuel_cost_usd": 0.0,
  "summary": {
    "number_of_stops": 0,
    "maximum_range_miles": 500.0,
    "fuel_economy_mpg": 10.0,
    "tank_capacity_gallons": 50.0,
    "starting_fuel_gallons": 50.0,
    "routing_provider_calls": 1
  }
}
```

---

## Verification & Test Results

- **Django Check**: `python manage.py check` reports 0 issues.
- **Pytest**: 15 tests passing in 0.64s covering cost minimization, vehicle invariants, unrounded arithmetic, leg refinement recomputation, foreign coordinate rejection, and request call limits.
- **Live Verification**: Successfully verified live route planning on `api.heigit.org` for both short journeys (LA to Vegas, 1 call) and long journeys (LA to Salt Lake City, 2 calls with waypoint refinement).

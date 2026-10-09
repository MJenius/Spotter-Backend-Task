# Production Fuel Route Optimization API

A mathematically grounded, production-hardened Django REST Framework service that plans cost-effective fuel stops for long-distance highway trips across the United States.

---

## 1. Quickstart & Clean-Checkout Guide

### Prerequisites
- Python 3.12, 3.13, or 3.14
- System PROJ libraries (for spatial coordinate projections):
  - Ubuntu/Debian: `sudo apt-get install -y libproj-dev proj-bin proj-data`
  - macOS: `brew install proj`
  - Windows: provided automatically by `pyproj` binary wheels

### Step-by-Step Local Setup

1. **Clone and create a virtual environment**:
   ```bash
   git clone https://github.com/MJenius/Spotter-Backend-Task.git
   cd Spotter-Backend-Task
   python -m venv venv
   # On Windows:
   .\venv\Scripts\activate
   # On Linux / macOS:
   source venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Configure environment variables**:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` to provide a real local secret and your HeiGIT OpenRouteService API key:
   ```env
   SECRET_KEY=django-insecure-your-local-dev-secret-key
   DEBUG=True
   ALLOWED_HOSTS=127.0.0.1,localhost
   HEIGIT_API_KEY=your_heigit_openrouteservice_api_key_here
   ```

4. **Run migrations and import fuel station data**:
   ```bash
   python manage.py migrate
   python manage.py import_fuel_stations
   ```

5. **Run tests offline**:
   ```bash
   pytest -v
   ```

6. **Start the development server**:
   ```bash
   python manage.py runserver 0.0.0.0:8000
   ```
   Open your browser to [http://127.0.0.1:8000/](http://127.0.0.1:8000/) for the interactive Leaflet map interface.

---

## 2. Core Architecture & Mathematical Guarantees

### Exact Dynamic Programming Fuel Optimizer
- Implemented in `fuel_planner/services/optimizer.py`.
- Formulated as an exact Dynamic Programming (DP) state-transition solver over the continuous vehicle refueling problem with finite tank capacity ($C = 50.0$ gal, $mpg = 10.0$):
  - **Critical-State Reduction**: Because fuel cost is piecewise linear and non-decreasing along each edge, any globally optimal plan refuels to reach an intermediate station with tank empty ($f = 0$) or refuels completely to tank capacity ($f = C$). The continuous state space reduces without loss of optimality to discrete critical arrival/departure states at each station candidate.
  - **Access Distance Model**: Station off-route access distances are explicitly computed via spatial projection (`access_miles_one_way`) and added to segment driving distances:
    $$D(u, v) = (p_v - p_u) + a_u + a_v$$
    Access travel is charged at the station rate, preventing distant off-route stations from being chosen over closer alternatives.
  - **Arithmetic Grounding**: State transitions, fuel consumption, and purchase amounts use exact rational arithmetic (`fractions.Fraction`) internally, converted to `Decimal` for financial outputs.
  - **Mathematical Oracle & Theorem**: Verified by an independent test suite (`test_optimizer_bruteforce.py`) across **250 randomized network scenarios**. For integral problem inputs (stations at integer miles, integral tank capacity, integral start fuel), the linear programming constraint matrix is totally unimodular, ensuring the continuous minimum equals the discrete gallon minimum. The oracle exhaustively searches all discrete gallon allocation vectors and verifies exact 100% agreement with the DP solver.

### Strict Coordinate Provenance Policy
- **Primary Data Reality**: The provided `fuel-prices-for-be-assessment.csv` dataset contains highway exit descriptions rather than numbered street addresses (e.g., `I-44, EXIT 283 & US-69`). 7,516 records are enriched with city-level centroid coordinates from the US Census dataset and categorized as `APPROXIMATE`.
- **Strict Default Everywhere**: `allow_approximate_stations` defaults strictly to `False` across serializers, views, services, and the frontend demo.
- If a route exceeds vehicle range and no verified `EXACT` coordinates exist, the API returns a transparent HTTP 422 error detailing that the dataset primarily contains approximate city centroids.
- Opting into approximate stations via `"allow_approximate_stations": true` is explicitly documented for demonstration purposes, with a clear note that individual stations are placed at city centroids.

### Route Refinement & Invariant Reconciliation
- When provisional fuel stops are selected, the planner calls the HeiGIT routing provider (`openrouteservice/v2/directions/driving-car/geojson`) with waypoints.
- `instructions: True` is enabled so the provider returns exact driving leg distances (`segments`).
- **Strict Reconciliation**:
  - If the provider returns a leg count that does not match the expected number of stops plus one, the planner raises an immediate error.
  - If recomputing fuel purchases on actual road leg distances drops or changes any provisional stop, the application rejects the itinerary with an explicit error rather than displaying mismatched route geometry.

### Defensible Geospatial Validation
- Implemented in `fuel_planner/services/geocoding.py` using `data/us_boundary.geojson` (Natural Earth 1:10m sovereign boundary polygons for USA, Canada, and Mexico).
- Authoritative polygon covers checks validate valid continental US, Alaska, and Hawaii locations without relying on coarse bounding boxes.
- Strictly rejects Canadian territory (Windsor ON across from Detroit MI, Fort Erie ON across from Buffalo NY, Vancouver, Toronto) and Mexican territory (Tijuana across from San Diego).
- Fails closed if boundary dataset is unavailable.

### Container & Deployment Architecture
- **Demonstration / Assessment Mode**: The provided `Dockerfile` runs Django's development server (`manage.py runserver 0.0.0.0:8000`) for zero-configuration local review and testing.
- **Production Architecture**: For production deployments, execute behind a production WSGI/ASGI application server (`gunicorn spotter_project.wsgi:application --workers 4 --threads 2 --bind 0.0.0.0:8000`) fronted by a reverse proxy (e.g. Nginx or AWS ALB), with PostgreSQL for relational storage and Redis for distributed caching.

---

## 3. API Usage

### Endpoint: `POST /api/v1/route-plan/`

#### Request (Default Strict Exact-Only)
```json
{
  "start": "Los Angeles, CA",
  "finish": "Las Vegas, NV",
  "starting_fuel_gallons": 50.0,
  "max_off_route_distance_miles": 5.0,
  "allow_approximate_stations": false
}
```

#### Request (Demonstration Mode with Approximate Stations)
```json
{
  "start": "Los Angeles, CA",
  "finish": "Salt Lake City, UT",
  "starting_fuel_gallons": 50.0,
  "max_off_route_distance_miles": 5.0,
  "allow_approximate_stations": true
}
```

#### Response (HTTP 200)
```json
{
  "success": true,
  "start": { "label": "Los Angeles, CA, USA", "coordinates": [-118.25703, 34.05513] },
  "finish": { "label": "Las Vegas, NV, USA", "coordinates": [-115.148516, 36.167256] },
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

## 4. Automated Test Suite

Run tests completely offline:
```bash
pytest -v
```

**Results:**
- **277 passing unit, integration, and brute-force tests** covering:
  - 250 randomized network scenarios mathematically verifying that the exact DP optimizer matches independent combinatorial brute-force solutions under totally unimodular interval constraints.
  - Strict invariants: vehicle fuel levels within $[0, 50]$ gallons at all points, stops strictly visited in route order.
  - Access distance charging and off-route detour evaluation.
  - Unrounded Decimal fuel purchase summation and invariant checks.
  - Refined route leg distance recomputation and sequence reconciliation.
  - Rejection of malformed, mismatched, non-positive, or non-finite refined legs and geometries.
  - Full coordinate bounds validation (finite, physical latitude/longitude) across geocoding, geometry points, and candidate fuel stations.
  - Upstream provider response schema validation and segment-distance summation verification.
  - Collision-free, full-precision SHA256 caching for both geocoding and routing queries.
  - Atomic CSV ingestion inside single database transaction with upfront column validation.
  - Typed exception mapping (`GeocodingRateLimitError`, `RoutingAuthError`, etc.) to accurate HTTP status codes (400, 422, 429, 502).
  - Strict default exclusion of approximate stations in serializers, views, and planner services.
  - Production `SECRET_KEY` validation preventing insecure defaults when `DEBUG=False`.
  - Geospatial validation using official US boundary GeoJSON with high-resolution sovereign boundary checks (USA, Canada, and Mexico) correctly discriminating close border pairs (Detroit vs Windsor, Buffalo vs Fort Erie, San Diego vs Tijuana, Alaska, Hawaii, and ocean coordinates).
  - Call budget limit enforcement ($\le 2$ directions-routing calls).

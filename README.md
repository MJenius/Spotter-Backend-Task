# Production Fuel Route Optimization API

A mathematically justified Django & Django REST Framework application that plans cost-effective fuel stops for long-distance driving across the United States.

---

## 1. Core Architecture & Refinements

### Exact Dynamic Programming Cost-Minimization Algorithm
- Implemented in `fuel_planner/services/optimizer.py`.
- Formulated as an exact Dynamic Programming (DP) state-transition solver over the continuous vehicle refueling problem with finite tank capacity:
  - **Critical-State Reduction**: Because fuel cost is piecewise linear and non-decreasing along each edge, any globally optimal plan refuels to reach an intermediate station with tank empty ($f = 0$) or refuels completely to tank capacity ($f = C$). The continuous state space reduces without loss of optimality to discrete critical arrival/departure states at each station candidate.
  - **Backward DP Transitions**: Evaluates exact minimum total cost to reach the destination from each candidate station under both critical fuel arrival levels, incorporating station off-route access detour distances charged at station rate.
  - **Arithmetic Grounding**: State transitions, fuel consumption, and purchase amounts use exact rational arithmetic (`fractions.Fraction`) internally, converted to `Decimal` for financial outputs.
  - **Empirical Proof**: Verified by an independent test suite (`test_optimizer_bruteforce.py`) that executes **250 randomized network scenarios** comparing DP outputs against an independent exhaustive brute-force search over all combinatorially feasible stop sequences and purchase fractions, proving 100% agreement.
- Guarantees strict invariants:
  - Fuel in tank strictly satisfies $0 \le \text{fuel} \le 50.0$ gallons at all times.
  - Fuel stops are visited strictly in route order.
  - Internal purchase quantities and costs are calculated with unrounded Decimals, ensuring the sum of purchase costs equals the total reported cost.

### Exact vs Approximate Station Eligibility Policy
- **Primary Data Reality**: The provided `fuel-prices-for-be-assessment.csv` dataset contains highway exit descriptions rather than numbered street addresses (e.g., `I-44, EXIT 283 & US-69`). 7,516 records are enriched with city-level centroid coordinates from the US Census dataset.
- **Strict Default Everywhere**: `allow_approximate_stations` defaults strictly to `False` across the request serializer (`RoutePlanRequestSerializer`), route planner (`RoutePlanningService`), API view controller (`RoutePlanAPIView`), and the Leaflet interactive map.
- If a route exceeds vehicle range and no verified `EXACT` coordinates exist, the API returns a transparent HTTP 422 error detailing that the dataset primarily contains approximate city centroids.
- Opting into approximate stations via `"allow_approximate_stations": true` is explicitly documented for demonstration purposes, with a clear note that individual stations are placed at city centroids.

### Route Refinement & Leg Verification
- When fuel stops are selected, the planner calls the HeiGIT routing provider (`openrouteservice/v2/directions/driving-car/geojson`) with waypoints.
- `instructions: True` is enabled so the provider returns exact driving leg distances (`segments`).
- **Strict Verification**: If the provider returns a leg count that does not match the expected number of stops plus one, the planner raises an immediate error. It **never** returns a refined route paired with fuel purchases calculated against a different route.
- Recomputed fuel purchases are validated against the actual refined leg distances.

### Defensible Geospatial Validation with US GeoJSON Boundary
- Implemented in `fuel_planner/services/geocoding.py` using `data/us_boundary.geojson`.
- Directly checks coordinates against the official US multi-polygon boundary using Shapely spatial operations.
- Validates US coordinates against the 49th parallel northern boundary (rejecting Canadian territory like Vancouver, Toronto, and Windsor while accommodating the Minnesota Northwest Angle up to 49.38°N).
- Rejects Mexican border territory south of the official border (e.g. Tijuana at 32.51°N vs San Diego at 32.71°N).
- Supports valid continental US, Alaska (51.0°–71.5°N, -180.0°–-129.0°W), and Hawaii (18.5°–22.5°N, -160.5°–-154.5°W).

---

## 2. API Usage

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

#### Request (With Approximate Stations for Demonstration)
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

## 3. Automated Test Suite

Run tests completely offline:
```bash
pytest
```

**Results:**
- **265 passing unit, integration, and brute-force tests** covering:
  - 250 randomized network scenarios mathematically verifying that the exact DP optimizer matches independent combinatorial brute-force solutions to the penny.
  - Strict invariants: vehicle fuel levels within $[0, 50]$ gallons at all points, stops strictly visited in route order.
  - Exact access distance charging (both off-route access and return distance).
  - Unrounded Decimal fuel purchase summation and invariant checks.
  - Refined route leg distance recomputation.
  - Rejection of malformed or mismatching refined legs.
  - Strict default exclusion of approximate stations in serializers, views, and planner services.
  - Production `SECRET_KEY` validation preventing insecure defaults when `DEBUG=False`.
  - Geospatial validation using official US boundary GeoJSON with high-resolution sovereign boundary checks (USA, Canada, and Mexico) correctly discriminating close border pairs (Detroit vs Windsor, Buffalo vs Fort Erie, San Diego vs Tijuana, Alaska, Hawaii, and overseas).
  - Call budget limit enforcement ($\le 2$ external calls).

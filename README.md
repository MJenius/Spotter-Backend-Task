# Production Fuel Route Optimization API

A mathematically justified Django & Django REST Framework application that plans cost-effective fuel stops for long-distance driving across the United States.

---

## 1. Core Architecture & Refinements

### Continuous Refueling Cost-Minimization Algorithm
- Implemented in `fuel_planner/services/optimizer.py`.
- Formulated based on the classic continuous vehicle refueling model along a line with fixed tank capacity:
  - At station $i$ with price $P_i$, if a cheaper station $j$ exists within the vehicle's full-tank range ($P_j < P_i$), the vehicle purchases only enough fuel to reach $j$ ($0$ fuel upon arrival).
  - If no cheaper station exists within range, and the destination is reachable within range, the vehicle purchases only enough fuel to reach the destination with $0$ fuel upon arrival.
  - If the destination is not reachable, the vehicle fills to maximum capacity (50.0 gal) because station $i$ provides the cheapest rate in the reachable horizon, then advances to the most cost-effective station ahead.
- Guarantees strict invariants:
  - Fuel in tank strictly satisfies $0 \le \text{fuel} \le 50.0$ gallons at all times.
  - Fuel stops are visited strictly in route order.
  - Internal purchase quantities and costs are calculated with unrounded Decimals, ensuring the sum of purchase costs equals the total reported cost.
  - Covered by exhaustive comparisons against alternative feasible plans on small networks.

### Exact vs Approximate Station Eligibility Policy
- **Primary Data Reality**: The provided `fuel-prices-for-be-assessment.csv` dataset contains highway exit descriptions rather than numbered street addresses (e.g., `I-44, EXIT 283 & US-69`). 7,516 records are enriched with city-level centroid coordinates from the US Census dataset.
- **Strict Default**: `allow_approximate_stations` defaults to `False` in the request serializer (`RoutePlanRequestSerializer`), route planner (`RoutePlanningService`), and the Leaflet interactive map.
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
- 13 passing unit and integration tests covering:
  - Exhaustive comparison against alternative feasible plans on small networks.
  - Invariants: vehicle fuel levels within $[0, 50]$ gallons, strictly in route order.
  - Unrounded Decimal fuel purchase summation.
  - Refined route leg distance recomputation.
  - Rejection of malformed or mismatching refined legs.
  - Strict default exclusion of approximate stations.
  - Geospatial validation using official US boundary polygon near international borders (Detroit vs Windsor, Buffalo vs Fort Erie, San Diego vs Tijuana, Alaska, Hawaii, and overseas).
  - Call budget limit enforcement ($\le 2$ external calls).

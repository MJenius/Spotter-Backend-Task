# Fuel Route Optimization API

A production-quality Django & Django REST Framework application that plans cost-effective fuel stops for long-distance driving across the United States.

## Features

- **Optimal Fuel Stop Selection**: Implements greedy lookahead optimization for minimal fuel cost.
- **Strict Vehicle Modeling**: 500-mile range, 50-gallon capacity, 10 MPG fuel economy.
- **Strict 2-Call Routing Budget**: Never queries routing APIs per candidate station; leverages local projected CRS (`EPSG:5070` Conus Albers) spatial indexing.
- **Interactive Leaflet Demo**: Full-featured interactive map with preset routes and live visual results.
- **Robust Geocoding & Validation**: Validates US boundaries and caches results.
- **Full Test Coverage**: 20 automated unit and integration tests passing offline.

---

## Prerequisites

- Python 3.12+ (tested with Python 3.14)
- Git
- Virtualenv

---

## Quick Setup

### 1. Clone & Environment

```bash
git clone <your-repo-url>
cd "Spotter Backend Task"

# Create and activate virtual environment
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
# source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Variables

Create `.env` based on `.env.example`:

```bash
cp .env.example .env
```

Ensure your `HEIGIT_API_KEY` is present in `.env`:
```ini
HEIGIT_API_KEY=your_heigit_api_key_here
DEBUG=True
SECRET_KEY=django-insecure-spotter-fuel-planner
```

### 3. Database Migration & Data Ingestion

```bash
python manage.py migrate
python manage.py import_fuel_stations
```

Output:
```text
=== Ingestion and Geocoding Summary ===
Total records read: 8151
Filtered (Non-US): 620
Invalid prices filtered: 0
Accurately matched: 0
Approximately matched (city-level): 7531
Unresolved: 0
Total stations saved: 7531
```

### 4. Run Automated Tests

All tests run completely offline with zero network or API dependency:

```bash
pytest
```

---

## Running the Application

Start the local Django development server:

```bash
python manage.py runserver
```

- **Interactive Map Demo**: Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your browser.
- **API Endpoint**: `POST http://127.0.0.1:8000/api/v1/route-plan/`

---

## API Specification

### Endpoint: `POST /api/v1/route-plan/`

#### Request Body
```json
{
  "start": "Los Angeles, CA",
  "finish": "Las Vegas, NV",
  "starting_fuel_gallons": 50.0,
  "max_off_route_distance_miles": 5.0
}
```

#### Successful Response (HTTP 200)
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
    "geometry": {
      "type": "LineString",
      "coordinates": [[-118.25703, 34.05513], "..."]
    },
    "legs": []
  },
  "fuel_stops": [],
  "fuel_consumed_gallons": 27.99,
  "fuel_purchased_gallons": 0.0,
  "fuel_purchase_cost_usd": 0.0,
  "total_fuel_cost_usd": 0.0,
  "cost_accounting_note": "total_fuel_cost_usd represents the actual out-of-pocket expenditure...",
  "summary": {
    "number_of_stops": 0,
    "maximum_range_miles": 500.0,
    "fuel_economy_mpg": 10.0,
    "tank_capacity_gallons": 50.0,
    "starting_fuel_gallons": 50.0,
    "routing_provider_calls": 1
  },
  "data_quality": {
    "coordinate_provenance": "us_cities_census_enriched",
    "stations_evaluated_in_corridor": 4
  }
}
```

---

## Postman Collection

Import `postman/Fuel_Route_Optimizer.postman_collection.json` into Postman to test:
1. Short Journey (<500 mi, No Stops) - LA to Las Vegas
2. Medium Journey (>500 mi, Refueling Required) - LA to Salt Lake City
3. Long Multi-Stop Journey (>1000 mi) - New York to Miami
4. Coordinate-based Input - Chicago to St. Louis

---

## Docker Deployment

Build and run using Docker Compose:
```bash
docker-compose up --build
```

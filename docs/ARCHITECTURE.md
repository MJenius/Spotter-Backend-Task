# Architecture and Algorithm Design

## 1. System Overview

The Fuel Route Optimizer solves the problem of finding a cost-optimal sequence of fuel stops for a vehicle driving between two points in the United States.

Key Vehicle Constraints:
- **Maximum tank capacity**: 50 gallons
- **Fuel economy**: 10 miles per gallon (0.10 gallons/mile)
- **Maximum single-tank range**: 500 miles

## 2. Optimization Algorithm

The optimizer is implemented in `fuel_planner/services/optimizer.py` as `FuelRouteOptimizer`. It operates as follows:

1. **Station Ordering and Deduplication**:
   - Stations projected within the route corridor are ordered by mile position along the road.
   - Stations at essentially identical route points are deduplicated in favor of the cheaper station.

2. **Reachability and Lookahead**:
   - At departure, the vehicle has `starting_fuel_gallons` (default: 50.0).
   - If the destination is reachable with current fuel (`fuel >= remaining_distance / 10.0`), the trip completes with 0 additional stops.
   - When a stop is required, the optimizer assesses all stations within reachable range (up to 500 miles ahead):
     - **Cheaper Station Exists Ahead**: The vehicle purchases just enough fuel to safely reach the cheaper station rather than overfilling at a higher price.
     - **No Cheaper Station Exists Ahead**:
       - If destination is within reach of a full tank: buy just enough fuel to reach the destination.
       - Otherwise: fill up completely to 50 gallons because current price is the best available, then advance toward the next station.

3. **Infeasible Gap Detection**:
   - If any consecutive segment between refueling opportunities exceeds 500 miles, an explicit error is returned detailing the gap location.

## 3. Route Provider Call-Budget Architecture

To remain within free-tier API quotas and ensure fast latency:
- **Call 1 (Baseline)**: Start and destination coordinates are sent to HeiGIT / OpenRouteService to obtain the road LineString geometry and baseline distance.
- **Local Spatial Search**: Station coordinates are transformed into Conus Albers equal-area projection (`EPSG:5070`) using Shapely and pyproj. Distance to the route and distance along the route are computed locally without calling the API.
- **Call 2 (Refinement)**: If the optimizer selects fuel stops, a single waypoint route request is made to capture real highway exit detours.
- **Max Call Count**: Never exceeds 2 provider calls during normal execution (and exactly 1 call if no refueling is required).

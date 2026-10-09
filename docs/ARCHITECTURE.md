# Architecture and Algorithm Design

## 1. System Overview

The Fuel Route Optimizer solves the problem of finding a cost-optimal sequence of fuel stops for a vehicle driving between two points in the United States.

Key Vehicle Constraints:
- **Maximum tank capacity ($C$)**: 50.0 gallons
- **Fuel economy ($mpg$)**: 10.0 miles per gallon (0.10 gallons/mile)
- **Maximum single-tank range**: 500.0 miles ($C \times mpg$)

---

## 2. Dynamic Programming Formulation & Optimality Grounding

The optimizer is implemented in `fuel_planner/services/optimizer.py` as an exact, forward Dynamic Programming state-transition solver over critical continuous refueling states.

### 2.1 The Gas Station Problem & Critical States
Following the seminal work by Khuller, Malekian, and Mestre (*"To Fill or not to Fill: The Gas Station Problem"*, ACM TALG 2011 / ESA 2007), for a fixed route with capacity $C$ and piecewise constant linear fuel pricing, the continuous infinity of possible arrival fuel levels collapses without loss of optimality to a finite set of critical states:

1. **Arrive Empty ($f = 0$)**: The vehicle bought just enough fuel at an earlier station to reach the current station.
2. **Arrive Partially Filled ($f = C - D(u, v)/mpg$)**: The vehicle filled its tank to capacity at an earlier station $u$ because $u$ offered a favorable price, consuming $D(u, v)/mpg$ gallons while traveling to $v$.
3. **Arrive from Origin ($f = S - D(\text{start}, v)/mpg$)**: The vehicle drove directly from the origin with initial fuel $S$.

### 2.2 Access Distance Model
For each candidate station $i$, the spatial searcher estimates the one-way perpendicular access distance $a_i$. The effective driving distance between node $u$ and node $v$ along the itinerary is:
$$D(u, v) = (p_v - p_u) + a_u + a_v \quad \text{where } a_{\text{start}} = a_{\text{dest}} = 0$$

Off-route access is charged at the station's fuel rate. If access travel exceeds fuel range, the transition is physically infeasible and rejected.

### 2.3 State Transitions & Rational Arithmetic
- **State Representation**: $(u, f)$ where $u \in \{0, \dots, n+1\}$ is the stop node and $f \in \mathbb{Q}$ is the exact rational fuel remaining upon arrival.
- **Value**: $(\text{cost}, \text{stops}, \text{backpointer})$.
- **Arithmetic**: Transition fuel levels, consumption rates, and purchase quantities are evaluated using exact rational arithmetic (`fractions.Fraction`) to prevent floating-point drift. Final financial costs are converted to `Decimal` rounded to cents ($0.01).
- **Complexity**: For $n$ candidate stations within range, there are $O(n)$ arrival fuel states per node and $O(n)$ outgoing transitions, giving $O(n^2)$ worst-case DP runtime. In practice, transitions are pruned when $(p_v - p_u) > 500$ miles, keeping execution sub-second for corridors with hundreds of stations.

---

## 3. Spatial Modeling & Regional Projections

Fuel stations along the highway corridor are evaluated using region-appropriate metric planar projections:
- **Continental US (CONUS)**: Conus Albers Equal Area (`EPSG:5070`)
- **Alaska**: Alaska Albers (`EPSG:3338`)
- **Hawaii**: Hawaii Albers Equal Area (`EPSG:3759`)

The station searcher computes:
- `access_miles_one_way`: Perpendicular distance from station coordinates to the road LineString.
- `detour_distance_miles`: $2.0 \times \text{access\_miles\_one\_way}$ for round-trip display.
- `route_position_miles`: Scaled cumulative mile along the baseline road geometry.

---

## 4. Route Refinement & Invariant Reconciliation

1. **Baseline Request**: Requests the initial driving LineString and route distance between start and destination coordinates.
2. **Candidate Search & Provisional Solve**: Solves the exact DP across eligible stations using the baseline route distance and access estimates.
3. **Refined Route Request**: If fuel stops are selected, requests a second directions route passing through the exact waypoint coordinates of the provisional stops.
4. **Leg Recomputation**: The provider returns exact driving leg distances for each highway segment. The optimizer re-runs with actual road distances ($a = 0$ as legs already encompass road access).
5. **Strict Reconciliation Guarantee**:
   - If provider leg count does not match $\text{stops} + 1$, the plan fails closed.
   - If actual road legs alter the optimal sequence (e.g. dropping a provisional stop), the application returns an unprocessable error rather than displaying a mismatched road geometry.

---

## 5. API Call Accounting

- **Directions Routing Calls**: Strictly capped at $\le 2$ requests per plan (1 for baseline, 1 for refinement if refueling).
- **Geocoding Calls**: Evaluated and cached independently with a 7-day TTL (`cache.set(..., 86400 * 7)`).
- **Rate Throttling**: Protected with Django REST Framework's AnonRateThrottle (60/min) and UserRateThrottle (120/min).

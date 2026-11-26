Measured over 7 scenarios, each scenario's own seed (`python -m aerolink.eval.ablations`, `backend/eval-results/m5-ablations.json`). Times in seconds; trips = completed flights.

| # | Configuration | Trips | Mean trip | p95 trip | Holding / trip | No route | Suggestions | Bunching | Medical response |
|---|---|---|---|---|---|---|---|---|---|
| B0 | shortest path, no traffic information | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |
| B1 | **classical navigation** (§6), no AI | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |
| A1 | B1 + AI ETA windows (`cv` ETAs in simulation, see PLAN E4.2) | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |
| A2 | B1 + AI ETA windows (`aerolink-gbm`) | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |
| A3 | A2 + demand forecaster | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |
| A4 | A3 + AI off-route detection | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |
| A5 | **full AeroLink** (A4 + risk ranking + en-route suggestions) | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |
| A5@50% | A5, half of the suggestions accepted (sensitivity) | 45 | 1,029 | 1,672 | 0.0 | 1132 | 0 | 8 | 734 |

**B1 vs A5 (as measured):** mean trip time +0 s (+0.0%), holding per trip +0.0 s, bunching events +0.

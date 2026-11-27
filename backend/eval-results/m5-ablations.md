Measured over 10 scenarios, each scenario's own seed (`python -m aerolink.eval.ablations`, `backend/eval-results/m5-ablations.json`). Times in seconds; trips = completed flights. Door-to-door = trip request to landing; ground delay = request to departure (pad-slot holds); ETA error and coverage compare the landing with the prediction made at departure (B1: fixed ±60 s window; A: p10-p90; B0 predicts nothing); the prediction is the ETA plus the waits the advisor predicts, and the cruise-only column scores the ETA alone; suggestion saving is the advisor's predicted saving of the accepted ones.

| # | Configuration | Trips | Mean trip | Door-to-door mean | Door-to-door p95 | Ground delay | Holding / trip | ETA error mean | ETA error p90 | Cruise-only ETA error mean | Window coverage | Suggestions offered / accepted | Predicted saving | No route | Bunching | Medical response |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B0 | shortest path, no traffic information | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | – | – | – | – | 0 / 0 | 0 | 0 | 223 | 717 |
| B1 | **classical navigation** (§6), no AI | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | 104 | 188 | 108 | 39% | 0 / 0 | 0 | 0 | 223 | 717 |
| A1 | B1 + AI ETA windows (`cv` ETAs in simulation, see PLAN E4.2) | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | 105 | 189 | 108 | 41% | 0 / 0 | 0 | 0 | 223 | 717 |
| A2 | B1 + AI ETA windows (`aerolink-gbm`) | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | 61 | 135 | 62 | 84% | 0 / 0 | 0 | 0 | 223 | 717 |
| A3 | A2 + demand forecaster | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | 61 | 135 | 62 | 84% | 0 / 0 | 0 | 0 | 223 | 717 |
| A4 | A3 + AI off-route detection | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | 61 | 135 | 62 | 84% | 0 / 0 | 0 | 0 | 223 | 717 |
| A5 | **full AeroLink** (A4 + risk ranking + en-route suggestions) | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | 61 | 135 | 62 | 84% | 0 / 0 | 0 | 0 | 223 | 717 |
| A5@50% | A5, half of the suggestions accepted (sensitivity) | 161 | 1,187 | 1,210 | 2,056 | 23.6 | 0.0 | 61 | 135 | 62 | 84% | 0 / 0 | 0 | 0 | 223 | 717 |

**B1 vs A5 (as measured):** mean door-to-door time +0 s (+0.0%), holding per trip +0.0 s, bunching events +0.

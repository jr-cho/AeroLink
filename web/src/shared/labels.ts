/**
 * Route-card badges (user decision, 2026-11-26).
 *
 * The server ranks options by lowest *cost* (time + expected wait + a bunching-risk penalty, ARCHITECTURE §5)
 * and labels rank 1 "fastest". A slightly slower but much less congested route can win, so calling it
 * "Fastest" would contradict the times on screen. Instead:
 *   - rank 1 (lowest cost) shows "Recommended";
 *   - the route with the shortest predicted time (cruise + expected wait) shows "Fastest".
 * Usually that's the same route, and the card shows both badges.
 */
import type { RouteOption } from "./types.ts";

export interface RouteBadges {
  recommended: boolean;
  fastest: boolean;
}

/** Predicted door-to-pad time the pilot sees: cruise time plus expected wait. */
export function predictedTime(r: RouteOption): number {
  return r.est_time_s + r.expected_wait_s;
}

/** Badges per `route_id`. Ties on time go to the better-ranked route. */
export function routeBadges(routes: RouteOption[]): Map<string, RouteBadges> {
  const out = new Map<string, RouteBadges>();
  if (!routes.length) return out;
  const fastest = routes.reduce((best, r) =>
    predictedTime(r) < predictedTime(best) || (predictedTime(r) === predictedTime(best) && r.rank < best.rank) ? r : best,
  );
  for (const r of routes) out.set(r.route_id, { recommended: r.rank === 1, fastest: r.route_id === fastest.route_id });
  return out;
}

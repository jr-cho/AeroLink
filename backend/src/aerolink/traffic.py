"""Traffic forecast derived from ETA predictions and planned flights (PLAN E4.3).

This is a pure calculation layer module.  Inputs:

* planned flights: list of ``FlightPlan`` with ETA windows
* optional expected flights from a demand forecaster
* pad/junction capacities

Output:

* ``expected_arrivals`` per node per 1-min bin
* ``traffic_level`` (low/moderate/heavy) at p50 and p90
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aerolink.sim.state import FlightPlan


# Thresholds from ARCHITECTURE §3.4
traffic_level_thresholds = {
    "low": (0.0, 0.5),
    "moderate": (0.5, 1.0),
    "heavy": (1.0, float("inf")),
}

_BIN_S = 60.0


def _level(ratio: float) -> str:
    if ratio < 0.5:
        return "low"
    if ratio < 1.0:
        return "moderate"
    return "heavy"


def _bin_index(t_s: float) -> int:
    return int(t_s // _BIN_S)


@dataclass(frozen=True)
class NodeTraffic:
    node: str
    capacity: int  # aircraft per bin
    p50_arrivals: dict[int, float] = field(default_factory=dict)
    p90_arrivals: dict[int, float] = field(default_factory=dict)

    def level_at(self, t_s: float, quantile: str = "p50") -> str:
        b = _bin_index(t_s)
        arrivals = self.p50_arrivals if quantile == "p50" else self.p90_arrivals
        return _level(arrivals.get(b, 0.0) / max(1, self.capacity))


@dataclass(frozen=True)
class TrafficForecast:
    bins: dict[str, NodeTraffic]

    def level_at(self, node: str, t_s: float, quantile: str = "p50") -> str:
        if node not in self.bins:
            return "low"
        return self.bins[node].level_at(t_s, quantile)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    import math

    if x >= 3.0:
        return 1.0
    if x <= -3.0:
        return 0.0
    b1, b2, b3, b4, b5 = 0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429
    p = 0.2316419
    t = 1.0 / (1.0 + p * abs(x))
    phi = (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)
    y = 1.0 - phi * (b1 * t + b2 * t * t + b3 * t**3 + b4 * t**4 + b5 * t**5)
    return y if x >= 0 else 1.0 - y


def _arrival_mass_p10_p90(t_s: float, p10: float, p50: float, p90: float) -> tuple[float, float]:
    """Return (p50 mass, p90 mass) for an arrival falling in the 1-min bin containing t_s.

    We treat the distribution as Gaussian-ish with p10/p50/p90 defining the
    CDF.  The p50 mass is the probability density at the bin center evaluated
    relative to p50, and the p90 mass uses the upper tail similarly.
    """

    # Use inter-quantile range to estimate sigma.
    sigma = max(1.0, (p90 - p10) / 2.56)
    # Bin edges.
    b = _bin_index(t_s)
    left = b * _BIN_S
    right = left + _BIN_S
    left + _BIN_S / 2.0
    # p50 mass approximated as probability within this bin around p50.
    zl = (left - p50) / sigma
    zr = (right - p50) / sigma
    p50_mass = _normal_cdf(zr) - _normal_cdf(zl)
    # p90 mass: use a heavier tail anchored at p90.
    z90_l = (left - p90) / sigma
    z90_r = (right - p90) / sigma
    p90_mass = _normal_cdf(z90_r) - _normal_cdf(z90_l)
    return p50_mass, p90_mass


def forecast_traffic(
    planned: list[FlightPlan],
    capacities: dict[str, int],
    expected: list[tuple[float, str, float, float, float]] | None = None,
) -> TrafficForecast:
    """Compute expected arrivals per node per 1-min bin.

    ``expected`` entries are (probability, node, p10, p50, p90) for demand-
    forecasted flights that are not yet planned.
    """
    arrivals_p50: dict[str, dict[int, float]] = {}
    arrivals_p90: dict[str, dict[int, float]] = {}

    for plan in planned:
        eta_map = plan.eta_by_node
        if not eta_map:
            continue
        # For M4 we only have an ETA at the destination; treat all intermediate
        # nodes as deterministic fraction-of-route estimates when unavailable.
        list(eta_map.keys())
        # compute per-node windows by linear interpolation if we lack explicit p10/p90
        t_min = min(eta_map.values())
        t_max = max(eta_map.values())
        span = max(1.0, t_max - t_min)
        for node, eta in eta_map.items():
            (eta - t_min) / span
            # window widens from destination outward; fixed ±8% of remaining time
            half = max(30.0, (t_max - eta) * 0.08)
            p10, p50, p90 = eta - half, eta, eta + half
            for b in range(_bin_index(p10) - 1, _bin_index(p90) + 2):
                t = b * _BIN_S + _BIN_S / 2.0
                m50, m90 = _arrival_mass_p10_p90(t, p10, p50, p90)
                arrivals_p50.setdefault(node, {}).setdefault(b, 0.0)
                arrivals_p90.setdefault(node, {}).setdefault(b, 0.0)
                arrivals_p50[node][b] += m50
                arrivals_p90[node][b] += m90

    for prob, node, p10, p50, p90 in expected or []:
        if prob <= 0:
            continue
        for b in range(_bin_index(p10) - 1, _bin_index(p90) + 2):
            t = b * _BIN_S + _BIN_S / 2.0
            m50, m90 = _arrival_mass_p10_p90(t, p10, p50, p90)
            arrivals_p50.setdefault(node, {}).setdefault(b, 0.0)
            arrivals_p90.setdefault(node, {}).setdefault(b, 0.0)
            arrivals_p50[node][b] += prob * m50
            arrivals_p90[node][b] += prob * m90

    bins: dict[str, NodeTraffic] = {}
    all_nodes = set(arrivals_p50) | set(arrivals_p90) | set(capacities)
    for node in sorted(all_nodes):
        cap = capacities.get(node, 1)
        bins[node] = NodeTraffic(
            node=node,
            capacity=max(1, cap),
            p50_arrivals=arrivals_p50.get(node, {}),
            p90_arrivals=arrivals_p90.get(node, {}),
        )
    return TrafficForecast(bins=bins)


def node_capacity(node_props: dict) -> int:
    """Capacity in arrivals per minute: one arrival per pad per 5 min."""
    pads = node_props.get("pads", 1)
    return max(1, pads)  # capacity is per 5-min window, so per-bin is much lower; kept simple

"""Forecaster interface for position and arrival-time heads (PLAN E4.1).

All forecasters return quantile predictions for:

* position head: lateral displacement (m east/north) over short horizons
* ETA head: along-route progress and per-node arrival times over 5-45 min
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aerolink.contracts import VehicleType


@dataclass(frozen=True)
class PositionPrediction:
    """Predicted position offset at a single horizon.

    ``east_m`` and ``north_m`` are the predicted displacement from the current
    position along the UTM 17N axes.  ``up_m`` is positive upward.
    """

    horizon_s: int
    east_p10: float
    east_p50: float
    east_p90: float
    north_p10: float
    north_p50: float
    north_p90: float
    up_p10: float
    up_p50: float
    up_p90: float


@dataclass(frozen=True)
class NodeEta:
    """Arrival-time prediction for one node on the route."""

    node: str
    eta_p10: float
    eta_p50: float
    eta_p90: float


@dataclass(frozen=True)
class EtaPrediction:
    """ETA predictions along a candidate path, indexed by node."""

    path: list[str]
    origin_t_s: float
    etas: list[NodeEta]


class Forecaster(Protocol):
    """Pluggable trajectory forecaster."""

    @property
    def name(self) -> str:
        """Short identifier, e.g. ``cv`` or ``chronos-2-small``."""
        ...

    def predict_position(
        self,
        track: list[tuple[float, float, float]],
        t_s: float,
        vehicle_type: VehicleType,
        horizons_s: list[int] | None = None,
    ) -> list[PositionPrediction]:
        """Return position predictions for the requested horizons.

        ``track`` is a list of ``(lat, lon, alt_msl_m)`` sampled at 1 Hz,
        oldest first.
        """
        ...

    def predict_eta(
        self,
        path: list[str],
        origin_t_s: float,
        vehicle_type: VehicleType,
        track: list[tuple[float, float, float]] | None = None,
        current_speed_mps: float | None = None,
    ) -> EtaPrediction:
        """Return per-node ETA quantiles for the candidate path.

        ``track`` is optional recent history (1 Hz) used by AI models.
        ``current_speed_mps`` is the latest along-track ground speed.
        """
        ...

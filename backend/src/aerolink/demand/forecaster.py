"""Demand forecasters and their baselines (PLAN E5.4, ARCHITECTURE §7.2).

Counts (departures per eVTOL site, dispatches per helipad, per 15 min):

* ``chronos``: Chronos-2, zero-shot, with time of day (sin/cos) as a known covariate
* ``mean_rate``: each site's mean count over the whole history (flat)
* ``last_schedule``: the same 15-min bin one day earlier
* ``constant_rate``: one rate for every site and bin (total history mean)
* ``tod_mean``: each site's mean for that time of day (not in the plan; a stronger,
  still simple reference, so the Chronos result has an honest yardstick)

Destinations of passenger trips: LightGBM multiclass on origin, hour and weekday,
against a uniform choice among the other sites.

``DemandModel`` turns both into *expected flights* for the traffic forecast: for every
site and upcoming bin, probability-weighted arrivals at each destination (ARCHITECTURE §7.3).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import lightgbm as lgb
import numpy as np

from aerolink.demand.synthetic import BIN_S, BINS_PER_DAY

QUANTILES = [0.1, 0.5, 0.9]


def _tod_covariates(start_bin: int, length: int) -> dict[str, np.ndarray]:
    phase = 2 * math.pi * ((np.arange(length) + start_bin) % BINS_PER_DAY) / BINS_PER_DAY
    return {"tod_sin": np.sin(phase), "tod_cos": np.cos(phase)}


def _poisson_band(rate: np.ndarray) -> np.ndarray:
    """(S, H, 3) p10/p50/p90 of a Poisson count with the given mean (normal approximation)."""
    sd = np.sqrt(np.maximum(rate, 1e-9))
    return np.stack([np.maximum(0.0, rate - 1.2816 * sd), rate, rate + 1.2816 * sd], axis=-1)


# Every count forecaster: (history[S, T], horizon H) -> (S, H, 3) p10/p50/p90.
# The history always ends at a day boundary or anywhere: its length T sets the time of day.


def mean_rate(history: np.ndarray, horizon: int) -> np.ndarray:
    return _poisson_band(np.repeat(history.mean(axis=1, keepdims=True), horizon, axis=1))


def constant_rate(history: np.ndarray, horizon: int) -> np.ndarray:
    return _poisson_band(np.full((history.shape[0], horizon), history.mean()))


def last_schedule(history: np.ndarray, horizon: int) -> np.ndarray:
    T = history.shape[1]
    idx = [T - BINS_PER_DAY + (h % BINS_PER_DAY) for h in range(horizon)]
    return _poisson_band(history[:, idx])


def tod_mean(history: np.ndarray, horizon: int) -> np.ndarray:
    S, T = history.shape
    days = T // BINS_PER_DAY
    tail = history[:, T - days * BINS_PER_DAY :].reshape(S, days, BINS_PER_DAY).mean(axis=1)
    idx = [(T + h) % BINS_PER_DAY for h in range(horizon)]
    return _poisson_band(tail[:, idx])


class ChronosCounts:
    """Chronos-2 on count series with time-of-day covariates."""

    def __init__(self, model_name: str = "autogluon/chronos-2-small", context_days: int = 14):
        self.model_name = model_name
        self.context_days = context_days
        self._pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is None:
            from chronos import BaseChronosPipeline

            self._pipeline = BaseChronosPipeline.from_pretrained(self.model_name, device_map="cpu")
        return self._pipeline

    def __call__(self, history: np.ndarray, horizon: int) -> np.ndarray:
        T = history.shape[1]
        ctx = min(T, self.context_days * BINS_PER_DAY)
        start = T - ctx
        past = _tod_covariates(start, ctx)
        future = _tod_covariates(T, horizon)
        inputs = [
            {"target": history[s, start:].astype(np.float32), "past_covariates": past, "future_covariates": future}
            for s in range(history.shape[0])
        ]
        quantiles, _ = self._load().predict_quantiles(inputs, prediction_length=horizon, quantile_levels=QUANTILES)
        out = np.stack([q.detach().cpu().numpy()[0] for q in quantiles])  # (S, H, 3)
        return np.maximum(0.0, np.sort(out, axis=-1))


COUNT_FORECASTERS: dict[str, Callable[[np.ndarray, int], np.ndarray]] = {
    "mean_rate": mean_rate,
    "last_schedule": last_schedule,
    "constant_rate": constant_rate,
    "tod_mean": tod_mean,
}


# --- destinations ---------------------------------------------------------------------------


class DestinationModel:
    """P(destination | origin, hour, weekday) for passenger trips (LightGBM multiclass)."""

    def __init__(self, sites: list[str], seed: int = 42):
        self.sites = sorted(sites)
        self.index = {s: i for i, s in enumerate(self.sites)}
        self.seed = seed
        self.model: lgb.LGBMClassifier | None = None

    def _x(self, origins: list[str], hours: np.ndarray, weekdays: np.ndarray) -> np.ndarray:
        return np.column_stack([[self.index[o] for o in origins], hours, weekdays]).astype(float)

    def fit(self, origins: list[str], hours: np.ndarray, weekdays: np.ndarray, dests: list[str]) -> DestinationModel:
        self.model = lgb.LGBMClassifier(
            n_estimators=200, learning_rate=0.05, num_leaves=15, random_state=self.seed, verbose=-1
        )
        self.model.fit(
            self._x(origins, hours, weekdays), np.array([self.index[d] for d in dests]), categorical_feature=[0]
        )
        return self

    def proba(self, origins: list[str], hours: np.ndarray, weekdays: np.ndarray) -> np.ndarray:
        """(N, n_sites) probabilities; never the origin itself."""
        assert self.model is not None
        raw = self.model.predict_proba(self._x(origins, hours, weekdays))
        p = np.zeros((len(origins), len(self.sites)))
        p[:, np.asarray(self.model.classes_)] = np.asarray(raw)
        for i, o in enumerate(origins):
            p[i, self.index[o]] = 0.0
        return p / np.maximum(1e-12, p.sum(axis=1, keepdims=True))

    def uniform(self, origins: list[str]) -> np.ndarray:
        p = np.ones((len(origins), len(self.sites)))
        for i, o in enumerate(origins):
            p[i, self.index[o]] = 0.0
        return p / p.sum(axis=1, keepdims=True)


# --- expected flights for the route engine -------------------------------------------------------


class DemandModel:
    """Next-day demand forecast, made once and cached, read by the route engine (A3+).

    ``forecast`` maps kind -> (sites, array[site, bin of day, 3]) of p10/p50/p90 counts per
    15 min for the forecast day.
    """

    def __init__(
        self,
        forecast: dict[str, tuple[list[str], np.ndarray]],
        destinations: DestinationModel | None,
        weekday: int,
    ):
        self.forecast = forecast
        self.destinations = destinations
        self.weekday = weekday

    @classmethod
    def from_history(
        cls,
        histories: dict[str, tuple[list[str], np.ndarray]],
        destinations: DestinationModel | None,
        weekday: int,
        count_forecaster: Callable[[np.ndarray, int], np.ndarray] = tod_mean,
    ) -> DemandModel:
        return cls(
            {k: (s, count_forecaster(h, BINS_PER_DAY)) for k, (s, h) in histories.items()}, destinations, weekday
        )

    def expected_flights(
        self,
        local_hour: float,
        t_s: float,
        travel_time_s: Callable[[str, str], float | None],
        horizon_s: float = 1800.0,
        medical_bases: dict[str, str] | None = None,
        min_prob: float = 0.02,
    ) -> list[tuple[float, str, float, float, float]]:
        """(probability, node, eta p10, p50, p90) of flights expected to depart in the next ``horizon_s``.

        ``t_s`` is the caller's clock at ``local_hour``. Passenger flights arrive at their
        likely destinations; medical dispatches arrive at the helipad (from ``medical_bases``,
        the nearest base, when given). ETAs are spread over the departure bin.
        """
        out: list[tuple[float, str, float, float, float]] = []
        first = int((local_hour * 3600.0) // BIN_S)
        n_bins = max(1, math.ceil(horizon_s / BIN_S))
        for kind, (sites, q) in self.forecast.items():
            for k in range(n_bins):
                b = (first + k) % BINS_PER_DAY
                dep_mid = t_s + (b * BIN_S + BIN_S / 2.0 - local_hour * 3600.0) % 86400.0
                for si, site in enumerate(sites):
                    rate = float(q[si, b, 1])
                    if rate < min_prob:
                        continue
                    if kind == "passenger":
                        if self.destinations is not None and site in self.destinations.index:
                            p = self.destinations.proba(
                                [site], np.array([(b + 0.5) * BIN_S / 3600.0]), np.array([self.weekday])
                            )[0]
                            dests = [(self.destinations.sites[j], float(p[j])) for j in np.flatnonzero(p > 0)]
                        else:
                            dests = []
                        for dest, pd in dests:
                            prob = min(1.0, rate * pd)
                            tt = travel_time_s(site, dest) if prob >= min_prob else None
                            if tt is not None:
                                out.append(
                                    (prob, dest, dep_mid - BIN_S / 2 + tt, dep_mid + tt, dep_mid + BIN_S / 2 + tt)
                                )
                    else:
                        base = (medical_bases or {}).get(site)
                        tt = travel_time_s(base, site) if base else 0.0
                        if tt is not None:
                            prob = min(1.0, rate)
                            out.append((prob, site, dep_mid - BIN_S / 2 + tt, dep_mid + tt, dep_mid + BIN_S / 2 + tt))
        return out

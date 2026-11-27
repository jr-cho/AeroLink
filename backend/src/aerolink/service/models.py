"""Model registry: every forecaster, the demand model and the off-route models, each loaded once.

Loading is slow (LightGBM bundles, Chronos weights), so the app runs ``load_all`` in a worker
thread at startup and the planner only ever asks the registry for what is already there.
Asking for a model that is not loaded yet loads it (once, under a lock); switching forecaster
at runtime therefore never reloads weights that are in memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aerolink.forecast.chronos import ChronosForecaster
from aerolink.forecast.cv import CVForecaster
from aerolink.forecast.gbm import GBMForecaster
from aerolink.forecast.interface import Forecaster
from aerolink.network import Network
from aerolink.service.settings import Settings

log = logging.getLogger("aerolink.models")
NAMES = ("cv", "chronos", "aerolink-gbm")
EVAL_FILE = "aerolink-gbm-e5.3.json"
EVAL_KEYS = ("horizon_s", "n", "fde_median_m", "fde_p90_m", "coverage_east", "coverage_north")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ModelRegistry:
    def __init__(self, network: Network, settings: Settings):
        self.network = network
        self.settings = settings
        self._locks = {n: threading.Lock() for n in NAMES}  # one per model: loading one never blocks another
        self._forecasters: dict[str, Forecaster] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self.demand: Any = None
        self.offroute: Any = None

    # --- forecasters ---------------------------------------------------------------------------
    def get(self, name: str) -> Forecaster:
        if name not in NAMES:
            raise ValueError(f"unknown forecaster {name!r}; choose from {list(NAMES)}")
        if name not in self._forecasters:
            with self._locks[name]:
                if name not in self._forecasters:
                    self._load(name)
        return self._forecasters[name]

    def loaded(self, name: str) -> bool:
        return name in self._forecasters

    def _load(self, name: str) -> None:
        t0 = time.perf_counter()
        info: dict[str, Any] = {"name": name, "version": name, "seed": None, "sha256": None, "error": None}
        if name == "cv":
            fc: Forecaster = CVForecaster(self.network)
        elif name == "chronos":
            # Without a track Chronos' ETA head is a constant-speed estimate and needs no weights,
            # so the forecaster is usable at once; ``load_weights`` fetches the model separately.
            fc = ChronosForecaster(self.network)
            info["version"] = fc.model_name
            info["weights_loaded"] = False
        else:
            mdir = self.settings.models_dir / "aerolink-gbm"
            fc = GBMForecaster.load(self.network, mdir)
            try:  # manifest.json is written next to the weights (the one inside the bundle has no hash)
                manifest = json.loads((mdir / "manifest.json").read_text())
            except (OSError, ValueError):
                manifest = getattr(fc, "manifest", {}) or {}
            info["seed"] = manifest.get("seed")
            info["sha256"] = manifest.get("sha256")
            info["sha256_matches_file"] = bool(info["sha256"]) and sha256_of(mdir / "model.joblib") == info["sha256"]
        info["load_time_s"] = round(time.perf_counter() - t0, 3)
        info["loaded_at"] = datetime.now(UTC).isoformat()
        self._forecasters[name] = fc
        self._info[name] = info

    def load_weights(self) -> None:
        """Chronos' pretrained weights (downloaded once, then cached). Slow or failing is not fatal."""
        ch = self.get("chronos")
        t0 = time.perf_counter()
        try:
            cast(Any, ch)._load()  # the Chronos forecaster only
            self._info["chronos"].update(weights_loaded=True, error=None)
        except Exception as e:  # noqa: BLE001 - offline, no cache, version mismatch
            self._info["chronos"]["error"] = f"weights not loaded: {e.__class__.__name__}: {e}"[:200]
            log.warning("chronos weights unavailable: %s", self._info["chronos"]["error"])
        self._info["chronos"]["load_time_s"] = round(self._info["chronos"]["load_time_s"] + time.perf_counter() - t0, 3)

    # --- demand and off-route -------------------------------------------------------------------
    def load_demand(self) -> None:
        try:
            from aerolink.eval.ablations import build_demand  # the same builder the ablations use

            self.demand = build_demand(self.network, datetime.now(UTC).isoformat(), use_chronos=False)
        except Exception as e:  # noqa: BLE001 - history missing: the demand term is simply absent
            log.warning("demand model unavailable: %s", e)

    def load_offroute(self) -> None:
        try:
            from aerolink.offroute.detector import OffRouteModels

            self.offroute = OffRouteModels.load(self.settings.models_dir / "offroute")
        except Exception as e:  # noqa: BLE001
            log.warning("off-route models unavailable: %s", e)

    def load_all(self) -> None:
        """Everything, once. Run in a worker thread. Failures are logged and reported by describe()."""
        for name in NAMES:
            try:
                self.get(name)
            except Exception as e:  # noqa: BLE001
                self._info[name] = {"name": name, "version": name, "error": f"{e.__class__.__name__}: {e}"[:200]}
                log.warning("model %s not loaded: %s", name, e)
        if self.demand is None:
            self.load_demand()
        if self.offroute is None:
            self.load_offroute()
        self.load_weights()  # last: it can be slow (a download) and nothing above waits for it

    # --- /api/models --------------------------------------------------------------------------
    def eval_scores(self, name: str) -> dict[str, Any]:
        path = self.settings.eval_dir / EVAL_FILE
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        out: dict[str, Any] = {}
        for key in ("position_sim_held_out", "position_real_held_out"):
            rows = data.get(key, {}).get(name)
            if rows:
                out[key] = [{k: r[k] for k in EVAL_KEYS if k in r} for r in rows]
        if name in ("cv", "aerolink-gbm") and "eta_sim_held_out" in data:
            prefix = "cv" if name == "cv" else "gbm"
            out["eta_sim_held_out"] = [
                {"horizon_max_min": r["horizon_max_min"], "n": r["n"], "mae_s": r[f"{prefix}_mae_s"],
                 "coverage": r[f"{prefix}_coverage"]}
                for r in data["eta_sim_held_out"]
            ]  # fmt: skip
        return out

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for name in NAMES:
            info = dict(self._info.get(name, {"name": name, "version": name, "error": None}))
            info["loaded"] = name in self._forecasters
            info["eval"] = self.eval_scores(name)
            out.append(info)
        return out

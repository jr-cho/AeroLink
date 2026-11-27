"""Runtime configuration from environment variables (12-factor; see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class Settings:
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = "backend"
    mqtt_password: str = ""
    database_url: str = "sqlite+aiosqlite:///aerolink-dev.db"
    network_file: Path = REPO / "airspace" / "central-fl-v1.geojson"
    graph_version: str = "central-fl-v1"
    constraints_cycle: str = "2026-10-29"
    method: str = "aerolink"  # or "classical"
    forecaster: str = "chronos"  # or "cv"
    traffic_interval_s: float = 5.0
    forecast_interval_s: float = 10.0
    suggestion_interval_s: float = 120.0
    snap_radius_m: float = 10_000.0
    nearby_radius_m: float = 30_000.0
    offroute_interval_s: float = 10.0
    track_ttl_s: float = 60.0  # simulated aircraft silent for longer than this leave the live state
    unit_silence_s: float = 3.0  # a unit silent for longer than this is offline (the Last Will takes ~3 min)
    ws_heartbeat_s: float = 5.0  # paired screens get a heartbeat this often
    health_stale_s: float = 30.0  # a unit's health heartbeat older than this (telemetry still flowing): degraded
    no_fix_degraded_s: float = 10.0  # no GNSS fix for longer than this: degraded
    rssi_degraded_dbm: float = -80.0  # Wi-Fi weaker than this: degraded
    watchdog_interval_s: float = 5.0  # server watchdogs are re-evaluated this often
    reroute_interval_s: float = 10.0  # ATC reroute proposals: conflicts scanned and stale proposals expired this often
    preload_models: bool = False  # the app sets this from PRELOAD_MODELS (default on); unit tests stay light
    scenario: str = "nominal_day"
    models_dir: Path = REPO / "backend" / "models"
    eval_dir: Path = REPO / "backend" / "eval-results"
    scenarios_dir: Path = REPO / "backend" / "scenarios"
    constraints_file: Path = REPO / "airspace" / "constraints-fl.geojson"
    llm_base_url: str = ""
    llm_model: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        """Names follow the repo's .env.example."""
        e = os.environ
        d = cls()
        return cls(
            mqtt_host=e.get("MQTT_HOST", d.mqtt_host),
            mqtt_port=int(e.get("MQTT_PORT", d.mqtt_port)),
            mqtt_user=e.get("MQTT_BACKEND_USERNAME", d.mqtt_user),
            mqtt_password=e.get("MQTT_BACKEND_PASSWORD", d.mqtt_password),
            database_url=e.get("DATABASE_URL", d.database_url),
            network_file=Path(e.get("NETWORK_FILE", str(d.network_file))),
            graph_version=e.get("GRAPH_VERSION", d.graph_version),
            constraints_cycle=e.get("CONSTRAINTS_CYCLE", d.constraints_cycle),
            method=e.get("DEFAULT_METHOD", d.method),
            forecaster=e.get("DEFAULT_FORECASTER", d.forecaster),
            traffic_interval_s=float(e.get("TRAFFIC_PUBLISH_SECONDS", d.traffic_interval_s)),
            forecast_interval_s=float(e.get("FORECAST_REFRESH_SECONDS", d.forecast_interval_s)),
            suggestion_interval_s=float(e.get("SUGGESTION_INTERVAL_SECONDS", d.suggestion_interval_s)),
            offroute_interval_s=float(e.get("OFFROUTE_INTERVAL_SECONDS", d.offroute_interval_s)),
            track_ttl_s=float(e.get("SIM_TRACK_TTL_SECONDS", d.track_ttl_s)),
            unit_silence_s=float(e.get("UNIT_SILENCE_SECONDS", d.unit_silence_s)),
            ws_heartbeat_s=float(e.get("WS_HEARTBEAT_SECONDS", d.ws_heartbeat_s)),
            health_stale_s=float(e.get("HEALTH_STALE_SECONDS", d.health_stale_s)),
            no_fix_degraded_s=float(e.get("NO_FIX_DEGRADED_SECONDS", d.no_fix_degraded_s)),
            rssi_degraded_dbm=float(e.get("RSSI_DEGRADED_DBM", d.rssi_degraded_dbm)),
            watchdog_interval_s=float(e.get("WATCHDOG_INTERVAL_SECONDS", d.watchdog_interval_s)),
            reroute_interval_s=float(e.get("REROUTE_INTERVAL_SECONDS", d.reroute_interval_s)),
            preload_models=e.get("PRELOAD_MODELS", "true").lower() not in ("0", "false", "no"),
            scenario=e.get("SIM_SCENARIO", d.scenario),
            models_dir=Path(e.get("MODELS_DIR", str(d.models_dir))),
            eval_dir=Path(e.get("EVAL_DIR", str(d.eval_dir))),
            scenarios_dir=Path(e.get("SCENARIOS_DIR", str(d.scenarios_dir))),
            constraints_file=Path(e.get("CONSTRAINTS_FILE", str(d.constraints_file))),
            llm_base_url=e.get("LLM_BASE_URL", ""),
            llm_model=e.get("LLM_MODEL", ""),
        )

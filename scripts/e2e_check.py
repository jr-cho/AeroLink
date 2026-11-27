#!/usr/bin/env python3
"""End-to-end checks against the running docker compose stack (PLAN E7, M6 exit).

Called by ``scripts/e2e.sh`` (which brings the stack up, runs the browser tests in ``web/tests/e2e`` and prints the
table). Runs with the backend's Python environment (httpx, aiomqtt) from ``backend/``:

    uv run python ../scripts/e2e_check.py --out .work/e2e-results/<run> pre     # API checks that need nothing special
    uv run python ../scripts/e2e_check.py --out .work/e2e-results/<run> post    # checks that restart / stop services
    uv run python ../scripts/e2e_check.py --out .work/e2e-results/<run> report  # merge with Playwright, print the table

Needs E2E_USER / E2E_PASS (the dashboard login the stack was started with). Secrets from ``.env`` are read into
variables and never printed or written to the evidence files.
"""

# ruff: noqa: E501, E702, B023
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import ssl
import statistics
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
BASE = os.environ.get("E2E_BASE", "https://localhost")
UNIT = "dev-test"
PY = sys.executable
FAKE_UNIT = REPO / "scripts" / "fake_unit.py"
CA = REPO / "mosquitto" / "certs" / "ca.crt"
PATH_ENV = "/usr/local/bin:/opt/homebrew/sbin:/opt/homebrew/bin:" + os.environ.get("PATH", "")
MEDICAL_WINDOW_S = float(os.environ.get("E2E_MEDICAL_WINDOW_S", "600"))

OUT: Path = REPO / ".work" / "e2e-results" / "adhoc"
RESULTS: list[dict[str, Any]] = []


# --- plumbing -----------------------------------------------------------------------------------------------
def env_key(name: str) -> str:
    """One value from .env, for use only (never printed)."""
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise KeyError(name)


def sh(
    cmd: list[str] | str,
    timeout: float = 300,
    env: dict[str, str] | None = None,
    check: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    full = {**os.environ, "PATH": PATH_ENV, **(env or {})}
    p = subprocess.run(
        cmd, shell=isinstance(cmd, str), cwd=cwd or REPO, capture_output=True, text=True, timeout=timeout, env=full
    )
    if check and p.returncode:
        raise RuntimeError(f"{cmd} failed ({p.returncode}): {p.stderr[-500:]}")
    return p


def dc(*args: str, timeout: float = 300, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return sh(["docker", "compose", *args], timeout=timeout, env=env)


def admin() -> httpx.Client:
    """Basic-auth client; also signed in as an ATC operator (G2) when E2E_OPS_USER / E2E_OPS_PASS are set."""
    c = httpx.Client(base_url=BASE, verify=False, auth=(os.environ["E2E_USER"], os.environ["E2E_PASS"]), timeout=60)
    if os.environ.get("E2E_OPS_USER"):
        r = c.post(
            "/api/auth/login", json={"username": os.environ["E2E_OPS_USER"], "password": os.environ["E2E_OPS_PASS"]}
        )
        r.raise_for_status()
        # Secure cookies are not sent back over plain http (tailscale / :80 setups): carry the token by hand
        tok = r.cookies.get("aerolink_ops") or re.search(
            r"aerolink_ops=([^;]+)", r.headers.get("set-cookie", "")
        ).group(1)
        c.headers["Cookie"] = f"aerolink_ops={tok}"
    return c


def anon() -> httpx.Client:
    return httpx.Client(base_url=BASE, verify=False, timeout=60)


def record(cid: str, title: str, status: str, detail: str, evidence: Any = None) -> None:
    RESULTS.append({"id": cid, "title": title, "status": status, "detail": detail})
    if evidence is not None:
        (OUT / "evidence").mkdir(parents=True, exist_ok=True)
        (OUT / "evidence" / f"{cid}.json").write_text(json.dumps(evidence, indent=2, default=str))
    print(f"[{status}] {cid} {title}: {detail}", flush=True)


def check(cid: str, title: str):
    """Run a check function ``fn() -> (status, detail, evidence)``; an exception is a FAIL with the traceback tail."""

    def deco(fn: Callable[[], tuple[str, str, Any]]):
        only = [x for x in os.environ.get("E2E_ONLY", "").split(",") if x]
        if only and cid not in only:
            return fn
        t0 = time.time()
        try:
            status, detail, ev = fn()
        except Exception as e:  # noqa: BLE001
            status, detail, ev = (
                "FAIL",
                f"exception {type(e).__name__}: {e}",
                {"traceback": traceback.format_exc()[-2000:]},
            )
        record(cid, title, status, f"{detail} ({time.time() - t0:.0f}s)", ev)
        return fn

    return deco


def wait_for(pred: Callable[[], Any], timeout: float, every: float = 0.5) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
        time.sleep(every)
    return None


def wait_healthy(timeout: float = 240) -> bool:
    def ok() -> bool:
        with anon() as c:
            return c.get("/healthz").json().get("ok") is True

    return bool(wait_for(ok, timeout, 2))


# --- MQTT observer (the backend account, host loopback) ---------------------------------------------------------
class Observer:
    """Collects every message on ``topics`` with its arrival time, in a background thread."""

    def __init__(self, topics: list[str]) -> None:
        self.topics, self.messages, self._stop = topics, [], threading.Event()
        self.ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self._thread.start()
        self.ready.wait(15)

    async def _run(self) -> None:
        import aiomqtt

        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    "127.0.0.1", 1883, username="backend", password=env_key("MQTT_BACKEND_PASSWORD")
                ) as c:
                    for t in self.topics:
                        await c.subscribe(t)
                    self.ready.set()
                    async for m in c.messages:
                        self.messages.append(
                            {"t": time.time(), "topic": str(m.topic), "payload": bytes(m.payload)[:20000]}
                        )
                        if self._stop.is_set():
                            return
            except Exception:  # noqa: BLE001
                self.ready.set()
                await asyncio.sleep(1)

    def stop(self) -> None:
        self._stop.set()

    def where(self, suffix: str) -> list[dict[str, Any]]:
        return [m for m in list(self.messages) if m["topic"].endswith(suffix)]


# --- fake unit + provisioning -----------------------------------------------------------------------------------
class Unit:
    def __init__(
        self, route: str = "ev-lal,ev-sef", *extra: str, vehicle_type: str = "passenger", track: Path | None = None
    ) -> None:
        args = [
            PY,
            str(FAKE_UNIT),
            "--id",
            UNIT,
            "--host",
            "localhost",
            "--tls",
            "--ca",
            str(CA),
            "--vehicle-type",
            vehicle_type,
        ]
        args += ["--track", str(track)] if track else ["--route", route]
        self.log = OUT / "logs" / f"fake_unit_{int(time.time() * 1000)}.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [*args, *extra],
            cwd=REPO / "backend",
            stdout=self.log.open("w"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "MQTT_PASSWORD": env_key("MQTT_TEST_UNIT_PASSWORD")},
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(5)


def provision(vehicle_type: str = "passenger") -> str:
    """Register (or re-register) the test unit; returns its pairing code. Ends any existing screen session."""
    p = dc(
        "exec",
        "-T",
        "-e",
        "AEROLINK_SITE=https://localhost",
        "backend",
        "python",
        "-m",
        "aerolink.service.devices",
        "add",
        UNIT,
        vehicle_type,
        "--callsign",
        "N999AL",
        "--demo",
        "--out-dir",
        "/tmp/labels",
    )
    m = re.search(r"pairing code \(shown once\): (\S+)", p.stdout)
    if not m:
        raise RuntimeError("provisioning failed: " + p.stdout[-300:] + p.stderr[-300:])
    return m.group(1)


def pair(code: str) -> httpx.Client:
    c = anon()
    r = c.post(f"/api/devices/{UNIT}/pair", json={"code": code})
    r.raise_for_status()
    return c


def device(c: httpx.Client) -> dict[str, Any]:
    return c.get(f"/api/devices/{UNIT}").json()


def active_scenario(a: httpx.Client, name: str) -> None:
    if a.get("/api/scenario").json()["scenario"] != name:
        a.put("/api/scenario", json={"scenario": name}).raise_for_status()


def sites() -> list[str]:
    feats = json.loads((REPO / "airspace" / "central-fl-v1.geojson").read_text())["features"]
    return [f["properties"]["id"] for f in feats if str(f["properties"].get("id", "")).startswith("ev-")]


# --- PRE checks ------------------------------------------------------------------------------------------------
def run_pre() -> None:
    a = admin()

    @check("1a", "clean bring-up: all five services running and healthy")
    def _():
        rows = [json.loads(line) for line in dc("ps", "--format", "json").stdout.splitlines() if line.strip()]
        got = {r["Service"]: (r["State"], r.get("Health", "")) for r in rows}
        want = {"mosquitto", "postgres", "backend", "sim", "caddy"}
        bad = {s: v for s, v in got.items() if v[0] != "running" or v[1] not in ("", "healthy")}
        ok = want <= set(got) and not bad
        return ("PASS" if ok else "FAIL"), f"services {sorted(got)}; not ok: {bad or 'none'}", got

    @check("1b", "migrations at head on Postgres; downgrade 0001 + upgrade head round-trip (scratch database)")
    def _():
        cur = dc("exec", "-T", "backend", "alembic", "current").stdout
        heads = dc("exec", "-T", "backend", "alembic", "heads").stdout
        scheme = dc("exec", "-T", "backend", "sh", "-c", 'echo "$DATABASE_URL" | cut -d: -f1').stdout.strip()
        dc(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "aerolink",
            "-d",
            "aerolink",
            "-c",
            "DROP DATABASE IF EXISTS aerolink_e2e_mig",
        )
        dc(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "aerolink",
            "-d",
            "aerolink",
            "-c",
            "CREATE DATABASE aerolink_e2e_mig",
        )
        # The live database is left alone: a copy of the URL pointing at a scratch database takes the round trip.
        script = (
            'export DATABASE_URL="$(echo "$DATABASE_URL" | sed \'s#/[^/]*$#/aerolink_e2e_mig#\')"; '
            "alembic upgrade head && alembic downgrade 0001 && alembic current && alembic upgrade head && alembic current"
        )
        rt = dc("exec", "-T", "backend", "sh", "-c", script)
        dc(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "aerolink",
            "-d",
            "aerolink",
            "-c",
            "DROP DATABASE IF EXISTS aerolink_e2e_mig",
        )
        head_id = heads.split()[0] if heads.split() else "?"
        ok = (
            "postgresql" in scheme
            and "(head)" in cur
            and rt.returncode == 0
            and rt.stdout.count(head_id) >= 1
            and "0001" in rt.stdout
        )
        return (
            ("PASS" if ok else "FAIL"),
            f"driver {scheme}; live {cur.strip().splitlines()[-1] if cur.strip() else '?'}; round trip rc={rt.returncode}",
            {"current": cur, "round_trip_out": rt.stdout[-800:], "err": rt.stderr[-800:]},
        )

    for port, label, extra in (
        (1883, "plain 1883", {}),
        (8883, "TLS 8883", {"BROKER_HOST": "localhost", "BROKER_PORT": "8883", "BROKER_CA": "mosquitto/certs/ca.crt"}),
    ):

        @check(f"2-{port}", f"broker ACL check, {label}: 9/9")
        def _(extra=extra, port=port):
            p = sh(["scripts/broker-check.sh"], env={"BROKER_PORT": str(port), **extra})
            n = p.stdout.count("PASS ")
            return ("PASS" if n == 9 and "FAIL" not in p.stdout else "FAIL"), f"{n}/9 pass", p.stdout

    @check("3", "auth boundary: open pilot paths 200, protected paths 401 without auth and 200 with")
    def _():
        asset = re.search(r'/assets/[^"\']+\.js', anon().get("/device/x").text)
        open_paths = [
            "/healthz",
            "/device/x",
            "/api/airspace",
            "/ops",
            "/login",
            asset.group(0) if asset else "/assets/none",
        ]
        # /api/ops/* and /api/alerts are exempt from basic auth but need an operator session (G2): 401 without one
        locked = ["/", "/api/state", "/api/devices", "/api/plan", "/api/ops/routes", "/api/alerts", "/api/adsb"]
        res: dict[str, Any] = {"open": {}, "locked_noauth": {}, "locked_auth": {}}
        with anon() as c:
            for p in open_paths:
                res["open"][p] = c.get(p).status_code
            for p in locked:
                res["locked_noauth"][p] = (c.post(p, json={}) if p == "/api/plan" else c.get(p)).status_code
        for p in locked:
            if p == "/api/plan":
                res["locked_auth"][p] = a.post(
                    p, json={"vehicle_type": "passenger", "origin_node": "ev-lal", "destination_node": "ev-orl"}
                ).status_code
            else:
                res["locked_auth"][p] = a.get(p).status_code
        # Caddy forward_auth (G2): an operator cookie alone (no basic auth) reads the shared endpoints; a bad cookie does not;
        # basic auth alone does not open the operator API.
        res["operator_cookie_only"], res["bad_cookie"], res["basic_only_on_operator_api"] = {}, {}, {}
        tok = a.headers.get("Cookie", "")
        if tok:
            with anon() as c:
                for p in ("/api/state", "/api/adsb", "/api/ops/routes"):
                    res["operator_cookie_only"][p] = c.get(p, headers={"Cookie": tok}).status_code
                for p in ("/api/state", "/api/ops/routes"):
                    res["bad_cookie"][p] = c.get(p, headers={"Cookie": "aerolink_ops=forged"}).status_code
            with httpx.Client(
                base_url=BASE, verify=False, auth=(os.environ["E2E_USER"], os.environ["E2E_PASS"]), timeout=60
            ) as c:
                for p in ("/api/ops/routes", "/api/alerts"):
                    res["basic_only_on_operator_api"][p] = c.get(p).status_code
        ok = (
            all(v == 200 for v in res["open"].values())
            and all(v == 401 for v in res["locked_noauth"].values())
            and all(v == 200 for v in res["locked_auth"].values())
            and (
                not tok
                or (
                    all(v == 200 for v in res["operator_cookie_only"].values())
                    and all(v == 401 for v in res["bad_cookie"].values())
                    and all(v == 401 for v in res["basic_only_on_operator_api"].values())
                )
            )
        )
        return ("PASS" if ok else "FAIL"), json.dumps(res), res

    active_scenario(a, "nominal_day")
    time.sleep(20)

    @check("4a", "M6 exit: >= 10 live aircraft in /api/state with nominal_day running")
    def _():
        samples = []
        for _i in range(6):
            st = a.get("/api/state").json()["aircraft"]
            samples.append(
                {
                    "n": len(st),
                    "by_source": {s: sum(1 for x in st if x["source"] == s) for s in {x["source"] for x in st}},
                    "fresh": sum(1 for x in st if not x["stale"]),
                }
            )
            time.sleep(10)
        ok = max(s["n"] for s in samples) >= 10
        return (
            ("PASS" if ok else "FAIL"),
            f"aircraft in /api/state per sample {[s['n'] for s in samples]} ({samples[-1]['by_source']}; parked sim aircraft count as on_ground)",
            samples,
        )

    nodes = sites()
    pairs = [
        ("ev-lal", "ev-orl"),
        ("ev-water-st", "ev-orl"),
        ("ev-sef", "ev-orl"),
        ("ev-lal", "ev-sef"),
        ("ev-orl", "ev-sef"),
        ("ev-water-st", "ev-sef"),
    ]
    pairs = [(o, d) for o, d in pairs if o in nodes and d in nodes] or [(nodes[0], nodes[-1])]
    original_method = a.get("/api/method").json()["method"]
    original_fc = a.get("/api/forecaster").json()
    original_fc.get("forecaster") or original_fc.get("name") or str(original_fc)

    def plan(o: str, d: str, vt: str = "passenger") -> dict[str, Any]:
        return a.post("/api/plan", json={"vehicle_type": vt, "origin_node": o, "destination_node": d}).json()

    @check("5", "M6 exit: switching classical <-> aerolink visibly changes route options (what exactly differs)")
    def _():
        out: dict[str, Any] = {"pairs": [], "forecaster": original_fc}
        for o, d in pairs:
            row: dict[str, Any] = {"pair": f"{o}->{d}"}
            for m in ("classical", "aerolink"):
                a.put("/api/method", json={"method": m}).raise_for_status()
                r = plan(o, d)
                routes = r.get("routes") or []
                top = routes[0] if routes else {}
                eta = top.get("eta") or []
                win = (
                    (
                        datetime.fromisoformat(eta[-1]["eta_p90"].replace("Z", "+00:00"))
                        - datetime.fromisoformat(eta[-1]["eta_p10"].replace("Z", "+00:00"))
                    ).total_seconds()
                    if eta
                    else None
                )
                row[m] = {
                    "status": r.get("status"),
                    "forecaster": r.get("forecaster"),
                    "summary": r.get("summary"),
                    "n_routes": len(routes),
                    "top": {
                        k: top.get(k)
                        for k in (
                            "label",
                            "distance_m",
                            "est_time_s",
                            "expected_wait_s",
                            "bunching_risk",
                            "traffic",
                            "reason",
                        )
                    },
                    "window_p10_p90_s": win,
                    "all_reasons": [x.get("reason") for x in routes],
                    "all_est_time_s": [round(x.get("est_time_s", 0), 1) for x in routes],
                    "eta_nodes": [[e["node"] for e in (x.get("eta") or [])] for x in routes],
                }
            out["pairs"].append(row)
        a.put("/api/method", json={"method": original_method})
        diffs: dict[str, int] = {
            "paths_or_rank": 0,
            "est_time_gt5s": 0,
            "wait_gt5s": 0,
            "window_gt10s": 0,
            "risk_or_traffic": 0,
            "reason_text": 0,
            "summary_text": 0,
        }
        for row in out["pairs"]:
            c, k = row["classical"], row["aerolink"]
            if c["eta_nodes"] != k["eta_nodes"]:
                diffs["paths_or_rank"] += 1
            if (
                c["top"]["est_time_s"] is not None
                and abs((c["top"]["est_time_s"] or 0) - (k["top"]["est_time_s"] or 0)) > 5
            ):
                diffs["est_time_gt5s"] += 1
            if abs((c["top"]["expected_wait_s"] or 0) - (k["top"]["expected_wait_s"] or 0)) > 5:
                diffs["wait_gt5s"] += 1
            if c["window_p10_p90_s"] is not None and abs(c["window_p10_p90_s"] - (k["window_p10_p90_s"] or 0)) > 10:
                diffs["window_gt10s"] += 1
            if (c["top"]["bunching_risk"] != k["top"]["bunching_risk"]) or (c["top"]["traffic"] != k["top"]["traffic"]):
                diffs["risk_or_traffic"] += 1
            if c["all_reasons"] != k["all_reasons"]:
                diffs["reason_text"] += 1
            if c["summary"] != k["summary"]:
                diffs["summary_text"] += 1
        out["difference_counts_over_pairs"] = diffs
        out["n_pairs"] = len(out["pairs"])
        # PASS = a user sees a different value on the options (times, windows, risk/traffic, reasons, summary, ranking).
        visible = any(v for v in diffs.values())
        substantive = any(
            diffs[k] for k in ("paths_or_rank", "est_time_gt5s", "wait_gt5s", "window_gt10s", "risk_or_traffic")
        )
        return (
            ("PASS" if visible else "FAIL"),
            f"{len(pairs)} pairs; pairs showing each difference {diffs}; numeric/ranking difference: {substantive}",
            out,
        )

    for fc in ("default", "aerolink-gbm"):

        @check(f"6-{fc}", f"M6 exit: /api/plan through Caddy (HTTPS, auth) p95 < 2 s, forecaster {fc}, 30+ requests")
        def _(fc=fc):
            if fc != "default":
                a.put("/api/forecaster", json={"forecaster": fc}, timeout=300).raise_for_status()
                wait_for(lambda: a.get("/api/forecaster").json().get("forecaster", fc) == fc, 120)
            a.put("/api/method", json={"method": "aerolink"})
            cur = a.get("/api/forecaster").json()
            times = []
            for i in range(36):
                o, d = pairs[i % len(pairs)]
                t0 = time.perf_counter()
                r = a.post("/api/plan", json={"vehicle_type": "passenger", "origin_node": o, "destination_node": d})
                times.append((time.perf_counter() - t0) * 1000)
                r.raise_for_status()
            times_s = sorted(times)
            p50, p95 = statistics.median(times), times_s[min(len(times_s) - 1, math.ceil(0.95 * len(times_s)) - 1)]
            return (
                ("PASS" if p95 < 2000 else "FAIL"),
                f"forecaster {cur}: n={len(times)} p50 {p50:.0f} ms, p95 {p95:.0f} ms, max {max(times):.0f} ms",
                {"ms": times, "forecaster": cur},
            )

    # back to the stack's default forecaster
    with_name = original_fc.get("forecaster") or original_fc.get("name")
    if with_name:
        a.put("/api/forecaster", json={"forecaster": with_name}, timeout=300)
    a.put("/api/method", json={"method": original_method})

    helper_unit: list[Unit] = []
    if not a.get("/api/ops/routes").json():
        # a lull in the simulator (nothing planned or flying): the test unit plans one flight so there is a route to look at
        hc = pair(provision())
        helper_unit.append(Unit("ev-lal,ev-sef"))
        wait_for(lambda: device(hc).get("online"), 30)
        planned_flight(a, hc)
        time.sleep(3)

    @check("14a", "ops dashboard data (/api/ops/*) with the sim running: routes, conflicts, congestion, alerts")
    def _():
        routes = a.get("/api/ops/routes").json()
        conf = a.get("/api/ops/conflicts").json()
        cong = a.get("/api/ops/congestion").json()
        alerts = a.get("/api/alerts").json()
        flights = a.get("/api/flights", params={"active": 1}).json()
        sources = sorted({r["source"] for r in routes})
        ok = (
            len(routes) >= 1
            and "conflicts" in conf
            and isinstance(cong, dict)
            and isinstance(alerts, list)
            and len(routes) == len({r["flight_id"] for r in routes})
        )
        return (
            ("PASS" if ok else "FAIL"),
            f"{len(routes)} routes (sources {sources}); {len(conf['conflicts'])} conflicts; {len(alerts)} alerts; congestion keys {sorted(cong)[:6]}; active flights {len(flights)}",
            {"routes": len(routes), "conflict_sample": conf["conflicts"][:3], "congestion_keys": sorted(cong)},
        )

    @check(
        "14b",
        "what-if returns alternatives, changes nothing (flight unchanged, no route-response / flight-status published)",
    )
    def _():
        routes = a.get("/api/ops/routes").json()
        pick = next((r for r in routes if r["status"] in ("active", "planned")), None)
        if pick is None:
            return "FAIL", "no flight to test", None
        before = {f["flight_id"]: (f["status"], f["route_id"], f.get("path")) for f in a.get("/api/flights").json()}
        obs = Observer(["aerolink/#"])
        time.sleep(1)
        n0 = len(obs.messages)
        w = a.get(f"/api/ops/whatif/{pick['flight_id']}")
        time.sleep(3)
        obs.stop()
        after = {f["flight_id"]: (f["status"], f["route_id"], f.get("path")) for f in a.get("/api/flights").json()}
        new = [
            m
            for m in obs.messages[n0:]
            if m["topic"].endswith(("/route-response", "/flight-status", "/route-request", "/route-selection"))
        ]
        j = w.json()
        same = all(after.get(k) == v for k, v in before.items())
        ok = w.status_code == 200 and "alternatives" in j and same and not new
        return (
            ("PASS" if ok else "FAIL"),
            f"flight {pick['aircraft_id']} ({pick['status']}): {len(j.get('alternatives', []))} alternatives; flights unchanged {same}; route/status publishes during the call {len(new)}",
            {"whatif": j, "published": [m["topic"] for m in new]},
        )

    for hu in helper_unit:
        hu.stop()

    @check("14c", "alert acknowledge leaves an audit line (acked_by + time) and survives a re-read")
    def _():
        rows = a.get("/api/alerts", params={"unacked": 1}).json()
        if not rows:
            # produce one: a short-lived unit that goes silent -> unit_offline event
            code = provision()
            u = Unit("ev-lal,ev-sef", "--silent-after", "3")
            time.sleep(14)
            u.stop()
            rows = a.get("/api/alerts", params={"unacked": 1}).json()
            _ = code
        if not rows:
            return "FAIL", "no unacknowledged alert available to acknowledge", None
        r = a.post(f"/api/alerts/{rows[0]['id']}/ack")
        again = [x for x in a.get("/api/alerts").json() if x["id"] == rows[0]["id"]]
        ok = (
            r.status_code == 200
            and again
            and again[0]["acked_by"] == (os.environ.get("E2E_OPS_USER") or os.environ["E2E_USER"])
            and again[0]["acked_at"]
        )
        return (
            ("PASS" if ok else "FAIL"),
            f"alert {rows[0]['id']} ({rows[0]['kind']}) -> acked_by {again[0]['acked_by'] if again else None}",
            {"ack": r.json() if r.status_code == 200 else r.text, "reread": again},
        )

    a.close()


# --- POST checks -----------------------------------------------------------------------------------------------
def planned_flight(a: httpx.Client, c: httpx.Client, dest: str = "ev-orl") -> dict[str, Any]:
    """The paired screen's flow over HTTP: route-request then route-selection (-> planned)."""
    rid = str(__import__("uuid").uuid4())
    r = c.post(f"/api/devices/{UNIT}/route-request", json={"destination": dest, "request_id": rid})
    r.raise_for_status()
    resp = r.json()
    route = resp["routes"][0]
    s = c.post(f"/api/devices/{UNIT}/route-selection", json={"request_id": rid, "route_id": route["route_id"]})
    s.raise_for_status()
    return {"request_id": rid, "route_id": route["route_id"], "flight": s.json(), "response": resp}


def run_post() -> None:
    a = admin()
    active_scenario(a, "nominal_day")
    wait_healthy(60)

    @check(
        "11",
        "unit liveness: silent unit -> offline <= 5 s on API + alerts; no-fix -> degraded not offline; resume -> online; health visible",
    )
    def _():
        ev: dict[str, Any] = {}
        code = provision()
        c = pair(code)
        obs = Observer([f"aerolink/aircraft/{UNIT}/telemetry"])
        t_start = datetime.now(UTC)
        u = Unit("ev-lal,ev-sef", "--silent-after", "12")
        online = wait_for(lambda: device(c)["status"] in ("online", "healthy") or device(c).get("online"), 30)
        ev["online_seen"] = bool(online)
        d0 = wait_for(lambda: (lambda d: d if d.get("health") else None)(device(c)), 25)
        ev["health_heartbeat"] = (d0 or {}).get("health")
        ev["health_age_s"] = (d0 or {}).get("health_age_s")
        t_off = wait_for(
            lambda: time.time() if device(c).get("online") is False or device(c)["status"] == "offline" else None,
            40,
            0.25,
        )
        t_last = max((m["t"] for m in obs.messages), default=None)
        lag = (t_off - t_last) if (t_off and t_last) else None
        ev["offline_lag_s"] = lag
        ev["status_after"] = device(c)["status"]
        ev["alerts"] = [
            x
            for x in a.get("/api/alerts", params={"kind": "unit_offline,unit_no_signal"}).json()
            if x.get("aircraft_id") == UNIT and datetime.fromisoformat(x["at"].replace("Z", "+00:00")) > t_start
        ]
        ev["process_still_running"] = (
            u.proc.poll() is None
        )  # silent, never disconnected: only the watchdog can have noticed
        u.stop()
        obs.stop()
        # no fix: degraded, not offline
        u2 = Unit("ev-lal,ev-sef", "--no-fix")
        seen = []
        wait_for(lambda: device(c)["status"] != "offline", 30, 0.5)  # the unit has just (re)connected
        wait_for(lambda: seen.append(device(c)["status"]) or (device(c)["status"] == "degraded"), 30, 1)
        time.sleep(4)
        seen.append(device(c)["status"])
        ev["no_fix_statuses"] = seen
        ev["no_fix_reasons"] = device(c).get("status_reasons")
        u2.stop()
        # resume with a fix
        u3 = Unit("ev-lal,ev-sef")
        back = wait_for(lambda: device(c)["status"] in ("online", "healthy") and device(c)["status"], 30, 1)
        ev["resumed_status"] = back
        u3.stop()
        ok = (
            ev["online_seen"]
            and ev["health_heartbeat"]
            and lag is not None
            and lag <= 5.0
            and ev["alerts"]
            and ev["process_still_running"]
            and "offline" not in seen
            and "degraded" in seen
            and back
        )
        return (
            ("PASS" if ok else "FAIL"),
            (
                f"offline {lag:.1f}s after the last message; offline alert {bool(ev['alerts'])}; no-fix statuses {sorted(set(seen))}; resumed {back}"
                if lag is not None
                else f"no offline seen {ev}"
            ),
            ev,
        )

    @check("9", "restart resilience: restart backend -> planned flight survives; paired screen keeps its session")
    def _():
        code = provision()
        c = pair(code)
        u = Unit("ev-lal,ev-sef")
        wait_for(lambda: device(c).get("online"), 30)
        pf = planned_flight(a, c)
        fid = pf["flight"]["flight_id"]
        before = [f for f in a.get("/api/flights").json() if f["flight_id"] == fid]
        dc("restart", "backend", timeout=180)
        healthy = wait_healthy(240)
        after_dev = device(c) if healthy else {}
        after = [f for f in a.get("/api/flights").json() if f["flight_id"] == fid]
        sess = c.get(f"/api/devices/{UNIT}").status_code
        u.stop()
        ok = (
            healthy
            and sess == 200
            and before
            and after
            and before[0]["flight_id"] == after[0]["flight_id"]
            and after[0]["status"] in ("planned", "active")
        )
        return (
            ("PASS" if ok else "FAIL"),
            f"flight {before[0]['flight_id'][:8] if before else None} status before {before[0]['status'] if before else None}, after {after[0]['status'] if after else None}; session still valid: {sess == 200}; current_flight in device view: {bool(after_dev.get('current_flight'))}",
            {"before": before, "after": after},
        )

    # item 15 needs a drifting unit; build a track along the planned path, then leave the corridor
    @check(
        "15",
        "revisions survive a restart: off-route recalculation published, backend restart, selecting the revision -> accepted",
    )
    def _():
        import importlib.util

        spec = importlib.util.spec_from_file_location("fake_unit", FAKE_UNIT)
        fu = importlib.util.module_from_spec(spec)
        sys.modules["fake_unit"] = fu
        spec.loader.exec_module(fu)  # type: ignore[union-attr]
        code = provision()
        c = pair(code)
        obs = Observer(["aerolink/aircraft/+/route-response", f"aerolink/aircraft/{UNIT}/flight-status"])
        # the path the unit will be offered for lal -> orl (the rank-1 route), so the track can follow it exactly
        offer = a.post(
            "/api/plan", json={"vehicle_type": "passenger", "origin_node": "ev-lal", "destination_node": "ev-orl"}
        ).json()
        path = [e["node"] for e in offer["routes"][0]["eta"]]
        if path[0] != "ev-lal":
            path = ["ev-lal", *path]
        pts = fu.corridor_path(fu.DEFAULT_NETWORK, path)
        # resample at ~40 m/s (one point per second), follow the path 150 s, then drift away ~100 m per second
        track, speed = [], 40.0
        cum = [0.0]
        for p, q in zip(pts, pts[1:], strict=False):
            cum.append(cum[-1] + fu._dist(p, q))

        def at(dm: float) -> tuple[float, float, float]:
            i = max(1, next((k for k, v in enumerate(cum) if v >= dm), len(cum) - 1))
            seg = max(1e-6, cum[i] - cum[i - 1])
            f = min(1.0, max(0.0, (dm - cum[i - 1]) / seg))
            (la, lo), (lb, lb2) = pts[i - 1], pts[i]
            return la + f * (lb - la), lo + f * (lb2 - lo), fu._bearing(pts[i - 1], pts[i])

        drift_lat = 0.0
        for k in range(330):
            lat, lon, brg = at(k * speed)
            if k > 150:
                drift_lat += 0.0009  # ~100 m/s north
            track.append({"lat": lat + drift_lat, "lon": lon, "alt_msl_m": 838.0, "speed_mps": speed})
        tf = OUT / "drift_track.json"
        tf.write_text(json.dumps(track))
        u = Unit(track=tf)
        wait_for(lambda: device(c).get("online"), 30)
        planned_flight(a, c, "ev-orl")
        t0 = time.time()

        def off_route_seen() -> bool:
            return any(json.loads(m["payload"]).get("status") == "off_route" for m in obs.where("/flight-status"))

        wait_for(off_route_seen, 330, 2)
        resp = wait_for(
            lambda: next(
                (
                    m
                    for m in obs.where("/route-response")
                    if f"/{UNIT}/" in m["topic"]
                    and json.loads(m["payload"]).get("revision", 0) >= 1
                    and str(json.loads(m["payload"]).get("summary", "")).startswith("Off route")
                ),
                None,
            ),
            60,
            2,
        )
        ev: dict[str, Any] = {"seconds_to_revision": time.time() - t0, "track_points": len(track), "offered_path": path}
        if not resp:
            ev["statuses"] = [json.loads(m["payload"]).get("status") for m in obs.where("/flight-status")]
            u.stop()
            obs.stop()
            return (
                "FAIL",
                f"no off-route recalculation (revision >= 1, 'Off route:' summary) within the run; flight statuses {ev['statuses']}",
                ev,
            )
        payload = json.loads(resp["payload"])
        ev["revision"] = payload["revision"]
        ev["summary"] = payload.get("summary")
        ev["statuses"] = [json.loads(m["payload"]).get("status") for m in obs.where("/flight-status")]
        rid, route_id = payload["request_id"], payload["routes"][0]["route_id"]
        dc("restart", "backend", timeout=180)
        wait_healthy(240)
        wait_for(lambda: device(c).get("online"), 60)  # the unit keeps publishing and reconnects its link
        c2 = c  # session survives restarts (checked in item 9)
        sel = c2.post(f"/api/devices/{UNIT}/route-selection", json={"request_id": rid, "route_id": route_id})
        ev["select_status"] = sel.status_code
        ev["select_body"] = sel.json() if sel.status_code < 500 else sel.text
        u.stop()
        obs.stop()
        ok = sel.status_code == 200 and sel.json().get("status") in ("planned", "active")
        return (
            ("PASS" if ok else "FAIL"),
            f"revision {payload['revision']} after {ev['seconds_to_revision']:.0f}s of drifting; after backend restart selecting it -> HTTP {sel.status_code} {ev['select_body'].get('status') if isinstance(ev['select_body'], dict) else ''}",
            ev,
        )

    @check(
        "7",
        f"M6 exit: medical_dispatch -> other flights' prediction_history changes + en-route suggestions (window {MEDICAL_WINDOW_S:.0f} s)",
    )
    def _():
        wait_healthy(60)
        obs = Observer(["aerolink/#"])
        a.put("/api/scenario", json={"scenario": "medical_dispatch"}).raise_for_status()
        t0 = time.time()
        hist_seen: dict[str, Any] = {}
        med_alerts: list[Any] = []
        while time.time() - t0 < MEDICAL_WINDOW_S:
            for f in a.get("/api/flights").json():
                for h in f.get("prediction_history", []):
                    if "medical" in str(h.get("reason", "")).lower() and f.get("vehicle_type") != "medical":
                        hist_seen[f"{f['aircraft_id']}|{h.get('t')}"] = {"aircraft": f["aircraft_id"], **h}
            time.sleep(10)
        med_alerts = a.get("/api/alerts", params={"kind": "medical_dispatch"}).json()
        revs = [m for m in obs.where("/route-response") if json.loads(m["payload"]).get("revision", 0) > 0]
        flights = a.get("/api/flights").json()
        n_med = sum(1 for f in flights if f.get("vehicle_type") == "medical")
        obs.stop()
        a.put("/api/scenario", json={"scenario": "nominal_day"})
        sug = [
            {
                "topic": m["topic"],
                "revision": json.loads(m["payload"])["revision"],
                "summary": json.loads(m["payload"]).get("summary"),
            }
            for m in revs
        ]
        ev = {
            "window_s": MEDICAL_WINDOW_S,
            "medical_flights_seen": n_med,
            "prediction_history_entries_caused_by_medical": list(hist_seen.values())[:25],
            "n_history": len(hist_seen),
            "medical_dispatch_alerts": len(med_alerts),
            "revision_gt0_route_responses": sug,
        }
        a_ok, b_ok = len(hist_seen) > 0, len(sug) > 0
        status = "PASS" if (a_ok and b_ok) else "FAIL"
        return (
            status,
            f"(a) {len(hist_seen)} prediction_history entries on non-medical flights caused by a medical flight: {'yes' if a_ok else 'NO'}; (b) en-route suggestions (revision > 0) published: {len(sug)} {'yes' if b_ok else 'NO'}; {n_med} medical flights, {len(med_alerts)} dispatch alerts",
            ev,
        )

    @check(
        "12", "watchdogs: deep health ok; stop sim -> simulator fails <= 30 s, back ok after start; same for mosquitto"
    )
    def _():
        ev: dict[str, Any] = {}

        def deep() -> dict[str, Any]:
            with anon() as c:
                j = c.get("/healthz", params={"deep": 1}).json()
            return {w["name"]: w["state"] for w in j["watchdogs"]} | {"_overall": j["state"], "_mqtt": j["mqtt"]}

        ev["baseline"] = deep()
        dc("stop", "sim", timeout=60)
        t = time.time()  # measured from when the stop returned (docker waits up to 10 s for the process to exit)
        failed = wait_for(lambda: deep().get("simulator") == "fail", 45, 1)
        ev["sim_fail_after_s"] = round(time.time() - t, 1) if failed else None
        ev["during_sim_stop"] = deep()
        dc("start", "sim", timeout=60)
        ok_sim = wait_for(lambda: deep().get("simulator") == "ok", 90, 2)
        ev["sim_ok_after_start"] = bool(ok_sim)
        dc("stop", "mosquitto", timeout=60)
        t = time.time()
        failed_m = wait_for(lambda: deep().get("mqtt") == "fail" or deep().get("_mqtt") is False, 45, 1)
        ev["mqtt_fail_after_s"] = round(time.time() - t, 1) if failed_m else None
        ev["during_mqtt_stop"] = deep()
        dc("start", "mosquitto", timeout=60)
        ok_m = wait_for(lambda: deep().get("mqtt") == "ok" and deep().get("_mqtt") is True, 90, 2)
        ev["mqtt_ok_after_start"] = bool(ok_m)
        ev["final"] = deep()
        events = a.get("/api/alerts", params={"kind": "watchdog"}).json()
        ev["watchdog_events"] = [(x["at"], x["severity"], x["message"]) for x in events[-20:]]
        ev["sim_false_fail_events_before_stop"] = [e for e in ev["watchdog_events"] if "simulator" in e[2].lower()]
        base_ok = all(
            v == "ok" for k, v in ev["baseline"].items() if not k.startswith("_") and k != "adsb"
        )  # adsb: environmental (rate limit)
        ok = base_ok and failed and ok_sim and failed_m and ok_m
        return (
            ("PASS" if ok else "FAIL"),
            f"baseline all ok {base_ok}; sim fail after {ev['sim_fail_after_s']} s, recovered {bool(ok_sim)}; mqtt fail after {ev['mqtt_fail_after_s']} s, recovered {bool(ok_m)}",
            ev,
        )

    wait_healthy(120)
    time.sleep(10)

    @check("13a", "live ADS-B: /api/adsb feed ok with internet, attribution present")
    def _():
        j = a.get("/api/adsb").json()
        feed = j["feed"]
        txt = json.dumps(j)
        attributed = "adsb.lol" in txt or "ODbL" in txt
        env = feed["state"] in ("rate_limited", "down") and (feed.get("budget_remaining") or 0) > 0
        ok = (feed["state"] == "ok" or env) and attributed
        note = (
            "ENVIRONMENTAL PASS-with-note (adsb.lol is rate-limiting this IP, our daily budget is intact); "
            if env
            else ""
        )
        return (
            ("PASS" if ok else "FAIL"),
            f"{note}feed {feed['state']} (budget left {feed.get('budget_remaining')}); {len(j['aircraft'])} aircraft kept (zero is valid at night); attribution: {feed.get('attribution') or j.get('attribution')}",
            {"feed": feed, "n": len(j["aircraft"])},
        )

    @check("13b", "ADSB_URL unreachable -> feed down, nothing else breaks (restore afterwards)")
    def _():
        override = OUT / "adsb-down.override.yaml"
        override.write_text("services:\n  backend:\n    environment:\n      ADSB_URL: http://127.0.0.1:9\n")
        base = ["-f", "compose.yaml", "-f", str(override)]
        ev: dict[str, Any] = {}
        try:
            dc(*base, "up", "-d", "--no-deps", "backend", timeout=300)
            wait_healthy(240)
            j = wait_for(
                lambda: (lambda x: x if x["feed"]["state"] in ("down", "stale") else None)(a.get("/api/adsb").json()),
                60,
                2,
            )
            ev["feed"] = (j or a.get("/api/adsb").json())["feed"]
            time.sleep(25)
            with anon() as c:
                health = c.get("/healthz", params={"deep": 1}).json()
            ev["watchdogs"] = {w["name"]: w["state"] for w in health["watchdogs"]}
            ev["state_ok"] = a.get("/api/state").status_code == 200
            ev["plan_ok"] = (
                a.post(
                    "/api/plan",
                    json={"vehicle_type": "passenger", "origin_node": "ev-lal", "destination_node": "ev-orl"},
                )
                .json()
                .get("status")
            )
            ev["ops_ok"] = a.get("/api/ops/routes").status_code == 200
        finally:
            dc("up", "-d", "--no-deps", "backend", timeout=300)
            wait_healthy(240)

        def restored() -> bool:
            f = a.get("/api/adsb").json()["feed"]
            # "down" with our budget intact and the upstream refusing us is the environment, not the restore
            return f["state"] in ("ok", "rate_limited") or (
                f["state"] == "down"
                and (f.get("budget_remaining") or 0) > 0
                and "429" in str(f.get("error", "")) + "rate"
            )

        back = wait_for(restored, 90, 3)
        ev["restored_ok"] = bool(back)
        ok = ev["feed"]["state"] == "down" and ev["state_ok"] and ev["plan_ok"] == "ok" and ev["ops_ok"] and back
        others = {k: v for k, v in ev["watchdogs"].items() if k != "adsb"}
        return (
            ("PASS" if ok else "FAIL"),
            f"feed {ev['feed']['state']}; state/plan/ops still serve: {ev['state_ok']}/{ev['plan_ok']}/{ev['ops_ok']}; other watchdogs {others}; restored {bool(back)}",
            ev,
        )

    a.close()


# --- backend suite (item 16) --------------------------------------------------------------------------------
def run_suite() -> None:
    @check("16", "full backend suite (host, with the retrained models), incl. the off-route E2 test")
    def _():
        p = sh(
            ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider"],
            timeout=3000,
            env={"OMP_NUM_THREADS": "1", "HF_HUB_OFFLINE": "1"},
            cwd=REPO
            / "backend",  # not the repo root: temp/ there holds QA scratch tests that are not part of the suite
        )
        (OUT / "logs").mkdir(parents=True, exist_ok=True)
        (OUT / "logs" / "pytest-full.log").write_text(p.stdout + p.stderr)
        tail = [line for line in p.stdout.splitlines() if line.strip()][-3:]
        return ("PASS" if p.returncode == 0 else "FAIL"), " | ".join(tail), {"tail": tail}


# --- report ----------------------------------------------------------------------------------------------------
def load_results() -> list[dict[str, Any]]:
    f = OUT / "results.json"
    return json.loads(f.read_text()) if f.exists() else []


def report() -> int:
    rows = load_results()
    pw = OUT / "playwright.json"
    if pw.exists():

        def walk(suite: dict[str, Any], prefix: str = ""):
            for s in suite.get("suites", []):
                yield from walk(s, prefix + (s["title"] + " > " if s.get("title") else ""))
            for spec in suite.get("specs", []):
                res = [t["results"][-1] for t in spec["tests"] if t.get("results")]
                st = "PASS" if res and all(r["status"] == "passed" for r in res) else "FAIL"
                err = ""
                if st == "FAIL" and res:
                    err = re.sub(r"\x1b\[[0-9;]*m", "", (res[-1].get("error", {}) or {}).get("message", ""))[
                        :300
                    ].replace("\n", " ")
                m = re.match(r"\[(\S+?)\]", spec["title"])
                yield {
                    "id": "pw-" + (m.group(1) if m else spec["title"][:6]),
                    "title": "browser: " + spec["title"],
                    "status": st,
                    "detail": err
                    or ("; ".join(a["description"] for t in spec["tests"] for a in t.get("annotations", [])) or "ok"),
                }

        data = json.loads(pw.read_text())
        for s in data.get("suites", []):
            rows += list(walk(s))
    width = max((len(r["id"]) for r in rows), default=4)
    lines = ["| id | result | check | detail |", "|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (re.sub(r"\D", "", r["id"].split("-")[0]) or "99").zfill(3) + r["id"]):
        lines.append(f"| {r['id']} | {r['status']} | {r['title']} | {r['detail'][:400].replace('|', '/')} |")
    n_fail = sum(1 for r in rows if r["status"] != "PASS")
    text = "\n".join(lines) + f"\n\n{len(rows) - n_fail}/{len(rows)} PASS, {n_fail} FAIL\n"
    (OUT / "RESULTS.md").write_text(text)
    print(text)
    _ = width
    return 1 if n_fail else 0


def main() -> int:
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("phase", choices=["pre", "post", "suite", "report"])
    a = ap.parse_args()
    OUT = Path(a.out).resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    ssl._create_default_https_context = ssl._create_unverified_context  # noqa: SLF001

    if a.phase == "report":
        return report()
    RESULTS.extend(load_results())
    {"pre": run_pre, "post": run_post, "suite": run_suite}[a.phase]()
    (OUT / "results.json").write_text(json.dumps(RESULTS, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

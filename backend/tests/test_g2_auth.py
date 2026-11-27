"""G2: ATC operator login, sessions on every ops endpoint, the operators CLI, migration 0005, unit callsigns.

Also exports ``operator_login`` for other test modules whose endpoints now need an operator session.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import app_state, grab, need, portal_of, validator
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aerolink.contracts import Telemetry
from aerolink.service import auth, devices, operators, pairing
from aerolink.service.app import create_app
from aerolink.service.db import operator_sessions
from aerolink.service.devices import (
    callsign_of,
    label_raster,
    provision,
    validate_callsign,
    write_label,
)
from aerolink.service.settings import Settings
from aerolink.service.store import Store

BACKEND = Path(__file__).resolve().parents[1]
CADDYFILE = BACKEND.parent / "caddy" / "Caddyfile"
PASSWORD = "correct horse 1"


def operator_login(c: TestClient, username: str = "op", password: str = PASSWORD) -> str:
    """Create the operator (once) and sign the client in; later requests and WebSockets carry the cookie.

    The cookie is Secure and the test client speaks plain http, so the token is sent as a plain Cookie header.
    """
    try:
        portal_of(c).call(auth.create_operator, app_state(c).service.store, username, password, username.title())
    except ValueError:
        pass  # already exists
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    token = grab(rf"{auth.COOKIE}=([^;]+)", r.headers["set-cookie"])
    c.headers["cookie"] = f"{auth.COOKIE}={token}"
    return token


def make_app(tmp_path: Path, name: str = "g2.db"):
    url = f"sqlite+aiosqlite:///{tmp_path / name}"

    async def prepare() -> None:
        st = Store.connect(url)
        await st.create_all()
        await st.close()

    asyncio.run(prepare())
    return url, create_app(Settings(database_url=url, forecaster="cv", suggestion_interval_s=0), mqtt=False)


@pytest.fixture
def client(tmp_path: Path):
    _, app = make_app(tmp_path)
    with TestClient(app) as c:
        yield c


def raw_login(c: TestClient, username: str, password: str, **kw):
    return c.post("/api/auth/login", json={"username": username, "password": password}, **kw)


def cookie_of(r) -> str:
    return "aerolink_ops=" + grab(r"aerolink_ops=([^;]+)", r.headers["set-cookie"])


def add_operator(c: TestClient, username: str = "alice", password: str = PASSWORD) -> None:
    portal_of(c).call(auth.create_operator, app_state(c).service.store, username, password, username.title())


# --- accounts and passwords ---------------------------------------------------------------------------------
def test_passwords_are_scrypt_hashed_with_a_salt_per_operator(client: TestClient):
    add_operator(client, "alice")
    add_operator(client, "bob")
    store = app_state(client).service.store
    a, b = (portal_of(client).call(store.get_operator, n) for n in ("alice", "bob"))
    assert a["password_hash"] != b["password_hash"] and a["password_salt"] != b["password_salt"]
    assert len(bytes.fromhex(a["password_salt"])) == 16 and PASSWORD not in json.dumps(a, default=str)
    assert auth.verify(PASSWORD, a["password_hash"], a["password_salt"])
    assert not auth.verify(PASSWORD + "x", a["password_hash"], a["password_salt"])
    listed = portal_of(client).call(store.list_operators)
    assert all("password" not in k for o in listed for k in o)  # list hides hashes


def test_username_and_password_rules_and_duplicates(client: TestClient):
    store = app_state(client).service.store
    create = lambda *a: portal_of(client).call(auth.create_operator, store, *a)  # noqa: E731
    assert create("  Carol.Jones ", PASSWORD) == "carol.jones"  # trimmed, lower case
    for bad in ("a", "has space", "x" * 33, "-lead", "ünï"):
        with pytest.raises(ValueError, match="username"):
            create(bad, PASSWORD)
    with pytest.raises(ValueError, match="password"):
        create("dave", "short")
    with pytest.raises(ValueError, match="already exists"):
        create("CAROL.JONES", PASSWORD)


# --- login, me, logout ------------------------------------------------------------------------------------
def test_login_sets_a_strict_http_only_cookie_and_me_logout_work(client: TestClient):
    add_operator(client, "alice")
    assert client.get("/api/auth/me").status_code == 401
    r = raw_login(client, " Alice ", PASSWORD)  # case and spaces forgiven in the username
    assert r.status_code == 200 and r.json()["username"] == "alice" and r.json()["name"] == "Alice"
    cookie = r.headers["set-cookie"].lower()
    assert all(x in cookie for x in ("httponly", "secure", "samesite=strict", "path=/", "max-age=43200"))
    token = grab(r"aerolink_ops=([^;]+)", r.headers["set-cookie"])
    client.headers["cookie"] = f"aerolink_ops={token}"
    me = client.get("/api/auth/me").json()
    assert {k: me[k] for k in ("username", "name")} == {"username": "alice", "name": "Alice"}
    left = (datetime.fromisoformat(me["expires_at"]) - datetime.now(UTC)).total_seconds()
    assert 12 * 3600 - 60 < left <= 12 * 3600
    store = app_state(client).service.store
    assert portal_of(client).call(store.get_operator, "alice")["last_login"] is not None
    assert client.post("/api/auth/logout").json() == {"ok": True}
    assert client.get("/api/auth/me").status_code == 401  # the session is gone server side, not just in the browser
    assert client.post("/api/auth/logout", headers={"cookie": ""}).status_code == 200  # harmless without a session


def test_cookie_is_not_secure_when_the_env_says_so(client: TestClient, monkeypatch):
    add_operator(client, "alice")
    monkeypatch.setenv("AEROLINK_COOKIE_SECURE", "0")
    assert "secure" not in raw_login(client, "alice", PASSWORD).headers["set-cookie"].lower()


def test_every_login_failure_looks_the_same(client: TestClient):
    add_operator(client, "alice")
    add_operator(client, "gone")
    store = app_state(client).service.store
    portal_of(client).call(store.set_operator_disabled, "gone", True)
    answers = [
        raw_login(client, u, p) for u, p in (("alice", "wrong password"), ("nobody", PASSWORD), ("gone", PASSWORD))
    ]
    assert {r.status_code for r in answers} == {401}
    assert len({r.text for r in answers}) == 1 and answers[0].json()["detail"]["error"] == "invalid_login"
    assert all("set-cookie" not in r.headers for r in answers)


def test_wrong_passwords_are_rate_limited_per_username(client: TestClient):
    add_operator(client, "alice")
    add_operator(client, "bob")
    for _ in range(auth.USER_MAX_FAILURES):
        assert raw_login(client, "alice", "wrong password").status_code == 401
    r = raw_login(client, "alice", PASSWORD)  # even the right password waits
    assert r.status_code == 429 and r.json()["detail"]["error"] == "too_many_attempts" and r.headers["retry-after"]
    assert raw_login(client, "bob", PASSWORD).status_code == 200  # other operators are not locked out


def test_wrong_passwords_are_rate_limited_per_address(client: TestClient):
    add_operator(client, "alice")
    ip = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
    for i in range(auth.IP_MAX_FAILURES):  # a different username each time: only the address links them
        assert raw_login(client, f"user{i}", "wrong password", headers=ip).status_code == 401
    assert raw_login(client, "alice", PASSWORD, headers=ip).status_code == 429
    assert raw_login(client, "alice", PASSWORD, headers={"x-forwarded-for": "198.51.100.1"}).status_code == 200


def test_a_good_login_forgets_earlier_typos(client: TestClient):
    add_operator(client, "alice")
    for _ in range(auth.USER_MAX_FAILURES - 1):
        raw_login(client, "alice", "wrong password")
    assert raw_login(client, "alice", PASSWORD).status_code == 200
    for _ in range(auth.USER_MAX_FAILURES - 1):
        assert raw_login(client, "alice", "wrong password").status_code == 401


# --- sessions guard every operator endpoint -----------------------------------------------------------------
GUARDED = [
    ("GET", "/api/ops/routes", None),
    ("GET", "/api/ops/conflicts", None),
    ("GET", "/api/ops/whatif/nothing", None),
    ("GET", "/api/ops/congestion", None),
    ("GET", "/api/alerts", None),
    ("POST", "/api/alerts/1/ack", None),
    ("PUT", "/api/method", {"method": "classical"}),
    ("PUT", "/api/forecaster", {"forecaster": "cv"}),
    ("PUT", "/api/scenario", {"scenario": "nominal_day"}),
]


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_endpoint_needs_an_operator_session(client: TestClient, method: str, path: str, body):
    forged = {"cookie": "aerolink_ops=not-a-real-token", "x-operator": "alice", "authorization": "Basic YWxpY2U6eA=="}
    for headers in ({}, forged):  # basic-auth and X-Operator no longer identify anyone
        r = client.request(method, path, json=body, headers=headers)
        assert r.status_code == 401 and r.json()["detail"]["error"] == "session_required", (path, r.text)
    operator_login(client)
    assert client.request(method, path, json=body).status_code != 401


def test_ops_websocket_refuses_a_visitor_without_a_session(client: TestClient):
    for headers in ({}, {"cookie": "aerolink_ops=forged"}):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect("/ws/ops", headers=headers):
                pass
        assert e.value.code == 4401
    operator_login(client)
    with client.websocket_connect("/ws/ops") as ws:
        assert ws.receive_json()["type"] == "state"


def test_public_reads_stay_open_to_the_web_map(client: TestClient):
    for path in ("/healthz", "/api/state", "/api/method", "/api/forecaster", "/api/scenario", "/api/traffic"):
        assert client.get(path).status_code == 200, path


def expire(c: TestClient, token: str) -> None:
    async def go() -> None:
        async with app_state(c).service.store.engine.begin() as con:
            await con.execute(
                sa.update(operator_sessions)
                .where(operator_sessions.c.token_hash == pairing.token_hash(token))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

    portal_of(c).call(go)


def test_an_expired_session_is_refused_everywhere(client: TestClient):
    token = operator_login(client)
    assert client.get("/api/ops/routes").status_code == 200
    expire(client, token)
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/ops/routes").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/ops"):
            pass


def test_sessions_last_twelve_hours_and_survive_a_restart(tmp_path: Path):
    url, app = make_app(tmp_path)
    with TestClient(app) as c:
        token = operator_login(c)
        row = portal_of(c).call(app_state(c).service.store.operator_for_session, pairing.token_hash(token))
        assert row["username"] == "op"
    assert auth.SESSION_TTL_S == 12 * 3600
    _, again = make_app(tmp_path, "g2.db")  # the same database file: a restarted backend
    with TestClient(again, headers={"cookie": f"aerolink_ops={token}"}) as c2:
        assert c2.get("/api/auth/me").json()["username"] == "op"


def test_disabling_or_changing_the_password_ends_sessions(client: TestClient):
    store = app_state(client).service.store
    operator_login(client, "alice")
    assert client.get("/api/auth/me").status_code == 200
    h, salt = auth.make_hash("another long password")
    assert portal_of(client).call(store.set_operator_password, "alice", h, salt) is True
    assert client.get("/api/auth/me").status_code == 401  # signed out by the password change
    assert (
        raw_login(client, "alice", PASSWORD).status_code == 401
        and raw_login(client, "alice", "another long password").status_code == 200
    )
    client.headers["cookie"] = cookie_of(raw_login(client, "alice", "another long password"))
    assert client.get("/api/auth/me").status_code == 200
    portal_of(client).call(store.set_operator_disabled, "alice", True)
    assert client.get("/api/auth/me").status_code == 401
    assert raw_login(client, "alice", "another long password").status_code == 401  # disabled: same generic answer


# --- the ack records the session user -----------------------------------------------------------------------
def test_ack_records_the_logged_in_operator_and_ignores_headers(client: TestClient):
    svc = app_state(client).service

    async def record():
        return await svc.events.record("watchdog", severity="warn", message="test alert")

    ev = portal_of(client).call(record)
    operator_login(client, "alice")
    assert client.post("/api/alerts/99999/ack").status_code == 404
    r = client.post(f"/api/alerts/{ev['id']}/ack", headers={"X-Operator": "mallory"})
    assert r.status_code == 200 and r.json()["acked_by"] == "alice"
    operator_login(client, "bob")
    again = client.post(f"/api/alerts/{ev['id']}/ack").json()
    assert again["acked_by"] == "alice"  # the first acknowledgement stays
    log = client.get("/api/alerts", params={"kind": "alert_ack"}).json()
    assert len(log) == 1 and log[0]["detail"]["operator"] == "alice"


# --- the operators CLI --------------------------------------------------------------------------------------
def run_cli(monkeypatch, tmp_path: Path, *argv: str, stdin: str = "") -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    operators.main(list(argv))


def test_operators_cli_add_passwd_disable_enable_list(tmp_path: Path, monkeypatch, capsys):
    async def prepare() -> None:
        st = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
        await st.create_all()
        await st.close()

    asyncio.run(prepare())
    run_cli(monkeypatch, tmp_path, "add", "Alice", "--name", "Alice Kim", "--password-stdin", stdin=PASSWORD + "\n")
    assert "alice created" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="already exists"):
        run_cli(monkeypatch, tmp_path, "add", "alice", "--password-stdin", stdin=PASSWORD)
    with pytest.raises(SystemExit, match="password must be"):
        run_cli(monkeypatch, tmp_path, "add", "bob", "--password-stdin", stdin="short\n")
    run_cli(monkeypatch, tmp_path, "list")
    out = capsys.readouterr().out
    assert "alice" in out and "Alice Kim" in out and "active" in out and "never" in out and PASSWORD not in out
    run_cli(monkeypatch, tmp_path, "passwd", "alice", "--password-stdin", stdin="a brand new password\n")
    run_cli(monkeypatch, tmp_path, "disable", "alice")
    run_cli(monkeypatch, tmp_path, "list")
    assert "disabled" in capsys.readouterr().out
    run_cli(monkeypatch, tmp_path, "enable", "alice")

    async def check() -> None:
        st = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
        row = need(await st.get_operator("alice"))
        assert not row["disabled"] and auth.verify("a brand new password", row["password_hash"], row["password_salt"])
        await st.close()

    asyncio.run(check())
    with pytest.raises(SystemExit, match="no operator"):
        run_cli(monkeypatch, tmp_path, "disable", "ghost")


def test_operators_cli_prompts_twice_without_echo(tmp_path: Path, monkeypatch):
    asked: list[str] = []
    answers = iter([PASSWORD, "different"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": asked.append(prompt) or next(answers))
    with pytest.raises(SystemExit, match="differ"):
        operators.read_password(False)
    assert asked == ["Password: ", "Repeat password: "]
    assert set(inspect_argv(operators)) == {"--name", "--password-stdin"}  # there is no --password VALUE option


def inspect_argv(mod) -> list[str]:
    import inspect

    return re.findall(r'add_argument\("(--[a-z-]+)"', inspect.getsource(mod))


# --- migration 0005 -----------------------------------------------------------------------------------------
def test_migration_0005_up_and_down_on_sqlite(tmp_path: Path):
    db = tmp_path / "m.db"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")

    def state() -> tuple[set[str], set[str]]:
        con = sqlite3.connect(db)
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        cols = {r[1] for r in con.execute("pragma table_info(devices)")}
        con.close()
        return tables, cols

    command.upgrade(cfg, "0004")
    con = sqlite3.connect(db)
    con.execute(
        "insert into devices (id, vehicle_type, demo, registered_at, online) "
        "values ('dev-old','passenger',0,'2026-01-01',0)"
    )
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    tables, cols = state()
    assert {"operators", "operator_sessions"} <= tables and "callsign" in cols
    con = sqlite3.connect(db)  # rows registered before G2 survive with no callsign
    assert con.execute("select callsign from devices where id='dev-old'").fetchone() == (None,)
    con.close()
    command.downgrade(cfg, "0004")
    tables, cols = state()
    assert not ({"operators", "operator_sessions"} & tables) and "callsign" not in cols and "events" in tables
    command.upgrade(cfg, "head")
    assert "operators" in state()[0]


# --- callsigns ----------------------------------------------------------------------------------------------
def test_callsign_validation():
    assert validate_callsign(" n123al ") == "N123AL"
    assert validate_callsign("AL-1") == "AL-1"
    for bad in ("", "A", "ABCDEFGHIJK", "N 123", "N123_AL", "ÅL123"):
        with pytest.raises(ValueError, match="callsign"):
            validate_callsign(bad)


def test_callsign_of_prefers_the_registry_then_derives_from_sim_ids():
    reg = {"dev-001": "N123AL", "dev-002": None}
    assert callsign_of("dev-001", "aircraft", reg) == "N123AL"
    assert callsign_of("dev-002", "aircraft", reg) == "dev-002"  # a unit registered before G2: the id
    assert callsign_of("sim-p03", "sim", reg) == "SIM-P03" and callsign_of("sim-m00", "sim", {}) == "SIM-M00"


def test_provision_requires_unique_valid_callsigns_and_prints_them_on_the_label(tmp_path: Path):
    async def go() -> None:
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
        await store.create_all()
        r = await provision(store, "dev-001", "passenger", False, "https://x.test", tmp_path / "lbl", "n123al")
        assert r["callsign"] == "N123AL"
        assert need(await store.get_device("dev-001"))["callsign"] == "N123AL"
        with pytest.raises(ValueError, match="already belongs"):
            await provision(store, "dev-002", "medical", False, "https://x.test", tmp_path / "lbl", "N123AL")
        await provision(
            store, "dev-001", "passenger", False, "https://x.test", tmp_path / "lbl", "N123AL"
        )  # same unit: ok
        with pytest.raises(ValueError, match="callsign must be"):
            await provision(store, "dev-003", "medical", False, "https://x.test", tmp_path / "lbl", "bad callsign!")
        assert await store.set_callsign("dev-001", "N999AL") and not await store.set_callsign("nope", "N1")
        assert [d["callsign"] for d in await store.list_devices()] == ["N999AL"]
        await store.close()

    asyncio.run(go())


def test_devices_cli_add_is_callsign_required_and_set_callsign_lists(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")

    async def prepare() -> None:
        st = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
        await st.create_all()
        await st.close()

    asyncio.run(prepare())
    with pytest.raises(SystemExit):  # argparse: --callsign is required
        devices.main(["add", "dev-001", "passenger", "--site", "https://x.test", "--out-dir", str(tmp_path)])
    capsys.readouterr()
    devices.main(
        ["add", "dev-001", "medical", "--callsign", "med-7", "--site", "https://x.test", "--out-dir", str(tmp_path)]
    )
    assert "(MED-7)" in capsys.readouterr().out and (tmp_path / "dev-001.png").exists()
    devices.main(["set-callsign", "dev-001", "n55al"])
    devices.main(["list"])
    assert "N55AL" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="not registered"):
        devices.main(["set-callsign", "nobody-1", "N1AL"])


def png_pixels(data: bytes) -> tuple[int, int, list[bytes]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    i, idat = 8, b""
    while i < len(data):
        n = int.from_bytes(data[i : i + 4], "big")
        if data[i + 4 : i + 8] == b"IDAT":
            idat += data[i + 8 : i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    return w, h, [raw[r * (w + 1) + 1 : (r + 1) * (w + 1)] for r in range(h)]


def test_label_prints_the_callsign_under_the_qr_code(tmp_path: Path):
    url = "https://x.test/device/dev-001?code=ABCDEFGHJK"
    plain, labelled = label_raster(url, None), label_raster(url, "N123AL")
    assert len(labelled) > len(plain) and len(labelled[0]) == len(plain[0])
    assert labelled[: len(plain)] == plain  # the QR code itself is untouched
    below = labelled[len(plain) - 5 :]
    assert any(any(r) for r in below) and not any(any(r) for r in plain[-30:])  # ink under the code, none before
    assert label_raster(url, "SIM-P03")[-1] == bytearray(len(plain[0]))  # longest names still fit the width
    svg, png = write_label(url, "dev-001", tmp_path, "N123AL")
    w, h, rows = png_pixels(png.read_bytes())
    assert (w, h) == (len(labelled[0]), len(labelled)) and set(b for r in rows for b in r) == {0, 255}
    text = svg.read_text()
    assert text.startswith("<svg") and "<path" in text and ET_ok(text)


def ET_ok(text: str) -> bool:
    import xml.etree.ElementTree as ET

    ET.fromstring(text)
    return True


# --- callsign in telemetry and the API ----------------------------------------------------------------------
def tel(aircraft_id: str, callsign: str | None, version: str = "4.1") -> dict:
    d = {
        "schema_version": version,
        "message_id": str(uuid4()),
        "aircraft_id": aircraft_id,
        "vehicle_type": "passenger",
        "timestamp": datetime.now(UTC).isoformat(),
        "position": {"lat": 28.5, "lon": -81.3, "alt_msl_m": 30.0},
        "motion": {"ground_speed_mps": 0.0, "course_deg": 0.0, "vertical_speed_mps": 0.0},
        "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": False},
        "active_flight_id": None,
    }
    if callsign is not None:
        d["callsign"] = callsign
    return d


def test_telemetry_contract_has_an_optional_callsign_in_schema_and_model():
    v = validator("telemetry")
    ok = [
        tel("dev-001", "N123AL"),
        tel("dev-001", None, "4.0"),
        tel("dev-001", None, "4.1"),
        tel("dev-001", "AL-01", "4.0"),
    ]
    for d in ok:
        assert not list(v.iter_errors(d))
        Telemetry.model_validate_json(json.dumps(d))
    for bad in ("n123al", "A", "ABCDEFGHIJK", "N 123", "", 12):
        d = tel("dev-001", None)
        d["callsign"] = bad
        assert list(v.iter_errors(d)), bad
        with pytest.raises(ValueError):
            Telemetry.model_validate_json(json.dumps(d))
    assert list(v.iter_errors(tel("dev-001", None, "4.2")))


def test_registry_callsign_wins_and_a_mismatch_is_logged_once(tmp_path: Path, fl_network):
    from test_service import Clock, make_service

    async def go() -> None:
        s = await make_service(tmp_path, fl_network, Clock())
        await provision(s.store, "dev-001", "passenger", False, "https://x.test", tmp_path / "l", "N123AL")
        await s.store.update_device("dev-001", online=True)
        for _ in range(3):  # a unit repeating a wrong callsign every second
            cast(Any, s.clock).t += timedelta(seconds=1)
            m = tel("dev-001", "N999ZZ")
            m["timestamp"] = s.clock().isoformat()
            await s.handle("aerolink/aircraft/dev-001/telemetry", json.dumps(m).encode())
        assert s.stats["telemetry_ok"] == 3 and "dev-001" in s.live.tracks  # still accepted
        rows = await s.store.events(kinds=["callsign_mismatch"])
        assert len(rows) == 1 and rows[0]["severity"] == "warn"
        assert rows[0]["detail"] == {"registry": "N123AL", "reported": "N999ZZ"}
        assert need(await s.store.get_device("dev-001"))["callsign"] == "N123AL"  # the registry was not overwritten
        m = tel("dev-001", "N123AL")  # the right callsign: no event
        m["timestamp"] = (s.clock() + timedelta(seconds=5)).isoformat()
        await s.handle("aerolink/aircraft/dev-001/telemetry", json.dumps(m).encode())
        assert len(await s.store.events(kinds=["callsign_mismatch"])) == 1
        await s.store.close()

    asyncio.run(go())


def test_callsign_is_exposed_on_state_devices_device_and_ops_routes(tmp_path: Path, fl_network):
    from test_e610_ops import row  # a flight row builder
    from test_service import node, telemetry

    url, app = make_app(tmp_path)
    asyncio.run(_provision(url, tmp_path))
    with TestClient(app, base_url="https://testserver") as c:
        svc = app_state(c).service
        lat, lon = node(fl_network, "ev-sef")
        now = datetime.now(UTC)
        portal_of(c).call(svc.handle, "aerolink/aircraft/dev-001/status", b"online")
        portal_of(c).call(svc.handle, "aerolink/aircraft/dev-001/telemetry", telemetry("dev-001", lat, lon, now))
        portal_of(c).call(svc.handle, "aerolink/aircraft/dev-002/telemetry", telemetry("dev-002", lat, lon, now))
        portal_of(c).call(svc.handle, "aerolink/sim/sim-p03/telemetry", telemetry("sim-p03", lat, lon, now))
        by_id = {a["id"]: a["callsign"] for a in c.get("/api/state").json()["aircraft"]}
        assert by_id == {"dev-001": "N123AL", "dev-002": "dev-002", "sim-p03": "SIM-P03"}
        devs = {d["id"]: d["callsign"] for d in c.get("/api/devices").json()}
        assert devs == {"dev-001": "N123AL", "dev-002": None}
        # pilot screen: the paired unit's own callsign, nothing editable
        r = c.post("/api/devices/dev-001/pair", json={"code": CODES["dev-001"]})
        assert r.status_code == 200
        c.headers["cookie"] = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
        assert c.get("/api/devices/dev-001").json()["callsign"] == "N123AL"
        p = fl_network.shortest_paths("ev-sef", "ev-orl", vehicle_type="passenger", k=1)[0]
        svc.flights["dev-001"] = row(fl_network, "dev-001", p, time.time())
        svc.flights["sim-p03"] = row(fl_network, "sim-p03", p, time.time(), source="sim")
        operator_login(c)
        routes = {x["aircraft_id"]: x["callsign"] for x in c.get("/api/ops/routes").json()}
        assert routes == {"dev-001": "N123AL", "sim-p03": "SIM-P03"}


CODES: dict[str, str] = {}


async def _provision(url: str, tmp_path: Path) -> None:
    st = Store.connect(url)
    CODES["dev-001"] = (await provision(st, "dev-001", "passenger", False, "https://x.test", tmp_path / "l", "N123AL"))[
        "code"
    ]
    await provision(st, "dev-002", "passenger", False, "https://x.test", tmp_path / "l")  # registered before G2
    await st.close()


# --- Caddy --------------------------------------------------------------------------------------------------
def test_caddyfile_exempts_the_operator_paths_and_gates_the_rest_with_the_session():
    text = CADDYFILE.read_text()
    lists = re.findall(r"^\t\tnot path (.+)$", text, re.M)
    assert len(lists) == 2 and lists[0] == lists[1]  # @session and @basic use the same exempt list
    exempt = set(lists[0].split())
    assert {"/login*", "/ops*", "/api/auth/*", "/api/ops/*", "/api/alerts*", "/ws/ops"} <= exempt
    assert {"/device/*", "/api/devices/*", "/ws/devices/*", "/healthz", "/api/airspace"} <= exempt
    assert not {"/", "/api/*", "/api/devices", "/api/state", "/api/method"} & exempt  # the map keeps its gate
    assert "forward_auth @session backend:8000" in text and "uri /api/auth/me" in text
    assert re.search(r"basic_auth @basic", text) and "route {" in text

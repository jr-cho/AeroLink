"""U1 story 5: live ADS-B in the pilot's traffic (U-API §4): within 10 NM, advisory, altitude relative to the unit."""

from __future__ import annotations

import json
from types import SimpleNamespace

from aerolink.service.ai import ADSB_NEAR_M, nearby_adsb, unit_adsb
from aerolink.service.hub import Hub

LAT, LON = 28.0, -81.5


def ac(icao: str, north_nm: float, alt_ft: float | None = 2500, seen_s: float = 2.0, **extra) -> dict:
    return {
        "icao": icao,
        "callsign": extra.get("callsign", icao.upper()),
        "lat": LAT + north_nm * 1852.0 / 111_320.0,
        "lon": LON,
        "alt_ft": alt_ft,
        "gs_kt": 120.0,
        "track_deg": 180.0,
        "seen_s": seen_s,
        "source": "adsb",
    }


def test_only_aircraft_within_ten_nautical_miles_are_listed_nearest_first():
    rows = nearby_adsb(
        [ac("far", 12.0), ac("mid", 6.0), ac("near", 2.0)], LAT, LON, unit_alt_m=2500 * 0.3048
    )  # fmt: skip
    assert [r["icao"] for r in rows] == ["near", "mid"]
    assert ADSB_NEAR_M == 10 * 1852.0


def test_stale_or_positionless_aircraft_are_not_shown():
    rows = nearby_adsb(
        [ac("stale", 1.0, seen_s=90.0), {**ac("nopos", 1.0), "lat": None}, ac("ok", 1.0)], LAT, LON, 0.0
    )  # fmt: skip
    assert [r["icao"] for r in rows] == ["ok"]


def test_relative_altitude_is_against_the_unit_and_null_without_an_altitude():
    unit_m = 1000 * 0.3048  # the unit at 1000 ft MSL
    rows = nearby_adsb([ac("above", 1.0, alt_ft=1500), ac("ground", 2.0, alt_ft=None)], LAT, LON, unit_m)
    by = {r["icao"]: r for r in rows}
    assert by["above"]["rel_alt_ft"] == 500
    assert by["ground"]["rel_alt_ft"] is None
    assert set(by["above"]) == {"icao", "callsign", "lat", "lon", "alt_ft", "gs_kt", "track_deg", "rel_alt_ft"}


def test_a_unit_without_a_feed_or_a_fix_gets_an_empty_list():
    track = SimpleNamespace(last=SimpleNamespace(position=SimpleNamespace(lat=LAT, lon=LON, alt_msl_m=0.0)))
    service = SimpleNamespace(live=SimpleNamespace(tracks={"dev-001": track}))
    feed = SimpleNamespace(snapshot=lambda: {"aircraft": [ac("a", 1.0)]})
    assert [r["icao"] for r in unit_adsb(SimpleNamespace(adsb=feed, service=service), "dev-001")] == ["a"]
    assert unit_adsb(SimpleNamespace(adsb=None, service=service), "dev-001") == []
    assert unit_adsb(SimpleNamespace(adsb=feed, service=service), "dev-999") == []  # no fix for this unit


def test_the_screens_traffic_message_carries_adsb_and_mqtt_does_not():
    hub = Hub()
    hub.adsb_source = lambda aid: {"adsb": [{"icao": "abc123"}], "adsb_state": "ok"}
    q = hub.subscribe("dev-001")
    traffic = json.dumps({"type": "traffic", "aircraft": []}).encode()
    hub.publish_messages([("aerolink/aircraft/dev-001/traffic", traffic)])
    msg = q.get_nowait()
    assert msg["type"] == "traffic" and msg["payload"]["adsb"] == [{"icao": "abc123"}]
    assert msg["payload"]["adsb_state"] == "ok"  # the screen can say "unavailable" when the feed is not ok

    hub.publish_messages([("aerolink/aircraft/dev-001/route-response", b'{"routes": []}')])
    assert "adsb" not in q.get_nowait()["payload"]  # only traffic gains the field
    assert json.loads(traffic) == {"type": "traffic", "aircraft": []}  # the MQTT payload itself is untouched


# --- C-03: pilot screens count as viewers; the screens are told the feed's state -------------------------------
from aerolink.adsb_live import AdsbFeed, AdsbSettings  # noqa: E402
from aerolink.service.ai import feed_state, pilot_screen_flying, unit_adsb_view  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _flight(aid: str, status: str) -> dict:
    return {"flight_id": f"f-{aid}", "aircraft_id": aid, "status": status}


def test_a_connected_pilot_screen_with_a_flight_counts_as_a_viewer():
    hub = Hub()
    service = SimpleNamespace(flights={"dev-001": _flight("dev-001", "active")})
    assert not pilot_screen_flying(service, hub)  # no screen connected
    q = hub.subscribe("dev-001")
    assert pilot_screen_flying(service, hub)  # connected and flying
    service.flights["dev-001"]["status"] = "ended"
    assert not pilot_screen_flying(service, hub)  # the flight is over: idle cadence again
    service.flights["dev-001"]["status"] = "planned"
    assert pilot_screen_flying(service, hub)  # planned counts too
    hub.unsubscribe("dev-001", q)
    assert not pilot_screen_flying(service, hub)  # the screen went away


def test_feed_polls_at_the_active_rate_when_a_pilot_screen_is_viewing_and_idle_otherwise():
    clock = FakeClock()
    feed = AdsbFeed(AdsbSettings(), clock=clock)
    hub = Hub()
    service = SimpleNamespace(flights={"dev-001": _flight("dev-001", "active")})
    ops_viewer = feed.viewers  # None: no ops socket in this test
    feed.viewers = lambda: bool(ops_viewer and ops_viewer()) or pilot_screen_flying(service, hub)
    assert not feed.viewed()
    assert feed.next_delay() == feed.settings.poll_idle_s  # nobody looking: 5 min
    hub.subscribe("dev-001")
    assert feed.viewed()
    assert feed.next_delay() == feed.settings.poll_active_s  # a connected pilot screen: 30 s


def test_the_feed_state_travels_with_the_nearby_list():
    assert feed_state(SimpleNamespace(adsb=None)) == "disabled"  # no feed in this process
    assert feed_state(SimpleNamespace(adsb=AdsbFeed(AdsbSettings(enabled=False)))) == "disabled"
    assert feed_state(SimpleNamespace(adsb=AdsbFeed(AdsbSettings(enabled=True), clock=FakeClock()))) == "down"
    track = SimpleNamespace(last=SimpleNamespace(position=SimpleNamespace(lat=LAT, lon=LON, alt_msl_m=0.0)))
    service = SimpleNamespace(live=SimpleNamespace(tracks={"dev-001": track}))
    view = unit_adsb_view(SimpleNamespace(adsb=None, service=service), "dev-001")
    assert view == {"adsb": [], "adsb_state": "disabled"}

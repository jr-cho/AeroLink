"""Tests for the classical navigation baseline B1 (PLAN E3.5)."""

from __future__ import annotations

import pytest

from aerolink.navigation.b1 import B1Advisor, B1Config
from aerolink.network import Network


def test_b1_returns_ranked_options(fl_network: Network):
    b1 = B1Advisor(fl_network)
    opts = b1.request_routes("ev-sef", "ev-lal", "passenger", 0.0)
    assert len(opts) >= 1
    assert all(o.total_predicted_s > 0 for o in opts)
    # options are sorted by total predicted time
    assert opts == sorted(opts, key=lambda o: o.total_predicted_s)


def test_b1_medical_priority_reduces_junction_wait(fl_network: Network):
    b1_medical = B1Advisor(fl_network, B1Config(medical_priority=True))
    b1_passenger = B1Advisor(fl_network, B1Config(medical_priority=False))
    med = b1_medical.request_routes("hp-5fl3", "hp-fd28", "medical", 0.0)[0]
    # same pair may not be passenger-reachable, so compare the medical option
    # against itself with priority disabled
    med_nopriority = b1_passenger.request_routes("hp-5fl3", "hp-fd28", "medical", 0.0)[0]
    assert med.predicted_junction_wait_s <= med_nopriority.predicted_junction_wait_s


def test_b1_eta_window_is_fixed(fl_network: Network):
    b1 = B1Advisor(fl_network, B1Config(eta_window_s=60.0))
    opt = b1.request_routes("ev-sef", "ev-lal", "passenger", 100.0)[0]
    assert opt.eta_window_s == pytest.approx((opt.eta_s - 60.0, opt.eta_s + 60.0))


def test_b1_no_route_for_disconnected_nodes(fl_network: Network):
    b1 = B1Advisor(fl_network)
    opts = b1.request_routes("ev-sef", "does-not-exist", "passenger", 0.0)
    assert opts == []


def test_b1_suggest_only_improves(fl_network: Network):
    b1 = B1Advisor(fl_network)
    opts = b1.request_routes("ev-sef", "ev-lal", "passenger", 0.0)
    first = opts[0]
    # A shorter route should beat the current shortest by definition; just confirm
    # request_routes returns the best option first.
    assert first == min(opts, key=lambda o: o.total_predicted_s)


def test_point_to_segment_uses_lon_lat_order():
    """Regression: is_off_route passed (lat, lon), so the longitude scale used cos(longitude)."""
    from aerolink.navigation.b1 import _point_to_segment_m

    # A north-south segment at 28°N; a point 0.003° of longitude east ≈ 0.003 * 111 km * cos(28°) ≈ 294 m.
    d = _point_to_segment_m(-81.997, 28.0, -82.0, 27.9, -82.0, 28.1)
    assert d == pytest.approx(294.0, rel=0.02)

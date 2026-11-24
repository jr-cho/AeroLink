"""Physical pad model (PLAN E3.2, ARCHITECTURE §9.1, hardening plan §7a).

A pad slot is held by an aircraft from the moment it is cleared to land until it
moves to a stand (``PAD_CLEAR_S`` after touchdown, if a stand is free) or, with
no stand, until it departs. An aircraft that asks for a pad when none is free
joins the site's FIFO queue and holds short of the pad; slots are handed to the
queue head in order as they free up. Taking off from a stand needs a pad slot
that no queued arrival is waiting for.

Stands default to the site's pad count for eVTOL vertiports and 1 for helipads;
``stands: 0`` reproduces the pad-only model.
"""

from __future__ import annotations

from aerolink.network import Network
from aerolink.sim.state import PadState

HOLD_DISTANCE_M = 500.0  # hold point, measured back from the pad along the route
PAD_REQUEST_DISTANCE_M = 4000.0  # remaining route at which an aircraft asks for a pad
LANDING_DURATION_S = 10.0
DEFAULT_TURNAROUND_S = 300.0  # ARCHITECTURE §3.4
PAD_CLEAR_S = 60.0  # time on the pad after touchdown before moving to a stand


def _sort_bookings(pad: PadState) -> None:
    pad.bookings.sort(key=lambda b: (b[0], b[1]))


def _overlap(t0: float, t1: float, s0: float, s1: float) -> bool:
    return t0 < s1 and s0 < t1


def _capacity_at(pad: PadState, t: float, exclude_id: str | None = None) -> int:
    """Free slots at instant ``t`` excluding a booking owned by ``exclude_id``."""
    used = sum(1 for start, end, owner in pad.bookings if owner != exclude_id and start <= t < end)
    return pad.capacity - used


def is_slot_open(pad: PadState, t_start: float, t_end: float, exclude_id: str | None = None) -> bool:
    """Whether ``[t_start, t_end)`` fits within capacity at every instant."""
    # Capacity is piecewise constant at booking boundaries; sample just inside
    # each segment plus the start.
    checkpoints = {t_start}
    for start, end, owner in pad.bookings:
        if owner == exclude_id:
            continue
        if _overlap(t_start, t_end, start, end):
            if start > t_start:
                checkpoints.add(start)
            if end < t_end:
                checkpoints.add(end)
    for t in checkpoints:
        if _capacity_at(pad, t, exclude_id=exclude_id) <= 0:
            return False
    return True


def find_slot(
    pad: PadState,
    t_requested: float,
    duration_s: float,
    tolerance_s: float,
    aircraft_id: str,
) -> tuple[float, float] | None:
    """Find a contiguous ``duration_s`` window within ``tolerance_s`` of
    ``t_requested`` that fits in the pad calendar.

    Search strategy: prefer the earliest window that starts no earlier than
    ``t_requested`` and no later than ``t_requested + tolerance_s``. If none,
    look backwards up to ``t_requested - tolerance_s``.
    """
    if duration_s <= 0:
        return None
    # Forward search.
    t = t_requested
    end = t_requested + tolerance_s + 1e-9
    while t <= end:
        if is_slot_open(pad, t, t + duration_s, exclude_id=aircraft_id):
            return (t, t + duration_s)
        # Jump to the end of the first conflicting booking that starts after t.
        nxt = t + duration_s
        for start, bend, owner in pad.bookings:
            if owner == aircraft_id:
                continue
            if start <= t < bend:
                nxt = max(nxt, bend)
            elif t < start < nxt:
                nxt = start
        # Avoid an infinite loop when nxt == t due to a tiny booking.
        if nxt <= t + 1e-9:
            nxt = t + 1.0
        t = nxt
    # Backward search.
    t = t_requested - duration_s
    start_earliest = t_requested - tolerance_s - duration_s - 1e-9
    while t >= start_earliest:
        if is_slot_open(pad, t, t + duration_s, exclude_id=aircraft_id):
            return (t, t + duration_s)
        # Jump backward over the booking ending just before t+duration_s.
        prev = t - 1.0
        for start, bend, owner in pad.bookings:
            if owner == aircraft_id:
                continue
            if start < t + duration_s <= bend:
                prev = min(prev, start - duration_s)
            elif t + duration_s > bend > t:
                prev = min(prev, bend - duration_s)
        if prev >= t:
            break
        t = prev
    return None


def book_slot(pad: PadState, t_start: float, t_end: float, aircraft_id: str) -> bool:
    """Add a booking if the slot is open. Returns success."""
    if not is_slot_open(pad, t_start, t_end, exclude_id=aircraft_id):
        return False
    pad.bookings.append((t_start, t_end, aircraft_id))
    _sort_bookings(pad)
    return True


def cancel_booking(pad: PadState, aircraft_id: str) -> None:
    pad.bookings = [b for b in pad.bookings if b[2] != aircraft_id]


def next_available_start(
    pad: PadState, t_after: float, duration_s: float, aircraft_id: str | None = None
) -> float | None:
    """Earliest time at or after ``t_after`` that can hold ``duration_s``."""
    t = t_after
    max_search = t_after + 7200.0  # safety bound
    while t <= max_search:
        if is_slot_open(pad, t, t + duration_s, exclude_id=aircraft_id):
            return t
        nxt = t + duration_s
        for start, bend, owner in pad.bookings:
            if owner == aircraft_id:
                continue
            if start <= t < bend:
                nxt = max(nxt, bend)
            elif t < start < nxt:
                nxt = start
        if nxt <= t + 1e-9:
            nxt = t + 1.0
        t = nxt
    return None


def default_stands(props: dict) -> int:
    if "helipad" in props.get("types", []):
        return 1
    return int(props.get("pads", 1))


def init_pads(
    network: Network,
    capacity_overrides: dict[str, int] | None = None,
    stand_overrides: dict[str, int] | None = None,
    default_stand_count: bool = False,
) -> dict[str, PadState]:
    """One ``PadState`` per landing site (waypoints excluded), sorted by node id.

    Stands come from ``stand_overrides``, else ``default_stands`` if
    ``default_stand_count``, else 0 (pad-only model).
    """
    overrides = capacity_overrides or {}
    stand_over = stand_overrides or {}
    unknown = set(overrides) - set(network.nodes)
    if unknown:
        raise ValueError(f"pad capacity overrides for unknown nodes: {sorted(unknown)}")
    unknown = set(stand_over) - set(network.nodes)
    if unknown:
        raise ValueError(f"stand capacity overrides for unknown nodes: {sorted(unknown)}")
    pads: dict[str, PadState] = {}
    for nid in sorted(network.nodes):
        props = network.nodes[nid]
        if "waypoint" in props.get("types", []):
            continue
        capacity = int(overrides.get(nid, props.get("pads", 1)))
        if capacity < 1:
            raise ValueError(f"pad {nid} must have capacity >= 1, got {capacity}")
        stands = int(stand_over.get(nid, default_stands(props) if default_stand_count else 0))
        if stands < 0:
            raise ValueError(f"site {nid} must have stands >= 0, got {stands}")
        pads[nid] = PadState(node_id=nid, capacity=capacity, stands=stands)
    return pads


def occupy(pad: PadState, aircraft_id: str) -> None:
    if aircraft_id in pad.occupants:
        return
    if not pad.has_free_slot():
        raise RuntimeError(f"pad {pad.node_id} is full; {aircraft_id} cannot occupy it")
    pad.occupants.append(aircraft_id)


def release(pad: PadState, aircraft_id: str) -> None:
    if aircraft_id in pad.occupants:
        pad.occupants.remove(aircraft_id)


def has_parking(pad: PadState) -> bool:
    return pad.has_free_stand() or pad.has_free_slot()


def park(pad: PadState, aircraft_id: str) -> bool:
    """Place a ground aircraft on a free stand, else on a pad slot. Returns
    ``on_stand``. Raises if the site is full."""
    if pad.has_free_stand():
        pad.stand_occupants.append(aircraft_id)
        return True
    occupy(pad, aircraft_id)
    return False


def move_to_stand(pad: PadState, aircraft_id: str) -> None:
    if not pad.has_free_stand():
        raise RuntimeError(f"no free stand at {pad.node_id} for {aircraft_id}")
    release(pad, aircraft_id)
    pad.stand_occupants.append(aircraft_id)


def leave_stand(pad: PadState, aircraft_id: str) -> None:
    if aircraft_id in pad.stand_occupants:
        pad.stand_occupants.remove(aircraft_id)


def request(pad: PadState, aircraft_id: str) -> bool:
    """Ask for a slot. Grants it immediately only if a slot is free *and* nobody is
    already queued (first come, first served); otherwise queues the aircraft."""
    if aircraft_id in pad.occupants:
        return True
    if pad.has_free_slot() and not pad.queue:
        pad.occupants.append(aircraft_id)
        return True
    if aircraft_id not in pad.queue:
        pad.queue.append(aircraft_id)
    return False


def leave_queue(pad: PadState, aircraft_id: str) -> None:
    if aircraft_id in pad.queue:
        pad.queue.remove(aircraft_id)


def grant_from_queue(pad: PadState) -> list[str]:
    """Hand free slots to queued aircraft in FIFO order; return those cleared."""
    cleared: list[str] = []
    while pad.queue and pad.has_free_slot():
        aid = pad.queue.pop(0)
        pad.occupants.append(aid)
        cleared.append(aid)
    return cleared

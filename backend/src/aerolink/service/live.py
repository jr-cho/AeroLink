"""In-memory live state: latest fix and a few minutes of 1 Hz history per aircraft, deduplication, ordering."""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime

from aerolink.contracts import Telemetry
from aerolink.service.topics import Source

HISTORY_S = 180  # off-route detection needs ~2 min: 60 s of context for the 30 s band forecast + 3 checks 10 s apart
MAX_HDOP = 5.0
GROUND_SPEED_MPS = 1.0  # a unit reporting less than this, not climbing or sinking, is sitting on its pad
GROUND_VSPEED_MPS = 0.5
SEGMENT_GAP_S = 30.0  # a telemetry gap longer than this starts a new track segment
MIN_SAMPLES_AFTER_BREAK = 5  # off-route is not judged until a segment that follows a break has this many samples
_R_EARTH_M = 6_371_000.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _R_EARTH_M * math.asin(math.sqrt(a))


@dataclass
class Track:
    aircraft_id: str
    source: Source
    last: Telemetry
    history: deque[tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=HISTORY_S))
    times: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY_S))  # unix seconds, parallel to history
    online: bool = True
    off_route: bool = False
    on_ground: bool = False  # parked on a pad (set by the service, which knows the flight status)
    segment_id: int = 0  # unix ms of the current segment's first sample
    segment_samples: int = 0  # accepted samples in the current segment
    after_break: bool = False  # the segment began after a reconnect, gap or fix recovery (not at first sight)

    @staticmethod
    def looks_grounded(msg: Telemetry) -> bool:
        """Motion alone: stationary and level. A hovering aircraft looks the same, so the flight status decides too."""
        m = msg.motion
        return m.ground_speed_mps < GROUND_SPEED_MPS and abs(m.vertical_speed_mps) < GROUND_VSPEED_MPS

    @property
    def judgeable(self) -> bool:
        """Enough consecutive samples for off-route detection: a jump after a gap must not look like a deviation."""
        return not self.after_break or self.segment_samples >= MIN_SAMPLES_AFTER_BREAK

    @property
    def timestamp(self) -> datetime:
        return self.last.timestamp


class LiveState:
    def __init__(self, dedup_window: int = 20_000):
        self.tracks: dict[str, Track] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedup_window = dedup_window
        self._break: dict[str, str] = {}  # aircraft id -> why its next sample starts a new segment
        self.last_break: dict[str, str] = {}  # aircraft id -> why its segment began

    def seen_before(self, message_id: str) -> bool:
        """Remember message ids (QoS 1 means duplicates happen); bounded memory."""
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        if len(self._seen) > self._dedup_window:
            self._seen.popitem(last=False)
        return False

    def accept(self, source: Source, msg: Telemetry) -> str | None:
        """Apply a telemetry message. Returns None if accepted, else why it was dropped."""
        if self.seen_before(str(msg.message_id)):
            return "duplicate"
        track = self.tracks.get(msg.aircraft_id)
        if msg.fix.type != "3d" or msg.fix.hdop > MAX_HDOP:
            if track is not None:
                self._break.setdefault(msg.aircraft_id, "fix_recovery")  # the next good sample starts a new segment
            return "poor_fix"
        if track is not None and msg.timestamp <= track.timestamp:
            return "out_of_order"
        if track is None:
            track = self.tracks[msg.aircraft_id] = Track(msg.aircraft_id, source, msg)
            self._begin_segment(track, msg, None)
        else:
            why = self._break.pop(msg.aircraft_id, None)
            if why is None and (msg.timestamp - track.timestamp).total_seconds() > SEGMENT_GAP_S:
                why = "gap"
            if why is not None:
                self._begin_segment(track, msg, why)
        track.last, track.online = msg, True
        track.segment_samples += 1
        track.history.append((msg.position.lat, msg.position.lon, msg.position.alt_msl_m))
        track.times.append(msg.timestamp.timestamp())
        return None

    def _begin_segment(self, track: Track, msg: Telemetry, why: str | None) -> None:
        track.segment_id = int(msg.timestamp.timestamp() * 1000)
        track.segment_samples, track.after_break = 0, why is not None
        track.history.clear()  # the detector and the trail must not see across the break
        track.times.clear()
        self._break.pop(msg.aircraft_id, None)
        if why:
            self.last_break[msg.aircraft_id] = why

    def note_reconnect(self, aircraft_id: str) -> None:
        """The unit reconnected (status online or back from silence): its next sample starts a new segment."""
        if aircraft_id in self.tracks:
            self._break[aircraft_id] = "reconnect"

    def drop(self, aircraft_id: str) -> bool:
        """Forget an aircraft (a simulated one that left); True if it was tracked."""
        self._break.pop(aircraft_id, None)
        return self.tracks.pop(aircraft_id, None) is not None

    def nearby(self, lat: float, lon: float, radius_m: float, exclude: str | None = None) -> list[Track]:
        """Airborne aircraft near a point (parked ones are not traffic)."""
        out = [
            t
            for t in self.tracks.values()
            if t.aircraft_id != exclude
            and not t.on_ground
            and distance_m(lat, lon, t.last.position.lat, t.last.position.lon) <= radius_m
        ]
        return sorted(out, key=lambda t: distance_m(lat, lon, t.last.position.lat, t.last.position.lon))

"""MQTT topic layout (ARCHITECTURE §12.1).

    aerolink/aircraft/{id}/{kind}   physical devices
    aerolink/sim/{id}/{kind}        simulator (same payloads)

The source (physical or simulated) comes from the namespace, which the broker ACL enforces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Source = Literal["aircraft", "sim"]
INBOUND = {"telemetry", "status", "health", "route-request", "route-selection"}
# Not device messages (the simulator's side of the contract, §8): a flown path, the simulated pilot's disclaimer
# acknowledgement and its answer to an ATC reroute. Accepted only in the sim namespace.
SIM_ONLY = {"flight-plan", "ai-ack", "reroute-response"}
OUTBOUND = {"route-response", "flight-status", "traffic"}
SIM_OUTBOUND = {"reroute"}  # backend -> simulated pilot: the approved ATC reroute (§8)
SUBSCRIPTIONS = [f"aerolink/{ns}/+/{kind}" for ns in ("aircraft", "sim") for kind in sorted(INBOUND)] + [
    f"aerolink/sim/+/{kind}" for kind in sorted(SIM_ONLY)
]


@dataclass(frozen=True)
class Topic:
    source: Source
    aircraft_id: str
    kind: str

    def __str__(self) -> str:
        return f"aerolink/{self.source}/{self.aircraft_id}/{self.kind}"


def parse(topic: str) -> Topic | None:
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "aerolink" or parts[1] not in ("aircraft", "sim"):
        return None
    if parts[3] not in INBOUND | OUTBOUND | SIM_ONLY | SIM_OUTBOUND:
        return None
    return Topic("sim" if parts[1] == "sim" else "aircraft", parts[2], parts[3])


def reply(to: Topic, kind: str) -> str:
    assert kind in OUTBOUND
    return str(Topic(to.source, to.aircraft_id, kind))

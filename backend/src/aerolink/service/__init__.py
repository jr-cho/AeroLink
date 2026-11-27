"""Service layer (ARCHITECTURE §11): owns all state and all I/O (MQTT, database, HTTP).

The calculation layer (routing, forecast, traffic, network) stays pure; this package
wires it to the outside world. Entry point: ``python -m aerolink.service``.
"""

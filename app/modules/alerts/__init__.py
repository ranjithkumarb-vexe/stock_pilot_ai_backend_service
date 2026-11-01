"""Monitor & alerts (BE-17, SPEC 7.7): detectors -> materiality -> dedupe -> deliver (SSE) -> track.

Import-light on purpose: `materiality`, `detectors`, `adaptive` are pure; `service` talks to the database;
`monitor` is the in-process APScheduler runner; `router` is the HTTP surface.
"""

# -*- coding: utf-8 -*-
"""Container health probe.

Two container modes share this image:

* ``python main.py --serve-only`` (compose ``server``) listens on HTTP.
* ``python main.py --schedule`` (the image's default ``CMD``, compose
  ``analyzer``) runs the scheduler only and never binds a port.

Probing HTTP in the second mode can never succeed, so a naive probe marks
every scheduler container ``unhealthy`` forever — which is what happened
before this file existed. The probe therefore first decides whether an HTTP
endpoint is expected at all and reports healthy when it is not.

Kept as a file rather than an inline ``python -c`` because Docker joins
``HEALTHCHECK CMD`` continuations into a single line, which destroys Python
block indentation (``for`` / ``try`` end up on one physical line and raise
``SyntaxError``) — a broken probe makes every container permanently unhealthy.

Ports are probed in order: ``API_PORT`` from the environment, then 8000.
The container's ``API_PORT`` comes from ``env_file`` while the actually bound
port comes from compose's ``--port`` interpolation, so the two can disagree.
"""

from __future__ import annotations

import os
import sys
import urllib.request

DEFAULT_PORT = "8000"
DEFAULT_HEALTH_PATH = "/api/health"
PROBE_TIMEOUT_SECONDS = 5
PID_1_CMDLINE = "/proc/1/cmdline"

# Any of these make the process serve HTTP; anything else (notably
# ``--schedule``, the image default) does not.
SERVE_FLAGS = (
    b"--serve-only",
    b"--serve",
    b"--webui-only",
    b"--webui",
)


def health_path() -> str:
    """Endpoint to probe. Overridable per image via ``DSA_HEALTH_PATH``."""
    raw = (os.environ.get("DSA_HEALTH_PATH") or "").strip()
    if not raw:
        return DEFAULT_HEALTH_PATH
    return raw if raw.startswith("/") else "/" + raw


def pid1_argv() -> list[str]:
    """Return PID 1's argv, or an empty list when it cannot be read.

    Reading PID 1 rather than ``sys.argv`` matters: the probe runs as a
    short-lived child, so ``sys.argv`` only describes the probe itself.
    """
    try:
        with open(PID_1_CMDLINE, "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def served_port(argv: list[str]) -> str | None:
    """Return the port from ``--port N`` / ``--port=N`` in PID 1's argv.

    This is the port the service actually bound. The ``API_PORT`` environment
    variable is not a reliable substitute: Compose interpolates ``--port`` from
    the host shell while ``API_PORT`` inside the container comes from
    ``env_file``, so the two can legitimately disagree — probing only the env
    value marked a healthy server on a custom port as ``unhealthy``.
    """
    for index, arg in enumerate(argv):
        if arg == "--port" and index + 1 < len(argv):
            return argv[index + 1].strip() or None
        if arg.startswith("--port="):
            return arg.split("=", 1)[1].strip() or None
    return None


def candidate_ports(argv: list[str] | None = None) -> list[str]:
    ports: list[str] = []
    for raw in (
        os.environ.get("DSA_HEALTH_PORT"),
        served_port(argv or []),
        os.environ.get("API_PORT"),
        DEFAULT_PORT,
    ):
        port = (raw or "").strip()
        if port and port not in ports:
            ports.append(port)
    return ports


def probe(port: str) -> bool:
    url = f"http://127.0.0.1:{port}{health_path()}"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return response.status == 200
    except Exception:
        return False


def http_expected(argv: list[str] | None = None) -> bool:
    """Return whether this container is supposed to expose an HTTP port.

    Images whose entrypoint is not ``main.py`` (no ``--serve`` flag) declare
    intent with ``DSA_HEALTH_PATH`` / ``DSA_HEALTH_PORT`` instead of relying on
    argv inspection — otherwise a plain ``python app.py`` would look like a
    scheduler-only container and report healthy without ever being probed.

    If PID 1's command line is unreadable we assume HTTP is expected, keeping
    the stricter behaviour for the case this probe was originally written for.
    """
    if (os.environ.get("DSA_HEALTH_PATH") or "").strip():
        return True
    if (os.environ.get("DSA_HEALTH_PORT") or "").strip():
        return True
    effective = pid1_argv() if argv is None else argv
    if not effective:
        return True
    return any(flag.decode() in effective for flag in SERVE_FLAGS)


def main() -> int:
    argv = pid1_argv()
    if not http_expected(argv):
        return 0
    return 0 if any(probe(port) for port in candidate_ports(argv)) else 1


if __name__ == "__main__":
    sys.exit(main())

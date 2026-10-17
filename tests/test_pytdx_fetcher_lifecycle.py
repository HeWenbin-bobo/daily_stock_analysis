# -*- coding: utf-8 -*-
"""Regression tests for PytdxFetcher connection lifecycle.

The connect-failure path used to leak one TCP socket per failed host: pytdx's
``connect()`` assigns ``self.client = TrafficStatSocket(...)`` *before* the
connection attempt, and on failure it neither closes nor clears that socket.
The retry loop then overwrote ``self.client`` on the next host, orphaning the
previous socket until interpreter exit.
"""

import socket
import sys
import unittest
from unittest.mock import MagicMock, patch

if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()
if "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.base import DataFetchError
from data_provider.pytdx_fetcher import PytdxFetcher


class _FakeApi:
    """Minimal TdxHq_API stand-in that mimics the real connect()/disconnect()."""

    def __init__(self):
        self.client = None
        self.disconnect_calls = 0

    def connect(self, ip, port, time_out=None, **_kwargs):
        # Real pytdx assigns self.client before attempting the connection.
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Force a deterministic failure without touching the network.
        self.client.settimeout(0.01)
        try:
            self.client.connect(("10.255.255.1", 1))
        except OSError:
            return False
        return True

    def disconnect(self):
        self.disconnect_calls += 1
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None


class PytdxSessionLeakTestCase(unittest.TestCase):
    def _open_socket_count(self, sockets):
        return len([s for s in sockets if s.fileno() != -1])

    def test_connect_failure_loop_does_not_leak_sockets(self) -> None:
        created = []
        original_init = socket.socket.__init__

        def tracked_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created.append(self)

        api = _FakeApi()
        fetcher = PytdxFetcher(hosts=[("10.255.255.1", 7709), ("10.255.255.2", 7709), ("10.255.255.3", 7709)])

        with patch.object(fetcher, "_get_pytdx", return_value=lambda: api), \
             patch.object(socket.socket, "__init__", tracked_init):
            with self.assertRaises(DataFetchError):
                with fetcher._pytdx_session():
                    self.fail("session must not open when every host fails")

        self.assertGreaterEqual(len(created), 3)
        # Every socket created by the failed attempts must be closed.
        self.assertEqual(self._open_socket_count(created), 0)


if __name__ == "__main__":
    unittest.main()

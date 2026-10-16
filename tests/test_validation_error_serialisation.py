# -*- coding: utf-8 -*-
"""Validation errors must stay JSON-serialisable.

Pydantic echoes the rejected input verbatim in ``RequestValidationError``.
A client sending ``NaN`` or ``Infinity`` therefore puts a non-finite float
into the error payload, which ``json`` refuses to encode -- turning a
legitimate 422 into a 500. These tests pin the sanitised behaviour.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

# Keep this test runnable when optional LLM runtime deps are not installed.
try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth  # noqa: E402
from api.app import create_app  # noqa: E402
from api.middlewares.error_handler import _json_safe  # noqa: E402
from src.config import Config  # noqa: E402
from src.storage import DatabaseManager  # noqa: E402


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class JsonSafeHelperTestCase(unittest.TestCase):
    """Unit coverage for the sanitiser itself."""

    def test_replaces_non_finite_floats_with_their_literal(self) -> None:
        # Shape mirrors pydantic's ``errors()`` output for a rejected bound.
        errors = [
            {
                "type": "less_than_equal",
                "loc": ("body", "quantity"),
                "msg": "Input should be less than or equal to 1000000000000",
                "input": float("nan"),
                "ctx": {"le": 1e12},
            },
            {
                "type": "less_than_equal",
                "loc": ("body", "price"),
                "msg": "Input should be less than or equal to 1000000000",
                "input": float("inf"),
                "ctx": {"le": 1e9},
            },
        ]

        sanitised = _json_safe(errors)

        self.assertEqual(sanitised[0]["input"], "nan")
        self.assertEqual(sanitised[1]["input"], "inf")
        # Everything else is untouched and the payload is now serialisable.
        self.assertEqual(sanitised[0]["ctx"], {"le": 1e12})
        self.assertEqual(sanitised[0]["loc"], ["body", "quantity"])
        json.dumps(sanitised)

    def test_keeps_finite_values_and_non_float_payloads(self) -> None:
        self.assertEqual(_json_safe(1.5), 1.5)
        self.assertEqual(_json_safe("abc"), "abc")
        self.assertIsNone(_json_safe(None))
        self.assertEqual(_json_safe({"a": [1, 2.5]}), {"a": [1, 2.5]})
        self.assertTrue(math.isfinite(_json_safe(-0.0)))

    def test_negative_infinity_is_also_replaced(self) -> None:
        self.assertEqual(_json_safe(float("-inf")), "-inf")

    def test_decodes_raw_bytes_body(self) -> None:
        # A non-JSON content type leaves the raw request body as bytes, which
        # ``json`` cannot encode any more than a non-finite float.
        errors = [
            {
                "type": "model_attributes_type",
                "loc": ("body",),
                "msg": "Input should be a valid dictionary",
                "input": b"hello",
            }
        ]

        sanitised = _json_safe(errors)

        self.assertEqual(sanitised[0]["input"], "hello")
        json.dumps(sanitised)

    def test_falls_back_to_repr_for_unknown_types(self) -> None:
        sanitised = _json_safe({"input": object()})
        self.assertIsInstance(sanitised["input"], str)
        json.dumps(sanitised)


class ValidationErrorResponseTestCase(unittest.TestCase):
    """End-to-end: a non-finite input yields 422, not 500."""

    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.env_path = self.data_dir / ".env"
        self.db_path = self.data_dir / "validation_error_test.db"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        app = create_app(static_dir=self.data_dir / "empty-static")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        _reset_auth_globals()
        self.temp_dir.cleanup()

    def _post_trade(self, quantity, price) -> tuple[int, dict]:
        payload = {
            "account_id": 1,
            "symbol": "600519",
            "trade_date": "2026-10-15",
            "side": "buy",
            "quantity": quantity,
            "price": price,
        }
        # NaN/Infinity are not valid JSON, so a hostile client has to emit them
        # by hand; httpx's json= would refuse.
        response = self.client.post(
            "/api/v1/portfolio/trades",
            content=json.dumps(payload, allow_nan=True).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = response.json()
        return response.status_code, body

    def test_nan_quantity_is_a_validation_error_not_a_server_error(self) -> None:
        status, body = self._post_trade(float("nan"), 100)
        self.assertEqual(status, 422, body)
        self.assertEqual(body.get("error"), "validation_error")
        self.assertIn("quantity", json.dumps(body))

    def test_infinite_price_is_a_validation_error_not_a_server_error(self) -> None:
        status, body = self._post_trade(10, float("inf"))
        self.assertEqual(status, 422, body)
        self.assertEqual(body.get("error"), "validation_error")
        self.assertIn("price", json.dumps(body))

    def test_non_json_content_type_is_a_validation_error(self) -> None:
        response = self.client.post(
            "/api/v1/portfolio/trades",
            content=b"hello",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json().get("error"), "validation_error")


if __name__ == "__main__":
    unittest.main()

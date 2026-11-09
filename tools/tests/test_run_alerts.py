#!/usr/bin/env python3
"""Local unit tests for Filing Alerts deep-link + subscriber merge (no network)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_alerts as ra  # noqa: E402


PRODUCT = "4d76b65f-9c86-4ae5-9095-404f810fee7d"
CHECKOUT = "b729b847-1472-4137-9239-d3df467af258"
SUB = "11111111-2222-4333-8444-555555555555"


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.polar = {}
        self.tg_updates = {"ok": True, "result": []}
        self.sent = []

    def __call__(self, url, data=None, headers=None, method="GET"):
        self.calls.append((method, url, data))
        if url.startswith("https://api.polar.sh"):
            path = url[len("https://api.polar.sh") :]
            if path in self.polar:
                val = self.polar[path]
                if isinstance(val, Exception):
                    raise val
                return val
            raise urllib_error_404(path)
        if "api.telegram.org" in url:
            if url.endswith("/getUpdates"):
                return self.tg_updates
            if url.endswith("/sendMessage"):
                payload = json.loads(data.decode()) if data else {}
                self.sent.append(payload)
                return {"ok": True, "result": payload}
            return {"ok": True}
        raise AssertionError(f"unexpected url {url}")


def urllib_error_404(path):
    import io
    import urllib.error

    raise urllib.error.HTTPError(path, 404, "Not Found", hdrs=None, fp=io.BytesIO(b"{}"))


class NormalizeTests(unittest.TestCase):
    def test_uuid(self):
        self.assertEqual(ra.normalize_checkout_id(CHECKOUT), CHECKOUT)

    def test_hex32(self):
        h = CHECKOUT.replace("-", "")
        self.assertEqual(ra.normalize_checkout_id(h), CHECKOUT)

    def test_query_wrapper(self):
        self.assertEqual(
            ra.normalize_checkout_id(f"checkout_id={CHECKOUT}&x=1"), CHECKOUT
        )

    def test_bad(self):
        self.assertIsNone(ra.normalize_checkout_id("not-an-id"))


class ParseTickersTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(ra.parse_tickers("nvda, aapl;MSFT"), ["NVDA", "AAPL", "MSFT"])


class LinkFromCheckoutTests(unittest.TestCase):
    def setUp(self):
        os.environ["POLAR_ACCESS_TOKEN"] = "test-token"
        ra.PRODUCT_ID = PRODUCT
        self.http = FakeHttp()
        self.state = ra.load_state(Path(tempfile.mkstemp()[1]))

    def test_succeeded_links_and_stores_chat(self):
        self.http.polar[f"/v1/checkouts/{CHECKOUT}"] = {
            "id": CHECKOUT,
            "status": "succeeded",
            "product_id": PRODUCT,
            "subscription_id": SUB,
            "customer_id": "cust-1",
            "customer_email": "a@b.co",
            "custom_field_data": {"tickers": "NVDA,AAPL"},
        }
        self.http.polar[f"/v1/subscriptions/{SUB}"] = {
            "id": SUB,
            "status": "trialing",
            "customer": {"email": "a@b.co"},
            "custom_field_data": {},
        }
        reply, rec = ra.link_from_checkout(CHECKOUT, "999001", self.state, http=self.http)
        self.assertIn("Linked", reply)
        self.assertIn("NVDA", reply)
        self.assertEqual(self.state["links"][SUB]["telegram_chat_id"], "999001")
        self.assertEqual(self.state["chat_subscriptions"]["999001"], SUB)
        self.assertEqual(rec["tickers"], ["NVDA", "AAPL"])
        self.assertEqual(rec["status"], "trialing")

    def test_open_checkout_rejected(self):
        self.http.polar[f"/v1/checkouts/{CHECKOUT}"] = {
            "id": CHECKOUT,
            "status": "open",
            "product_id": PRODUCT,
            "subscription_id": None,
            "custom_field_data": {},
        }
        reply, rec = ra.link_from_checkout(CHECKOUT, "1", self.state, http=self.http)
        self.assertIn("open", reply.lower())
        self.assertIsNone(rec)

    def test_wrong_product(self):
        self.http.polar[f"/v1/checkouts/{CHECKOUT}"] = {
            "id": CHECKOUT,
            "status": "succeeded",
            "product_id": "00000000-0000-4000-8000-000000000000",
            "subscription_id": SUB,
            "custom_field_data": {},
        }
        reply, rec = ra.link_from_checkout(CHECKOUT, "1", self.state, http=self.http)
        self.assertIn("not for Filing Alerts", reply)
        self.assertIsNone(rec)


class ProcessTelegramTests(unittest.TestCase):
    def setUp(self):
        os.environ["POLAR_ACCESS_TOKEN"] = "test-token"
        ra.PRODUCT_ID = PRODUCT
        self.http = FakeHttp()
        self.state = ra.load_state(Path(tempfile.mkstemp()[1]))
        self.http.polar[f"/v1/checkouts/{CHECKOUT}"] = {
            "id": CHECKOUT,
            "status": "succeeded",
            "product_id": PRODUCT,
            "subscription_id": SUB,
            "customer_id": "cust-1",
            "custom_field_data": {"tickers": "TSLA"},
        }
        self.http.polar[f"/v1/subscriptions/{SUB}"] = {
            "id": SUB,
            "status": "active",
            "customer": {"email": "x@y.z"},
            "custom_field_data": {},
        }

    def test_start_deep_link(self):
        self.http.tg_updates = {
            "ok": True,
            "result": [
                {
                    "update_id": 10,
                    "message": {
                        "chat": {"id": 4242},
                        "text": f"/start {CHECKOUT}",
                    },
                }
            ],
        }
        n = ra.process_telegram_updates("tok", self.state, http=self.http)
        self.assertEqual(n, 1)
        self.assertEqual(self.state["telegram_offset"], 11)
        self.assertEqual(self.state["links"][SUB]["telegram_chat_id"], "4242")
        self.assertTrue(any("Linked" in (s.get("text") or "") for s in self.http.sent))

    def test_tickers_command_after_link(self):
        self.state["links"][SUB] = {
            "telegram_chat_id": "4242",
            "tickers": ["TSLA"],
            "subscription_id": SUB,
        }
        self.state["chat_subscriptions"]["4242"] = SUB
        self.http.tg_updates = {
            "ok": True,
            "result": [
                {
                    "update_id": 20,
                    "message": {"chat": {"id": 4242}, "text": "/tickers NVDA, MSFT"},
                }
            ],
        }
        n = ra.process_telegram_updates("tok", self.state, http=self.http)
        self.assertEqual(n, 1)
        self.assertEqual(self.state["links"][SUB]["tickers"], ["NVDA", "MSFT"])


class LoadSubscribersMergeTests(unittest.TestCase):
    def setUp(self):
        os.environ["POLAR_ACCESS_TOKEN"] = "test-token"
        ra.PRODUCT_ID = PRODUCT
        self.http = FakeHttp()
        self.state = {"links": {}, "chat_subscriptions": {}}

    def test_old_custom_field_flow(self):
        self.http.polar[
            f"/v1/subscriptions/?product_id={PRODUCT}&active=true&limit=100&page=1"
        ] = {
            "items": [
                {
                    "id": SUB,
                    "status": "active",
                    "customer": {"email": "old@flow"},
                    "custom_field_data": {
                        "telegram_chat_id": "777",
                        "tickers": "AAPL",
                    },
                }
            ],
            "pagination": {"max_page": 1},
        }
        subs = ra.load_subscribers(self.state, http=self.http)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["telegram_chat_id"], "777")
        self.assertEqual(subs[0]["tickers"], ["AAPL"])

    def test_deep_link_overrides_missing_chat_field(self):
        self.state["links"][SUB] = {
            "telegram_chat_id": "888",
            "tickers": ["NVDA"],
        }
        self.http.polar[
            f"/v1/subscriptions/?product_id={PRODUCT}&active=true&limit=100&page=1"
        ] = {
            "items": [
                {
                    "id": SUB,
                    "status": "trialing",
                    "customer": {"email": "new@flow"},
                    "custom_field_data": {"tickers": "NVDA"},  # no telegram_chat_id
                }
            ],
            "pagination": {"max_page": 1},
        }
        # load_subscribers may still try orders when telegram_chat_id missing on cfd
        self.http.polar[f"/v1/orders/?subscription_id={SUB}&limit=5"] = {"items": []}
        subs = ra.load_subscribers(self.state, http=self.http)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["telegram_chat_id"], "888")
        self.assertEqual(subs[0]["tickers"], ["NVDA"])


if __name__ == "__main__":
    unittest.main()

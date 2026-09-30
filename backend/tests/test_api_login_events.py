"""Sign-in security events (app.api.login_events): new_device_login on a new country or a new device, mfa_changed
when the Firebase second-factor identifier changes. Stdlib only (fake store/notifier/audit); the SQL side
(user_devices, users.mfa_factor_hash) is covered in test_api_store_db."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import login_events as le  # noqa: E402

PEPPER = b"p" * 32
CHROME_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")
CHROME_MAC_NEWER = CHROME_MAC.replace("129.0.0.0", "130.0.6723.58").replace("10_15_7", "14_6")
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.6 Mobile/15E148 Safari/604.1")


class World:
    def __init__(self) -> None:
        self.countries: set[tuple[str, str]] = set()
        self.devices: set[tuple[str, str]] = set()
        self.factor: dict[str, str] = {}
        self.alerts: list[dict] = []
        self.audit: list[dict] = []

    def svc(self):
        w = self

        class Store:
            def record_login_country(self, conn, user_id, country):
                seen = {c for u, c in w.countries if u == user_id}
                w.countries.add((user_id, country))
                return country not in seen and bool(seen)

            def record_device(self, conn, user_id, device_hash, label):
                seen = {d for u, d in w.devices if u == user_id}
                w.devices.add((user_id, device_hash))
                return device_hash not in seen and bool(seen)

            def set_mfa_factor_hash(self, conn, user_id, factor_hash):
                w.factor[user_id] = factor_hash

        return SimpleNamespace(
            store=Store(), config=SimpleNamespace(pepper=PEPPER),
            notifier=SimpleNamespace(notify=lambda conn, **kw: w.alerts.append(kw)),
            audit=SimpleNamespace(write=lambda conn, **kw: w.audit.append(kw)))

    def sign_in(self, *, ua=CHROME_MAC, device_id=None, country="MY", factor=None, uid="u1"):
        headers = {"user-agent": ua}
        if device_id:
            headers["x-device-id"] = device_id
        dh, label = le.device_key(headers, PEPPER)
        claims = {"firebase": {"sign_in_second_factor": "totp",
                               **({"second_factor_identifier": factor} if factor else {})}}
        user = {"id": uid, "mfa_factor_hash": self.factor.get(uid)}
        return le.record_sign_in(None, self.svc(), user=user, claims=claims, country=country, device_hash=dh,
                                 device_label_=label, ip_hash="ip")


class DeviceKeyTest(unittest.TestCase):
    def test_label_and_version_stable_hash(self):
        h1, l1 = le.device_key({"user-agent": CHROME_MAC}, PEPPER)
        h2, _ = le.device_key({"user-agent": CHROME_MAC_NEWER}, PEPPER)
        h3, l3 = le.device_key({"user-agent": IPHONE}, PEPPER)
        self.assertEqual((l1, l3), ("Chrome on macOS", "Safari on iOS"))
        self.assertEqual(h1, h2)                     # browser/OS updates are not a "new device"
        self.assertNotEqual(h1, h3)
        self.assertRegex(h1, r"^[0-9a-f]{64}$")

    def test_device_id_header_wins_and_is_validated(self):
        a, _ = le.device_key({"user-agent": CHROME_MAC, "x-device-id": "A" * 20}, PEPPER)
        b, _ = le.device_key({"user-agent": CHROME_MAC, "x-device-id": "B" * 20}, PEPPER)
        ua_only, _ = le.device_key({"user-agent": CHROME_MAC}, PEPPER)
        bad, _ = le.device_key({"user-agent": CHROME_MAC, "x-device-id": "short"}, PEPPER)
        self.assertNotEqual(a, b)
        self.assertEqual(bad, ua_only)               # malformed id → UA fallback
        self.assertEqual(le.device_key({}, PEPPER), (None, None))

    def test_factor_hash(self):
        self.assertIsNone(le.mfa_factor_hash({"firebase": {"sign_in_second_factor": "totp"}}, PEPPER))
        h = le.mfa_factor_hash({"firebase": {"second_factor_identifier": "f1"}}, PEPPER)
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertNotEqual(h, le.mfa_factor_hash({"firebase": {"second_factor_identifier": "f2"}}, PEPPER))


class RecordSignInTest(unittest.TestCase):
    def test_first_sign_in_is_silent_then_new_device_and_country_alert_once(self):
        w = World()
        self.assertEqual(w.sign_in(), [])                               # first device + first country: silent
        self.assertEqual(w.sign_in(ua=CHROME_MAC_NEWER), [])            # same device after a browser update
        self.assertEqual(w.sign_in(ua=IPHONE), ["new_device_login"])
        a = w.alerts[-1]
        self.assertEqual((a["kind"], a["severity"], a["user_id"]), ("new_device_login", "warn", "u1"))
        self.assertEqual(a["payload"], {"reason": "new_device", "country": "MY", "device": "Safari on iOS"})
        self.assertEqual(w.sign_in(ua=IPHONE), [])                      # seen now
        self.assertEqual(w.sign_in(ua=IPHONE, country="SG"), ["new_device_login"])
        self.assertEqual(w.alerts[-1]["payload"], {"reason": "new_country", "country": "SG"})
        # new country AND new device in one request → ONE alert
        n = len(w.alerts)
        self.assertEqual(w.sign_in(ua=IPHONE, device_id="D" * 24, country="TH"), ["new_device_login"])
        self.assertEqual(len(w.alerts), n + 1)
        self.assertEqual(w.alerts[-1]["payload"]["reason"], "new_device")
        self.assertEqual([x["action"] for x in w.audit], ["auth.new_device", "auth.new_country", "auth.new_device"])
        self.assertTrue(all("reason" not in x["payload"] for x in w.audit))

    def test_mfa_changed_only_when_factor_differs(self):
        w = World()
        self.assertEqual(w.sign_in(), [])                               # no factor claim: nothing stored
        self.assertNotIn("u1", w.factor)
        self.assertEqual(w.sign_in(factor="fac-1"), [])                 # first factor: stored silently
        first = w.factor["u1"]
        self.assertEqual(w.sign_in(factor="fac-1"), [])
        self.assertEqual(w.sign_in(factor="fac-2"), ["mfa_changed"])
        self.assertNotEqual(w.factor["u1"], first)
        self.assertEqual(w.alerts[-1]["kind"], "mfa_changed")
        self.assertEqual(w.audit[-1]["action"], "auth.mfa_changed")
        self.assertEqual(w.sign_in(factor="fac-2"), [])
        self.assertNotIn("fac-2", repr(w.factor) + repr(w.alerts) + repr(w.audit))   # only hashes


if __name__ == "__main__":
    unittest.main()

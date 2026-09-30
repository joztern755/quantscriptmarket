from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import unittest
from types import SimpleNamespace

try:  # the cryptography native backend is broken on some dev boxes (pyo3 panic); never skip in CI
    import jwt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except BaseException as e:  # noqa: BLE001
    if isinstance(e, (KeyboardInterrupt, SystemExit)) or os.environ.get("CI"):
        raise
    raise unittest.SkipTest(f"cryptography/PyJWT unavailable: {type(e).__name__}") from None

from app.errors import ExternalServiceError, Forbidden, StepUpRequired, Unauthorized
from app.security import auth
from app.security.auth import (
    AuthContext, ChainedVerifier, GoogleCertCache, PyJwtFirebaseVerifier, StaticCertSource, build_verifier,
    extract_bearer, require_admin, require_mfa, require_step_up,
)

PROJECT = "aijalon-test"
NOW = 1_790_000_000
KID = "kid-1"


def _make_key_and_cert(cn="securetoken"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime(2020, 1, 1)).not_valid_after(dt.datetime(2040, 1, 1))
            .sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


KEY, CERT_PEM = _make_key_and_cert()
OTHER_KEY, OTHER_PEM = _make_key_and_cert("other")


def claims(**over):
    c = {
        "iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT, "sub": "uid123", "user_id": "uid123",
        "iat": NOW - 10, "exp": NOW - 10 + 3600, "auth_time": NOW - 60,
        "email": "a@example.com", "email_verified": True,
        "firebase": {"sign_in_provider": "google.com", "sign_in_second_factor": "totp", "identities": {}},
    }
    for k, v in over.items():
        if v is _DROP:
            c.pop(k, None)
        else:
            c[k] = v
    return c


_DROP = object()


def mint(c=None, key=KEY, kid=KID, alg="RS256"):
    return jwt.encode(c or claims(), key, algorithm=alg, headers={"kid": kid})


def _b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def forge(header: dict, payload: dict, sig: bytes = b"") -> str:
    signing_input = _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(payload).encode())
    return signing_input + "." + _b64(sig)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.v = PyJwtFirebaseVerifier(PROJECT, StaticCertSource({KID: CERT_PEM}), clock=lambda: NOW)

    def test_valid(self):
        ctx = self.v.verify(mint())
        self.assertEqual(ctx.uid, "uid123")
        self.assertEqual(ctx.sign_in_provider, "google.com")
        self.assertEqual(ctx.second_factor, "totp")
        self.assertTrue(ctx.email_verified)
        self.assertEqual(ctx.auth_time, NOW - 60)
        self.assertEqual(ctx.claims["aud"], PROJECT)
        self.assertNotIn("claims", repr(ctx))
        with self.assertRaises(TypeError):
            ctx.claims["aud"] = "x"  # read-only

    def test_apple_ok_other_providers_refused(self):
        self.assertEqual(self.v.verify(mint(claims(firebase={"sign_in_provider": "apple.com"}))).sign_in_provider, "apple.com")
        for p in ("password", "phone", "anonymous", "custom", "github.com", None):
            with self.assertRaises(Unauthorized, msg=p):
                self.v.verify(mint(claims(firebase={"sign_in_provider": p, "sign_in_second_factor": "totp"})))

    def test_expired(self):
        with self.assertRaises(Unauthorized):
            self.v.verify(mint(claims(iat=NOW - 4000, exp=NOW - 400, auth_time=NOW - 4000)))

    def test_leeway(self):
        self.v.verify(mint(claims(iat=NOW - 3600 + 10, exp=NOW + 10, auth_time=NOW - 3600)))
        self.v.verify(mint(claims(exp=NOW - 5, iat=NOW - 3605, auth_time=NOW - 3605)))  # within 30s leeway

    def test_wrong_aud_iss(self):
        for c in (claims(aud="other-project"), claims(aud=[PROJECT, "x"]), claims(iss="https://securetoken.google.com/other"),
                  claims(iss="https://evil.example/aijalon-test"), claims(aud=_DROP), claims(iss=_DROP)):
            with self.assertRaises(Unauthorized):
                self.v.verify(mint(c))

    def test_future_and_inconsistent_times(self):
        for c in (claims(iat=NOW + 600, exp=NOW + 4000, auth_time=NOW),
                  claims(auth_time=NOW + 600),
                  claims(auth_time=NOW - 5 + 300),              # auth_time after iat
                  claims(iat=NOW - 10, exp=NOW + 7200),        # lifetime > 1h
                  claims(auth_time=_DROP), claims(auth_time="123"), claims(exp=True)):
            with self.assertRaises(Unauthorized, msg=str(c)):
                self.v.verify(mint(c))

    def test_subject(self):
        for c in (claims(sub="", user_id=""), claims(sub="x" * 129, user_id="x" * 129), claims(user_id="someone-else"),
                  claims(sub=_DROP)):
            with self.assertRaises(Unauthorized):
                self.v.verify(mint(c))

    def test_alg_none_rejected(self):
        with self.assertRaises(Unauthorized):
            self.v.verify(forge({"alg": "none", "kid": KID, "typ": "JWT"}, claims()))
        with self.assertRaises(Unauthorized):
            self.v.verify(forge({"alg": "none", "kid": KID}, claims(), b"x"))

    def test_hs256_key_confusion_rejected(self):
        header = {"alg": "HS256", "kid": KID, "typ": "JWT"}
        signing_input = _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(claims()).encode())
        sig = hmac.new(CERT_PEM.encode(), signing_input.encode(), hashlib.sha256).digest()
        with self.assertRaises(Unauthorized):
            self.v.verify(signing_input + "." + _b64(sig))
        # and an RS256 header with an HMAC signature
        header = {"alg": "RS256", "kid": KID}
        signing_input = _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(claims()).encode())
        sig = hmac.new(CERT_PEM.encode(), signing_input.encode(), hashlib.sha256).digest()
        with self.assertRaises(Unauthorized):
            self.v.verify(signing_input + "." + _b64(sig))

    def test_other_algs_rejected(self):
        with self.assertRaises(Unauthorized):
            self.v.verify(mint(alg="RS512"))
        with self.assertRaises(Unauthorized):
            self.v.verify(mint(alg="PS256"))

    def test_wrong_signing_key_and_unknown_kid(self):
        with self.assertRaises(Unauthorized):
            self.v.verify(mint(key=OTHER_KEY))
        with self.assertRaises(Unauthorized):
            self.v.verify(mint(kid="nope"))
        with self.assertRaises(Unauthorized):
            self.v.verify(forge({"alg": "RS256"}, claims()))  # no kid

    def test_tampered_payload(self):
        tok = mint()
        h, p, s = tok.split(".")
        p2 = _b64(json.dumps(claims(sub="admin", user_id="admin")).encode())
        with self.assertRaises(Unauthorized):
            self.v.verify(".".join([h, p2, s]))

    def test_garbage(self):
        for t in ("", "abc", "a.b.c", "x" * 9000, None, 123):
            with self.assertRaises(Unauthorized):
                self.v.verify(t)

    def test_tenant_rejected(self):
        with self.assertRaises(Unauthorized):
            self.v.verify(mint(claims(firebase={"sign_in_provider": "google.com", "tenant": "t1"})))

    def test_no_mfa_token_verifies_but_policy_rejects(self):
        ctx = self.v.verify(mint(claims(firebase={"sign_in_provider": "google.com"})))
        self.assertIsNone(ctx.second_factor)
        with self.assertRaises(Unauthorized):
            require_mfa(ctx)


def ctx(second="totp", auth_time=NOW - 60):
    return AuthContext("uid", None, True, "google.com", second, auth_time, {})


class PolicyTests(unittest.TestCase):
    def test_require_mfa(self):
        require_mfa(ctx())
        for s in (None, "phone", "", "TOTP", "passkey"):
            with self.assertRaises(Unauthorized, msg=s):
                require_mfa(ctx(second=s))

    def test_step_up(self):
        require_step_up(ctx(auth_time=NOW - 300), now=NOW)
        require_step_up(ctx(auth_time=NOW - 10), now=dt.datetime.fromtimestamp(NOW, dt.timezone.utc))
        with self.assertRaises(StepUpRequired):
            require_step_up(ctx(auth_time=NOW - 301), now=NOW)
        with self.assertRaises(StepUpRequired):
            require_step_up(ctx(second="phone", auth_time=NOW), now=NOW)
        with self.assertRaises(StepUpRequired):
            require_step_up(ctx(second=None, auth_time=NOW), now=NOW)
        with self.assertRaises(StepUpRequired):
            require_step_up(ctx(auth_time=NOW + 3600), now=NOW)  # future auth_time
        with self.assertRaises(ValueError):
            require_step_up(ctx(), now=dt.datetime(2026, 1, 1))  # naive

    def test_admin(self):
        require_admin(ctx(), lambda uid: "admin", now=NOW)
        with self.assertRaises(Forbidden):
            require_admin(ctx(), lambda uid: "user", now=NOW)
        with self.assertRaises(Forbidden):
            require_admin(ctx(), lambda uid: None, now=NOW)

        def boom(uid):
            raise RuntimeError("db down")
        with self.assertRaises(Forbidden):
            require_admin(ctx(), boom, now=NOW)
        with self.assertRaises(StepUpRequired):
            require_admin(ctx(auth_time=NOW - 1000), lambda uid: "admin", now=NOW)
        # token custom claims are ignored
        c = AuthContext("uid", None, True, "google.com", "totp", NOW, {"role": "admin", "admin": True})
        with self.assertRaises(Forbidden):
            require_admin(c, lambda uid: "user", now=NOW)

    def test_extract_bearer(self):
        self.assertEqual(extract_bearer("Bearer abc.def.ghi"), "abc.def.ghi")
        self.assertEqual(extract_bearer("bearer abc"), "abc")
        for h in (None, "", "Basic abc", "Bearer", "Bearer a b", "Bearer " + "x" * 9000):
            with self.assertRaises(Unauthorized):
                extract_bearer(h)


class CertCacheTests(unittest.TestCase):
    def setUp(self):
        self.t = 1000.0
        self.calls = 0
        self.body = json.dumps({KID: CERT_PEM}).encode()
        self.cc = "public, max-age=100, must-revalidate, no-transform"
        self.fail = False

        def fetch(url, timeout):
            self.calls += 1
            if self.fail:
                raise OSError("network down")
            return 200, self.body, self.cc

        self.cache = GoogleCertCache(fetch=fetch, clock=lambda: self.t, stale_grace_s=50)

    def test_caches_for_max_age(self):
        self.cache.get_key(KID)
        self.t += 99
        self.cache.get_key(KID)
        self.assertEqual(self.calls, 1)
        self.t += 2
        self.cache.get_key(KID)
        self.assertEqual(self.calls, 2)

    def test_unknown_kid_refresh_throttled(self):
        self.cache.get_key(KID)
        with self.assertRaises(Unauthorized):
            self.cache.get_key("rotated")
        self.assertEqual(self.calls, 1)  # just fetched; throttled
        self.t += 61
        self.body = json.dumps({KID: CERT_PEM, "rotated": OTHER_PEM}).encode()
        self.cache.get_key("rotated")
        self.assertEqual(self.calls, 2)

    def test_stale_grace_then_fail_closed(self):
        self.cache.get_key(KID)
        self.fail = True
        self.t += 120  # expired at +100, within 50s grace
        self.cache.get_key(KID)
        self.t += 40   # +160: beyond grace
        with self.assertRaises(ExternalServiceError):
            self.cache.get_key(KID)

    def test_initial_failure_and_bad_payloads(self):
        self.fail = True
        with self.assertRaises(ExternalServiceError):
            self.cache.get_key(KID)
        self.fail = False
        for body in (b"not json", b"{}", json.dumps({KID: "garbage"}).encode()):
            self.body = body
            self.t += 1000
            with self.assertRaises(ExternalServiceError):
                self.cache.get_key(KID)

    def test_ttl_parsing(self):
        c = GoogleCertCache(fetch=lambda u, t: (200, b"{}", ""), min_ttl_s=60, max_ttl_s=3600)
        self.assertEqual(c._ttl("max-age=19000"), 3600)
        self.assertEqual(c._ttl("max-age=5"), 60)
        self.assertEqual(c._ttl("no-store"), 60)
        self.assertEqual(c._ttl("public, max-age=600"), 600)

    def test_end_to_end_with_verifier(self):
        v = PyJwtFirebaseVerifier(PROJECT, self.cache, clock=lambda: NOW)
        self.assertEqual(v.verify(mint()).uid, "uid123")

    def test_https_only(self):
        with self.assertRaises(ValueError):
            auth._urllib_fetch("http://example.com", 1)


class AdminVerifierAndBuildTests(unittest.TestCase):
    def setUp(self):
        self._saved = auth._fb_auth

    def tearDown(self):
        auth._fb_auth = self._saved

    def test_firebase_admin_mapping(self):
        class RevokedIdTokenError(Exception):
            pass

        class CertificateFetchError(Exception):
            pass

        calls = []

        def verify_id_token(token, app=None, check_revoked=False):
            calls.append(check_revoked)
            if token == "revoked":
                raise RevokedIdTokenError("revoked")
            if token == "certs":
                raise CertificateFetchError("x")
            return dict(claims(), uid="uid123")

        auth._fb_auth = SimpleNamespace(verify_id_token=verify_id_token, CertificateFetchError=CertificateFetchError)
        v = auth.FirebaseAdminVerifier(PROJECT)
        self.assertEqual(v.verify("ok").uid, "uid123")
        self.assertEqual(calls, [True])
        with self.assertRaises(Unauthorized):
            v.verify("revoked")
        with self.assertRaises(ExternalServiceError):
            v.verify("certs")

    def test_chained_disagreement(self):
        a = SimpleNamespace(verify=lambda t: ctx())
        b = SimpleNamespace(verify=lambda t: AuthContext("other", None, True, "google.com", "totp", NOW - 60, {}))
        with self.assertRaises(Unauthorized):
            ChainedVerifier(a, b).verify("t")
        self.assertEqual(ChainedVerifier(a, a).verify("t").uid, "uid")

    def test_build(self):
        s = SimpleNamespace(is_prod=False, firebase_project_id=PROJECT)
        self.assertIsInstance(build_verifier(s, certs=StaticCertSource({KID: CERT_PEM})), PyJwtFirebaseVerifier)
        auth._fb_auth = None
        with self.assertRaises(RuntimeError):
            build_verifier(SimpleNamespace(is_prod=True, firebase_project_id=PROJECT), certs=StaticCertSource({}))
        with self.assertRaises(ValueError):
            build_verifier(SimpleNamespace(is_prod=False, firebase_project_id=""), certs=StaticCertSource({}))


if __name__ == "__main__":
    unittest.main()

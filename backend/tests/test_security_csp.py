from __future__ import annotations

import unittest

from app.security.csp import API_SECURITY_HEADERS, api_security_headers, web_csp, web_security_headers


def parse(csp: str) -> dict[str, list[str]]:
    out = {}
    for part in csp.split(";"):
        toks = part.split()
        if toks:
            out[toks[0]] = toks[1:]
    return out


class WebCspTests(unittest.TestCase):
    def setUp(self):
        self.csp = web_csp(firebase_project_id="aijalon-prod")
        self.d = parse(self.csp)

    def test_core_directives(self):
        self.assertEqual(self.d["default-src"], ["'self'"])
        self.assertEqual(self.d["frame-ancestors"], ["'none'"])
        self.assertEqual(self.d["base-uri"], ["'none'"])
        self.assertEqual(self.d["object-src"], ["'none'"])
        self.assertEqual(self.d["form-action"], ["'self'"])
        self.assertEqual(self.d["img-src"], ["'self'", "data:"])
        self.assertIn("upgrade-insecure-requests", self.d)

    def test_no_unsafe_sources(self):
        for bad in ("'unsafe-inline'", "'unsafe-eval'", "http:", " * ", "blob:", "*.googleapis.com", "*.firebaseapp.com"):
            self.assertNotIn(bad, self.csp)

    def test_required_hosts(self):
        s = self.d["script-src"]
        for h in ("'self'", "https://www.gstatic.com/firebasejs/", "https://apis.google.com", "https://js.stripe.com"):
            self.assertIn(h, s)
        f = self.d["frame-src"]
        for h in ("https://aijalon-prod.firebaseapp.com", "https://js.stripe.com", "https://hooks.stripe.com", "'self'"):
            self.assertIn(h, f)
        c = self.d["connect-src"]
        for h in ("'self'", "https://api.aijalon.trade", "https://api.hyperliquid.xyz", "https://api.stripe.com",
                  "https://identitytoolkit.googleapis.com", "https://securetoken.googleapis.com"):
            self.assertIn(h, c)
        self.assertEqual(self.d["style-src"], ["'self'", "https://fonts.googleapis.com"])
        self.assertEqual(self.d["font-src"], ["https://fonts.gstatic.com"])

    def test_custom_auth_domain(self):
        d = parse(web_csp(firebase_project_id="p", firebase_auth_domain="aijalon.trade"))
        self.assertNotIn("https://p.firebaseapp.com", d["frame-src"])
        self.assertIn("'self'", d["frame-src"])

    def test_extra_and_refusals(self):
        d = parse(web_csp(extra={"frame-src": ["https://appleid.apple.com"]}))
        self.assertIn("https://appleid.apple.com", d["frame-src"])
        for bad in ({"script-src": ["'unsafe-inline'"]}, {"script-src": ["*"]}, {"connect-src": ["http://x"]},
                    {"script-src": ["https://a; script-src *"]}, {"bogus-src": ["https://a"]}):
            with self.assertRaises(ValueError):
                web_csp(extra=bad)
        self.assertIn("report-uri https://csp.example/r", web_csp(report_uri="https://csp.example/r"))

    def test_web_headers(self):
        h = web_security_headers(self.csp)
        self.assertEqual(h["Content-Security-Policy"], self.csp)
        self.assertIn("preload", h["Strict-Transport-Security"])
        self.assertEqual(h["Referrer-Policy"], "strict-origin")
        self.assertEqual(h["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")


class ApiHeaderTests(unittest.TestCase):
    def test_api_headers(self):
        h = api_security_headers()
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertEqual(api_security_headers(cacheable_public=True, max_age_s=30)["Cache-Control"], "public, max-age=30")
        self.assertEqual(API_SECURITY_HEADERS["Cache-Control"], "no-store")  # not mutated


if __name__ == "__main__":
    unittest.main()

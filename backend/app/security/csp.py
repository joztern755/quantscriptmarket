"""Security headers for the API and the CSP / headers for the web app (SPEC §5.7).

Source of truth for the WEB policy
----------------------------------
``web/build.mjs`` GENERATES the site's Content-Security-Policy at build time (``web/dist/csp.txt``; the ``<meta>``
copy omits the header-only directives) from ``web/public/app-config.json``. The reviewed, committed copy is
``infra/csp.txt``; ``firebase.json`` serves exactly that string as the Hosting header, and ``infra/csp_sync.py``
(``make csp-check`` / ``make csp-sync``) keeps the three identical. The policy currently served (= infra/csp.txt,
embedded verbatim so csp_sync can tell when this description goes stale — update it together with csp.txt):

default-src 'self'; script-src 'self' https://www.gstatic.com/firebasejs/12.3.0/ https://apis.google.com/js/ https://apis.google.com/_/scs/ https://js.stripe.com https://*.js.stripe.com 'sha256-D9BZJ0AXKj/x8t8OlNhu5edJdPzYzh2u1oizKkm2Ep8='; style-src 'self' https://fonts.googleapis.com; img-src 'self' data:; font-src https://fonts.gstatic.com; connect-src 'self' https://api.aijalon.trade https://api.hyperliquid.xyz https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://api.stripe.com; frame-src 'self' https://js.stripe.com https://*.js.stripe.com https://hooks.stripe.com; object-src 'none'; base-uri 'none'; form-action 'self'; require-trusted-types-for 'script'; frame-ancestors 'none'; upgrade-insecure-requests; report-uri https://api.aijalon.trade/v1/csp-report; report-to csp

``web_csp()`` below is a REFERENCE builder (unit-tested) of the same directive set; it is not what Hosting serves and
does not know the build-time parts (pinned SDK version path, import-map hash, Trusted Types, reporting). The API adds
``api_security_headers()`` to every response (its own, much stricter JSON-only CSP).

Host-by-host rationale for the web CSP (VERIFY in a staging browser before launch):

script-src
  'self'                              our bundle (tsc -> web/dist). No inline script except the import map below, no
                                      eval.
  https://www.gstatic.com/firebasejs/<version>/   Firebase JS SDK, PATH-SCOPED to the pinned version (SPEC §9;
                                      SECURITY M2): all of www.gstatic.com hosts many Google scripts. The modules are
                                      also SRI-pinned through the import map (web/sri.json).
  'sha256-…'                          the inline <script type=importmap> carrying those SRI hashes (production builds
                                      refuse to build without web/sri.json).
  https://apis.google.com/js/, https://apis.google.com/_/scs/   gapi loader + its chunks, used by Firebase Auth
                                      signInWithPopup/Redirect for the hidden auth iframe. Path-scoped (SECURITY M2);
                                      known risk: apis.google.com has historically offered CSP-bypass gadgets.
  https://js.stripe.com, https://*.js.stripe.com   Stripe.js / Payment Element (Stripe serves some assets from
                                      subdomains of js.stripe.com).
require-trusted-types-for 'script'    Trusted Types: script sinks only through a policy (public/boot-guard.js
                                      installs the default one) — the enforcing control behind the DOM tripwires.
frame-src
  'self'                              Firebase auth iframe: authDomain = aijalon.trade (Firebase Hosting serves
                                      /__/auth/*; avoids third-party-storage breakage in Safari/Chrome). A different
                                      authDomain / <project>.firebaseapp.com is added by the build only when
                                      configured — never ``https://*.firebaseapp.com``.
  https://js.stripe.com, https://*.js.stripe.com, https://hooks.stripe.com   Payment Element + 3-D Secure frames.
  NOT included: https://appleid.apple.com. With Firebase, Apple sign-in runs in the popup / top-level redirect
  (authDomain/__/auth/handler -> appleid.apple.com), never in a frame of our page.
connect-src
  'self', https://api.aijalon.trade, https://api.hyperliquid.xyz
  https://identitytoolkit.googleapis.com, https://securetoken.googleapis.com   Firebase Auth REST (sign-in, MFA
                                      enrolment/sign-in incl. TOTP, token refresh). Enumerated instead of
                                      ``https://*.googleapis.com``, which would include storage.googleapis.com — an
                                      exfiltration channel to any attacker-owned bucket after an XSS.
  https://api.stripe.com              Stripe.js API calls from the page.
  If WalletConnect is added later it needs its relay (wss://relay.walletconnect.com / .org) — not included.
img-src 'self' data:   style-src 'self' https://fonts.googleapis.com   font-src https://fonts.gstatic.com
  Stripe renders its UI inside its own iframes, so its images/fonts/styles are governed by Stripe's CSP, not ours.
  UNVERIFIED: whether Stripe.js injects a <style> element into the host page (would need a hash or
  'unsafe-inline' in style-src). Test in staging; do not add 'unsafe-inline' for scripts under any circumstances.
object-src 'none'; base-uri 'none'; form-action 'self'.
Header-only: frame-ancestors 'none'; upgrade-insecure-requests; report-uri https://api.aijalon.trade/v1/csp-report
  + report-to csp (Reporting-Endpoints header) — the api's rate-limited, sanitising POST /v1/csp-report.

Other launch notes:
* Apple Pay via Stripe needs /.well-known/apple-developer-merchantid-domain-association served by Firebase Hosting
  and the domain registered in Stripe.
* Cross-Origin-Opener-Policy for the web is `same-origin-allow-popups`: plain `same-origin` breaks Firebase
  signInWithPopup (the SDK must observe the popup).
* Permissions-Policy deliberately does not set `payment` (Stripe's iframes need it delegated) nor `hid`/`usb`
  (admins sign payouts with a hardware wallet in the browser, SPEC §2.1).
"""
from __future__ import annotations

from typing import Iterable, Mapping

__all__ = ["API_SECURITY_HEADERS", "api_security_headers", "web_csp", "web_security_headers", "WEB_CSP_DIRECTIVE_ORDER"]

HSTS = "max-age=63072000; includeSubDomains; preload"

API_SECURITY_HEADERS: dict[str, str] = {
    "Strict-Transport-Security": HSTS,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # JSON only: nothing may load, frame, or be framed.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "Cross-Origin-Resource-Policy": "same-site",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), browsing-topics=()",
    "Cache-Control": "no-store",
}


def api_security_headers(*, cacheable_public: bool = False, max_age_s: int = 60) -> dict[str, str]:
    """Headers for every API response. `cacheable_public=True` only for anonymous /v1/public/* GETs."""
    h = dict(API_SECURITY_HEADERS)
    if cacheable_public:
        h["Cache-Control"] = f"public, max-age={int(max_age_s)}"
    return h


_SAME_ORIGIN_AUTH_DOMAINS = frozenset({"aijalon.trade"})

WEB_CSP_DIRECTIVE_ORDER = (
    "default-src", "script-src", "style-src", "img-src", "font-src", "connect-src", "frame-src", "object-src",
    "base-uri", "form-action", "frame-ancestors",
)


def _web_csp_directives(api_origin: str, firebase_project_id: str | None, firebase_auth_domain: str | None) -> dict[str, list[str]]:
    frame = ["'self'"]  # covers authDomain == the web origin (Firebase Hosting serves /__/auth/*)
    if firebase_auth_domain:
        if firebase_auth_domain not in _SAME_ORIGIN_AUTH_DOMAINS:
            _check_source(f"https://{firebase_auth_domain}")
            frame.append(f"https://{firebase_auth_domain}")
    elif firebase_project_id:
        frame.append(f"https://{firebase_project_id}.firebaseapp.com")
    frame += ["https://js.stripe.com", "https://*.js.stripe.com", "https://hooks.stripe.com"]
    return {
        "default-src": ["'self'"],
        "script-src": ["'self'", "https://www.gstatic.com/firebasejs/", "https://apis.google.com",
                       "https://js.stripe.com", "https://*.js.stripe.com"],
        "style-src": ["'self'", "https://fonts.googleapis.com"],
        "img-src": ["'self'", "data:"],
        "font-src": ["https://fonts.gstatic.com"],
        "connect-src": ["'self'", api_origin, "https://api.hyperliquid.xyz",
                        "https://identitytoolkit.googleapis.com", "https://securetoken.googleapis.com",
                        "https://api.stripe.com"],
        "frame-src": frame,
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
    }


def _check_source(src: str) -> None:
    bad = ("'unsafe-inline'", "'unsafe-eval'", "*", "http:", "https:", "'unsafe-hashes'", "blob:")
    if src in bad or src.startswith("http://") or any(c in src for c in ";, \t\r\n"):
        raise ValueError(f"refusing unsafe CSP source {src!r}")


def web_csp(*, api_origin: str = "https://api.aijalon.trade", firebase_project_id: str | None = None,
            firebase_auth_domain: str | None = None, report_uri: str | None = None,
            extra: Mapping[str, Iterable[str]] | None = None) -> str:
    """CSP for the SPA. `firebase_auth_domain` = the web config's authDomain (e.g. "aijalon.trade" or
    "<project>.firebaseapp.com"); `extra` adds sources after review (unsafe keywords/wildcards are refused)."""
    _check_source(api_origin)
    d = _web_csp_directives(api_origin, firebase_project_id, firebase_auth_domain)
    for name, sources in (extra or {}).items():
        if name not in d:
            raise ValueError(f"unknown/unsupported directive {name!r}")
        for s in sources:
            _check_source(s)
            if s not in d[name]:
                d[name].append(s)
    parts = [f"{k} {' '.join(d[k])}" for k in WEB_CSP_DIRECTIVE_ORDER]
    parts.append("upgrade-insecure-requests")
    if report_uri:
        _check_source(report_uri)
        parts.append(f"report-uri {report_uri}")
    return "; ".join(parts)


def web_security_headers(csp: str) -> dict[str, str]:
    """Headers for Firebase Hosting (all paths)."""
    return {
        "Content-Security-Policy": csp,
        "Strict-Transport-Security": HSTS,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin",
        "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), browsing-topics=()",
    }

"""HTTPS-only replacement for ``urllib.request.urlopen`` (standard library only; also shipped in the sandbox image).

``urllib.request.urlopen`` dispatches on the URL scheme through a default opener that also handles ``file:``,
``ftp:`` and ``data:`` URLs, and its redirect handler follows ``ftp:`` redirects (bandit B310, CWE-22). Every
outbound call in this codebase goes to an https endpoint (Hyperliquid info API, Google certs, KYC provider,
Cloud Run sandbox), so the opener below registers ONLY the HTTPS handler: a non-https URL is rejected before any
I/O, and a redirect to any other scheme fails with ``URLError("unknown url type")``. TLS certificates and host
names are verified (``ssl.create_default_context()``, the same default ``urlopen`` uses). Behaviour is otherwise
identical to ``urlopen``: non-2xx responses raise ``urllib.error.HTTPError``, http(s) redirects are followed.
Environment proxies are deliberately not honoured (Cloud Run has none; egress is controlled by the VPC).
"""
from __future__ import annotations

import ssl
import urllib.parse
import urllib.request
from typing import Any

__all__ = ["https_open"]

_OPENER = urllib.request.OpenerDirector()
for _h in (urllib.request.UnknownHandler(),
           urllib.request.HTTPSHandler(context=ssl.create_default_context()),
           urllib.request.HTTPDefaultErrorHandler(),
           urllib.request.HTTPRedirectHandler(),
           urllib.request.HTTPErrorProcessor()):
    _OPENER.add_handler(_h)


def https_open(req: urllib.request.Request | str, *, timeout: float) -> Any:
    """Open an https URL (``Request`` or string). Raises ``ValueError`` for any other scheme."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise ValueError("only https URLs may be opened")
    return _OPENER.open(req, timeout=timeout)

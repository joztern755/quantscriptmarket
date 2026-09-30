/* Runs FIRST, before any other script (external file: CSP forbids inline script). SECURITY M1 + M2.
 *
 * 1. Frame-buster (defence in depth behind X-Frame-Options / frame-ancestors, which Hosting sends on every path
 *    except Firebase's own /__/auth/*): the SPA is never meant to be framed. If it is, hide it and do not boot
 *    (core main.ts checks window.__aijFramed and refuses to start).
 * 2. Trusted Types default policy (CSP `require-trusted-types-for 'script'`): our own code has no HTML/script sinks
 *    (web/build.mjs scans for them), so this policy only matters for third-party code (Firebase/gapi, Stripe.js):
 *      - createScriptURL: only our origin, the Firebase SDK path, gapi, and Stripe — anything else is refused;
 *      - createScript: always refused (no eval-like sinks);
 *      - createHTML: passed through unchanged (no-op) so third-party widgets keep working.
 */
(function () {
  "use strict";
  var framed = true;
  try { framed = window.top !== window.self; } catch (e) { framed = true; }
  if (framed) {
    window.__aijFramed = true;
    try { document.documentElement.style.setProperty("display", "none", "important"); } catch (e) { /* ignore */ }
  }
  var tt = window.trustedTypes;
  if (!tt || typeof tt.createPolicy !== "function") return;
  var PREFIXES = [
    location.origin + "/",
    "https://www.gstatic.com/firebasejs/",
    "https://apis.google.com/js/",
    "https://apis.google.com/_/scs/",
    "https://js.stripe.com/"
  ];
  function allowedScriptUrl(s) {
    var u;
    try { u = new URL(String(s), document.baseURI); } catch (e) { return false; }
    if (u.protocol !== "https:" && u.origin !== location.origin) return false;
    var href = u.href;
    for (var i = 0; i < PREFIXES.length; i++) if (href.indexOf(PREFIXES[i]) === 0) return true;
    return /\.js\.stripe\.com$/.test(u.hostname) && u.protocol === "https:";
  }
  try {
    tt.createPolicy("default", {
      createHTML: function (s) { return s; },
      createScript: function () { throw new TypeError("aijalon: dynamic script evaluation is not allowed"); },
      createScriptURL: function (s) {
        if (allowedScriptUrl(s)) return s;
        throw new TypeError("aijalon: script URL not allowed by the Trusted Types policy");
      }
    });
  } catch (e) { /* a default policy already exists (should never happen): CSP still enforces script-src */ }
})();

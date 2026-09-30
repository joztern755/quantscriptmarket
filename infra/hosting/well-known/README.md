# Files served under https://aijalon.trade/.well-known/

The deploy workflow copies every file in this folder (except this README) to `web/dist/.well-known/` before
`firebase deploy`. Firebase Hosting would otherwise not publish a dot-folder from a build.

Required for Apple Pay through Stripe:

* `apple-developer-merchantid-domain-association` — download it from Stripe Dashboard → Settings →
  Payment method domains → Add `aijalon.trade` (Stripe shows the file link), save it here **unchanged**
  (no extension, no newline edits), commit, deploy, then click "Verify" in Stripe. The deploy smoke test
  checks the URL returns 200.

Optional: `security.txt` (RFC 9116) with a security contact and an `Expires:` line.

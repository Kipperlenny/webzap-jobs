# Security policy

Please report vulnerabilities privately by email to **kontakt@webzap.eu** instead of opening a public issue.
You'll get an answer within a few days. Please give us reasonable time to fix the issue before disclosing it.

How the app protects data (see also the README):
- **Stored data:** email addresses and texts are encrypted at rest (Fernet). Emails are looked up via an HMAC hash.
  Every link token is stored only as a SHA-256 hash.
- **No accounts:** tokenized links do everything. Links that change data show a page first, and the change happens
  with a POST, so mail scanners that open links can't trigger anything.
- **Website:** no cookies and no third-party resources. It sends strict security headers (CSP, HSTS,
  `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex` on token pages).
- **Outgoing link checks:** they refuse non-public addresses (SSRF protection).
- **Containers:** read-only, running as a non-root user, with all capabilities dropped.

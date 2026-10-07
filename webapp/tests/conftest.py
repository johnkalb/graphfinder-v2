"""Shared test setup.

Production trusts only Cloudflare's signed Access JWT; the tests authenticate
with the plain Cf-Access-Authenticated-User-Email header instead, which
_request_user_email() honours only with this opt-in."""
import os

os.environ.setdefault("CF_ACCESS_TRUST_EMAIL_HEADER", "1")

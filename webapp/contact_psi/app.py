"""State and policy helpers used by the FastAPI integration."""
from __future__ import annotations
import datetime
import os
from collections import defaultdict
import pysodium
from . import oprf

REQUEST_MAX_POINTS = 3000
DAILY_MAX_POINTS = 5000
_usage_counters = {}
_clock = lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
_secret = None
_key_version = None
_manifest = None
_manifest_meta = {}

def configure(secret_scalar, key_version, manifest, metadata=None):
    global _secret, _key_version, _manifest, _manifest_meta
    _secret, _key_version, _manifest = secret_scalar, int(key_version), manifest
    _manifest_meta = metadata or {"key_version": _key_version}

def load_configuration():
    global _secret, _key_version
    if _secret is None:
        raw = os.environ.get("CONTACT_PSI_SERVER_SECRET")
        if not raw:
            raise RuntimeError("CONTACT_PSI_SERVER_SECRET is not configured")
        import base64
        _secret = base64.b64decode(raw)
        _key_version = int(os.environ.get("CONTACT_PSI_KEY_VERSION", "1"))
    return _secret, _key_version

def current_manifest_meta():
    return dict(_manifest_meta)

def evaluate(points: list[bytes], key_version: int) -> list[bytes]:
    secret, configured_version = load_configuration()
    if int(key_version) != configured_version:
        raise ValueError("unknown key version")
    return [oprf.eval_s(secret, point) for point in points]

def consume_quota(user: str, count: int) -> bool:
    now = _clock()
    day = now.date().isoformat()
    key = (user or "anonymous", day)
    used = _usage_counters.get(key, 0)
    if used + count > DAILY_MAX_POINTS:
        return False
    _usage_counters[key] = used + count
    return True

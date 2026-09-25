"""Root pytest conftest: put the repo root on sys.path so tests can
`import womens_nonprofits_pipeline` (and other root-level modules) regardless
of the directory pytest is invoked from -- mirrors the sys.path.insert(0, ...)
pattern already used by build_index.py etc.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Tests identify users with the plain Cf-Access-Authenticated-User-Email
# header; production ignores it (forgeable by anyone who reaches the origin
# directly) and trusts only the signed JWT. See pathfinder._request_user_email.
os.environ.setdefault("CF_ACCESS_TRUST_EMAIL_HEADER", "1")

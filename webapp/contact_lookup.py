"""Contact lookup through the subscriber's own provider account (2026-11-15).

A subscriber pastes their own API key for a contact-data provider (Hunter,
Apollo, People Data Labs, RocketReach). "Find contact info" on a person then
asks that provider, under that key, for the person's email / phone / address,
and shows the answer. hooconnex is only the conduit:

  * results are NEVER stored, cached or logged -- only an audit line saying
    who looked up whom, with which provider, in which mode, and how many
    items came back (no values);
  * keys are stored encrypted (AES-GCM), shown back only as their last 4
    characters, and a workspace admin can set one key for the whole team;
  * two modes: "work" (work email / office phone) and "personal" (personal
    email, mobile, home address). Personal lookups are capped per day and are
    refused for people in protected roles -- judges, law enforcement,
    prosecutors, election officials -- whose home details many laws shield
    (the caller supplies the protected check, which needs the graph);
  * every lookup needs the user's current certification of permissible
    purpose: no FCRA uses (employment, tenant, credit or insurance
    screening), no harassment or stalking, follow the provider's terms and
    anti-spam / do-not-call law.

Providers without a usable API here (Whitepages, TruePeopleSearch, LinkedIn,
Google) are offered as links that open the provider's own site in a new tab
(`links`), under the same protected-role and certification rules.

Pure functions over the app DB (db.connect, '?' placeholders), like
workspaces.py, so tests run on SQLite and prod on Postgres. HTTP goes
through `http` (requests by default), which tests replace.
"""
import base64
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlencode

try:
    import db
except ImportError:  # imported as webapp.contact_lookup
    from webapp import db

WORK, PERSONAL = "work", "personal"
MODES = (WORK, PERSONAL)
DAILY_CAP = {WORK: 200, PERSONAL: 25}
TIMEOUT_S = 25

CERT_VERSION = "2026-11-15"
CERT_STATEMENTS = [
    "I will use contact details found here only for a legitimate business, professional, fundraising, "
    "journalistic or personal-reconnection purpose.",
    "I will NOT use them to decide anyone's eligibility for employment, housing, credit, insurance or any other "
    "purpose covered by the Fair Credit Reporting Act (FCRA). hooconnex is not a consumer reporting agency.",
    "I will not use them to harass, stalk, threaten or intimidate anyone, or to locate someone who does not want "
    "to be found.",
    "I will follow my provider's terms of service and the laws on email marketing, calls and texts (for example "
    "CAN-SPAM, the TCPA and do-not-call rules, GDPR/CCPA where they apply).",
    "Lookups run under my (or my team's) own provider account. hooconnex does not store the results, and logs "
    "who looked up whom so misuse can be investigated.",
]
PURPOSES = ["Business development / sales", "Recruiting a candidate who has applied or opted in",
            "Fundraising / donor outreach", "Journalism / research", "Reconnecting with someone I know", "Other"]

# what each API provider can return; personal-capable providers need the certification + protected check
PROVIDERS = {
    "hunter": {"label": "Hunter", "modes": [WORK], "site": "https://hunter.io/api-keys",
               "note": "Work email from name + company domain."},
    "apollo": {"label": "Apollo", "modes": [WORK, PERSONAL], "site": "https://app.apollo.io/#/settings/integrations/api",
               "note": "Work email; personal email in personal mode. (Phone numbers need Apollo's async reveal -- "
                       "not supported yet.)"},
    "pdl": {"label": "People Data Labs", "modes": [WORK, PERSONAL], "site": "https://dashboard.peopledatalabs.com/",
            "note": "Work email; personal emails, phones and address in personal mode."},
    "rocketreach": {"label": "RocketReach", "modes": [WORK, PERSONAL], "site": "https://rocketreach.co/api",
                    "note": "Work/personal emails and phones. A fresh lookup can take a minute -- run it again."},
}

FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com",
            "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "gmx.com",
            "gmx.net", "mail.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "fastmail.com",
            "yandex.ru", "qq.com", "163.com"}


class LookupError_(Exception):
    """A refused request; .status is the HTTP status to return."""
    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def _now():
    return datetime.now(timezone.utc)


# --- key encryption ----------------------------------------------------------------

def _master_key():
    """32 bytes from CONTACT_LOOKUP_SECRET, else derived (domain-separated) from
    CONTACT_PSI_SERVER_SECRET so prod needs no new secret. Rotating the source
    secret makes stored keys unreadable: users re-enter them."""
    s = os.environ.get("CONTACT_LOOKUP_SECRET") or os.environ.get("CONTACT_PSI_SERVER_SECRET")
    if not s:
        raise LookupError_("contact lookup is not configured on this server", 503)
    return hashlib.sha256(b"hooconnex-contact-lookup-keys-v1|" + s.encode()).digest()


def _aad(owner, ws_id, provider):
    return f"{owner or ''}|{ws_id or ''}|{provider}".encode()


def encrypt_key(api_key, owner, ws_id, provider):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_master_key()).encrypt(nonce, api_key.encode(), _aad(owner, ws_id, provider))
    return base64.b64encode(nonce + ct).decode()


def decrypt_key(blob, owner, ws_id, provider):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob)
    return AESGCM(_master_key()).decrypt(raw[:12], raw[12:], _aad(owner, ws_id, provider)).decode()


# --- workspace membership (same rule as workspaces._member: outsiders get 404) ---------

def _ws_role(conn, ws_id, email):
    r = conn.execute("SELECT m.role FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id "
                     "WHERE m.workspace_id = ? AND m.email = ? AND m.removed_at IS NULL AND w.deleted_at IS NULL",
                     (ws_id, email)).fetchone()
    if r is None:
        raise LookupError_("Not Found", 404)
    return r["role"]


def _ws_audit(conn, ws_id, email, action, detail):
    conn.execute("INSERT INTO ws_audit (workspace_id, email, action, detail, at) VALUES (?, ?, ?, ?, ?)",
                 (ws_id, email, action, json.dumps(detail), _now().isoformat()))


# --- keys --------------------------------------------------------------------------

def save_key(db_path, email, provider, api_key, ws_id=None):
    if provider not in PROVIDERS:
        raise LookupError_("unknown provider")
    api_key = (api_key or "").strip()
    if not (8 <= len(api_key) <= 400) or any(c.isspace() for c in api_key):
        raise LookupError_("that doesn't look like an API key")
    conn = db.connect(db_path)
    try:
        owner = None if ws_id else email
        if ws_id and _ws_role(conn, ws_id, email) != "admin":
            raise LookupError_("only a workspace admin can set the team's key", 403)
        blob = encrypt_key(api_key, owner, ws_id, provider)
        _delete_keys(conn, owner, ws_id, provider)
        conn.execute("INSERT INTO lookup_keys (owner_email, workspace_id, provider, ciphertext, last4, set_by, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (owner, ws_id, provider, blob, api_key[-4:], email, _now().isoformat()))
        if ws_id:
            _ws_audit(conn, ws_id, email, "lookup_key_set", {"provider": provider})
        conn.commit()
    finally:
        conn.close()


def _delete_keys(conn, owner, ws_id, provider):
    if ws_id:
        conn.execute("UPDATE lookup_keys SET deleted_at = ?, ciphertext = '' WHERE workspace_id = ? AND provider = ? "
                     "AND deleted_at IS NULL", (_now().isoformat(), ws_id, provider))
    else:
        conn.execute("UPDATE lookup_keys SET deleted_at = ?, ciphertext = '' WHERE owner_email = ? "
                     "AND workspace_id IS NULL AND provider = ? AND deleted_at IS NULL",
                     (_now().isoformat(), owner, provider))


def delete_key(db_path, email, provider, ws_id=None):
    conn = db.connect(db_path)
    try:
        if ws_id and _ws_role(conn, ws_id, email) != "admin":
            raise LookupError_("only a workspace admin can remove the team's key", 403)
        _delete_keys(conn, None if ws_id else email, ws_id, provider)
        if ws_id:
            _ws_audit(conn, ws_id, email, "lookup_key_removed", {"provider": provider})
        conn.commit()
    finally:
        conn.close()


def _key_rows(conn, email, ws_ids):
    rows = conn.execute("SELECT provider, workspace_id, last4, ciphertext FROM lookup_keys "
                        "WHERE deleted_at IS NULL AND owner_email = ? AND workspace_id IS NULL", (email,)).fetchall()
    rows = [dict(r) for r in rows]
    for w in ws_ids:
        rows += [dict(r) for r in conn.execute(
            "SELECT provider, workspace_id, last4, ciphertext FROM lookup_keys "
            "WHERE deleted_at IS NULL AND workspace_id = ?", (w,)).fetchall()]
    return rows


def _get_key(conn, email, provider, ws_id):
    if ws_id:
        _ws_role(conn, ws_id, email)
        r = conn.execute("SELECT ciphertext FROM lookup_keys WHERE workspace_id = ? AND provider = ? "
                         "AND deleted_at IS NULL", (ws_id, provider)).fetchone()
        owner = None
    else:
        r = conn.execute("SELECT ciphertext FROM lookup_keys WHERE owner_email = ? AND workspace_id IS NULL "
                         "AND provider = ? AND deleted_at IS NULL", (email, provider)).fetchone()
        owner = email
    if r is None:
        raise LookupError_(f"no {PROVIDERS[provider]['label']} API key saved"
                           + (" for this workspace" if ws_id else ""), 400, "no_key")
    try:
        return decrypt_key(r["ciphertext"], owner, ws_id, provider)
    except Exception:
        raise LookupError_("the saved key can't be read any more -- please enter it again", 409, "no_key")


# --- certification ------------------------------------------------------------------

def certification(conn, email):
    r = conn.execute("SELECT purpose, accepted_at FROM lookup_certifications WHERE email = ? AND version = ? "
                     "ORDER BY accepted_at DESC", (email, CERT_VERSION)).fetchone()
    return dict(r) if r else None


def certify(db_path, email, purpose, accepted):
    if not accepted:
        raise LookupError_("please tick the box to accept the certification")
    if purpose not in PURPOSES:
        raise LookupError_("please pick your main purpose")
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO lookup_certifications (email, version, purpose, accepted_at) VALUES (?, ?, ?, ?)",
                     (email, CERT_VERSION, purpose, _now().isoformat()))
        conn.commit()
    finally:
        conn.close()


# --- status / history ------------------------------------------------------------------

def used_today(conn, email):
    since = (_now() - timedelta(days=1)).isoformat()
    rows = conn.execute("SELECT mode, count(*) AS n FROM lookup_audit WHERE email = ? AND at >= ? AND outcome <> 'refused' "
                        "GROUP BY mode", (email, since)).fetchall()
    used = {m: 0 for m in MODES}
    used.update({r["mode"]: r["n"] for r in rows})
    return used


def status(db_path, email, workspaces):
    """workspaces: [{id, name, role}] the user belongs to."""
    conn = db.connect(db_path)
    try:
        keys = _key_rows(conn, email, [w["id"] for w in workspaces])
        cert = certification(conn, email)
        used = used_today(conn, email)
    finally:
        conn.close()
    provs = []
    for pid, p in PROVIDERS.items():
        mine = next((k for k in keys if k["provider"] == pid and k["workspace_id"] is None), None)
        team = {k["workspace_id"]: k["last4"] for k in keys if k["provider"] == pid and k["workspace_id"]}
        provs.append({"id": pid, "label": p["label"], "modes": p["modes"], "note": p["note"], "site": p["site"],
                      "my_key": ("…" + mine["last4"]) if mine else None,
                      "team_keys": {str(w): "…" + l4 for w, l4 in team.items()}})
    return {"certified": cert, "cert_version": CERT_VERSION, "cert_statements": CERT_STATEMENTS,
            "purposes": PURPOSES, "providers": provs, "workspaces": workspaces,
            "daily_cap": DAILY_CAP, "used_today": used}


def history(db_path, email, limit=100):
    conn = db.connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT person, provider, mode, workspace_id, outcome, n_items, at FROM lookup_audit WHERE email = ? "
            "ORDER BY id DESC LIMIT ?", (email, int(limit))).fetchall()]
    finally:
        conn.close()


def _audit(conn, email, ws_id, person, provider, mode, outcome, n_items=0):
    conn.execute("INSERT INTO lookup_audit (email, workspace_id, person, provider, mode, outcome, n_items, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (email, ws_id, person, provider, mode, outcome, int(n_items), _now().isoformat()))
    if ws_id:
        _ws_audit(conn, ws_id, email, "contact_lookup",
                  {"person": person, "provider": provider, "mode": mode, "outcome": outcome})


# --- the lookup -----------------------------------------------------------------------

def split_name(name):
    parts = [p for p in re.sub(r"\s*\(.*\)\s*$", "", name or "").replace(",", " ").split() if p]
    parts = [p for p in parts if p.lower().rstrip(".") not in {"jr", "sr", "ii", "iii", "iv", "dr", "mr", "ms", "mrs"}]
    if len(parts) < 2:
        return (parts[0] if parts else ""), ""
    return parts[0], parts[-1]


def base_name(node):
    """'Mark Greene (Ibm)' -> 'Mark Greene'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", node or "").strip()


def _clean_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    d = re.sub(r"^www\.", "", d)
    return d if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d) else ""


def _email_kind(addr, typ=None):
    t = (typ or "").lower()
    if t in ("personal",):
        return PERSONAL
    if t in ("professional", "work", "current_professional"):
        return WORK
    dom = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
    return PERSONAL if dom in FREEMAIL else WORK


def _item(typ, kind, value, detail=None):
    v = str(value).strip()
    return {"type": typ, "kind": kind, "value": v, "detail": detail} if v else None


def _strs(x, *keys):
    """Values from a string / list of strings / list of dicts (first matching key)."""
    if x is None:
        return []
    if isinstance(x, (str, int)):
        return [str(x)]
    out = []
    if isinstance(x, list):
        for v in x:
            if isinstance(v, dict):
                for k in keys:
                    if v.get(k):
                        out.append(str(v[k]))
                        break
            elif v:
                out.append(str(v))
    return out


def _request(provider, api_key, q, mode):
    """(method, url, params, headers) for one provider call."""
    first, last = q["first"], q["last"]
    if provider == "hunter":
        p = {"first_name": first, "last_name": last}
        if q["domain"]:
            p["domain"] = q["domain"]
        elif q["company"]:
            p["company"] = q["company"]
        if q["linkedin"]:
            p["linkedin_handle"] = q["linkedin"]
        if not (q["domain"] or q["company"] or q["linkedin"]):
            raise LookupError_("Hunter needs a company or its web domain")
        return "GET", "https://api.hunter.io/v2/email-finder", p, {"X-API-KEY": api_key}
    if provider == "apollo":
        p = {"first_name": first, "last_name": last}
        if q["company"]:
            p["organization_name"] = q["company"]
        if q["domain"]:
            p["domain"] = q["domain"]
        if q["linkedin"]:
            p["linkedin_url"] = q["linkedin"]
        if mode == PERSONAL:
            p["reveal_personal_emails"] = "true"
        return "POST", "https://api.apollo.io/api/v1/people/match", p, {"x-api-key": api_key}
    if provider == "pdl":
        p = {"name": q["name"], "min_likelihood": 6}
        if q["domain"] or q["company"]:
            p["company"] = q["domain"] or q["company"]
        if q["linkedin"]:
            p["profile"] = q["linkedin"]
        if q["location"]:
            p["location"] = q["location"]
        return "GET", "https://api.peopledatalabs.com/v5/person/enrich", p, {"X-Api-Key": api_key}
    if provider == "rocketreach":
        p = {}
        if q["linkedin"]:
            p["linkedin_url"] = q["linkedin"]
        if q["company"]:
            p.update(name=q["name"], current_employer=q["company"])
        if not p:
            raise LookupError_("RocketReach needs the person's current employer or LinkedIn URL")
        return "GET", "https://api.rocketreach.co/api/v2/person/lookup", p, {"Api-Key": api_key}
    raise LookupError_("unknown provider")


def _parse(provider, code, body):
    """(outcome, items, confidence, note). outcome: found / no_match / pending /
    opted_out. Raises LookupError_ for key / credit / rate problems."""
    if code in (401, 403):
        raise LookupError_(f"{PROVIDERS[provider]['label']} rejected the API key", 400, "bad_key")
    if code == 402:
        raise LookupError_(f"your {PROVIDERS[provider]['label']} account is out of credits", 402)
    if code == 429:
        raise LookupError_(f"{PROVIDERS[provider]['label']} says too many requests -- try again shortly", 429)
    if code == 451:
        return "opted_out", [], None, "This person has asked the provider not to process their data."
    if code == 404:
        return "no_match", [], None, None
    if code >= 400 or not isinstance(body, dict):
        raise LookupError_(f"{PROVIDERS[provider]['label']} returned an error (HTTP {code})", 502)
    items = []
    conf = note = None
    if provider == "hunter":
        d = body.get("data") or {}
        if d.get("email"):
            items.append(_item("email", WORK, d["email"], (d.get("verification") or {}).get("status")))
        if d.get("phone_number"):
            items.append(_item("phone", WORK, d["phone_number"]))
        conf = d.get("score")
    elif provider == "apollo":
        p = body.get("person") or {}
        conf = p.get("match_confidence")
        if not p or conf == "none":
            return "no_match", [], conf, None
        seen = set()
        for addr, st in [(p.get("email"), p.get("email_status"))] + \
                [(e, None) for e in _strs((p.get("contact") or {}).get("contact_emails"), "email")]:
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                items.append(_item("email", _email_kind(addr), addr, st))
        addr = (p.get("contact") or {}).get("present_raw_address")
        if addr:
            items.append(_item("address", PERSONAL, addr))
    elif provider == "pdl":
        d = body.get("data") or {}
        conf = body.get("likelihood")
        if d.get("work_email"):
            items.append(_item("email", WORK, d["work_email"]))
        for e in _strs(d.get("personal_emails")) + _strs(d.get("recommended_personal_email")):
            items.append(_item("email", PERSONAL, e))
        for e in _strs(d.get("emails"), "address"):
            items.append(_item("email", _email_kind(e), e))
        for ph in _strs(d.get("mobile_phone")):
            items.append(_item("phone", PERSONAL, ph, "mobile"))
        for ph in _strs(d.get("phone_numbers"), "number"):
            items.append(_item("phone", PERSONAL, ph))
        street = d.get("location_street_address")
        if street:
            loc = ", ".join(str(x) for x in (street, d.get("location_locality"), d.get("location_region"),
                                             d.get("location_postal_code")) if x)
            items.append(_item("address", PERSONAL, loc))
    elif provider == "rocketreach":
        st = body.get("status")
        if st and st != "complete":
            return "pending", [], None, "RocketReach is still searching -- run the lookup again in a minute."
        for e in body.get("emails") or []:
            if isinstance(e, dict) and e.get("email"):
                items.append(_item("email", _email_kind(e["email"], e.get("type")), e["email"], e.get("grade")))
        for k, kind in (("current_work_email", WORK), ("current_personal_email", PERSONAL)):
            if body.get(k):
                items.append(_item("email", kind, body[k]))
        for ph in body.get("phones") or []:
            if isinstance(ph, dict) and (ph.get("number") or ph.get("e164")):
                t = (ph.get("type") or "").lower()
                items.append(_item("phone", WORK if "direct" in t or "office" in t else PERSONAL,
                                   ph.get("number") or ph.get("e164"), ph.get("type")))
        if body.get("current_employer_phone"):
            items.append(_item("phone", WORK, body["current_employer_phone"], "company line"))
    # de-duplicate
    out, seen = [], set()
    for it in items:
        if it and (it["type"], it["value"].lower()) not in seen:
            seen.add((it["type"], it["value"].lower()))
            out.append(it)
    return ("found" if out else "no_match"), out, conf, note


def _http_default(method, url, params, headers):
    import requests
    r = requests.request(method, url, params=params, headers={**headers, "Accept": "application/json"},
                         timeout=TIMEOUT_S)
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body


http = _http_default


def run_lookup(db_path, email, node, provider, mode, inputs, protected_reason=None, ws_id=None):
    """Ask `provider` about graph person `node`. inputs: company / domain /
    linkedin / location typed or confirmed by the user (the NAME always comes
    from the node, so the protected-role check can't be sidestepped by typing
    a different name). protected_reason: why `node` is a protected person, or
    None. Returns {outcome, items, confidence, note}; nothing is stored."""
    if provider not in PROVIDERS:
        raise LookupError_("unknown provider")
    if mode not in PROVIDERS[provider]["modes"]:
        raise LookupError_(f"{PROVIDERS[provider]['label']} supports only work lookups here")
    name = base_name(node)
    first, last = split_name(name)
    if not last:
        raise LookupError_("a first and last name are needed")
    conn = db.connect(db_path)
    try:
        if certification(conn, email) is None:
            raise LookupError_("please accept the contact-lookup certification first", 428, "certification_required")
        if ws_id:
            _ws_role(conn, ws_id, email)
        if mode == PERSONAL and protected_reason:
            _audit(conn, email, ws_id, node, provider, mode, "refused")
            conn.commit()
            raise LookupError_("Personal contact details can't be looked up for this person: " + protected_reason,
                               403, "protected")
        why = block_reason(block_for(conn, node), mode)
        if why:
            _audit(conn, email, ws_id, node, provider, mode, "refused")
            conn.commit()
            raise LookupError_("Not available: " + why + ".", 403, "blocked")
        if used_today(conn, email)[mode] >= DAILY_CAP[mode]:
            raise LookupError_(f"daily limit reached ({DAILY_CAP[mode]} {mode} lookups in 24 hours)", 429, "cap")
        api_key = _get_key(conn, email, provider, ws_id)
    finally:
        conn.close()

    q = {"name": f"{first} {last}" if provider != "pdl" else name, "first": first, "last": last,
         "company": (inputs.get("company") or "").strip()[:200],
         "domain": _clean_domain(inputs.get("domain")),
         "linkedin": (inputs.get("linkedin") or "").strip()[:300] if re.match(
             r"^(https?://)?([a-z]+\.)?linkedin\.com/in/", (inputs.get("linkedin") or "").strip(), re.I) else "",
         "location": (inputs.get("location") or "").strip()[:120]}
    method, url, params, headers = _request(provider, api_key, q, mode)
    try:
        code, body = http(method, url, params, headers)
    except Exception:
        # never log the response or the key; just that it failed
        outcome, items, conf, note = "error", [], None, None
        err = LookupError_(f"couldn't reach {PROVIDERS[provider]['label']} -- try again", 502)
    else:
        err = None
        try:
            outcome, items, conf, note = _parse(provider, code, body)
        except LookupError_ as e:
            outcome, items, conf, note, err = "error", [], None, None, e
    if mode == WORK:
        items = [i for i in items if i["kind"] == WORK]
    conn = db.connect(db_path)
    try:
        _audit(conn, email, ws_id, node, provider, mode, outcome, len(items))
        conn.commit()
    finally:
        conn.close()
    if err:
        raise err
    return {"outcome": outcome, "items": items, "confidence": conf, "note": note,
            "queried": {k: v for k, v in q.items() if k not in ("first", "last") and v}}


# --- people blocking lookups about themselves ---------------------------------------------
# A person in the graph can switch lookups about themselves off: "personal"
# (home address, personal email, mobile -- the default) or "all". A block
# takes effect at once, since it can only reduce what is shown. It is
# "verified" straight away when the person's name matches the requester's
# account name; otherwise it is "pending" until a site admin confirms it --
# and is enforced while pending. A rejected or removed block stops applying.

BLOCK_SCOPES = (PERSONAL, "all")
_ENFORCED = ("verified", "pending")


def _norm_name(s):
    return " ".join(re.sub(r"[^a-z ]", " ", (s or "").lower()).split())


def block_for(conn, node):
    """The enforced block on `node` ({scope, status}) or None."""
    r = conn.execute("SELECT scope, status FROM lookup_blocks WHERE node = ? AND status IN ('verified', 'pending') "
                     "ORDER BY CASE scope WHEN 'all' THEN 0 ELSE 1 END LIMIT 1", (node,)).fetchone()
    return dict(r) if r else None


def blocked(db_path, node):
    conn = db.connect(db_path)
    try:
        return block_for(conn, node)
    finally:
        conn.close()


def request_block(db_path, email, node, scope, account_name, note=""):
    if scope not in BLOCK_SCOPES:
        raise LookupError_("pick what to block")
    status_ = "verified" if account_name and _norm_name(base_name(node)) == _norm_name(account_name) else "pending"
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE lookup_blocks SET status = 'removed', decided_at = ? WHERE node = ? AND requested_by = ? "
                     "AND status IN ('verified', 'pending')", (_now().isoformat(), node, email))
        conn.execute("INSERT INTO lookup_blocks (node, scope, status, requested_by, note, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (node, scope, status_, email, (note or "")[:1000], _now().isoformat()))
        conn.commit()
    finally:
        conn.close()
    return status_


def my_blocks(db_path, email):
    conn = db.connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, node, scope, status, created_at FROM lookup_blocks WHERE requested_by = ? "
            "AND status IN ('verified', 'pending') ORDER BY id DESC", (email,)).fetchall()]
    finally:
        conn.close()


def remove_block(db_path, email, block_id, is_admin=False):
    conn = db.connect(db_path)
    try:
        r = conn.execute("SELECT requested_by FROM lookup_blocks WHERE id = ?", (block_id,)).fetchone()
        if r is None or (r["requested_by"] != email and not is_admin):
            raise LookupError_("Not Found", 404)
        conn.execute("UPDATE lookup_blocks SET status = 'removed', decided_by = ?, decided_at = ? WHERE id = ?",
                     (email, _now().isoformat(), block_id))
        conn.commit()
    finally:
        conn.close()


def pending_blocks(db_path):
    conn = db.connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, node, scope, requested_by, note, created_at FROM lookup_blocks WHERE status = 'pending' "
            "ORDER BY id").fetchall()]
    finally:
        conn.close()


def decide_block(db_path, admin_email, block_id, approve):
    conn = db.connect(db_path)
    try:
        n = conn.execute("UPDATE lookup_blocks SET status = ?, decided_by = ?, decided_at = ? WHERE id = ? "
                         "AND status = 'pending'", ("verified" if approve else "rejected", admin_email,
                                                    _now().isoformat(), block_id)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        raise LookupError_("Not Found", 404)


def block_reason(block, mode):
    """Why `mode` is refused under `block`, or None."""
    if block and (block["scope"] == "all" or mode == PERSONAL):
        return "this person has asked for " + ("contact lookups" if block["scope"] == "all"
                                               else "personal contact details") + " about them to be switched off"
    return None


# --- links to providers' own sites (no key; same rules) -----------------------------------

def links(node, company="", location="", protected_reason=None, block=None):
    name = base_name(node)
    first, last = split_name(name)
    if block_reason(block, WORK):
        return {WORK: [], PERSONAL: []}
    if block_reason(block, PERSONAL):
        protected_reason = protected_reason or "blocked"
    work = [
        {"label": "LinkedIn", "url": "https://www.linkedin.com/search/results/people/?"
         + urlencode({"keywords": " ".join(x for x in (name, company) if x)})},
        {"label": "Google (email)", "url": "https://www.google.com/search?"
         + urlencode({"q": f'"{name}" ' + (f'"{company}" ' if company else "") + "email"})},
    ]
    personal = []
    if not protected_reason and last:
        personal = [
            {"label": "Whitepages", "url": f"https://www.whitepages.com/name/{quote(first)}-{quote(last)}"
             + (f"/{quote(location.replace(', ', '-').replace(' ', '-'))}" if location else "")},
            {"label": "TruePeopleSearch", "url": "https://www.truepeoplesearch.com/results?"
             + urlencode({"name": f"{first} {last}", **({"citystatezip": location} if location else {})})},
        ]
    return {WORK: work, PERSONAL: personal}

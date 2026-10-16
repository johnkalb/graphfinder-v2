"""AI financing web + Form D sales-compensation recipients -> Postgres (2026-10-15).

Three reversible sets, each with its own source_data:

  AI_DEALS_2026          the hand-sourced deals in exports/ai_deals.py (equity,
                         debt, arranged debt, compute contracts, leases,
                         backstops, chip orders, warrants), one row per
                         (from, to, type) with every deal in the evidence
  AI_COMPANY_LEADERS     founders / CEOs / board members of the neoclouds the
                         graph had little or nothing on (sourced below)
  SEC_FORM_D_RECIPIENTS  Form D "sales compensation recipients": the
                         broker-dealer or placement agent paid to raise each
                         private offering (firm -> issuer), the individual
                         registered rep (rep -> issuer, rep -> firm), and
                         AI-company SPVs (SPV -> the company it buys into).
                         All quarters 2008q1 onward; the harvester
                         (harvest_sec_form_d_bulk.py) only loads RELATEDPERSONS.

  python load_ai_financing.py --zips <dir of {q}_d.zip Form D data sets>            # dry run
  python load_ai_financing.py --zips <dir> --commit                                 # insert
  python load_ai_financing.py --zips <dir> --commit --replace-form-d                # reload Form D: delete its rows + insert
Undo: DELETE FROM relationships WHERE source_data IN ('AI_DEALS_2026',
      'AI_COMPANY_LEADERS', 'SEC_FORM_D_RECIPIENTS');
"""
import argparse
import csv
import glob
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "exports"))
from ai_deals import DEALS  # noqa: E402

# deal-table entity -> the name the graph already uses (its best-connected
# spelling), so deals attach to existing nodes; new names for the rest
DB_NAME = {
    "OpenAI": "OpenAi, Inc.", "Anthropic": "Anthropic", "Microsoft": "Microsoft Corporation",
    "Amazon": "Amazon.com, Inc.", "Alphabet / Google": "GOOGLE LLC", "Meta": "Meta Platforms, Inc.",
    "Oracle": "Oracle Corporation", "NVIDIA": "NVIDIA CORPORATION", "AMD": "Advanced Micro Devices, Inc.",
    "Broadcom": "Broadcom Inc.", "Cerebras": "Cerebras Systems Inc.", "Dell": "Dell Technologies Inc.",
    "Cisco": "Cisco Systems, Inc.", "Tesla": "Tesla, Inc.", "Nokia": "Nokia Corporation", "Lenovo": "Lenovo Group Ltd.",
    "CoreWeave": "CoreWeave, Inc.", "Nebius": "Nebius Group N.V.", "Lambda": "Lambda, Inc./DE", "Crusoe": "Crusoe Inc.",
    "Fluidstack": "Fluidstack Ltd", "IREN": "IREN Ltd", "Applied Digital": "Applied Digital Corp.",
    "Core Scientific": "Core Scientific, Inc.", "Cipher Mining": "Cipher Mining Inc.", "TeraWulf": "TERAWULF INC.",
    "Vantage Data Centers": "Vantage Data Centers Holding Co", "Nscale": "Nscale", "Together AI": "Together AI",
    "Vultr": "Vultr", "Volta": "Volta", "Stargate LLC": "Stargate LLC",
    "Beignet Investor (Hyperion SPV)": "Beignet Investor LLC", "SoftBank": "Softbank Group Corp", "MGX": "MGX",
    "Blackstone": "Blackstone Inc.", "Blue Owl": "Blue Owl Capital Holdings LP", "Magnetar": "Magnetar Capital LLC",
    "Citadel": "Citadel LLC", "Jane Street": "JANE STREET GROUP, LLC", "Point72": "Point72 Asset Management",
    "Apollo": "Apollo Global Management, Inc.", "BlackRock": "BlackRock, Inc.",
    "PIMCO": "Pacific Investment Management Company LLC", "Coatue": "Coatue Management", "GIC": "GIC Private Ltd",
    "ICONIQ": "Iconiq Capital", "D1 Capital Partners": "D1 Capital Partners L.P.", "Capital Group": "The Capital Group Companies, Inc.",
    "D. E. Shaw": "D. E. Shaw & Co., L.P.", "Andreessen Horowitz": "A16Z", "Fidelity": "FMR LLC",
    "Valor Equity Partners": "Valor Equity Partners", "Founders Fund": "Founders Fund", "Mubadala Capital": "Mubadala Capital",
    "Qatar Investment Authority": "Qatar Investment Authority", "Situational Awareness": "Situational Awareness Partners LP",
    "TWG Global": "TWG Global", "Aramco Ventures": "Aramco Ventures", "LuminArx": "LuminArx Capital Management",
    "Aker": "Aker ASA", "8090 Industries": "8090 Industries", "JPMorgan": "JPMorgan Chase & Co.",
    "Morgan Stanley": "Morgan Stanley", "Goldman Sachs": "THE GOLDMAN SACHS GROUP, INC.",
    "MUFG": "Mitsubishi UFJ Financial Group Inc",
}
REL = {"equity": "INVESTED_IN", "debt": "LENT_TO", "arranged": "ARRANGED_FINANCING_FOR", "compute": "COMPUTE_CUSTOMER_OF",
       "lease": "LEASES_FROM", "backstop": "BACKSTOPS", "chips": "CHIP_CUSTOMER_OF", "warrant": "WARRANT_TO"}
SKIP_ENTITIES = {"Bond investors"}            # Oracle's bond buyers: no single counterparty


def db_name(entity, deal_type, date):
    if entity == "SpaceX / xAI":               # xAI merged into SpaceX in early 2026
        return "X.AI Holdings Corp." if date < "2026-03" else "SpaceX"
    if entity == "Amazon" and deal_type in ("compute", "lease"):
        return "Amazon Web Services LLC"
    return DB_NAME[entity]


def deal_rows():
    grouped = defaultdict(list)
    for d in DEALS:
        date, a, b, typ = d[0], d[1], d[2], d[3]
        if a in SKIP_ENTITIES or b in SKIP_ENTITIES:
            continue
        grouped[(db_name(a, typ, date), db_name(b, typ, date), REL[typ])].append(d)
    rows = []
    for (a, b, rel), ds in grouped.items():
        ev = {"source": "AI_DEALS_2026",
              "total_usd_bn": round(sum(d[4] or 0 for d in ds), 3) or None,
              "deals": [{"date": d[0], "type": d[3], "usd_bn": d[4], "note": d[5], "confidence": d[6], "url": d[7]} for d in ds]}
        rows.append((None, a, "ORG", None, b, "ORG", rel, "AI_DEALS_2026", json.dumps(ev)))
    rows.append((None, "X.AI Holdings Corp.", "ORG", None, "SpaceX", "ORG", "ACQUIRED_BY", "AI_DEALS_2026",
                 json.dumps({"source": "AI_DEALS_2026", "date": "2026-02", "note": "xAI merged into SpaceX (all-stock)",
                             "url": "https://finance.yahoo.com/news/elon-musk-confirms-spacex-merger-with-xai-ahead-of-ipo-220616795.html"})))
    return rows


# (person, relation, company, source url) -- checked 2026-10-15
LEADERS = [
    ("Josh Payne", "FOUNDER", "Nscale", "https://en.wikipedia.org/wiki/Nscale"),
    ("Josh Payne", "CHIEF_EXECUTIVE_OFFICER", "Nscale", "https://en.wikipedia.org/wiki/Nscale"),
    ("Justin Osofsky", "OFFICER", "Nscale", "https://datacentremagazine.com/news/nscale-names-metas-justin-osofsky-as-new-coo-for-global-scale"),
    ("Nick Clegg", "DIRECTOR", "Nscale", "https://www.wikidata.org/wiki/Q132721548"),
    ("Sheryl Sandberg", "DIRECTOR", "Nscale", "https://www.wikidata.org/wiki/Q132721548"),
    ("Vipul Ved Prakash", "CHIEF_EXECUTIVE_OFFICER", "Together AI", "https://www.clay.com/dossier/together-ai-ceo"),
    ("Vipul Ved Prakash", "FOUNDER", "Together AI", "https://techcrunch.com/2026/07/01/neocloud-together-ai-raises-800m-leaps-to-8-3b-valuation"),
    ("Ce Zhang", "FOUNDER", "Together AI", "https://techcrunch.com/2026/07/01/neocloud-together-ai-raises-800m-leaps-to-8-3b-valuation"),
    ("Percy Liang", "FOUNDER", "Together AI", "https://techcrunch.com/2026/07/01/neocloud-together-ai-raises-800m-leaps-to-8-3b-valuation"),
    ("Chris Re", "FOUNDER", "Together AI", "https://www.clay.com/dossier/together-ai-ceo"),
    ("David Aninowsky", "FOUNDER", "Vultr", "https://www.vultr.com/company/our-team/"),
    ("J.J. Kardwell", "CHIEF_EXECUTIVE_OFFICER", "Vultr", "https://www.vultr.com/company/our-team/"),
    ("Chase Lochmiller", "CHIEF_EXECUTIVE_OFFICER", "Crusoe Inc.", "https://crusoe.ai/about/leadership"),
    ("Chase Lochmiller", "FOUNDER", "Crusoe Inc.", "https://crusoe.ai/about/leadership"),
    ("Cully Cavness", "FOUNDER", "Crusoe Inc.", "https://crusoe.ai/about/leadership"),
    ("Michael Gordon", "OFFICER", "Crusoe Inc.",
     "https://www.globenewswire.com/news-release/2025/12/11/3203962/0/en/Crusoe-Appoints-Former-MongoDB-Executive-Michael-Gordon-as-COO-and-CFO.html"),
    ("Cesar Maklary", "FOUNDER", "Fluidstack Ltd", "https://www.sacra.com/c/fluidstack"),
    ("Michel Combes", "CHIEF_EXECUTIVE_OFFICER", "Lambda, Inc./DE",
     "https://bloomberg.com/news/articles/2026-05-05/ai-cloud-provider-lambda-taps-former-sprint-ceo-as-new-leader"),
    ("Stephen Balaban", "FOUNDER", "Lambda, Inc./DE",
     "https://bloomberg.com/news/articles/2026-05-05/ai-cloud-provider-lambda-taps-former-sprint-ceo-as-new-leader"),
    ("Michael Balaban", "FOUNDER", "Lambda, Inc./DE",
     "https://bloomberg.com/news/articles/2026-05-05/ai-cloud-provider-lambda-taps-former-sprint-ceo-as-new-leader"),
    ("Arkady Volozh", "CHIEF_EXECUTIVE_OFFICER", "Nebius Group N.V.", "https://www.wikidata.org/wiki/Q47120756"),
]


def leader_rows():
    return [(None, p, "PERSON", None, c, "ORG", rel, "AI_COMPANY_LEADERS", json.dumps({"source": "AI_COMPANY_LEADERS", "url": u}))
            for p, rel, c, u in LEADERS]


# ---- Form D sales compensation recipients ----------------------------------
JUNK = {"", "NONE", "N/A", "NA", "N.A.", "NOT APPLICABLE", "-", "--", "0", "NONE.", "NOT APPLICABLE.", "N/A N/A"}
ORG_WORDS = re.compile(
    r"\b(LLC|L\.?L\.?C|LP|L\.?P|LLP|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LTD|LIMITED|GROUP|PARTNERS|CAPITAL|"
    r"SECURITIES|FINANCIAL|ADVISORS|ADVISERS|MARKETS|BANK|INVESTMENTS?|SERVICES|MANAGEMENT|HOLDINGS|TRUST|FUND|PLC|"
    r"AG|SA|GMBH|ASSOCIATES|EQUITIES|WEALTH|BROKERAGE|AMERICAS|INTERNATIONAL|GLOBAL|ADVISORY)\b", re.I)
# ambiguous words need their AI context: "Harvey SmidCap Offshore Fund",
# "Mistral Equity Partners" and "Cursor Capital I" are unrelated funds
AI_SPV = re.compile(r"\b(openai|anthropic|perplexity|x\.?ai|databricks|scale ai|mistral(?= ai)|agc mistral|thinking machines|"
                    r"coreweave|cerebras|groq|anysphere|cursor(?= (ai|spv))|safe superintelligence|figure ai|"
                    r"glean(?=[- ]\d| spv| technologies)|harvey ai|elevenlabs|crusoe|lambda|fluidstack|nscale|together ai|"
                    r"reflection ai|physical intelligence|skild)\b", re.I)
NOT_AN_SPV_INTO = re.compile(r"startup fund|equity partners|smidcap|harvey road|harvey capital|tech fund", re.I)
SPV_WORDS = re.compile(r"series|spv|fund|\blp\b|l\.p\.|syndicate|vehicle|co-invest|feeder|aggregator|opportunit|"
                       r"\bllc\b.*\b(a|an)\b|holdings? i\b|\binvestment\b", re.I)
SPV_TARGET = {"openai": "OpenAi, Inc.", "anthropic": "Anthropic", "perplexity": "Perplexity AI, Inc.", "xai": "X.AI Holdings Corp.",
              "x.ai": "X.AI Holdings Corp.", "databricks": "Databricks, Inc.", "scale ai": "SCALE AI, INC.", "mistral": "Mistral AI",
              "thinking machines": "Thinking Machines Lab", "coreweave": "CoreWeave, Inc.", "cerebras": "Cerebras Systems Inc.",
              "groq": "Groq, Inc.", "anysphere": "Anysphere", "cursor": "Anysphere", "safe superintelligence": "Safe Superintelligence",
              "figure ai": "Figure AI", "glean": "GLEAN TECHNOLOGIES, INC.", "harvey": "Harvey AI Corp", "elevenlabs": "ELEVENLABS, INC.",
              "crusoe": "Crusoe Inc.", "lambda": "Lambda, Inc./DE", "fluidstack": "Fluidstack Ltd", "nscale": "Nscale",
              "together ai": "Together AI", "reflection ai": "REFLECTION AI, INC.", "physical intelligence": "Physical Intelligence",
              "skild": "Skild AI"}


def _tsv(z, suffix):
    n = next((n for n in z.namelist() if n.upper().endswith(suffix)), None)
    return csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8", errors="replace"), delimiter="\t") if n else []


def clean(s):
    s = re.sub(r"\s+", " ", (s or "")).strip(" ,.")
    return "" if s.upper() in JUNK else s


def is_person(name, crd):
    toks = name.split()
    return (bool(crd) and crd.strip("0").isdigit() and 2 <= len(toks) <= 5 and not ORG_WORDS.search(name)
            and not any(ch.isdigit() for ch in name))


# firms whose names pass is_person()
PERSON_LIKE_FIRMS = re.compile(r"(Morgan Stanley|Raymond James|Edward Jones|Charles Schwab|Stifel Nicolaus|"
                               r"Robert W\.? Baird|Janney Montgomery|Piper Sandler|Oppenheimer|Cantor Fitzgerald|"
                               r"Wells Fargo|Merrill Lynch|Goldman Sachs|Lazard|Evercore|Houlihan Lokey)\b", re.I)
_FIRM_SUFFIX = re.compile(r"[,.]?\s+(LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|L\.?P\.?|LLP|Co\.?)$", re.I)


def short_firm(firm):
    """'H & L Equities, LLC' -> 'H & L Equities' for rep labels."""
    for _ in range(2):                       # 'Foo Securities Co., LLC'
        firm = _FIRM_SUFFIX.sub("", firm).strip(" ,")
    if firm.isupper():                       # keep acronyms: 'UBS FINANCIAL SERVICES' -> 'UBS Financial Services'
        firm = " ".join(w if len(w) <= 3 else w.title() for w in firm.split())
    return firm


def _money(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def form_d_rows(zip_dir):
    agg = {}                                     # (src, src_type, tgt, rel, src_id, tgt_id) -> evidence

    def add(src, stype, tgt, rel, sid, tid, q, acc, off):
        k = (src, stype, tgt, rel, sid, tid)
        ev = agg.get(k)
        if ev is None:
            ev = agg[k] = {"source": "SEC_FORM_D_RECIPIENTS", "filings": 0, "first_quarter": q, "last_quarter": q,
                           "accessions": [], "max_amount_sold_usd": None, "max_investors": None}
        ev["filings"] += 1
        ev["first_quarter"], ev["last_quarter"] = min(ev["first_quarter"], q), max(ev["last_quarter"], q)
        if len(ev["accessions"]) < 5:
            ev["accessions"].append(acc)
        if off:
            sold, inv = _money(off.get("TOTALAMOUNTSOLD")), _money(off.get("TOTALNUMBERALREADYINVESTED"))
            if sold is not None and (ev["max_amount_sold_usd"] or 0) < sold:
                ev["max_amount_sold_usd"] = sold
            if inv is not None and (ev["max_investors"] or 0) < inv:
                ev["max_investors"] = int(inv)

    rep_names, rep_firms = defaultdict(Counter), defaultdict(Counter)    # CRD -> spellings / firms
    rep_events = []

    def rep_label(rid):
        name = rep_names[rid].most_common(1)[0][0]
        parts = [p.strip() for p in name.split(",")]
        if len(parts) == 2 and all(parts):          # 'Shollmier, Dudley' -> 'Dudley Shollmier'
            name = f"{parts[1]} {parts[0]}"
        elif len(parts) == 3 and parts[1].rstrip(".").lower() in ("jr", "sr", "ii", "iii"):
            name = f"{parts[2]} {parts[0]} {parts[1]}"   # 'Lopez, Jr., Luis O' -> 'Luis O Lopez Jr.'
        if name.isupper():
            name = name.title()
        if not rep_firms[rid]:
            return name
        return f"{name} ({short_firm(rep_firms[rid].most_common(1)[0][0])})"

    paths = sorted(glob.glob(os.path.join(zip_dir, "*_d.zip")) + glob.glob(os.path.join(zip_dir, "20??q?.zip")))
    # Some filings swap the fields (recipient 'Morgan Stanley', broker-dealer
    # 'Micah Todd Lowry'). A recipient name seen more often as a broker-dealer
    # than as a recipient is a firm.
    as_bd, as_rn = Counter(), Counter()
    for path in paths:
        for r in _tsv(zipfile.ZipFile(path), "RECIPIENTS.TSV"):
            as_bd[clean(r.get("ASSOCIATEDBDNAME")).lower()] += 1
            as_rn[clean(r.get("RECIPIENTNAME")).lower()] += 1
    firm_names = {n for n, c in as_bd.items() if n and c > as_rn[n]}

    for path in paths:
        q = re.search(r"(\d{4}q\d)", os.path.basename(path)).group(1)
        z = zipfile.ZipFile(path)
        issuer = {}
        for r in _tsv(z, "ISSUERS.TSV"):
            if r.get("IS_PRIMARYISSUER_FLAG", "YES") in ("YES", "Y", ""):
                issuer[r["ACCESSIONNUMBER"]] = (clean(r["ENTITYNAME"]), (r.get("CIK") or "").zfill(10))
        offer = {r["ACCESSIONNUMBER"]: r for r in _tsv(z, "OFFERING.TSV")}
        for r in _tsv(z, "RECIPIENTS.TSV"):
            acc = r["ACCESSIONNUMBER"]
            iss = issuer.get(acc)
            if not iss or not iss[0]:
                continue
            iname, cik = iss
            off = offer.get(acc)
            rn, rcrd = clean(r.get("RECIPIENTNAME")), (r.get("RECIPIENTCRDNUMBER") or "").strip()
            bd, bdcrd = clean(r.get("ASSOCIATEDBDNAME")), (r.get("ASSOCIATEDBDCRDNUMBER") or "").strip()
            bdcrd = bdcrd if bdcrd.strip("0").isdigit() else ""
            if rn and rn.lower() in firm_names and rn.lower() != bd.lower():
                rep, firm = "", rn                  # swapped fields: the 'broker-dealer' is a person
            else:
                rep = rn if rn and rn != bd and is_person(rn, rcrd) else ""
                firm = bd or ("" if rep else rn)
            if firm and is_person(firm, "1") and not PERSON_LIKE_FIRMS.match(firm):
                # a person in the firm field: as a bare-name ORG node it would
                # merge with namesakes. A one-off is a misfiled rep (dropped);
                # a regular one is a one-person broker-dealer ('Gary Cutson').
                firm = f"{firm.title() if firm.isupper() else firm} (broker-dealer)" if firm.lower() in firm_names else ""
            if firm:
                add(firm, "ORG", iname, "PLACEMENT_AGENT_FOR", f"CRD:{bdcrd}" if bdcrd and bd else None, cik, q, acc, off)
            if rep:
                rid = f"CRD:{rcrd.lstrip('0')}"
                rep_names[rid][rep] += 1
                if firm:
                    rep_firms[rid][firm] += 1
                offsub = {k: off.get(k) for k in ("TOTALAMOUNTSOLD", "TOTALNUMBERALREADYINVESTED")} if off else None
                rep_events.append((rid, firm, bdcrd, iname, cik, q, acc, offsub))
        # AI-company SPVs: the vehicle buys into the company its name points at
        for acc, (iname, cik) in issuer.items():
            m = AI_SPV.search(iname)
            if m and SPV_WORDS.search(iname) and not NOT_AN_SPV_INTO.search(iname):
                key = m.group(1).lower()
                key = {"agc mistral": "mistral", "harvey ai": "harvey"}.get(key, key)
                target = SPV_TARGET.get(key)
                if target and target.lower() not in iname.lower():
                    add(iname, "ORG", target, "SPV_INVESTS_IN", cik, None, q, acc, offer.get(acc))
    # Reps are keyed by CRD and named "Name (Firm)" after their most frequent
    # firm: a bare name merged brokers with their namesakes in the graph
    # ("Scott Harrison" of H & L Equities had 3,543 links on 2026-10-16), and
    # one firm per CRD keeps a rep who changed firms a single node.
    for rid, firm, bdcrd, iname, cik, q, acc, offsub in rep_events:
        label = rep_label(rid)
        add(label, "PERSON", iname, "PLACEMENT_AGENT_FOR", rid, cik, q, acc, offsub)
        if firm:
            add(label, "PERSON", firm, "EMPLOYED_BY", rid, f"CRD:{bdcrd}" if bdcrd else None, q, acc, None)
    rows = []
    for (src, stype, tgt, rel, sid, tid), ev in agg.items():
        if src.lower() == tgt.lower():
            continue
        rows.append((sid, src, stype, tid, tgt, "COMPANY" if rel != "SPV_INVESTS_IN" else "ORG", rel,
                     "SEC_FORM_D_RECIPIENTS", json.dumps(ev)))
    return rows


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--zips", help="dir of {q}_d.zip Form D data sets (omit to load only deals + leaders)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--replace-form-d", action="store_true",
                    help="first delete every SEC_FORM_D_RECIPIENTS row (e.g. after a labelling change); "
                         "same transaction as the insert")
    args = ap.parse_args()
    if args.replace_form_d and not args.zips:
        sys.exit("--replace-form-d needs --zips, or the Form D rows would only be deleted")
    if args.zips and not os.path.isdir(args.zips):
        sys.exit(f"--zips: no such directory: {args.zips}")
    zips = args.zips
    rows = deal_rows() + leader_rows() + (form_d_rows(zips) if zips else [])
    by = defaultdict(int)
    for r in rows:
        by[(r[7], r[6])] += 1
    for k, v in sorted(by.items()):
        print(f"  {k[0]:24} {k[1]:24} {v:8,}")
    print(f"{len(rows):,} rows total")
    if not args.commit:
        print("dry run -- add --commit to insert")
        return
    sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
    import psycopg2
    import pg_secret
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    if args.replace_form_d:
        cur.execute("DELETE FROM relationships WHERE source_data = 'SEC_FORM_D_RECIPIENTS'")
        print(f"deleted {cur.rowcount:,} old SEC_FORM_D_RECIPIENTS rows")
    inserted = execute_values(
        cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, "
             "relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING RETURNING 1",
        rows, page_size=5000, fetch=True)
    conn.commit()
    print(f"inserted {len(inserted):,} of {len(rows):,} rows (the rest already existed)")
    cur.execute("SELECT source_data, count(*) FROM relationships WHERE source_data IN "
                "('AI_DEALS_2026','AI_COMPANY_LEADERS','SEC_FORM_D_RECIPIENTS') GROUP BY 1")
    print(cur.fetchall())


if __name__ == "__main__":
    main()

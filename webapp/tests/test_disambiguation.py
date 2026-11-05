"""Common-name splitting and junk-name filtering (webapp/disambiguation.py)."""
import json
import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from disambiguation import MIN_DISTINCT, Splitter, is_junk_name  # noqa: E402


def fec(employer):
    return json.dumps({"source": "FEC", "employer": employer})


def pat(pid):
    return json.dumps({"patent_id": pid})


def make(exempt=()):
    sp = Splitter(exempt)
    for emp in ("Cornerstone Government Affairs", "FedEx", "Valley Proteins", "Kaitar Resources", "RETIRED"):
        sp.observe_fec("michael smith", fec(emp))
    for emp in ("Greylock Partners", "GREYLOCK PARTNERS LLC", "LinkedIn"):
        sp.observe_fec("reid hoffman", fec(emp))
    for i, org in enumerate(("Huawei", "VIA Labs, Inc.", "Apple", "Alibaba")):
        sp.observe_inventor_at("wei wang", org, pat(f"p{i}"))
    sp.observe_inventor_at("robert s langer", "MIT", pat("p9"))
    sp.finalize()
    return sp


def test_names_with_many_employers_are_ambiguous():
    sp = make()
    assert sp.fec_ambiguous == {"michael smith"}       # 4 informative employers; RETIRED doesn't count
    assert sp.inv_ambiguous == {"wei wang"}
    assert MIN_DISTINCT == 4


def test_one_persons_job_changes_dont_split():
    sp = make()
    # "Greylock Partners" and "GREYLOCK PARTNERS LLC" normalise to one employer
    assert sp.fec_endpoint("reid hoffman", "Reid Hoffman", fec("LinkedIn")) == ("reid hoffman", "Reid Hoffman")


def test_ambiguous_donor_is_split_by_employer():
    sp = make()
    key, display = sp.fec_endpoint("michael smith", "MICHAEL SMITH", fec("CORNERSTONE GOVERNMENT AFFAIRS"))
    assert display == "MICHAEL SMITH (Cornerstone Government Affairs)"
    assert key == "michael smith (cornerstone government affairs)"
    # same employer, different spelling -> same node
    assert sp.fec_endpoint("michael smith", "Michael Smith", fec("Cornerstone Government Affairs, Inc."))[0] == key


def test_ambiguous_donor_without_employer_is_dropped():
    sp = make()
    assert sp.fec_endpoint("michael smith", "MICHAEL SMITH", fec("RETIRED")) is None
    assert sp.fec_endpoint("michael smith", "MICHAEL SMITH", None) is None
    assert sp.dropped == 2


def test_verified_people_are_not_split():
    sp = make(exempt={"michael smith"})
    assert sp.fec_endpoint("michael smith", "Michael Smith", fec("FedEx")) == ("michael smith", "Michael Smith")


def test_verified_name_with_huge_spread_is_still_split():
    # "MICHAEL SMITH" is verified (a journalist) but has 112 employers in FEC data
    sp = Splitter({"michael smith"})
    for i in range(12):
        sp.observe_fec("michael smith", fec(f"Employer {i}"))
    sp.finalize()
    assert sp.fec_ambiguous == {"michael smith"}


def test_inventor_split_by_assignee_and_by_patent_lookup():
    sp = make()
    assert sp.patent_endpoint("wei wang", "Wei Wang", pat("p1"), assignee="VIA Labs, Inc.")[1] == "Wei Wang (VIA Labs, Inc)"
    # co-inventor edge: the company comes from the patent
    assert sp.patent_endpoint("wei wang", "Wei Wang", pat("p0"))[1] == "Wei Wang (Huawei)"
    assert sp.patent_endpoint("wei wang", "Wei Wang", pat("unknown")) is None
    assert sp.patent_endpoint("robert s langer", "Robert S Langer", pat("p9")) == ("robert s langer", "Robert S Langer")


def test_junk_names():
    for junk in (".", "-", "None None", "Vacant Vacant", "SEE SCH O FOR COMPENSATION",
                 "See Supplemental Information", "N/a None", "Anonymous", "NA"):
        assert is_junk_name(junk), junk
    for real in ("Na Li", "Na Na", "Yang Yang", "None But the Brave (1965 film)", "ANONYMOUS TRUST",
                 "Open Society Foundations", "Unknown Subscriber"):
        assert not is_junk_name(real), real


def test_fec_recipient_names_committees_and_drops_conduits():
    from disambiguation import fec_recipient
    names = {"C00000935": "DCCC", "C00577130": "BERNIE 2016"}
    ev = lambda rid: json.dumps({"source": "FEC", "recipient": rid})
    assert fec_recipient(ev("C00000935"), names) == "DCCC"
    assert fec_recipient(ev("C00577130"), names) == "BERNIE 2016"
    # unknown id keeps a placeholder, but with the FULL id (no truncation merges)
    assert fec_recipient(ev("C00999999"), names) == "FEC Campaign Committee C00999999"
    assert fec_recipient(ev("C00401224"), names) is None      # ActBlue: real recipient unknown
    assert fec_recipient(ev("C00694323"), names) is None      # WinRed
    assert fec_recipient(ev(""), names) is None


# --- union officers vs. elite namesakes ---
def _union(regions=None):
    from disambiguation import UnionSplitter
    us = UnionSplitter(regions if regions is not None else {"michael grunwald": 3, "rare person": 1})
    lm2 = lambda d: json.dumps({"title": "VICE PRESIDENT", "designation": d})
    us.observe_lm2("michael grunwald", "Michael Grunwald", "GOVERNMENT EMPLOYEES AFGE AFL-CIO", lm2("LU 2040"))
    us.observe_lm2("michael grunwald", "Michael Grunwald", "GOVERNMENT EMPLOYEES AFGE AFL-CIO", lm2("LU 2040"))
    us.observe_lm2("michael grunwald", "Michael Grunwald", "ELECTRICAL WORKERS IBEW AFL-CIO", lm2("NHQ"))
    for k, d in (("rare person", "Rare Person"), ("jane local", "Jane Local"), ("pat leader", "Pat Leader")):
        us.observe_lm2(k, d, "TEAMSTERS", lm2("NHQ" if k == "pat leader" else "LU 5"))
    assert us.has_union_roles("michael grunwald") and not us.has_union_roles("someone else")
    us.observe_other("michael grunwald", "The Washington Post", "WIKIDATA_MEDIA")
    us.observe_other("rare person", "Acme Corp", "SEC_FORM_345")               # elite, but a one-region name
    us.observe_other("jane local", "TEAMSTERS LOCAL 5 PENSION FUND", "LITTLESIS")  # union-related only
    us.observe_other("pat leader", "Democratic Party", "LITTLESIS")              # elite tie...
    us.observe_other("pat leader", "International Brotherhood of Teamsters", "LITTLESIS")  # ...but a known leader
    # a LOCAL union leader documented in LittleSis (Laphonza Butler, SEIU Local 2015) stays one person too
    us.observe_lm2("laphonza butler", "Laphonza Butler", "SERVICE EMPLOYEES", lm2("LU 2015"))
    us.observe_other("laphonza butler", "United States Senate", "LITTLESIS")
    us.observe_other("laphonza butler", "Service Employees International Union Local 2015", "LITTLESIS")
    return us


def test_union_split_targets_shared_names_with_elite_ties():
    us = _union({"michael grunwald": 3, "rare person": 1, "jane local": 9, "pat leader": 9, "laphonza butler": 4})
    us.finalize()
    assert us.split_names == {"michael grunwald"}     # rare name, union-only ties and known leader all stay
    assert us.primary["michael grunwald"] == "GOVERNMENT EMPLOYEES AFGE AFL-CIO"


def test_union_split_moves_union_rows_only():
    us = _union({"michael grunwald": 3})
    us.finalize()
    assert us.wants("michael grunwald", "OLMS_LM2", "ELECTRICAL WORKERS IBEW AFL-CIO")
    assert us.wants("michael grunwald", "IRS_990", "IBEW LOCAL 76 HEALTH & WELFARE TRUST")
    assert not us.wants("michael grunwald", "IRS_990", "Overbrook Foundation")
    assert not us.wants("michael grunwald", "WIKIDATA_MEDIA", "The Washington Post")
    assert not us.wants("someone else", "OLMS_LM2", "TEAMSTERS")
    # LM-2 rows land on their own union; other union-related rows on the main one
    assert us.endpoint("michael grunwald", "Michael Grunwald", "OLMS_LM2", "ELECTRICAL WORKERS IBEW AFL-CIO") == \
        ("michael grunwald (electrical workers ibew afl cio)", "Michael Grunwald (Electrical Workers Ibew Afl-Cio)")
    assert us.endpoint("michael grunwald", "Michael Grunwald", "IRS_990", "IBEW LOCAL 76 TRUST")[1] == \
        "Michael Grunwald (Government Employees Afge Afl-Cio)"
    assert us.moved == 2


def test_union_split_off_without_region_table():
    us = _union({})
    us.finalize()
    assert us.split_names == set()


def test_first_last():
    from disambiguation import first_last
    assert first_last("Michael R. Grunwald Jr") == "michael grunwald"
    assert first_last("MADONNA") == ""

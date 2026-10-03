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

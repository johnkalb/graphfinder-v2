"""Build My Path told users to "reach out to State of Wisconsin directly"
(2026-11-19): path steps now carry an "org" flag, and court-record
placeholders like "Pro Se" are no longer graph nodes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pathfinder as pf  # noqa: E402
from disambiguation import is_junk_name  # noqa: E402


def test_org_labels():
    for org in ["State of Wisconsin", "THE LONG RUN PAC", "NATIONAL GRAIN AND FEED ASSOCIATION", "Gates Foundation"]:
        assert pf._label_is_org(org), org
    for person in ["MIKE TATE", "Kevin Gray", "Mandela Barnes", "Bill de Blasio"]:
        assert not pf._label_is_org(person), person


def test_court_placeholders_are_junk():
    for junk in ["Pro Se", "pro se litigant", "Attorney at Law", "Self", "self-represented"]:
        assert is_junk_name(junk), junk
    for real in ["Selfridge", "Seth Pro", "Prose Partners"]:
        assert not is_junk_name(real), real

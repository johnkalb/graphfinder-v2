"""The browser's contacts-file parser and match ranking (webapp/contact_psi.js,
2026-11-17), run under Node. Skipped where Node isn't installed."""
import json
import os
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contact_psi.js")
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")


def run(expr, *args):
    code = (f"const m = require({json.dumps(JS)}); const args = JSON.parse(process.argv[1]);"
            f"process.stdout.write(JSON.stringify(({expr})(...args)));")
    out = subprocess.run([NODE, "-e", code, json.dumps(args)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def parse(text):
    return run("t => m.contactPsiParseFile(t)", text)


def test_csv_with_name_and_company_columns():
    r = parse("First Name,Last Name,Company\nBarack,Obama,\nNancy,Pelosi,US House\n")
    assert r["format"] == "columns"
    assert r["contacts"] == [{"name": "Barack Obama", "company": "", "title": ""},
                             {"name": "Nancy Pelosi", "company": "US House", "title": ""}]


def test_linkedin_notes_above_header_and_quoted_commas():
    text = ('Notes:\n"When exporting your connection data, you may notice that some of the email addresses are missing."\n\n'
            'First Name,Last Name,URL,Email Address,Company,Position,Connected On\n'
            'Jane,Doe,https://x,jane@x.com,"Acme, Inc.",CEO,01 Jan 2024\n')
    assert parse(text)["contacts"] == [{"name": "Jane Doe", "company": "Acme, Inc.", "title": "CEO"}]


def test_google_and_outlook_columns():
    google = ('Name,Given Name,Family Name,Notes,Organization 1 - Name,Organization 1 - Title\n'
              'Al Q Pha,Al,Pha,"line one\nline two",Beta Co,VP\n')
    assert parse(google)["contacts"] == [{"name": "Al Pha", "company": "Beta Co", "title": "VP"}]
    # Outlook's "Title" is Mr./Ms.; "Job Title" is the job
    outlook = "Title,First Name,Middle Name,Last Name,Company,Job Title,E-mail Address\nMs.,Cee,,Dee,Gamma Ltd,Rep,c@d.co\n"
    assert parse(outlook)["contacts"] == [{"name": "Cee Dee", "company": "Gamma Ltd", "title": "Rep"}]


def test_full_name_column_only():
    assert [c["name"] for c in parse("Name,Phone\nMeryl Streep,555\n")["contacts"]] == ["Meryl Streep"]


def test_vcard():
    vcf = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Cee Dee\r\nORG:Gamma Ltd;Sales\r\nTITLE:Rep\r\nTEL:555\r\nEND:VCARD\r\n"
           "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Smith;Anna;;;\r\nitem1.ORG:Acme\\, Inc.\r\nEND:VCARD\r\n")
    r = parse(vcf)
    assert r["format"] == "vcard"
    assert r["contacts"] == [{"name": "Cee Dee", "company": "Gamma Ltd", "title": "Rep"},
                             {"name": "Anna Smith", "company": "Acme, Inc.", "title": ""}]


def test_headerless_lines():
    r = parse("Barack Obama\nNancy,Pelosi,US House\nMeryl Streep, Actor Guild\nCher\n")
    assert r["format"] == "lines"
    assert [(c["name"], c["company"]) for c in r["contacts"]] == [
        ("Barack Obama", ""), ("Nancy Pelosi", "US House"), ("Meryl Streep", "Actor Guild")]


def test_company_picks_the_right_namesake():
    cands = {"Mark Greene": {"a": {"rank": 0, "similarity": 1, "tier": "exact", "name": "Mark Greene", "order": 0},
                             "b": {"rank": 0, "similarity": 1, "tier": "exact", "name": "Mark Greene (Ibm)", "order": 1}}}
    ranked = run("(c, i) => m.contactPsiRank(c, i)", cands, {"Mark Greene": {"company": "IBM"}})
    assert ranked["Mark Greene"][0]["name"] == "Mark Greene (Ibm)"
    assert run("c => m.contactPsiConfident(c)", ranked["Mark Greene"])
    plain = run("(c, i) => m.contactPsiRank(c, i)", cands, {})
    assert plain["Mark Greene"][0]["name"] == "Mark Greene" and not run("c => m.contactPsiConfident(c)", plain["Mark Greene"])

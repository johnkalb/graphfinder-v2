"""Private/public user links (webapp/my_links.py) -- private-service spec decisions 1-3, 10-12."""
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import my_links as ml  # noqa: E402
from test_department import init_test_department_db  # noqa: E402

VERIFIED = {"barack obama", "meryl streep"}
is_verified = lambda name: name.lower() in VERIFIED


@pytest.fixture
def dbp(tmp_path):
    p = str(tmp_path / "td.db")
    init_test_department_db(p)
    return p


def test_links_are_private_by_default_and_only_the_owners(dbp):
    link, err = ml.add_link(dbp, "a@x.org", "Jane Doe", source="linkedin")
    assert err is None and link["visibility"] == "private"
    assert [l["contact"] for l in ml.list_links(dbp, "a@x.org")] == ["Jane Doe"]
    assert ml.list_links(dbp, "b@x.org") == []
    assert ml.public_links(dbp) == []


def test_any_matched_person_can_be_linked_visibly_since_2026_11_27(dbp):
    link, err = ml.add_link(dbp, "a@x.org", "Jane Doe", visibility="public")
    assert err is None and link["visibility"] == "public"
    _, err = ml.add_link(dbp, "a@x.org", "Somebody", contact_type=ml.UNMATCHED, visibility="public")
    assert err and "only be private" in err
    assert [l["contact"] for l in ml.public_links(dbp)] == ["Jane Doe"]


def test_the_other_person_can_hide_one_link_and_it_stays_hidden(dbp):
    owner_name = {"a@x.org": "Alex Owner", "b@x.org": "Bea Owner"}.get
    ml.add_link(dbp, "a@x.org", "Jane Doe", visibility="public")
    ml.add_link(dbp, "b@x.org", "Jane Doe", visibility="public")
    assert ml.hide_about_me(dbp, owner_name, "Jane Doe", "Alex Owner", "jane@x.org") == 1
    assert [l["owner_email"] for l in ml.public_links(dbp)] == ["b@x.org"]          # only that link
    assert [l["contact"] for l in ml.removed_public_links(dbp)] == ["Jane Doe"]       # leaves the shared map
    mine = ml.list_links(dbp, "a@x.org")[0]
    assert mine["visibility"] == "private"                                            # owner keeps it privately
    _, err = ml.set_visibility(dbp, "a@x.org", mine["id"], "public")
    assert err and "stay hidden" in err                                               # and can't re-publish it
    # either order of the two names works
    assert ml.hide_about_me(dbp, owner_name, "Bea Owner", "Jane Doe", "jane@x.org") == 1
    assert ml.public_links(dbp) == []


def test_hide_everything_about_me_blocks_future_visible_links(dbp):
    ml.add_link(dbp, "a@x.org", "Jane Doe", visibility="public")
    ml.add_link(dbp, "b@x.org", "Jane Doe", visibility="public")
    assert ml.hide_about_me(dbp, lambda e: e, "Jane Doe", "whoever", "jane@x.org", everything=True) == 2
    assert ml.public_links(dbp) == []
    _, err = ml.add_link(dbp, "c@x.org", "Jane Doe", visibility="public")
    assert err and "stay hidden" in err
    link, err = ml.add_link(dbp, "c@x.org", "Jane Doe")                              # private is still fine
    assert err is None and link["visibility"] == "private"


def test_links_added_while_private_by_default_are_asked_about_once(dbp, monkeypatch):
    monkeypatch.setattr(ml, "VISIBLE_DEFAULT_SINCE", "2999-01-01")     # everything below counts as "before"
    ml.add_link(dbp, "a@x.org", "Jane Doe")
    ml.add_link(dbp, "a@x.org", "Old Contact")
    ml.add_link(dbp, "a@x.org", "No Match", contact_type=ml.UNMATCHED)
    ml.hide_about_me(dbp, lambda e: e, "Old Contact", "x", "oc@x.org", everything=True)
    assert ml.legacy_private_count(dbp, "a@x.org") == 1                 # not the unmatched or opted-out one
    assert ml.flag(dbp, "a@x.org", "existing_links_answer") is None
    assert ml.publish_existing(dbp, "a@x.org") == 1
    assert [l["contact"] for l in ml.public_links(dbp)] == ["Jane Doe"]
    ml.set_flag(dbp, "a@x.org", "existing_links_answer", "yes")
    assert ml.flag(dbp, "a@x.org", "existing_links_answer") == "yes"


def test_public_cap(dbp, monkeypatch):
    monkeypatch.setattr(ml, "MAX_PUBLIC_LINKS", 1)
    ml.add_link(dbp, "a@x.org", "Barack Obama", visibility="public")
    _, err = ml.add_link(dbp, "a@x.org", "Meryl Streep", visibility="public")
    assert err and "maximum" in err


def test_delete_is_immediate_and_public_deletes_are_listed_for_removal(dbp):
    pub, _ = ml.add_link(dbp, "a@x.org", "Barack Obama", visibility="public", is_verified=is_verified)
    priv, _ = ml.add_link(dbp, "a@x.org", "Jane Doe")
    assert ml.delete_link(dbp, "a@x.org", pub["id"]) and ml.delete_link(dbp, "a@x.org", priv["id"])
    assert ml.list_links(dbp, "a@x.org") == []
    assert ml.public_links(dbp) == []
    assert [l["contact"] for l in ml.removed_public_links(dbp)] == ["Barack Obama"]   # private ones never were public
    assert not ml.delete_link(dbp, "b@x.org", pub["id"])                             # only the owner can delete


def test_making_a_public_link_private_withdraws_it(dbp):
    pub, _ = ml.add_link(dbp, "a@x.org", "Barack Obama", visibility="public", is_verified=is_verified)
    priv, err = ml.set_visibility(dbp, "a@x.org", pub["id"], "private", is_verified)
    assert err is None and priv["visibility"] == "private"
    assert ml.public_links(dbp) == []
    assert [l["contact"] for l in ml.removed_public_links(dbp)] == ["Barack Obama"]
    assert [l["visibility"] for l in ml.list_links(dbp, "a@x.org")] == ["private"]


def test_legacy_approved_self_reported_links_migrate_as_private(dbp):
    import sqlite3
    conn = sqlite3.connect(dbp)
    conn.execute("INSERT INTO user_links (subject, object, predicate, submitter_email, approved_at) "
                 "VALUES ('A Person', 'Jane Doe', 'LINKEDIN_CONNECTION', 'a@x.org', '2026-10-05')")
    conn.execute("INSERT INTO user_links (subject, object, predicate, submitter_email, approved_at) "
                 "VALUES ('Someone', 'Other Org', 'FAMILY', 'b@x.org', '2026-10-05')")
    conn.commit()
    conn.close()
    assert ml.migrate_legacy_user_links(dbp) == 1
    assert ml.migrate_legacy_user_links(dbp) == 0          # idempotent
    links = ml.list_links(dbp, "a@x.org")
    assert [(l["contact"], l["visibility"], l["source"]) for l in links] == [("Jane Doe", "private", "linkedin")]


def test_agreement(dbp):
    assert not ml.agreement_accepted(dbp, "a@x.org")
    ml.accept_agreement(dbp, "a@x.org")
    ml.accept_agreement(dbp, "a@x.org")
    assert ml.agreement_accepted(dbp, "a@x.org")


def test_account_deletion_removes_everything(dbp):
    ml.add_link(dbp, "a@x.org", "Barack Obama", visibility="public", is_verified=is_verified)
    ml.add_link(dbp, "a@x.org", "Jane Doe")
    ml.accept_agreement(dbp, "a@x.org")
    ml.delete_account_links(dbp, "a@x.org")
    assert ml.list_links(dbp, "a@x.org") == []
    assert not ml.agreement_accepted(dbp, "a@x.org")
    assert [l["contact"] for l in ml.removed_public_links(dbp)] == ["Barack Obama"]


def test_export_csv(dbp):
    ml.add_link(dbp, "a@x.org", "Jane Doe", source="linkedin")
    out = ml.export_csv(dbp, "a@x.org")
    assert out.splitlines()[0] == "contact,type,relation,source,visibility,added"
    assert "Jane Doe,PERSON,SELF_ATTESTED_CONTACT,linkedin,private" in out


def test_none_of_these_keeps_an_unmatched_private_contact(dbp):
    link, err = ml.add_unmatched(dbp, "a@x.org", "  Mark   Greene ", source="linkedin")
    assert err is None
    assert (link["contact"], link["contact_type"], link["visibility"], link["relation"]) == \
        ("Mark Greene", ml.UNMATCHED, "private", "LINKEDIN_CONNECTION")
    again, _ = ml.add_unmatched(dbp, "a@x.org", "Mark Greene")
    assert again["id"] == link["id"]
    _, err = ml.set_visibility(dbp, "a@x.org", link["id"], "public", lambda n: True)
    assert err and "only be private" in err
    assert ml.public_links(dbp) == []


def test_matching_later_replaces_the_unmatched_placeholder(dbp):
    ml.add_unmatched(dbp, "a@x.org", "Mark Greene")
    ml.add_link(dbp, "a@x.org", "Mark Greene (Ibm)")
    assert [(l["contact"], l["contact_type"]) for l in ml.list_links(dbp, "a@x.org")] == \
        [("Mark Greene (Ibm)", "PERSON")]


def test_wrong_person_unmatches_and_withdraws_a_public_link(dbp):
    pub, _ = ml.add_link(dbp, "a@x.org", "Barack Obama", visibility="public", is_verified=is_verified)
    new, err = ml.mark_wrong_person(dbp, "a@x.org", pub["id"])
    assert err is None and new["contact_type"] == ml.UNMATCHED and new["visibility"] == "private"
    assert [l["contact"] for l in ml.removed_public_links(dbp)] == ["Barack Obama"]
    assert ml.public_links(dbp) == []
    split, _ = ml.add_link(dbp, "a@x.org", "Mark Greene (Texas Senate)")
    new, _ = ml.mark_wrong_person(dbp, "a@x.org", split["id"])
    assert new["contact"] == "Mark Greene"
    assert sorted(l["contact"] for l in ml.list_links(dbp, "a@x.org")) == ["Barack Obama", "Mark Greene"]
    assert ml.mark_wrong_person(dbp, "b@x.org", new["id"]) == (None, "link not found")

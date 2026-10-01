import gzip
import json
import time

import pytest

import crawlie_poster as cp


def fact(key, cat="group_vs_group"):
    return {"key": key, "category": cat, "text": f"Fact {key}."}


@pytest.mark.parametrize("text,n,expected", [
    ("all", 3, [0, 1, 2]), ("YES", 2, [0, 1]), ("none", 3, []), ("skip", 3, []),
    ("1 3", 3, [0, 2]), ("3,1", 3, [2, 0]), ("2 2", 3, [1]),
    ("4", 3, None), ("0", 3, None), ("maybe", 3, None), ("", 3, None),
])
def test_parse_reply(text, n, expected):
    assert cp.parse_reply(text, n) == expected


def test_pick_candidates_rotates_categories_and_skips_posted():
    facts = [fact("g1"), fact("g2"), fact("g3"), fact("w1", "who_you_know"), fact("b1", "bridge")]
    picks = cp.pick_candidates(facts, posted={"g1"})
    assert [f["key"] for f in picks] == ["g2", "w1", "b1"]
    stats = [fact("s1", "stats_separation"), fact("c1", "stats_charity")]
    picks = cp.pick_candidates(facts + stats, posted=set())
    assert [f["key"] for f in picks] == ["g1", "s1", "w1"]


@pytest.fixture
def env_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "FACTS", str(tmp_path / "facts.json.gz"))
    with gzip.open(cp.FACTS, "wt", encoding="utf-8") as f:
        json.dump({"generated_at": "2026-09-27T00:00:00", "publishable":
                   [fact("g1"), fact("w1", "who_you_know"), fact("g2"), fact("b1", "bridge")]}, f)
    sent, posted, updates = [], [], []
    monkeypatch.setattr(cp, "notify", lambda env, text: sent.append(text) or True)
    monkeypatch.setattr(cp, "publish", lambda env, f: posted.append(f["key"]) or f"https://x/{f['key']}")

    def fake_tg(env, method, **params):
        assert method == "getUpdates"
        res = [u for u in updates if u["update_id"] >= params["offset"]]
        return {"ok": True, "result": res}
    monkeypatch.setattr(cp, "tg", fake_tg)
    env = {"CRAWLIE_TELEGRAM_CHAT_ID": "42"}
    state = {"posted": {}, "proposal": None, "queue": [], "tg_offset": 0, "last_post_at": 0}
    return env, state, sent, posted, updates


def reply(updates, uid, text, chat=42):
    updates.append({"update_id": uid, "message": {"chat": {"id": chat}, "text": text}})


def test_full_cycle_propose_approve_post_with_spacing(env_state):
    env, state, sent, posted, updates = env_state
    cp.cmd_propose(env, state)
    assert [f["key"] for f in state["proposal"]["facts"]] == ["g1", "w1", "b1"]
    assert "1. Fact g1." in sent[-1]

    reply(updates, 10, "hello", chat=999)        # stranger: ignored
    reply(updates, 11, "1 3")
    cp.cmd_tick(env, state)
    assert posted == ["g1"] and [f["key"] for f in state["queue"]] == ["b1"]
    assert state["proposal"] is None and state["tg_offset"] == 12

    cp.cmd_tick(env, state)                      # within spacing: no second post
    assert posted == ["g1"]
    state["last_post_at"] = time.time() - cp.POST_SPACING - 1
    cp.cmd_tick(env, state)
    assert posted == ["g1", "b1"] and state["queue"] == []

    state["proposal"] = None
    cp.cmd_propose(env, state)                   # posted/queued facts never re-offered
    assert [f["key"] for f in state["proposal"]["facts"]] == ["g2", "w1"]


def test_propose_waits_for_pending_answer(env_state):
    env, state, sent, posted, updates = env_state
    cp.cmd_propose(env, state)
    cp.cmd_propose(env, state)
    assert len(sent) == 1


def test_none_reply_leaves_facts_for_later(env_state):
    env, state, sent, posted, updates = env_state
    cp.cmd_propose(env, state)
    reply(updates, 1, "none")
    cp.cmd_tick(env, state)
    assert posted == [] and state["queue"] == [] and state["posted"] == {}
    cp.cmd_propose(env, state)
    assert [f["key"] for f in state["proposal"]["facts"]] == ["g2", "w1", "b1"]   # never-offered g2 first


def test_unanswered_days_rotate_through_the_pool():
    facts = [fact("g1"), fact("g2"), fact("w1", "who_you_know"), fact("w2", "who_you_know"),
             fact("s1", "stats_separation"), fact("c1", "stats_charity"), fact("k1", "stats_banks")]
    offered, seen = {}, []
    for day in range(1, 4):
        picks = [f["key"] for f in cp.pick_candidates(facts, set(), offered)]
        seen.append(picks)
        offered.update({k: day for k in picks})
    assert seen[0] == ["g1", "s1", "w1"]
    assert seen[1] == ["c1", "k1", "g2"]           # categories never offered go first
    assert set(seen[2]) & {"w2"}                    # the rest of the pool comes round


def test_proposal_not_recorded_when_telegram_send_fails(env_state, monkeypatch):
    env, state, sent, posted, updates = env_state
    monkeypatch.setattr(cp, "notify", lambda env, text: False)
    cp.cmd_propose(env, state)
    assert state["proposal"] is None


def test_failed_post_stays_queued(env_state, monkeypatch):
    env, state, sent, posted, updates = env_state
    state["queue"] = [fact("g1")]

    def boom(env, f):
        raise RuntimeError("server down")
    monkeypatch.setattr(cp, "publish", boom)
    cp.cmd_tick(env, state)
    assert [f["key"] for f in state["queue"]] == ["g1"] and state["posted"] == {}

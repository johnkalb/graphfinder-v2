"""Plain-English questions (ask_engine.py + /api/my/ask): the tool loop, the
unanswerable path, cost and number checks, and the ask_log table."""
from types import SimpleNamespace as NS

import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

pytestmark = pytest.mark.timeout(30)
USER = {"Cf-Access-Authenticated-User-Email": "beta.user@example.com"}

try:
    import ask_engine as ae
except ImportError:
    from webapp import ask_engine as ae


def text(t):
    return NS(type="text", text=t)


def use(tool_name, i, **inp):
    return NS(type="tool_use", id=f"t{i}", name=tool_name, input=inp)


class FakeClient:
    """Plays back scripted turns; records what the model was sent."""
    def __init__(self, turns):
        self.turns, self.sent = list(turns), []
        self.messages = self

    def create(self, **kw):
        self.sent.append(kw)
        return NS(content=self.turns.pop(0),
                  usage=NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0))


def test_tool_loop_answers_from_tool_results():
    seen = {}
    def donors_to(name):
        seen["name"] = name
        return {"result": {"donors": [{"donor": "A Donor &amp; Co", "usd": 2500000}] * 60}}
    fake = FakeClient([[text("Looking."), use("donors_to", 1, name="Warnock for Georgia")],
                       [text("The biggest donor gave $2.5M.")]])
    res = ae.run("who funds warnock", {"donors_to": donors_to}, client=fake, system="sys")
    assert res["status"] == "answered" and res["answer"] == "The biggest donor gave $2.5M."
    assert seen["name"] == "Warnock for Georgia" and res["steps"] == 2
    assert res["calls"][0]["tool"] == "donors_to" and len(res["calls"][0]["result"]["result"]["donors"]) == 60
    # the model sees at most MODEL_ROWS rows of a list, with a count of the rest
    sent = fake.sent[1]["messages"][-1]["content"][0]["content"]
    assert sent.count("A Donor & Co") == ae.MODEL_ROWS and "list continues" in sent
    assert res["usage"]["input"] == 2000 and res["cost_usd"] == pytest.approx((2000 * 0.10 + 200 * 0.50) / 1e6)
    assert ae.unverified_numbers(res["answer"], res["calls"]) == []


def test_cannot_answer_is_logged_as_unanswerable():
    fake = FakeClient([[use("cannot_answer", 1, reason="No salary data",
                            closest_questions=["Who is on the board of Acme?"])],
                       [text("Sorry, we don't have salaries.")]])
    res = ae.run("what does the Acme CEO earn", {}, client=fake, system="s")
    assert res["status"] == "unanswerable" and res["calls"] == []
    assert res["unanswerable"] == {"reason": "No salary data", "closest": ["Who is on the board of Acme?"]}


def test_tool_errors_go_back_to_the_model_and_steps_are_capped():
    def boom(name):
        raise RuntimeError("down")
    turns = [[use("entity", i, name="x")] for i in range(ae.MAX_STEPS)] + [[text("Gave up.")]]
    fake = FakeClient(turns)
    res = ae.run("q", {"entity": boom}, client=fake, system="s")
    assert res["answer"] == "Gave up." and res["steps"] == ae.MAX_STEPS + 1
    assert fake.sent[-1]["tool_choice"] == {"type": "none"}            # last turn must answer
    assert res["calls"][0]["error"] == "RuntimeError: down"
    assert fake.sent[1]["messages"][-1]["content"][0]["is_error"] is True


def test_unknown_tool_and_bad_arguments_are_reported():
    fake = FakeClient([[use("nope", 1), use("entity", 2, wrong="x")], [text("ok")]])
    res = ae.run("q", {"entity": lambda name: {}}, client=fake, system="s")
    assert [c["error"][:11] for c in res["calls"]] == ["unknown too", "bad argumen"]


def test_unverified_numbers_flags_invented_amounts_only():
    calls = [{"result": {"usd": 101123456.0, "n": 42, "year": "2022"}}]
    assert ae.unverified_numbers("Gave $101.1M over 42 gifts in 2022.", calls) == []
    assert ae.unverified_numbers("Gave $250 million.", calls) == ["$250 million"]


def test_system_prompt_names_the_user_and_capabilities():
    s = ae.system_prompt("Jane Doe", 27, [("CO_DIRECTOR", "Co-Director")], "2026-10-20")
    assert "Jane Doe" in s and "27 private links" in s and "CO_DIRECTOR: Co-Director" in s
    assert "biggest donors" in s


# --- the endpoint -------------------------------------------------------------------

@pytest.fixture
def ask_ready(monkeypatch):
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "all")
    monkeypatch.setattr(pf, "_resolve_name", lambda x: x)
    monkeypatch.setattr(pf, "_graph_backend_ready", lambda: True)
    monkeypatch.setattr(pf.mentioned_with_fallback, "_get_anthropic_key", lambda: "test-key")
    script = {"turns": None}
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: FakeClient(script["turns"]))
    return script


def test_ask_needs_beta_and_agreement(client, ask_ready, monkeypatch):
    assert client.post("/api/my/ask", json={"question": "hello there"}).status_code == 401
    assert client.post("/api/my/ask", json={"question": "hello there"}, headers=USER).status_code == 428
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "admins")
    assert client.post("/api/my/ask", json={"question": "hello there"}, headers=USER).status_code == 404


def test_ask_uses_my_links_logs_and_takes_feedback(client, ask_ready, monkeypatch):
    client.post("/api/agreement/accept", headers=USER)
    client.post("/api/my/links", json={"contact": "Jane Pal"}, headers=USER)
    ask_ready["turns"] = [[use("my_contacts", 1)], [text("You have 1 contact: Jane Pal.")]]
    r = client.post("/api/my/ask", json={"question": "who are my contacts?"}, headers=USER)
    d = r.json()
    assert r.status_code == 200 and d["status"] == "answered" and "Jane Pal" in d["answer"]
    assert d["calls"][0]["result"]["contacts"][0]["name"] == "Jane Pal"
    assert "meta" not in d and "capabilities" not in d                    # cost shown to admins only

    other = {"Cf-Access-Authenticated-User-Email": "someone.else@example.com"}
    assert client.post(f"/api/my/ask/{d['id']}/feedback", json={"helpful": False}, headers=other).status_code in (404, 428)
    assert client.post(f"/api/my/ask/{d['id']}/feedback", json={"helpful": False, "note": "wanted more"},
                       headers=USER).json()["success"]

    monkeypatch.setenv("USER_LINKS_SYNC_TOKEN", "tok")
    assert client.get("/api/internal/user-links/ask-log").status_code == 404
    rows = client.get("/api/internal/user-links/ask-log", headers={"X-Sync-Token": "tok"}).json()["rows"]
    row = next(x for x in rows if x["id"] == d["id"])
    assert row["question"] == "who are my contacts?" and row["status"] == "answered"
    assert row["tools"][0]["tool"] == "my_contacts" and row["tools"][0]["rows"] == 1
    assert row["feedback"] == -1 and row["feedback_note"] == "wanted more" and row["cost_usd"] > 0

    pf._ml.delete_account_links(pf._ml_db(), "beta.user@example.com")
    rows = client.get("/api/internal/user-links/ask-log", headers={"X-Sync-Token": "tok"}).json()["rows"]
    assert next(x for x in rows if x["id"] == d["id"])["email"] is None


def test_unanswerable_shows_what_we_can_answer(client, ask_ready):
    client.post("/api/agreement/accept", headers=USER)
    ask_ready["turns"] = [[use("cannot_answer", 1, reason="no salaries", closest_questions=["Who runs Acme?"])],
                          [text("Sorry, we don't have salary data.")]]
    d = client.post("/api/my/ask", json={"question": "what does the Acme CEO earn?"}, headers=USER).json()
    assert d["status"] == "unanswerable" and d["unanswerable"]["closest"] == ["Who runs Acme?"]
    assert d["capabilities"] and "salaries" in d["not_available"]


def test_daily_limit(client, ask_ready, monkeypatch):
    me = {"Cf-Access-Authenticated-User-Email": "limit.user@example.com"}
    client.post("/api/agreement/accept", headers=me)
    monkeypatch.setattr(pf, "ASK_DAILY_LIMIT", 1)
    ask_ready["turns"] = [[text("Hi.")]]
    assert client.post("/api/my/ask", json={"question": "first one"}, headers=me).status_code == 200
    r = client.post("/api/my/ask", json={"question": "second one"}, headers=me)
    assert r.status_code == 429 and r.json()["status"] == "limit"


def test_model_failure_is_logged_politely(client, ask_ready, monkeypatch):
    client.post("/api/agreement/accept", headers=USER)
    ask_ready["turns"] = []          # the fake client raises IndexError on the first call
    d = client.post("/api/my/ask", json={"question": "anything"}, headers=USER).json()
    assert d["status"] == "error" and "logged" in d["answer"] and d["capabilities"]

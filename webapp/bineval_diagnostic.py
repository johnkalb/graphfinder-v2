def generate_bineval_questions(entity_name: str, context_text: str):
    """
    Returns a BinEval-style meta-prompt for diagnosing a retrieval failure.
    """
    return f"""
    You are an expert graph evaluator for sixdegrees.net.
    A user searched for the entity: "{entity_name}"
    The retrieval system returned the following context:
    ---
    {context_text[:2000]}
    ---

    Decompose the failure into these atomic binary questions and answer them:

    1. Is the entity explicitly mentioned in the provided context? (Yes/No)
    2. Is the entity present but under a different name or spelling variant? (Yes/No)
    3. Is the entity absent from this context but likely present in a broader source (e.g., Wikidata, SEC)? (Yes/No)
    4. Is the retrieval failure due to ambiguous name collision? (Yes/No)

    Provide the verdict (Yes/No) and a one-sentence explanation for each.
    """


def generate_feedback_diagnosis_prompt(category: str, feedback_text: str) -> str:
    """Meta-prompt for an automated first-pass triage of a tester's
    high-priority feedback report (bug/performance/confusion/trust/coverage
    -- see CASE_OPEN_CATEGORIES in test_department.py). Pure prompt
    construction only, no network call -- the actual Gemini request is made
    in pathfinder.py (_get_feedback_diagnosis), matching how
    _generate_path_narrative is factored: prompt-building stays here,
    HTTP/API-key handling stays where those conventions already live."""
    return f"""
You are triaging a user-submitted report for sixdegrees.net, a social-network
pathfinding research tool. The report was auto-classified as category
"{category}".

Report text:
---
{feedback_text.strip()[:2000]}
---

Produce a short, structured first-pass diagnosis for the operator:

1. Likely root cause (one sentence): is this most likely a data-coverage gap
   (the entity/relationship isn't in the underlying graph), a UI/rendering
   bug, a performance/latency problem, a misunderstanding of how the tool
   works, or something else? Say which, plainly.
2. Confidence (High/Medium/Low) in that assessment given only the report
   text -- be honest if the text is too vague to tell.
3. Suggested next step for the operator (one sentence): e.g. "check whether
   {{entity}} exists in the graph", "check recent error logs for this route",
   "reply asking for a screenshot/reproduction steps", etc.

Do not speculate beyond what the report text supports. If the text doesn't
give enough information for a specific answer, say so explicitly rather than
guessing. Keep the entire response under 120 words, plain text, no markdown.
"""

"""Link scoring: combine relationship evidence into a single
P(would take a phone call) per pair, using a noisy-OR model.

Each editorial probability P_c = P(would take call | this category links the pair).
Combining independent evidence by noisy-OR:
    P(call) = 1 - prod_over_used(1 - P_c)
Interpretation: "at least one of these relationships is strong enough that they'd
take the call." More evidence only increases confidence, and it saturates gently
(no naive-Bayes overconfidence). The editorial probabilities are used directly.

Independence handling:
  - Correlated co-employment categories (CO_OFFICER, CO_DIRECTOR, CO_EXECUTIVE,
    EMPLOYMENT, SEC_INSIDER) collapse to a SINGLE strongest signal, because they
    almost always derive from the same shared company (the source data does not
    retain which company, so we conservatively count co-employment once).
  - All other categories contribute independently.

Path probability = product of per-pair posteriors == sum of -log(p) edge weights,
so the most-probable path is the shortest path in -log space (Dijkstra), and
k-shortest-paths in -log space yields paths in decreasing probability order.
"""
import math

# Editorial P(would take call | only this single category links the pair).
CATEGORY_PROB = {
    "FAMILY": 0.92,
    "FRIEND": 0.90,
    "COMMUNICATED": 0.80,
    "CO_EXECUTIVE": 0.87,
    "CO_DIRECTOR": 0.87,
    "CO_OFFICER": 0.90,
    "EMPLOYMENT": 0.70,
    "TRAVEL_MET": 0.70,
    "ADVISORY": 0.70,
    "MEMBERSHIP": 0.50,
    "EDUCATION": 0.50,
    "DONATION": 0.10,
    "FINANCIAL": 0.60,
    "LOBBYING": 0.80,
    "STAFF_AIDE": 0.90,
    "PUBLIC_OFFICE": 0.60,
    "FELLOW_OFFICEHOLDER": 0.75,
    "SEC_INSIDER": 0.50,
    "NEWS_COMENTION": 0.10,
    "CO_OCCURS_DOC": 0.05,
    "WEAK_SOCIAL": 0.20,
    "SAME_ORG_OVERLAP": 0.18,
    "SAME_SCHOOL_OVERLAP": 0.12,
    "OTHER": 0.05,
}

# Categories that derive from the same shared employer -> count once (strongest).
CO_EMPLOYMENT = {"CO_OFFICER", "CO_DIRECTOR", "CO_EXECUTIVE", "EMPLOYMENT",
                 "SEC_INSIDER", "SAME_ORG_OVERLAP"}

# Categories that are NOT relationships (same-entity markers) -> ignore / merge nodes.
NON_RELATION = {"SAME_ENTITY"}

# Human-readable label + how the probability was established, per category.
CATEGORY_DESC = {
    "FAMILY": ("Family / Spouse", "Family or marital tie. Among the strongest signals — family members almost always take each other's calls."),
    "FRIEND": ("Friend", "Explicitly documented friendship (including close, childhood, or romantic ties). Stated friendship strongly implies a call would be accepted."),
    "COMMUNICATED": ("Direct Communication", "Documented calls, emails, or correspondence between the parties. Direct prior contact makes a future call likely."),
    "CO_OFFICER": ("Co-Officer", "Both served as officers of the same company (SEC filings). Corporate colleagues at the officer level typically know each other."),
    "CO_DIRECTOR": ("Co-Director", "Both served on the same company's board (SEC filings). Fellow directors interact directly in board settings."),
    "CO_EXECUTIVE": ("Co-Executive", "Both held senior executive roles, generally at the same organization. Senior colleagues are likely to take each other's calls."),
    "EMPLOYMENT": ("Employment", "An employment relationship connected the parties. Working together implies meaningful acquaintance."),
    "TRAVEL_MET": ("Travel / Meeting", "Documented joint travel or a recorded meeting. In-person contact supports call acceptance."),
    "ADVISORY": ("Advisory Role", "Advisory-board, trustee, or advisor relationship. Advisory ties imply ongoing professional contact."),
    "MEMBERSHIP": ("Shared Membership", "Shared membership or affiliation in an organization. Common membership is a moderate signal — members don't always know one another."),
    "EDUCATION": ("Shared Education", "Attended the same institution. A moderate signal — alumni overlap doesn't guarantee acquaintance."),
    "DONATION": ("Political Donation", "One party donated to the other's campaign (campaign-finance records). A weak signal — most donors never speak with recipients."),
    "FINANCIAL": ("Financial Transaction", "A payment, grant, or fund commitment between the parties. Financial dealings imply some direct contact."),
    "LOBBYING": ("Lobbying", "A documented lobbying relationship. Lobbyists typically have direct access to their targets."),
    "STAFF_AIDE": ("Staff / Aide", "A staff or aide relationship (works directly for). Close working proximity strongly implies call acceptance."),
    "PUBLIC_OFFICE": ("Public Office", "Both connected through holding public office. A moderate signal depending on overlap."),
    "FELLOW_OFFICEHOLDER": ("Fellow Officeholder", "Served in the same body (e.g. governors, judges, justices). Peers in the same institution generally know each other."),
    "SEC_INSIDER": ("SEC Insider", "Insider, 10%-owner, or filer relationship at the same company. A moderate corporate signal."),
    "NEWS_COMENTION": ("News Co-mention", "Both named in the same news article (GDELT). A weak signal — co-mention in news does not imply acquaintance."),
    "CO_OCCURS_DOC": ("Document Co-occurrence", "Both names appear in the same Epstein estate document. The weakest signal — co-occurrence is not evidence of a real relationship."),
    "WEAK_SOCIAL": ("Weak Social Tie", "Mentor, neighbor, acquaintance, or roommate. A weak-to-moderate informal tie."),
    "SAME_ORG_OVERLAP": ("Same Workplace (overlapping years)", "Both worked at the same organization during overlapping years (Wikidata employment dates). A weak-to-moderate signal — colleagues at a large employer may never have met, but overlapping tenure raises the odds."),
    "SAME_SCHOOL_OVERLAP": ("Same School (overlapping years)", "Both attended the same institution during overlapping years (Wikidata education dates). A weak signal — large schools dilute the chance of acquaintance, but contemporaneous enrollment is suggestive."),
    "OTHER": ("Other", "An uncategorized relationship. Treated as very weak evidence."),
}

METHODOLOGY = (
    "Every connection is assigned a probability answering one question: "
    "\u201cif this were the only link between two people, how likely is it that one would (or would have) taken the other's phone call?\u201d "
    "These per-category probabilities are editorial estimates based on what each relationship type actually implies "
    "about real acquaintance.\n\n"
    "When two people share several relationships, the evidence is combined with a noisy-OR model: "
    "P = 1 \u2212 \u220f(1 \u2212 p\u1d62). Intuitively, the connection is at least as strong as its strongest single link, and additional "
    "links can only raise confidence \u2014 never lower it. Correlated corporate links (co-officer, co-director, etc.) are counted "
    "once, since they usually come from the same shared company.\n\n"
    "A path's overall viability is the product of its links' probabilities \u2014 the chance a warm introduction would succeed at "
    "every step from start to end. Because each step can only reduce the product, a short chain of strong links can beat a long "
    "chain of weak ones; the \u201cbest path\u201d shown is the one with the highest overall probability, which is not always the "
    "shortest. Alternate paths are ranked by probability so you can apply your own private knowledge."
)

_DEFAULT_P = CATEGORY_PROB["OTHER"]


def dedup_categories(categories):
    """Return the deduplicated list of categories actually used for scoring."""
    cats = set(c for c in categories if c not in NON_RELATION)
    if not cats:
        return []
    used = []
    coemp = [c for c in cats if c in CO_EMPLOYMENT]
    if coemp:
        used.append(max(coemp, key=lambda c: CATEGORY_PROB.get(c, _DEFAULT_P)))
    for c in cats:
        if c not in CO_EMPLOYMENT:
            used.append(c)
    return used


def score_pair(categories):
    """Given the categories linking a pair, return (probability, used_categories)
    via noisy-OR. Returns (None, []) if no scorable relationship."""
    used = dedup_categories(categories)
    if not used:
        return None, []
    prod = 1.0
    for c in used:
        prod *= (1.0 - CATEGORY_PROB.get(c, _DEFAULT_P))
    return 1.0 - prod, used


def neg_log_weight(probability):
    """Convert a pair probability to a non-negative edge weight for shortest-path.
    Higher probability -> lower weight. Most-probable path = min sum of weights."""
    p = min(max(probability, 1e-9), 1 - 1e-9)
    return -math.log(p)

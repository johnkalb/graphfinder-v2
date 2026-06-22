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
    "OTHER": 0.05,
}

# Categories that derive from the same shared employer -> count once (strongest).
CO_EMPLOYMENT = {"CO_OFFICER", "CO_DIRECTOR", "CO_EXECUTIVE", "EMPLOYMENT", "SEC_INSIDER"}

# Categories that are NOT relationships (same-entity markers) -> ignore / merge nodes.
NON_RELATION = {"SAME_ENTITY"}

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

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
    "A path's overall viability has TWO parts. First, the link strength: the product of every step's "
    "take-the-call probability. Second, the forwarding factor: reaching someone through a chain also "
    "requires each INTERMEDIARY to actually pass you along, not just take your call. Milgram's original "
    "experiment (~20% of chains completed) and Dodds, Muhamad & Watts (2003, ~37% passed each message on) "
    "show this forwarding step is the main reason long chains fail. We model it as FORWARD_PROB (default 0.37) "
    "raised to the number of intermediaries (hops minus one). A direct, one-hop connection has no intermediary, "
    "so it takes no forwarding penalty; every extra link multiplies the odds down sharply. This is why a short "
    "chain of strong links can far outscore a long chain, and why paths beyond two or three hops are honestly "
    "labeled tenuous. The forwarding rate is the single softest assumption in the model and is treated as a "
    "tunable parameter \u2014 a motivated, system-guided search (where you stay involved at every step and each "
    "person need only make one easy introduction) plausibly does better than the passive 37%. The \u201cbest path\u201d "
    "shown is the one with the highest combined probability, which is not always the shortest. Alternate paths "
    "are ranked by probability so you can apply your own private knowledge."
)

_DEFAULT_P = CATEGORY_PROB["OTHER"]

# ---------------------------------------------------------------------------
# Forwarding probability (the chain-completion factor).
#
# A path existing in the graph is NOT the same as a real introduction
# succeeding. Each INTERMEDIARY in a chain must be willing to make the
# follow-on introduction, not just take the call. Milgram (~20% completion)
# and Dodds/Muhamad/Watts 2003 (~37% per-step pass-along in a low-incentive
# email experiment) show this forwarding step is the dominant reason long
# chains fail.
#
# Model: P(chain) = [product of edge take-call probs] * FORWARD_PROB^(hops-1)
#   - The initiator (you) is motivated -> no forwarding factor for the 1st hop.
#   - The target is the destination -> does not forward.
#   - Only the (hops-1) INTERMEDIARIES must choose to forward.
#   - A direct 1-hop link gets FORWARD_PROB^0 = 1 (no penalty) -- correct.
#
# FORWARD_PROB is the single weakest assumption in the whole model and is
# deliberately exposed as a tunable parameter. 0.37 is the Watts/Dodds passive
# figure; real motivated use likely runs higher (DARPA Red Balloon, Pickard
# 2011, showed incentive dominates).
FORWARD_PROB = 0.37          # passive / unguided chain (default display)

# "Build My Path" GUIDED mode. Here the motivated user stays in the loop at
# every step: after B introduces them to C, the USER personally contacts C with
# B's warm intro. So downstream hops are NOT passive forwarding -- they are
# warm-intro favors with report-back accountability. Two-tier per-hop rates:
#   - The user's OWN committed contact (first relay): high follow-through,
#     because agreeing then ghosting carries social cost (they must report the
#     intro was made). Net of P(agree) x P(follow-through | agree) ~ 0.85.
#   - DOWNSTREAM warm-intro hops: a favor for a friend-of-a-friend, warm and
#     motivated but a weaker tie -> ~0.60 (between own-contact and passive).
FORWARD_GUIDED_OWN = 0.85        # first relay: user's own committed contact
FORWARD_GUIDED_DOWNSTREAM = 0.60 # subsequent warm-intro relays


def path_probability(edge_probs, forward_prob=FORWARD_PROB):
    """Passive combination: link strength x forward_prob^(intermediaries).

    edge_probs: list of per-hop P(take call), length = number of hops.
    Returns (combined_prob, link_component, forward_component).
    """
    link = 1.0
    for p in edge_probs:
        link *= p
    n_relays = max(0, len(edge_probs) - 1)   # intermediaries who must forward
    forward = forward_prob ** n_relays
    return link * forward, link, forward


def path_probability_guided(edge_probs,
                            own=FORWARD_GUIDED_OWN,
                            downstream=FORWARD_GUIDED_DOWNSTREAM):
    """Guided (Build My Path) combination with two-tier per-relay forwarding.

    Relays are the (hops-1) intermediaries. The FIRST relay is the user's own
    committed contact (rate `own`); every subsequent relay is a downstream
    warm-intro hop (rate `downstream`). A direct 1-hop link has no relay and
    therefore no forwarding penalty -- same as passive.

    Returns (combined_prob, link_component, forward_component).
    """
    link = 1.0
    for p in edge_probs:
        link *= p
    n_relays = max(0, len(edge_probs) - 1)
    forward = 1.0
    for r in range(n_relays):
        forward *= own if r == 0 else downstream
    return link * forward, link, forward



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

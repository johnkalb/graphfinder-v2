"""Shared detector for GDELT-sourced person names corrupted by fused-word
artifacts (e.g. "donald trumpannounced", "kash patelsaid").

Root cause: GDELT's own GKG entity-extraction pipeline occasionally drops
the space/sentence-boundary between a name and the verb that follows it in
the source article ("Trump announced..." -> "Trumpannounced"). The raw
Persons column already ships lowercase and pre-corrupted -- this is upstream
GDELT noise, not something introduced by our harvesters. The harvesters
currently pass every name through verbatim with no validation.

Tuned for high precision over recall: only flags a token when it is long
(>=13 chars) AND ends with one of a set of distinctive multi-syllable
reporting-verb suffixes. Short suffixes (is/on/was/met/etc.) are deliberately
excluded because they collide constantly with real surnames (e.g.
"malliotakis", "horne-francis"), which showed up as false positives during
tuning against the live production data.
"""
import re

_VERB_SUFFIXES = [
    "announced", "attended", "endorsed", "nominated", "reported", "appointed",
    "resigned", "testified", "confirmed", "criticized", "promised", "threatened",
    "apologized", "admitted", "explained", "responded", "continued", "suggested",
    "revealed", "insisted", "welcomed", "remained", "believes", "defended",
    "rejected", "demanded", "declared", "disputed", "dismissed", "warned",
    "pleaded", "vetoed", "tweeted", "posted", "stated", "claimed", "argued",
    "blasted", "slammed", "praised", "visited", "hosted",
]
_SUFFIX_RE = re.compile("(" + "|".join(_VERB_SUFFIXES) + ")$")
_MIN_TOKEN_LEN = 13


def is_fused_name(name: str) -> bool:
    """Return True if any whitespace-token in `name` looks like a
    name+reporting-verb fusion artifact."""
    if not name:
        return False
    for tok in name.split():
        if len(tok) >= _MIN_TOKEN_LEN and _SUFFIX_RE.search(tok):
            return True
    return False

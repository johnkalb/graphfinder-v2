"""GDELT co-occurrence relation classification -- productionized from this
session's validated prototype (see specifications/gdelt-cooccurrence-
classification.md). Turns a raw "these two people were mentioned in the same
article" signal into a specific, confident relation type, using:

  1. Chunked REBEL (Babelscape/rebel-large) relation extraction -- a plain
     512-token truncation silently drops real relations in longer articles
     (confirmed: Home Depot/Arthur Blank, an athlete's event participation
     both sat past the cutoff); a token-aware sliding window recovers them.
  2. Cross-document corroboration as the confidence signal -- validated as
     the thing that actually distinguishes real relations from
     hallucinations. Same-document self-consistency (multi-beam resampling
     of one article) does NOT work: a confirmed hallucination
     ("John Swinney member of Scottish Labour") scored 10/10 beam votes,
     higher than the true fact (1/10) -- resampling catches random noise,
     not a systematic misread the model repeats every time it sees the same
     text. Requiring >=2 INDEPENDENT articles to agree is what worked,
     validated on a real true positive (Markle/Harry -> spouse, 6/7 sources)
     and a real true negative (McGowan/Turner -- 5 candidate URLs collapsed
     to 1 independent source after de-duplication, correctly not promoted).
  3. Near-duplicate detection before counting sources -- without it, one
     wire story syndicated across N sites looks like "N independent sources
     agree", which would systematically let syndicated content out-vote
     genuine independent reporting. difflib similarity >=0.7 cleanly
     separates real duplicates (0.99-1.00 observed) from independent
     articles (<0.5 observed).

Deliberately does NOT import webapp/ code (see webapp/relation_categories.py's
own header comment: no harvester imports webapp code today, and this isn't
the place to start) -- the GDELT DOC API query function below is a close
duplicate of webapp/mentioned_with_fallback.py's search_gdelt_comention(),
not a shared import.
"""
from __future__ import annotations

import difflib
import logging
import threading
import time
import warnings
from collections import defaultdict

logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore", category=UserWarning, module="transformers")

DUP_THRESHOLD = 0.7  # validated: real duplicates >=0.99, independent articles <0.5
PROMOTION_THRESHOLD = 2  # independent sources required to agree (user-set, per session discussion)
CHUNK_MAX_TOKENS = 450
CHUNK_OVERLAP_TOKENS = 50
REBEL_NUM_SEQUENCES = 6

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
_GDELT_MIN_INTERVAL = 10.0  # GDELT states ~1 req/5s per caller, but a live test
                            # (2026-09-13) got 429 on every request despite
                            # honoring that exact interval -- confirmed via an
                            # isolated bare requests.get() with no prior calls
                            # from that process at all, so it's IP-level
                            # throttling/degradation on GDELT's side, not our
                            # request spacing. Doubled as a defensive margin;
                            # does not fix an externally-degraded/overloaded
                            # GDELT DOC API, which just needs to clear on its
                            # own or be retried later.
_gdelt_lock = threading.Lock()
_gdelt_last_call = [0.0]

# REBEL emits Wikidata-property-style English labels ("spouse", "founded by",
# "member of political party"). categorize() in webapp/relation_categories.py
# already recognizes a large existing relation_type vocabulary (confirmed by
# reading it directly) -- most single-word labels match it automatically once
# uppercased with underscores (e.g. "employer" -> "EMPLOYER", "advisor" ->
# "ADVISOR"), so this table only needs entries for labels whose naive
# transform would NOT already match something categorize() recognizes.
_RELATION_TYPE_MAP = {
    "spouse": "SPOUSE",
    # NOT "ROMANTIC_PARTNER" -- categorize()'s CO_EXECUTIVE bucket does a
    # substring match on "PARTNER" (for law-firm/business partners) BEFORE
    # its later exact-match FRIEND list is even reached, so
    # "ROMANTIC_PARTNER" is caught there first and miscategorized as
    # CO_EXECUTIVE (confirmed directly). "FRIEND" reaches the correct
    # bucket since it doesn't collide with any earlier substring check.
    "unmarried partner": "FRIEND",
    "partner": "FRIEND",
    "father": "FAMILY",
    "mother": "FAMILY",
    "child": "FAMILY",
    "sibling": "FAMILY",
    "relative": "FAMILY",
    "father-in-law": "FAMILY",
    "mother-in-law": "FAMILY",
    "stepparent": "FAMILY",
    "founded by": "FOUNDER",
    "founder of": "FOUNDER",
    "chief executive officer": "CHIEF_EXECUTIVE_OFFICER",
    "board member": "BOARD_MEMBER",
    "member of political party": "MEMBER_OF",
    "member of sports team": "MEMBER_OF",
    "member of": "MEMBER_OF",
    "employer": "EMPLOYER",
    "educated at": "EDUCATED_AT",
    "alma mater": "ALMA_MATER",
    "advisor": "ADVISOR",
    "candidacy in election": "CANDIDATE",
    "replaces": "FELLOW_OFFICEHOLDER",
    "replaced by": "FELLOW_OFFICEHOLDER",
}


class GdeltTransientError(Exception):
    """Rate-limited (429) or otherwise temporarily unavailable -- distinct
    from a confirmed empty result set, matching
    webapp/mentioned_with_fallback.py's contract of the same name so a blip
    is never mistaken for genuinely no coverage."""


def normalize_relation_type(rebel_label: str) -> str:
    """Map a REBEL-extracted label to a relation_type string
    webapp/relation_categories.py's categorize() will recognize -- no
    changes to that file needed. Unmapped labels pass through
    uppercased/underscored; categorize()'s OTHER fallback (weight 0.05,
    see webapp/link_scoring.py) handles them safely, same as any other
    harvester's occasional odd relation_type today."""
    key = (rebel_label or "").strip().lower()
    if key in _RELATION_TYPE_MAP:
        return _RELATION_TYPE_MAP[key]
    return key.upper().replace(" ", "_").replace("-", "_")


# --- REBEL model (loaded lazily, once per process -- ~11.6s/chunk CPU
# inference measured this session, so re-loading per call would be a real
# cost, not just a style choice) ---

_tokenizer = None
_model = None


def _get_model():
    global _tokenizer, _model
    if _model is None:
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        logger.info("loading Babelscape/rebel-large...")
        _tokenizer = AutoTokenizer.from_pretrained("Babelscape/rebel-large")
        _model = AutoModelForSeq2SeqLM.from_pretrained("Babelscape/rebel-large")
        logger.info("REBEL loaded.")
    return _tokenizer, _model


def extract_triplets(text: str) -> list[dict]:
    """Standard REBEL output parser: pulls <triplet>/<subj>/<obj>-delimited
    generated text apart into {head, type, tail} dicts."""
    triplets = []
    relation, subject, object_ = "", "", ""
    current = "x"
    for token in text.replace("<s>", "").replace("<pad>", "").replace("</s>", "").split():
        if token == "<triplet>":
            current = "t"
            if relation != "":
                triplets.append({"head": subject.strip(), "type": relation.strip(), "tail": object_.strip()})
                relation = ""
            subject = ""
        elif token == "<subj>":
            current = "s"
            if relation != "":
                triplets.append({"head": subject.strip(), "type": relation.strip(), "tail": object_.strip()})
            object_ = ""
        elif token == "<obj>":
            current = "o"
            relation = ""
        else:
            if current == "t":
                subject += " " + token
            elif current == "s":
                object_ += " " + token
            elif current == "o":
                relation += " " + token
    if subject != "" and relation != "" and object_ != "":
        triplets.append({"head": subject.strip(), "type": relation.strip(), "tail": object_.strip()})
    return triplets


def chunk_text(text: str, max_tokens: int = CHUNK_MAX_TOKENS, overlap_tokens: int = CHUNK_OVERLAP_TOKENS) -> list[str]:
    """Token-aware sliding window -- fixes REBEL's 512-token truncation
    silently dropping real relations in longer articles (confirmed this
    session on two real cases)."""
    tokenizer, _ = _get_model()
    ids = tokenizer.encode(text, add_special_tokens=False)
    chunks = []
    start = 0
    while start < len(ids):
        end = min(start + max_tokens, len(ids))
        chunks.append(tokenizer.decode(ids[start:end], skip_special_tokens=True))
        if end >= len(ids):
            break
        start = end - overlap_tokens
    return chunks


def run_rebel(text: str, num_sequences: int = REBEL_NUM_SEQUENCES) -> list[dict]:
    """Chunked, multi-beam REBEL extraction over one document's full text.
    Multi-beam surfaces more candidate relations per chunk than greedy
    decode; same-document self-consistency across those beams is NOT used
    as a confidence signal (validated this session not to work) -- that
    signal comes from classify_pair()'s cross-document corroboration
    instead."""
    tokenizer, model = _get_model()
    triplets_all = []
    seen = set()
    for chunk in chunk_text(text):
        inputs = tokenizer(chunk, max_length=512, truncation=True, return_tensors="pt")
        outputs = model.generate(
            **inputs, max_length=256, num_beams=num_sequences, num_return_sequences=num_sequences
        )
        decoded = tokenizer.batch_decode(outputs, skip_special_tokens=False)
        for sentence in decoded:
            for t in extract_triplets(sentence):
                key = (t["head"].lower(), t["type"].lower(), t["tail"].lower())
                if key not in seen:
                    seen.add(key)
                    triplets_all.append(t)
    return triplets_all


def collapse_near_duplicates(docs: list[tuple[str, str]]) -> list[tuple[tuple[str, str], list[str]]]:
    """docs: list of (url, text). Returns one representative (url, text) per
    cluster of near-identical documents, so a wire story syndicated across
    N sites counts as ONE independent source, not N (validated: the
    McGowan/Turner fix -- 5 URLs of one AAP wire story collapsed to 1)."""
    reps: list[tuple[tuple[str, str], list[str]]] = []
    for url, text in docs:
        if not text:
            continue
        placed = False
        for (rep_url, rep_text), urls_in_cluster in reps:
            ratio = difflib.SequenceMatcher(None, text, rep_text).ratio()
            if ratio >= DUP_THRESHOLD:
                urls_in_cluster.append(url)
                placed = True
                break
        if not placed:
            reps.append(((url, text), [url]))
    return reps


def classify_from_docs(name_a: str, name_b: str, docs: list[tuple[str, str]],
                        promotion_threshold: int = PROMOTION_THRESHOLD) -> dict:
    """Core corroboration pipeline over already-fetched (url, text) pairs --
    kept separate from classify_pair() so regression tests can replay
    cached article text without re-fetching. Returns:
      {"promoted": {relation_type: [supporting urls]}, "n_independent": int,
       "all_relation_docs": {relation_type: [supporting urls]}}
    Never a silent black box -- the full evidence trail is always returned,
    promoted or not."""
    clusters = collapse_near_duplicates(docs)
    n_independent = len(clusters)

    a, b = name_a.lower(), name_b.lower()
    relation_docs: dict[str, set] = defaultdict(set)
    for (rep_url, text), _urls_in_cluster in clusters:
        triplets = run_rebel(text)
        direct = [
            t for t in triplets
            if (a in t["head"].lower() or a in t["tail"].lower())
            and (b in t["head"].lower() or b in t["tail"].lower())
        ]
        for t in direct:
            relation_docs[t["type"].lower()].add(rep_url)

    promoted = {rel: sorted(urls) for rel, urls in relation_docs.items() if len(urls) >= promotion_threshold}
    return {
        "promoted": promoted,
        "n_independent": n_independent,
        "all_relation_docs": {rel: sorted(urls) for rel, urls in relation_docs.items()},
    }


def fetch_article(url: str) -> str | None:
    """trafilatura-based article text fetch. ~1s/doc, ~83-85% success rate
    observed this session (paywalls/dead links account for the rest)."""
    import trafilatura
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        return trafilatura.extract(downloaded)
    except Exception as e:
        logger.warning("article fetch failed for %s: %s", url, e)
        return None


def classify_pair(name_a: str, name_b: str, candidate_urls: list[str],
                   promotion_threshold: int = PROMOTION_THRESHOLD) -> dict:
    """Full pipeline: fetch each candidate URL -> dedup -> chunked REBEL per
    independent document -> promote a relation only if promotion_threshold+
    independent sources agree. This is what gdelt_classify_new_people.py
    calls; classify_from_docs() is the reusable core for testing against
    already-fetched text."""
    docs = []
    for url in candidate_urls:
        text = fetch_article(url)
        if text:
            docs.append((url, text))
    result = classify_from_docs(name_a, name_b, docs, promotion_threshold)
    result["fetched"] = len(docs)
    result["candidates"] = len(candidate_urls)
    return result


def search_gdelt_comention(name_a: str, name_b: str, timeout: int = 25, maxrecords: int = 10) -> list[dict]:
    """Live query against GDELT's DOC 2.0 API for candidate article URLs
    mentioning both names. Deliberate near-duplicate of
    webapp/mentioned_with_fallback.py's function of the same name (not a
    shared import -- see module docstring) with the same rate-limit
    discipline and transient-error contract."""
    import requests
    t0 = time.time()
    with _gdelt_lock:
        wait = _GDELT_MIN_INTERVAL - (time.time() - _gdelt_last_call[0])
        if wait > 0:
            time.sleep(wait)
        query = f'"{name_a}" "{name_b}"'
        params = {"query": query, "mode": "artlist", "maxrecords": maxrecords, "format": "json", "sort": "datedesc"}
        try:
            r = requests.get(GDELT_DOC_API, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            logger.warning("GDELT request failed after %.1fs: %s", time.time() - t0, e)
            raise GdeltTransientError(f"request failed: {e}")
        finally:
            _gdelt_last_call[0] = time.time()
    elapsed = time.time() - t0
    if r.status_code != 200:
        logger.warning("GDELT HTTP %s after %.1fs, body: %s", r.status_code, elapsed, r.text[:300])
        raise GdeltTransientError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except Exception as e:
        logger.warning("GDELT returned unparseable JSON after %.1fs: %s -- body: %s", elapsed, e, r.text[:300])
        raise GdeltTransientError(f"bad response: {e}")
    articles = data.get("articles") or []
    logger.info("GDELT search for %r <-> %r: %d articles in %.1fs", name_a, name_b, len(articles), elapsed)
    return [{"url": a.get("url", ""), "title": a.get("title", ""), "seendate": a.get("seendate", "")}
            for a in articles if a.get("url")]

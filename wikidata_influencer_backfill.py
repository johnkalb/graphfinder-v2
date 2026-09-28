#!/usr/bin/env python3
"""Wikidata backfill: US online influencers -- YouTubers, podcasters,
influencers, streamers, TikTokers (2026-09-28).

source_data='WIKIDATA_INFLUENCERS' (reversible:
    DELETE FROM relationships WHERE source_data='WIKIDATA_INFLUENCERS';)

Wikidata barely records who appears in whose videos, so influencers connect
through what it does record (the user accepted this caveat 2026-09-28):
  * PERSON -FOUNDER_OF-> company/org they founded (P112, inverse)  -> CO_EXECUTIVE
    (not "FOUNDER": build_scored_edges' POS_RELS conflation filter drops exact
    FOUNDER edges whose target merely *looks* like a person name, e.g.
    "Kylie Cosmetics")
  * PERSON -EMPLOYEE-> employer (P108)                -> EMPLOYMENT
  * PERSON -MEMBER_OF-> group/org (P463)              -> MEMBERSHIP
  * PERSON -AWARD_RECEIVED-> award (P166)             -> AWARD
  * PERSON -SPOUSE/FAMILY-> PERSON (P26, P3373/P22/P25/P40)  -> FAMILY
  * PERSON -ROMANTIC_PARTNER-> PERSON (P451, incl. past)      -> FRIEND
  (YouTube "Play Button" milestones are not treated as awards.)
Follower counts (P8687, max across platforms) ride along in each row's evidence.

Identity is wikidata_media_backfill.resolve_people() -- same rules, separate
cache dir. Usage mirrors that script:
    python wikidata_influencer_backfill.py            # dry run
    python wikidata_influencer_backfill.py --commit   # write to Postgres
"""
import json
import os
import re
import sys
from collections import Counter

os.environ.setdefault("WD_MEDIA_CACHE", r"C:\Users\johnk\graphfinder-clean\data\wd_influencers")
import wikidata_media_backfill as m  # noqa: E402  (reads WD_MEDIA_CACHE at import)

SOURCE = "WIKIDATA_INFLUENCERS"
OCCUPATIONS = {"YouTuber": "Q17125263", "influencer": "Q2906862", "TikToker": "Q94791573",
               "streamer": "Q50279140", "podcaster": "Q15077007"}
# P451 "unmarried partner" includes past relationships -> ROMANTIC_PARTNER
# (categorized FRIEND, 0.90) rather than SPOUSE/FAMILY.
FAMILY_PROPS = {"P26": "SPOUSE", "P451": "ROMANTIC_PARTNER", "P3373": "FAMILY", "P22": "FAMILY",
                "P25": "FAMILY", "P40": "FAMILY"}
PERSON_RELS = set(FAMILY_PROPS.values())
# YouTube "Play Buttons" are subscriber milestones given to tens of thousands of
# channels -- as AWARD edges they'd link unrelated YouTubers. Not an award.
SKIP_AWARD = re.compile(r"play button", re.I)


def fetch_influencers():
    """{qid: {"occupations": [...], "followers": max P8687 or 0}} for US people."""
    out = {}
    for occ, q in OCCUPATIONS.items():
        for r in m.sparql(f"SELECT ?p (MAX(?f) AS ?fol) WHERE {{ ?p wdt:P106 wd:{q} ; wdt:P27 wd:Q30 . "
                          f"OPTIONAL {{ ?p wdt:P8687 ?f }} }} GROUP BY ?p"):
            p = m.qid(r, "p")
            rec = out.setdefault(p, {"occupations": [], "followers": 0})
            rec["occupations"].append(occ)
            if m.val(r, "fol"):
                rec["followers"] = max(rec["followers"], int(float(m.val(r, "fol"))))
        m.log(f"  influencers: {occ} done, {len(out):,} people so far")
    return out


def fetch_links(people):
    """[(person, relation, target_qid, target_label, target_is_human)]."""
    fam_values = " ".join(f"wdt:{p}" for p in FAMILY_PROPS)
    out = []
    for n, b in enumerate(m.batched(sorted(people), 50)):
        q = f"""SELECT ?p ?rel ?o ?oLabel ?human WHERE {{
          VALUES ?p {{ {m.values(b)} }}
          {{ ?o wdt:P112 ?p . BIND("FOUNDER_OF" AS ?rel) }}
          UNION {{ ?p wdt:P108 ?o . BIND("EMPLOYEE" AS ?rel) }}
          UNION {{ ?p wdt:P463 ?o . BIND("MEMBER_OF" AS ?rel) }}
          UNION {{ ?p wdt:P166 ?o . BIND("AWARD_RECEIVED" AS ?rel) }}
          UNION {{ VALUES ?fp {{ {fam_values} }} ?p ?fp ?o . BIND(STR(?fp) AS ?rel) }}
          OPTIONAL {{ ?o wdt:P31 wd:Q5 . BIND(true AS ?human) }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }} }}"""
        for r in m.sparql(q):
            # family links keep the raw property id (P26, P451...); mapped in main()
            rel = m.val(r, "rel").rsplit("/", 1)[-1]
            out.append((m.qid(r, "p"), rel, m.qid(r, "o"), m.val(r, "oLabel"), bool(m.val(r, "human"))))
        if n % 10 == 0:
            m.log(f"  links: {min((n + 1) * 50, len(people)):,}/{len(people):,} people, {len(out):,} links")
    return out


def main():
    commit = "--commit" in sys.argv
    infl = m.cached("influencers", fetch_influencers)
    links = [(p, FAMILY_PROPS.get(rel, rel), o, lab, h)
             for p, rel, o, lab, h in m.cached("links_v2", lambda: fetch_links(set(infl)))]
    links = [lk for lk in links if not (lk[1] == "AWARD_RECEIVED" and SKIP_AWARD.search(lk[3] or ""))]
    m.log(f"influencers={len(infl):,} | links={len(links):,} " +
          str(Counter(r for _, r, _, _, _ in links).most_common()))

    # People to place in the graph: the influencers plus any human link target
    # (family members, and the rare human "employer"/"founded" target).
    humans = set(infl) | {o for _, _, o, _, h in links if h}
    labels = m.repair_labels("person_labels",
                             m.cached("person_labels", lambda: m.fetch_labels(humans, with_desc=True)),
                             with_desc=True)
    own_targets = {}   # identity evidence: what this import links each person to
    for p, _, o, olabel, _ in links:
        name = (labels.get(o) or [olabel])[0] if o in labels else olabel
        if name:
            own_targets.setdefault(p, set()).add(m.canon_key(name))
    node_name, gnodes, key_to_idx = m.resolve_people(humans, labels, own_targets)

    rows = set()
    skipped = Counter()
    for p, rel, o, olabel, human in links:
        if p not in node_name:
            skipped["influencer unresolved"] += 1
            continue
        ev = json.dumps({"qid": p, "target_qid": o, "followers": infl.get(p, {}).get("followers", 0),
                         "occupations": infl.get(p, {}).get("occupations", []), "src": "wdqs influencers"})
        if human:
            if rel not in PERSON_RELS or o not in node_name:
                skipped[f"human target for {rel}"] += 1
                continue
            rows.add((node_name[p], "PERSON", node_name[o], "PERSON", rel, ev))
        else:
            if not olabel or re.fullmatch(r"Q\d+", olabel):
                skipped["target without label"] += 1
                continue
            if rel in PERSON_RELS:
                skipped["non-human family target"] += 1
                continue
            k = m.canon_key(olabel)
            target = gnodes[key_to_idx[k]] if k in key_to_idx else olabel
            rows.add((node_name[p], "PERSON", target, "ORG", rel, ev))
    uniq = {}
    for r in rows:
        uniq.setdefault((r[0], r[2], r[4]), r)
    rows = list(uniq.values())
    m.log(f"skipped: {dict(skipped)}")
    m.log(f"rows to write: {len(rows):,}  " + ", ".join(f"{k}={v:,}" for k, v in Counter(r[4] for r in rows).most_common()))
    m.log(f"distinct people: {len({r[0] for r in rows}):,} | distinct targets: {len({r[2] for r in rows}):,}")
    for who in ("MrBeast", "Kim Kardashian", "Joe Rogan", "Logan Paul", "Charli D'Amelio", "Emma Chamberlain"):
        mine = [r for r in rows if r[0] == who or r[0].startswith(who + " (")][:6]
        m.log(f"  sample {who}: " + "; ".join(f"{r[4]} -> {r[2]}" for r in mine))
    with open(os.path.join(m.CACHE_DIR, "rows_preview.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if not commit:
        m.log("dry run -- pass --commit to write")
        return
    import psycopg2
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                        "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                   [(None, s, st, None, t, tt, rel, SOURCE, ev) for s, st, t, tt, rel, ev in rows], page_size=5000)
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    m.log(f"committed. rows tagged {SOURCE}: {cur.fetchone()[0]:,}")
    conn.close()


if __name__ == "__main__":
    main()

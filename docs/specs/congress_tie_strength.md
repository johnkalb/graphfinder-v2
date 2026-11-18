# Congress tie-strength pilot: spec

*Status: draft for review, 2026-11-18. Nothing built yet.*
*Decisions so far: Congress first, as the pilot for a general "shared-context" model; data back to the 103rd Congress (1993).*

## 1. Why

Paths today treat every link as roughly as strong as its relation category says (`link_scoring.py` gives one prior per category; path search minimises the sum of −log p, `pathfinder.py` ~L1540–1595). Two kinds of evidence about members of Congress are missing or flattened:

- **Shared service is real but undifferentiated.** We harvested "fellow representative / fellow senator" links but `build_scored_edges.py` drops them (`DROP_RELATIONS`, L93): every member linked to every other member formed cliques of 435 that flooded paths. Being in the same chamber says little; being on the same 25-member subcommittee for six years, in the same party, says a lot.
- **We have no committee, delegation, leadership or co-sponsorship data at all.** These are the best public signals of who works with whom on the Hill.

Goal: for any two people who served in Congress since 1993, estimate **how likely they are to know each other** from the contexts they shared, put that number on a link, and explain it in the path ("Both on House Financial Services 2019–23, both Democrats, co-sponsored 14 bills").

The pilot also proves the general model (section 9): contexts with size, time overlap, role and homophily, scored by a calibrated function, which then extends to boards, patents, classes and firms.

## 2. Scope

**In:** House and Senate members serving in the 103rd–119th Congresses (1993–2026), their terms, party, state and district; committee and subcommittee assignments with role (chair, ranking member, member); party leadership posts; state delegations; entering class; bill co-sponsorship; DW-NOMINATE ideology scores.

**Out (for now):** staff (we already have STAFF_AIDE links from other sources; untouched), roll-call agreement beyond NOMINATE, caucuses (Freedom Caucus, CBC, etc.; no clean historical source; candidate for phase 2), state legislatures, members who left before 1993 (they keep their existing links).

## 3. Data sources

| Need | Source | Coverage | Notes |
|---|---|---|---|
| Members, terms, party, state, district | `unitedstates/congress-legislators` `legislators-current.yaml` + `legislators-historical.yaml` | All Congresses | Each member carries **bioguide, FEC candidate IDs, Wikidata QID, ICPSR, OpenSecrets** IDs — this is how we match to our graph (section 5). Party per term, so party switchers are handled. |
| Committee and subcommittee names/codes | same repo, `committees-current.yaml`, `committees-historical.yaml` | All | Thomas/system codes join the membership sources. |
| Committee assignments, 1993–2017 | Charles Stewart III & Jonathan Woon, *Congressional Committee Assignments*, MIT data page; House file on Harvard Dataverse extends to the 117th (2021–22) | House 103rd–117th, Senate 103rd–115th | Includes rank on committee and chair/ranking flags. Verify file contents in phase 0. Subcommittee coverage to be checked. |
| Committee assignments, recent | `committee-membership-current.yaml` (current Congress); **its git history** gives a snapshot per Congress back to ~2013; GovInfo *Congressional Directory* (CDIR) XML as a fallback | Senate 116th–119th, House 118th–119th (gap fill) | Git history is the cheapest route; CDIR if snapshots are missing. |
| Co-sponsorship | GovInfo **BILLSTATUS** bulk XML | 108th–119th (2003–) | Sponsor + cosponsors with dates, per bill. |
| Co-sponsorship, 1993–2002 | congress.gov API v3 (free key) bill + cosponsors endpoints | 103rd–107th | ~50K bills; within the API's rate limit in a few days of polling. |
| Ideology | Voteview member file (DW-NOMINATE) | All | Joins on ICPSR. Used as a homophily feature, not a link. |
| Leadership posts | congress-legislators `terms[].leadership_roles` + Wikidata P39 | Mostly complete | Speaker, majority/minority leaders, whips, conference chairs. |

All free and redistributable (public domain or CC0/academic citation). Stewart & Woon require citation in the methods page.

## 4. Data model (pipeline Postgres, new tables)

```
cg_member(bioguide PK, name, node_label, qid, icpsr, fec_ids[], match_method, match_conf)
cg_term(bioguide, congress, chamber, state, district, party, start, end)
cg_context(context_id PK, kind, congress, chamber, name, parent_id, size)
    kind ∈ committee | subcommittee | delegation | class | party_conference | leadership | chamber
cg_membership(context_id, bioguide, role, start, end)          -- role: chair|ranking|member|...
cg_cosponsor_pair(a, b, congress, n_bills, n_bills_sponsor_led) -- a < b; aggregated
cg_nominate(bioguide, congress, dim1, dim2)
cg_tie(a, b, p, features JSONB, explanation, model_version)    -- output, one row per pair kept
```

Contexts are per Congress (a committee in the 110th and the same committee in the 111th are separate rows with the same name), so time overlap falls out of counting shared context-Congresses. Size is computed, not entered.

## 5. Matching members to graph nodes

In order, stopping at the first hit:
1. **Wikidata QID** → our verified-figure QID mapping (`qid_resolver.py` output).
2. **FEC candidate ID** → the candidate's campaign committee node → the person linked to it as candidate.
3. **Name + chamber/state** against nodes that already have a PUBLIC_OFFICE or FELLOW_OFFICEHOLDER link.
4. Otherwise create a new person node `Name (U.S. Representative, ST)`, with namesake-safe labelling per the existing split rules.

Every match records its method; a report lists method-3 and method-4 cases for spot checks. Target: ≥ 98% of 1993+ members matched by method 1 or 2.

## 6. Tie-strength model

For a pair (a, b), build features from everything they shared:

| Feature | Definition |
|---|---|
| `ctx_k` for each shared context | 1 / log2(size + 1) summed over shared context-Congresses, separately per kind (subcommittee, committee, delegation, class, leadership, party conference, chamber) |
| `years_overlap` | Congresses both served in the same chamber |
| `same_party` | Share of overlapping Congresses with the same party |
| `nominate_dist` | Mean distance in DW-NOMINATE dim 1 over overlapping Congresses |
| `role_pair` | Chair + ranking member of the same (sub)committee; both in leadership |
| `cosponsor` | log(1 + bills co-sponsored together); separately, bills where one sponsored and the other co-sponsored |
| `same_state` | Ever in the same state delegation (also covers House–Senate pairs) |
| `recency` | Congresses since their last shared context (ties decay; stale ties still count) |

Model: logistic regression **p = σ(w·x + b)**, one per chamber-pair (House–House, Senate–Senate, House–Senate), fitted as in section 7. Interpretable and cheap; coefficients reported in the build log. A pair's final link probability is the model's p, capped at the FELLOW_OFFICEHOLDER prior's ceiling (0.75) unless co-sponsorship or a LittleSis direct tie pushes it higher.

**What becomes a link:**
- **Pairs** with p ≥ 0.30 (threshold tuned in section 7) become `CONGRESS_COLLEAGUE` links with their p and an explanation string. Expected order of magnitude: a few hundred thousand pairs across 1993–2026, nowhere near the 435² cliques we dropped.
- **Large contexts** (chamber, party conference): not paired. One hub node per Congress and chamber (e.g. `116th Congress — House`), each member linked with a low probability scaled by 1/log(size). A path can cross it but pays for it, so it is a last resort, not a shortcut.
- Pairs below threshold produce no link.

Links carry a new category `CONGRESS_COLLEAGUE` (added to `relation_categories.py` and `link_scoring.py`), so they can be weighted, filtered and explained separately from FELLOW_OFFICEHOLDER (judges, governors), which is unchanged.

## 7. Calibration (the weights are fitted, not hand-tuned)

**Positive labels:** pairs we have independent evidence know each other:
- LittleSis social/professional/family links between two members;
- GDELT_HAIKU links (Haiku-confirmed relationships from news);
- pairs who co-sponsored ≥ 5 bills **held out** from the features (fit with `cosponsor` excluded, then compare; avoids training on the answer);
- Wikidata "significant person"/spouse/relative statements between members.

**Negatives:** random overlapping pairs from the same Congress and chamber, sampled 20:1, minus any positives.

Fit on 1993–2014, test on 2015–2026. Report AUC, the precision of pairs above threshold, and coefficient signs (same party should be positive; larger contexts should be weaker). If a coefficient comes out with the wrong sign, drop the feature rather than force it.

## 8. Path engine and UI

- No engine change: new links slot into the existing −log p costs. Hub nodes need one addition: **exclude Congress hub nodes from the first and last hop of a path** unless the user's own endpoint is a member (stops "A is in Congress, B is in Congress, done").
- Path explanation: the link's `explanation` string is shown like evidence today. Example: *"House Financial Services 2019–23 (both Democrats); Subcommittee on Capital Markets 2021–22; co-sponsored 14 bills"*.
- The ⓘ identity card for a member gains "Served: House 1993–1995 (WA-1), Senate 2001–" from `cg_term`.

## 9. Evaluation and acceptance

1. **Calibration** (section 7): test AUC ≥ 0.80; above-threshold precision against positives ≥ 0.5 (most kept pairs have independent evidence of a real tie).
2. **Known pairs:** a hand list of ~30 well-documented working relationships (e.g. committee chair and ranking-member pairs, same-state senators, co-authors of major bills) and ~30 known non-relationships. Each known pair should appear as a link or a two-hop path; each non-pair should not get a direct link.
3. **Path diff:** run 200 member-to-member and member-to-donor path queries before and after. Report the share that changed and judge 30 changed paths by eye with you. Accept if the changes are better in at least 2 of 3.
4. **No regressions:** path latency (`check_latency.py`) within 10% of today; graph size growth reported; the existing path test suite passes.

## 10. Phases

| Phase | Work | Output |
|---|---|---|
| 0. Sources | Download and inspect every source; confirm Stewart/Woon coverage and the git-history snapshots; fill the 116th–118th gap | Coverage table per Congress × chamber × data type |
| 1. Load | Harvesters → `cg_*` tables on the optiplex; member matching (section 5) with the match report | Tables loaded; ≥ 98% matched by ID |
| 2. Model | Feature build, label set, fit, evaluation (section 7, 9.1) | Model report: coefficients, AUC, threshold |
| 3. Graph | `CONGRESS_COLLEAGUE` links + hub nodes into `build_scored_edges.py`; category added | One nightly rebuild with the new links, on a branch |
| 4. Verify | Sections 9.2–9.4 with you; then deploy | Prod-verified paths |

Phases 0–2 touch only the pipeline database and change nothing users see. Phase 3 is the first change to the graph and goes out only after your sign-off on the evaluation.

## 11. Generalising after the pilot

The same context model, with the same features where they apply:

| Context | Size | Overlap | Role | Homophily |
|---|---|---|---|---|
| Corporate / nonprofit board | seats | years served together | chair, committee chairs | — |
| Patent | inventor count | filing year | first inventor | same assignee |
| School class | class size (estimated) | class year | — | same program |
| Law-firm / employer | headcount band | years | partner | same office |
| FEC | — | — | — | shared donors/causes (homophily only, never a link on its own) |

Each needs its own label set; the calibration code is shared. This is also where the person/organization typing work fits (contexts are organizations; members are people).

## 12. Risks

- **Committee gaps** (Senate 116th–118th, subcommittees pre-2003) weaken features for some Congresses. Mitigation: phase 0 coverage table; the model gets a "missing data" indicator, not a zero.
- **Label leakage:** co-sponsorship is both evidence and feature. Handled by the held-out fit in section 7.
- **Over-linking politicians** makes paths gravitate to Congress. Mitigation: the threshold, the hub exclusion rule, and the path-diff check in section 9.3.
- **Matching errors** on common names (e.g. two "Mike Rogers"). Mitigation: ID-first matching; method-3 matches require state + chamber agreement.

## 12a. Results of the first build (2026-11-18)

Built: `congress_ties.py` (load, match, fit, export to staging, publish), `congress_caucuses.py` (Wikipedia history), `congress_eval.py` (section 9 checks); build, scoring and `/api/evidence` changes; tests in `webapp/tests/test_congress_ties.py`.

- **Data:** 1,802 members since 1993; full committees 1993–2026 with no gap (Stewart & Woon to the 115th, then 211 git snapshots of the membership file); subcommittees from 2013; 16 caucuses from about 2005 (Tea Party Caucus kept to 2011–2015); 167K bills (Fowler/Cornell data before 2003, GovInfo after).
- **Matching:** 1,762 members (97.8%) matched to graph nodes with office evidence; 19 by name only; 21 get new nodes.
- **Model:** test AUC 0.896. Features kept (all with the expected sign): co-sponsorship (two kinds), same party, committee work (committees + subcommittees), delegation, cross-party work (co-sponsorship and chair/ranking across party lines), leadership, caucus, ideology distance. Dropped for wrong signs or sampling artefacts: years together per chamber, entering class, the "data available" indicators. Negatives are sampled uniformly over pairs who served together (fixing two sampling biases found during fitting).
- **Rule on top of the model:** a committee's chair and ranking member always get at least a strong tie (the labels rarely record these pairings; Dodd–Shelby, Waxman–Barton scored low without it).
- **Output:** 22,713 pairs (7,092 strong at p ≥ 0.5, 15,621 weak at 0.2–0.5; p capped at 0.75). Precision against independent evidence: 0.87 at 0.5, 0.79 at 0.2.
- **Evaluation:** 29 of 30 known working relationships get a direct link (the miss, Ryan–Murray, served in different chambers). Best paths changed for 13 of 100 member-to-member and 18 of 100 member-to-outsider queries, mostly replacing hops through "US House of Representatives", PAC donations or school classes; average path probability up 9–11%.
- **Status:** staged in `cg_tie_candidate`; `python congress_ties.py publish` makes it live for the next nightly rebuild.

## 13. Decisions (2026-11-18)

1. **Keep both strong and weak ties.** Pairs above the strong threshold are ordinary links; pairs between the weak and strong thresholds are also links, labelled "weak tie" in path explanations.
2. **No Congress hub nodes in the graph.** Chamber and party-conference membership are used only as model features and in explanations ("both served in the 116th Congress"). Section 6's hub nodes and section 8's hub exclusion rule are dropped.
3. **Caucuses: scrape them.** Caucus membership (Freedom Caucus, Problem Solvers, CBC, Blue Dogs, Progressive Caucus, New Democrats, etc.) is added as a context kind `caucus` once a source is built.
4. No extra hand-checked relationships; section 9.2 uses public, well-documented pairs.

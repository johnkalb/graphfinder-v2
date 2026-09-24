# AmLaw / Big-Law firm target backlog

Source: [Wikipedia "List of largest law firms by revenue"](https://en.wikipedia.org/wiki/List_of_largest_law_firms_by_revenue),
2025 revenue figures (close enough in spirit to the American Lawyer's Am Law 100 methodology,
and freely accessible unlike Law.com Compass which is paywalled for the official ranking).
Original scope (see `.hermes.old/agents/amlaw-roster/scripts/README.md`): full AmLaw 100/200,
not just a pilot batch. Per-firm work is real reconnaissance (JSON API vs Playwright, robots.txt,
existing-DB-name check) added to `FIRMS` in `amlaw_roster.py` -- never a guessed/fabricated entry.

Status column: `working` (harvesting live), `blocked_bot_mitigation`, `blocked_robots_txt` (site's
own robots.txt disallows the API path -- not attempted, matching the same respect-it stance as
bot-mitigation), `needs_recon` (site inspected, no clean path found yet, deferred), or blank (not
yet attempted).

| rank | firm | status |
|---|---|---|
| 1 | Kirkland & Ellis | working |
| 2 | Latham & Watkins | working |
| 3 | DLA Piper | blocked_bot_mitigation |
| 4 | A&O Shearman | needs_recon (confirmed target DB name: "A&O Shearman", 1 existing LITTLESIS edge. Site uses third-party "Sitecore Discover" -- same platform as DLA Piper -- POST https://discover-euc1.sitecorecloud.io/discover/v2/217407760, public search-only key found in their JS bundle: `01-5853e162-d37961d72cf9ebb36fcf6079715d64b20fb3ba3c`. The exact widget config (rfk_id + source ID -- confirmed DLA Piper's own values don't transfer, server validates them per-domain) needs a real request body captured live, which requires a genuine Playwright session with request listeners per this repo's own established fallback -- chat-based browser control can't install an interceptor before the page's own initial auto-fetch fires) |
| 5 | Skadden, Arps, Slate, Meagher & Flom | working |
| 6 | Gibson Dunn | needs_recon (WordPress "WP Grid Builder" plugin, AJAX POST /?wpgb-ajax=render returns opaque HTML fragments not clean JSON, letter-facet click behavior unclear -- needs a real Playwright session with request-body capture, not just the browser extension's network tracker) |
| 7 | Sidley Austin | blocked_robots_txt (robots.txt disallows `*/api/*` for all crawlers not individually named -- their attorney search is API-driven like every other firm checked, so this is a hard stop, not a workaround target) |
| 8 | Ropes & Gray | working |
| 9 | Baker McKenzie | blocked_bot_mitigation (real first-party Sitecore API found -- POST /en/api/sitecore/people/search, triggered live in a real Chrome session -- but a Playwright/headless probe of the same letter-filtered URL was served a Cloudflare "Just a moment..." managed challenge (403), confirming a scripted harvester would be blocked even though an interactive real-browser session isn't. Not attempting to bypass, matching the DLA Piper precedent) |
| 10 | White & Case | blocked_bot_mitigation (robots.txt itself returns 403 regardless of User-Agent -- confirmed via curl with both a research UA and a real Chrome UA string. A site that blocks even the compliance-check document is a deliberate anti-automation posture; not attempted further) |
| 11 | Morgan, Lewis & Bockius | blocked_robots_txt (robots.txt: `User-agent: *` / `Disallow:/api/*` -- blanket block on all API paths for every crawler, same policy stop as Sidley Austin) |
| 12 | Clifford Chance | needs_recon (robots.txt clear but `Crawl-delay: 10` -- must respect. Server-rendered AEM pages, not a JSON API: name+bio-URL sit cleanly in `<h1><a href="..." title="Full Name">` markup, easy to regex-extract. 4 tabs -- Partners (723, default/no view param), Senior Lawyers (437, `counselsview=true`), Associates (2233, `lawyersview=true`), Business Management (101, `othersview=true`) -- each paginated via `?browseazlink=<A-Z>&<view>=true&page=<N>`, confirmed working for counselsview/lawyersview/othersview via their own href links in the page. But the default (Partners, no view param) tab's own page>1 URL is NOT in the page's link list, and a guessed `?browseazlink=B&page=2` returned a DIFFERENT tab's results, not Partners page 2 -- the real mechanism for that tab is unconfirmed. Also, ~3500 people at 10/page with a mandatory 10s crawl-delay is a ~1hr+ harvest once solved.) |
| 13 | Freshfields | working |
| 14 | Linklaters | needs_recon (robots.txt wide open. Next.js + Sitecore XM Cloud site, ~2800 lawyers across 117 pages of ~24/page. The Next.js `_next/data/<buildId>/find-a-lawyer.json?page=N` endpoint exists and returns real JSON, but only page config/dictionary strings -- the actual paginated expert list is fetched by a separate client-side runtime call (likely Sitecore Experience Edge GraphQL, unconfirmed) that two different Playwright capture attempts failed to catch (cookie-banner overlay and pagination-link click-target issues). A large embedded page script (461KB) holds only a small curated "Expert Priority" leadership subset, not the full roster.) |
| 15 | Hogan Lovells | needs_recon (real merger 2026-07-01 with Cadwalader -> now "Hogan Lovells Cadwalader" / hlc.com, 3,200+ lawyers -- confirmed legitimate via web search, not a suspicious redirect. robots.txt blanket-disallows `/sitecore/` but their API lives under `/api/sitecore/...` which is clear. Found `GET /api/sitecore/people/GetAutoSuggestSearchResults?searchTerm=<term>` -- clean JSON (Title/ItemLink), no auth -- but it's a true autosuggest widget: hard-capped at exactly 10 results regardless of take/limit/pageSize/top overrides, and single-character searchTerm values error out (redirect to /Errorpages/error500.html). Not viable for bulk enumeration as-is. The real "Our people" grid page clearly paginates through the full roster in the browser UI, but its underlying endpoint wasn't found in the two JS files that reference people-search.) |
| 16 | Simpson Thacher & Bartlett | needs_recon (robots.txt disallows both `/search` and `/dataservices` for all crawlers -- both plausible names for the real backend, though neither confirmed as the actual mechanism. The /our-team page's initial HTML has no obvious pagination link or JSON API call; images.tmb-thmb thumbnails suggest a Sitefinity/Kentico-style CMS. Not confirmed either way within a quick pass -- worth a closer look with the full recon protocol rather than the fast pass used here.) |
| 17 | Jones Day | needs_recon (re-diagnosed 2026-09-17 -- the cookie-overlay theory was wrong; the shared adapter's OneTrust dismissal landed and didn't fix it. Issue #1, solved: bare `.CoveoResult` also matches a hidden `CoveoRecommendation` widget; scoping to `.CoveoSearchInterface:not(.CoveoRecommendation) .CoveoResult` + waiting for `state="attached"` instead of visible correctly returns real names/URLs. Issue #2, unsolved: the pager and even the correctly-scoped results report display:block/visible computed style but a literal 0x0 bounding box; scrolling/interacting detaches the DOM subtree (page rebuilds it -- one attempt logged a surprise navigation to `#sort=relevancy`). Needs a real non-headless session with DOM-mutation observation, not resolvable via short scripted probes.) |
| 18 | Dentons | blocked_bot_mitigation (WAF-level User-Agent filtering: a research UA gets 403 even on robots.txt itself, a real Chrome UA string gets 200. Getting past this would require sending a browser-spoofed User-Agent, which crosses the no-spoofing line -- not attempted. robots.txt content (fetched with a browser UA only for verification) also disallows `/sitecore/` blanket, so even past the WAF the actual API path would need to avoid that prefix.) |
| 19 | Goodwin Procter | blocked_robots_txt (robots.txt: `User-agent: *` blanket-disallows both `/api/` and `*search*` -- same policy stop as Sidley Austin/Morgan Lewis) |
| 20 | Paul, Weiss, Rifkind, Wharton & Garrison | needs_recon (robots.txt clear. Client-rendered app -- URL query params like `?lastname=A&pageSize=12` are pure client-routing state, not real server params (confirmed: identical 41KB shell HTML regardless of query string or User-Agent). Real data clearly loads somewhere (24 correct profile links render in the DOM after selecting a letter) but the actual fetch call never appeared in network tracking despite several attempts -- possibly a timing issue or a request type the tracker doesn't surface. Needs a proper Playwright request-listener session.) |
| 21 | Greenberg Traurig | working (dry-run: 2,835 unique, 0 dupes) |
| 22 | Davis Polk & Wardwell | working (dry-run: 1,279 unique, matches nbHits exactly, after fixing Algolia's 1000-result pagination cap via office x job_title facet splitting) |
| 23 | Quinn Emanuel Urquhart & Sullivan | needs_recon (robots.txt: none exists, no restrictions. Found the real endpoint via a targeted Playwright capture: POST /Umbraco/surface/AttorneysListSurface/GetAttorneyListByFilter?byCountry=1757&byChar=all&...&currentPage=<N>, returns raw HTML fragments with the same `<h3><a href title>` markup as several other firms -- clean to regex-extract. But replaying it standalone (plain requests, even with a prior GET to establish ASP.NET_SessionId/__RequestVerificationToken cookies, X-Requested-With, Referer) still gets a generic IIS 500 -- something about the real browser's request (headers, antiforgery token value, or timing) isn't reproduced yet. 24 pages of ~50 confirmed via the live UI.) |
| 24 | Norton Rose Fulbright | blocked_robots_txt (robots.txt allow-lists only specific named crawlers (Googlebot, Bingbot, DuckDuckBot, Slurp, YandexBot, SemrushBot, social-media bots) with varying restrictions -- everything else falls under a fallback `Disallow: /` blanket block. Our harvester's honest UA isn't any of those named bots, so it's fully blocked by policy.) |
| 25 | King & Spalding | working (dry-run: 1,484 unique, 0 dupes, exact match) |
| 26 | CMS | blocked_bot_mitigation (Cloudflare challenge-platform triggered under a headless Playwright probe of /en/int/people -- same signature as Baker McKenzie -- confirming a scripted harvester would be blocked even though an interactive session loads fine) |
| 27 | Paul Hastings | needs_recon (robots.txt wide open. Gatsby.js + Contentful-backed site, 1,374 professionals shown in the UI. Checked all 5 Gatsby static-query page-data JSON files referenced by the professionals page -- none contain the actual roster (largest is 50KB, way too small for 1,374 full records), so the real listing is fetched by a genuine runtime call (likely Contentful's Content Delivery API directly, or a custom backend) that a 6-second Playwright network capture window didn't catch. Needs more targeted recon.) |
| 28 | McDermott Will & Schulte | needs_recon (real rebrand: mwe.com now redirects to mcdermottlaw.com, "McDermott Will & Schulte" -- confirmed legitimate, not a hijack. robots.txt clear of /api/ or people-search blocks. Site actively fingerprints automation (sends `webdriverDetected: true` to New Relic under Playwright -- a passive detection signal, not an active block, since the page still loaded fine) which is a mild caution flag. No XHR/fetch data call for the roster appeared in a 5s capture window, and raw HTML has no server-rendered profile links either -- needs more targeted recon.) |
| 29 | Cooley | working (dry-run: 120/120 unique across multiple pages, 0 dupes -- confirms the shared PlaywrightAdapter cookie-dismissal fix works) |
| 30 | Sullivan & Cromwell | blocked_bot_mitigation (robots.txt empty/no restrictions, but /lawyers/ itself returns 403 "Forbidden: Access is denied" consistently -- confirmed via curl with both a research UA and a real browser UA string, AND via a fresh headless Playwright session (a genuine Chromium engine, not a spoofed UA). Real anti-automation measure, not attempted further.) |
| 31 | Holland & Knight | blocked_bot_mitigation (robots.txt references Incapsula resource paths, and a headless Playwright probe of /en/professionals triggers a Cloudflare challenge-platform request -- confirmed anti-automation posture) |
| 32 | Weil, Gotshal & Manges | blocked_bot_mitigation (Cloudflare challenge-platform triggered under headless Playwright probe of /people) |
| 33 | Mayer Brown | blocked_bot_mitigation (Cloudflare challenge-platform triggered under headless Playwright probe of /en/people -- same Cloudflare ray/fingerprint pattern seen on Holland & Knight and Weil, suggesting a shared hosting/CDN vendor across several firms rather than each site configuring this independently) |
| 34 | Milbank | blocked_robots_txt (robots.txt directly disallows `/en/professionals/index.html?q=` for all crawlers -- the exact query mechanism needed to search/filter the professionals directory) |
| 35 | Willkie Farr & Gallagher | needs_recon (robots.txt wide open, no restrictions at all. No API/XHR call found in a Playwright capture of /professionals, and no server-rendered profile links in the raw HTML either -- needs deeper interactive recon (click-triggered capture) not done yet.) |
| 36 | Covington & Burling | blocked_robots_txt (robots.txt disallows `/coveo/rest/` for all crawlers -- several sibling firms in this backlog (Latham, Jones Day, Cooley) use exactly this Coveo-for-Sitecore REST API, so this is very likely a direct policy block on their real search mechanism) |
| 37 | Herbert Smith Freehills Kramer | blocked_bot_mitigation (real rebrand: herbertsmithfreehills.com now redirects to hsfkramer.com, confirmed legitimate merger naming. robots.txt clear (Crawl-delay: 5) but Cloudflare challenge-platform (precursor_interstitial) triggered under headless Playwright probe of /find-a-lawyer) |
| 38 | Cleary Gottlieb Steen & Hamilton | working (dry-run: 1,204 unique, 0 dupes, exact match) |
| 39 | Eversheds Sutherland | blocked_bot_mitigation (WAF-level User-Agent filtering, same pattern as Dentons: research UA gets 403 even on robots.txt itself, real Chrome UA string gets 200. Not attempting a browser-spoofed UA.) |
| 40 | Debevoise & Plimpton | blocked_bot_mitigation (Cloudflare challenge-platform triggered under headless Playwright probe of /lawyers -- same "3453988267" fingerprint seen on Holland & Knight/Weil/Mayer Brown, reinforcing the shared-vendor theory) |
| 41 | Wilmer Cutler Pickering Hale and Dorr | working (dry-run: 1,095 unique, 0 dupes, exact match) |
| 42 | Orrick, Herrington & Sutcliffe | blocked_robots_txt (robots.txt disallows `/people`/`/People` (with query-string variants) directly -- only individual bio pages beneath /people/ are Allow-excepted, meaning the actual directory/search listing itself is a deliberate policy block, not just a search-widget path) |
| 43 | Dechert | blocked_robots_txt (robots.txt disallows `*?*search=` and `*?*sort=` query params for all crawlers -- an AEM site (jcr:content references), where a people directory almost always uses one of these two params for filtering/sorting) |
| 44 | Reed Smith | working (third-party Algolia search, public search-only key embedded in the site's own front-end JS -- same category as Davis Polk's/DLA Piper's public keys, not a credential. POST jitn45twfd-dsn.algolia.net/1/indexes/*/queries, index 'main:people'. Confirmed working standalone via plain requests with no cookies. hitsPerPage=100 -> nbHits=1550/nbPages=10, clean pagination. robots.txt only blocks faceted-search query params on reedsmith.com itself, doesn't cover the Algolia host. See `recon/reed-smith.md`.) |
| 45 | Akin Gump Strauss Hauer & Feld | blocked_robots_txt (robots.txt explicitly names and blocks `User-agent: ClaudeBot` / `Disallow: /` and `User-agent: GPTBot` / `Disallow: /`, plus a blanket `Disallow: /*?*` -- any URL with a query string -- for the default group. This harvester's own UA string isn't literally named, but the intent is explicit and unambiguous; not attempted further, consistent with treating robots.txt as a hard policy stop even where a technical workaround might exist.) |
| 46 | Wilson Sonsini Goodrich & Rosati | working (first-party JSON site-search API, plain requests, no auth. GET /_site/search?l=\<A-Z\>&f=\<offset\>&space=1&v=attorney. Fixed page size of 15 hits per response regardless of offset -- confirmed via letters A (total=41) and B (total=89) at f=0/15/30/45, each 15 non-overlapping hits -- loop f+=15 until f>=per-letter total. robots.txt only blocks two index pages + a json path, doesn't cover /_site/search. See `recon/wilson-sonsini.md`.) |
| 47 | Morrison & Foerster | blocked_robots_txt (robots.txt has a blanket `Disallow: /api` for the default user-agent group -- the attorney directory is API-driven like every other firm checked, so this is a hard policy stop, same pattern as Sidley Austin and Morgan Lewis & Bockius.) |
| 48 | Yingke | needs_recon (world's largest law firm by headcount, ~15,000+ lawyers/~111 offices, but structurally a loose network of independently-branded local China offices rather than one unified attorney database -- likely a poor fit for this per-firm scraper pattern even if reachable. Real domain, confirmed via web search after several guessed domains failed, is www.yingkelawyer.com -- but it is genuinely unreachable (curl exit 000) from both this Windows box and the Optiplex execution host over plain HTTPS, consistent with geo-fencing/Great-Firewall-related blocking of a China-hosted domain from US IPs rather than a bot-mitigation signal. Deprioritized -- not worth further recon time unless connectivity changes.) |
| 49 | Proskauer Rose | needs_recon (robots.txt clear -- only blocks one PDF-export path and /admin/*, and individual bio pages live at a clean /professionals/\<slug\> path per sitemap.xml -- but the /professionals directory page itself is a search-gated widget with an empty default state ("0 results / Please start your search to reveal your results" until a filter is actually clicked). Two attempts to click the 'A' alphabetical filter and capture the resulting network call both failed on a Playwright selector-matching issue, not a site-side block -- no JSON API or DOM change captured yet. Needs a proper interactive Playwright session, not a short scripted probe.) |
| 50 | Squire Patton Boggs | working (server-rendered ASP.NET page, clean semantic markup -- .c-person-card / .c-person-card__content h3 for name, single wrapping \<a href="/our-people/\<slug\>/"\>. An AJAX 'Load more' button POSTs PageNumber + an ASP.NET anti-forgery token tied to the session cookie, appending 24 more cards per click (confirmed 24->48->72->96->120->144 across 6 clicks, no plateau). Used a real Playwright session rather than plain requests specifically to avoid re-deriving the anti-forgery token by hand. robots.txt `Disallow:` is empty -- completely unrestricted. See `recon/squire-patton-boggs.md`.) |
| 51 | Alston & Bird | blocked_bot_mitigation (robots.txt itself returns a Cloudflare "Attention Required!" interstitial challenge page rather than the actual file -- confirmed via a full-body fetch, not just the HTTP status code, since Cloudflare's challenge pages commonly return HTTP 200 with challenge HTML rather than a 403. A site that gates even the compliance-check document behind bot mitigation is a deliberate anti-automation posture, matching the White & Case precedent already in this backlog -- not attempted further.) |
| 52 | K&L Gates | needs_recon (robots.txt has no Disallow rules at all -- completely unrestricted. Clean semantic markup found: .s-bio-card / .s-bio-card__heading (name+bio href, e.g. /lawyers/Brodie-Erwin) / .s-bio-card__office / .s-bio-card__phone, 50 cards present on initial page load. Numbered pagination buttons ('1' through '8') are present, but clicking button '2' -- after dismissing the OneTrust cookie-consent overlay that otherwise intercepts the click, same issue as jones-day/cooley -- produced no change in the rendered card list and no new xhr/fetch request. No JSON API observed at all. Needs direct visual/DOM inspection of what those numbered buttons actually control before this can be built out.) |
| 53 | Ashurst | blocked_robots_txt (real merger: ashurst.com now 308-redirects to ashurstperkinscoie.com, same pattern as the Hogan Lovells/Cadwalader and Herbert Smith Freehills/Kramer mergers already in this backlog. The new domain's robots.txt disallows /api/*, /sitecore, /Sitecore, /sitecore/api/ssc/* -- the same first-party Sitecore API family used by working firms Kirkland & Ellis and Greenberg Traurig, so this is a real, deliberate block of the exact endpoint family this harvester would otherwise use -- plus */people/?q=* and */search/*. Hard policy stop, not attempted further.) |
| 54 | Foley & Lardner | |
| 55 | Winston & Strawn | |
| 56 | Perkins Coie | |
| 57 | King & Wood Mallesons | |
| 58 | Sheppard, Mullin, Richter & Hampton | |
| 59 | Cravath, Swaine & Moore | |
| 60 | Wachtell, Lipton, Rosen & Katz | |
| 61 | Arnold & Porter | |
| 62 | Troutman Pepper Locke | |
| 63 | Fried, Frank, Harris, Shriver & Jacobson | |
| 64 | McGuireWoods | |
| 65 | Kim & Chang | |
| 66 | Clyde & Co | |
| 67 | O'Melveny & Myers | |
| 68 | Faegre Drinker | |
| 69 | BakerHostetler | |
| 70 | Vinson & Elkins | |
| 71 | Polsinelli | |
| 72 | Nelson Mullins Riley & Scarborough | |
| 73 | Seyfarth Shaw | |
| 74 | Fragomen, Del Rey, Bernsen & Loewy | |
| 75 | Hunton Andrews Kurth | |
| 76 | Pinsent Masons | |
| 77 | Slaughter and May | |
| 78 | Katten Muchin Rosenman | |
| 79 | Bryan Cave Leighton Paisner | |
| 80 | Venable | |
| 81 | Baker Botts | |
| 82 | Pillsbury Winthrop Shaw Pittman | |
| 83 | Fenwick & West | |
| 84 | Simmons & Simmons | |
| 85 | Fox Rothschild | |
| 86 | Gordon Rees Scully Mansukhani | |
| 87 | Lewis Brisbois Bisgaard & Smith | |
| 88 | Gowling WLG | |
| 89 | Barnes & Thornburg | |
| 90 | Blank Rome | |
| 91 | Bird & Bird | |
| 92 | Littler Mendelson | |
| 93 | Ogletree, Deakins, Nash, Smoak & Stewart | |
| 94 | Blake, Cassels & Graydon | |
| 95 | Cozen O'Connor | |
| 96 | Husch Blackwell | |
| 97 | Addleshaw Goddard | |
| 98 | Taft Stettinius & Hollister | |
| 99 | Mintz, Levin, Cohn, Ferris, Glovsky, and Popeo | |
| 100 | Jackson Lewis | |

Note: several are UK/international firms (Clifford Chance, Linklaters, Freshfields, Slaughter and
May, etc.) with no US name-collision guarantee -- still worth the DB name-collision check per the
README's mandatory step, since the graph already has plenty of non-US entities.

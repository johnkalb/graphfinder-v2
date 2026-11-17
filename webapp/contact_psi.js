/* Private contact matching, shared by the main page and the Team workspace
   page (served at /contact-psi.js). Moved out of pathfinder.py's page
   template 2026-11-17. */

/* The contact file is parsed locally.  The only values sent below are
   blinded Ristretto points; plaintext names never enter a fetch body.
   Tier derivation (normalizeExact/phoneticKey/lshBandTokens) is a
   byte-for-byte port of webapp/contact_psi/keys.py -- verified against
   the real Python module and the pinned test vectors before landing here
   (see specifications/contact-check-psi-test-vectors.json). */

const CONTACT_PSI_SUFFIXES = new Set(['jr', 'sr', 'ii', 'iii', 'iv', 'v']);
const CONTACT_PSI_SOUNDEX = {};
for (const c of 'bfpv') CONTACT_PSI_SOUNDEX[c] = '1';
for (const c of 'cgjkqsxz') CONTACT_PSI_SOUNDEX[c] = '2';
for (const c of 'dt') CONTACT_PSI_SOUNDEX[c] = '3';
CONTACT_PSI_SOUNDEX['l'] = '4';
for (const c of 'mn') CONTACT_PSI_SOUNDEX[c] = '5';
CONTACT_PSI_SOUNDEX['r'] = '6';
for (const c of 'aeiouy') CONTACT_PSI_SOUNDEX[c] = '0';

// --- Compact contact-matching manifest (format 2, contact_psi/manifest.py) ---
// The exact-name tier is downloaded WHOLE (and cached in the browser), so
// the download says nothing about which contacts are being checked. The
// sound-alike tier comes in 256 small shards; we ask for the ones we need
// plus random others, in random order.
async function contactPsiFetchCached(url, onChunk) {
  const abs = new URL(url, location.href).href;
  let cache = null;
  try { cache = await caches.open('contact-psi-v2'); } catch (e) {}
  if (cache) {
    try { const hit = await cache.match(abs); if (hit) return new Uint8Array(await hit.arrayBuffer()); } catch (e) {}
  }
  const res = await fetch(abs);
  if (!res.ok) throw new Error('Could not download the private matching file (HTTP ' + res.status + ') -- try again.');
  const reader = res.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length;
    if (onChunk) onChunk(value.length);
  }
  const buf = new Uint8Array(got);
  let o = 0;
  for (const c of chunks) { buf.set(c, o); o += c.length; }
  if (cache) {
    try {
      const base = abs.split('?')[0];
      for (const k of await cache.keys()) if (k.url.split('?')[0] === base && k.url !== abs) await cache.delete(k);
      await cache.put(abs, new Response(buf, { headers: { 'Content-Type': 'application/octet-stream' } }));
    } catch (e) {}
  }
  return buf;
}

function contactPsiParseCompact(buf, prefixBytes) {
  if (buf[0] !== 0x50 || buf[1] !== 0x53 || buf[2] !== 0x49 || buf[3] !== 0x58) throw new Error('The matching file is damaged -- try again.');
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const n = dv.getUint32(9, true);
  const p0 = 13, o0 = p0 + n * prefixBytes;
  return { buf, dv, n, p0, o0, b0: o0 + (n + 1) * 4, pb: prefixBytes };
}

function contactPsiCompactFind(f, key) {
  const cmp = i => { const base = f.p0 + i * f.pb; for (let j = 0; j < f.pb; j++) { const d = f.buf[base + j] - key[j]; if (d) return d; } return 0; };
  let lo = 0, hi = f.n;
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (cmp(mid) < 0) lo = mid + 1; else hi = mid; }
  const out = [];
  for (let i = lo; i < f.n && cmp(i) === 0; i++) {
    const a = f.dv.getUint32(f.o0 + i * 4, true), b = f.dv.getUint32(f.o0 + (i + 1) * 4, true);
    out.push(f.buf.subarray(f.b0 + a, f.b0 + b));
  }
  return out;
}

async function contactPsiCompactLookup(queries, meta, onStatus, sha256, textBytes, concatBytes) {
  const pb = meta.exact_prefix_bytes || 6, tagBits = (meta.tag_bytes || 12) * 8;
  const decryptAll = async (q, cts) => {
    if (!cts.length) return;
    const k = await crypto.subtle.importKey('raw', q.aesKeyBytes, 'AES-GCM', false, ['decrypt']);
    const iv = (await sha256(concatBytes(textBytes('contact-psi-nonce-v1'), q.aesKeyBytes))).slice(0, 12);
    for (const ct of cts) {
      try {
        const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv, tagLength: tagBits }, k, ct);
        const found = new TextDecoder().decode(pt).split('\n').filter(Boolean);
        (q.payloads || (q.payloads = [])).push({ matches: found.map(n => ({ id: n, name: n })) });
      } catch (e) { /* another key with the same prefix -- expected */ }
    }
  };
  // 1. exact names: every part, always
  const totalMB = Math.max(1, Math.round((meta.exact_bytes || 0) / 1e6));
  let got = 0;
  const show = () => {
    onStatus(`<span class="psi-spinner"></span> Downloading the private matching file `
      + `(${Math.round(got / 1e6)} of ~${totalMB} MB; your browser keeps it for next time)…`);
  };
  const parts = [];
  for (const url of meta.exact_urls) parts.push(contactPsiParseCompact(await contactPsiFetchCached(url, n => { got += n; show(); }), pb));
  onStatus('<span class="psi-spinner"></span> Matching…');
  for (const q of queries) {
    if (q.tier !== 'exact') continue;
    await decryptAll(q, contactPsiCompactFind(parts[Math.floor(q.aesKeyBytes[0] * parts.length / 256)], q.aesKeyBytes));
  }
  // 2. sound-alike names: needed shards + random padding, shuffled
  const fq = queries.filter(q => q.tier === 'phonetic');
  if (!fq.length) return { gaveUp: 0, fuzzyShards: 0 };
  const hex = b => b.toString(16).padStart(2, '0');
  const want = new Set(fq.map(q => hex(q.aesKeyBytes[0])));
  const minShards = Math.min(256, meta.fuzzy_min_shards || 32);
  while (want.size < minShards) want.add(hex(crypto.getRandomValues(new Uint8Array(1))[0]));
  const ids = [...want];
  for (let i = ids.length - 1; i > 0; i--) {
    const j = crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1);
    [ids[i], ids[j]] = [ids[j], ids[i]];
  }
  const shards = {};
  let gaveUp = 0;
  const max = meta.fuzzy_batch_max || 64;
  for (let i = 0; i < ids.length; i += max) {
    const batch = ids.slice(i, i + max);
    let res = null;
    for (let attempt = 0; attempt < 4 && !(res && res.ok); attempt++) {
      if (attempt) await new Promise(r => setTimeout(r, 500 * attempt));
      try {
        res = await fetch(meta.fuzzy_batch_url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                  body: JSON.stringify({ shards: batch }) });
      } catch (e) { res = null; }
    }
    if (!(res && res.ok)) { gaveUp += batch.length; continue; }
    const buf = new Uint8Array(await res.arrayBuffer());
    const dv = new DataView(buf.buffer);
    for (let o = 0; o + 5 <= buf.length; ) {
      const id = hex(buf[o]), len = dv.getUint32(o + 1, true);
      o += 5;
      if (len) shards[id] = contactPsiParseCompact(buf.subarray(o, o + len), pb);
      o += len;
    }
  }
  for (const q of fq) {
    const f = shards[hex(q.aesKeyBytes[0])];
    if (f) await decryptAll(q, contactPsiCompactFind(f, q.aesKeyBytes));
  }
  return { gaveUp, fuzzyShards: ids.length };
}

function contactPsiNormalizeExact(name) {
  let text = String(name).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  text = text.replace(/[-'.,]/g, ' ').replace(/[^a-z0-9\s]/g, '');
  const tokens = text.split(/\s+/).filter(Boolean);
  if (tokens.length && CONTACT_PSI_SUFFIXES.has(tokens[tokens.length - 1])) tokens.pop();
  return tokens.sort().join(' ');
}

function contactPsiSoundexToken(token) {
  const letters = [...token].filter(c => c >= 'a' && c <= 'z');
  if (!letters.length) return '0000';
  const digits = [];
  let last = null;
  for (const c of letters) {
    if (c === 'h' || c === 'w') continue;
    const cls = CONTACT_PSI_SOUNDEX[c];
    if (cls !== last) {
      digits.push(cls);
      if (digits.length === 4) break;
    }
    last = cls;
  }
  while (digits.length < 4) digits.push('0');
  return digits.join('');
}

function contactPsiPhoneticKey(name) {
  const normalized = contactPsiNormalizeExact(name);
  if (!normalized) return '';
  return normalized.split(' ').map(contactPsiSoundexToken).sort().join('|');
}

// "Mark Greene (Ibm)" -> "Mark Greene": split namesakes carry their employer
// in parentheses (webapp/disambiguation.py, contact_psi.manifest.base_name).
function contactPsiBaseName(name) {
  const i = name.indexOf(' (');
  return name.endsWith(')') && i > 0 ? name.slice(0, i) : name;
}

function contactPsiNodeOrg(name) {
  const i = name.indexOf(' (');
  return name.endsWith(')') && i > 0 ? name.slice(i + 2, -1) : '';
}

const PSI_ORG_STOP = new Set(['inc', 'llc', 'llp', 'lp', 'ltd', 'limited', 'co', 'corp', 'corporation', 'company',
  'the', 'group', 'plc', 'holdings', 'sa', 'ag', 'gmbh', 'of', 'and', 'us', 'usa']);
function contactPsiOrgTokens(org) {
  return (org || '').toLowerCase().replace(/&/g, ' and ').replace(/[^a-z0-9 ]/g, ' ').split(/\s+/)
    .filter(t => t && !PSI_ORG_STOP.has(t));
}

// 0..1: how well a LinkedIn company names a candidate's employer.
// "IBM" ~ "International Business Machines Corporation" via initials.
function contactPsiOrgScore(company, nodeOrg) {
  const a = contactPsiOrgTokens(company), b = contactPsiOrgTokens(nodeOrg);
  if (!a.length || !b.length) return 0;
  const A = new Set(a), B = new Set(b);
  if (a.join(' ') === b.join(' ')) return 1;
  const initials = t => t.map(w => w[0]).join('');
  if ((a.length === 1 && a[0].length > 1 && a[0] === initials(b))
      || (b.length === 1 && b[0].length > 1 && b[0] === initials(a))) return 0.9;
  const shared = [...A].filter(t => B.has(t)).length;
  if (shared && (shared === A.size || shared === B.size)) return 0.8;
  return shared / new Set([...A, ...B]).size;
}

function contactPsiTrigrams(name) {
  const padded = ' ' + contactPsiNormalizeExact(name) + ' ';
  const set = new Set();
  for (let i = 0; i <= padded.length - 3; i++) set.add(padded.slice(i, i + 3));
  return set;
}

function contactPsiJaccard(a, b) {
  if (!a.size && !b.size) return 1;
  let intersection = 0;
  for (const g of a) if (b.has(g)) intersection++;
  const union = a.size + b.size - intersection;
  return union ? intersection / union : 0;
}

function contactPsiFnv1a32(value) {
  let h = 0x811C9DC5;
  for (const byte of new TextEncoder().encode(value)) {
    h ^= byte;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

const CONTACT_PSI_MERSENNE_PRIME = (1n << 61n) - 1n;
// Pinned coefficient table (first 24 of the 32 in
// specifications/contact-check-psi-test-vectors.json) -- must never be
// regenerated, only copied verbatim, or phonetic/possible-tier matches
// silently stop working against the manifest built with the Python table.
const CONTACT_PSI_MINHASH_COEFFICIENTS = [
  [1979489632114620755n, 1509789760387149117n], [1918805637228309363n, 833186062390129979n],
  [495730073182853211n, 595723440359463397n], [90324890263903832n, 445813994702288431n],
  [2257285402276830348n, 1119254023105354110n], [1021786013160126921n, 1265193707630753218n],
  [212068324108876953n, 504007361524737770n], [414280352786679535n, 803222919456732142n],
  [1768409434185088727n, 1986075548297811767n], [1742620678125402464n, 2257046664463391349n],
  [1415753637546083748n, 593533786031525589n], [1196761435141869264n, 2265235534276433043n],
  [395539926857611198n, 457238027093643287n], [1503513604051188411n, 1643417863877736261n],
  [1816084546320001784n, 1408403254850552953n], [1707502431300134009n, 1007438581320924473n],
  [2225548798102152013n, 1814493586781104521n], [567344556111402844n, 1649644300899723705n],
  [463722948478937003n, 762157748086021223n], [1390024454749409324n, 597300290928022787n],
  [535273311151590049n, 445969734299799432n], [1794290647980509285n, 1609987668421870033n],
  [1379461383878485334n, 176252640438661704n], [1433166327964526570n, 1929046851930636275n],
];

function contactPsiMinhashSignature(trigramSet) {
  if (!trigramSet.size) return new Array(24).fill(0n);
  const hashes = [...trigramSet].map(g => BigInt(contactPsiFnv1a32(g)));
  return CONTACT_PSI_MINHASH_COEFFICIENTS.map(([a, b]) => {
    let m = null;
    for (const x of hashes) {
      const v = (a * x + b) % CONTACT_PSI_MERSENNE_PRIME;
      if (m === null || v < m) m = v;
    }
    return m;
  });
}

function contactPsiLshBandTokens(name) {
  const signature = contactPsiMinhashSignature(contactPsiTrigrams(name));
  const tokens = [];
  for (let i = 0; i < 3; i++) tokens.push(`${i}:` + signature.slice(i * 8, (i + 1) * 8).join(','));
  return tokens;
}

// Minimal RFC 4180 CSV line splitter (handles quoted fields, including
// commas and escaped "" inside a quoted field) -- LinkedIn's export quotes
// company/position names that routinely contain commas ("Acme, Inc."), so a
// plain line.split(',') would misalign columns on exactly the rows that
// matter most.
function csvSplitLine(line) {
  const fields = [];
  let cur = '', inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuotes) {
      if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (c === '"') inQuotes = false;
      else cur += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ',') { fields.push(cur); cur = ''; }
    else cur += c;
  }
  fields.push(cur);
  return fields;
}

// --- Reading a contacts file (2026-11-17) ------------------------------------
// LinkedIn Connections.csv, Google / Outlook / Apple contact CSVs, any CSV
// with name columns, vCard, or a plain list of names. Returns
// {contacts: [{name, company, title}], format}. Emails, phone numbers and
// every other column are ignored, and nothing here leaves the browser.
const CONTACT_PSI_COLUMNS = {
  first: ['first name', 'given name', 'firstname', 'first'],
  last: ['last name', 'family name', 'surname', 'lastname', 'last'],
  full: ['name', 'full name', 'display name', 'contact name', 'contact'],
  company: ['company', 'organization name', 'organization 1 - name', 'organization', 'organisation', 'company name',
            'employer', 'firm', 'account name'],
  // "job title" before "title": Outlook's "Title" column is Mr./Ms.
  title: ['job title', 'position', 'organization title', 'organization 1 - title', 'title', 'role'],
};

// Whole-file CSV reader (RFC 4180): quoted fields may hold commas, "" and
// line breaks (Google's export puts multi-line notes in quotes).
function contactPsiCsvRows(text) {
  const rows = [];
  let row = [], cur = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cur += '"'; i++; }
      else if (c === '"') quoted = false;
      else cur += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(cur); cur = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cur); rows.push(row); row = []; cur = '';
    } else cur += c;
  }
  if (cur || row.length) { row.push(cur); rows.push(row); }
  return rows.filter(r => r.some(f => f.trim()));
}

function contactPsiParseFile(text) {
  text = String(text || '').replace(/^﻿/, '');
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim().slice(0, 200);
  const out = [], seen = new Set();
  const add = (name, company, title) => {
    name = clean(name);
    if (name.length <= 3 || !/\p{L}/u.test(name) || !name.includes(' ')) return;   // first and last name
    const c = { name, company: clean(company), title: clean(title) };
    const key = (c.name + '|' + c.company).toLowerCase();
    if (!seen.has(key)) { seen.add(key); out.push(c); }
  };
  if (/^\s*BEGIN:VCARD\s*$/im.test(text)) {
    const unesc = s => String(s || '').replace(/\\n/gi, ' ').replace(/\\([,;\\])/g, '$1');
    for (const card of text.split(/^\s*BEGIN:VCARD\s*$/im).slice(1)) {
      const f = {};
      for (const line of card.replace(/\r?\n[ \t]/g, '').split(/\r?\n/)) {
        const i = line.indexOf(':');
        if (i < 0) continue;
        const key = line.slice(0, i).split(';')[0].toUpperCase().replace(/^ITEM\d+\./, '');
        if (!(key in f)) f[key] = line.slice(i + 1);
      }
      const n = (f.N || '').split(';');
      add(f.FN ? unesc(f.FN) : [n[1], n[0]].map(unesc).filter(Boolean).join(' '),
          unesc((f.ORG || '').split(';')[0]), unesc(f.TITLE));
    }
    return { contacts: out, format: 'vcard' };
  }
  const rows = contactPsiCsvRows(text);
  const norm = s => s.trim().toLowerCase().replace(/\s+/g, ' ');
  // the header can sit below a few lines of notes (LinkedIn does this)
  for (let h = 0; h < Math.min(rows.length, 20); h++) {
    const header = rows[h].map(norm);
    const ix = {};
    for (const [k, names] of Object.entries(CONTACT_PSI_COLUMNS)) {
      ix[k] = -1;
      for (const nm of names) { const i = header.indexOf(nm); if (i >= 0) { ix[k] = i; break; } }
    }
    if (!((ix.first >= 0 && ix.last >= 0) || ix.full >= 0)) continue;
    const get = (r, i) => (i >= 0 ? (r[i] || '').trim() : '');
    for (const r of rows.slice(h + 1)) {
      const first = get(r, ix.first), last = get(r, ix.last);
      add(first && last ? `${first} ${last}` : (get(r, ix.full) || `${first} ${last}`), get(r, ix.company), get(r, ix.title));
    }
    return { contacts: out, format: 'columns' };
  }
  // No header row: one contact per line. "First,Last[,Company[,Title]]" when
  // the first two fields are single words, else "Full Name[,Company[,Title]]".
  for (const r of rows) {
    const f = r.map(s => s.trim());
    if (f.length >= 2 && /^\S+$/.test(f[0]) && /^\S+$/.test(f[1])) add(`${f[0]} ${f[1]}`, f[2], f[3]);
    else add(f[0], f[1], f[2]);
  }
  return { contacts: out, format: 'lines' };
}

// --- The matching itself -----------------------------------------------------
// names: the contacts to check; info: {name: {company, position}} -- used only
// here, to rank same-named people; onStatus(html): progress messages.
// Resolves to {candidates: {name: [{name, tier, org, ...}, best first]},
// gaveUp, shardsTotal}. Only blinded points are sent to the server.
async function contactPsiMatch(names, info = {}, onStatus = () => {}) {
  let RistrettoPoint;
  try {
    ({ RistrettoPoint } = await import('https://cdn.jsdelivr.net/npm/@noble/curves@1.9.7/esm/ed25519.js/+esm'));
  } catch (e) {
    throw new Error('Could not load the crypto library (cdn.jsdelivr.net) needed for contact matching -- check your network connection, or that your browser/DNS isn’t blocking that domain, and try again.');
  }
  let metaResponse;
  try {
    metaResponse = await fetch('/api/contacts/manifest-meta');
  } catch (e) {
    throw new Error('Could not reach the server (manifest-meta) -- check your network connection and try again.');
  }
  if (!metaResponse.ok) throw new Error('Contact matching is temporarily unavailable');
  const meta = await metaResponse.json();

  const sha512 = async (bytes) => new Uint8Array(await crypto.subtle.digest('SHA-512', bytes));
  const sha256 = async (bytes) => new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const textBytes = (s) => new TextEncoder().encode(s);
  const concatBytes = (...arrs) => { const out = new Uint8Array(arrs.reduce((n, a) => n + a.length, 0)); let o = 0; for (const a of arrs) { out.set(a, o); o += a.length; } return out; };
  const toB64 = bytes => btoa(String.fromCharCode(...bytes));
  const fromB64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const bytesToHexPrefix = (bytes, n) => Array.from(bytes.slice(0, n)).map(b => b.toString(16).padStart(2, '0')).join('');
  const leBytesToBigInt = (bytes) => { let x = 0n; for (let i = bytes.length - 1; i >= 0; i--) x = (x << 8n) | BigInt(bytes[i]); return x; };
  const randomScalar = () => RistrettoPoint.Fn.create(leBytesToBigInt(crypto.getRandomValues(new Uint8Array(32))));
  const pointFor = async (namespace, item) => RistrettoPoint.hashToCurve(await sha512(concatBytes(textBytes(namespace), new Uint8Array([0]), textBytes(item))));

  // Compact manifest (format 2): exact always, sound-alike only for files of
  // up to meta.fuzzy_max_contacts, no LSH "possible" tier.
  const compact = meta.format === 2;
  const fuzzyOn = !compact || names.length <= (meta.fuzzy_max_contacts || 1000);
  const queries = [];
  for (const name of names) {
    const exact = contactPsiNormalizeExact(name);
    if (exact) queries.push({ name, tier: 'exact', item: exact });
    const phonetic = fuzzyOn ? contactPsiPhoneticKey(name) : null;
    if (phonetic) queries.push({ name, tier: 'phonetic', item: phonetic });
    if (!compact) for (const token of contactPsiLshBandTokens(name)) queries.push({ name, tier: 'possible', item: token });
  }

  // Refuse up front, before any quota is spent, if the file is bigger than
  // what this account can check right now (daily quota + one-time bulk).
  try {
    const qr = await fetch('/api/contacts/quota');
    if (qr.ok) {
      const quota = await qr.json();
      if (queries.length > quota.daily_points + quota.bulk_points) {
        throw new Error(`This file has ${names.length.toLocaleString()} contacts. You can check about `
          + `${quota.contacts_now.toLocaleString()} right now (${quota.daily_contacts.toLocaleString()} a day, plus a one-time `
          + `allowance of ${quota.bulk_contacts.toLocaleString()} for your first big upload). Split the file, or try again tomorrow.`);
      }
    }
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('This file has')) throw e;
  }

  for (const q of queries) {
    q.blindScalar = randomScalar();
    q.blindedPoint = (await pointFor(q.tier, q.item)).multiply(q.blindScalar);
  }

  const evaluatedPoints = [];
  for (let offset = 0; offset < queries.length; offset += 3000) {
    const chunk = queries.slice(offset, offset + 3000);
    let response;
    try {
      response = await fetch('/api/contacts/oprf-eval', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key_version: meta.key_version, points: chunk.map(q => toB64(q.blindedPoint.toRawBytes())) }),
      });
    } catch (e) {
      throw new Error(`Could not reach the server (oprf-eval, batch at offset ${offset}) -- check your network connection and try again.`);
    }
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'OPRF evaluation failed');
    evaluatedPoints.push(...(await response.json()).points);
  }

  // Unblind and derive the AES key for every query.
  const kvBytes = new Uint8Array(4);
  new DataView(kvBytes.buffer).setUint32(0, meta.key_version, false);
  for (let i = 0; i < queries.length; i++) {
    const q = queries[i];
    const responsePoint = RistrettoPoint.fromHex(fromB64(evaluatedPoints[i]));
    const unblinded = responsePoint.multiply(RistrettoPoint.Fn.inv(q.blindScalar));
    q.aesKeyBytes = await sha256(concatBytes(textBytes('contact-psi-v1'), kvBytes, unblinded.toRawBytes()));
    q.prefix = bytesToHexPrefix(q.aesKeyBytes, 4);
    q.shardId = q.prefix.slice(0, meta.shard_hex_chars || 2);
  }

  // Format 2: whole exact file + padded sound-alike shards. Older format 1
  // (JSON shards, batched POSTs -- one GET per shard tripped Cloudflare's
  // rate limit) is still read if a server serves it.
  const buckets = {};
  let gaveUp = 0, shardsTotal = 0;
  if (compact) {
    const r = await contactPsiCompactLookup(queries, meta, onStatus, sha256, textBytes, concatBytes);
    gaveUp = r.gaveUp;
    shardsTotal = r.fuzzyShards;
  } else {
    const needed = [...new Set(queries.map(q => q.shardId))];
    shardsTotal = needed.length;
    const batchUrl = meta.manifest_shards_batch_url || '/api/contacts/manifest-shards';
    const batchMax = meta.manifest_shards_batch_max || 25;
    const fetchBatch = async (shardIds) => {
      for (let attempt = 0; ; attempt++) {
        let response;
        try {
          response = await fetch(batchUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                             body: JSON.stringify({ shards: shardIds }) });
        } catch (e) {
          if (attempt < 2) { await new Promise(r => setTimeout(r, 300 * (attempt + 1))); continue; }
          gaveUp += shardIds.length;
          return;
        }
        if (response.status === 429 && attempt < 4) {
          const retryAfter = Math.min(Number(response.headers.get('Retry-After')) || 1, 3);
          await new Promise(r => setTimeout(r, retryAfter * 1000));
          continue;
        }
        if (!response.ok) { gaveUp += shardIds.length; return; }
        Object.assign(buckets, (await response.json()).buckets || {});
        return;
      }
    };
    const queue = [];
    for (let i = 0; i < needed.length; i += batchMax) queue.push(needed.slice(i, i + batchMax));
    const worker = async () => { while (queue.length) await fetchBatch(queue.shift()); };
    await Promise.all(Array.from({ length: Math.min(4, queue.length) }, worker));
  }

  const tierRank = { exact: 0, phonetic: 1, possible: 2 };
  // Every plausible graph node per contact, not just the top one: a common
  // name has namesakes ("Mark Greene", "Michael Smith (Goldman Sachs)", ...).
  const candsByContact = {};
  for (const q of queries) {
    let payloads = q.payloads;   // compact format: already looked up
    if (!payloads) {
      payloads = [];
      const entries = buckets[q.prefix] || [];
      if (!entries.length) continue;
      const cryptoKey = await crypto.subtle.importKey('raw', q.aesKeyBytes, 'AES-GCM', false, ['decrypt']);
      for (const entry of entries) {
        try {
          const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: fromB64(entry.nonce) }, cryptoKey, fromB64(entry.ct));
          payloads.push(JSON.parse(new TextDecoder().decode(pt)));
        } catch (e) { /* wrong key for this bucket (prefix collision) -- expected */ }
      }
    }
    for (const payload of payloads) {
      for (const m of payload.matches || []) {
        // Sound-alike keys are many-to-one ("Katharine Lee" and "Jude Law"
        // share a Soundex code), so re-check the spelling locally before
        // showing a non-exact match.
        let similarity = 1;
        if (q.tier !== 'exact') {
          similarity = contactPsiJaccard(contactPsiTrigrams(q.name), contactPsiTrigrams(contactPsiBaseName(m.name)));
          if (similarity < 0.5) continue;
        }
        const rank = tierRank[q.tier];
        const cands = candsByContact[q.name] || (candsByContact[q.name] = {});
        const current = cands[m.id];
        if (!current || rank < current.rank || (rank === current.rank && similarity > current.similarity)) {
          cands[m.id] = { rank, similarity, tier: q.tier, name: m.name, id: m.id,
                          order: current ? current.order : Object.keys(cands).length };
        }
      }
    }
  }
  return { candidates: contactPsiRank(candsByContact, info), gaveUp, shardsTotal };
}

// Best first: a namesake whose employer matches the contact's company, then
// exact before sound-alike, then spelling, then the server's order (degree).
function contactPsiRank(candsByContact, info) {
  const candidates = {};
  for (const contact of Object.keys(candsByContact)) {
    const company = (info[contact] || {}).company || '';
    candidates[contact] = Object.values(candsByContact[contact])
      .map(c => Object.assign(c, { org: contactPsiOrgScore(company, contactPsiNodeOrg(c.name)) }))
      .sort((a, b) => (b.org >= 0.5) - (a.org >= 0.5) || a.rank - b.rank || b.org - a.org
                      || b.similarity - a.similarity || a.order - b.order)
      .slice(0, 12);
  }
  return candidates;
}

// A match the person can add without choosing between namesakes.
function contactPsiConfident(cands) {
  return cands[0].tier !== 'possible' && (cands.length === 1 || cands[0].org >= 0.5);
}

if (typeof module !== 'undefined') module.exports = { contactPsiParseFile, contactPsiCsvRows, contactPsiRank, contactPsiConfident,
  contactPsiNormalizeExact, contactPsiPhoneticKey, contactPsiOrgScore };

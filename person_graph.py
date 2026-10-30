"""People-only graph for community detection (2026-10-30).

Communities are made of people, not people and organizations (operator's
rule): Leiden runs on ties between two people, never on organization nodes.

  * a direct person-person link is a tie, weighted by its edge probability;
  * two people linked to the same organization are tied, weighted
    w1 * w2 / (members - 1): sharing a 5-person board counts far more than
    sharing a 20,000-member union (Newman's collaboration weighting);
  * an organization with more than CAP people would make every pair a tie
    (the IBEW alone: 350M pairs), so each of its members is instead tied to
    RING random fellow members on either side of a shuffled ring, with the
    same total weight per member;
  * organization-organization links make no tie.

Each tie keeps a bit mask of the link categories it came from, so callers can
tell school ties from board ties. Organizations stay available as context: the
person-organization links come back too, for "which organizations does this
community centre on".
"""
import numpy as np

CAP = 200
RING = 5


def category_masks(cats):
    """cats: per-edge tuples of categories -> (int64 bit mask per edge, category names by bit)."""
    names = sorted({c for cs in cats for c in cs})
    bit = {c: 1 << i for i, c in enumerate(names)}
    return np.array([sum(bit[c] for c in set(cs)) for cs in cats], np.int64), names


def build(n, person, A, B, W, M, seed=42):
    """n nodes; person: bool per node; A, B, W, M: edge endpoints, weights and
    category masks (donation-only edges already removed, if wanted).

    Returns a dict: pid (node id of each person, i.e. local id -> node id),
    ta, tb, tw, tm (ties between local ids, weight, category mask), and
    link_person, link_org (node ids of every person-organization link)."""
    person = np.asarray(person, bool)
    pp = person[A] & person[B]
    po = person[A] ^ person[B]
    per = np.where(person[A], A, B)[po]
    org = np.where(person[A], B, A)[po]
    wo, mo = W[po], M[po]
    o = np.argsort(org, kind="stable")
    per_s, org_s, wo, mo = per[o], org[o], wo[o], mo[o]
    starts = np.flatnonzero(np.r_[True, org_s[1:] != org_s[:-1]]) if len(org_s) else np.array([], np.int64)
    ends = np.r_[starts[1:], len(org_s)]
    srcs, tgts, ws, ms = [A[pp]], [B[pp]], [W[pp]], [M[pp]]
    rng = np.random.default_rng(seed)
    for s, e in zip(starts.tolist(), ends.tolist()):
        m = e - s
        if m < 2:
            continue
        mem, mw, mm = per_s[s:e], wo[s:e], mo[s:e]
        if m <= CAP:
            i, j = np.triu_indices(m, 1)
            srcs.append(mem[i]); tgts.append(mem[j])
            ws.append(mw[i] * mw[j] / (m - 1)); ms.append(mm[i] | mm[j])
        else:
            q = rng.permutation(m)
            mem, mw, mm = mem[q], mw[q], mm[q]
            for r in range(1, RING + 1):
                j = (np.arange(m) + r) % m
                srcs.append(mem); tgts.append(mem[j])
                ws.append(mw * mw[j] / (2 * RING)); ms.append(mm | mm[j])
    S, T = np.concatenate(srcs), np.concatenate(tgts)
    Wt, Mt = np.concatenate(ws), np.concatenate(ms)
    del srcs, tgts, ws, ms
    a, b = np.minimum(S, T), np.maximum(S, T)
    keep = a != b       # the same person reached twice (duplicate links)
    key = a[keep] * n + b[keep]
    Wt, Mt = Wt[keep], Mt[keep]
    del S, T, a, b
    o = np.argsort(key, kind="stable")
    key, Wt, Mt = key[o], Wt[o], Mt[o]
    first = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    tw = np.add.reduceat(Wt, first)
    tm = np.bitwise_or.reduceat(Mt, first)
    key = key[first]
    pid = np.flatnonzero(person)
    loc = -np.ones(n, np.int64)
    loc[pid] = np.arange(len(pid))
    return {"pid": pid, "ta": loc[key // n], "tb": loc[key % n], "tw": tw, "tm": tm,
            "link_person": per, "link_org": org}

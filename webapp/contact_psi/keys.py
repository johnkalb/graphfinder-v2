"""Canonical, cross-language item derivation for contact PSI."""
from __future__ import annotations
import re
import unicodedata

P = 2**61 - 1
BANDS = 3
ROWS_PER_BAND = 8
MINHASH_COEFFICIENTS = [
    (1979489632114620755, 1509789760387149117), (1918805637228309363, 833186062390129979),
    (495730073182853211, 595723440359463397), (90324890263903832, 445813994702288431),
    (2257285402276830348, 1119254023105354110), (1021786013160126921, 1265193707630753218),
    (212068324108876953, 504007361524737770), (414280352786679535, 803222919456732142),
    (1768409434185088727, 1986075548297811767), (1742620678125402464, 2257046664463391349),
    (1415753637546083748, 593533786031525589), (1196761435141869264, 2265235534276433043),
    (395539926857611198, 457238027093643287), (1503513604051188411, 1643417863877736261),
    (1816084546320001784, 1408403254850552953), (1707502431300134009, 1007438581320924473),
    (2225548798102152013, 1814493586781104521), (567344556111402844, 1649644300899723705),
    (463722948478937003, 762157748086021223), (1390024454749409324, 597300290928022787),
    (535273311151590049, 445969734299799432), (1794290647980509285, 1609987668421870033),
    (1379461383878485334, 176252640438661704), (1433166327964526570, 1929046851930636275),
    (1079066032773371540, 1482811487008790911), (272359009063176401, 307189749562611427),
    (802406842664134071, 895266475138573569), (993843574814203135, 424729575475090894),
    (1630875757507514403, 1523534392560322564), (2003581921769767963, 460751848290301596),
    (657865912588432896, 1279658912092487520), (2067991725699937825, 1862262960166710096),
]

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_SOUNDEX = {
    **dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"), "l": "4", **dict.fromkeys("mn", "5"),
    "r": "6", **dict.fromkeys("aeiouy", "0"),
}

def normalize_exact(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[-'.,]", " ", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    tokens = text.split()
    if tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(sorted(tokens))

def soundex_token(token: str) -> str:
    letters = [c for c in token.lower() if "a" <= c <= "z"]
    if not letters:
        return "0000"
    digits, last = [], None
    for c in letters:
        if c in "hw":
            continue
        cls = _SOUNDEX[c]
        if cls != last:
            digits.append(cls)
            if len(digits) == 4:
                break
        last = cls
    return "".join(digits).ljust(4, "0")

def phonetic_key(name: str) -> str:
    normalized = normalize_exact(name)
    return "|".join(sorted(soundex_token(token) for token in normalized.split())) if normalized else ""

def trigrams(name: str) -> set[str]:
    padded = " " + normalize_exact(name) + " "
    return {padded[i:i + 3] for i in range(max(0, len(padded) - 2))}

def fnv1a32(value: str) -> int:
    h = 0x811C9DC5
    for byte in value.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h

def minhash_signature(name: str) -> list[int]:
    grams = trigrams(name)
    if not grams:
        return [0] * 24
    return [min((a * fnv1a32(g) + b) % P for g in grams) for a, b in MINHASH_COEFFICIENTS[:24]]

def lsh_band_tokens(name: str) -> list[str]:
    signature = minhash_signature(name)
    return [f"{i}:" + ",".join(str(v) for v in signature[i * ROWS_PER_BAND:(i + 1) * ROWS_PER_BAND]) for i in range(BANDS)]

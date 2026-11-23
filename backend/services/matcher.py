"""Field comparison. Every comparer returns:
    {status: MATCH|MISMATCH|NOT_FOUND, match_score: 0-100, expected, extracted, method, note?}
"""
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Optional

from rapidfuzz import fuzz

TITLES = {"mr", "mrs", "ms", "miss", "shri", "sri", "shree", "smt", "shrimati", "srimati", "sushri", "kumari",
          "km", "kum", "dr", "late"}
DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%d %b %Y",
                "%B %d, %Y", "%b %d, %Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y")

DEFAULT_THRESHOLD = 85.0

# Words small models write instead of a real null
NULL_WORDS = {"", "null", "none", "nil", "n/a", "na", "-", "--", "not available", "not printed", "unknown"}
NEGATIONS = {"not", "no", "non", "fail", "failed", "unsuccessful", "absent", "withheld", "cancelled"}
FILLER_WORDS ={"of", "the", "and", "for", "&", "in"}


# ---------------------------------------------------------------- normalisers
def _norm_text(s: Any) -> str:
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    s = re.sub(r"[^\w\s]|_", " ", s)
    return " ".join(s.split())


def _norm_name(s: Any) -> str:
    return " ".join(t for t in _norm_text(s).split() if t not in TITLES)


def _norm_id(s: Any) -> str:
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", str(s)).casefold())


def to_number(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group(0)) if m else None


def parse_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v).strip())
    s = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", s, flags=re.I)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ------------------------------------------------------------------- comparers
def _result(status, score, expected, extracted, method, note=None):
    r = {"status": status, "match_score": round(float(score), 1),
         "expected": expected, "extracted": extracted, "method": method}
    if note:
        r["note"] = note
    return r


def _expand_abbreviations(short: str, long: str) -> str:
    """Rewrite `long` so that any run of words whose initials spell an abbreviation used in `short`
    is replaced by that abbreviation.

    'cbse'      / 'central board of secondary education'          -> 'cbse'
    'iit guwahati' / 'indian institute of technology guwahati'      -> 'iit guwahati'
    'gate 2026' / 'graduate aptitude test in engineering 2026'      -> 'gate 2026'
    """
    words = long.split()
    for tok in short.split():
        if not (2 <= len(tok) <= 8 and tok.isalpha()) or tok in words:
            continue
        idx = [i for i, w in enumerate(words) if w not in FILLER_WORDS]  # content words only
        for k in range(len(idx) - len(tok) + 1):
            run = idx[k:k + len(tok)]
            if "".join(words[i][0] for i in run) == tok:
                words = words[:run[0]] + [tok] + words[run[-1] + 1:]
                break
    return " ".join(words)


def _contains(a: str, b: str) -> bool:
    """All words of the shorter value appear in the longer one ('pass' in 'pass in first division').
    A negation in the longer value that the shorter lacks ('not pass', 'not qualified') blocks it."""
    ta, tb = set(a.split()), set(b.split())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not short or not short <= long_:
        return False
    return not ((long_ - short) & NEGATIONS)


def _variants(raw: Any, norm) -> list:
    """The value itself plus, if it has a bracketed part, the text outside and inside the brackets.

    'Kumhar(Prajapati)' -> ['kumhar prajapati', 'kumhar', 'prajapati']   (documents often print an
    alternate name or a code in brackets, and an applicant may give either one)
    """
    s = str(raw)
    out = [norm(s)]
    inside = re.findall(r"[(\[{]([^)\]}]*)[)\]}]", s)
    if inside:
        out += [norm(re.sub(r"[(\[{][^)\]}]*[)\]}]", " ", s))] + [norm(i) for i in inside]
    seen, res = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            res.append(v)
    return res


def _merge_split_tokens(a: str, b: str):
    """A space inside a name is not a different name: 'ram kumar malviya' / 'ramkumar malviya'
    -> both 'ramkumar malviya' (adjacent words are merged when the other side has them joined)."""
    def merge(src: str, other: set) -> str:
        toks, out, i = src.split(), [], 0
        while i < len(toks):
            if i + 1 < len(toks) and toks[i] + toks[i + 1] in other:
                out.append(toks[i] + toks[i + 1])
                i += 2
            else:
                out.append(toks[i])
                i += 1
        return " ".join(out)
    return merge(a, set(b.split())), merge(b, set(a.split()))


MIN_NAME_PART_SIMILARITY = 75.0


def _weakest_name_part(a: str, b: str) -> float:
    """How well the worst-matching word of the shorter name is matched by the longer name (0-100).

    A single letter is an initial and matches any word starting with it ('r k malviya' ~ 'ram kumar malviya').
    """
    ta, tb = a.split(), b.split()
    short, pool = (ta, list(tb)) if len(ta) <= len(tb) else (tb, list(ta))
    worst = 100.0
    for t in short:
        if not pool:
            return 0.0
        if len(t) == 1:
            hit = next((i for i, w in enumerate(pool) if w.startswith(t)), None)
            sim, idx = (100.0, hit) if hit is not None else (0.0, 0)
        else:
            idx = max(range(len(pool)), key=lambda i: fuzz.ratio(t, pool[i]))
            sim = fuzz.ratio(t, pool[idx])
        worst = min(worst, sim)
        pool.pop(idx)
    return worst


def _expand_initials(short: str, long: str) -> str:
    """Replace single-letter words in `short` by the word of `long` they stand for ('r k malviya' -> 'ram kumar malviya')."""
    pool, out = long.split(), []
    for t in short.split():
        if len(t) == 1:
            hit = next((w for w in pool if w.startswith(t) and w not in short.split()), None)
            out.append(hit or t)
            if hit:
                pool.remove(hit)
        else:
            out.append(t)
    return " ".join(out)


def _score_pair(spec, a: str, b: str, is_name: bool):
    """Similarity (0-100) of two already-normalised strings, and an optional explanatory note."""
    if spec.get("contains") and any(_contains(x, y) for x, y in
                                    ((a, b), (a, _expand_abbreviations(a, b)), (_expand_abbreviations(b, a), b))):
        return 100.0, "Expected value found within the document value."
    score, note = fuzz.token_sort_ratio(a, b), None

    # word spacing: 'ram kumar' / 'ramkumar'
    # (only an IDENTICAL letter sequence is forgiven: 'ram kumar' vs 'rama kumari' are different people)
    a2, b2 = _merge_split_tokens(a, b)
    candidates = [fuzz.token_sort_ratio(a2, b2)]
    if a.replace(" ", "") == b.replace(" ", ""):
        candidates.append(100.0)
    for cand in candidates:
        if cand > score:
            score, note = cand, "Differs only in word spacing." if cand == 100 else None

    if not is_name:  # abbreviations: CBSE / IIT Guwahati / GATE 2026 vs their full forms
        b_exp, a_exp = _expand_abbreviations(a, b), _expand_abbreviations(b, a)
        for x, y in ((a, b_exp), (a_exp, b)):
            if (x, y) != (a, b) and fuzz.token_sort_ratio(x, y) > score:
                score, note = fuzz.token_sort_ratio(x, y), "Matched using an abbreviation."
    if score < 100 and fuzz.token_set_ratio(a, b) == 100:
        # one name is a token-subset of the other (e.g. missing middle name)
        smaller = min(len(a.split()), len(b.split()))
        subset = 90.0 if smaller >= 2 else 70.0
        if subset > score:
            score, note = subset, "One value is a subset of the other (missing/extra name part)."
    if is_name:  # written initials: 'R K Malviya' ~ 'Ram Kumar Malviya' (never shown as an exact match)
        for x, y in ((_expand_initials(a2, b2), b2), (a2, _expand_initials(b2, a2))):
            if (x, y) != (a2, b2) and min(fuzz.token_sort_ratio(x, y), 95.0) > score:
                score, note = min(fuzz.token_sort_ratio(x, y), 95.0), "Matched using initials."
    if is_name and score >= 70 and _weakest_name_part(a2, b2) < MIN_NAME_PART_SIMILARITY:
        # An overall score can hide a wrong first name behind a long identical surname
        # ('Ram Kumar Malviya' vs 'Shyam Kumar Malviya'): every name part needs a close counterpart.
        score, note = 70.0, "A part of the name differs."
    if not is_name:
        # Fuzzy matching forgives a single wrong character, which is wrong for numbers
        # ("Semester 4" vs "Semester 5", "GATE 2025" vs "GATE 2026"): differing numbers = mismatch.
        na = {t for t in a.split() if any(c.isdigit() for c in t)}
        nb = {t for t in b.split() if any(c.isdigit() for c in t)}
        if na and nb and na != nb:
            score, note = min(score, 60.0), "Numbers in the two values differ."
    return score, note


def _cmp_name_or_text(spec, expected, extracted, is_name):
    norm = _norm_name if is_name else _norm_text
    method = "fuzzy_name" if is_name else "fuzzy_text"
    va, vb = _variants(expected, norm), _variants(extracted, norm)
    if not va or not vb:
        return _result("NOT_FOUND", 0, expected, extracted, method)

    best_score, best_note = -1.0, None
    for x in va:
        for y in vb:
            score, note = _score_pair(spec, x, y, is_name)
            if score > best_score:
                best_score = score
                best_note = note or ("Matched using the alternate name in brackets."
                                     if (x, y) != (va[0], vb[0]) else None)
    threshold = float(spec.get("threshold", DEFAULT_THRESHOLD))
    status = "MATCH" if best_score >= threshold else "MISMATCH"
    return _result(status, best_score, expected, extracted, method, best_note)


def _cmp_id(spec, expected, extracted):
    a, b = _norm_id(expected), _norm_id(extracted)
    if not a or not b:
        return _result("NOT_FOUND", 0, expected, extracted, "exact_id")
    if a == b:
        return _result("MATCH", 100, expected, extracted, "exact_id")
    score = fuzz.ratio(a, b)
    note = "Near match - possible OCR/reading error." if score >= 80 else None
    return _result("MISMATCH", score, expected, extracted, "exact_id", note)


def _cmp_number(spec, expected, extracted, percentage=False):
    method = "numeric_percentage" if percentage else "numeric"
    e, x = to_number(expected), to_number(extracted)
    if x is None:
        return _result("NOT_FOUND", 0, expected, extracted, method)
    if e is None:
        return _result("MISMATCH", 0, expected, extracted, method, "Expected value is not a number.")
    tol = float(spec.get("tolerance", 0.5 if percentage else 0))
    decay = float(spec.get("decay") or (10 if percentage else max(1.0, abs(e) * 0.1)))
    diff = abs(e - x)
    if diff <= tol + 1e-9:
        return _result("MATCH", 100, e, x, method)
    score = max(0.0, 100 * (1 - (diff - tol) / decay))
    return _result("MISMATCH", score, e, x, method, f"Difference {round(diff, 4)} exceeds tolerance {tol}.")


def _cmp_date(spec, expected, extracted):
    e, x = parse_date(expected), parse_date(extracted)
    if _empty(extracted):
        return _result("NOT_FOUND", 0, expected, extracted, "date")
    if e is None or x is None:
        same = _norm_id(expected) == _norm_id(extracted)
        return _result("MATCH" if same else "MISMATCH", 100 if same else 0, expected, extracted, "date",
                       "Could not parse one of the dates; compared as text.")
    return _result("MATCH" if e == x else "MISMATCH", 100 if e == x else 0,
                   e.isoformat(), x.isoformat(), "date")


def compare_field(spec: dict, expected: Any, extracted: Any) -> dict:
    ftype = spec["type"]
    if ftype != "table" and _empty(extracted):
        return _result("NOT_FOUND", 0, expected, extracted, ftype)
    if ftype == "name":
        return _cmp_name_or_text(spec, expected, extracted, True)
    if ftype == "text":
        return _cmp_name_or_text(spec, expected, extracted, False)
    if ftype == "id":
        return _cmp_id(spec, expected, extracted)
    if ftype == "number":
        return _cmp_number(spec, expected, extracted)
    if ftype == "percentage":
        return _cmp_number(spec, expected, extracted, percentage=True)
    if ftype == "date":
        return _cmp_date(spec, expected, extracted)
    return _result("NOT_FOUND", 0, expected, extracted, ftype, "Field type is not matchable.")

"""Optional per-document-type fix-ups, selected by "postprocess" in the type config.

A hook receives (values, warnings) and may modify `values` in place. Board-agnostic.

Design rule: small vision models read individual table rows well but readily invent
grand totals / percentages / maximums that are not on the document. So subject rows are
the source of truth, and totals are CALCULATED from them whenever possible.
"""
import re
from typing import Callable, Dict, List

from backend.services.matcher import NULL_WORDS, parse_date, to_number

# Board-specific fixed per-subject maximum, keyed by a regex on the board name.
# CBSE class 10/12 scholastic subjects are always marked out of 100.
FIXED_SUBJECT_MAX = [(re.compile(r"cbse|central board", re.I), 100.0)]

FAIL_GRADES = {"E", "E1", "E2", "F", "FAIL", "AB", "ABSENT"}
COMPONENT_KEYS =("theory_marks", "internal_marks", "practical_marks")
LEGACY_KEYS = {"ia_marks": "internal_marks", "external_marks": "theory_marks"}


def _clean_subject(s: dict, warnings: List[str]) -> dict:
    s = {LEGACY_KEYS.get(k, k): v for k, v in s.items()}
    code, grade = s.get("subject_code"), s.get("printed_grade")
    row = {
        "subject_name": str(s["subject_name"]).strip(),
        "subject_code": None if code is None or str(code).strip().lower() in NULL_WORDS else str(code).strip(),
        "theory_marks": to_number(s.get("theory_marks")),
        "internal_marks": to_number(s.get("internal_marks")),
        "practical_marks": to_number(s.get("practical_marks")),
        "obtained_marks": to_number(s.get("obtained_marks")),
        "maximum_marks": to_number(s.get("maximum_marks")),
        "printed_grade": None if grade is None or str(grade).strip().lower() in NULL_WORDS else str(grade).strip(),
    }
    name = row["subject_name"]

    # A single "IA/PR" column often gets copied into both internal and practical.
    if row["practical_marks"] is not None and row["practical_marks"] == row["internal_marks"]:
        row["practical_marks"] = None
    # ...and the row TOTAL is sometimes echoed into a component column as well.
    if (row["obtained_marks"] is not None and row["practical_marks"] == row["obtained_marks"]
            and (row["theory_marks"] is not None or row["internal_marks"] is not None)):
        row["practical_marks"] = None

    # Empty cells of grade-only rows (e.g. Work Experience: grade A1, no marks) often come back
    # as 0. All-zero marks with a passing grade is impossible, so those are really "no marks".
    mark_vals = [row[k] for k in (*COMPONENT_KEYS, "obtained_marks")]
    if (any(v is not None for v in mark_vals) and all(v in (None, 0) for v in mark_vals)
            and row["printed_grade"] and row["printed_grade"].upper() not in FAIL_GRADES):
        for k in (*COMPONENT_KEYS, "obtained_marks"):
            row[k] = None
        row["maximum_marks"] = None

    row["is_grade_only"] = all(row[k] is None for k in (*COMPONENT_KEYS, "obtained_marks"))

    # obtained > maximum: the maximum is the value that was misread (often a copy of a mark)
    if (row["obtained_marks"] is not None and row["maximum_marks"] is not None
            and row["obtained_marks"] > row["maximum_marks"]):
        row["maximum_marks"] = None

    return row


def _rows_verdict(marked: List[dict]):
    """Can the subject rows be cross-checked against themselves?

    True  - some rows have >=2 components and every such row adds up to its total (verified)
    False - at least one such row does not add up (rows were misread)
    None  - no row has enough components to check
    """
    checkable = [r for r in marked if sum(r[k] is not None for k in COMPONENT_KEYS) >= 2]
    if not checkable:
        return None
    return all(r["obtained_marks"] is not None
               and abs(sum(r[k] for k in COMPONENT_KEYS if r[k] is not None) - r["obtained_marks"]) <= 1
               for r in checkable)


def _subset_explains(marked: List[dict], p_obt: float, p_max: float) -> bool:
    """True if the printed total equals the sum of a MAJORITY of the rows (at least 2, more than half).

    An official aggregate only leaves out a minority of subjects (additional / optional ones).
    Requiring a majority stops an invented total from being 'explained' by a single coincidental row
    (e.g. a model copying the last row's 80/100 as the grand total).
    """
    pairs = [(r["obtained_marks"], r["maximum_marks"]) for r in marked]
    if not pairs or len(pairs) > 14 or any(o is None or m is None for o, m in pairs):
        return False
    n = len(pairs)
    for mask in range(1, (1 << n) - 1):
        if bin(mask).count("1") < 2 or bin(mask).count("1") * 2 <= n:
            continue
        o = m = 0.0
        for i in range(n):
            if mask >> i & 1:
                o += pairs[i][0]
                m += pairs[i][1]
        if abs(o - p_obt) <= 0.5 and abs(m - p_max) <= 0.5:
            return True
    return False


def _check_dob(values: dict, warnings: List[str]) -> None:
    """Reject a 'date of birth' that cannot be one (e.g. the date under the photo)."""
    dob = parse_date(values.get("date_of_birth"))
    if values.get("date_of_birth") is None:
        return
    ey = to_number(str(values.get("exam_year") or "")[:4])
    if dob is None:
        return
    lo, hi = (ey - 80, ey - 8) if ey else (1930, 2020)  # catches photo/issue dates, tolerates mature candidates
    if not lo <= dob.year <= hi:
        warnings.append(f"Date of birth '{values['date_of_birth']}' ignored: not plausible for "
                        f"an exam in {int(ey) if ey else 'this year'} (likely a photo/issue date).")
        values["date_of_birth"] = None


def marksheet(values: dict, warnings: List[str]) -> None:
    """Any board / university marksheet: marks-based, grade-based (CGPA/SGPA) or mixed."""
    raw_subjects = values.get("subjects")
    rows = [_clean_subject(s, warnings) for s in (raw_subjects if isinstance(raw_subjects, list) else [])
            if isinstance(s, dict) and s.get("subject_name")]

    # The same "subject code" on every row is not a subject code (e.g. the enrolment number).
    if len(rows) > 1 and len({r["subject_code"] for r in rows}) == 1:
        for r in rows:
            r["subject_code"] = None

    board = str(values.get("board_or_university") or "")
    fixed_max = next((m for rx, m in FIXED_SUBJECT_MAX if rx.search(board)), None)
    marked = [r for r in rows if not r["is_grade_only"]]
    if fixed_max is not None:
        for r in marked:
            r["maximum_marks"] = fixed_max
    if "subjects" in values:
        values["subjects"] = rows or None

    sum_obt = (sum(r["obtained_marks"] for r in marked)
               if marked and all(r["obtained_marks"] is not None for r in marked) else None)
    sum_max = (sum(r["maximum_marks"] for r in marked)
               if marked and all(r["maximum_marks"] is not None for r in marked) else None)

    p_obt, p_max, p_pct = (values.get("total_marks_obtained"), values.get("total_marks_maximum"),
                           values.get("percentage"))

    # Which source to trust for the grand total?
    #  - Subject rows that are self-consistent (components add up to each row total) are verified,
    #    so they win over a "printed" total, which small models readily invent (e.g. CBSE prints none).
    #  - If the rows are NOT self-consistent they were misread (e.g. the MAXIMUM column group taken
    #    as marks obtained), so a printed grand total (MP Board prints one) is the better source.
    verdict = _rows_verdict(marked)  # True = verified, False = contradicted, None = cannot be checked
    row_max = max((r["obtained_marks"] for r in marked if r["obtained_marks"] is not None), default=0)
    printed_sane = (p_obt is not None and p_obt >= row_max and (p_max is None or p_obt <= p_max))
    # A printed aggregate that omits some subjects (additional / optional ones, e.g. MP Board '#')
    # is legitimate when some subset of the rows adds up to exactly the printed obtained AND maximum.
    explained = (sum_obt is not None and p_obt is not None and p_max is not None
                 and abs(p_obt - sum_obt) > 0.5 and _subset_explains(marked, p_obt, p_max))
    if explained:
        warnings.append("The printed grand total leaves out some subject(s) (additional/optional); "
                        "the printed total is used.")
    if sum_obt is not None and not explained and (verdict is True or not printed_sane):
        obt = sum_obt
        mx = sum_max if sum_max is not None else p_max
        if p_obt is not None and abs(p_obt - sum_obt) > 0.5:
            warnings.append(f"Grand total read from the document ({p_obt:g}) differs from the sum of the "
                            f"subject rows ({sum_obt:g}); the sum is used.")
        if verdict is not True:
            warnings.append("Subject rows could not be verified (no component marks to cross-check); "
                            "verify the totals manually.")
    else:
        obt, mx = p_obt, (p_max if p_max is not None else sum_max)
        if marked and obt is not None and not explained:
            warnings.append("Subject rows " + ("look misread (component marks do not add up to the row totals)"
                            if verdict is False else "could not be verified (no component marks to cross-check)")
                            + "; the grand total printed on the document is used instead.")
        if obt is not None and mx is not None and obt > mx:
            warnings.append(f"Grand total {obt:g} exceeds maximum {mx:g}; totals ignored.")
            obt = mx = None
    if obt is not None:
        values["total_marks_obtained"] = obt
    if mx is not None:
        values["total_marks_maximum"] = mx

    computed = round(obt / mx * 100, 2) if obt is not None and mx else None
    if computed is not None:
        if p_pct is not None and abs(p_pct - computed) > 0.5:
            warnings.append(f"Percentage read from the document ({p_pct:g}) differs from {computed:g} calculated "
                            f"from the marks; the calculated value is used.")
        values["percentage"] = computed
        values["percentage_source"] = f"calculated: {obt:g}/{mx:g}"
    elif p_pct is not None:
        values["percentage_source"] = "read from document (not verifiable from subject rows)"
    else:
        values["percentage_source"] = None
        if values.get("cgpa") is not None or values.get("sgpa") is not None:
            warnings.append("Document is grade-point based and prints no percentage or marks total; "
                            "percentage could not be derived.")

    _check_dob(values, warnings)


def _clean_section_scores(values: dict) -> None:
    """Normalise section_scores and drop rows that cannot be a real per-section score."""
    if "section_scores" not in values:  # not asked of the model
        return
    rows = values.get("section_scores")
    if not isinstance(rows, list):
        values["section_scores"] = None
        return
    paper_names = {str(values.get(k)).strip().lower() for k in ("test_paper", "exam_name") if values.get(k)}
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("section"):
            continue
        row = {"section": str(r["section"]).strip(), "score": to_number(r.get("score")),
               "max_score": to_number(r.get("max_score")), "percentile": to_number(r.get("percentile"))}
        if row["section"].lower() in paper_names:  # the paper itself is not a section of itself
            continue
        if row["score"] is not None and row["max_score"] is not None and row["score"] > row["max_score"]:
            continue
        if all(row[k] is None for k in ("score", "max_score", "percentile")):
            continue
        out.append(row)
    values["section_scores"] = out or None


def scorecard(values: dict, warnings: List[str]) -> None:
    """Entrance / competitive-exam scorecards (JEE, NEET, GATE, CAT, CUET ...)."""
    score, mx = values.get("score"), values.get("max_score")
    marks, mmax = values.get("marks_obtained"), values.get("max_marks")

    # A maximum below the score is contradictory (e.g. GATE: model copies "out of 100" into max_score
    # although the GATE score is out of 1000 and its maximum is not printed). Drop it.
    if score is not None and mx is not None and score > mx:
        warnings.append(f"max_score {mx:g} ignored: lower than the score {score:g}.")
        values["max_score"] = mx = None

    _clean_section_scores(values)

    # A per-category cut-off table (General / OBC / SC...) says nothing about THIS candidate's category,
    # and "qualified" is not printed - only implied by marks >= cut-off. Models infer both; discard.
    if any(values.get(k) is not None for k in ("qualifying_marks_general", "qualifying_marks_ews_obc",
                                               "qualifying_marks_sc_st_pwd")):
        dropped = [k for k in ("category", "qualifying_status") if values.get(k) is not None]
        for k in dropped:
            values[k] = None
        if dropped:
            warnings.append(f"{' and '.join(dropped)} ignored: the card only lists cut-off marks per category "
                            f"and does not state them for the candidate.")

    # Only one number printed with a maximum (e.g. NEET 620/720): the score IS the marks.
    if marks is None and score is not None and mx is not None and mmax is None:
        marks, mmax = score, mx
        values["marks_obtained"], values["max_marks"] = marks, mmax
    # ...but never let the same number stand in for both when two different ones are printed.
    if marks is not None and mmax is not None and marks > mmax:
        warnings.append(f"Marks {marks:g} exceed their maximum {mmax:g}; check the reading.")
    # Percentage is never invented: derived only from raw marks / their maximum.
    if values.get("percentage") is None and marks is not None and mmax and marks <= mmax:
        values["percentage"] = round(marks / mmax * 100, 2)
        values["percentage_source"] = f"calculated: {marks:g}/{mmax:g}"
    elif values.get("percentage") is not None:
        values["percentage_source"] = "read from document"
    pct = values.get("percentile")
    if pct is not None and not 0 <= pct <= 100:
        warnings.append(f"Percentile {pct:g} is outside 0-100; check the reading.")
    _check_dob(values, warnings)


HOOKS: Dict[str, Callable[[dict, List[str]], None]] = {"marksheet": marksheet, "scorecard": scorecard}


def run(name: str, values: dict, warnings: List[str]) -> None:
    hook = HOOKS.get(name or "")
    if hook:
        hook(values, warnings)

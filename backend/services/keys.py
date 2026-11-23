"""Understanding the keys a caller sends: normalising them and, for keys we know nothing about,
inferring how their value should be compared (name / id / number / percentage / date / text)."""
import re
from typing import Any

from backend.services.matcher import parse_date, to_number


def normalize_key(raw: Any) -> str:
    """"Father's Name" -> "fathers_name", "Roll No." -> "roll_no"."""
    s = str(raw).strip().lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def humanize(raw: Any) -> str:
    """"policy_no" -> "policy no" (for the prompt)."""
    return re.sub(r"[_\s]+", " ", str(raw).strip()) or str(raw)


PERSON_WORDS = {"applicant", "candidate", "student", "father", "mother", "guardian", "parent", "holder", "patient",
                "husband", "wife", "spouse", "person", "full", "first", "last", "middle", "sur", "nominee",
                "owner", "employee", "member", "customer", "beneficiary", "fathers", "mothers", "parents"}
ID_SUFFIXES = {"number", "no", "num", "id", "code", "uid", "ref", "reference"}          # "policy_no", "roll_number"
ID_WORDS = {"roll", "aadhaar", "aadhar", "passport", "licence", "license", "serial"}    # identifiers on their own
ID_KEYS = {"registration", "enrollment", "enrolment", "pan", "aadhaar", "aadhar", "uan", "gstin"}
DATE_WORDS = {"date", "dob", "birth", "birthday", "expiry", "expires", "valid", "validity", "issued", "doi"}
NUMBER_WORDS = {"marks", "score", "rank", "income", "amount", "total", "cgpa", "gpa", "sgpa", "cpi", "spi", "age",
                "count", "candidates", "qualifying", "percentile", "salary", "fee", "fees", "price", "balance",
                "rate", "weight", "height", "years", "maximum", "minimum", "max", "min", "credits"}


def infer_type(key: str, value: Any) -> str:
    """Pick the comparison type for a key no profile knows. `key` must already be normalised."""
    tokens = set(key.split("_"))
    if "percent" in key or "percentage" in tokens or key.endswith("pct"):
        return "percentage"
    if tokens & DATE_WORDS or (isinstance(value, str) and parse_date(value) is not None):
        return "date"
    if key.split("_")[-1] in ID_SUFFIXES or tokens & ID_WORDS or key in ID_KEYS:
        return "id"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, str) and re.fullmatch(r"-?[\d,]+(\.\d+)?\s*%?", value.strip()):
        return "percentage" if value.strip().endswith("%") else "number"   # "12,000" / "81%" read as numbers
    if tokens & NUMBER_WORDS and to_number(value) is not None:
        return "number"
    if "name" in tokens and (tokens == {"name"} or tokens & PERSON_WORDS):
        return "name"
    return "text"

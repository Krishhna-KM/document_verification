"""Turns the caller's key/value JSON into a plan: which profile (if any) applies, which fields the model must
read, and how each key is to be compared.

There is no "document type" in the API. Instead, the keys themselves are used to pick an internal *profile*
from document_types.json (marksheet rules, scorecard rules, ...). A profile only supplies prompt instructions,
known field meanings/aliases and post-processing (e.g. computing a percentage from subject marks); keys that no
profile knows are still searched for and compared, with the comparison type inferred from the key and value.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.errors import RequestError
from backend.services.keys import humanize, infer_type, normalize_key
from backend.services.registry import FIELD_TYPES, NUMERIC_OPTIONS, registry

MAX_KEYS = 50
MAX_KEY_CHARS = 64
MAX_VALUE_CHARS = 500
ALLOWED_OPTIONS = {*NUMERIC_OPTIONS, "contains", "type"}

# Present on almost every kind of document, so they say nothing about which kind this is.
IDENTITY_FIELDS = {"applicant_name", "father_name", "mother_name", "date_of_birth"}
MIN_VOTE = 2   # at least one exact key of a profile (or two aliases) is needed to pick it


@dataclass
class Check:
    key: str            # exactly as the caller wrote it
    field: str          # the internal field it is read into / compared with
    expected: Any       # the caller's value
    spec: dict          # comparison rules (type, tolerance, threshold, weight ...)


@dataclass
class Plan:
    profile_key: Optional[str]
    profile: dict
    fields: Dict[str, dict]            # every field the model may be asked for (known + dynamic)
    checks: List[Check]
    skipped: List[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------ validation
def _check_options(key: str, options: dict) -> dict:
    """Validate and coerce the per-key matching options sent as {"value": ..., "tolerance": ...}."""
    unknown = sorted(set(options) - ALLOWED_OPTIONS)
    if unknown:
        raise RequestError(f"Key '{key}': unknown option(s) {unknown}. Allowed: {sorted(ALLOWED_OPTIONS)}.")
    out = dict(options)
    for opt in NUMERIC_OPTIONS:
        if opt in out:
            try:
                out[opt] = float(out[opt])
            except (TypeError, ValueError):
                raise RequestError(f"Key '{key}': option '{opt}' must be a number.")
            if out[opt] < 0:
                raise RequestError(f"Key '{key}': option '{opt}' must not be negative.")
    if "contains" in out and not isinstance(out["contains"], bool):
        raise RequestError(f"Key '{key}': option 'contains' must be true or false.")
    if "type" in out and (out["type"] not in FIELD_TYPES or out["type"] == "table"):
        raise RequestError(f"Key '{key}': option 'type' must be one of {sorted(FIELD_TYPES - {'table'})}.")
    return out


def _parse_entries(metadata: Any):
    """-> ([(user_key, normalised_key, value, options)], [keys skipped because their value is empty])."""
    if not isinstance(metadata, dict) or not metadata:
        raise RequestError("The JSON must be a non-empty object of key/value pairs, e.g. {\"name\": \"Asha Verma\"}.")
    if len(metadata) > MAX_KEYS:
        raise RequestError(f"The JSON has too many keys ({len(metadata)}); the maximum is {MAX_KEYS}.")

    entries, skipped, seen = [], [], {}
    for raw_key, raw_val in metadata.items():
        user_key = str(raw_key)
        key = normalize_key(user_key)
        if not key or len(user_key) > MAX_KEY_CHARS:
            raise RequestError(f"Key {user_key[:40]!r} is invalid: keys must be 1-{MAX_KEY_CHARS} characters "
                               f"and contain letters or digits.")
        if key in seen:
            raise RequestError(f"Keys '{seen[key]}' and '{user_key}' are the same key.")
        seen[key] = user_key

        options = {}
        if isinstance(raw_val, dict):
            options = _check_options(user_key, {k: v for k, v in raw_val.items() if k != "value"})
            raw_val = raw_val.get("value")
        if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
            skipped.append(user_key)  # nothing to compare against
            continue
        if isinstance(raw_val, (dict, list, tuple, bool)):
            raise RequestError(f"The value of '{user_key}' must be a string or a number.")
        if len(str(raw_val)) > MAX_VALUE_CHARS:
            raise RequestError(f"The value of '{user_key}' is too long (maximum {MAX_VALUE_CHARS} characters).")
        entries.append((user_key, key, raw_val, options))
    if not entries:
        raise RequestError("Every key in the JSON has an empty value, so there is nothing to verify.")
    return entries, skipped


# --------------------------------------------------------------------------------- profile choice
def _resolve_in(profile: dict, key: str) -> Optional[Tuple[str, str]]:
    """-> (field, "exact"|"alias") if `key` is one of the profile's fields or aliases."""
    if key in profile["fields"]:
        return key, "exact"
    for fname, spec in profile["fields"].items():
        if key in {a.lower() for a in spec.get("aliases", [])}:
            return fname, "alias"
    return None


def detect_profile(keys: Iterable[str]) -> Optional[str]:
    """Pick the internal profile whose fields the keys resemble most; None if no profile fits."""
    keys = list(keys)
    profiles = [(pk, cfg) for pk, cfg in registry.all().items() if not cfg.get("allow_dynamic_fields")]
    hits = {pk: [_resolve_in(cfg, k) for k in keys] for pk, cfg in profiles}
    spread = [sum(1 for pk, _ in profiles if hits[pk][i]) for i in range(len(keys))]  # profiles knowing key i

    best, best_score = None, (0, 0)
    for pk, _ in profiles:  # config order breaks ties
        vote = covered = 0
        for i, hit in enumerate(hits[pk]):
            if not hit:
                continue
            covered += 1
            fname, kind = hit
            if fname in IDENTITY_FIELDS:
                continue
            unique = spread[i] == 1
            vote += (3 if unique else 2) if kind == "exact" else (2 if unique else 1)
        if (vote, covered) > best_score:
            best, best_score = pk, (vote, covered)
    return best if best_score[0] >= MIN_VOTE else None


# ------------------------------------------------------------------------------------------ plan
def build_plan(metadata: Any) -> Plan:
    entries, skipped = _parse_entries(metadata)
    profile_key = detect_profile(key for _, key, _, _ in entries)
    profile = registry.get(profile_key) if profile_key else None
    base = profile or registry.get("generic") or {"fields": {}}

    fields = {k: dict(v) for k, v in base["fields"].items()}   # known fields (also supplies `needs`)
    checks: List[Check] = []
    for user_key, key, value, options in entries:
        hit = _resolve_in(base, key)
        if hit:
            fname = hit[0]
            spec = {**fields[fname], **options}
            if spec["type"] == "table":
                raise RequestError(f"'{user_key}' cannot be used for matching.")
        else:  # a key no profile knows: still searched for, compared by an inferred type
            fname = key
            spec = {"type": options.get("type") or infer_type(key, value),
                    "description": f"The value of '{humanize(user_key)}' as printed on the document", **options}
            fields[fname] = {k: v for k, v in spec.items() if k not in NUMERIC_OPTIONS and k != "contains"}
        checks.append(Check(user_key, fname, value, spec))
    return Plan(profile_key, profile or {}, fields, checks, skipped)

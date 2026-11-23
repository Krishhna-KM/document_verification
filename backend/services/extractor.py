"""Builds the extraction prompt, calls the model, parses + normalises its JSON answer."""
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.config import settings
from backend.errors import ExtractionError, ServerConfigError
from backend.services.matcher import NULL_WORDS, to_number
from backend.services.model_client import call_model

TYPE_HINTS = {
    "name": "name",
    "text": "text",
    "id": "string, exactly as printed",
    "number": "number",
    "percentage": "number, no % sign",
    "date": "date as printed",
    "table": "array",
}
RETRY_RESERVE = 200  # room for the "your last answer was not valid JSON" hint


def _render(profile: dict, fields: Dict[str, dict]) -> str:
    """`profile` may be empty: then only the generic rules apply."""
    lines = "\n".join(f'- {k} ({TYPE_HINTS[s["type"]]}): {s.get("description", "")}'.rstrip(": ")
                      for k, s in fields.items())
    instr = profile.get("instructions", "")
    return f"""Extract data from the attached document image. For each field below, find the value printed on the document that corresponds to it.

FIELDS (key (type): meaning):
{lines}

RULES:
{instr}
- Only values visibly printed; null if absent or illegible. Never guess.
- Numbers are plain JSON numbers (no commas, no leading zeros). confidence is 0-100 per value.
Reply with ONE JSON object, no markdown, in exactly this shape:
{{"fields":{{"<key>":{{"value":<value or null>,"confidence":<0-100>}}}},"notes":[]}}
Include every key listed under FIELDS."""


def scope_fields(fields: Dict[str, dict], requested: Iterable[str]) -> Dict[str, dict]:
    """The requested fields plus whatever their post-processing needs (a field's `needs` list)."""
    wanted = set(requested)
    for key in list(wanted):
        wanted |= {n for n in fields.get(key, {}).get("needs", []) if n in fields}
    return {k: v for k, v in fields.items() if k in wanted}


def build_prompt(profile: dict, fields: Dict[str, dict], requested: Iterable[str] = (),
                 max_chars: int = 0) -> Tuple[str, Dict[str, dict]]:
    """Returns (prompt, fields_asked_of_the_model).

    Only the requested fields (+ those they depend on) are asked for: small vision models skip or
    misplace values when given a long field list.
    """
    asked = scope_fields(fields, requested)
    prompt = _render(profile, asked)
    if max_chars and len(prompt) > max_chars:
        raise ServerConfigError(
            f"The extraction prompt is {len(prompt)} characters, over the {max_chars}-character limit.",
            details="Send fewer keys per request, shorten the profile instructions in document_types.json, "
                    "or raise MODEL_PROMPT_MAX_CHARS.")
    return prompt, asked


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in model response")
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(text[start:])
    except json.JSONDecodeError:
        # models sometimes emit numbers with leading zeros (058) - invalid JSON
        fixed = re.sub(r"(?<=[:\[,\s])0+(\d+)(?=\s*[,}\]])", r"\1", text[start:])
        obj, _ = dec.raw_decode(fixed)
    if not isinstance(obj, dict):
        raise ValueError("model JSON is not an object")
    return obj


def _normalise(spec: dict, value: Any) -> Any:
    if value is None:
        return None
    t = spec["type"]
    if t in ("number", "percentage"):
        return to_number(value)
    if t == "table":
        return value if isinstance(value, list) else None
    s = str(value).strip()
    return None if s.lower() in NULL_WORDS else s


def extract(profile: dict, fields: Dict[str, dict], images: List[str],
            mock_hint: Optional[dict] = None, raw_document: Optional[bytes] = None,
            requested: Iterable[str] = ()) -> Tuple[dict, dict, List[str], str]:
    """Returns (values, confidences, model_notes, raw_text). `values` holds only the fields asked of the model."""
    limit = settings.MODEL_PROMPT_MAX_CHARS
    # keep room for the retry hint; a limit of 0 means "no limit", so never let the result fall to 0
    base_prompt, asked = build_prompt(profile, fields, requested, max(1, limit - RETRY_RESERVE) if limit else 0)
    prompt, raw, err = base_prompt, "", None
    for _ in range(2):
        raw = call_model(prompt, images, mock_hint, raw_document)
        try:
            payload = parse_json(raw)
            # a tolerated flat answer ({"key": value}) still has to contain at least one requested key
            got = payload["fields"] if isinstance(payload.get("fields"), dict) else payload
            if not any(k in got for k in asked):
                raise ValueError("the answer contains none of the requested fields")
            break
        except (ValueError, json.JSONDecodeError) as e:
            err = e
            prompt = base_prompt + f"\nYour previous answer was unusable ({e}). Return the JSON object in the exact shape above."
    else:
        raise ExtractionError("The model did not return a usable answer for this document.",
                              details=f"{err}; raw answer: {raw[:300]!r}")

    values, conf = {}, {}
    for key, spec in asked.items():
        item = got.get(key)
        if isinstance(item, dict) and "value" in item:
            val, c = item["value"], item.get("confidence")
        else:  # tolerate a flat {"key": value} answer
            val, c = item, None
        values[key] = _normalise(spec, val)
        c = to_number(c)
        conf[key] = None if c is None or values[key] is None else max(0.0, min(100.0, c))
    notes = [str(n) for n in payload.get("notes", []) if n] if isinstance(payload.get("notes"), list) else []
    return values, conf, notes, raw

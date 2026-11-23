"""Orchestrates one verification: key/value JSON + document -> read the keys from the document -> compare."""
import logging
import time
import uuid
from typing import Any, Dict, Optional

from backend.config import settings
from backend.services import extractor, postprocess
from backend.services.document_loader import load_document
from backend.services.matcher import compare_field
from backend.services.resolver import build_plan

log = logging.getLogger(__name__)

FULLY, PARTIALLY, NOT = "FULLY_MATCHED", "PARTIALLY_MATCHED", "NOT_MATCHED"


def classify(res: dict) -> str:
    """Map the matcher's verdict onto the three statuses the API reports.

    FULLY_MATCHED     the value matches the document (within the field's tolerance / threshold)
    PARTIALLY_MATCHED close but not a match: e.g. one name part differs, an id is a near miss, a number is a
                      little outside tolerance (score >= PARTIAL_MATCH_MIN_SCORE)
    NOT_MATCHED       different, or not found on the document at all
    """
    if res["status"] == "MATCH":
        return FULLY
    if res["status"] == "MISMATCH" and res["match_score"] >= settings.PARTIAL_MATCH_MIN_SCORE:
        return PARTIALLY
    return NOT


def verify_document(raw: bytes, metadata: Any, debug: bool = False, request_id: Optional[str] = None) -> dict:
    started = time.time()
    request_id = request_id or uuid.uuid4().hex[:12]

    plan = build_plan(metadata)            # validates the JSON (RequestError) and decides how to compare each key
    images = load_document(raw)            # validates the document (DocumentError)

    mock = settings.MODEL_PROVIDER == "mock"
    values, conf, model_notes, raw_text = extractor.extract(
        plan.profile, plan.fields, images, raw_document=raw, requested={c.field for c in plan.checks},
        mock_hint={c.field: c.expected for c in plan.checks} if mock else None)

    warnings = []
    if mock:
        warnings.append("MOCK MODE: results are NOT from a real model.")
    if plan.skipped:
        warnings.append(f"Skipped keys with an empty value: {', '.join(plan.skipped)}.")

    # A bug or odd input in post-processing must not lose the whole verification: keep the raw values.
    try:
        postprocess.run(plan.profile.get("postprocess"), values, warnings)
    except Exception:
        log.exception("request=%s post-processing failed (profile=%s)", request_id, plan.profile_key)
        warnings.append("Post-processing failed, so derived values (totals / percentage) may be missing or unverified.")
    for key in conf:  # a value discarded by post-processing keeps no confidence
        if values.get(key) is None:
            conf[key] = None

    results, details, weighted, total_w = [], [], 0.0, 0.0
    counts = {FULLY: 0, PARTIALLY: 0, NOT: 0}
    for chk in plan.checks:
        try:
            res = compare_field(chk.spec, chk.expected, values.get(chk.field))
        except Exception:  # one unmatchable key must not fail the others
            log.exception("request=%s comparison failed for key=%s", request_id, chk.key)
            res = {"status": "ERROR", "match_score": 0.0, "extracted": values.get(chk.field),
                   "method": chk.spec.get("type"), "note": "This key could not be compared."}
        found = res["status"] not in ("NOT_FOUND", "ERROR")
        if res["status"] == "NOT_FOUND" and not res.get("note"):
            res["note"] = "Not found on the document (it may carry this value under a different label)."
        status = classify(res)
        counts[status] += 1

        # The API response carries only these five values per key ...
        results.append({
            "key": chk.key,
            "value": chk.expected,
            "doc_value": res.get("extracted"),
            "match_score": res["match_score"],
            "status": status,
        })
        # ... everything else about the key is available with debug=true, in the same order.
        details.append({
            "key": chk.key,
            "field": chk.field,                 # the internal name the key was understood as
            "found": found,
            "extraction_confidence": conf.get(chk.field),
            "method": res.get("method"),
            "note": res.get("note"),
        })

        w = float(chk.spec.get("weight", 1))
        weighted += w * res["match_score"]
        total_w += w

    total = len(results)
    overall = FULLY if counts[FULLY] == total else NOT if counts[NOT] == total else PARTIALLY

    elapsed = int((time.time() - started) * 1000)
    log.info("request=%s profile=%s status=%s keys=%d pages=%d ms=%d",
             request_id, plan.profile_key, overall, total, len(images), elapsed)

    out: Dict[str, Any] = {"results": results}
    if debug:
        out["debug"] = {
            "request_id": request_id,
            "overall_status": overall,
            "overall_match_score": round(weighted / total_w, 1) if total_w else 0.0,
            "summary": {"total": total, "fully_matched": counts[FULLY], "partially_matched": counts[PARTIALLY],
                        "not_matched": counts[NOT]},
            "results": details,
            "detected_document_type": plan.profile_key,
            "extracted_data": values,
            "warnings": warnings,
            "model_notes": model_notes,
            "processing": {"model": settings.MODEL_NAME, "provider": settings.MODEL_PROVIDER,
                           "pages_processed": len(images), "time_ms": elapsed},
            "raw_model_output": raw_text,
        }
    return out

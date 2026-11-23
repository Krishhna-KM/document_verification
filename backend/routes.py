import base64
import binascii
import json
import logging

from flask import Blueprint, g, jsonify, request

from backend.config import settings
from backend.errors import RequestError
from backend.services.verifier import verify_document

api = Blueprint("api", __name__, url_prefix="/api")
log = logging.getLogger(__name__)

JSON_FILE_FIELDS = ("json_file", "json", "metadata_file")    # accepted names for the uploaded JSON file
DOCUMENT_FIELDS = ("file", "document")


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _parse_json_bytes(data: bytes, what: str):
    """Parse an uploaded JSON file / text, with an error message that says where it is wrong."""
    if len(data) > settings.MAX_JSON_KB * 1024:
        raise RequestError(f"The {what} is too large (maximum {settings.MAX_JSON_KB} KB).")
    try:
        text = data.decode("utf-8-sig")        # tolerates the BOM that Windows editors add
    except UnicodeDecodeError:
        raise RequestError(f"The {what} is not UTF-8 text.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RequestError(f"The {what} is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno}).")


def _first_file(names):
    for n in names:
        if request.files.get(n):
            return request.files[n]
    return None


def _read_request():
    """multipart/form-data: `file` (the document) + `json_file` (a .json file of key/value pairs),
    or a `metadata` form field holding that JSON as text.
    application/json: {"document_base64": "...", "metadata": {...}}."""
    if request.files:
        doc = _first_file(DOCUMENT_FIELDS)
        if not doc:
            raise RequestError("Missing the document: send it in a form field named 'file'.")
        raw = doc.read()
        json_file = _first_file(JSON_FILE_FIELDS)
        if json_file:
            metadata = _parse_json_bytes(json_file.read(), "JSON file")
        elif request.form.get("metadata"):
            metadata = _parse_json_bytes(request.form["metadata"].encode("utf-8"), "'metadata' field")
        else:
            raise RequestError("Missing the key/value JSON: send it as a file in a form field named 'json_file' "
                               "(or as text in a 'metadata' field).")
        debug = _truthy(request.form.get("debug", "false"))
    elif request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise RequestError("The request body is not a valid JSON object.")
        b64 = body.get("document_base64") or ""
        if not isinstance(b64, str):
            raise RequestError("'document_base64' must be a base64 string.")
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[-1]
        try:
            raw = base64.b64decode(b64, validate=True) if b64 else b""
        except (binascii.Error, ValueError):
            raise RequestError("'document_base64' is not valid base64.")
        metadata = body.get("metadata")
        if isinstance(metadata, str):
            metadata = _parse_json_bytes(metadata.encode("utf-8"), "'metadata' value")
        if metadata is None:
            raise RequestError("'metadata' (the object of key/value pairs) is required.")
        debug = _truthy(body.get("debug", False))
    elif request.mimetype == "multipart/form-data":
        raise RequestError("Missing the document: send it in a form field named 'file'.")
    else:
        raise RequestError("Send multipart/form-data (a 'file' and a 'json_file') or an application/json body.",
                           status=415, code="UNSUPPORTED_MEDIA_TYPE")

    if not raw:
        raise RequestError("No document supplied.")
    return raw, metadata, debug


@api.get("/health")
def health():
    problems = settings.problems()
    return jsonify(status="ok" if not problems else "degraded", provider=settings.MODEL_PROVIDER,
                   model=settings.MODEL_NAME, model_configured=not problems,
                   **({"problems": problems} if problems else {}))


@api.post("/verify")
def verify():
    raw, metadata, debug = _read_request()
    return jsonify(verify_document(raw, metadata, debug, request_id=g.request_id))

"""Client for the hosted vision model.

Providers (MODEL_PROVIDER): "ocr_api" (internal /v1/ocr multipart endpoint), "openai" (any OpenAI-compatible
/chat/completions endpoint) and "mock" (no model; for testing the plumbing). If your endpoint uses a different
request shape, this is the only file to change: keep `call_model(...) -> str` and return the raw answer text.

Failures are raised as specific ModelError subclasses; upstream response bodies go to `details` (logged, and only
returned to callers in DEBUG) so internals are not leaked to API users.
"""
import base64
import binascii
import json
import logging
import time
from typing import Callable, Dict, List, Optional

import requests

from backend.config import settings
from backend.errors import (ModelAuthError, ModelBadResponse, ModelRejected, ModelTimeout, ModelUnavailable,
                            ServerConfigError)

log = logging.getLogger(__name__)


def _mock(mock_hint: Optional[Dict]) -> str:
    """Echoes the request metadata back as if it had been read from the document."""
    fields = {k: {"value": v, "confidence": 50} for k, v in (mock_hint or {}).items()}
    return json.dumps({
        "fields": fields,
        "notes": ["MOCK PROVIDER: no model was called; values are echoed from the request metadata."],
    })


def _post_with_retry(send: Callable[[], requests.Response]) -> requests.Response:
    """Runs `send` (one HTTP POST). Retries once on connection errors / 5xx / 429.

    Timeouts are NOT retried (a retry would double an already long wait); 401/403 and other 4xx fail at once.
    """
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            r = send()
        except requests.Timeout as e:
            raise ModelTimeout(f"The model service did not answer within {settings.MODEL_TIMEOUT} seconds.",
                               details=str(e)) from e
        except requests.ConnectionError as e:
            last = ModelUnavailable("The model service could not be reached.", details=str(e))
        except requests.RequestException as e:
            raise ModelUnavailable("The request to the model service failed.", details=str(e)) from e
        else:
            code = r.status_code
            if code in (401, 403):
                raise ModelAuthError("The model service rejected the server's credentials "
                                     "(check MODEL_API_KEY in the server's .env).", details=r.text[:300])
            if code >= 500 or code == 429:
                last = ModelUnavailable(f"The model service is unavailable or busy (HTTP {code}).",
                                        details=r.text[:300])
            elif code >= 400:
                raise ModelRejected(f"The model service rejected the request (HTTP {code}).", details=r.text[:300])
            else:
                return r
        log.warning("model call attempt %d failed: %s", attempt + 1, last)
        if attempt == 0:
            time.sleep(1.5)
    raise last  # type: ignore[misc]


def _json(r: requests.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        raise ModelBadResponse("The model service did not return JSON.", details=r.text[:300])
    if not isinstance(body, dict):
        raise ModelBadResponse("The model service returned an unexpected response.", details=str(body)[:300])
    return body


def _call_ocr_api(prompt: str, images: List[str], raw: Optional[bytes]) -> str:
    """Internal /v1/ocr endpoint: multipart upload, answer comes back in the "text" key.

    PDFs are sent as-is (the server renders up to max_pages); images are sent as the
    cleaned/resized JPEG produced by document_loader.
    """
    if raw is not None and raw[:5] == b"%PDF-":
        file_part = ("document.pdf", raw, "application/pdf")
    else:
        try:
            file_part = ("document.jpg", base64.b64decode(images[0].split(",", 1)[1]), "image/jpeg")
        except (IndexError, binascii.Error) as e:
            raise ModelBadResponse("Internal error preparing the image for the model service.") from e

    data = {"model": settings.MODEL_NAME, "prompt": prompt,
            "max_pages": str(settings.MAX_PDF_PAGES), "max_tokens": str(settings.MODEL_MAX_TOKENS)}
    headers = {"Authorization": f"Bearer {settings.MODEL_API_KEY}"} if settings.MODEL_API_KEY else {}

    r = _post_with_retry(lambda: requests.post(settings.MODEL_API_URL, headers=headers, data=data,
                                               files={"file": file_part}, timeout=settings.MODEL_TIMEOUT))
    body = _json(r)
    text = body.get("text")
    if text is None and isinstance(body.get("pages"), list):
        text = "\n".join(str(p.get("text", "")) for p in body["pages"] if isinstance(p, dict))
    if not isinstance(text, str) or not text.strip():
        raise ModelBadResponse("The model service returned an empty answer.", details=str(body)[:300])
    return text


def _openai_endpoint() -> str:
    url = settings.MODEL_API_URL.rstrip("/")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def _call_openai(prompt: str, images: List[str]) -> str:
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    payload = {
        "model": settings.MODEL_NAME,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": settings.MODEL_MAX_TOKENS,
    }
    headers = {"Content-Type": "application/json"}
    if settings.MODEL_API_KEY:
        headers["Authorization"] = f"Bearer {settings.MODEL_API_KEY}"

    r = _post_with_retry(lambda: requests.post(_openai_endpoint(), json=payload, headers=headers,
                                               timeout=settings.MODEL_TIMEOUT))
    body = _json(r)
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ModelBadResponse("The model service returned an unexpected response.", details=str(body)[:300])
    if not isinstance(text, str) or not text.strip():
        raise ModelBadResponse("The model service returned an empty answer.", details=str(body)[:300])
    return text


def call_model(prompt: str, images: List[str], mock_hint: Optional[Dict] = None,
               raw: Optional[bytes] = None) -> str:
    provider = settings.MODEL_PROVIDER
    if provider == "mock":
        return _mock(mock_hint)
    if provider not in ("ocr_api", "openai"):
        raise ServerConfigError(f"Unknown MODEL_PROVIDER '{provider}' in the server configuration.")
    if not settings.MODEL_API_URL:
        raise ServerConfigError("MODEL_API_URL is not configured on the server.")
    return _call_ocr_api(prompt, images, raw) if provider == "ocr_api" else _call_openai(prompt, images)

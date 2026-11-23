import logging
import re
import uuid

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from backend.config import settings
from backend.errors import ApiError
from backend.routes import api

log = logging.getLogger("backend.app")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _error_response(status: int, code: str, message: str, details=None):
    body = {"error": message, "code": code, "request_id": getattr(g, "request_id", None)}
    if details and settings.DEBUG:  # internals (upstream bodies etc.) only in development
        body["details"] = details
    return jsonify(body), status


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = settings.MAX_UPLOAD_MB * 1024 * 1024
    app.json.sort_keys = False  # keep document types / fields in the order written in document_types.json
    app.register_blueprint(api)

    for problem in settings.problems():
        log.warning("configuration problem: %s", problem)
    log.info("provider=%s model=%s", settings.MODEL_PROVIDER, settings.MODEL_NAME)

    @app.before_request
    def assign_request_id():
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex[:12]

    @app.after_request
    def echo_request_id(response):
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        return response

    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        level = logging.ERROR if e.status >= 500 else logging.WARNING
        log.log(level, "request=%s %s %s: %s%s", getattr(g, "request_id", "-"), e.status, e.code, e,
                f" | {e.details}" if e.details else "")
        return _error_response(e.status, e.code, str(e), e.details)

    @app.errorhandler(HTTPException)
    def _http_error(e: HTTPException):
        if e.code == 413:
            return _error_response(413, "PAYLOAD_TOO_LARGE",
                                   f"The upload is too large. The maximum size is {settings.MAX_UPLOAD_MB} MB.")
        if e.code == 405:
            return _error_response(405, "METHOD_NOT_ALLOWED", "This HTTP method is not allowed for this URL.")
        if e.code == 404:
            return _error_response(404, "NOT_FOUND", "No such endpoint.")
        return _error_response(e.code or 500, f"HTTP_{e.code}", e.description or e.name)

    @app.errorhandler(Exception)
    def _unhandled(e: Exception):
        log.exception("request=%s unexpected error", getattr(g, "request_id", "-"))
        return _error_response(500, "INTERNAL_ERROR",
                               "Unexpected server error. Quote the request_id when reporting it.")

    # simple test frontend
    @app.get("/")
    def index():
        return send_from_directory(settings.FRONTEND_DIR, "index.html")

    return app

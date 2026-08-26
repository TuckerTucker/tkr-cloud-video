"""Request routing for the console, independent of any socket.

Routing is separated from the server so the whole API can be exercised as
function calls. The server below is then thin enough to be read in one sitting,
which matters for a process that holds a RunPod token and a delivery credential.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Final

from tkr_cloud_video.console.intake import form_constraints
from tkr_cloud_video.console.service import ConsoleService
from tkr_cloud_video.core.errors import AppError

MAX_BODY_BYTES: Final[int] = 262_144
JSON_CONTENT_TYPE: Final[str] = "application/json; charset=utf-8"

# Served by exact name. The console never maps a request path onto the
# filesystem, so there is no traversal to defend against.
STATIC_ASSETS: Final[dict[str, tuple[str, str]]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

IDENTIFIER_PATTERN: Final[str] = r"[A-Za-z0-9._-]{1,128}"
RUN_STATUS_PATH: Final[re.Pattern[str]] = re.compile(
    rf"^/api/runs/({IDENTIFIER_PATTERN})$"
)
RUN_CANCEL_PATH: Final[re.Pattern[str]] = re.compile(
    rf"^/api/runs/({IDENTIFIER_PATTERN})/cancel$"
)
WARMUP_STATUS_PATH: Final[re.Pattern[str]] = re.compile(
    rf"^/api/warmups/({IDENTIFIER_PATTERN})$"
)
JOB_RESULT_PATH: Final[re.Pattern[str]] = re.compile(
    rf"^/api/jobs/({IDENTIFIER_PATTERN})/result$"
)
JOB_LINK_PATH: Final[re.Pattern[str]] = re.compile(
    rf"^/api/jobs/({IDENTIFIER_PATTERN})/link$"
)


@dataclass(frozen=True, slots=True)
class Response:
    """One rendered response, with the headers it must be served under.

    Args:
        status: HTTP status.
        body: Already-encoded body.
        content_type: The exact content type to serve.
        headers: Any additional response headers.

    """

    status: int
    body: bytes
    content_type: str
    headers: Mapping[str, str] = field(default_factory=dict)


def _json(payload: Mapping[str, object], status: int = 200) -> Response:
    """Render one JSON response.

    The store is always `no-store`. Preflight echoes the wire text and a
    delivery response carries a signed URL, and neither may sit in a disk cache
    after the tab is closed.
    """
    return Response(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        content_type=JSON_CONTENT_TYPE,
    )


def _failure(code: str, message: str, status: int) -> Response:
    """Render one routing-level failure in the page's error shape."""
    return _json(
        {
            "ok": False,
            "error_code": code,
            "message": message,
            "error_context": None,
            "defects": [],
        },
        status,
    )


def content_security_policy(media_origin: str) -> str:
    """Return the policy the page is served under.

    The page holds a signed delivery URL for as long as it is open, so the
    policy is written to bound where that value could go: scripts and styles
    come only from this origin, the only cross-origin load permitted at all is
    the video itself from the delivery endpoint, and there is no origin the page
    may connect to, post a form to, or be framed by.
    """
    return "; ".join(
        (
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            f"media-src {media_origin}",
            "connect-src 'self'",
            "form-action 'none'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        )
    )


def read_static(name: str) -> bytes:
    """Read one packaged static asset by its exact name."""
    return (
        resources.files("tkr_cloud_video.console").joinpath("static", name).read_bytes()
    )


def _decode(body: bytes) -> object:
    """Decode a JSON request body, refusing anything oversized or malformed."""
    if len(body) > MAX_BODY_BYTES:
        raise AppError(
            "request_too_large",
            "The request body exceeds the console's ceiling.",
            context={"operation": "decode_body"},
        )
    try:
        return json.loads(body or b"{}")
    except json.JSONDecodeError as error:
        raise AppError(
            "invalid_envelope",
            "The request body is not a JSON document.",
            context={"operation": "decode_body"},
            cause=error,
        ) from error


def _requested_ttl(payload: object) -> int | None:
    """Return a caller-requested link lifetime, when one was supplied."""
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("ttl_seconds")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def dispatch(
    service: ConsoleService, method: str, path: str, body: bytes
) -> Response:
    """Route one request to the console service.

    Args:
        service: The composed console.
        method: The HTTP method.
        path: The request path, without its query string.
        body: The raw request body.

    Returns:
        The rendered response. An application error becomes an error document
        rather than a stack trace, because the page renders every failure the
        same way and a console that answered 500 with prose would be the one
        thing it could not display.

    """
    asset = STATIC_ASSETS.get(path)
    if asset is not None:
        if method != "GET":
            return _failure("method_not_allowed", "This path serves GET.", 405)
        name, content_type = asset
        return Response(200, read_static(name), content_type)
    try:
        return await _dispatch_api(service, method, path, body)
    except AppError as error:
        return _json(
            {
                "ok": False,
                "error_code": error.code,
                "message": str(error),
                "retryable": error.retryable,
                "error_context": dict(error.context) or None,
                "defects": [],
            },
            400,
        )


async def _dispatch_api(
    service: ConsoleService, method: str, path: str, body: bytes
) -> Response:
    """Route one API request, raising AppError for a refused body."""
    if path == "/api/constraints" and method == "GET":
        return _json(form_constraints())
    if path == "/api/scope" and method == "GET":
        return _json(service.scope())
    if path == "/api/history" and method == "GET":
        return _json(service.history())
    if path == "/api/endpoint-health" and method == "GET":
        return _json(await service.endpoint_health())
    if path == "/api/warmup" and method == "POST":
        return _json(await service.warmup())
    if path == "/api/preflight" and method == "POST":
        return _json(service.preflight(_decode(body)))
    if path == "/api/generate" and method == "POST":
        return _json(await service.generate(_decode(body)))

    matched = WARMUP_STATUS_PATH.match(path)
    if matched and method == "GET":
        return _json(await service.observe_warmup(matched.group(1)))

    matched = RUN_STATUS_PATH.match(path)
    if matched and method == "GET":
        return _json(await service.observe(matched.group(1)))
    matched = RUN_CANCEL_PATH.match(path)
    if matched and method == "POST":
        return _json(await service.cancel(matched.group(1)))
    matched = JOB_RESULT_PATH.match(path)
    if matched and method == "GET":
        return _json(await service.result(matched.group(1)))
    matched = JOB_LINK_PATH.match(path)
    if matched and method == "POST":
        return _json(
            await service.delivery_link(matched.group(1), _requested_ttl(_decode(body)))
        )
    return _failure("not_found", "No such console route.", 404)

"""The loopback socket shell around the console's routes.

The standard library's threading server is used deliberately. This process
holds a RunPod token and a delivery credential, so its dependency surface is
part of its security posture, and a local single-operator console does not need
a framework to answer a handful of routes. Each request runs its own event loop,
so nothing here may hold an asyncio primitive across requests.
"""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import urlparse

from tkr_cloud_video.console.routes import (
    MAX_BODY_BYTES,
    Response,
    content_security_policy,
    dispatch,
)
from tkr_cloud_video.console.service import ComposedConsole

SERVER_BANNER: Final[str] = "tkr-cloud-video-console"

# A browser resolves any name to any address, so a page on another origin could
# otherwise reach this server by pointing a hostname at 127.0.0.1. The console
# authenticates nothing, so the Host header is checked instead.
PERMITTED_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})


def _media_origin(endpoint: str) -> str:
    """Return the one cross-origin the page may load a video from."""
    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    """Serves the console's routes on one connection."""

    server_version = SERVER_BANNER
    sys_version = ""
    protocol_version = "HTTP/1.1"
    console: ComposedConsole

    def do_GET(self) -> None:
        """Serve one GET."""
        self._serve("GET")

    def do_POST(self) -> None:
        """Serve one POST."""
        self._serve("POST")

    def log_message(self, format: str, *args: object) -> None:
        """Discard the base class's request log.

        A request line carries the job id and, for a delivery route, would sit
        beside the response that issued a signed link. The console keeps no
        access log rather than keep one it would have to redact.
        """

    def _permitted_host(self) -> bool:
        """Report whether the Host header names this loopback server."""
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if ":" in host else host
        return name.strip("[]") in PERMITTED_HOSTS

    def _serve(self, method: str) -> None:
        """Read one request, route it, and write the response."""
        if not self._permitted_host():
            self._write(
                Response(
                    421,
                    b'{"ok":false,"error_code":"host_not_permitted",'
                    b'"message":"The console serves loopback only.","defects":[]}',
                    "application/json; charset=utf-8",
                )
            )
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._write(
                Response(
                    413,
                    b'{"ok":false,"error_code":"request_too_large",'
                    b'"message":"The request body is too large.","defects":[]}',
                    "application/json; charset=utf-8",
                )
            )
            return
        body = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        response = asyncio.run(dispatch(self.console.service, method, path, body))
        self._write(response)

    def _write(self, response: Response) -> None:
        """Write one response under the console's fixed security headers."""
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            content_security_policy(
                _media_origin(self.console.settings.b2_s3_endpoint)
            ),
        )
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)


def build_server(console: ComposedConsole) -> ThreadingHTTPServer:
    """Bind the console's loopback server without serving on it.

    Args:
        console: The composed console.

    Returns:
        The bound server. Binding and serving are separated so a test can bind
        port zero, read the port the operating system chose, and shut it down.

    """
    handler = type(
        "BoundConsoleRequestHandler",
        (ConsoleRequestHandler,),
        {"console": console},
    )
    return ThreadingHTTPServer(
        (console.settings.bind_host, console.settings.bind_port), handler
    )


def serve(console: ComposedConsole) -> None:
    """Serve the console until interrupted."""
    server = build_server(console)
    port = server.server_address[1]
    print(f"console listening on http://{console.settings.bind_host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

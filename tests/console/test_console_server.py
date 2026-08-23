"""Routing, the security headers the page is served under, and the socket."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from tests.console.conftest import (
    CREDENTIALS,
    FakeObjectReader,
    SigningClock,
    settings,
)
from tests.console.test_console_service import REQUEST, build
from tests.console.test_signed_delivery import manifest
from tkr_cloud_video.console.routes import (
    MAX_BODY_BYTES,
    content_security_policy,
    dispatch,
    read_static,
)
from tkr_cloud_video.console.run_client import RunStatus
from tkr_cloud_video.console.server import build_server
from tkr_cloud_video.console.service import ComposedConsole, compose_console


async def call(
    service: Any, method: str, path: str, body: bytes = b""
) -> tuple[int, Any]:
    """Dispatch one request and decode its JSON body."""
    response = await dispatch(service, method, path, body)
    return response.status, json.loads(response.body)


@pytest.mark.asyncio
async def test_the_page_and_its_assets_are_served_by_exact_name() -> None:
    """No request path is ever mapped onto the filesystem."""
    service, _, _, _ = build()

    for path, fragment in (
        ("/", b"<title>tkr-cloud-video console</title>"),
        ("/app.css", b"--accent"),
        ("/app.js", b"function boot"),
    ):
        response = await dispatch(service, "GET", path, b"")
        assert response.status == 200
        assert fragment in response.body

    status, payload = await call(service, "GET", "/../pyproject.toml")
    assert status == 404
    assert payload["error_code"] == "not_found"


@pytest.mark.asyncio
async def test_an_unknown_route_and_a_wrong_method_are_named() -> None:
    """Every failure is an error document the page can render."""
    service, _, _, _ = build()

    assert (await call(service, "GET", "/api/nope"))[0] == 404
    assert (await call(service, "POST", "/"))[0] == 405


@pytest.mark.asyncio
async def test_the_form_constraints_and_scope_are_served() -> None:
    """The page builds its form from the registry, not from a copy."""
    service, _, _, _ = build()

    status, constraints = await call(service, "GET", "/api/constraints")
    assert status == 200
    assert constraints["model_sets"]

    _, scope = await call(service, "GET", "/api/scope")
    assert scope["principal_id"] == "runpod-endpoint"


@pytest.mark.asyncio
async def test_a_preflight_refusal_and_an_acceptance_share_one_shape() -> None:
    """The page renders a local refusal exactly as it renders a remote one."""
    service, runs, _, _ = build()

    _, accepted = await call(
        service, "POST", "/api/preflight", json.dumps(REQUEST).encode()
    )
    _, refused = await call(
        service,
        "POST",
        "/api/preflight",
        json.dumps({**REQUEST, "frames": 74}).encode(),
    )

    assert accepted["ok"] is True
    assert refused["ok"] is False
    assert set(refused) >= {"error_code", "message", "defects"}
    assert runs.submitted == []


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_refused_as_an_envelope() -> None:
    """A malformed body is named, not raised."""
    service, _, _, _ = build()

    status, payload = await call(service, "POST", "/api/generate", b"{not json")

    assert status == 400
    assert payload["error_code"] == "invalid_envelope"


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_it_is_parsed() -> None:
    """The ceiling is enforced in the router as well as at the socket."""
    service, _, _, _ = build()

    status, payload = await call(
        service, "POST", "/api/generate", b"x" * (MAX_BODY_BYTES + 1)
    )

    assert status == 400
    assert payload["error_code"] == "request_too_large"


@pytest.mark.asyncio
async def test_the_generate_observe_and_deliver_routes_carry_one_run_through() -> None:
    """The three routes the page drives, end to end over the router."""
    reader = FakeObjectReader()
    service, runs, _, registry = build(reader=reader)

    _, submitted = await call(
        service, "POST", "/api/generate", json.dumps(REQUEST).encode()
    )
    run_id = submitted["run"]["run_id"]
    record = registry.by_run(run_id)
    assert record is not None
    reader.objects[record.commit_key] = manifest(
        record.job_id, record.attempt_id
    ).canonical_bytes()
    runs.statuses[run_id] = RunStatus(
        run_id=run_id,
        status="COMPLETED",
        output={"ok": True, "job_id": record.job_id},
    )

    _, observed = await call(service, "GET", f"/api/runs/{run_id}")
    _, delivered = await call(
        service, "POST", f"/api/jobs/{record.job_id}/link", b'{"ttl_seconds": 120}'
    )
    _, history = await call(service, "GET", "/api/history")
    _, cancelled = await call(service, "POST", f"/api/runs/{run_id}/cancel")

    assert observed["result_state"] == "completed"
    assert "X-Amz-Expires=120" in delivered["url"]
    assert len(history["runs"]) == 1
    assert cancelled["ok"] is True


@pytest.mark.asyncio
async def test_a_result_route_answers_for_a_registered_job() -> None:
    """Metadata without a link is its own route."""
    reader = FakeObjectReader()
    service, _, _, registry = build(reader=reader)
    await call(service, "POST", "/api/generate", json.dumps(REQUEST).encode())
    record = next(iter(registry))
    reader.objects[record.commit_key] = manifest(
        record.job_id, record.attempt_id
    ).canonical_bytes()

    _, payload = await call(service, "GET", f"/api/jobs/{record.job_id}/result")

    assert payload["state"] == "completed"


def test_the_policy_bounds_where_a_signed_url_could_go() -> None:
    """The page holds a delivery URL, so its policy is written around that."""
    policy = content_security_policy("https://s3.ca-east-006.backblazeb2.com")

    assert "default-src 'none'" in policy
    assert "media-src https://s3.ca-east-006.backblazeb2.com" in policy
    assert "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'none'" in policy


def test_the_packaged_assets_are_readable_from_the_installed_package() -> None:
    """The page ships with the wheel rather than being read from the checkout."""
    assert b"<!doctype html>" in read_static("index.html")


def _serve(console: ComposedConsole) -> tuple[str, threading.Thread, Any]:
    """Bind the console on an ephemeral port and serve it on a thread."""
    server = build_server(console)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_address[1]}", thread, server


def test_the_bound_server_answers_loopback_and_refuses_a_rebound_host() -> None:
    """The console authenticates nothing, so the Host header is what it checks.

    A page on another origin can point a hostname at 127.0.0.1 and reach this
    server through a browser. Rejecting an unexpected Host is what stops that
    page from driving a console holding a RunPod token.
    """
    console = compose_console(settings(bind_port=0), CREDENTIALS, SigningClock())
    base, _, server = _serve(console)
    try:
        with urllib.request.urlopen(f"{base}/api/constraints", timeout=5) as response:  # noqa: S310 - a loopback URL this test built
            assert response.status == 200
            assert response.headers["Content-Security-Policy"]
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert json.load(response)["model_sets"]

        rebound = urllib.request.Request(  # noqa: S310 - a loopback URL this test built
            f"{base}/api/constraints", headers={"Host": "console.attacker.example"}
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(rebound, timeout=5)  # noqa: S310 - a loopback URL this test built
        assert raised.value.code == 421
    finally:
        server.shutdown()
        server.server_close()


def test_the_served_page_never_carries_a_credential() -> None:
    """Nothing the browser receives contains a secret this process holds."""
    console = compose_console(settings(bind_port=0), CREDENTIALS, SigningClock())
    base, _, server = _serve(console)
    try:
        for path in ("/", "/app.js", "/app.css", "/api/constraints", "/api/scope"):
            with urllib.request.urlopen(f"{base}{path}", timeout=5) as response:  # noqa: S310 - a loopback URL this test built
                body = response.read().decode("utf-8", "replace")
            assert CREDENTIALS.runpod_api_key not in body
            assert CREDENTIALS.delivery_key_id not in body
            assert CREDENTIALS.delivery_application_key not in body
    finally:
        server.shutdown()
        server.server_close()


def test_a_console_may_not_be_bound_off_loopback() -> None:
    """The address is constrained in the settings, not in the launch command."""
    with pytest.raises(ValueError, match="loopback"):
        settings(bind_host="0.0.0.0")  # noqa: S104


@pytest.mark.asyncio
async def test_a_requested_link_lifetime_is_read_only_when_it_is_a_number() -> None:
    """A body that names no usable lifetime falls back to the configured one."""
    reader = FakeObjectReader()
    service, _, _, registry = build(reader=reader)
    await call(service, "POST", "/api/generate", json.dumps(REQUEST).encode())
    record = next(iter(registry))
    reader.objects[record.commit_key] = manifest(
        record.job_id, record.attempt_id
    ).canonical_bytes()
    path = f"/api/jobs/{record.job_id}/link"

    for body in (b"[]", b'{"ttl_seconds": true}', b'{"ttl_seconds": "long"}', b"{}"):
        _, payload = await call(service, "POST", path, body)
        assert "X-Amz-Expires=900" in payload["url"]


def test_the_bound_server_serves_posts_and_refuses_an_oversized_body() -> None:
    """The socket enforces the ceiling before it reads a body into memory."""
    console = compose_console(settings(bind_port=0), CREDENTIALS, SigningClock())
    base, _, server = _serve(console)
    try:
        request = urllib.request.Request(  # noqa: S310 - a loopback URL this test built
            f"{base}/api/preflight",
            data=json.dumps(REQUEST).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - a loopback URL this test built
            assert json.load(response)["ok"] is True

        oversized = urllib.request.Request(  # noqa: S310 - a loopback URL this test built
            f"{base}/api/preflight",
            data=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(oversized, timeout=5)  # noqa: S310 - a loopback URL this test built
        assert raised.value.code == 413
    finally:
        server.shutdown()
        server.server_close()

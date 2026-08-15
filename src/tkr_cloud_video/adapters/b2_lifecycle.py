"""Bucket lifecycle rule configuration through the native B2 API.

rclone speaks the S3-compatible surface, which has no notion of a B2 lifecycle
rule, so this is the one place the native API is used. It is operator-invoked
only: setting bucket configuration needs a capability no runtime credential
holds, and no worker process constructs anything in this module.

Pinned to API version v4, which is the version whose response shape Backblaze
documents: ``accountId`` at the top level and the storage API URL nested at
``apiInfo.storageApi.apiUrl``. A response that does not carry those is refused
rather than guessed at, because the alternative is writing bucket
configuration against a URL we inferred.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.lifecycle_rules import BucketLifecycleRule

B2_AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"
API_VERSION_PATH = "/b2api/v4"
MAXIMUM_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0

RULE_FIELDS: dict[str, str] = {
    "fileNamePrefix": "file_name_prefix",
    "daysFromUploadingToHiding": "days_from_uploading_to_hiding",
    "daysFromHidingToDeleting": "days_from_hiding_to_deleting",
    "daysFromStartingToCancelingUnfinishedLargeFiles": (
        "days_from_starting_to_canceling_unfinished_large_files"
    ),
}


class BucketConfigurationError(AppError):
    """The bucket configuration API could not be used as declared."""


class HttpTransport(Protocol):
    """Bounded JSON-over-HTTP port for the native B2 API."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one decoded JSON object."""
        ...


@dataclass(frozen=True, slots=True)
class UrllibHttpTransport(HttpTransport):
    """Dependency-free HTTPS transport constrained to Backblaze hosts."""

    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    maximum_response_bytes: int = MAXIMUM_RESPONSE_BYTES

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one request without blocking the event loop."""
        return await asyncio.to_thread(
            self._request_sync, method, url, headers, payload
        )

    def _request_sync(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not url.startswith("https://"):
            raise BucketConfigurationError(
                "bucket_api_url_rejected",
                "the bucket configuration API must be reached over HTTPS",
            )
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(url, data=body, method=method)  # noqa: S310
        for name, value in headers.items():
            request.add_header(name, value)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw = response.read(self.maximum_response_bytes + 1)
        except (HTTPError, URLError, TimeoutError) as error:
            raise BucketConfigurationError(
                "bucket_api_unavailable",
                "the bucket configuration API could not be reached",
                retryable=True,
                cause=error,
            ) from error
        if len(raw) > self.maximum_response_bytes:
            raise BucketConfigurationError(
                "bucket_api_response_too_large",
                "the bucket configuration API returned an oversized response",
            )
        return _json_object(raw)


@dataclass(frozen=True, slots=True)
class B2Account:
    """Operator credential for bucket configuration, never a worker secret."""

    key_id: str = field(repr=False)
    application_key: str = field(repr=False)
    bucket_id: str

    def __post_init__(self) -> None:
        """Reject empty credential or bucket values."""
        if not self.key_id or not self.application_key or not self.bucket_id:
            raise ValueError("B2 account configuration cannot be empty")

    def basic_authorization(self) -> str:
        """Return the documented HTTP basic auth value."""
        pair = f"{self.key_id}:{self.application_key}".encode()
        return "Basic " + base64.b64encode(pair).decode()


class B2BucketRuleAdapter:
    """Reads and writes one bucket's lifecycle rules over the native API."""

    def __init__(self, account: B2Account, transport: HttpTransport) -> None:
        """Initialize with an operator credential and a bounded transport."""
        self._account = account
        self._transport = transport

    async def read(self) -> tuple[BucketLifecycleRule, ...]:
        """Return the lifecycle rules currently in force on the bucket."""
        api_url, account_id, token = await self._authorize()
        response = await self._transport.request(
            "POST",
            f"{api_url}{API_VERSION_PATH}/b2_list_buckets",
            headers={"Authorization": token},
            payload={"accountId": account_id, "bucketId": self._account.bucket_id},
        )
        buckets = response.get("buckets")
        if not isinstance(buckets, list) or not buckets:
            raise BucketConfigurationError(
                "bucket_not_found",
                "the configured bucket was not returned by the provider",
            )
        first = buckets[0]
        rules = first.get("lifecycleRules") if isinstance(first, dict) else None
        return _decode_rules(rules if isinstance(rules, list) else [])

    async def apply(self, rules: tuple[BucketLifecycleRule, ...]) -> None:
        """Replace the bucket's lifecycle rules with the supplied set."""
        api_url, account_id, token = await self._authorize()
        await self._transport.request(
            "POST",
            f"{api_url}{API_VERSION_PATH}/b2_update_bucket",
            headers={"Authorization": token},
            payload={
                "accountId": account_id,
                "bucketId": self._account.bucket_id,
                "lifecycleRules": [_encode_rule(rule) for rule in rules],
            },
        )

    async def _authorize(self) -> tuple[str, str, str]:
        """Return the storage API URL, account id, and authorization token."""
        response = await self._transport.request(
            "GET",
            B2_AUTHORIZE_URL,
            headers={"Authorization": self._account.basic_authorization()},
        )
        token = response.get("authorizationToken")
        account_id = response.get("accountId")
        api_info = response.get("apiInfo")
        storage = api_info.get("storageApi") if isinstance(api_info, dict) else None
        api_url = storage.get("apiUrl") if isinstance(storage, dict) else None
        if (
            not isinstance(token, str)
            or not isinstance(account_id, str)
            or not isinstance(api_url, str)
            or not api_url.startswith("https://")
        ):
            raise BucketConfigurationError(
                "bucket_api_shape_unexpected",
                "the authorization response did not carry the documented fields",
            )
        return api_url.rstrip("/"), account_id, token


def _encode_rule(rule: BucketLifecycleRule) -> dict[str, Any]:
    """Render one rule in exactly the fields the provider accepts."""
    encoded: dict[str, Any] = {"fileNamePrefix": rule.file_name_prefix}
    for wire_name, attribute in RULE_FIELDS.items():
        if wire_name == "fileNamePrefix":
            continue
        encoded[wire_name] = getattr(rule, attribute)
    return encoded


def _decode_rules(payload: list[Any]) -> tuple[BucketLifecycleRule, ...]:
    """Parse provider rules, refusing any field the contract does not name.

    An unrecognized field means the bucket carries configuration this project
    cannot reason about, which would make a drift check report agreement it
    has not actually established.
    """
    decoded: list[BucketLifecycleRule] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise BucketConfigurationError(
                "bucket_rule_shape_unexpected",
                "a lifecycle rule in force is not an object",
            )
        unknown = sorted(set(entry).difference(RULE_FIELDS))
        if unknown:
            raise BucketConfigurationError(
                "bucket_rule_field_unknown",
                "a lifecycle rule in force carries an unrecognized field",
                context={"rule": ",".join(unknown)},
            )
        prefix = entry.get("fileNamePrefix")
        if not isinstance(prefix, str):
            raise BucketConfigurationError(
                "bucket_rule_shape_unexpected",
                "a lifecycle rule in force has no prefix",
            )
        decoded.append(
            BucketLifecycleRule(
                file_name_prefix=prefix,
                days_from_uploading_to_hiding=_optional_int(
                    entry.get("daysFromUploadingToHiding")
                ),
                days_from_hiding_to_deleting=_optional_int(
                    entry.get("daysFromHidingToDeleting")
                ),
                days_from_starting_to_canceling_unfinished_large_files=_optional_int(
                    entry.get("daysFromStartingToCancelingUnfinishedLargeFiles")
                ),
            )
        )
    return tuple(decoded)


def _optional_int(value: object) -> int | None:
    """Return a whole-day count, rejecting anything that is not one."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BucketConfigurationError(
            "bucket_rule_shape_unexpected",
            "a lifecycle rule period is not a whole number of days",
        )
    return value


def _json_object(raw: bytes) -> dict[str, Any]:
    """Parse a provider response, requiring a JSON object."""
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BucketConfigurationError(
            "bucket_api_response_invalid",
            "the bucket configuration API returned invalid JSON",
            cause=error,
        ) from error
    if not isinstance(value, dict):
        raise BucketConfigurationError(
            "bucket_api_response_invalid",
            "the bucket configuration API returned a non-object response",
        )
    return value

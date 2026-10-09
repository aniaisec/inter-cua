"""Copyable HTTP client. A settled run is returned for the caller to interpret."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

ACTIVE = frozenset({"queued", "running"})
STOPPED = frozenset({"escalated", "finished", "error", "lost"})


class Conflict(RuntimeError):
    """The key belongs to another request. Do not replace it and try again."""


class UnknownSubmission(RuntimeError):
    """No answer received; retain the Submission and reconcile its identity."""


@dataclass(frozen=True)
class Submission:
    payload: bytes
    idempotency_key: str
    request_id: str

    @classmethod
    def create(cls, body: dict[str, Any], *, key: str, request_id: str) -> Submission:
        if not key or not request_id:
            raise ValueError("persist a nonempty idempotency key and request id before submitting")
        # Capture the exact request, so caller mutations cannot alter a retry.
        return cls(json.dumps(body, sort_keys=True, allow_nan=False).encode(), key, request_id)


class RunClient:
    def __init__(self, http: httpx.Client, *, api_key: str, tenant: str) -> None:
        self.http = http
        self.headers = {"Authorization": f"Bearer {api_key}", "X-Cua-Tenant": tenant}

    def submit(self, submission: Submission, *, transport_retries: int = 1) -> dict[str, Any]:
        if transport_retries < 0:
            raise ValueError("transport_retries must be nonnegative")
        headers = {
            **self.headers,
            "X-Request-Id": submission.request_id,
            "Idempotency-Key": submission.idempotency_key,
            "Content-Type": "application/json",
        }
        for attempt in range(transport_retries + 1):
            try:
                response = self.http.post("/runs", content=submission.payload, headers=headers)
                return self._read(response)
            except httpx.TransportError:
                if attempt == transport_retries:
                    raise UnknownSubmission(
                        "response unavailable; retain this key and payload, "
                        "and reconcile before new work"
                    ) from None
        raise AssertionError("unreachable")

    @staticmethod
    def _read(response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 409:
            raise Conflict(
                "request conflict; inspect the existing run, never choose a fresh retry key"
            )
        response.raise_for_status()
        record: dict[str, Any] = response.json()
        if (
            not isinstance(record, dict)
            or record.get("state") not in ACTIVE | STOPPED
            or not isinstance(record.get("run_id"), str)
            or not record["run_id"]
        ):
            raise ValueError("unrecognized run response; reconcile without resubmitting")
        return record

    def poll(
        self, record: dict[str, Any], *, timeout_s: float = 120, interval_s: float = 0.2
    ) -> dict[str, Any]:
        if (
            not math.isfinite(timeout_s)
            or not math.isfinite(interval_s)
            or timeout_s <= 0
            or interval_s < 0
        ):
            raise ValueError(
                "poll timeout must be finite and positive, interval finite and nonnegative"
            )
        deadline = time.monotonic() + timeout_s
        run_id = record["run_id"]
        while True:
            state = record.get("state")
            if state in STOPPED:
                # Escalated: explicit human action. Lost/unknown side effect: reconciliation.
                # Finished includes business outcomes and failures; inspect result.kind.
                return record
            if state not in ACTIVE:
                raise ValueError("unrecognized run state; reconcile without resubmitting")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"keep polling run {run_id}; do not create a replacement invocation"
                )
            time.sleep(min(interval_s, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"keep polling run {run_id}; the polling deadline expired")
            response = self.http.get(
                f"/runs/{quote(run_id, safe='')}", headers=self.headers, timeout=remaining
            )
            record = self._read(response)
            if record["run_id"] != run_id:
                raise ValueError("poll returned another run; reconcile without resubmitting")

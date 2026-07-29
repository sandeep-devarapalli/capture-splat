from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .json_utils import ensure_finite, reject_constant
from .live_session import (
    LIVE_ACK_SCHEMA,
    LIVE_FINALIZE_SCHEMA,
    LIVE_REPLAY_SUMMARY_SCHEMA,
    LiveReplayPlan,
    ReplayAsset,
    ReplayFrame,
    build_live_replay_plan,
    expand_missing_ranges,
    validate_live_ack,
)

DEFAULT_LIVE_RECEIVER = "http://127.0.0.1:43127"
API_PREFIX = "/api/capture-splat/live/v0.1"
MAX_RESPONSE_BYTES = 1024 * 1024


class LiveReplayError(RuntimeError):
    pass


def _receiver_target(receiver: str) -> tuple[str, int]:
    parsed = urlparse(receiver)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("receiver must be a loopback HTTP URL")
    if parsed.path not in {"", "/"}:
        raise ValueError("receiver URL must not include a path")
    host = parsed.hostname
    if host is None:
        raise ValueError("receiver must include a host")
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("receiver must use a loopback host")
        except ValueError as exc:
            if str(exc) == "receiver must use a loopback host":
                raise
            raise ValueError("receiver must use 127.0.0.1, ::1, or localhost") from exc
    try:
        port = parsed.port or 43127
    except ValueError as exc:
        raise ValueError("receiver port is invalid") from exc
    return host, port


class LiveReceiverClient:
    def __init__(self, receiver: str, *, timeout: float = 30.0) -> None:
        self.receiver = receiver.rstrip("/")
        host, port = _receiver_target(receiver)
        self._target = (host, port)
        self._timeout = timeout
        self._connection = http.client.HTTPConnection(host, port, timeout=timeout)

    def close(self) -> None:
        self._connection.close()

    def reconnect(self) -> None:
        self.close()
        self._connection = http.client.HTTPConnection(*self._target, timeout=self._timeout)

    def _session_path(self, session_id: str) -> str:
        return f"{API_PREFIX}/sessions/{quote(session_id, safe='')}"

    def _decode_ack(self, response: http.client.HTTPResponse) -> dict[str, Any]:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            self.close()
            raise LiveReplayError("receiver response exceeded one MiB")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LiveReplayError(f"receiver returned non-UTF-8 HTTP {response.status}") from exc
        if not 200 <= response.status < 300:
            detail = text.strip().replace("\n", " ")[:400]
            raise LiveReplayError(f"receiver returned HTTP {response.status}: {detail}")
        try:
            payload = json.loads(text, parse_constant=reject_constant)
            return validate_live_ack(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LiveReplayError("receiver returned an invalid live ACK") from exc

    def _json_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            ensure_finite(payload)
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        self._connection.request(method, path, body=body, headers=headers)
        return self._decode_ack(self._connection.getresponse())

    def put_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return self._json_request("PUT", self._session_path(str(session["session_id"])), session)

    def put_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        path = f"{self._session_path(str(frame['session_id']))}/frames/{int(frame['sequence_id'])}"
        return self._json_request("PUT", path, frame)

    def put_asset(self, session_id: str, sequence_id: int, asset: ReplayAsset) -> dict[str, Any]:
        role = quote(asset.role, safe="")
        path = f"{self._session_path(session_id)}/frames/{sequence_id}/assets/{role}"
        expected_sha = str(asset.reference["sha256"])
        expected_size = int(asset.reference["size_bytes"])
        digest = hashlib.sha256()
        sent = 0
        with asset.path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != expected_size:
                raise LiveReplayError(f"asset changed after replay plan creation: {asset.reference['path']}")
            self._connection.putrequest("PUT", path)
            self._connection.putheader("Accept", "application/json")
            self._connection.putheader("Content-Type", str(asset.reference["media_type"]))
            self._connection.putheader("Content-Length", str(expected_size))
            self._connection.putheader("X-Capture-Splat-SHA256", expected_sha)
            self._connection.endheaders()
            while sent < expected_size:
                chunk = stream.read(min(1024 * 1024, expected_size - sent))
                if not chunk:
                    self.close()
                    raise LiveReplayError(f"asset changed after replay plan creation: {asset.reference['path']}")
                sent += len(chunk)
                digest.update(chunk)
                self._connection.send(chunk)
            grew = bool(stream.read(1))
        actual_sha = f"sha256:{digest.hexdigest()}"
        if grew or sent != expected_size or actual_sha != expected_sha:
            self.close()
            raise LiveReplayError(f"asset changed after replay plan creation: {asset.reference['path']}")
        return self._decode_ack(self._connection.getresponse())

    def get_status(self, session_id: str) -> dict[str, Any]:
        return self._json_request("GET", self._session_path(session_id))

    def finalize(self, session_id: str, final_sequence_id: int) -> dict[str, Any]:
        payload = {
            "schema": LIVE_FINALIZE_SCHEMA,
            "session_id": session_id,
            "final_sequence_id": final_sequence_id,
        }
        return self._json_request("POST", f"{self._session_path(session_id)}/finalize", payload)


def _send_frame(client: LiveReceiverClient, replay_frame: ReplayFrame) -> dict[str, Any]:
    ack = client.put_frame(replay_frame.metadata)
    for asset in replay_frame.assets:
        ack = client.put_asset(str(replay_frame.metadata["session_id"]), replay_frame.sequence_id, asset)
    return ack


def _summary(
    plan: LiveReplayPlan,
    receiver: str,
    status: str,
    ack: dict[str, Any],
    *,
    planned_order: list[int],
    sent_sequence_ids: list[int],
    unique_acks: int,
    duplicate_sends: int,
    resumed: bool,
    disconnects: int,
    transport_recoveries: int,
) -> dict[str, Any]:
    payload = {
        "schema": LIVE_REPLAY_SUMMARY_SCHEMA,
        "status": status,
        "session_id": plan.session["session_id"],
        "receiver": receiver,
        "frame_count": len(plan.frames),
        "planned_order": planned_order,
        "sent_sequence_ids": sent_sequence_ids,
        "unique_acks": unique_acks,
        "duplicate_sends": duplicate_sends,
        "resumed": resumed,
        "simulated_disconnects": disconnects,
        "transport_recoveries": transport_recoveries,
        "received_count": ack["received_count"],
        "contiguous_count": ack["contiguous_count"],
        "pending_count": ack["pending_count"],
        "next_expected_sequence_id": ack["next_expected_sequence_id"],
        "missing_ranges": ack["missing_ranges"],
        "finalized": ack["finalized"],
        "authority": "proposal_only",
    }
    ensure_finite(payload)
    return payload


def replay_live_session(
    capture: Path,
    *,
    receiver: str = DEFAULT_LIVE_RECEIVER,
    session_id: str | None = None,
    delay_ms: int = 0,
    shuffle: bool = False,
    seed: int = 0,
    duplicate_every: int = 0,
    disconnect_after: int | None = None,
    disconnect_seconds: float = 0.0,
    resume: bool = False,
    client_factory: Callable[[str], LiveReceiverClient] = LiveReceiverClient,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if delay_ms < 0:
        raise ValueError("delay_ms must be non-negative")
    if duplicate_every < 0:
        raise ValueError("duplicate_every must be non-negative")
    if disconnect_after is not None and disconnect_after < 1:
        raise ValueError("disconnect_after must be at least one")
    if disconnect_seconds < 0:
        raise ValueError("disconnect_seconds must be non-negative")
    _receiver_target(receiver)

    resolved_session_id = session_id or str(uuid.uuid4())
    plan = build_live_replay_plan(capture, resolved_session_id)
    frames = {frame.sequence_id: frame for frame in plan.frames}
    planned_order = list(frames)
    if shuffle:
        random.Random(seed).shuffle(planned_order)

    client = client_factory(receiver)
    try:
        ack = client.put_session(plan.session)
        if ack["schema"] != LIVE_ACK_SCHEMA or ack["session_id"] != resolved_session_id:
            raise LiveReplayError("receiver ACK does not match the replay session")
        if resume:
            ack = client.get_status(resolved_session_id)
    except Exception:
        client.close()
        raise
    if ack["finalized"]:
        client.close()
        return _summary(
            plan, receiver, "finalized", ack, planned_order=planned_order, sent_sequence_ids=[], unique_acks=0,
            duplicate_sends=0, resumed=resume, disconnects=0, transport_recoveries=0,
        )

    remaining = planned_order[:]
    if resume:
        missing = expand_missing_ranges(ack["missing_ranges"])
        remaining = [sequence_id for sequence_id in planned_order if sequence_id in missing]
    sent_sequence_ids: list[int] = []
    unique_acks = 0
    duplicate_sends = 0
    max_received = int(ack["received_count"])
    disconnects = 0
    transport_recoveries = 0
    completed_primary_sends = 0
    simulated = False
    recovery_failures = 0

    try:
        while remaining:
            sequence_id = remaining[0]
            try:
                ack = _send_frame(client, frames[sequence_id])
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                if not resume or recovery_failures >= 2:
                    raise LiveReplayError(f"receiver transport failed: {exc}") from exc
                recovery_failures += 1
                transport_recoveries += 1
                client.reconnect()
                sleep(disconnect_seconds)
                client.put_session(plan.session)
                ack = client.get_status(resolved_session_id)
                missing = expand_missing_ranges(ack["missing_ranges"])
                remaining = [item for item in planned_order if item in missing]
                max_received = max(max_received, int(ack["received_count"]))
                continue
            recovery_failures = 0
            sent_sequence_ids.append(sequence_id)
            completed_primary_sends += 1
            received = int(ack["received_count"])
            if received > max_received:
                unique_acks += received - max_received
                max_received = received
            remaining.pop(0)

            if duplicate_every and completed_primary_sends % duplicate_every == 0:
                ack = _send_frame(client, frames[sequence_id])
                sent_sequence_ids.append(sequence_id)
                duplicate_sends += 1
                max_received = max(max_received, int(ack["received_count"]))

            if disconnect_after is not None and unique_acks >= disconnect_after and not simulated:
                simulated = True
                disconnects += 1
                client.close()
                if not resume:
                    return _summary(
                        plan, receiver, "interrupted", ack, planned_order=planned_order,
                        sent_sequence_ids=sent_sequence_ids, unique_acks=unique_acks,
                        duplicate_sends=duplicate_sends, resumed=False, disconnects=disconnects,
                        transport_recoveries=transport_recoveries,
                    )
                sleep(disconnect_seconds)
                client = client_factory(receiver)
                client.put_session(plan.session)
                ack = client.get_status(resolved_session_id)
                missing = expand_missing_ranges(ack["missing_ranges"])
                remaining = [item for item in planned_order if item in missing]
                max_received = max(max_received, int(ack["received_count"]))

            if remaining and delay_ms:
                sleep(delay_ms / 1000.0)

        ack = client.finalize(resolved_session_id, len(plan.frames))
        if not ack["finalized"] or ack["status"] != "finalized":
            raise LiveReplayError("receiver did not confirm finalization")
        return _summary(
            plan, receiver, "finalized", ack, planned_order=planned_order,
            sent_sequence_ids=sent_sequence_ids, unique_acks=unique_acks,
            duplicate_sends=duplicate_sends, resumed=resume, disconnects=disconnects,
            transport_recoveries=transport_recoveries,
        )
    finally:
        client.close()

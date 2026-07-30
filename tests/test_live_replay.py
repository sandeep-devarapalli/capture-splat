from __future__ import annotations

import hashlib
import json
import random
import socket
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
from PIL import Image

from capture_splat.json_utils import write_json_strict
from capture_splat import cli
from capture_splat.live_replay import replay_live_session


def _capture(root: Path, count: int = 3) -> Path:
    (root / "rgb").mkdir(parents=True)
    frames = []
    for sequence_id in range(1, count + 1):
        name = f"frame-{sequence_id}.jpg"
        Image.new("RGB", (4, 3), (sequence_id * 20, 10, 5)).save(root / "rgb" / name)
        frames.append({
            "rgb": f"rgb/{name}",
            "timestamp": float(sequence_id),
            "transform_matrix": [
                [1, 0, 0, sequence_id * 0.1],
                [0, 1, 0, 0],
                [0, 0, 1, -sequence_id * 0.2],
                [0, 0, 0, 1],
            ],
            "capture_quality": {"accepted": True, "score": 0.9},
        })
    write_json_strict(root / "capture.json", {
        "schema": "capture_splat.v0.3",
        "session_config": {"scale_authority": "arkit_vio_metric", "up_axis": [0, 1, 0]},
        "intrinsics": {"fl_x": 4.0, "fl_y": 3.0, "cx": 2.0, "cy": 1.5, "w": 4, "h": 3},
        "frames": frames,
    })
    return root


def _ranges(missing: list[int]) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    for value in missing:
        if result and result[-1]["end"] == value - 1:
            result[-1]["end"] = value
        else:
            result.append({"start": value, "end": value})
    return result


class _ReceiverState:
    def __init__(self) -> None:
        self.session: dict[str, Any] | None = None
        self.frames: dict[int, dict[str, Any]] = {}
        self.assets: dict[tuple[int, str], bytes] = {}
        self.received: set[int] = set()
        self.frame_order: list[int] = []
        self.asset_order: list[tuple[int, str]] = []
        self.finalize_payloads: list[dict[str, Any]] = []
        self.finalized = False
        self.drop_next_asset_ack = False
        self.lock = threading.Lock()

    def ack(self, operation: str, status: str, *, sequence_id: int | None = None, asset_role: str | None = None) -> dict[str, Any]:
        assert self.session is not None
        expected = int(self.session["expected_frame_count"])
        contiguous = 0
        while contiguous + 1 in self.received:
            contiguous += 1
        payload: dict[str, Any] = {
            "schema": "capture_splat.live_ack.v0.1",
            "session_id": self.session["session_id"],
            "operation": operation,
            "status": status,
            "received_count": len(self.received),
            "contiguous_count": contiguous,
            "pending_count": len(self.received) - contiguous,
            "expected_frame_count": expected,
            "next_expected_sequence_id": contiguous + 1,
            "missing_ranges": _ranges([value for value in range(1, expected + 1) if value not in self.received]),
            "finalized": self.finalized,
        }
        if sequence_id is not None:
            payload["sequence_id"] = sequence_id
        if asset_role is not None:
            payload["asset_role"] = asset_role
        return payload


def _asset_reference(frame: dict[str, Any], role: str) -> dict[str, Any] | None:
    if role == "source":
        return frame["source_frame"]
    assets = frame.get("assets", {})
    if role in {"depth", "confidence"}:
        return assets.get(role)
    if role.startswith("mask-"):
        kind = role.removeprefix("mask-")
        return next((value for value in assets.get("masks", []) if value["kind"] == kind), None)
    return None


def _required_roles(frame: dict[str, Any]) -> set[str]:
    result = {"source"}
    assets = frame.get("assets", {})
    result.update(key for key in ("depth", "confidence") if key in assets)
    result.update(f"mask-{mask['kind']}" for mask in assets.get("masks", []))
    return result


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _ReceiverState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _json(self) -> dict[str, Any]:
        return json.loads(self._body().decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parts(self) -> list[str]:
        return self.path.split("?", 1)[0].strip("/").split("/")

    def do_PUT(self) -> None:
        parts = self._parts()
        if len(parts) == 6:
            payload = self._json()
            with self.state.lock:
                if self.state.session is None:
                    self.state.session = payload
                    status = "accepted"
                elif self.state.session == payload:
                    status = "duplicate"
                else:
                    self._send({"error": "session conflict"}, 409)
                    return
                self._send(self.state.ack("session", status))
            return
        if len(parts) == 8 and parts[6] == "frames":
            payload = self._json()
            sequence_id = int(parts[7])
            with self.state.lock:
                prior = self.state.frames.get(sequence_id)
                if prior is not None and prior != payload:
                    self._send({"error": "frame conflict"}, 409)
                    return
                if prior is None:
                    self.state.frames[sequence_id] = payload
                    self.state.frame_order.append(sequence_id)
                self._send(self.state.ack("frame", "duplicate" if prior else "incomplete", sequence_id=sequence_id))
            return
        if len(parts) == 10 and parts[6] == "frames" and parts[8] == "assets":
            sequence_id = int(parts[7])
            role = parts[9]
            body = self._body()
            with self.state.lock:
                frame = self.state.frames[sequence_id]
                reference = _asset_reference(frame, role)
                digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
                if reference is None or len(body) != reference["size_bytes"] or digest != reference["sha256"]:
                    self._send({"error": "asset mismatch"}, 422)
                    return
                key = (sequence_id, role)
                prior = self.state.assets.get(key)
                if prior is not None and prior != body:
                    self._send({"error": "asset conflict"}, 409)
                    return
                self.state.assets[key] = body
                self.state.asset_order.append(key)
                if _required_roles(frame) <= {asset_role for seq, asset_role in self.state.assets if seq == sequence_id}:
                    self.state.received.add(sequence_id)
                ack = self.state.ack("asset", "duplicate" if prior else "accepted", sequence_id=sequence_id, asset_role=role)
                if self.state.drop_next_asset_ack:
                    self.state.drop_next_asset_ack = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self._send(ack)
            return
        self._send({"error": "not found"}, 404)

    def do_GET(self) -> None:
        with self.state.lock:
            self._send(self.state.ack("resume", "finalized" if self.state.finalized else "accepted"))

    def do_POST(self) -> None:
        payload = self._json()
        with self.state.lock:
            self.state.finalize_payloads.append(payload)
            expected = int(self.state.session["expected_frame_count"])  # type: ignore[index]
            if payload["final_sequence_id"] != expected or self.state.received != set(range(1, expected + 1)):
                self._send({"error": "missing frames"}, 409)
                return
            self.state.finalized = True
            self._send(self.state.ack("finalize", "finalized"))


@contextmanager
def _receiver(*, drop_next_asset_ack: bool = False) -> Iterator[tuple[str, _ReceiverState]]:
    state = _ReceiverState()
    state.drop_next_asset_ack = drop_next_asset_ack
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_replay_is_sequential_and_finalized(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")
    with _receiver() as (url, state):
        summary = replay_live_session(capture, receiver=url, session_id="sequential")
    assert summary["schema"] == "capture_splat.live_replay_summary.v0.1"
    assert summary["status"] == "finalized"
    assert summary["planned_order"] == [1, 2, 3]
    assert summary["sent_sequence_ids"] == [1, 2, 3]
    assert summary["received_count"] == 3
    assert summary["finalized"] is True
    assert state.session is not None
    assert state.session["schema"] == "capture_splat.live_session.v0.1"
    assert state.finalize_payloads == [{
        "schema": "capture_splat.live_finalize.v0.1",
        "session_id": "sequential",
        "final_sequence_id": 3,
    }]
    assert state.frame_order == [1, 2, 3]


def test_http_replay_shuffle_duplicate_and_delay_are_deterministic(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")
    sleeps: list[float] = []
    expected = [1, 2, 3]
    random.Random(17).shuffle(expected)
    with _receiver() as (url, state):
        summary = replay_live_session(
            capture,
            receiver=url,
            session_id="shuffled",
            shuffle=True,
            seed=17,
            delay_ms=25,
            duplicate_every=2,
            sleep=sleeps.append,
        )
    assert summary["planned_order"] == expected
    assert summary["sent_sequence_ids"] == [expected[0], expected[1], expected[1], expected[2]]
    assert summary["duplicate_sends"] == 1
    assert sleeps == [0.025, 0.025]
    assert state.received == {1, 2, 3}


def test_disconnect_without_resume_returns_strict_interrupted_summary(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")
    with _receiver() as (url, state):
        summary = replay_live_session(
            capture, receiver=url, session_id="interrupted", disconnect_after=1
        )
    assert summary["status"] == "interrupted"
    assert summary["simulated_disconnects"] == 1
    assert summary["received_count"] == 1
    assert summary["missing_ranges"] == [{"start": 2, "end": 3}]
    assert state.finalized is False


def test_disconnect_with_resume_queries_durable_state_and_finalizes(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")
    sleeps: list[float] = []
    with _receiver() as (url, state):
        summary = replay_live_session(
            capture,
            receiver=url,
            session_id="resumed",
            disconnect_after=1,
            disconnect_seconds=0.5,
            resume=True,
            sleep=sleeps.append,
        )
    assert summary["status"] == "finalized"
    assert summary["simulated_disconnects"] == 1
    assert summary["received_count"] == 3
    assert sleeps == [0.5]
    assert state.finalized is True


def test_cross_process_resume_sends_only_missing_sequences(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")
    with _receiver() as (url, state):
        first = replay_live_session(capture, receiver=url, session_id="cross-process", disconnect_after=1)
        second = replay_live_session(capture, receiver=url, session_id="cross-process", resume=True)
    assert first["status"] == "interrupted"
    assert second["status"] == "finalized"
    assert second["sent_sequence_ids"] == [2, 3]
    assert state.frame_order == [1, 2, 3]


def test_lost_asset_ack_recovers_from_receiver_status(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")
    with _receiver(drop_next_asset_ack=True) as (url, state):
        summary = replay_live_session(capture, receiver=url, session_id="lost-ack", resume=True)
    assert summary["status"] == "finalized"
    assert summary["transport_recoveries"] == 1
    assert summary["sent_sequence_ids"] == [2, 3]
    assert state.received == {1, 2, 3}


@pytest.mark.parametrize("receiver", ["https://127.0.0.1:43127", "http://example.com:43127", "http://127.0.0.1:43127/path"])
def test_receiver_is_loopback_http_only(tmp_path: Path, receiver: str) -> None:
    with pytest.raises(ValueError, match="receiver"):
        replay_live_session(_capture(tmp_path / "capture"), receiver=receiver, session_id="unsafe")


def test_initial_session_failure_closes_client(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=1)

    class FailingClient:
        closed = False

        def put_session(self, session: dict[str, Any]) -> dict[str, Any]:
            raise OSError("receiver unavailable")

        def close(self) -> None:
            self.closed = True

    client = FailingClient()
    with pytest.raises(OSError, match="receiver unavailable"):
        replay_live_session(capture, receiver="http://127.0.0.1:43127", client_factory=lambda _: client)  # type: ignore[arg-type]
    assert client.closed is True


def test_cli_prints_interrupted_summary_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    captured: dict[str, Any] = {}

    def fake_replay(capture: Path, **options: Any) -> dict[str, Any]:
        captured.update({"capture": capture, **options})
        return {
            "schema": "capture_splat.live_replay_summary.v0.1",
            "status": "interrupted",
            "session_id": "cli-session",
            "finalized": False,
        }

    monkeypatch.setattr(cli, "replay_live_session", fake_replay)
    monkeypatch.setattr(sys, "argv", [
        "capture-splat", "replay-live-session", "--capture", "/tmp/capture", "--session-id", "cli-session",
        "--delay-ms", "10", "--shuffle", "--seed", "7", "--duplicate-every", "2",
        "--disconnect-after", "1", "--disconnect-seconds", "0.5", "--resume",
    ])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "interrupted"
    assert captured["capture"] == Path("/tmp/capture")
    assert captured["delay_ms"] == 10
    assert captured["shuffle"] is True
    assert captured["seed"] == 7
    assert captured["duplicate_every"] == 2
    assert captured["disconnect_after"] == 1
    assert captured["disconnect_seconds"] == 0.5
    assert captured["resume"] is True

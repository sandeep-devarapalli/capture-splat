import hashlib
import http.server
import json
import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Swift host probe requires macOS")


@pytest.fixture(scope="module")
def live_sender_probe(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    repository = Path(__file__).resolve().parents[1]
    build_root = tmp_path_factory.mktemp("live-sender-probe")
    executable = build_root / "live-sender-probe"
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(build_root / "clang-module-cache")
    environment["SWIFT_MODULECACHE_PATH"] = str(build_root / "swift-module-cache")
    sources = [
        repository / "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveAuthContract.swift",
        repository / "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveAuthClient.swift",
        repository / "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveSenderQueue.swift",
        repository / "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveSender.swift",
        repository / "tests/swift/LiveSenderProbe.swift",
    ]
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-swift-version",
            "5",
            "-parse-as-library",
            "-D",
            "CAPTURE_SPLAT_LIVE_TESTING",
            *map(str, sources),
            "-o",
            str(executable),
        ],
        check=True,
        env=environment,
    )
    return executable, repository


def _run(probe: tuple[Path, Path], scenario: str, working_root: Path) -> dict[str, object]:
    executable, _ = probe
    result = subprocess.run(
        [str(executable), scenario, str(working_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_live_auth_vectors_and_strict_decoding(
    live_sender_probe: tuple[Path, Path],
) -> None:
    _, repository = live_sender_probe
    result = _run(live_sender_probe, "auth-vectors", repository)

    assert result == {
        "duplicate_json_rejected": True,
        "expiry_boundary_rejected": True,
        "extra_auth_error_rejected": True,
        "noncanonical_base64_rejected": True,
        "nonfinite_json_rejected": True,
        "pairing_proof": "twUi3bjTleWDkaZRduwOY4Q5Cn_DIGwR4fhkR5hlQU4",
        "qr_round_trip": True,
        "request_has_final_newline": True,
        "request_vector_match": True,
        "zero_signature_rejected": True,
    }


def test_pairing_identity_counters_and_revocation(
    live_sender_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_sender_probe, "pairing", tmp_path)

    assert result == {
        "durable_counters": True,
        "empty_get_rules": True,
        "expired_grant_removed": True,
        "expiry_rejected": True,
        "fresh_retry_request_ids": True,
        "grant_reloaded": True,
        "identical_pairing_retry": True,
        "pairing_secret_persisted": False,
        "pending_pairing_cleared": True,
        "pending_pairing_recovered": True,
        "pin_mismatch_rejected": True,
        "revocation_rejected": True,
        "revoked_grant_removed": True,
        "stable_device_id": True,
    }


def test_pinned_transport_accepts_only_the_declared_tls_certificate(
    live_sender_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    certificate = tmp_path / "receiver.pem"
    private_key = tmp_path / "receiver-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=world-studio.local",
        ],
        check=True,
        capture_output=True,
    )

    requests: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("Content-Length")))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        der = ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding="ascii"))
        configuration = {
            "certificate_sha256": f"sha256:{hashlib.sha256(der).hexdigest()}",
            "port": server.server_port,
        }
        (tmp_path / "transport.json").write_text(
            json.dumps(configuration, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        result = _run(live_sender_probe, "pinned-transport", tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result == {
        "body_match": True,
        "status_code": 200,
        "tls_pin_accepted": True,
        "wrong_pin_rejected": True,
    }
    assert requests == [("/health", "0")]


def test_bounded_queue_resume_and_fail_closed_validation(
    live_sender_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_sender_probe, "queue", tmp_path)

    assert result == {
        "acknowledged_retry_disposition": "duplicate",
        "capacity_disposition": "capacity_exceeded",
        "checksum_mismatch_rejected": True,
        "conflict_rejected": True,
        "conflicting_acknowledged_retry_rejected": True,
        "contradictory_ack_rejected": True,
        "corrupt_state_rejected": True,
        "duplicate_disposition": "duplicate",
        "final_sequence_bound_enforced": True,
        "finalization_ready": True,
        "finalized": True,
        "false_finalization_rejected": True,
        "gap_acknowledged": [2],
        "identical_acknowledged_retry_disposition": "duplicate",
        "missing_finalization_blocked": True,
        "out_of_order_accepted": True,
        "overflow_ack_rejected": True,
        "post_finalization_rejected": True,
        "restart_pending": [1],
        "source_preserved": True,
        "stale_progress_ignored": True,
        "symlink_rejected": True,
        "unsafe_paths_rejected": True,
    }


def test_progressive_session_binding_is_durable_and_fail_closed(
    live_sender_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_sender_probe, "progressive", tmp_path)

    assert result == {
        "conflicting_binding_rejected": True,
        "contradictory_open_ack_rejected": True,
        "corrupt_manifest_rejected": True,
        "corrupt_manifest_restart_rejected": True,
        "derived_session_id_matches": True,
        "differing_expected_rejected": True,
        "expected_count_promoted": True,
        "finalize_payload_valid": True,
        "idempotent_after_finalize": True,
        "immutable_session_rejected": True,
        "lost_finalize_ack_interrupted": True,
        "manifest_schema_mismatch_rejected": True,
        "manifest_reverified_before_send": True,
        "mismatched_seed_rejected": True,
        "missing_manifest_rejected": True,
        "opened_before_manifest": True,
        "pre_manifest_session_sent": True,
        "restart_preserved_binding": True,
        "restart_resumed_finalization": True,
        "stale_nil_ignored": True,
    }


def test_sender_engine_retries_with_bounded_concurrency_and_resumes_first(
    live_sender_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_sender_probe, "engine", tmp_path)

    assert result == {
        "acknowledged_frames": 2,
        "attempted_frames": 2,
        "authorization_owner_enforced": True,
        "finalized": True,
        "initial_statuses": ["idle", "interrupted"],
        "interruption_disposition": "retryable",
        "lost_ack_retried": True,
        "lost_finalization_ack_resumed": True,
        "maximum_concurrency": 2,
        "post_finalization_idempotent": True,
        "queued_frames": 0,
        "recovery_status": "finalized",
        "resume_before_frames": True,
        "v0_1_finalize_payload_valid": True,
    }


def test_sender_policy_yields_to_capture_safety(
    live_sender_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_sender_probe, "policy", tmp_path)

    assert result == {
        "background": "background",
        "blocked_error": "blocked",
        "cancelled_error": "cancelled",
        "contract_error": "blocked",
        "critical": "thermal_pressure",
        "failure_priority": "blocked",
        "network": "network_unavailable",
        "queue_error": "blocked",
        "ready": "ready",
        "receiver": "receiver_unavailable",
        "retryable_error": "retryable",
        "serious": "thermal_pressure",
        "storage": "low_storage",
        "unknown_error": "blocked",
    }


def test_sender_sources_are_capture_loop_independent() -> None:
    repository = Path(__file__).resolve().parents[1]
    source_root = repository / "apps/ios/CaptureSplat/CaptureSplat/Sources"
    sender_sources = [
        source_root / "LiveAuthContract.swift",
        source_root / "LiveAuthClient.swift",
        source_root / "LiveSenderQueue.swift",
        source_root / "LiveSender.swift",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in sender_sources)

    assert "ARFrame" not in combined
    assert "CVPixelBuffer" not in combined
    assert "import ARKit" not in combined
    assert "import CoreVideo" not in combined
    assert "tlsMinimumSupportedProtocolVersion = .TLSv13" in combined
    assert "SecCertificateCopyData" in combined
    assert "SecTrustEvaluateWithError" in combined
    assert "willPerformHTTPRedirection" in combined

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Swift host probe requires macOS")


@pytest.fixture(scope="module")
def live_pairing_app_probe(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    repository = Path(__file__).resolve().parents[1]
    build_root = tmp_path_factory.mktemp("live-pairing-app-probe")
    executable = build_root / "live-pairing-app-probe"
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(build_root / "clang-module-cache")
    environment["SWIFT_MODULECACHE_PATH"] = str(build_root / "swift-module-cache")
    sources = repository / "apps/ios/CaptureSplat/CaptureSplat/Sources"
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-swift-version",
            "5",
            "-parse-as-library",
            "-D",
            "CAPTURE_SPLAT_LIVE_TESTING",
            str(sources / "LiveAuthContract.swift"),
            str(sources / "LiveAuthClient.swift"),
            str(sources / "LiveApplicationSupport.swift"),
            str(sources / "LiveBonjourResolver.swift"),
            str(sources / "LivePairingCoordinator.swift"),
            str(sources / "LiveSenderQueue.swift"),
            str(repository / "tests/swift/LivePairingAppProbe.swift"),
            "-o",
            str(executable),
        ],
        check=True,
        env=environment,
    )
    return executable


def test_pairing_app_persists_and_resumes_without_implicit_network(
    live_pairing_app_probe: Path,
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [str(live_pairing_app_probe), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "application_support_root": True,
        "background_cleanup_durable": True,
        "bonjour_exact_match": True,
        "bonjour_wrong_service_ignored": True,
        "cancelled_durable_grant_surfaced": True,
        "cancellation_stops_discovery": True,
        "corrupt_profile_recovered_from_keychain": True,
        "corruption_failed_closed": True,
        "expired_cancel_grant_rejected": True,
        "interrupted_after_lost_response": True,
        "local_forget_durable": True,
        "multibyte_device_name_bounded": True,
        "no_discovery_before_opt_in": True,
        "no_secret_in_profile": True,
        "paired_after_retry": True,
        "paired_restored_without_network": True,
        "pending_restored_without_network": True,
        "pending_transfer_blocks_credential_reset": True,
        "pending_transfer_blocks_forget": True,
        "pending_symlink_blocks_pairing_clear": True,
        "queue_path_confined": True,
        "second_pairing_blocked": True,
        "startup_failure_reset_failed_closed": True,
        "traversal_rejected": True,
    }


def test_pairing_privacy_declarations_are_narrow() -> None:
    repository = Path(__file__).resolve().parents[1]
    plist_path = (
        repository
        / "apps/ios/CaptureSplat/CaptureSplat/Resources/Info.plist"
    )
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["NSBonjourServices"] == ["_capturesplat._tcp"]
    assert "specific World Studio Mac" in plist["NSLocalNetworkUsageDescription"]
    assert "pairing QR" in plist["NSCameraUsageDescription"]
    assert "UIBackgroundModes" not in plist


def test_pairing_wiring_stays_outside_the_capture_loop() -> None:
    repository = Path(__file__).resolve().parents[1]
    source_root = repository / "apps/ios/CaptureSplat/CaptureSplat/Sources"
    live_sources = [
        source_root / "LiveApplicationSupport.swift",
        source_root / "LiveBonjourResolver.swift",
        source_root / "LivePairingCoordinator.swift",
        source_root / "LivePairingView.swift",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in live_sources)

    assert "ARFrame" not in combined
    assert "CVPixelBuffer" not in combined
    assert "import ARKit" not in combined
    assert "import CoreVideo" not in combined
    assert "CaptureController" not in combined
    assert "LiveSender(" not in combined
    assert "LiveSenderQueue.open" not in combined
    assert "DataScannerViewController" in combined
    assert "dismantleUIViewController" in combined
    assert "stopScanning()" in combined
    assert ".onChange(of: coordinator.snapshot.phase)" in combined
    assert "if phase != .scanning" in combined

    content = (source_root / "ContentView.swift").read_text(encoding="utf-8")
    assert "if activeSheet != .livePairing" in content
    assert ".disabled(capture.isRecording || capture.isStarting || capture.isFinalizing)" in content
    assert "liveSender: liveSender" in content

    pairing_view = (source_root / "LivePairingView.swift").read_text(encoding="utf-8")
    assert "Abandon Pending Live Transfer" in pairing_view
    assert "try await liveSender.abandonPendingTransfer()" in pairing_view
    assert "never deleted by live transfer recovery" in pairing_view

    app = (source_root / "CaptureSplatApp.swift").read_text(encoding="utf-8")
    assert "hasPendingLiveTransfer: LivePairingCoordinator" in app
    assert "currentSessionURL: paths.currentSessionURL" in app
    assert "pendingCaptureURL: paths.pendingCaptureURL" in app


def test_pairing_sources_are_registered_once() -> None:
    repository = Path(__file__).resolve().parents[1]
    project = (
        repository
        / "apps/ios/CaptureSplat/CaptureSplat.xcodeproj/project.pbxproj"
    ).read_text(encoding="utf-8")

    for name in [
        "LiveApplicationSupport.swift",
        "LiveBonjourResolver.swift",
        "LivePairingCoordinator.swift",
        "LivePairingView.swift",
    ]:
        assert project.count(f"/* {name} in Sources */") == 2
        assert project.count(f"path = {name};") == 1

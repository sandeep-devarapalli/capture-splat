import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCES = REPOSITORY / "apps/ios/CaptureSplat/CaptureSplat/Sources"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Swift host probe requires macOS",
)


@pytest.fixture(scope="module")
def live_capture_journal_probe(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    build_root = tmp_path_factory.mktemp("live-capture-journal-probe")
    executable = build_root / "live-capture-journal-probe"
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(build_root / "clang-module-cache")
    environment["SWIFT_MODULECACHE_PATH"] = str(build_root / "swift-module-cache")
    sources = [
        SOURCES / "LiveAuthContract.swift",
        SOURCES / "LiveAuthClient.swift",
        SOURCES / "LiveSenderQueue.swift",
        SOURCES / "LiveCaptureJournal.swift",
        REPOSITORY / "tests/swift/LiveCaptureJournalProbe.swift",
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
    return executable


def run_probe(executable: Path, scenario: str, working_root: Path) -> dict[str, bool]:
    result = subprocess.run(
        [str(executable), scenario, str(working_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_journal_commit_reopen_and_duplicate_handling(
    live_capture_journal_probe: Path,
    tmp_path: Path,
) -> None:
    assert all(run_probe(live_capture_journal_probe, "lifecycle", tmp_path).values())


def test_journal_rejects_gaps_nonfinite_values_and_unsafe_evidence(
    live_capture_journal_probe: Path,
    tmp_path: Path,
) -> None:
    assert all(run_probe(live_capture_journal_probe, "invalid", tmp_path).values())


def test_journal_rejects_corruption_and_only_ignores_exact_incoming_files(
    live_capture_journal_probe: Path,
    tmp_path: Path,
) -> None:
    assert all(run_probe(live_capture_journal_probe, "corruption", tmp_path).values())


def test_journal_finalization_is_exact_immutable_and_terminal(
    live_capture_journal_probe: Path,
    tmp_path: Path,
) -> None:
    assert all(run_probe(live_capture_journal_probe, "finalization", tmp_path).values())


def test_capture_notifies_live_sender_only_after_journal_commit() -> None:
    controller = (SOURCES / "CaptureController.swift").read_text(encoding="utf-8")
    frame_commit = "try LiveCaptureJournal.commitAcceptedFrame(liveFrameEvent)"
    frame_notify = "self.liveSenderEventSink?.frameCommitted(liveFrameEvent)"
    preview = "let previewSamples = self.makePointCloudPreviewSamples("
    final_commit = "try LiveCaptureJournal.commitFinalization(liveFinalization)"
    final_notify = "liveSenderEventSink?.captureFinalized(liveFinalization)"

    assert controller.index(frame_commit) < controller.index(frame_notify)
    assert controller.index(frame_notify) < controller.index(preview)
    assert controller.index(final_commit) < controller.index(final_notify)
    assert "DispatchQueue.main.sync {" not in controller
    assert "DispatchQueue.main.async {" in controller
    assert 'self.appendSessionEvent(\n                        "live_journal_frame_failed"' in controller
    assert 'appendSessionEvent("live_journal_finalization_failed"' in controller
    assert "let featurePointCount = latestFeaturePointCount" in controller
    assert "featurePointCount: featurePointCount" in controller


def test_live_capture_journal_is_registered_once() -> None:
    project = (
        REPOSITORY
        / "apps/ios/CaptureSplat/CaptureSplat.xcodeproj/project.pbxproj"
    ).read_text(encoding="utf-8")

    assert project.count("/* LiveCaptureJournal.swift in Sources */") == 2
    assert project.count("path = LiveCaptureJournal.swift;") == 1

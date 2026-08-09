import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Swift host probe requires macOS")
REPOSITORY = Path(__file__).resolve().parents[1]
SOURCES = REPOSITORY / "apps/ios/CaptureSplat/CaptureSplat/Sources"


def _swift_function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated Swift function: {signature}")


def test_keyframe_policy_boundaries_and_invalid_pose_are_deterministic(tmp_path: Path) -> None:
    executable = tmp_path / "capture-keyframe-policy-probe"
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(tmp_path / "clang-module-cache")
    environment["SWIFT_MODULECACHE_PATH"] = str(tmp_path / "swift-module-cache")
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            str(SOURCES / "CaptureKeyframePolicy.swift"),
            str(REPOSITORY / "tests/swift/CaptureKeyframePolicyProbe.swift"),
            "-o",
            str(executable),
        ],
        check=True,
        env=environment,
    )
    output = json.loads(subprocess.run(
        [str(executable)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout)

    assert output["video_intervals"] == [0.2, 0.2, 0.5, 1]
    assert output["standard_interval"] == 0.5
    cases = output["cases"]
    assert cases["first"]["disposition"] == "evaluateQuality"
    assert cases["new_below"] == {
        "disposition": "waitForMovement",
        "observed": 0.049999,
        "remaining_cm": 1,
        "required": 0.05,
    }
    assert cases["new_exact"]["disposition"] == "evaluateQuality"
    assert cases["same_below"]["disposition"] == "waitForMovement"
    assert cases["same_below"]["required"] == pytest.approx(0.07)
    assert cases["same_exact"]["disposition"] == "evaluateQuality"
    assert cases["negative"]["disposition"] == "waitForMovement"
    assert cases["negative"]["observed"] == 0
    assert cases["nan"]["disposition"] == "invalidPose"
    assert cases["infinity"]["disposition"] == "invalidPose"
    assert cases["invalid_policy"]["disposition"] == "invalidPolicy"


def test_capture_controller_arms_novelty_before_unchanged_quality_gate() -> None:
    source = (SOURCES / "CaptureController.swift").read_text(encoding="utf-8")
    content = (SOURCES / "ContentView.swift").read_text(encoding="utf-8")
    project = (
        REPOSITORY / "apps/ios/CaptureSplat/CaptureSplat.xcodeproj/project.pbxproj"
    ).read_text(encoding="utf-8")

    candidate_gate = source.index(
        "frame.timestamp - lastCandidateFrameTimestamp >= activeKeyframeCandidateInterval"
    )
    novelty_gate = source.index("CaptureKeyframePolicy.videoNoveltyDecision", candidate_gate)
    candidate_depth = source.index("if !shouldRefreshLiveGuidance", novelty_gate)
    image_quality = source.index("let frameQuality = estimateFrameQuality", candidate_depth)
    quality_gate = source.index("let keyframeDecision = evaluateKeyframeCandidate", image_quality)
    assert candidate_gate < novelty_gate < candidate_depth < image_quality < quality_gate
    assert source.index("!cameraTransformIsFinite", candidate_gate) < novelty_gate
    assert 'if scanTargetMode == "video_3dgs" {' in source
    assert 'case .waitForMovement where trackingStatus == "normal":' in source

    novelty_recorder = _swift_function(source, "private func recordNoveltyWait(")
    assert "recordKeyframeEvent" not in novelty_recorder
    assert "skippedKeyframes" not in novelty_recorder
    assert '"skipped_keyframe_candidates": skippedKeyframes' in source
    assert '"quality_gate_hold_count": skippedKeyframes' in source
    assert '"novelty_wait_observations": noveltyWaits' in source
    assert '"candidate_interval_policy"' in source

    evaluator = _swift_function(source, "private func evaluateKeyframeCandidate(")
    assert hashlib.sha256(evaluator.encode()).hexdigest() == (
        "ac9b07749da0921589a67c3dc503cdb72d07343322b547be6bd5f18d3a1d82b3"
    )
    for threshold in (
        "private let minBlurScore = 0.006",
        "private let maxAngularVelocityDegPerSec = 40.0",
        "private let maxTranslationSpeedMetersPerSec = 0.8",
        "private let minFeatureGridCoverage = 0.25",
        "private let minVideoParallaxMeters = 0.05",
        "private let videoKeyframeScoreThreshold = 0.68",
        "moved < requiredParallax * 1.4",
    ):
        assert threshold in source

    assert 'case "too_similar_to_last_keyframe":' in source
    assert 'case "score_below_threshold":' in source
    assert "Hold steady on textured detail until you feel a haptic." in source
    assert "Move like a slow video" not in content
    assert "quality holds" in content
    assert "move waits" in content
    assert ") skipped" not in content
    assert "droppedFrames > 0 && rgbRate < 1.5" not in source
    assert "systemUptime - lastWriterBusyDropUptime < 1" in source
    assert "Thermal Serious: any active live transfer is paused" in source
    assert 'else if thermalStateText == "serious"' in source
    assert "Capture continues." not in source
    assert "Recording continues." not in source
    assert project.count("CaptureKeyframePolicy.swift in Sources") == 2
    assert project.count("path = CaptureKeyframePolicy.swift") == 1


def test_thermal_pause_exposes_manual_local_export_without_abandoning_resume() -> None:
    content = (SOURCES / "ContentView.swift").read_text(encoding="utf-8")
    fallback = _swift_function(content, "private var liveThermalFallbackCard")

    fallback_invocation = content.index("if showsThermalLocalExportFallback {")
    expanded_branch = content.index("if isCapturePanelExpanded {")
    assert fallback_invocation < expanded_branch
    assert "livePairing.snapshot.hasCurrentPairing" in content
    assert 'capture.thermalStateText == "serious"' in content
    assert 'capture.thermalStateText == "critical"' in content
    assert "Thermal local-export mode" in fallback
    assert "Manual Export" in fallback
    assert "ShareLink(item: directory)" in fallback
    assert "capture.isCapturePackageReady" in fallback
    assert "capture.hasRecoverablePartialCapture" in fallback
    assert "pending transfer" in content
    assert "abandonPendingTransfer" not in fallback
    assert "stopRecording()" not in fallback

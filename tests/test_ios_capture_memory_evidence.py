from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
CONTROLLER = (
    REPOSITORY
    / "apps/ios/CaptureSplat/CaptureSplat/Sources/CaptureController.swift"
)


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


def test_capture_memory_probe_is_bounded_and_off_the_arkit_callback() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")
    frame_callback = _swift_function(source, "func session(_ session: ARSession, didUpdate frame: ARFrame)")
    scheduler = _swift_function(source, "private func scheduleProcessMemorySampleIfNeeded(")
    probe = _swift_function(source, "private static func readProcessMemory()")

    assert "private let processMemorySampleIntervalSeconds: TimeInterval = 2.0" in source
    assert "scheduleProcessMemorySampleIfNeeded(at: ProcessInfo.processInfo.systemUptime)" in frame_callback
    assert "task_info(" not in frame_callback
    assert "processMemoryProbeQueue.async" in scheduler
    assert "DispatchQueue.main.async" in scheduler
    assert "processMemoryProbeInFlight" in scheduler
    assert "uptime - lastProcessMemoryProbeUptime >= processMemorySampleIntervalSeconds" in scheduler
    assert "task_info(" in probe
    assert "TASK_VM_INFO" in probe
    assert "information.phys_footprint" in probe
    assert "information.resident_size" in probe


def test_capture_memory_evidence_resets_and_is_written_honestly() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")
    reset = _swift_function(source, "private func resetProcessMemoryEvidence(")
    report = _swift_function(source, "private func processMemoryReport()")
    writer = _swift_function(source, "private func writeMetadata()")

    assert "processMemoryProbeGeneration &+= 1" in reset
    assert "processMemorySampleCount = 0" in reset
    assert "peakPhysicalFootprintBytes = nil" in reset
    assert "peakResidentBytes = nil" in reset
    assert '"status": processMemoryProbeStatus' in report
    assert '"sample_count": processMemorySampleCount' in report
    assert '"current_physical_footprint_bytes"' in report
    assert '"peak_physical_footprint_bytes"' in report
    assert '"current_resident_bytes"' in report
    assert '"peak_resident_bytes"' in report
    assert "NSNull()" in report
    assert '"capture_duration_seconds": captureDuration' in writer
    assert '"memory": processMemoryReport()' in writer

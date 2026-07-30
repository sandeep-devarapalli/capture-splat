import Foundation
import XCTest

final class LiveSenderAckBenchmarkTests: XCTestCase {
    private static let maximumStdoutBytes = 64 * 1024
    private static let stdoutMarker = "CAPTURE_SPLAT_ACK_BENCHMARK_JSON="

    func testReconcileAcknowledgedFrames360() async throws {
        try await runReconcileBenchmark(acknowledgedFrameCount: 360)
    }

    func testReconcileAcknowledgedFrames720() async throws {
        try await runReconcileBenchmark(acknowledgedFrameCount: 720)
    }

    func testReconcileAcknowledgedFrames1000() async throws {
        try await runReconcileBenchmark(acknowledgedFrameCount: 1_000)
    }

    func testReconcileAcknowledgedFrames10000() async throws {
        try await runReconcileBenchmark(acknowledgedFrameCount: 10_000)
    }

    func testReconcileAcknowledgedFrames50000() async throws {
        try await runReconcileBenchmark(acknowledgedFrameCount: 50_000)
    }

    func testColdReopenAcknowledgedFrames360() async throws {
        try await runColdReopenBenchmark(acknowledgedFrameCount: 360)
    }

    func testColdReopenAcknowledgedFrames720() async throws {
        try await runColdReopenBenchmark(acknowledgedFrameCount: 720)
    }

    func testColdReopenAcknowledgedFrames1000() async throws {
        try await runColdReopenBenchmark(acknowledgedFrameCount: 1_000)
    }

    func testColdReopenAcknowledgedFrames10000() async throws {
        try await runColdReopenBenchmark(acknowledgedFrameCount: 10_000)
    }

    func testColdReopenAcknowledgedFrames50000() async throws {
        try await runColdReopenBenchmark(acknowledgedFrameCount: 50_000)
    }

    func testUnpacedAcknowledgementStream720() async throws {
        let workspaceURL = temporaryWorkspace("unpaced-stream")
        defer {
            try? FileManager.default.removeItem(at: workspaceURL)
        }
        let result = try await LiveSenderAckBenchmarkCore.runUnpacedStream(
            finalAcknowledgedFrameCount: 720,
            workspaceURL: workspaceURL
        )
        XCTAssertTrue(result.platform.optimizedBuild)
        XCTAssertEqual(result.acknowledgementDurationsNanoseconds.count, 720)
        XCTAssertEqual(result.persistedState.acknowledgedFrameCount, 720)
        XCTAssertEqual(result.persistedState.pendingFrameCount, 0)
        assertFailClosedGate(
            result.gateResult,
            platform: result.platform
        )
        try attach(
            result,
            name: "capture_splat.live_sender_ack_benchmark.unpaced.720.json"
        )
    }

    func testPacedAcknowledgementStream720() async throws {
        let workspaceURL = temporaryWorkspace("paced-stream")
        defer {
            try? FileManager.default.removeItem(at: workspaceURL)
        }
        let configuration = try LiveSenderAckBenchmarkPacedConfiguration(
            initialAcknowledgedFrameCount: 420,
            finalAcknowledgedFrameCount: 720,
            acknowledgementsPerSecond: 5,
            nominalDurationSeconds: 60
        )
        let result = try await LiveSenderAckBenchmarkCore.runPacedStream(
            configuration: configuration,
            workspaceURL: workspaceURL
        )
        XCTAssertTrue(result.platform.optimizedBuild)
        XCTAssertEqual(result.acknowledgementDurationsNanoseconds.count, 300)
        XCTAssertEqual(result.persistedState.acknowledgedFrameCount, 720)
        XCTAssertEqual(result.persistedState.pendingFrameCount, 0)
        XCTAssertEqual(result.finalBacklogFrames, 0)
        assertFailClosedGate(
            result.gateResult,
            platform: result.platform
        )
        try attach(
            result,
            name: "capture_splat.live_sender_ack_benchmark.paced.720.json"
        )
    }

    private func runColdReopenBenchmark(
        acknowledgedFrameCount: Int
    ) async throws {
        let configuration = try LiveSenderAckBenchmarkConfiguration(
            acknowledgedFrameCount: acknowledgedFrameCount,
            trialIndex: try trialIndex()
        )
        let workspaceURL = temporaryWorkspace(
            "cold-reopen-\(acknowledgedFrameCount)"
        )
        defer {
            try? FileManager.default.removeItem(at: workspaceURL)
        }
        let seededState = try LiveSenderAckBenchmarkCore.prepareCompleteState(
            configuration: configuration,
            workspaceURL: workspaceURL
        )
        let result = try await LiveSenderAckBenchmarkCore.reopenAndProbe(
            configuration: configuration,
            workspaceURL: workspaceURL
        )
        XCTAssertEqual(seededState, result.persistedState)
        XCTAssertTrue(result.platform.optimizedBuild)
        assertPlatformEligibility(result.platform)
        try attach(
            result,
            name:
                "capture_splat.live_sender_ack_benchmark.cold_reopen."
                + "\(acknowledgedFrameCount).\(configuration.trialIndex).json"
        )
    }

    private func runReconcileBenchmark(
        acknowledgedFrameCount: Int
    ) async throws {
        let configuration = try LiveSenderAckBenchmarkConfiguration(
            acknowledgedFrameCount: acknowledgedFrameCount,
            trialIndex: try trialIndex()
        )
        let workspaceURL = temporaryWorkspace(
            "reconcile-\(acknowledgedFrameCount)"
        )
        defer {
            try? FileManager.default.removeItem(at: workspaceURL)
        }
        let result = try await LiveSenderAckBenchmarkCore.prepareAndReconcile(
            configuration: configuration,
            workspaceURL: workspaceURL
        )
        XCTAssertTrue(result.platform.optimizedBuild)
        XCTAssertEqual(
            result.reconciledSequenceIDs,
            [acknowledgedFrameCount]
        )
        assertPlatformEligibility(result.platform)
        try attach(
            result,
            name:
                "capture_splat.live_sender_ack_benchmark.reconcile."
                + "\(acknowledgedFrameCount).\(configuration.trialIndex).json"
        )
    }

    private func temporaryWorkspace(_ label: String) -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent(
            "capture-splat-live-ack-benchmark-\(label)-\(UUID().uuidString)",
            isDirectory: true
        )
    }

    private func assertFailClosedGate(
        _ gateResult: String,
        platform: LiveSenderAckBenchmarkPlatform
    ) {
        if platform.isPhysicalDevice
            && platform.isDesignatedACKBenchmarkDevice
            && platform.optimizedBuild {
            XCTAssertEqual(
                gateResult,
                "measurement_passed_requires_aggregate_evaluation"
            )
        } else {
            XCTAssertTrue(gateResult.hasPrefix("not_evaluated_"))
        }
        XCTAssertNotEqual(gateResult, "passed")
    }

    private func assertPlatformEligibility(
        _ platform: LiveSenderAckBenchmarkPlatform
    ) {
        if platform.isPhysicalDevice
            && platform.isDesignatedACKBenchmarkDevice
            && platform.optimizedBuild {
            XCTAssertEqual(
                platform.physicalGateResult,
                "physical_trial_requires_aggregate_gate_evaluation"
            )
        } else {
            XCTAssertNotEqual(
                platform.physicalGateResult,
                "physical_trial_requires_aggregate_gate_evaluation"
            )
        }
    }

    private func attach<T: Encodable>(
        _ result: T,
        name: String
    ) throws {
        let json = try LiveSenderAckBenchmarkCore.canonicalJSONData(result)
        let attachment = XCTAttachment(
            data: json,
            uniformTypeIdentifier: "public.json"
        )
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)

        let stdoutBytes = Self.stdoutMarker.utf8.count + json.count + 1
        XCTAssertLessThanOrEqual(
            stdoutBytes,
            Self.maximumStdoutBytes,
            "Canonical benchmark evidence exceeded the bounded stdout budget."
        )
        guard stdoutBytes <= Self.maximumStdoutBytes else {
            return
        }
        let jsonString = try XCTUnwrap(String(data: json, encoding: .utf8))
        print(Self.stdoutMarker + jsonString)
    }

    private func trialIndex() throws -> Int {
        guard let rawValue = ProcessInfo.processInfo.environment[
            "CAPTURE_SPLAT_ACK_BENCHMARK_TRIAL_INDEX"
        ] else {
            return 0
        }
        return try XCTUnwrap(
            Int(rawValue),
            "CAPTURE_SPLAT_ACK_BENCHMARK_TRIAL_INDEX must be a non-negative integer."
        )
    }
}

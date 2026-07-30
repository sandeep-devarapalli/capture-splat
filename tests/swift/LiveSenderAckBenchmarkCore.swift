import CryptoKit
import Darwin
import Foundation

public struct LiveSenderAckBenchmarkConfiguration: Codable, Equatable, Sendable {
    public let acknowledgedFrameCount: Int
    public let trialIndex: Int

    public init(acknowledgedFrameCount: Int, trialIndex: Int) throws {
        guard acknowledgedFrameCount > 0,
              acknowledgedFrameCount <= 99_999_999,
              trialIndex >= 0 else {
            throw LiveSenderAckBenchmarkError.invalidConfiguration
        }
        self.acknowledgedFrameCount = acknowledgedFrameCount
        self.trialIndex = trialIndex
    }

    enum CodingKeys: String, CodingKey {
        case acknowledgedFrameCount = "acknowledged_frame_count"
        case trialIndex = "trial_index"
    }
}

public struct LiveSenderAckBenchmarkStateEvidence: Codable, Equatable, Sendable {
    public let payloadBytes: Int
    public let envelopeBytes: Int
    public let payloadSHA256: String
    public let envelopeSHA256: String
    public let acknowledgedFrameCount: Int
    public let pendingFrameCount: Int

    enum CodingKeys: String, CodingKey {
        case payloadBytes = "payload_bytes"
        case envelopeBytes = "envelope_bytes"
        case payloadSHA256 = "payload_sha256"
        case envelopeSHA256 = "envelope_sha256"
        case acknowledgedFrameCount = "acknowledged_frame_count"
        case pendingFrameCount = "pending_frame_count"
    }
}

public struct LiveSenderAckBenchmarkQueueLimitsEvidence:
    Codable, Equatable, Sendable {
    public let maximumFrames: Int
    public let maximumBytes: Int64
    public let maximumInFlight: Int
    public let scope: String

    enum CodingKeys: String, CodingKey {
        case maximumFrames = "maximum_frames"
        case maximumBytes = "maximum_bytes"
        case maximumInFlight = "maximum_in_flight"
        case scope
    }
}

public struct LiveSenderAckBenchmarkProcessEvidence:
    Codable, Equatable, Sendable {
    public let launchID: String
    public let processID: Int

    enum CodingKeys: String, CodingKey {
        case launchID = "launch_id"
        case processID = "process_id"
    }
}

public struct LiveSenderAckBenchmarkSequenceProbe: Codable, Equatable, Sendable {
    public let sequenceID: Int
    public let identicalDisposition: String
    public let conflictingReferenceRejected: Bool

    enum CodingKeys: String, CodingKey {
        case sequenceID = "sequence_id"
        case identicalDisposition = "identical_disposition"
        case conflictingReferenceRejected = "conflicting_reference_rejected"
    }
}

public struct LiveSenderAckBenchmarkCorrectness: Codable, Equatable, Sendable {
    public let productionOpenValidatedSeed: Bool
    public let productionReopenValidatedPersistedState: Bool
    public let reconciledSequenceIDs: [Int]
    public let sequenceProbes: [LiveSenderAckBenchmarkSequenceProbe]

    enum CodingKeys: String, CodingKey {
        case productionOpenValidatedSeed = "production_open_validated_seed"
        case productionReopenValidatedPersistedState =
            "production_reopen_validated_persisted_state"
        case reconciledSequenceIDs = "reconciled_sequence_ids"
        case sequenceProbes = "sequence_probes"
    }
}

public struct LiveSenderAckBenchmarkPlatform: Codable, Equatable, Sendable {
    public let operatingSystem: String
    public let operatingSystemVersion: String
    public let machine: String
    public let architecture: String
    public let thermalState: String
    public let isPhysicalDevice: Bool
    public let isOldestSupportedLiDARiPhone: Bool
    public let optimizedBuild: Bool
    public let physicalGateResult: String

    enum CodingKeys: String, CodingKey {
        case operatingSystem = "operating_system"
        case operatingSystemVersion = "operating_system_version"
        case machine, architecture
        case thermalState = "thermal_state"
        case isPhysicalDevice = "is_physical_device"
        case isOldestSupportedLiDARiPhone = "is_oldest_supported_lidar_iphone"
        case optimizedBuild = "optimized_build"
        case physicalGateResult = "physical_gate_result"
    }
}

public struct LiveSenderAckBenchmarkMemory: Codable, Equatable, Sendable {
    public let footprintBeforeBytes: UInt64
    public let footprintAfterReconcileBytes: UInt64
    public let footprintAfterReopenBytes: UInt64
    public let maximumObservedFootprintBytes: UInt64
    public let maximumObservedDeltaBytes: Int64
    public let kernelReportedPeakFootprintBytes: UInt64

    enum CodingKeys: String, CodingKey {
        case footprintBeforeBytes = "footprint_before_bytes"
        case footprintAfterReconcileBytes = "footprint_after_reconcile_bytes"
        case footprintAfterReopenBytes = "footprint_after_reopen_bytes"
        case maximumObservedFootprintBytes = "maximum_observed_footprint_bytes"
        case maximumObservedDeltaBytes = "maximum_observed_delta_bytes"
        case kernelReportedPeakFootprintBytes =
            "kernel_reported_peak_footprint_bytes"
    }
}

public struct LiveSenderAckBenchmarkPhaseMemory: Codable, Equatable, Sendable {
    public let footprintBeforeBytes: UInt64
    public let footprintAfterBytes: UInt64
    public let footprintDeltaBytes: Int64
    public let kernelReportedPeakFootprintBytes: UInt64

    enum CodingKeys: String, CodingKey {
        case footprintBeforeBytes = "footprint_before_bytes"
        case footprintAfterBytes = "footprint_after_bytes"
        case footprintDeltaBytes = "footprint_delta_bytes"
        case kernelReportedPeakFootprintBytes =
            "kernel_reported_peak_footprint_bytes"
    }
}

public struct LiveSenderAckBenchmarkThermalStates: Codable, Equatable, Sendable {
    public let before: String
    public let after: String
}

public struct LiveSenderAckBenchmarkStreamCorrectness: Codable, Equatable, Sendable {
    public let productionOpenValidatedExternalState: Bool
    public let everyAcknowledgementReconciledExactlyOneFrame: Bool
    public let sequenceProbes: [LiveSenderAckBenchmarkSequenceProbe]

    enum CodingKeys: String, CodingKey {
        case productionOpenValidatedExternalState =
            "production_open_validated_external_state"
        case everyAcknowledgementReconciledExactlyOneFrame =
            "every_acknowledgement_reconciled_exactly_one_frame"
        case sequenceProbes = "sequence_probes"
    }
}

public struct LiveSenderAckBenchmarkUnpacedStreamPhase:
    Codable, Equatable, Sendable {
    public let schema: String
    public let finalAcknowledgedFrameCount: Int
    public let process: LiveSenderAckBenchmarkProcessEvidence
    public let queueLimits: LiveSenderAckBenchmarkQueueLimitsEvidence
    public let seedState: LiveSenderAckBenchmarkStateEvidence
    public let persistedState: LiveSenderAckBenchmarkStateEvidence
    public let acknowledgementDurationsNanoseconds: [UInt64]
    public let elapsedNanoseconds: UInt64
    public let durableAcknowledgementsPerSecond: Double
    public let memory: LiveSenderAckBenchmarkPhaseMemory
    public let thermalStates: LiveSenderAckBenchmarkThermalStates
    public let platform: LiveSenderAckBenchmarkPlatform
    public let correctness: LiveSenderAckBenchmarkStreamCorrectness
    public let gateResult: String

    enum CodingKeys: String, CodingKey {
        case schema
        case finalAcknowledgedFrameCount = "final_acknowledged_frame_count"
        case process
        case queueLimits = "queue_limits"
        case seedState = "seed_state"
        case persistedState = "persisted_state"
        case acknowledgementDurationsNanoseconds =
            "acknowledgement_durations_nanoseconds"
        case elapsedNanoseconds = "elapsed_nanoseconds"
        case durableAcknowledgementsPerSecond =
            "durable_acknowledgements_per_second"
        case memory
        case thermalStates = "thermal_states"
        case platform, correctness
        case gateResult = "gate_result"
    }
}

public struct LiveSenderAckBenchmarkPacedConfiguration:
    Codable, Equatable, Sendable {
    public let initialAcknowledgedFrameCount: Int
    public let finalAcknowledgedFrameCount: Int
    public let acknowledgementCount: Int
    public let acknowledgementsPerSecond: Int
    public let nominalDurationSeconds: Int

    public init(
        initialAcknowledgedFrameCount: Int,
        finalAcknowledgedFrameCount: Int,
        acknowledgementsPerSecond: Int,
        nominalDurationSeconds: Int
    ) throws {
        let (acknowledgementCount, countOverflow) =
            acknowledgementsPerSecond.multipliedReportingOverflow(
                by: nominalDurationSeconds
            )
        guard initialAcknowledgedFrameCount >= 0,
              finalAcknowledgedFrameCount > initialAcknowledgedFrameCount,
              finalAcknowledgedFrameCount <= 99_999_999,
              acknowledgementsPerSecond > 0,
              nominalDurationSeconds > 0,
              !countOverflow,
              acknowledgementCount
                == finalAcknowledgedFrameCount - initialAcknowledgedFrameCount else {
            throw LiveSenderAckBenchmarkError.invalidConfiguration
        }
        self.initialAcknowledgedFrameCount = initialAcknowledgedFrameCount
        self.finalAcknowledgedFrameCount = finalAcknowledgedFrameCount
        self.acknowledgementCount = acknowledgementCount
        self.acknowledgementsPerSecond = acknowledgementsPerSecond
        self.nominalDurationSeconds = nominalDurationSeconds
    }

    enum CodingKeys: String, CodingKey {
        case initialAcknowledgedFrameCount =
            "initial_acknowledged_frame_count"
        case finalAcknowledgedFrameCount = "final_acknowledged_frame_count"
        case acknowledgementCount = "acknowledgement_count"
        case acknowledgementsPerSecond = "acknowledgements_per_second"
        case nominalDurationSeconds = "nominal_duration_seconds"
    }
}

public struct LiveSenderAckBenchmarkPacedStreamPhase:
    Codable, Equatable, Sendable {
    public let schema: String
    public let configuration: LiveSenderAckBenchmarkPacedConfiguration
    public let process: LiveSenderAckBenchmarkProcessEvidence
    public let queueLimits: LiveSenderAckBenchmarkQueueLimitsEvidence
    public let seedState: LiveSenderAckBenchmarkStateEvidence
    public let persistedState: LiveSenderAckBenchmarkStateEvidence
    public let acknowledgementDurationsNanoseconds: [UInt64]
    public let elapsedNanoseconds: UInt64
    public let drainDurationNanoseconds: UInt64
    public let maximumBacklogFrames: Int
    public let backlogAtNominalEndFrames: Int
    public let finalBacklogFrames: Int
    public let memory: LiveSenderAckBenchmarkPhaseMemory
    public let thermalStates: LiveSenderAckBenchmarkThermalStates
    public let platform: LiveSenderAckBenchmarkPlatform
    public let correctness: LiveSenderAckBenchmarkStreamCorrectness
    public let gateResult: String

    enum CodingKeys: String, CodingKey {
        case schema, configuration, process
        case queueLimits = "queue_limits"
        case seedState = "seed_state"
        case persistedState = "persisted_state"
        case acknowledgementDurationsNanoseconds =
            "acknowledgement_durations_nanoseconds"
        case elapsedNanoseconds = "elapsed_nanoseconds"
        case drainDurationNanoseconds = "drain_duration_nanoseconds"
        case maximumBacklogFrames = "maximum_backlog_frames"
        case backlogAtNominalEndFrames = "backlog_at_nominal_end_frames"
        case finalBacklogFrames = "final_backlog_frames"
        case memory
        case thermalStates = "thermal_states"
        case platform, correctness
        case gateResult = "gate_result"
    }
}

public struct LiveSenderAckBenchmarkReconcilePhase: Codable, Equatable, Sendable {
    public let schema: String
    public let configuration: LiveSenderAckBenchmarkConfiguration
    public let process: LiveSenderAckBenchmarkProcessEvidence
    public let queueLimits: LiveSenderAckBenchmarkQueueLimitsEvidence
    public let seedState: LiveSenderAckBenchmarkStateEvidence
    public let persistedState: LiveSenderAckBenchmarkStateEvidence
    public let reconcileDurationNanoseconds: UInt64
    public let memory: LiveSenderAckBenchmarkPhaseMemory
    public let platform: LiveSenderAckBenchmarkPlatform
    public let reconciledSequenceIDs: [Int]

    enum CodingKeys: String, CodingKey {
        case schema, configuration, process
        case queueLimits = "queue_limits"
        case seedState = "seed_state"
        case persistedState = "persisted_state"
        case reconcileDurationNanoseconds = "reconcile_duration_nanoseconds"
        case memory, platform
        case reconciledSequenceIDs = "reconciled_sequence_ids"
    }
}

public struct LiveSenderAckBenchmarkReopenPhase: Codable, Equatable, Sendable {
    public let schema: String
    public let configuration: LiveSenderAckBenchmarkConfiguration
    public let process: LiveSenderAckBenchmarkProcessEvidence
    public let queueLimits: LiveSenderAckBenchmarkQueueLimitsEvidence
    public let persistedState: LiveSenderAckBenchmarkStateEvidence
    public let reopenDurationNanoseconds: UInt64
    public let memory: LiveSenderAckBenchmarkPhaseMemory
    public let platform: LiveSenderAckBenchmarkPlatform
    public let sequenceProbes: [LiveSenderAckBenchmarkSequenceProbe]

    enum CodingKeys: String, CodingKey {
        case schema, configuration, process
        case queueLimits = "queue_limits"
        case persistedState = "persisted_state"
        case reopenDurationNanoseconds = "reopen_duration_nanoseconds"
        case memory, platform
        case sequenceProbes = "sequence_probes"
    }
}

public struct LiveSenderAckBenchmarkTrial: Codable, Equatable, Sendable {
    public let schema: String
    public let configuration: LiveSenderAckBenchmarkConfiguration
    public let queueLimits: LiveSenderAckBenchmarkQueueLimitsEvidence
    public let seedState: LiveSenderAckBenchmarkStateEvidence
    public let persistedState: LiveSenderAckBenchmarkStateEvidence
    public let reconcileDurationNanoseconds: UInt64
    public let reopenDurationNanoseconds: UInt64
    public let memory: LiveSenderAckBenchmarkMemory
    public let platform: LiveSenderAckBenchmarkPlatform
    public let correctness: LiveSenderAckBenchmarkCorrectness

    enum CodingKeys: String, CodingKey {
        case schema, configuration
        case queueLimits = "queue_limits"
        case seedState = "seed_state"
        case persistedState = "persisted_state"
        case reconcileDurationNanoseconds = "reconcile_duration_nanoseconds"
        case reopenDurationNanoseconds = "reopen_duration_nanoseconds"
        case memory, platform, correctness
    }
}

public enum LiveSenderAckBenchmarkError: Error, Equatable, LocalizedError, Sendable {
    case invalidConfiguration
    case workspaceNotEmpty
    case invalidSeedState(String)
    case correctnessFailure(String)
    case memoryProbeFailed

    public var errorDescription: String? {
        switch self {
        case .invalidConfiguration:
            return "The ACK benchmark configuration is invalid."
        case .workspaceNotEmpty:
            return "The ACK benchmark workspace must not already contain benchmark state."
        case .invalidSeedState(let message):
            return "The externally seeded sender state is invalid: \(message)"
        case .correctnessFailure(let message):
            return "The ACK benchmark correctness check failed: \(message)"
        case .memoryProbeFailed:
            return "Darwin TASK_VM_INFO did not provide a physical footprint."
        }
    }
}

public enum LiveSenderAckBenchmarkCore {
    public static let trialSchema = "capture_splat.live_sender_ack_benchmark_trial.v0.1"
    public static let reconcilePhaseSchema =
        "capture_splat.live_sender_ack_benchmark_reconcile_phase.v0.1"
    public static let reopenPhaseSchema =
        "capture_splat.live_sender_ack_benchmark_reopen_phase.v0.1"
    public static let unpacedStreamPhaseSchema =
        "capture_splat.live_sender_ack_benchmark_unpaced_stream_phase.v0.1"
    public static let pacedStreamPhaseSchema =
        "capture_splat.live_sender_ack_benchmark_paced_stream_phase.v0.1"
    private static let processEvidence = LiveSenderAckBenchmarkProcessEvidence(
        launchID: UUID().uuidString.lowercased(),
        processID: Int(Darwin.getpid())
    )

    public static func run(
        configuration: LiveSenderAckBenchmarkConfiguration,
        workspaceURL: URL
    ) async throws -> LiveSenderAckBenchmarkTrial {
        let reconcileWorkspace = workspaceURL.appendingPathComponent(
            "reconcile",
            isDirectory: true
        )
        let reopenWorkspace = workspaceURL.appendingPathComponent(
            "cold-reopen",
            isDirectory: true
        )
        let reconcile = try await prepareAndReconcile(
            configuration: configuration,
            workspaceURL: reconcileWorkspace
        )
        let externallySeededState = try prepareCompleteState(
            configuration: configuration,
            workspaceURL: reopenWorkspace
        )
        let reopen = try await reopenAndProbe(
            configuration: configuration,
            workspaceURL: reopenWorkspace
        )
        guard externallySeededState == reopen.persistedState,
              reconcile.persistedState == reopen.persistedState else {
            throw LiveSenderAckBenchmarkError.correctnessFailure(
                "the externally seeded complete state differs from production persistence"
            )
        }
        let maximumFootprint = max(
            reconcile.memory.footprintBeforeBytes,
            reconcile.memory.footprintAfterBytes,
            reopen.memory.footprintBeforeBytes,
            reopen.memory.footprintAfterBytes
        )
        return LiveSenderAckBenchmarkTrial(
            schema: trialSchema,
            configuration: configuration,
            queueLimits: reopen.queueLimits,
            seedState: reconcile.seedState,
            persistedState: reopen.persistedState,
            reconcileDurationNanoseconds: reconcile.reconcileDurationNanoseconds,
            reopenDurationNanoseconds: reopen.reopenDurationNanoseconds,
            memory: LiveSenderAckBenchmarkMemory(
                footprintBeforeBytes: reconcile.memory.footprintBeforeBytes,
                footprintAfterReconcileBytes: reconcile.memory.footprintAfterBytes,
                footprintAfterReopenBytes: reopen.memory.footprintAfterBytes,
                maximumObservedFootprintBytes: maximumFootprint,
                maximumObservedDeltaBytes: max(
                    reconcile.memory.footprintDeltaBytes,
                    reopen.memory.footprintDeltaBytes
                ),
                kernelReportedPeakFootprintBytes: max(
                    reconcile.memory.kernelReportedPeakFootprintBytes,
                    reopen.memory.kernelReportedPeakFootprintBytes
                )
            ),
            platform: reopen.platform,
            correctness: LiveSenderAckBenchmarkCorrectness(
                productionOpenValidatedSeed: true,
                productionReopenValidatedPersistedState: true,
                reconciledSequenceIDs: reconcile.reconciledSequenceIDs,
                sequenceProbes: reopen.sequenceProbes
            )
        )
    }

    public static func prepareAndReconcile(
        configuration: LiveSenderAckBenchmarkConfiguration,
        workspaceURL: URL
    ) async throws -> LiveSenderAckBenchmarkReconcilePhase {
        let workspace = workspaceURL.standardizedFileURL
        let captureRoot = workspace.appendingPathComponent("capture", isDirectory: true)
        let stateURL = workspace.appendingPathComponent("state/queue.json")
        guard !FileManager.default.fileExists(atPath: stateURL.path) else {
            throw LiveSenderAckBenchmarkError.workspaceNotEmpty
        }
        try FileManager.default.createDirectory(
            at: captureRoot,
            withIntermediateDirectories: true
        )

        let fixture = try makeFixture(
            acknowledgedFrameCount: configuration.acknowledgedFrameCount,
            captureRoot: captureRoot
        )
        let seededData = try makeSeedEnvelope(
            session: fixture.session,
            frames: [fixture.lastFrame],
            acknowledgedFrames: fixture.acknowledgedFrames,
            expectedFrameCount: configuration.acknowledgedFrameCount
        )
        try FileManager.default.createDirectory(
            at: stateURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try seededData.write(to: stateURL, options: [.atomic])
        let seedEvidence = try stateEvidence(stateURL)

        let limits = try queueLimits(configuration.acknowledgedFrameCount)
        let queue = try await LiveSenderQueue.open(
            captureRoot: captureRoot,
            stateURL: stateURL,
            limits: limits,
            session: fixture.session
        )

        let before = try physicalFootprint()
        let acknowledgement = try LiveSenderAcknowledgement(
            sessionID: fixture.session.sessionID,
            operation: .resume,
            status: .accepted,
            receivedCount: configuration.acknowledgedFrameCount,
            contiguousCount: configuration.acknowledgedFrameCount,
            pendingCount: 0,
            expectedFrameCount: configuration.acknowledgedFrameCount,
            nextExpectedSequenceID: configuration.acknowledgedFrameCount + 1,
            missingRanges: [],
            finalized: false
        )
        let reconcileStart = DispatchTime.now().uptimeNanoseconds
        let reconciliation = try await queue.reconcile(acknowledgement)
        let reconcileDuration = DispatchTime.now().uptimeNanoseconds - reconcileStart
        let afterReconcile = try physicalFootprint()

        let persistedEvidence = try stateEvidence(stateURL)
        guard persistedEvidence.acknowledgedFrameCount
                == configuration.acknowledgedFrameCount,
              persistedEvidence.pendingFrameCount == 0 else {
            throw LiveSenderAckBenchmarkError.correctnessFailure(
                "the persisted ledger does not contain the expected exact identities"
            )
        }

        return LiveSenderAckBenchmarkReconcilePhase(
            schema: reconcilePhaseSchema,
            configuration: configuration,
            process: processEvidence,
            queueLimits: queueLimitsEvidence(limits),
            seedState: seedEvidence,
            persistedState: persistedEvidence,
            reconcileDurationNanoseconds: reconcileDuration,
            memory: phaseMemory(before: before, after: afterReconcile),
            platform: platform(),
            reconciledSequenceIDs: reconciliation.acknowledgedSequenceIDs
        )
    }

    public static func prepareCompleteState(
        configuration: LiveSenderAckBenchmarkConfiguration,
        workspaceURL: URL
    ) throws -> LiveSenderAckBenchmarkStateEvidence {
        let seeded = try prepareExternalState(
            initialAcknowledgedFrameCount: configuration.acknowledgedFrameCount,
            finalAcknowledgedFrameCount: configuration.acknowledgedFrameCount,
            workspaceURL: workspaceURL
        )
        return seeded.evidence
    }

    public static func runUnpacedStream(
        finalAcknowledgedFrameCount: Int,
        workspaceURL: URL
    ) async throws -> LiveSenderAckBenchmarkUnpacedStreamPhase {
        guard finalAcknowledgedFrameCount > 0,
              finalAcknowledgedFrameCount <= 99_999_999 else {
            throw LiveSenderAckBenchmarkError.invalidConfiguration
        }
        let seeded = try prepareExternalState(
            initialAcknowledgedFrameCount: 0,
            finalAcknowledgedFrameCount: finalAcknowledgedFrameCount,
            workspaceURL: workspaceURL
        )
        let limits = try queueLimits(finalAcknowledgedFrameCount)
        let queue = try await LiveSenderQueue.open(
            captureRoot: seeded.captureRoot,
            stateURL: seeded.stateURL,
            limits: limits,
            session: seeded.session
        )
        let before = try physicalFootprint()
        let thermalBefore = thermalState()
        let start = DispatchTime.now().uptimeNanoseconds
        var durations: [UInt64] = []
        durations.reserveCapacity(finalAcknowledgedFrameCount)
        for sequenceID in 1...finalAcknowledgedFrameCount {
            let duration = try await reconcileOne(
                sequenceID: sequenceID,
                finalAcknowledgedFrameCount: finalAcknowledgedFrameCount,
                sessionID: seeded.session.sessionID,
                queue: queue
            )
            durations.append(duration)
        }
        let elapsed = DispatchTime.now().uptimeNanoseconds - start
        let probes = try await probeExactIdentities(
            queue: queue,
            sessionID: seeded.session.sessionID,
            finalAcknowledgedFrameCount: finalAcknowledgedFrameCount
        )
        let persisted = try stateEvidence(seeded.stateURL)
        try validateCompletedStreamState(
            persisted,
            finalAcknowledgedFrameCount: finalAcknowledgedFrameCount
        )
        let after = try physicalFootprint()
        let phaseMemory = phaseMemory(before: before, after: after)
        let measuredPlatform = platform()
        let throughput = Double(finalAcknowledgedFrameCount)
            * 1_000_000_000.0 / Double(max(elapsed, 1))
        let correctness = LiveSenderAckBenchmarkStreamCorrectness(
            productionOpenValidatedExternalState: true,
            everyAcknowledgementReconciledExactlyOneFrame: true,
            sequenceProbes: probes
        )
        return LiveSenderAckBenchmarkUnpacedStreamPhase(
            schema: unpacedStreamPhaseSchema,
            finalAcknowledgedFrameCount: finalAcknowledgedFrameCount,
            process: processEvidence,
            queueLimits: queueLimitsEvidence(limits),
            seedState: seeded.evidence,
            persistedState: persisted,
            acknowledgementDurationsNanoseconds: durations,
            elapsedNanoseconds: elapsed,
            durableAcknowledgementsPerSecond: throughput,
            memory: phaseMemory,
            thermalStates: LiveSenderAckBenchmarkThermalStates(
                before: thermalBefore,
                after: thermalState()
            ),
            platform: measuredPlatform,
            correctness: correctness,
            gateResult: streamGateResult(
                platform: measuredPlatform,
                persistedState: persisted,
                acknowledgementDurationsNanoseconds: durations,
                memory: phaseMemory,
                throughput: throughput,
                maximumBacklogFrames: nil,
                finalBacklogFrames: nil
            )
        )
    }

    public static func runPacedStream(
        configuration: LiveSenderAckBenchmarkPacedConfiguration,
        workspaceURL: URL
    ) async throws -> LiveSenderAckBenchmarkPacedStreamPhase {
        let seeded = try prepareExternalState(
            initialAcknowledgedFrameCount:
                configuration.initialAcknowledgedFrameCount,
            finalAcknowledgedFrameCount:
                configuration.finalAcknowledgedFrameCount,
            workspaceURL: workspaceURL
        )
        let limits = try queueLimits(configuration.finalAcknowledgedFrameCount)
        let queue = try await LiveSenderQueue.open(
            captureRoot: seeded.captureRoot,
            stateURL: seeded.stateURL,
            limits: limits,
            session: seeded.session
        )
        let before = try physicalFootprint()
        let thermalBefore = thermalState()
        let start = DispatchTime.now().uptimeNanoseconds
        let nominalDuration = UInt64(configuration.nominalDurationSeconds)
            * 1_000_000_000
        var durations: [UInt64] = []
        var completionTimes: [UInt64] = []
        var maximumBacklog = 0
        durations.reserveCapacity(configuration.acknowledgementCount)
        completionTimes.reserveCapacity(configuration.acknowledgementCount)

        for offset in 0..<configuration.acknowledgementCount {
            let scheduledOffset = UInt64(offset) * 1_000_000_000
                / UInt64(configuration.acknowledgementsPerSecond)
            let scheduled = start + scheduledOffset
            let now = DispatchTime.now().uptimeNanoseconds
            if now < scheduled {
                try await Task.sleep(nanoseconds: scheduled - now)
            }
            let serviceStart = DispatchTime.now().uptimeNanoseconds
            maximumBacklog = max(
                maximumBacklog,
                arrivedAcknowledgementCount(
                    elapsedNanoseconds: serviceStart - start,
                    totalCount: configuration.acknowledgementCount,
                    rate: configuration.acknowledgementsPerSecond
                ) - offset
            )
            let sequenceID =
                configuration.initialAcknowledgedFrameCount + offset + 1
            durations.append(try await reconcileOne(
                sequenceID: sequenceID,
                finalAcknowledgedFrameCount:
                    configuration.finalAcknowledgedFrameCount,
                sessionID: seeded.session.sessionID,
                queue: queue
            ))
            let completion = DispatchTime.now().uptimeNanoseconds
            completionTimes.append(completion)
            maximumBacklog = max(
                maximumBacklog,
                arrivedAcknowledgementCount(
                    elapsedNanoseconds: completion - start,
                    totalCount: configuration.acknowledgementCount,
                    rate: configuration.acknowledgementsPerSecond
                ) - (offset + 1)
            )
        }

        let nominalEnd = start + nominalDuration
        let completion = DispatchTime.now().uptimeNanoseconds
        if completion < nominalEnd {
            try await Task.sleep(nanoseconds: nominalEnd - completion)
        }
        let measurementEnd = DispatchTime.now().uptimeNanoseconds
        let completedAtNominalEnd = completionTimes.filter {
            $0 <= nominalEnd
        }.count
        let backlogAtNominalEnd =
            configuration.acknowledgementCount - completedAtNominalEnd
        let drainDuration = backlogAtNominalEnd > 0
            && measurementEnd > nominalEnd
            ? measurementEnd - nominalEnd
            : 0
        let probes = try await probeExactIdentities(
            queue: queue,
            sessionID: seeded.session.sessionID,
            finalAcknowledgedFrameCount:
                configuration.finalAcknowledgedFrameCount
        )
        let persisted = try stateEvidence(seeded.stateURL)
        try validateCompletedStreamState(
            persisted,
            finalAcknowledgedFrameCount:
                configuration.finalAcknowledgedFrameCount
        )
        let after = try physicalFootprint()
        let phaseMemory = phaseMemory(before: before, after: after)
        let measuredPlatform = platform()
        let correctness = LiveSenderAckBenchmarkStreamCorrectness(
            productionOpenValidatedExternalState: true,
            everyAcknowledgementReconciledExactlyOneFrame: true,
            sequenceProbes: probes
        )
        return LiveSenderAckBenchmarkPacedStreamPhase(
            schema: pacedStreamPhaseSchema,
            configuration: configuration,
            process: processEvidence,
            queueLimits: queueLimitsEvidence(limits),
            seedState: seeded.evidence,
            persistedState: persisted,
            acknowledgementDurationsNanoseconds: durations,
            elapsedNanoseconds: measurementEnd - start,
            drainDurationNanoseconds: drainDuration,
            maximumBacklogFrames: maximumBacklog,
            backlogAtNominalEndFrames: backlogAtNominalEnd,
            finalBacklogFrames: 0,
            memory: phaseMemory,
            thermalStates: LiveSenderAckBenchmarkThermalStates(
                before: thermalBefore,
                after: thermalState()
            ),
            platform: measuredPlatform,
            correctness: correctness,
            gateResult: streamGateResult(
                platform: measuredPlatform,
                persistedState: persisted,
                acknowledgementDurationsNanoseconds: durations,
                memory: phaseMemory,
                throughput: nil,
                maximumBacklogFrames: maximumBacklog,
                finalBacklogFrames: 0
            )
        )
    }

    public static func reopenAndProbe(
        configuration: LiveSenderAckBenchmarkConfiguration,
        workspaceURL: URL
    ) async throws -> LiveSenderAckBenchmarkReopenPhase {
        let workspace = workspaceURL.standardizedFileURL
        let captureRoot = workspace.appendingPathComponent("capture", isDirectory: true)
        let stateURL = workspace.appendingPathComponent("state/queue.json")
        guard FileManager.default.fileExists(atPath: stateURL.path) else {
            throw LiveSenderAckBenchmarkError.invalidSeedState(
                "the reconciled state is missing"
            )
        }
        let session = try makeSession(
            acknowledgedFrameCount: configuration.acknowledgedFrameCount,
            captureRoot: captureRoot,
            writeEvidence: false
        )
        let limits = try queueLimits(configuration.acknowledgedFrameCount)
        let before = try physicalFootprint()
        let reopenStart = DispatchTime.now().uptimeNanoseconds
        let reopened = try await LiveSenderQueue.open(
            captureRoot: captureRoot,
            stateURL: stateURL,
            limits: limits,
            session: session
        )
        let reopenDuration = DispatchTime.now().uptimeNanoseconds - reopenStart
        let afterReopen = try physicalFootprint()
        let persistedEvidence = try stateEvidence(stateURL)
        guard persistedEvidence.acknowledgedFrameCount
                == configuration.acknowledgedFrameCount,
              persistedEvidence.pendingFrameCount == 0 else {
            throw LiveSenderAckBenchmarkError.correctnessFailure(
                "the reopened ledger does not contain the expected exact identities"
            )
        }
        let sequenceIDs = probeSequenceIDs(configuration.acknowledgedFrameCount)
        var probes: [LiveSenderAckBenchmarkSequenceProbe] = []
        for sequenceID in sequenceIDs {
            let identical = try frameReference(
                sequenceID: sequenceID,
                sessionID: session.sessionID
            ).frame
            let duplicate = try await reopened.enqueue(identical)
            guard duplicate.disposition == .duplicate else {
                throw LiveSenderAckBenchmarkError.correctnessFailure(
                    "sequence \(sequenceID) was not recognized as an exact duplicate"
                )
            }
            let conflict = try conflictingFrame(from: identical)
            var conflictRejected = false
            do {
                _ = try await reopened.enqueue(conflict)
            } catch LiveSenderQueueError.frameConflict(let rejected)
                where rejected == sequenceID {
                conflictRejected = true
            }
            guard conflictRejected else {
                throw LiveSenderAckBenchmarkError.correctnessFailure(
                    "sequence \(sequenceID) did not reject a conflicting reference"
                )
            }
            probes.append(LiveSenderAckBenchmarkSequenceProbe(
                sequenceID: sequenceID,
                identicalDisposition: duplicate.disposition.rawValue,
                conflictingReferenceRejected: true
            ))
        }

        return LiveSenderAckBenchmarkReopenPhase(
            schema: reopenPhaseSchema,
            configuration: configuration,
            process: processEvidence,
            queueLimits: queueLimitsEvidence(limits),
            persistedState: persistedEvidence,
            reopenDurationNanoseconds: reopenDuration,
            memory: phaseMemory(before: before, after: afterReopen),
            platform: platform(),
            sequenceProbes: probes
        )
    }

    public static func canonicalJSONData<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(value)
    }

    public static func sha256(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map {
            String(format: "%02x", $0)
        }.joined()
    }

    private struct Fixture {
        let session: LiveSenderSessionReference
        let lastFrame: LiveSenderFrameReference
        let acknowledgedFrames: [SeedAcknowledgedFrameIdentity]
    }

    private struct FrameMaterial {
        let frame: LiveSenderFrameReference
        let metadata: Data
        let source: Data
    }

    private struct ExternallySeededState {
        let session: LiveSenderSessionReference
        let captureRoot: URL
        let stateURL: URL
        let evidence: LiveSenderAckBenchmarkStateEvidence
    }

    private struct SeedState: Encodable {
        let schema: String
        let session: LiveSenderSessionReference
        let frames: [LiveSenderFrameReference]
        let acknowledgedFrames: [SeedAcknowledgedFrameIdentity]
        let receiverProgress: SeedReceiverProgress
        let finalized: Bool

        enum CodingKeys: String, CodingKey {
            case schema, session, frames
            case acknowledgedFrames = "acknowledged_frames"
            case receiverProgress = "receiver_progress"
            case finalized
        }
    }

    private struct SeedAcknowledgedFrameIdentity: Codable, Equatable {
        let sequenceID: Int
        let referenceSHA256: String

        enum CodingKeys: String, CodingKey {
            case sequenceID = "sequence_id"
            case referenceSHA256 = "reference_sha256"
        }
    }

    private struct SeedReceiverProgress: Encodable {
        let receivedCount: Int
        let contiguousCount: Int
        let pendingCount: Int
        let expectedFrameCount: Int
        let nextExpectedSequenceID: Int
        let missingRanges: [LiveSenderMissingRange]

        enum CodingKeys: String, CodingKey {
            case receivedCount = "received_count"
            case contiguousCount = "contiguous_count"
            case pendingCount = "pending_count"
            case expectedFrameCount = "expected_frame_count"
            case nextExpectedSequenceID = "next_expected_sequence_id"
            case missingRanges = "missing_ranges"
        }
    }

    private struct SeedEnvelope: Encodable {
        let schema: String
        let payloadSHA256: String
        let payloadBase64: String

        enum CodingKeys: String, CodingKey {
            case schema
            case payloadSHA256 = "payload_sha256"
            case payloadBase64 = "payload_base64"
        }
    }

    private static func prepareExternalState(
        initialAcknowledgedFrameCount: Int,
        finalAcknowledgedFrameCount: Int,
        workspaceURL: URL
    ) throws -> ExternallySeededState {
        guard initialAcknowledgedFrameCount >= 0,
              finalAcknowledgedFrameCount > 0,
              initialAcknowledgedFrameCount <= finalAcknowledgedFrameCount,
              finalAcknowledgedFrameCount <= 99_999_999 else {
            throw LiveSenderAckBenchmarkError.invalidConfiguration
        }
        let workspace = workspaceURL.standardizedFileURL
        let captureRoot = workspace.appendingPathComponent(
            "capture",
            isDirectory: true
        )
        let stateURL = workspace.appendingPathComponent("state/queue.json")
        guard !FileManager.default.fileExists(atPath: stateURL.path) else {
            throw LiveSenderAckBenchmarkError.workspaceNotEmpty
        }
        try FileManager.default.createDirectory(
            at: captureRoot,
            withIntermediateDirectories: true
        )
        let session = try makeSession(
            acknowledgedFrameCount: finalAcknowledgedFrameCount,
            captureRoot: captureRoot,
            writeEvidence: true
        )
        var acknowledged: [SeedAcknowledgedFrameIdentity] = []
        var pending: [LiveSenderFrameReference] = []
        acknowledged.reserveCapacity(initialAcknowledgedFrameCount)
        pending.reserveCapacity(
            finalAcknowledgedFrameCount - initialAcknowledgedFrameCount
        )
        for sequenceID in 1...finalAcknowledgedFrameCount {
            let material = try frameReference(
                sequenceID: sequenceID,
                sessionID: session.sessionID
            )
            if sequenceID <= initialAcknowledgedFrameCount {
                acknowledged.append(SeedAcknowledgedFrameIdentity(
                    sequenceID: sequenceID,
                    referenceSHA256: sha256(
                        try canonicalJSONData(material.frame)
                    )
                ))
            } else {
                pending.append(material.frame)
                try write(
                    material.source,
                    relativePath: material.frame.assets[0].file.relativePath,
                    under: captureRoot
                )
                try write(
                    material.metadata,
                    relativePath: material.frame.metadata.relativePath,
                    under: captureRoot
                )
            }
        }
        let stateData = try makeQueueEnvelope(
            session: session,
            frames: pending,
            acknowledgedFrames: acknowledged,
            expectedFrameCount: finalAcknowledgedFrameCount,
            receivedCount: initialAcknowledgedFrameCount
        )
        try FileManager.default.createDirectory(
            at: stateURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try stateData.write(to: stateURL, options: [.atomic])
        return ExternallySeededState(
            session: session,
            captureRoot: captureRoot,
            stateURL: stateURL,
            evidence: try stateEvidence(stateURL)
        )
    }

    private static func reconcileOne(
        sequenceID: Int,
        finalAcknowledgedFrameCount: Int,
        sessionID: String,
        queue: LiveSenderQueue
    ) async throws -> UInt64 {
        let missingRanges: [LiveSenderMissingRange]
        if sequenceID < finalAcknowledgedFrameCount {
            missingRanges = [
                try LiveSenderMissingRange(
                    start: sequenceID + 1,
                    end: finalAcknowledgedFrameCount
                ),
            ]
        } else {
            missingRanges = []
        }
        let acknowledgement = try LiveSenderAcknowledgement(
            sessionID: sessionID,
            operation: .resume,
            status: .accepted,
            receivedCount: sequenceID,
            contiguousCount: sequenceID,
            pendingCount: 0,
            expectedFrameCount: finalAcknowledgedFrameCount,
            nextExpectedSequenceID: sequenceID + 1,
            missingRanges: missingRanges,
            finalized: false
        )
        let start = DispatchTime.now().uptimeNanoseconds
        let reconciliation = try await queue.reconcile(acknowledgement)
        let duration = DispatchTime.now().uptimeNanoseconds - start
        guard reconciliation.acknowledgedSequenceIDs == [sequenceID] else {
            throw LiveSenderAckBenchmarkError.correctnessFailure(
                "ACK \(sequenceID) did not reconcile exactly one expected identity"
            )
        }
        return duration
    }

    private static func probeExactIdentities(
        queue: LiveSenderQueue,
        sessionID: String,
        finalAcknowledgedFrameCount: Int
    ) async throws -> [LiveSenderAckBenchmarkSequenceProbe] {
        var probes: [LiveSenderAckBenchmarkSequenceProbe] = []
        for sequenceID in probeSequenceIDs(finalAcknowledgedFrameCount) {
            let identical = try frameReference(
                sequenceID: sequenceID,
                sessionID: sessionID
            ).frame
            let duplicate = try await queue.enqueue(identical)
            guard duplicate.disposition == .duplicate else {
                throw LiveSenderAckBenchmarkError.correctnessFailure(
                    "sequence \(sequenceID) was not recognized as an exact duplicate"
                )
            }
            var conflictRejected = false
            do {
                _ = try await queue.enqueue(try conflictingFrame(from: identical))
            } catch LiveSenderQueueError.frameConflict(let rejected)
                where rejected == sequenceID {
                conflictRejected = true
            }
            guard conflictRejected else {
                throw LiveSenderAckBenchmarkError.correctnessFailure(
                    "sequence \(sequenceID) did not reject a conflicting reference"
                )
            }
            probes.append(LiveSenderAckBenchmarkSequenceProbe(
                sequenceID: sequenceID,
                identicalDisposition: duplicate.disposition.rawValue,
                conflictingReferenceRejected: true
            ))
        }
        return probes
    }

    private static func validateCompletedStreamState(
        _ evidence: LiveSenderAckBenchmarkStateEvidence,
        finalAcknowledgedFrameCount: Int
    ) throws {
        guard evidence.acknowledgedFrameCount == finalAcknowledgedFrameCount,
              evidence.pendingFrameCount == 0 else {
            throw LiveSenderAckBenchmarkError.correctnessFailure(
                "the progressive stream did not persist its complete exact ledger"
            )
        }
    }

    private static func arrivedAcknowledgementCount(
        elapsedNanoseconds: UInt64,
        totalCount: Int,
        rate: Int
    ) -> Int {
        let wholeSeconds = elapsedNanoseconds / 1_000_000_000
        let remainder = elapsedNanoseconds % 1_000_000_000
        let arrived = wholeSeconds * UInt64(rate)
            + remainder * UInt64(rate) / 1_000_000_000
            + 1
        return min(
            totalCount,
            arrived > UInt64(Int.max) ? Int.max : Int(arrived)
        )
    }

    private static func streamGateResult(
        platform: LiveSenderAckBenchmarkPlatform,
        persistedState: LiveSenderAckBenchmarkStateEvidence,
        acknowledgementDurationsNanoseconds: [UInt64],
        memory: LiveSenderAckBenchmarkPhaseMemory,
        throughput: Double?,
        maximumBacklogFrames: Int?,
        finalBacklogFrames: Int?
    ) -> String {
        guard platform.isPhysicalDevice else {
            return "not_evaluated_non_physical"
        }
        guard platform.isOldestSupportedLiDARiPhone else {
            return "not_evaluated_ineligible_device"
        }
        guard platform.optimizedBuild else {
            return "not_evaluated_unoptimized_build"
        }
        guard !acknowledgementDurationsNanoseconds.isEmpty else {
            return "failed"
        }
        let ordered = acknowledgementDurationsNanoseconds.sorted()
        let p50 = percentile(ordered, fraction: 0.50)
        let p95 = percentile(ordered, fraction: 0.95)
        let latencyPassed = p50 <= 50_000_000
            && p95 <= 100_000_000
            && ordered.last! <= 200_000_000
        let statePassed = persistedState.payloadBytes < 24 * 1024 * 1024
            && persistedState.envelopeBytes < 32 * 1024 * 1024
        let memoryPassed = memory.footprintDeltaBytes <= 16 * 1024 * 1024
            && memory.kernelReportedPeakFootprintBytes <= 128 * 1024 * 1024
        let throughputPassed = throughput.map { $0 >= 10.0 } ?? true
        let backlogPassed = maximumBacklogFrames.map { $0 <= 8 } ?? true
        let finalBacklogPassed = finalBacklogFrames.map { $0 == 0 } ?? true
        return latencyPassed
            && statePassed
            && memoryPassed
            && throughputPassed
            && backlogPassed
            && finalBacklogPassed
            ? "measurement_passed_requires_aggregate_evaluation"
            : "failed"
    }

    private static func percentile(
        _ ordered: [UInt64],
        fraction: Double
    ) -> UInt64 {
        let index = max(
            0,
            Int(ceil(Double(ordered.count) * fraction)) - 1
        )
        return ordered[index]
    }

    private static func makeFixture(
        acknowledgedFrameCount count: Int,
        captureRoot: URL
    ) throws -> Fixture {
        let session = try makeSession(
            acknowledgedFrameCount: count,
            captureRoot: captureRoot,
            writeEvidence: true
        )
        let sessionID = session.sessionID
        var acknowledged: [SeedAcknowledgedFrameIdentity] = []
        acknowledged.reserveCapacity(max(count - 1, 0))
        var last: FrameMaterial?
        for sequenceID in 1...count {
            let material = try frameReference(
                sequenceID: sequenceID,
                sessionID: sessionID
            )
            if sequenceID == count {
                last = material
            } else {
                acknowledged.append(SeedAcknowledgedFrameIdentity(
                    sequenceID: sequenceID,
                    referenceSHA256: sha256(try canonicalJSONData(material.frame))
                ))
            }
        }
        guard let last else {
            throw LiveSenderAckBenchmarkError.invalidConfiguration
        }
        try write(
            last.source,
            relativePath: last.frame.assets[0].file.relativePath,
            under: captureRoot
        )
        try write(
            last.metadata,
            relativePath: last.frame.metadata.relativePath,
            under: captureRoot
        )
        return Fixture(
            session: session,
            lastFrame: last.frame,
            acknowledgedFrames: acknowledged
        )
    }

    private static func makeSession(
        acknowledgedFrameCount count: Int,
        captureRoot: URL,
        writeEvidence: Bool
    ) throws -> LiveSenderSessionReference {
        let sessionID = "ack-benchmark-v0-1-\(count)"
        let manifestData = try JSONSerialization.data(
            withJSONObject: ["schema": "capture_splat.v0.3"],
            options: [.sortedKeys]
        )
        let manifestReference = try fileReference(
            path: "capture.json",
            data: manifestData,
            mediaType: "application/json"
        )
        let sessionData = try sessionMetadata(
            sessionID: sessionID,
            expectedFrameCount: count,
            manifest: manifestReference
        )
        let sessionPath = "live/session.json"
        if writeEvidence {
            try write(manifestData, relativePath: "capture.json", under: captureRoot)
            try write(sessionData, relativePath: sessionPath, under: captureRoot)
        }
        return try LiveSenderSessionReference(
            sessionID: sessionID,
            expectedFrameCount: count,
            metadata: try fileReference(
                path: sessionPath,
                data: sessionData,
                mediaType: "application/json"
            ),
            authorization: try LiveSenderAuthorizationBinding(
                desktopID: LiveAuthEncoding.identity(
                    prefix: "wsd",
                    publicKeyX963: Data(repeating: 0x11, count: 65)
                ),
                deviceID: LiveAuthEncoding.identity(
                    prefix: "csd",
                    publicKeyX963: Data(repeating: 0x22, count: 65)
                )
            )
        )
    }

    private static func makeSeedEnvelope(
        session: LiveSenderSessionReference,
        frames: [LiveSenderFrameReference],
        acknowledgedFrames: [SeedAcknowledgedFrameIdentity],
        expectedFrameCount: Int
    ) throws -> Data {
        try makeQueueEnvelope(
            session: session,
            frames: frames,
            acknowledgedFrames: acknowledgedFrames,
            expectedFrameCount: expectedFrameCount,
            receivedCount: expectedFrameCount - 1
        )
    }

    private static func makeQueueEnvelope(
        session: LiveSenderSessionReference,
        frames: [LiveSenderFrameReference],
        acknowledgedFrames: [SeedAcknowledgedFrameIdentity],
        expectedFrameCount: Int,
        receivedCount: Int
    ) throws -> Data {
        let missingRanges: [LiveSenderMissingRange]
        if receivedCount < expectedFrameCount {
            missingRanges = [
                try LiveSenderMissingRange(
                    start: receivedCount + 1,
                    end: expectedFrameCount
                ),
            ]
        } else {
            missingRanges = []
        }
        let state = SeedState(
            schema: "capture_splat.live_sender_queue_state.v0.1",
            session: session,
            frames: frames,
            acknowledgedFrames: acknowledgedFrames,
            receiverProgress: SeedReceiverProgress(
                receivedCount: receivedCount,
                contiguousCount: receivedCount,
                pendingCount: 0,
                expectedFrameCount: expectedFrameCount,
                nextExpectedSequenceID: receivedCount + 1,
                missingRanges: missingRanges
            ),
            finalized: false
        )
        let payload = try canonicalJSONData(state)
        let envelope = SeedEnvelope(
            schema: "capture_splat.live_sender_queue_envelope.v0.1",
            payloadSHA256: sha256(payload),
            payloadBase64: payload.base64EncodedString()
        )
        return try canonicalJSONData(envelope)
    }

    private static func queueLimits(_ count: Int) throws -> LiveSenderQueueLimits {
        try LiveSenderQueueLimits(
            maximumFrames: max(count, 1),
            maximumBytes: Int64.max / 4,
            maximumInFlight: min(
                LiveSenderQueueLimits.maximumAllowedInFlight,
                count
            )
        )
    }

    private static func queueLimitsEvidence(
        _ limits: LiveSenderQueueLimits
    ) -> LiveSenderAckBenchmarkQueueLimitsEvidence {
        LiveSenderAckBenchmarkQueueLimitsEvidence(
            maximumFrames: limits.maximumFrames,
            maximumBytes: limits.maximumBytes,
            maximumInFlight: limits.maximumInFlight,
            scope: "benchmark_only_not_product_cap"
        )
    }

    private static func phaseMemory(
        before: FootprintSample,
        after: FootprintSample
    ) -> LiveSenderAckBenchmarkPhaseMemory {
        let maximumObserved = max(after.current, after.peak)
        let delta: Int64
        if maximumObserved >= before.current {
            let difference = maximumObserved - before.current
            delta = difference > UInt64(Int64.max)
                ? Int64.max
                : Int64(difference)
        } else {
            delta = 0
        }
        return LiveSenderAckBenchmarkPhaseMemory(
            footprintBeforeBytes: before.current,
            footprintAfterBytes: after.current,
            footprintDeltaBytes: delta,
            kernelReportedPeakFootprintBytes: max(before.peak, after.peak)
        )
    }

    private static func sessionMetadata(
        sessionID: String,
        expectedFrameCount: Int,
        manifest: LiveSenderFileReference
    ) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schema": "capture_splat.live_session.v0.1",
            "session_id": sessionID,
            "created_at": "2026-07-30T00:00:00.000Z",
            "source_manifest": [
                "path": manifest.relativePath,
                "sha256": manifest.sha256,
                "size_bytes": manifest.sizeBytes,
                "schema": "capture_splat.v0.3",
            ],
            "expected_frame_count": expectedFrameCount,
            "coordinate_system": [
                "id": "arkit_world",
                "units": "meters",
                "handedness": "right",
                "world_up": "+Y",
                "camera_forward": "-Z",
                "matrix_layout": "row-major",
                "vector_convention": "column-vector",
            ],
            "authority": "proposal_only",
        ], options: [.sortedKeys])
    }

    private static func frameReference(
        sequenceID: Int,
        sessionID: String
    ) throws -> FrameMaterial {
        let source = Data("benchmark-source-\(sequenceID)\n".utf8)
        let sourcePath = String(format: "rgb/%08d.jpg", sequenceID)
        let sourceReference = try fileReference(
            path: sourcePath,
            data: source,
            mediaType: "image/jpeg"
        )
        let metadata = try frameMetadata(
            sessionID: sessionID,
            sequenceID: sequenceID,
            source: sourceReference
        )
        let metadataReference = try fileReference(
            path: String(format: "live/frames/%08d.json", sequenceID),
            data: metadata,
            mediaType: "application/json"
        )
        return FrameMaterial(
            frame: try LiveSenderFrameReference(
                sessionID: sessionID,
                sequenceID: sequenceID,
                metadata: metadataReference,
                assets: [
                    LiveSenderAssetReference(role: .source, file: sourceReference),
                ]
            ),
            metadata: metadata,
            source: source
        )
    }

    private static func frameMetadata(
        sessionID: String,
        sequenceID: Int,
        source: LiveSenderFileReference
    ) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schema": "capture_splat.live_frame.v0.1",
            "session_id": sessionID,
            "sequence_id": sequenceID,
            "timestamp": [
                "value": Double(sequenceID),
                "clock_domain": "arkit_session",
            ],
            "source_frame": [
                "path": source.relativePath,
                "sha256": source.sha256,
                "size_bytes": source.sizeBytes,
                "media_type": source.mediaType,
                "width": 2,
                "height": 2,
            ],
            "intrinsics": [
                "model": "pinhole",
                "fl_x": 2.0,
                "fl_y": 2.0,
                "cx": 1.0,
                "cy": 1.0,
                "calibration_width": 2,
                "calibration_height": 2,
                "applies_to": "source_frame",
            ],
            "camera_to_world": [
                1.0, 0.0, 0.0, Double(sequenceID),
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "coordinate_frame": "arkit_world",
            "tracking": ["state": "normal"],
            "quality": [
                "accepted": true,
                "score": 0.9,
                "reason": "benchmark_fixture",
            ],
        ], options: [.sortedKeys])
    }

    private static func fileReference(
        path: String,
        data: Data,
        mediaType: String
    ) throws -> LiveSenderFileReference {
        try LiveSenderFileReference(
            relativePath: path,
            sizeBytes: Int64(data.count),
            sha256: sha256(data),
            mediaType: mediaType
        )
    }

    private static func conflictingFrame(
        from frame: LiveSenderFrameReference
    ) throws -> LiveSenderFrameReference {
        let conflictingMetadata = try LiveSenderFileReference(
            relativePath: "live/conflicts/\(frame.sequenceID).json",
            sizeBytes: frame.metadata.sizeBytes,
            sha256: sha256(Data("conflict-\(frame.sequenceID)".utf8)),
            mediaType: "application/json"
        )
        return try LiveSenderFrameReference(
            sessionID: frame.sessionID,
            sequenceID: frame.sequenceID,
            metadata: conflictingMetadata,
            assets: frame.assets
        )
    }

    private static func probeSequenceIDs(_ count: Int) -> [Int] {
        Array(Set([1, (count + 1) / 2, count])).sorted()
    }

    private static func stateEvidence(_ stateURL: URL) throws
        -> LiveSenderAckBenchmarkStateEvidence {
        let envelope = try Data(contentsOf: stateURL)
        let object = try JSONSerialization.jsonObject(with: envelope)
        guard let dictionary = object as? [String: Any],
              Set(dictionary.keys) == Set([
                  "schema",
                  "payload_sha256",
                  "payload_base64",
              ]),
              dictionary["schema"] as? String
                  == "capture_splat.live_sender_queue_envelope.v0.1",
              let payloadSHA256 = dictionary["payload_sha256"] as? String,
              let payloadBase64 = dictionary["payload_base64"] as? String,
              let payload = Data(base64Encoded: payloadBase64),
              sha256(payload) == payloadSHA256,
              let payloadObject = try JSONSerialization.jsonObject(with: payload)
                  as? [String: Any],
              let acknowledged = payloadObject["acknowledged_frames"] as? [[String: Any]],
              let frames = payloadObject["frames"] as? [[String: Any]] else {
            throw LiveSenderAckBenchmarkError.invalidSeedState(
                "the envelope, checksum, or payload shape is invalid"
            )
        }
        return LiveSenderAckBenchmarkStateEvidence(
            payloadBytes: payload.count,
            envelopeBytes: envelope.count,
            payloadSHA256: payloadSHA256,
            envelopeSHA256: sha256(envelope),
            acknowledgedFrameCount: acknowledged.count,
            pendingFrameCount: frames.count
        )
    }

    private static func write(
        _ data: Data,
        relativePath: String,
        under root: URL
    ) throws {
        let destination = root.appendingPathComponent(relativePath)
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try data.write(to: destination, options: [.atomic])
    }

    private struct FootprintSample {
        let current: UInt64
        let peak: UInt64
    }

    private static func physicalFootprint() throws -> FootprintSample {
        var information = task_vm_info_data_t()
        var count = mach_msg_type_number_t(
            MemoryLayout<task_vm_info_data_t>.size
                / MemoryLayout<natural_t>.size
        )
        let result = withUnsafeMutablePointer(to: &information) { pointer in
            pointer.withMemoryRebound(
                to: integer_t.self,
                capacity: Int(count)
            ) { rebound in
                task_info(
                    mach_task_self_,
                    task_flavor_t(TASK_VM_INFO),
                    rebound,
                    &count
                )
            }
        }
        guard result == KERN_SUCCESS else {
            throw LiveSenderAckBenchmarkError.memoryProbeFailed
        }
        let peakFieldEnd = (
            MemoryLayout<task_vm_info_data_t>.offset(
                of: \.ledger_phys_footprint_peak
            ) ?? MemoryLayout<task_vm_info_data_t>.size
        ) + MemoryLayout<Int64>.size
        let peakFieldCount = mach_msg_type_number_t(
            (peakFieldEnd + MemoryLayout<natural_t>.size - 1)
                / MemoryLayout<natural_t>.size
        )
        let peak: UInt64
        if count >= peakFieldCount,
           information.ledger_phys_footprint_peak >= 0 {
            peak = UInt64(information.ledger_phys_footprint_peak)
        } else {
            peak = information.phys_footprint
        }
        return FootprintSample(current: information.phys_footprint, peak: peak)
    }

    private static func platform() -> LiveSenderAckBenchmarkPlatform {
        let operatingSystem: String
        let isPhysicalDevice: Bool
        #if os(iOS)
        operatingSystem = "ios"
        #if targetEnvironment(simulator)
        isPhysicalDevice = false
        #else
        isPhysicalDevice = true
        #endif
        #elseif os(macOS)
        operatingSystem = "macos"
        isPhysicalDevice = false
        #else
        operatingSystem = "unknown"
        isPhysicalDevice = false
        #endif

        let model = machine()
        let oldestSupportedLiDAR = isPhysicalDevice
            && (model == "iPhone13,3" || model == "iPhone13,4")
        return LiveSenderAckBenchmarkPlatform(
            operatingSystem: operatingSystem,
            operatingSystemVersion: ProcessInfo.processInfo.operatingSystemVersionString,
            machine: model,
            architecture: architecture(),
            thermalState: thermalState(),
            isPhysicalDevice: isPhysicalDevice,
            isOldestSupportedLiDARiPhone: oldestSupportedLiDAR,
            optimizedBuild: optimizedBuild(),
            physicalGateResult: physicalGateResult(
                isPhysicalDevice: isPhysicalDevice,
                isEligibleDevice: oldestSupportedLiDAR
            )
        )
    }

    private static func machine() -> String {
        var value = utsname()
        guard uname(&value) == 0 else { return "unknown" }
        return withUnsafePointer(to: &value.machine) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1) {
                String(cString: $0)
            }
        }
    }

    private static func architecture() -> String {
        #if arch(arm64)
        return "arm64"
        #elseif arch(x86_64)
        return "x86_64"
        #else
        return "unknown"
        #endif
    }

    private static func optimizedBuild() -> Bool {
        #if CAPTURE_SPLAT_ACK_BENCHMARK_OPTIMIZED
        return !_isDebugAssertConfiguration()
        #else
        return false
        #endif
    }

    private static func physicalGateResult(
        isPhysicalDevice: Bool,
        isEligibleDevice: Bool
    ) -> String {
        guard isPhysicalDevice else {
            return "not_evaluated_non_physical"
        }
        guard isEligibleDevice else {
            return "not_evaluated_ineligible_device"
        }
        guard optimizedBuild() else {
            return "not_evaluated_unoptimized_build"
        }
        return "physical_trial_requires_aggregate_gate_evaluation"
    }

    private static func thermalState() -> String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }
}

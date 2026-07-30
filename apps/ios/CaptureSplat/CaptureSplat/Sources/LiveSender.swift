import Foundation

protocol LiveAuthenticatedRequesting: Sendable {
    func validateSenderAuthorization(now: Date) async throws -> LiveSenderAuthorizationBinding

    func perform(
        method: String,
        path: String,
        body: LiveAuthenticatedBody,
        now: Date
    ) async throws -> Data
}

extension LiveAuthenticatedHTTPClient: LiveAuthenticatedRequesting {}

enum LiveSenderThermalState: String, Codable, Sendable {
    case nominal
    case fair
    case serious
    case critical
}

struct LiveSenderEnvironment: Codable, Equatable, Sendable {
    let isForeground: Bool
    let networkAvailable: Bool
    let receiverAvailable: Bool
    let availableStorageBytes: Int64
    let thermalState: LiveSenderThermalState
}

enum LiveSenderPauseReason: String, Codable, Sendable {
    case background
    case networkUnavailable = "network_unavailable"
    case receiverUnavailable = "receiver_unavailable"
    case lowStorage = "low_storage"
    case thermalPressure = "thermal_pressure"
}

struct LiveSenderPolicy: Codable, Equatable, Sendable {
    let minimumAvailableStorageBytes: Int64
    let requiresForeground: Bool
    let pausesAtSeriousThermalState: Bool

    init(
        minimumAvailableStorageBytes: Int64 = 512 * 1024 * 1024,
        requiresForeground: Bool = true,
        pausesAtSeriousThermalState: Bool = true
    ) throws {
        guard minimumAvailableStorageBytes >= 0 else {
            throw LiveAuthContractError.invalid("Minimum available storage cannot be negative.")
        }
        self.minimumAvailableStorageBytes = minimumAvailableStorageBytes
        self.requiresForeground = requiresForeground
        self.pausesAtSeriousThermalState = pausesAtSeriousThermalState
    }

    func pauseReason(for environment: LiveSenderEnvironment) -> LiveSenderPauseReason? {
        if requiresForeground && !environment.isForeground { return .background }
        if environment.availableStorageBytes < minimumAvailableStorageBytes { return .lowStorage }
        if pausesAtSeriousThermalState,
           environment.thermalState == .serious || environment.thermalState == .critical {
            return .thermalPressure
        }
        if !environment.networkAvailable { return .networkUnavailable }
        if !environment.receiverAvailable { return .receiverUnavailable }
        return nil
    }
}

struct LiveSenderRetryPolicy: Codable, Equatable, Sendable {
    let maximumAttempts: Int
    let initialDelayMilliseconds: Int
    let maximumDelayMilliseconds: Int

    init(
        maximumAttempts: Int = 3,
        initialDelayMilliseconds: Int = 250,
        maximumDelayMilliseconds: Int = 4_000
    ) throws {
        guard maximumAttempts > 0,
              initialDelayMilliseconds >= 0,
              maximumDelayMilliseconds >= initialDelayMilliseconds else {
            throw LiveAuthContractError.invalid("Live sender retry policy is invalid.")
        }
        self.maximumAttempts = maximumAttempts
        self.initialDelayMilliseconds = initialDelayMilliseconds
        self.maximumDelayMilliseconds = maximumDelayMilliseconds
    }

    func delayMilliseconds(afterAttempt attempt: Int) -> Int {
        guard initialDelayMilliseconds > 0, attempt > 0 else { return 0 }
        let shift = min(attempt - 1, 20)
        let (scaled, overflow) = initialDelayMilliseconds.multipliedReportingOverflow(
            by: 1 << shift
        )
        return min(overflow ? maximumDelayMilliseconds : scaled, maximumDelayMilliseconds)
    }
}

protocol LiveSenderSleeping: Sendable {
    func sleep(milliseconds: Int) async throws
}

struct SystemLiveSenderSleeper: LiveSenderSleeping {
    func sleep(milliseconds: Int) async throws {
        guard milliseconds > 0 else { return }
        try await Task.sleep(nanoseconds: UInt64(milliseconds) * 1_000_000)
    }
}

enum LiveSenderRunStatus: String, Codable, Sendable {
    case idle
    case paused
    case interrupted
    case awaitingFrames = "awaiting_frames"
    case finalized
}

private struct LiveSenderPausedError: Error {
    let reason: LiveSenderPauseReason
}

struct LiveSenderRunSummary: Codable, Equatable, Sendable {
    let schema: String
    let status: LiveSenderRunStatus
    let sessionID: String?
    let pauseReason: LiveSenderPauseReason?
    let attemptedFrameCount: Int
    let acknowledgedFrameCount: Int
    let queuedFrameCount: Int
    let queuedBytes: Int64
    let finalized: Bool
    let lastError: String?

    enum CodingKeys: String, CodingKey {
        case schema, status
        case sessionID = "session_id"
        case pauseReason = "pause_reason"
        case attemptedFrameCount = "attempted_frame_count"
        case acknowledgedFrameCount = "acknowledged_frame_count"
        case queuedFrameCount = "queued_frame_count"
        case queuedBytes = "queued_bytes"
        case finalized
        case lastError = "last_error"
    }
}

actor LiveSender {
    typealias EnvironmentProvider = @Sendable () async -> LiveSenderEnvironment
    typealias Clock = @Sendable () -> Date

    private let queue: LiveSenderQueue
    private let requester: any LiveAuthenticatedRequesting
    private let policy: LiveSenderPolicy
    private let retryPolicy: LiveSenderRetryPolicy
    private let sleeper: any LiveSenderSleeping

    init(
        queue: LiveSenderQueue,
        requester: any LiveAuthenticatedRequesting,
        policy: LiveSenderPolicy,
        retryPolicy: LiveSenderRetryPolicy,
        sleeper: any LiveSenderSleeping = SystemLiveSenderSleeper()
    ) {
        self.queue = queue
        self.requester = requester
        self.policy = policy
        self.retryPolicy = retryPolicy
        self.sleeper = sleeper
    }

    func runOnce(
        environment: @escaping EnvironmentProvider,
        clock: @escaping Clock = { Date() }
    ) async -> LiveSenderRunSummary {
        let lease = UUID()
        guard await queue.acquireSenderLease(lease) else {
            return (try? await summary(
                status: .idle,
                pauseReason: nil,
                attemptedFrames: 0,
                acknowledgedFrames: 0,
                error: "A live sender run is already active."
            )) ?? LiveSenderRunSummary(
                schema: "capture_splat.live_sender_run_summary.v0.1",
                status: .idle,
                sessionID: nil,
                pauseReason: nil,
                attemptedFrameCount: 0,
                acknowledgedFrameCount: 0,
                queuedFrameCount: 0,
                queuedBytes: 0,
                finalized: false,
                lastError: "A live sender run is already active."
            )
        }
        let result = await runLeased(environment: environment, clock: clock)
        await queue.releaseSenderLease(lease)
        return result
    }

    private func runLeased(
        environment: @escaping EnvironmentProvider,
        clock: @escaping Clock
    ) async -> LiveSenderRunSummary {
        var attemptedFrames = 0
        var acknowledgedFrames = 0
        do {
            if try await queue.snapshot().finalized {
                return try await summary(
                    status: .finalized,
                    pauseReason: nil,
                    attemptedFrames: 0,
                    acknowledgedFrames: 0
                )
            }
            if let pause = policy.pauseReason(for: await environment()) {
                return try await summary(
                    status: .paused,
                    pauseReason: pause,
                    attemptedFrames: 0,
                    acknowledgedFrames: 0
                )
            }
            let authorization = try await requester.validateSenderAuthorization(now: clock())
            try await queue.validateAuthorizationBinding(authorization)

            let session = try await queue.sessionForSend()
            let sessionURL = try await queue.verifiedFileURL(for: session.metadata)
            let sessionPath = "\(LiveAuthContract.liveAPIRoot)/sessions/\(session.sessionID)"
            let sessionACK = try await requestACK(
                method: "PUT",
                path: sessionPath,
                body: .file(
                    sessionURL,
                    byteCount: session.metadata.sizeBytes,
                    sha256: session.metadata.sha256,
                    contentType: session.metadata.mediaType
                ),
                expectedOperation: .session,
                expectedSequenceID: nil,
                expectedAssetRole: nil,
                environment: environment,
                clock: clock
            )
            _ = try await queue.reconcile(sessionACK)
            if sessionACK.finalized {
                return try await summary(
                    status: .finalized,
                    pauseReason: nil,
                    attemptedFrames: 0,
                    acknowledgedFrames: 0
                )
            }

            let resumeACK = try await requestACK(
                method: "GET",
                path: sessionPath,
                body: .empty,
                expectedOperation: .resume,
                expectedSequenceID: nil,
                expectedAssetRole: nil,
                environment: environment,
                clock: clock
            )
            _ = try await queue.reconcile(resumeACK)
            if resumeACK.finalized {
                return try await summary(
                    status: .finalized,
                    pauseReason: nil,
                    attemptedFrames: 0,
                    acknowledgedFrames: 0
                )
            }

            while true {
                if let pause = policy.pauseReason(for: await environment()) {
                    return try await summary(
                        status: .paused,
                        pauseReason: pause,
                        attemptedFrames: attemptedFrames,
                        acknowledgedFrames: acknowledgedFrames
                    )
                }
                let frames = try await queue.pendingSelection()
                if frames.isEmpty { break }
                attemptedFrames += frames.count
                let outcomes = await withTaskGroup(of: FrameOutcome.self) { group in
                    for frame in frames {
                        group.addTask {
                            do {
                                let ack = try await self.send(
                                    frame: frame,
                                    environment: environment,
                                    clock: clock
                                )
                                return .acknowledged(ack)
                            } catch let paused as LiveSenderPausedError {
                                return .paused(paused.reason)
                            } catch {
                                return .failed(error.localizedDescription)
                            }
                        }
                    }
                    var collected: [FrameOutcome] = []
                    for await outcome in group {
                        collected.append(outcome)
                    }
                    return collected
                }
                var failures: [String] = []
                var pauses: [LiveSenderPauseReason] = []
                for outcome in outcomes {
                    switch outcome {
                    case .acknowledged(let ack):
                        let result = try await queue.reconcile(ack)
                        acknowledgedFrames += result.acknowledgedSequenceIDs.count
                    case .failed(let message):
                        failures.append(message)
                    case .paused(let reason):
                        pauses.append(reason)
                    }
                }
                if let pause = pauses.sorted(by: { $0.rawValue < $1.rawValue }).first {
                    return try await summary(
                        status: .paused,
                        pauseReason: pause,
                        attemptedFrames: attemptedFrames,
                        acknowledgedFrames: acknowledgedFrames
                    )
                }
                if let failure = failures.sorted().first {
                    return try await summary(
                        status: .interrupted,
                        pauseReason: nil,
                        attemptedFrames: attemptedFrames,
                        acknowledgedFrames: acknowledgedFrames,
                        error: failure
                    )
                }
            }

            if let finalization = try await queue.finalizationForSend() {
                let payload = LiveFinalizePayload(
                    schema: "capture_splat.live_finalize.v0.1",
                    sessionID: finalization.sessionID,
                    finalSequenceID: finalization.finalSequenceID
                )
                let body = try LiveStrictJSON.canonicalData(payload)
                let ack = try await requestACK(
                    method: "POST",
                    path: "\(sessionPath)/finalize",
                    body: .data(body, contentType: "application/json"),
                    expectedOperation: .finalize,
                    expectedSequenceID: nil,
                    expectedAssetRole: nil,
                    environment: environment,
                    clock: clock
                )
                _ = try await queue.reconcile(ack)
                return try await summary(
                    status: ack.finalized ? .finalized : .interrupted,
                    pauseReason: nil,
                    attemptedFrames: attemptedFrames,
                    acknowledgedFrames: acknowledgedFrames,
                    error: ack.finalized ? nil : "Receiver did not finalize the complete session."
                )
            }

            return try await summary(
                status: .awaitingFrames,
                pauseReason: nil,
                attemptedFrames: attemptedFrames,
                acknowledgedFrames: acknowledgedFrames
            )
        } catch let paused as LiveSenderPausedError {
            return (try? await summary(
                status: .paused,
                pauseReason: paused.reason,
                attemptedFrames: attemptedFrames,
                acknowledgedFrames: acknowledgedFrames
            )) ?? LiveSenderRunSummary(
                schema: "capture_splat.live_sender_run_summary.v0.1",
                status: .paused,
                sessionID: nil,
                pauseReason: paused.reason,
                attemptedFrameCount: attemptedFrames,
                acknowledgedFrameCount: acknowledgedFrames,
                queuedFrameCount: 0,
                queuedBytes: 0,
                finalized: false,
                lastError: nil
            )
        } catch {
            return (try? await summary(
                status: .interrupted,
                pauseReason: nil,
                attemptedFrames: attemptedFrames,
                acknowledgedFrames: acknowledgedFrames,
                error: error.localizedDescription
            )) ?? LiveSenderRunSummary(
                schema: "capture_splat.live_sender_run_summary.v0.1",
                status: .interrupted,
                sessionID: nil,
                pauseReason: nil,
                attemptedFrameCount: attemptedFrames,
                acknowledgedFrameCount: acknowledgedFrames,
                queuedFrameCount: 0,
                queuedBytes: 0,
                finalized: false,
                lastError: error.localizedDescription
            )
        }
    }

    private func send(
        frame: LiveSenderFrameReference,
        environment: @escaping EnvironmentProvider,
        clock: @escaping Clock
    ) async throws -> LiveSenderAcknowledgement {
        let metadataURL = try await queue.verifiedFileURL(for: frame.metadata)
        var latestACK = try await requestACK(
            method: "PUT",
            path: "\(LiveAuthContract.liveAPIRoot)/sessions/\(frame.sessionID)/frames/\(frame.sequenceID)",
            body: .file(
                metadataURL,
                byteCount: frame.metadata.sizeBytes,
                sha256: frame.metadata.sha256,
                contentType: frame.metadata.mediaType
            ),
            expectedOperation: .frame,
            expectedSequenceID: frame.sequenceID,
            expectedAssetRole: nil,
            environment: environment,
            clock: clock
        )
        let order = Dictionary(
            uniqueKeysWithValues: LiveSenderAssetRole.allCases.enumerated().map { ($1, $0) }
        )
        for asset in frame.assets.sorted(by: { order[$0.role]! < order[$1.role]! }) {
            let fileURL = try await queue.verifiedFileURL(for: asset.file)
            latestACK = try await requestACK(
                method: "PUT",
                path: "\(LiveAuthContract.liveAPIRoot)/sessions/\(frame.sessionID)/frames/\(frame.sequenceID)/assets/\(asset.role.rawValue)",
                body: .file(
                    fileURL,
                    byteCount: asset.file.sizeBytes,
                    sha256: asset.file.sha256,
                    contentType: asset.file.mediaType
                ),
                expectedOperation: .asset,
                expectedSequenceID: frame.sequenceID,
                expectedAssetRole: asset.role,
                environment: environment,
                clock: clock
            )
        }
        guard latestACK.sessionID == frame.sessionID,
              latestACK.sequenceID == frame.sequenceID,
              latestACK.receivedCount > 0,
              latestACK.status == .accepted || latestACK.status == .duplicate else {
            throw LiveSenderQueueError.invalidAcknowledgement(
                "Final frame asset did not produce a durable accepted or duplicate ACK."
            )
        }
        return latestACK
    }

    private func requestACK(
        method: String,
        path: String,
        body: LiveAuthenticatedBody,
        expectedOperation: LiveSenderAcknowledgement.Operation,
        expectedSequenceID: Int?,
        expectedAssetRole: LiveSenderAssetRole?,
        environment: @escaping EnvironmentProvider,
        clock: @escaping Clock
    ) async throws -> LiveSenderAcknowledgement {
        var lastError: Error?
        for attempt in 1...retryPolicy.maximumAttempts {
            do {
                if let pause = policy.pauseReason(for: await environment()) {
                    throw LiveSenderPausedError(reason: pause)
                }
                let response = try await requester.perform(
                    method: method,
                    path: path,
                    body: body,
                    now: clock()
                )
                let acknowledgement = try Self.decodeACK(response)
                try await queue.validateAcknowledgementContract(acknowledgement)
                guard acknowledgement.operation == expectedOperation,
                      acknowledgement.sequenceID == expectedSequenceID,
                      acknowledgement.assetRole == expectedAssetRole else {
                    throw LiveSenderQueueError.invalidAcknowledgement(
                        "ACK identity does not match the requested live resource."
                    )
                }
                return acknowledgement
            } catch {
                lastError = error
                let retryable = (error as? LiveAuthenticatedRequestError)?.retryable ?? false
                guard retryable, attempt < retryPolicy.maximumAttempts else { throw error }
                try await sleeper.sleep(
                    milliseconds: retryPolicy.delayMilliseconds(afterAttempt: attempt)
                )
            }
        }
        throw lastError ?? LiveAuthenticatedRequestError.network("Live request failed.")
    }

    private func summary(
        status: LiveSenderRunStatus,
        pauseReason: LiveSenderPauseReason?,
        attemptedFrames: Int,
        acknowledgedFrames: Int,
        error: String? = nil
    ) async throws -> LiveSenderRunSummary {
        let snapshot = try await queue.snapshot()
        return LiveSenderRunSummary(
            schema: "capture_splat.live_sender_run_summary.v0.1",
            status: status,
            sessionID: snapshot.sessionID,
            pauseReason: pauseReason,
            attemptedFrameCount: attemptedFrames,
            acknowledgedFrameCount: acknowledgedFrames,
            queuedFrameCount: snapshot.queuedFrameCount,
            queuedBytes: snapshot.queuedBytes,
            finalized: snapshot.finalized,
            lastError: error
        )
    }

    private static func decodeACK(_ data: Data) throws -> LiveSenderAcknowledgement {
        let acknowledgement = try LiveStrictJSON.decode(
            LiveSenderAcknowledgement.self,
            from: data
        )
        let object = try JSONSerialization.jsonObject(with: data)
        guard let dictionary = object as? [String: Any] else {
            throw LiveSenderQueueError.invalidAcknowledgement("ACK must be a JSON object.")
        }
        let required: Set<String> = [
            "schema",
            "session_id",
            "operation",
            "status",
            "received_count",
            "contiguous_count",
            "pending_count",
            "expected_frame_count",
            "next_expected_sequence_id",
            "missing_ranges",
            "finalized",
        ]
        let allowed = required.union(["sequence_id", "asset_role", "message"])
        guard required.isSubset(of: dictionary.keys), Set(dictionary.keys).isSubset(of: allowed) else {
            throw LiveSenderQueueError.invalidAcknowledgement("ACK has missing or additional fields.")
        }
        guard let ranges = dictionary["missing_ranges"] as? [[String: Any]],
              ranges.allSatisfy({ Set($0.keys) == Set(["start", "end"]) }) else {
            throw LiveSenderQueueError.invalidAcknowledgement(
                "ACK missing ranges have missing or additional fields."
            )
        }
        if let message = acknowledgement.message, message.isEmpty {
            throw LiveSenderQueueError.invalidAcknowledgement("ACK message cannot be empty.")
        }
        return acknowledgement
    }

    private enum FrameOutcome: Sendable {
        case acknowledged(LiveSenderAcknowledgement)
        case failed(String)
        case paused(LiveSenderPauseReason)
    }
}

private struct LiveFinalizePayload: Codable {
    let schema: String
    let sessionID: String
    let finalSequenceID: Int

    enum CodingKeys: String, CodingKey {
        case schema
        case sessionID = "session_id"
        case finalSequenceID = "final_sequence_id"
    }
}

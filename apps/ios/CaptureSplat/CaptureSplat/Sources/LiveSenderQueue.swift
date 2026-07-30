import CryptoKit
import Darwin
import Foundation

public enum LiveSenderAssetRole: String, Codable, CaseIterable, Sendable {
    case source
    case depth
    case confidence
    case maskPerson = "mask-person"
    case maskValid = "mask-valid"
    case maskObject = "mask-object"
}

public struct LiveSenderAuthorizationBinding: Codable, Equatable, Sendable {
    public let desktopID: String
    public let deviceID: String

    public init(desktopID: String, deviceID: String) throws {
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        try LiveAuthValidation.identity(deviceID, prefix: "csd")
        self.desktopID = desktopID
        self.deviceID = deviceID
    }

    enum CodingKeys: String, CodingKey {
        case desktopID = "desktop_id"
        case deviceID = "device_id"
    }
}

public struct LiveSenderFileReference: Codable, Equatable, Sendable {
    public let relativePath: String
    public let sizeBytes: Int64
    public let sha256: String
    public let mediaType: String

    public init(relativePath: String, sizeBytes: Int64, sha256: String, mediaType: String) throws {
        try LiveSenderValidation.fileReference(
            relativePath: relativePath,
            sizeBytes: sizeBytes,
            sha256: sha256,
            mediaType: mediaType
        )
        self.relativePath = relativePath
        self.sizeBytes = sizeBytes
        self.sha256 = sha256
        self.mediaType = mediaType
    }

    enum CodingKeys: String, CodingKey {
        case relativePath = "relative_path"
        case sizeBytes = "size_bytes"
        case sha256
        case mediaType = "media_type"
    }
}

public struct LiveSenderAssetReference: Codable, Equatable, Sendable {
    public let role: LiveSenderAssetRole
    public let file: LiveSenderFileReference

    public init(role: LiveSenderAssetRole, file: LiveSenderFileReference) {
        self.role = role
        self.file = file
    }
}

public struct LiveSenderSessionReference: Codable, Equatable, Sendable {
    public let sessionID: String
    public let expectedFrameCount: Int?
    public let metadata: LiveSenderFileReference
    public let authorization: LiveSenderAuthorizationBinding

    public init(
        sessionID: String,
        expectedFrameCount: Int?,
        metadata: LiveSenderFileReference,
        authorization: LiveSenderAuthorizationBinding
    ) throws {
        try LiveSenderValidation.sessionID(sessionID)
        if let expectedFrameCount,
           !(1...LiveSenderValidation.maximumSequenceID).contains(expectedFrameCount) {
            throw LiveSenderQueueError.invalidReference(
                "expected_frame_count is outside the receiver limit"
            )
        }
        self.sessionID = sessionID
        self.expectedFrameCount = expectedFrameCount
        self.metadata = metadata
        self.authorization = authorization
    }

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case expectedFrameCount = "expected_frame_count"
        case metadata, authorization
    }
}

public struct LiveSenderFrameReference: Codable, Equatable, Sendable {
    public let sessionID: String
    public let sequenceID: Int
    public let metadata: LiveSenderFileReference
    public let assets: [LiveSenderAssetReference]

    public init(
        sessionID: String,
        sequenceID: Int,
        metadata: LiveSenderFileReference,
        assets: [LiveSenderAssetReference]
    ) throws {
        try LiveSenderValidation.frame(
            sessionID: sessionID,
            sequenceID: sequenceID,
            metadata: metadata,
            assets: assets
        )
        self.sessionID = sessionID
        self.sequenceID = sequenceID
        self.metadata = metadata
        self.assets = assets
    }

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case sequenceID = "sequence_id"
        case metadata, assets
    }
}

public struct LiveSenderFinalizationReference: Codable, Equatable, Sendable {
    public let sessionID: String
    public let finalSequenceID: Int

    public init(sessionID: String, finalSequenceID: Int) throws {
        try LiveSenderValidation.sessionID(sessionID)
        guard (1...LiveSenderValidation.maximumSequenceID).contains(finalSequenceID) else {
            throw LiveSenderQueueError.invalidReference(
                "final_sequence_id is outside the receiver limit"
            )
        }
        self.sessionID = sessionID
        self.finalSequenceID = finalSequenceID
    }

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case finalSequenceID = "final_sequence_id"
    }
}

public struct LiveSenderMissingRange: Codable, Equatable, Sendable {
    public let start: Int
    public let end: Int

    public init(start: Int, end: Int) throws {
        guard start > 0, end >= start else {
            throw LiveSenderQueueError.invalidAcknowledgement("missing ranges must be positive and ordered")
        }
        self.start = start
        self.end = end
    }

    fileprivate func contains(_ sequenceID: Int) -> Bool {
        start <= sequenceID && sequenceID <= end
    }
}

public struct LiveSenderAcknowledgement: Codable, Equatable, Sendable {
    public enum Operation: String, Codable, Sendable {
        case session
        case frame
        case asset
        case resume
        case finalize
    }

    public enum Status: String, Codable, Sendable {
        case accepted
        case duplicate
        case incomplete
        case finalized
    }

    public let schema: String
    public let sessionID: String
    public let operation: Operation
    public let status: Status
    public let sequenceID: Int?
    public let assetRole: LiveSenderAssetRole?
    public let receivedCount: Int
    public let contiguousCount: Int
    public let pendingCount: Int
    public let expectedFrameCount: Int?
    public let nextExpectedSequenceID: Int
    public let missingRanges: [LiveSenderMissingRange]
    public let finalized: Bool
    public let message: String?

    public init(
        schema: String = "capture_splat.live_ack.v0.1",
        sessionID: String,
        operation: Operation,
        status: Status,
        sequenceID: Int? = nil,
        assetRole: LiveSenderAssetRole? = nil,
        receivedCount: Int,
        contiguousCount: Int,
        pendingCount: Int,
        expectedFrameCount: Int?,
        nextExpectedSequenceID: Int,
        missingRanges: [LiveSenderMissingRange],
        finalized: Bool,
        message: String? = nil
    ) throws {
        guard schema == "capture_splat.live_ack.v0.1" else {
            throw LiveSenderQueueError.invalidAcknowledgement("unsupported ACK schema")
        }
        try LiveSenderValidation.sessionID(sessionID)
        guard receivedCount >= 0, contiguousCount >= 0, pendingCount >= 0 else {
            throw LiveSenderQueueError.invalidAcknowledgement("ACK counts must be non-negative")
        }
        if let expectedFrameCount, expectedFrameCount < 1 {
            throw LiveSenderQueueError.invalidAcknowledgement("expected_frame_count must be positive")
        }
        guard nextExpectedSequenceID > 0 else {
            throw LiveSenderQueueError.invalidAcknowledgement("next_expected_sequence_id must be positive")
        }
        if let sequenceID, sequenceID < 1 {
            throw LiveSenderQueueError.invalidAcknowledgement("sequence_id must be positive")
        }
        try LiveSenderValidation.missingRanges(missingRanges)

        self.schema = schema
        self.sessionID = sessionID
        self.operation = operation
        self.status = status
        self.sequenceID = sequenceID
        self.assetRole = assetRole
        self.receivedCount = receivedCount
        self.contiguousCount = contiguousCount
        self.pendingCount = pendingCount
        self.expectedFrameCount = expectedFrameCount
        self.nextExpectedSequenceID = nextExpectedSequenceID
        self.missingRanges = missingRanges
        self.finalized = finalized
        self.message = message
    }

    enum CodingKeys: String, CodingKey {
        case schema
        case sessionID = "session_id"
        case operation, status
        case sequenceID = "sequence_id"
        case assetRole = "asset_role"
        case receivedCount = "received_count"
        case contiguousCount = "contiguous_count"
        case pendingCount = "pending_count"
        case expectedFrameCount = "expected_frame_count"
        case nextExpectedSequenceID = "next_expected_sequence_id"
        case missingRanges = "missing_ranges"
        case finalized, message
    }
}

public struct LiveSenderQueueLimits: Codable, Equatable, Sendable {
    public static let maximumAllowedInFlight = 8

    public let maximumFrames: Int
    public let maximumBytes: Int64
    public let maximumInFlight: Int

    public init(maximumFrames: Int, maximumBytes: Int64, maximumInFlight: Int) throws {
        guard maximumFrames > 0,
              maximumBytes > 0,
              maximumInFlight > 0,
              maximumInFlight <= Self.maximumAllowedInFlight,
              maximumInFlight <= maximumFrames else {
            throw LiveSenderQueueError.invalidReference(
                "queue limits must be positive and maximum_in_flight must not exceed 8 or maximum_frames"
            )
        }
        self.maximumFrames = maximumFrames
        self.maximumBytes = maximumBytes
        self.maximumInFlight = maximumInFlight
    }
}

public struct LiveSenderQueueSnapshot: Codable, Equatable, Sendable {
    public let sessionID: String?
    public let queuedFrameCount: Int
    public let queuedBytes: Int64
    public let maximumFrames: Int
    public let maximumBytes: Int64
    public let pendingSequenceIDs: [Int]
    public let omittedPendingCount: Int
    public let receiverMissingRanges: [LiveSenderMissingRange]
    public let finalizationPending: Bool
    public let finalized: Bool

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case queuedFrameCount = "queued_frame_count"
        case queuedBytes = "queued_bytes"
        case maximumFrames = "maximum_frames"
        case maximumBytes = "maximum_bytes"
        case pendingSequenceIDs = "pending_sequence_ids"
        case omittedPendingCount = "omitted_pending_count"
        case receiverMissingRanges = "receiver_missing_ranges"
        case finalizationPending = "finalization_pending"
        case finalized
    }
}

public enum LiveSenderEnqueueDisposition: String, Codable, Sendable {
    case accepted
    case duplicate
    case capacityExceeded = "capacity_exceeded"
}

public struct LiveSenderEnqueueResult: Codable, Equatable, Sendable {
    public let disposition: LiveSenderEnqueueDisposition
    public let snapshot: LiveSenderQueueSnapshot
}

public struct LiveSenderReconciliationResult: Codable, Equatable, Sendable {
    public let acknowledgedSequenceIDs: [Int]
    public let snapshot: LiveSenderQueueSnapshot

    enum CodingKeys: String, CodingKey {
        case acknowledgedSequenceIDs = "acknowledged_sequence_ids"
        case snapshot
    }
}

public enum LiveSenderQueueError: Error, Equatable, LocalizedError, Sendable {
    case invalidReference(String)
    case unsafeRelativePath(String)
    case sourceSymlink(String)
    case sourceOutsideCaptureRoot(String)
    case sourceMissing(String)
    case sourceNotRegularFile(String)
    case sourceSizeMismatch(String)
    case sourceChecksumMismatch(String)
    case stateCorrupt(String)
    case sessionNotLoaded
    case sessionConflict
    case frameConflict(Int)
    case finalizationConflict
    case queueFinalized
    case invalidAcknowledgement(String)
    case acknowledgementSessionMismatch
    case authorizationMismatch
    case persistenceFailed(String)

    public var errorDescription: String? {
        switch self {
        case let .invalidReference(message),
             let .stateCorrupt(message),
             let .invalidAcknowledgement(message),
             let .persistenceFailed(message):
            return message
        case let .unsafeRelativePath(path):
            return "Unsafe relative path: \(path)"
        case let .sourceSymlink(path):
            return "Source path contains a symbolic link: \(path)"
        case let .sourceOutsideCaptureRoot(path):
            return "Source path escapes the capture root: \(path)"
        case let .sourceMissing(path):
            return "Source file is missing: \(path)"
        case let .sourceNotRegularFile(path):
            return "Source is not a regular file: \(path)"
        case let .sourceSizeMismatch(path):
            return "Source size does not match its reference: \(path)"
        case let .sourceChecksumMismatch(path):
            return "Source checksum does not match its reference: \(path)"
        case .sessionNotLoaded:
            return "The live sender queue has no session."
        case .sessionConflict:
            return "The live sender queue belongs to a different session."
        case let .frameConflict(sequenceID):
            return "Sequence \(sequenceID) conflicts with its queued frame."
        case .finalizationConflict:
            return "The finalization reference conflicts with the queued session."
        case .queueFinalized:
            return "The live sender queue is already finalized."
        case .acknowledgementSessionMismatch:
            return "The ACK belongs to a different session."
        case .authorizationMismatch:
            return "The live sender queue belongs to another paired desktop or device."
        }
    }
}

public actor LiveSenderQueue {
    private static let stateSchema = "capture_splat.live_sender_queue_state.v0.1"
    private static let envelopeSchema = "capture_splat.live_sender_queue_envelope.v0.1"
    private static let maximumPayloadBytes = 48 * 1024 * 1024 - 4096
    private static let maximumEnvelopeBytes = 64 * 1024 * 1024

    private let captureRoot: URL
    private let resolvedCaptureRoot: URL
    private let stateURL: URL
    private let limits: LiveSenderQueueLimits
    private let fileManager: FileManager
    private var state: PersistentState?
    private var senderLease: UUID?

    public init(
        captureRoot: URL,
        stateURL: URL,
        limits: LiveSenderQueueLimits,
        fileManager: FileManager = .default
    ) throws {
        let standardizedRoot = captureRoot.standardizedFileURL
        var rootInfo = stat()
        guard Darwin.lstat(standardizedRoot.path, &rootInfo) == 0 else {
            throw LiveSenderQueueError.sourceMissing(standardizedRoot.path)
        }
        guard rootInfo.st_mode & S_IFMT != S_IFLNK else {
            throw LiveSenderQueueError.sourceSymlink(standardizedRoot.path)
        }
        guard rootInfo.st_mode & S_IFMT == S_IFDIR else {
            throw LiveSenderQueueError.invalidReference("captureRoot must be a directory")
        }

        let resolvedRoot = standardizedRoot.resolvingSymlinksInPath()
        let standardizedState = stateURL.standardizedFileURL
        let rootPrefix = resolvedRoot.path.hasSuffix("/") ? resolvedRoot.path : resolvedRoot.path + "/"
        let resolvedStateParent = standardizedState.deletingLastPathComponent().resolvingSymlinksInPath()
        let resolvedState = resolvedStateParent.appendingPathComponent(standardizedState.lastPathComponent)
        guard resolvedState.path != resolvedRoot.path, !resolvedState.path.hasPrefix(rootPrefix) else {
            throw LiveSenderQueueError.invalidReference("stateURL must be outside captureRoot")
        }

        self.captureRoot = standardizedRoot
        self.resolvedCaptureRoot = resolvedRoot
        self.stateURL = standardizedState
        self.limits = limits
        self.fileManager = fileManager
    }

    public static func open(
        captureRoot: URL,
        stateURL: URL,
        limits: LiveSenderQueueLimits,
        session: LiveSenderSessionReference,
        fileManager: FileManager = .default
    ) async throws -> LiveSenderQueue {
        let queue = try LiveSenderQueue(
            captureRoot: captureRoot,
            stateURL: stateURL,
            limits: limits,
            fileManager: fileManager
        )
        try await queue.loadOrCreate(session: session)
        return queue
    }

    public func loadOrCreate(session: LiveSenderSessionReference) throws {
        try validate(session)
        if fileManager.fileExists(atPath: stateURL.path) {
            let loaded = try loadState()
            guard loaded.session == session else {
                throw LiveSenderQueueError.sessionConflict
            }
            try validate(loaded)
            state = loaded
            return
        }

        try verify(session)
        let created = PersistentState(
            schema: Self.stateSchema,
            session: session,
            frames: [],
            acknowledgedFrames: [],
            receiverProgress: nil,
            finalization: nil,
            finalized: false
        )
        state = created
        try persist(created)
    }

    public func sessionForSend() throws -> LiveSenderSessionReference {
        guard let state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        try verify(state.session)
        return state.session
    }

    func validateAuthorizationBinding(_ authorization: LiveSenderAuthorizationBinding) throws {
        guard let state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        guard state.session.authorization == authorization else {
            throw LiveSenderQueueError.authorizationMismatch
        }
    }

    func acquireSenderLease(_ lease: UUID) -> Bool {
        guard senderLease == nil else { return false }
        senderLease = lease
        return true
    }

    func releaseSenderLease(_ lease: UUID) {
        if senderLease == lease {
            senderLease = nil
        }
    }

    public func enqueue(_ frame: LiveSenderFrameReference) throws -> LiveSenderEnqueueResult {
        guard var state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        guard !state.finalized else {
            throw LiveSenderQueueError.queueFinalized
        }
        try validate(frame)
        guard frame.sessionID == state.session.sessionID else {
            throw LiveSenderQueueError.sessionConflict
        }
        if let finalization = state.finalization,
           frame.sequenceID > finalization.finalSequenceID {
            throw LiveSenderQueueError.finalizationConflict
        }
        if let expected = state.session.expectedFrameCount, frame.sequenceID > expected {
            throw LiveSenderQueueError.invalidReference("sequence_id exceeds expected_frame_count")
        }
        if let acknowledged = state.acknowledgedFrames.first(where: {
            $0.sequenceID == frame.sequenceID
        }) {
            guard acknowledged.referenceSHA256 == (try frameIdentity(frame).referenceSHA256) else {
                throw LiveSenderQueueError.frameConflict(frame.sequenceID)
            }
            return LiveSenderEnqueueResult(disposition: .duplicate, snapshot: snapshot(from: state))
        }
        if receiverConfirms(frame.sequenceID, progress: state.receiverProgress) {
            return LiveSenderEnqueueResult(disposition: .duplicate, snapshot: snapshot(from: state))
        }
        if let existing = state.frames.first(where: { $0.sequenceID == frame.sequenceID }) {
            guard existing == frame else {
                throw LiveSenderQueueError.frameConflict(frame.sequenceID)
            }
            return LiveSenderEnqueueResult(disposition: .duplicate, snapshot: snapshot(from: state))
        }

        try verify(frame)
        let frameBytes = try bytes(in: frame)
        let queuedBytes = try bytes(in: state.frames)
        let (proposedBytes, overflow) = queuedBytes.addingReportingOverflow(frameBytes)
        guard !overflow,
              state.frames.count < limits.maximumFrames,
              proposedBytes <= limits.maximumBytes else {
            return LiveSenderEnqueueResult(
                disposition: .capacityExceeded,
                snapshot: snapshot(from: state)
            )
        }

        state.frames.append(frame)
        state.frames.sort { $0.sequenceID < $1.sequenceID }
        try persist(state)
        self.state = state
        return LiveSenderEnqueueResult(disposition: .accepted, snapshot: snapshot(from: state))
    }

    public func pendingSelection(limit requestedLimit: Int? = nil) throws -> [LiveSenderFrameReference] {
        guard let state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        let count = min(max(requestedLimit ?? limits.maximumInFlight, 0), limits.maximumInFlight)
        guard count > 0 else {
            return []
        }
        let selected = Array(state.frames.prefix(count))
        for frame in selected {
            try verify(frame)
        }
        return selected
    }

    public func verifiedFileURL(for reference: LiveSenderFileReference) throws -> URL {
        try verify(reference)
    }

    func validateAcknowledgementContract(_ acknowledgement: LiveSenderAcknowledgement) throws {
        guard let state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        try validate(acknowledgement)
        guard acknowledgement.sessionID == state.session.sessionID else {
            throw LiveSenderQueueError.acknowledgementSessionMismatch
        }
        guard acknowledgement.expectedFrameCount == state.session.expectedFrameCount else {
            throw LiveSenderQueueError.invalidAcknowledgement(
                "ACK expected_frame_count conflicts with the queued session"
            )
        }
        if acknowledgement.finalized {
            try validateFinalized(acknowledgement, state: state)
        }
    }

    public func reconcile(_ acknowledgement: LiveSenderAcknowledgement) throws -> LiveSenderReconciliationResult {
        guard var state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        try validateAcknowledgementContract(acknowledgement)

        let incomingProgress = ReceiverProgress(
            receivedCount: acknowledgement.receivedCount,
            contiguousCount: acknowledgement.contiguousCount,
            pendingCount: acknowledgement.pendingCount,
            expectedFrameCount: acknowledgement.expectedFrameCount,
            nextExpectedSequenceID: acknowledgement.nextExpectedSequenceID,
            missingRanges: acknowledgement.missingRanges
        )
        let progress = try mergedProgress(current: state.receiverProgress, incoming: incomingProgress)
        let acknowledged = state.frames.compactMap { frame in
            receiverConfirms(frame.sequenceID, progress: progress) ? frame.sequenceID : nil
        }
        let newlyAcknowledged = try state.frames.compactMap { frame in
            acknowledged.binarySearch(frame.sequenceID) ? try frameIdentity(frame) : nil
        }
        state.acknowledgedFrames.append(contentsOf: newlyAcknowledged.filter { identity in
            !state.acknowledgedFrames.contains(where: { $0.sequenceID == identity.sequenceID })
        })
        state.acknowledgedFrames.sort { $0.sequenceID < $1.sequenceID }
        state.frames.removeAll { frame in
            acknowledged.binarySearch(frame.sequenceID)
        }
        state.receiverProgress = progress
        if acknowledgement.finalized {
            state.frames.removeAll()
            state.acknowledgedFrames.removeAll()
            state.finalization = nil
            state.finalized = true
        }
        try persist(state)
        self.state = state
        return LiveSenderReconciliationResult(
            acknowledgedSequenceIDs: acknowledged,
            snapshot: snapshot(from: state)
        )
    }

    public func setFinalization(_ reference: LiveSenderFinalizationReference) throws {
        guard var state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        guard !state.finalized else {
            throw LiveSenderQueueError.queueFinalized
        }
        guard reference.sessionID == state.session.sessionID else {
            throw LiveSenderQueueError.finalizationConflict
        }
        if let expected = state.session.expectedFrameCount, reference.finalSequenceID != expected {
            throw LiveSenderQueueError.finalizationConflict
        }
        if let existing = state.finalization, existing != reference {
            throw LiveSenderQueueError.finalizationConflict
        }
        let sequenceExceedsFinal = state.frames.contains {
            $0.sequenceID > reference.finalSequenceID
        } || state.acknowledgedFrames.contains {
            $0.sequenceID > reference.finalSequenceID
        }
        let progressExceedsFinal = state.receiverProgress.map {
            $0.receivedCount > reference.finalSequenceID
                || $0.contiguousCount > reference.finalSequenceID
                || $0.nextExpectedSequenceID > reference.finalSequenceID + 1
                || $0.missingRanges.contains { $0.end > reference.finalSequenceID }
        } ?? false
        guard !sequenceExceedsFinal, !progressExceedsFinal else {
            throw LiveSenderQueueError.finalizationConflict
        }
        state.finalization = reference
        try persist(state)
        self.state = state
    }

    public func finalizationForSend() throws -> LiveSenderFinalizationReference? {
        guard let state else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        guard let finalization = state.finalization else { return nil }
        let (next, overflow) = finalization.finalSequenceID.addingReportingOverflow(1)
        guard !state.finalized,
              state.frames.isEmpty,
              let progress = state.receiverProgress,
              !overflow,
              progress.receivedCount == finalization.finalSequenceID,
              progress.contiguousCount == finalization.finalSequenceID,
              progress.pendingCount == 0,
              progress.missingRanges.isEmpty,
              progress.nextExpectedSequenceID == next,
              progress.expectedFrameCount == nil
                  || progress.expectedFrameCount == finalization.finalSequenceID else {
            return nil
        }
        return finalization
    }

    public func snapshot(pendingLimit: Int = 64) throws -> LiveSenderQueueSnapshot {
        guard let state else {
            return emptySnapshot
        }
        return snapshot(from: state, pendingLimit: pendingLimit)
    }

    private var emptySnapshot: LiveSenderQueueSnapshot {
        LiveSenderQueueSnapshot(
            sessionID: nil,
            queuedFrameCount: 0,
            queuedBytes: 0,
            maximumFrames: limits.maximumFrames,
            maximumBytes: limits.maximumBytes,
            pendingSequenceIDs: [],
            omittedPendingCount: 0,
            receiverMissingRanges: [],
            finalizationPending: false,
            finalized: false
        )
    }

    private func snapshot(from state: PersistentState, pendingLimit: Int = 64) -> LiveSenderQueueSnapshot {
        let boundedLimit = min(max(pendingLimit, 0), 256)
        let pending = state.frames.prefix(boundedLimit).map(\.sequenceID)
        return LiveSenderQueueSnapshot(
            sessionID: state.session.sessionID,
            queuedFrameCount: state.frames.count,
            queuedBytes: (try? bytes(in: state.frames)) ?? Int64.max,
            maximumFrames: limits.maximumFrames,
            maximumBytes: limits.maximumBytes,
            pendingSequenceIDs: pending,
            omittedPendingCount: state.frames.count - pending.count,
            receiverMissingRanges: state.receiverProgress?.missingRanges ?? [],
            finalizationPending: state.finalization != nil && !state.finalized,
            finalized: state.finalized
        )
    }

    private func receiverConfirms(_ sequenceID: Int, progress: ReceiverProgress?) -> Bool {
        guard let progress else {
            return false
        }
        if sequenceID < progress.nextExpectedSequenceID {
            return true
        }
        guard let expected = progress.expectedFrameCount, sequenceID <= expected else {
            return false
        }
        return !progress.missingRanges.contains { $0.contains(sequenceID) }
    }

    @discardableResult
    private func verify(_ reference: LiveSenderFileReference) throws -> URL {
        try LiveSenderValidation.fileReference(
            relativePath: reference.relativePath,
            sizeBytes: reference.sizeBytes,
            sha256: reference.sha256,
            mediaType: reference.mediaType
        )
        let url = try confinedURL(for: reference.relativePath)
        var info = stat()
        guard Darwin.lstat(url.path, &info) == 0 else {
            throw LiveSenderQueueError.sourceMissing(reference.relativePath)
        }
        guard info.st_mode & S_IFMT != S_IFLNK else {
            throw LiveSenderQueueError.sourceSymlink(reference.relativePath)
        }
        guard info.st_mode & S_IFMT == S_IFREG else {
            throw LiveSenderQueueError.sourceNotRegularFile(reference.relativePath)
        }
        guard info.st_size == reference.sizeBytes else {
            throw LiveSenderQueueError.sourceSizeMismatch(reference.relativePath)
        }
        guard try Self.sha256(of: url) == reference.sha256 else {
            throw LiveSenderQueueError.sourceChecksumMismatch(reference.relativePath)
        }
        return url
    }

    private func verify(_ frame: LiveSenderFrameReference) throws {
        try validate(frame)
        let metadataURL = try verify(frame.metadata)
        for asset in frame.assets {
            try verify(asset.file)
        }
        let metadata = try decodeMetadata(
            LiveSenderFrameMetadataEvidence.self,
            from: metadataURL,
            reference: frame.metadata
        )
        guard metadata.schema == "capture_splat.live_frame.v0.1",
              metadata.sessionID == frame.sessionID,
              metadata.sequenceID == frame.sequenceID else {
            throw LiveSenderQueueError.invalidReference(
                "frame metadata identity does not match its queue reference"
            )
        }
        let declared = try metadata.declaredAssets()
        let queued = Dictionary(uniqueKeysWithValues: frame.assets.map { ($0.role, $0.file) })
        guard declared == queued else {
            throw LiveSenderQueueError.invalidReference(
                "frame metadata assets do not match the queued file references"
            )
        }
    }

    private func verify(_ session: LiveSenderSessionReference) throws {
        let metadataURL = try verify(session.metadata)
        let metadata = try decodeMetadata(
            LiveSenderSessionMetadataEvidence.self,
            from: metadataURL,
            reference: session.metadata
        )
        guard metadata.schema == "capture_splat.live_session.v0.1",
              metadata.sessionID == session.sessionID,
              metadata.expectedFrameCount == session.expectedFrameCount else {
            throw LiveSenderQueueError.invalidReference(
                "session metadata identity does not match its queue reference"
            )
        }
        try verify(metadata.sourceManifest.reference())
    }

    private func decodeMetadata<T: Decodable>(
        _ type: T.Type,
        from url: URL,
        reference: LiveSenderFileReference
    ) throws -> T {
        guard reference.mediaType == "application/json",
              reference.sizeBytes <= 1024 * 1024 else {
            throw LiveSenderQueueError.invalidReference(
                "live metadata must be application/json and no larger than 1 MiB"
            )
        }
        do {
            return try LiveStrictJSON.decode(T.self, from: Data(contentsOf: url))
        } catch {
            throw LiveSenderQueueError.invalidReference(
                "live metadata is not strict contract JSON"
            )
        }
    }

    private func confinedURL(for relativePath: String) throws -> URL {
        try LiveSenderValidation.safeRelativePath(relativePath)
        var componentURL = captureRoot
        for component in relativePath.split(separator: "/", omittingEmptySubsequences: false) {
            componentURL.appendPathComponent(String(component), isDirectory: false)
            var info = stat()
            if Darwin.lstat(componentURL.path, &info) == 0, info.st_mode & S_IFMT == S_IFLNK {
                throw LiveSenderQueueError.sourceSymlink(relativePath)
            }
        }

        let standardized = captureRoot.appendingPathComponent(relativePath).standardizedFileURL
        let resolved = standardized.resolvingSymlinksInPath()
        let rootPrefix = resolvedCaptureRoot.path.hasSuffix("/")
            ? resolvedCaptureRoot.path
            : resolvedCaptureRoot.path + "/"
        guard resolved.path.hasPrefix(rootPrefix) else {
            throw LiveSenderQueueError.sourceOutsideCaptureRoot(relativePath)
        }
        return standardized
    }

    private func validate(_ session: LiveSenderSessionReference) throws {
        try LiveSenderValidation.sessionID(session.sessionID)
        if let expected = session.expectedFrameCount,
           !(1...LiveSenderValidation.maximumSequenceID).contains(expected) {
            throw LiveSenderQueueError.invalidReference(
                "expected_frame_count must be between 1 and \(LiveSenderValidation.maximumSequenceID)"
            )
        }
        try LiveSenderValidation.validate(session.metadata)
        guard session.metadata.mediaType == "application/json" else {
            throw LiveSenderQueueError.invalidReference("session metadata must use application/json")
        }
    }

    private func validate(_ frame: LiveSenderFrameReference) throws {
        try LiveSenderValidation.frame(
            sessionID: frame.sessionID,
            sequenceID: frame.sequenceID,
            metadata: frame.metadata,
            assets: frame.assets
        )
    }

    private func validate(_ acknowledgement: LiveSenderAcknowledgement) throws {
        guard acknowledgement.schema == "capture_splat.live_ack.v0.1" else {
            throw LiveSenderQueueError.invalidAcknowledgement("unsupported ACK schema")
        }
        try LiveSenderValidation.sessionID(acknowledgement.sessionID)
        try LiveSenderValidation.missingRanges(acknowledgement.missingRanges)
        guard acknowledgement.receivedCount >= 0,
              acknowledgement.contiguousCount >= 0,
              acknowledgement.pendingCount >= 0,
              acknowledgement.contiguousCount <= acknowledgement.receivedCount else {
            throw LiveSenderQueueError.invalidAcknowledgement("ACK counts are invalid")
        }
        let (nextExpected, nextOverflow) = acknowledgement.contiguousCount.addingReportingOverflow(1)
        let expectedPending = acknowledgement.receivedCount - acknowledgement.contiguousCount
        guard !nextOverflow,
              acknowledgement.nextExpectedSequenceID == nextExpected,
              acknowledgement.pendingCount == expectedPending else {
            throw LiveSenderQueueError.invalidAcknowledgement("ACK counts are invalid")
        }
        if let expected = acknowledgement.expectedFrameCount {
            guard expected >= acknowledgement.receivedCount,
                  expected >= acknowledgement.contiguousCount,
                  acknowledgement.missingRanges.allSatisfy({
                      $0.start > acknowledgement.contiguousCount && $0.end <= expected
                  }) else {
                throw LiveSenderQueueError.invalidAcknowledgement(
                    "ACK expected count conflicts with its progress"
                )
            }
            var missing = 0
            for range in acknowledgement.missingRanges {
                let (length, lengthOverflow) = (range.end - range.start).addingReportingOverflow(1)
                let (total, totalOverflow) = missing.addingReportingOverflow(length)
                guard !lengthOverflow, !totalOverflow else {
                    throw LiveSenderQueueError.invalidAcknowledgement(
                        "ACK missing range count overflowed"
                    )
                }
                missing = total
            }
            let firstMissing = acknowledgement.missingRanges.first?.start
            guard missing == expected - acknowledgement.receivedCount,
                  (acknowledgement.contiguousCount == expected
                      ? acknowledgement.missingRanges.isEmpty
                      : firstMissing == acknowledgement.nextExpectedSequenceID) else {
                throw LiveSenderQueueError.invalidAcknowledgement(
                    "ACK missing ranges do not match its received count"
                )
            }
        }
        if let sequence = acknowledgement.sequenceID, sequence < 1 {
            throw LiveSenderQueueError.invalidAcknowledgement("sequence_id must be positive")
        }
        switch acknowledgement.operation {
        case .frame:
            guard acknowledgement.sequenceID != nil,
                  acknowledgement.assetRole == nil,
                  acknowledgement.status == .incomplete
                      || acknowledgement.status == .duplicate else {
                throw LiveSenderQueueError.invalidAcknowledgement("Frame ACK identity is invalid")
            }
        case .asset:
            guard acknowledgement.sequenceID != nil,
                  acknowledgement.assetRole != nil,
                  acknowledgement.status == .incomplete
                      || acknowledgement.status == .accepted
                      || acknowledgement.status == .duplicate else {
                throw LiveSenderQueueError.invalidAcknowledgement("Asset ACK identity is invalid")
            }
        case .session:
            guard acknowledgement.sequenceID == nil,
                  acknowledgement.assetRole == nil,
                  acknowledgement.status == .accepted
                      || acknowledgement.status == .duplicate else {
                throw LiveSenderQueueError.invalidAcknowledgement("Session ACK identity is invalid")
            }
        case .resume:
            guard acknowledgement.sequenceID == nil,
                  acknowledgement.assetRole == nil,
                  acknowledgement.status == .accepted
                      || acknowledgement.status == .finalized else {
                throw LiveSenderQueueError.invalidAcknowledgement("Session ACK identity is invalid")
            }
        case .finalize:
            guard acknowledgement.sequenceID == nil,
                  acknowledgement.assetRole == nil,
                  acknowledgement.status == .finalized else {
                throw LiveSenderQueueError.invalidAcknowledgement("Finalization ACK identity is invalid")
            }
        }
        if acknowledgement.finalized {
            guard acknowledgement.status == .finalized
                      || (acknowledgement.operation == .session
                          && acknowledgement.status == .duplicate),
                  acknowledgement.missingRanges.isEmpty,
                  acknowledgement.pendingCount == 0 else {
                throw LiveSenderQueueError.invalidAcknowledgement("Finalized ACK is inconsistent")
            }
        } else if acknowledgement.status == .finalized {
            throw LiveSenderQueueError.invalidAcknowledgement(
                "Non-finalized ACK cannot use finalized status"
            )
        }
    }

    private func validateFinalized(
        _ acknowledgement: LiveSenderAcknowledgement,
        state: PersistentState
    ) throws {
        guard let finalization = state.finalization else {
            throw LiveSenderQueueError.invalidAcknowledgement(
                "Finalized ACK has no matching local finalization request"
            )
        }
        let (next, overflow) = finalization.finalSequenceID.addingReportingOverflow(1)
        guard !overflow,
              acknowledgement.receivedCount == finalization.finalSequenceID,
              acknowledgement.contiguousCount == finalization.finalSequenceID,
              acknowledgement.pendingCount == 0,
              acknowledgement.nextExpectedSequenceID == next,
              acknowledgement.missingRanges.isEmpty else {
            throw LiveSenderQueueError.invalidAcknowledgement(
                "Finalized ACK does not prove the declared final sequence"
            )
        }
    }

    private func mergedProgress(
        current: ReceiverProgress?,
        incoming: ReceiverProgress
    ) throws -> ReceiverProgress {
        guard let current else { return incoming }
        if progress(incoming, dominates: current) {
            return incoming
        }
        if progress(current, dominates: incoming) {
            return current
        }
        throw LiveSenderQueueError.invalidAcknowledgement(
            "ACK progress conflicts with previously durable receiver state"
        )
    }

    private func progress(_ candidate: ReceiverProgress, dominates prior: ReceiverProgress) -> Bool {
        guard candidate.expectedFrameCount == prior.expectedFrameCount,
              candidate.receivedCount >= prior.receivedCount,
              candidate.contiguousCount >= prior.contiguousCount else {
            return false
        }
        guard candidate.expectedFrameCount != nil else {
            return true
        }
        return missingRanges(candidate.missingRanges, areSubsetOf: prior.missingRanges)
    }

    private func missingRanges(
        _ candidate: [LiveSenderMissingRange],
        areSubsetOf prior: [LiveSenderMissingRange]
    ) -> Bool {
        var priorIndex = 0
        for range in candidate {
            while priorIndex < prior.count, prior[priorIndex].end < range.start {
                priorIndex += 1
            }
            guard priorIndex < prior.count,
                  prior[priorIndex].start <= range.start,
                  prior[priorIndex].end >= range.end else {
                return false
            }
        }
        return true
    }

    private func validate(_ state: PersistentState) throws {
        guard state.schema == Self.stateSchema else {
            throw LiveSenderQueueError.stateCorrupt("unsupported sender queue state schema")
        }
        try validate(state.session)
        guard state.frames.count <= limits.maximumFrames else {
            throw LiveSenderQueueError.stateCorrupt("persisted frame count exceeds the configured limit")
        }
        guard try bytes(in: state.frames) <= limits.maximumBytes else {
            throw LiveSenderQueueError.stateCorrupt("persisted byte count exceeds the configured limit")
        }
        var priorSequenceID = 0
        for frame in state.frames {
            try validate(frame)
            guard frame.sessionID == state.session.sessionID,
                  frame.sequenceID > priorSequenceID else {
                throw LiveSenderQueueError.stateCorrupt("persisted frames are not unique and ordered")
            }
            priorSequenceID = frame.sequenceID
        }
        priorSequenceID = 0
        for acknowledged in state.acknowledgedFrames {
            guard acknowledged.sequenceID > priorSequenceID,
                  LiveSenderValidation.isSHA256(acknowledged.referenceSHA256),
                  !state.frames.contains(where: { $0.sequenceID == acknowledged.sequenceID }) else {
                throw LiveSenderQueueError.stateCorrupt(
                    "persisted acknowledged frame identities are invalid"
                )
            }
            priorSequenceID = acknowledged.sequenceID
        }
        if let progress = state.receiverProgress {
            guard progress.receivedCount >= 0,
                  progress.contiguousCount >= 0,
                  progress.pendingCount >= 0,
                  progress.contiguousCount <= progress.receivedCount,
                  progress.pendingCount == progress.receivedCount - progress.contiguousCount,
                  progress.nextExpectedSequenceID > 0 else {
                throw LiveSenderQueueError.stateCorrupt("persisted receiver progress is invalid")
            }
            if let expected = progress.expectedFrameCount, expected < 1 {
                throw LiveSenderQueueError.stateCorrupt("persisted expected_frame_count is invalid")
            }
            try LiveSenderValidation.missingRanges(progress.missingRanges)
        }
        if let finalization = state.finalization {
            guard finalization.sessionID == state.session.sessionID,
                  (1...LiveSenderValidation.maximumSequenceID)
                    .contains(finalization.finalSequenceID) else {
                throw LiveSenderQueueError.stateCorrupt("persisted finalization is invalid")
            }
        }
        guard !state.finalized || (state.frames.isEmpty && state.finalization == nil) else {
            throw LiveSenderQueueError.stateCorrupt("finalized state still contains pending work")
        }
    }

    private func bytes(in frame: LiveSenderFrameReference) throws -> Int64 {
        try ([frame.metadata] + frame.assets.map(\.file)).reduce(into: Int64(0)) { total, reference in
            let (next, overflow) = total.addingReportingOverflow(reference.sizeBytes)
            guard !overflow else {
                throw LiveSenderQueueError.invalidReference("queued byte count overflow")
            }
            total = next
        }
    }

    private func bytes(in frames: [LiveSenderFrameReference]) throws -> Int64 {
        try frames.reduce(into: Int64(0)) { total, frame in
            let (next, overflow) = total.addingReportingOverflow(try bytes(in: frame))
            guard !overflow else {
                throw LiveSenderQueueError.invalidReference("queued byte count overflow")
            }
            total = next
        }
    }

    private func frameIdentity(_ frame: LiveSenderFrameReference) throws -> AcknowledgedFrameIdentity {
        AcknowledgedFrameIdentity(
            sequenceID: frame.sequenceID,
            referenceSHA256: Self.sha256(try Self.canonicalData(frame))
        )
    }

    private func loadState() throws -> PersistentState {
        let data: Data
        do {
            data = try Data(contentsOf: stateURL, options: .mappedIfSafe)
        } catch {
            throw LiveSenderQueueError.stateCorrupt("unable to read sender queue state")
        }
        guard data.count <= Self.maximumEnvelopeBytes else {
            throw LiveSenderQueueError.stateCorrupt("sender queue state exceeds its size limit")
        }

        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data, options: [])
        } catch {
            throw LiveSenderQueueError.stateCorrupt("sender queue state is not valid JSON")
        }
        guard let dictionary = object as? [String: Any],
              Set(dictionary.keys) == Set(["schema", "payload_sha256", "payload_base64"]),
              dictionary["schema"] as? String == Self.envelopeSchema,
              let expectedHash = dictionary["payload_sha256"] as? String,
              let encodedPayload = dictionary["payload_base64"] as? String,
              let payload = Data(base64Encoded: encodedPayload) else {
            throw LiveSenderQueueError.stateCorrupt("sender queue envelope is invalid")
        }
        guard Self.sha256(payload) == expectedHash else {
            throw LiveSenderQueueError.stateCorrupt("sender queue payload checksum does not match")
        }

        let decoded: PersistentState
        do {
            decoded = try JSONDecoder().decode(PersistentState.self, from: payload)
        } catch {
            throw LiveSenderQueueError.stateCorrupt("sender queue payload cannot be decoded")
        }
        let canonicalPayload = try Self.canonicalData(decoded)
        guard payload == canonicalPayload else {
            throw LiveSenderQueueError.stateCorrupt("sender queue payload is not canonical")
        }
        let envelope = PersistentEnvelope(
            schema: Self.envelopeSchema,
            payloadSHA256: expectedHash,
            payloadBase64: encodedPayload
        )
        guard data == (try Self.canonicalData(envelope)) else {
            throw LiveSenderQueueError.stateCorrupt("sender queue envelope is not canonical")
        }
        return decoded
    }

    private func persist(_ state: PersistentState) throws {
        try validate(state)
        let payload = try Self.canonicalData(state)
        guard payload.count <= Self.maximumPayloadBytes else {
            throw LiveSenderQueueError.persistenceFailed("sender queue state exceeds its size limit")
        }
        let envelope = PersistentEnvelope(
            schema: Self.envelopeSchema,
            payloadSHA256: Self.sha256(payload),
            payloadBase64: payload.base64EncodedString()
        )
        let data = try Self.canonicalData(envelope)
        guard data.count <= Self.maximumEnvelopeBytes else {
            throw LiveSenderQueueError.persistenceFailed("sender queue envelope exceeds its size limit")
        }
        do {
            try Self.atomicWrite(data, to: stateURL, fileManager: fileManager)
        } catch let error as LiveSenderQueueError {
            throw error
        } catch {
            throw LiveSenderQueueError.persistenceFailed(error.localizedDescription)
        }
    }

    private static func canonicalData<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        do {
            return try encoder.encode(value)
        } catch {
            throw LiveSenderQueueError.stateCorrupt("sender queue state cannot be encoded")
        }
    }

    private static func sha256(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).hex
    }

    private static func sha256(of url: URL) throws -> String {
        let handle: FileHandle
        do {
            handle = try FileHandle(forReadingFrom: url)
        } catch {
            throw LiveSenderQueueError.sourceMissing(url.lastPathComponent)
        }
        defer {
            try? handle.close()
        }
        var hasher = SHA256()
        do {
            while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
                hasher.update(data: chunk)
            }
        } catch {
            throw LiveSenderQueueError.sourceMissing(url.lastPathComponent)
        }
        return "sha256:" + hasher.finalize().hex
    }

    private static func atomicWrite(_ data: Data, to destination: URL, fileManager: FileManager) throws {
        let directory = destination.deletingLastPathComponent()
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            throw LiveSenderQueueError.persistenceFailed("unable to create sender state directory")
        }

        let temporary = directory.appendingPathComponent(".\(destination.lastPathComponent).\(UUID().uuidString).incoming")
        guard fileManager.createFile(
            atPath: temporary.path,
            contents: nil,
            attributes: [.posixPermissions: 0o600]
        ) else {
            throw LiveSenderQueueError.persistenceFailed("unable to create temporary sender state")
        }
        do {
            let handle = try FileHandle(forWritingTo: temporary)
            try handle.write(contentsOf: data)
            try handle.synchronize()
            try handle.close()

            guard Darwin.rename(temporary.path, destination.path) == 0 else {
                throw LiveSenderQueueError.persistenceFailed("unable to atomically replace sender state")
            }
            let directoryDescriptor = Darwin.open(directory.path, O_RDONLY)
            guard directoryDescriptor >= 0 else {
                throw LiveSenderQueueError.persistenceFailed("unable to open sender state directory")
            }
            defer { Darwin.close(directoryDescriptor) }
            guard Darwin.fsync(directoryDescriptor) == 0 else {
                throw LiveSenderQueueError.persistenceFailed("unable to synchronize sender state directory")
            }
        } catch {
            try? fileManager.removeItem(at: temporary)
            if let queueError = error as? LiveSenderQueueError {
                throw queueError
            }
            throw LiveSenderQueueError.persistenceFailed(error.localizedDescription)
        }
    }
}

private struct ReceiverProgress: Codable, Equatable {
    let receivedCount: Int
    let contiguousCount: Int
    let pendingCount: Int
    let expectedFrameCount: Int?
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

private struct PersistentState: Codable, Equatable {
    let schema: String
    let session: LiveSenderSessionReference
    var frames: [LiveSenderFrameReference]
    var acknowledgedFrames: [AcknowledgedFrameIdentity]
    var receiverProgress: ReceiverProgress?
    var finalization: LiveSenderFinalizationReference?
    var finalized: Bool

    enum CodingKeys: String, CodingKey {
        case schema, session, frames
        case acknowledgedFrames = "acknowledged_frames"
        case receiverProgress = "receiver_progress"
        case finalization, finalized
    }
}

private struct AcknowledgedFrameIdentity: Codable, Equatable {
    let sequenceID: Int
    let referenceSHA256: String

    enum CodingKeys: String, CodingKey {
        case sequenceID = "sequence_id"
        case referenceSHA256 = "reference_sha256"
    }
}

private struct PersistentEnvelope: Codable, Equatable {
    let schema: String
    let payloadSHA256: String
    let payloadBase64: String

    enum CodingKeys: String, CodingKey {
        case schema
        case payloadSHA256 = "payload_sha256"
        case payloadBase64 = "payload_base64"
    }
}

private struct LiveSenderSessionMetadataEvidence: Decodable {
    let schema: String
    let sessionID: String
    let expectedFrameCount: Int?
    let sourceManifest: LiveSenderSourceManifestEvidence

    enum CodingKeys: String, CodingKey {
        case schema
        case sessionID = "session_id"
        case expectedFrameCount = "expected_frame_count"
        case sourceManifest = "source_manifest"
    }
}

private struct LiveSenderSourceManifestEvidence: Decodable {
    let path: String
    let sizeBytes: Int64
    let sha256: String

    enum CodingKeys: String, CodingKey {
        case path
        case sizeBytes = "size_bytes"
        case sha256
    }

    func reference() throws -> LiveSenderFileReference {
        try LiveSenderFileReference(
            relativePath: path,
            sizeBytes: sizeBytes,
            sha256: sha256,
            mediaType: "application/json"
        )
    }
}

private struct LiveSenderDeclaredFileEvidence: Decodable {
    let path: String
    let sizeBytes: Int64
    let sha256: String
    let mediaType: String

    enum CodingKeys: String, CodingKey {
        case path
        case sizeBytes = "size_bytes"
        case sha256
        case mediaType = "media_type"
    }

    func reference() throws -> LiveSenderFileReference {
        try LiveSenderFileReference(
            relativePath: path,
            sizeBytes: sizeBytes,
            sha256: sha256,
            mediaType: mediaType
        )
    }
}

private struct LiveSenderMaskEvidence: Decodable {
    let kind: String
    let path: String
    let sizeBytes: Int64
    let sha256: String
    let mediaType: String

    enum CodingKeys: String, CodingKey {
        case kind, path
        case sizeBytes = "size_bytes"
        case sha256
        case mediaType = "media_type"
    }

    func role() throws -> LiveSenderAssetRole {
        switch kind {
        case "person": return .maskPerson
        case "valid": return .maskValid
        case "object": return .maskObject
        default:
            throw LiveSenderQueueError.invalidReference("frame metadata mask kind is invalid")
        }
    }

    func reference() throws -> LiveSenderFileReference {
        try LiveSenderFileReference(
            relativePath: path,
            sizeBytes: sizeBytes,
            sha256: sha256,
            mediaType: mediaType
        )
    }
}

private struct LiveSenderOptionalAssetEvidence: Decodable {
    let depth: LiveSenderDeclaredFileEvidence?
    let confidence: LiveSenderDeclaredFileEvidence?
    let masks: [LiveSenderMaskEvidence]?
}

private struct LiveSenderFrameMetadataEvidence: Decodable {
    let schema: String
    let sessionID: String
    let sequenceID: Int
    let sourceFrame: LiveSenderDeclaredFileEvidence
    let assets: LiveSenderOptionalAssetEvidence?

    enum CodingKeys: String, CodingKey {
        case schema
        case sessionID = "session_id"
        case sequenceID = "sequence_id"
        case sourceFrame = "source_frame"
        case assets
    }

    func declaredAssets() throws -> [LiveSenderAssetRole: LiveSenderFileReference] {
        var result: [LiveSenderAssetRole: LiveSenderFileReference] = [
            .source: try sourceFrame.reference(),
        ]
        if let depth = assets?.depth {
            result[.depth] = try depth.reference()
        }
        if let confidence = assets?.confidence {
            result[.confidence] = try confidence.reference()
        }
        for mask in assets?.masks ?? [] {
            let role = try mask.role()
            guard result[role] == nil else {
                throw LiveSenderQueueError.invalidReference(
                    "frame metadata asset roles must be unique"
                )
            }
            result[role] = try mask.reference()
        }
        return result
    }
}

private enum LiveSenderValidation {
    static let maximumSequenceID = 99_999_999

    static func sessionID(_ value: String) throws {
        guard !value.isEmpty, value.count <= 128,
              value.first?.isASCIIAlphaNumeric == true,
              value.allSatisfy({ $0.isASCIIAlphaNumeric || "._-".contains($0) }) else {
            throw LiveSenderQueueError.invalidReference(
                "session_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}"
            )
        }
    }

    static func safeRelativePath(_ value: String) throws {
        let components = value.split(separator: "/", omittingEmptySubsequences: false)
        let looksLikeURI: Bool
        if let colon = value.firstIndex(of: ":") {
            let scheme = value[..<colon]
            looksLikeURI = scheme.first?.isASCIIAlpha == true
                && scheme.dropFirst().allSatisfy { $0.isASCIIAlphaNumeric || "+.-".contains($0) }
        } else {
            looksLikeURI = false
        }
        guard !value.isEmpty,
              !value.hasPrefix("/"),
              !value.hasSuffix("/"),
              !value.contains("\\"),
              !value.unicodeScalars.contains(where: { $0.value == 0 }),
              !looksLikeURI,
              !components.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else {
            throw LiveSenderQueueError.unsafeRelativePath(value)
        }
    }

    static func fileReference(
        relativePath: String,
        sizeBytes: Int64,
        sha256: String,
        mediaType: String
    ) throws {
        try safeRelativePath(relativePath)
        guard sizeBytes > 0 else {
            throw LiveSenderQueueError.invalidReference("size_bytes must be positive")
        }
        guard isSHA256(sha256) else {
            throw LiveSenderQueueError.invalidReference("sha256 must use 64 lowercase hexadecimal digits")
        }
        let mediaParts = mediaType.split(separator: "/", omittingEmptySubsequences: false)
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789!#$&^_.+-")
        guard mediaParts.count == 2,
              mediaParts.allSatisfy({
                  $0.first?.isASCIIAlphaNumeric == true
                      && $0.unicodeScalars.allSatisfy(allowed.contains)
              }) else {
            throw LiveSenderQueueError.invalidReference("media_type must be a lowercase MIME type")
        }
    }

    static func isSHA256(_ value: String) -> Bool {
        value.count == 71
            && value.hasPrefix("sha256:")
            && value.dropFirst(7).allSatisfy(\.isLowercaseHexDigit)
    }

    static func validate(_ reference: LiveSenderFileReference) throws {
        try fileReference(
            relativePath: reference.relativePath,
            sizeBytes: reference.sizeBytes,
            sha256: reference.sha256,
            mediaType: reference.mediaType
        )
    }

    static func frame(
        sessionID: String,
        sequenceID: Int,
        metadata: LiveSenderFileReference,
        assets: [LiveSenderAssetReference]
    ) throws {
        try self.sessionID(sessionID)
        guard (1...maximumSequenceID).contains(sequenceID) else {
            throw LiveSenderQueueError.invalidReference(
                "sequence_id must be between 1 and \(maximumSequenceID)"
            )
        }
        try validate(metadata)
        guard metadata.mediaType == "application/json" else {
            throw LiveSenderQueueError.invalidReference("frame metadata must use application/json")
        }
        guard !assets.isEmpty, assets.contains(where: { $0.role == .source }) else {
            throw LiveSenderQueueError.invalidReference("a frame must contain one source asset")
        }
        var roles = Set<LiveSenderAssetRole>()
        for asset in assets {
            guard roles.insert(asset.role).inserted else {
                throw LiveSenderQueueError.invalidReference("frame asset roles must be unique")
            }
            try validate(asset.file)
        }
    }

    static func missingRanges(_ ranges: [LiveSenderMissingRange]) throws {
        var previousEnd = 0
        for range in ranges {
            guard range.start > previousEnd, range.end >= range.start else {
                throw LiveSenderQueueError.invalidAcknowledgement(
                    "missing ranges must be sorted and disjoint"
                )
            }
            previousEnd = range.end
        }
    }
}

private extension Character {
    var isASCIIAlpha: Bool {
        unicodeScalars.count == 1 && unicodeScalars.first.map {
            (65...90).contains($0.value) || (97...122).contains($0.value)
        } == true
    }

    var isASCIIAlphaNumeric: Bool {
        isASCIIAlpha || (unicodeScalars.count == 1 && unicodeScalars.first.map {
            (48...57).contains($0.value)
        } == true)
    }

    var isLowercaseHexDigit: Bool {
        unicodeScalars.count == 1 && unicodeScalars.first.map {
            (48...57).contains($0.value) || (97...102).contains($0.value)
        } == true
    }
}

private extension SHA256.Digest {
    var hex: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

private extension Array where Element == Int {
    func binarySearch(_ value: Int) -> Bool {
        var lower = startIndex
        var upper = endIndex
        while lower < upper {
            let middle = lower + distance(from: lower, to: upper) / 2
            if self[middle] == value {
                return true
            }
            if self[middle] < value {
                lower = middle + 1
            } else {
                upper = middle
            }
        }
        return false
    }
}

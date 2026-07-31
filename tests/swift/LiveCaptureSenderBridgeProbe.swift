import CoreGraphics
import CryptoKit
import Foundation
import ImageIO

private final class FixedRandom: LiveRandomSource, @unchecked Sendable {
    private let lock = NSLock()
    private let value: Data
    private var calls = 0

    init(_ value: Data) {
        self.value = value
    }

    func bytes(count: Int) throws -> Data {
        lock.withLock {
            calls += 1
            return Data(value.prefix(count))
        }
    }

    func callCount() -> Int {
        lock.withLock { calls }
    }
}

private actor BlockingRequester: LiveAuthenticatedRequesting {
    private let authorization: LiveSenderAuthorizationBinding
    private var blocked = true
    private var requestCount = 0

    init(authorization: LiveSenderAuthorizationBinding) {
        self.authorization = authorization
    }

    func validateSenderAuthorization(
        now: Date
    ) async throws -> LiveSenderAuthorizationBinding {
        authorization
    }

    func perform(
        method: String,
        path: String,
        body: LiveAuthenticatedBody,
        now: Date
    ) async throws -> Data {
        requestCount += 1
        while blocked {
            try await Task.sleep(nanoseconds: 2_000_000)
        }
        throw LiveAuthenticatedRequestError.network("probe transport interrupted")
    }

    func calls() -> Int {
        requestCount
    }

    func unblock() {
        blocked = false
    }
}

private actor ProbeConnector: LiveCaptureSenderConnecting {
    private let context: LiveCaptureSenderConnectionContext
    private let transport: BlockingRequester
    private var contextCalls = 0

    init(
        context: LiveCaptureSenderConnectionContext,
        transport: BlockingRequester
    ) {
        self.context = context
        self.transport = transport
    }

    func currentContext(now: Date) async throws -> LiveCaptureSenderConnectionContext {
        contextCalls += 1
        return context
    }

    func requester(
        for context: LiveCaptureSenderConnectionContext
    ) async throws -> any LiveAuthenticatedRequesting {
        guard context == self.context else {
            throw LiveSenderQueueError.authorizationMismatch
        }
        return transport
    }

    func calls() -> Int {
        contextCalls
    }
}

private actor RecordingSleeper: LiveSenderSleeping {
    private var recordedDelays: [Int] = []

    func sleep(milliseconds: Int) async throws {
        recordedDelays.append(milliseconds)
    }

    func delays() -> [Int] {
        recordedDelays
    }
}

private actor RecoveringRequester: LiveAuthenticatedRequesting {
    private let authorization: LiveSenderAuthorizationBinding
    private let sessionID: String
    private let failureCount: Int
    private var requestCount = 0

    init(
        authorization: LiveSenderAuthorizationBinding,
        sessionID: String,
        failureCount: Int
    ) {
        self.authorization = authorization
        self.sessionID = sessionID
        self.failureCount = failureCount
    }

    func validateSenderAuthorization(
        now: Date
    ) async throws -> LiveSenderAuthorizationBinding {
        authorization
    }

    func perform(
        method: String,
        path: String,
        body: LiveAuthenticatedBody,
        now: Date
    ) async throws -> Data {
        requestCount += 1
        if requestCount <= failureCount {
            throw LiveAuthenticatedRequestError.network(
                "probe receiver unavailable"
            )
        }
        let operation: LiveSenderAcknowledgement.Operation
        switch method {
        case "PUT":
            operation = .session
        case "GET":
            operation = .resume
        default:
            throw LiveAuthenticatedRequestError.corruptBody(
                "unexpected probe operation"
            )
        }
        let acknowledgement = try LiveSenderAcknowledgement(
            sessionID: sessionID,
            operation: operation,
            status: .accepted,
            receivedCount: 0,
            contiguousCount: 0,
            pendingCount: 0,
            expectedFrameCount: nil,
            nextExpectedSequenceID: 1,
            missingRanges: [],
            finalized: false
        )
        return try LiveStrictJSON.canonicalData(acknowledgement)
    }

    func calls() -> Int {
        requestCount
    }
}

private actor RecoveringConnector: LiveCaptureSenderConnecting {
    private let context: LiveCaptureSenderConnectionContext
    private let requesterValue: RecoveringRequester
    private var contextCalls = 0

    init(
        context: LiveCaptureSenderConnectionContext,
        requester: RecoveringRequester
    ) {
        self.context = context
        requesterValue = requester
    }

    func currentContext(now: Date) async throws -> LiveCaptureSenderConnectionContext {
        contextCalls += 1
        return context
    }

    func requester(
        for context: LiveCaptureSenderConnectionContext
    ) async throws -> any LiveAuthenticatedRequesting {
        guard context == self.context else {
            throw LiveSenderQueueError.authorizationMismatch
        }
        return requesterValue
    }

    func calls() -> Int {
        contextCalls
    }
}

private actor GatedContextConnector: LiveCaptureSenderConnecting {
    private let context: LiveCaptureSenderConnectionContext
    private let requesterValue: BlockingRequester
    private var contextCalls = 0

    init(
        context: LiveCaptureSenderConnectionContext,
        requester: BlockingRequester
    ) {
        self.context = context
        requesterValue = requester
    }

    func currentContext(now: Date) async throws -> LiveCaptureSenderConnectionContext {
        contextCalls += 1
        while true {
            try await Task.sleep(nanoseconds: 2_000_000)
        }
    }

    func requester(
        for context: LiveCaptureSenderConnectionContext
    ) async throws -> any LiveAuthenticatedRequesting {
        requesterValue
    }

    func calls() -> Int {
        contextCalls
    }
}

private actor SlidingWindowRequester: LiveAuthenticatedRequesting {
    private let authorization: LiveSenderAuthorizationBinding
    private let sessionID: String
    private let finalSequenceID: Int
    private var contiguousCount = 0
    private var completedSequences: [Int] = []
    private var didFinalize = false

    init(
        authorization: LiveSenderAuthorizationBinding,
        sessionID: String,
        finalSequenceID: Int
    ) {
        self.authorization = authorization
        self.sessionID = sessionID
        self.finalSequenceID = finalSequenceID
    }

    func validateSenderAuthorization(
        now: Date
    ) async throws -> LiveSenderAuthorizationBinding {
        authorization
    }

    func perform(
        method: String,
        path: String,
        body: LiveAuthenticatedBody,
        now: Date
    ) async throws -> Data {
        if path.hasSuffix("/finalize") {
            guard contiguousCount == finalSequenceID else {
                throw LiveAuthenticatedRequestError.corruptBody(
                    "finalization arrived before every frame"
                )
            }
            didFinalize = true
            return try acknowledgement(
                operation: .finalize,
                status: .finalized,
                expectedFrameCount: finalSequenceID,
                finalized: true
            )
        }
        let components = path.split(separator: "/").map(String.init)
        if let framesIndex = components.firstIndex(of: "frames"),
           framesIndex + 1 < components.count,
           let sequenceID = Int(components[framesIndex + 1]) {
            if let assetsIndex = components.firstIndex(of: "assets"),
               assetsIndex + 1 < components.count,
               let role = LiveSenderAssetRole(
                   rawValue: components[assetsIndex + 1]
               ) {
                let completesFrame = role == .confidence
                if completesFrame {
                    guard sequenceID == contiguousCount + 1 else {
                        throw LiveAuthenticatedRequestError.corruptBody(
                            "frame completion was not contiguous"
                        )
                    }
                    contiguousCount = sequenceID
                    completedSequences.append(sequenceID)
                }
                return try acknowledgement(
                    operation: .asset,
                    status: completesFrame ? .accepted : .incomplete,
                    sequenceID: sequenceID,
                    assetRole: role
                )
            }
            return try acknowledgement(
                operation: .frame,
                status: .incomplete,
                sequenceID: sequenceID
            )
        }
        return try acknowledgement(
            operation: method == "GET" ? .resume : .session,
            status: .accepted
        )
    }

    func sequences() -> [Int] {
        completedSequences
    }

    func finalized() -> Bool {
        didFinalize
    }

    private func acknowledgement(
        operation: LiveSenderAcknowledgement.Operation,
        status: LiveSenderAcknowledgement.Status,
        sequenceID: Int? = nil,
        assetRole: LiveSenderAssetRole? = nil,
        expectedFrameCount: Int? = nil,
        finalized: Bool = false
    ) throws -> Data {
        try LiveStrictJSON.canonicalData(
            LiveSenderAcknowledgement(
                sessionID: sessionID,
                operation: operation,
                status: status,
                sequenceID: sequenceID,
                assetRole: assetRole,
                receivedCount: contiguousCount,
                contiguousCount: contiguousCount,
                pendingCount: 0,
                expectedFrameCount: expectedFrameCount,
                nextExpectedSequenceID: contiguousCount + 1,
                missingRanges: [],
                finalized: finalized
            )
        )
    }
}

private actor SlidingWindowConnector: LiveCaptureSenderConnecting {
    private let context: LiveCaptureSenderConnectionContext
    private let requesterValue: SlidingWindowRequester

    init(
        context: LiveCaptureSenderConnectionContext,
        requester: SlidingWindowRequester
    ) {
        self.context = context
        requesterValue = requester
    }

    func currentContext(now: Date) async throws -> LiveCaptureSenderConnectionContext {
        context
    }

    func requester(
        for context: LiveCaptureSenderConnectionContext
    ) async throws -> any LiveAuthenticatedRequesting {
        guard context == self.context else {
            throw LiveSenderQueueError.authorizationMismatch
        }
        return requesterValue
    }
}

private struct ForgedPendingPointer: Codable {
    let schema: String
    let captureDirectoryName: String
    let createdAt: String
    let desktopID: String
    let sessionID: String
    let metadata: LiveSenderFileReference

    enum CodingKeys: String, CodingKey {
        case schema
        case captureDirectoryName = "capture_directory_name"
        case createdAt = "created_at"
        case desktopID = "desktop_id"
        case sessionID = "session_id"
        case metadata
    }
}

private struct ForgedPendingEnvelope: Codable {
    let schema: String
    let payloadBase64URL: String
    let payloadSHA256: String

    enum CodingKeys: String, CodingKey {
        case schema
        case payloadBase64URL = "payload_b64u"
        case payloadSHA256 = "payload_sha256"
    }
}

@main
private enum LiveCaptureSenderBridgeProbe {
    static func main() async throws {
        guard CommandLine.arguments.count == 3 else {
            throw LiveAuthContractError.invalid("usage: probe SCENARIO WORKING_ROOT")
        }
        let root = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        switch CommandLine.arguments[1] {
        case "metadata":
            try metadata(root: root)
        case "file-evidence":
            try await fileEvidence(root: root)
        case "bridge":
            try await bridge(root: root)
        case "retry-recovery":
            try await retryRecovery(root: root)
        case "pairing-environment":
            try await pairingAndEnvironment(root: root)
        case "pending-crash":
            try await pendingCrash(root: root)
        case "desktop-gating":
            try await desktopGating(root: root)
        case "empty-abort":
            try await emptyAbort(root: root)
        case "abandon-pointers":
            try await abandonPointers(root: root)
        case "tampered-recovery":
            try await tamperedRecovery(root: root)
        case "tiny-capacity":
            try await tinyCapacity(root: root)
        case "current-pending-conflict":
            try await currentPendingConflict(root: root)
        default:
            throw LiveAuthContractError.invalid("unknown probe scenario")
        }
    }

    private static func metadata(root: URL) throws {
        let seed = Data(0..<32)
        let date = try LiveAuthTime.parse("2026-07-30T10:00:00.000Z")
        let encoded = try LiveCaptureMetadataEncoder.session(
            seed: seed,
            createdAt: date
        )
        let expectedSession = Data(
            """
            {"authority":"proposal_only","coordinate_system":{"camera_forward":"-Z","handedness":"right","id":"arkit_world","matrix_layout":"row-major","units":"meters","vector_convention":"column-vector","world_up":"+Y"},"created_at":"2026-07-30T10:00:00.000Z","expected_frame_count":null,"schema":"capture_splat.live_session.v0.2","session_id":"csl_SMOhjzjH7dE8x3yB5A0KBAo4YL6A4IzY1U570kVX_D8","source_session_seed_b64u":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"}
            """.utf8
        )
        let fixtureURL = root.appendingPathComponent(
            "contracts/live-session/v0.1/fixtures/valid_frame.json"
        )
        let fixture = try Data(contentsOf: fixtureURL)
        let frame = try LiveStrictJSON.decode(
            LiveCaptureFrameMetadata.self,
            from: fixture
        )
        let canonicalFrame = try LiveStrictJSON.canonicalData(frame)
        let canonicalRoundTrip = try LiveStrictJSON.decodeCanonical(
            LiveCaptureFrameMetadata.self,
            from: canonicalFrame
        )
        let expectedSessionCount = try JSONSerialization
            .jsonObject(with: encoded.data) as? [String: Any]
        emit([
            "canonical_frame_round_trip": canonicalRoundTrip == frame,
            "canonical_frame_sha256": LiveAuthEncoding.sha256(canonicalFrame),
            "canonical_session_match": encoded.data == expectedSession,
            "canonical_session_sha256": LiveAuthEncoding.sha256(encoded.data),
            "fixture_mask_round_trip": canonicalRoundTrip.assets?.masks?.count == 1,
            "session_expected_count_is_null":
                expectedSessionCount?["expected_frame_count"] is NSNull,
            "session_id_match":
                encoded.metadata.sessionID
                    == "csl_SMOhjzjH7dE8x3yB5A0KBAo4YL6A4IzY1U570kVX_D8",
        ])
    }

    private static func fileEvidence(root: URL) async throws {
        let capture = root.appendingPathComponent("capture", isDirectory: true)
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        let jpeg = try writeJPEG(
            root: capture,
            path: "rgb/frame_000001.jpg",
            width: 4,
            height: 3
        )
        let dimensions = try LiveCaptureFileEvidence.jpegDimensions(url: jpeg)
        let source = try LiveCaptureFileEvidence.reference(
            captureRoot: capture,
            relativePath: "rgb/frame_000001.jpg",
            mediaType: "image/jpeg"
        )

        let traversalRejected = throwsError {
            _ = try LiveCaptureFileEvidence.reference(
                captureRoot: capture,
                relativePath: "../escape.jpg",
                mediaType: "image/jpeg"
            )
        }
        let outside = try write(
            root,
            path: "outside.bin",
            data: Data("outside".utf8)
        )
        let link = capture.appendingPathComponent("rgb/link.jpg")
        try FileManager.default.createSymbolicLink(
            at: link,
            withDestinationURL: outside
        )
        let symlinkRejected = throwsError {
            _ = try LiveCaptureFileEvidence.reference(
                captureRoot: capture,
                relativePath: "rgb/link.jpg",
                mediaType: "image/jpeg"
            )
        }

        let sessionBytes = try LiveCaptureMetadataEncoder.session(
            seed: Data(0..<32),
            createdAt: try LiveAuthTime.parse("2026-07-30T10:00:00.000Z")
        )
        let metadataURL = try write(
            capture,
            path: "metadata/live/session.json",
            data: sessionBytes.data
        )
        let metadataReference = try reference(
            capture: capture,
            url: metadataURL,
            mediaType: "application/json"
        )
        let authorization = try fixtureAuthorization()
        let session = try LiveSenderSessionReference(
            sessionID: sessionBytes.metadata.sessionID,
            expectedFrameCount: nil,
            metadata: metadataReference,
            authorization: authorization
        )
        let queue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: root.appendingPathComponent("queue.json"),
            limits: try queueLimits(),
            session: session
        )
        let originalBytes = try Data(contentsOf: jpeg)
        var changedBytes = originalBytes
        changedBytes[changedBytes.startIndex] ^= 0x01
        try changedBytes.write(to: jpeg, options: .atomic)
        let changedFileRejected = await throwsAsyncError {
            _ = try await queue.verifiedFileURL(for: source)
        }

        let event = frameEvent(captureRoot: capture)
        let nanRejected = throwsError {
            _ = try LiveCaptureMetadataEncoder.frame(
                sessionID: sessionBytes.metadata.sessionID,
                event: replacingTimestamp(event, with: .nan),
                source: source,
                sourceDimensions: (4, 3),
                depth: source,
                confidence: nil
            )
        }
        let dimensionMismatchRejected = throwsError {
            _ = try LiveCaptureMetadataEncoder.frame(
                sessionID: sessionBytes.metadata.sessionID,
                event: event,
                source: source,
                sourceDimensions: (3, 4),
                depth: source,
                confidence: nil
            )
        }
        emit([
            "actual_jpeg_dimensions": dimensions.width == 4 && dimensions.height == 3,
            "changed_file_rejected": changedFileRejected,
            "dimension_mismatch_rejected": dimensionMismatchRejected,
            "nan_rejected": nanRejected,
            "symlink_rejected": symlinkRejected,
            "traversal_rejected": traversalRejected,
        ])
    }

    private static func bridge(root: URL) async throws {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent("capture-001", isDirectory: true)
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        _ = try writeJPEG(
            root: capture,
            path: "rgb/frame_000001.jpg",
            width: 4,
            height: 3
        )
        _ = try write(
            capture,
            path: "depth/depth_000001.npy",
            data: Data("depth-evidence".utf8)
        )
        _ = try write(
            capture,
            path: "confidence/confidence_000001.npy",
            data: Data("confidence-evidence".utf8)
        )
        _ = try writeJPEG(
            root: capture,
            path: "rgb/frame_000002.jpg",
            width: 4,
            height: 3
        )
        _ = try write(
            capture,
            path: "depth/depth_000002.npy",
            data: Data("depth-evidence-2".utf8)
        )
        _ = try write(
            capture,
            path: "confidence/confidence_000002.npy",
            data: Data("confidence-evidence-2".utf8)
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let context = LiveCaptureSenderConnectionContext(
            authorization: authorization,
            discovery: LiveDiscoveryIdentity(
                serviceType: LiveAuthContract.bonjourServiceType,
                serviceName: "World Studio Probe",
                domain: LiveAuthContract.bonjourDomain
            ),
            certificateSHA256: LiveAuthEncoding.sha256(Data("certificate".utf8))
        )
        let transport = BlockingRequester(authorization: authorization)
        let connector = ProbeConnector(context: context, transport: transport)
        let seed = Data(0..<32)
        let random = FixedRandom(seed)
        var bridge: LiveCaptureSenderBridge? = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: connector,
            random: random,
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 1,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        bridge!.setPairedDesktopID(authorization.desktopID)
        let createdAt = try LiveAuthTime.parse("2026-07-30T10:00:00.000Z")
        let startDisposition = bridge!.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: createdAt
            )
        )
        let sessionID = try LiveSenderProgressiveSessionIdentity.sessionID(
            sourceSessionSeedBase64URL:
                "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
        )
        let bindingReady = try await eventually {
            try LiveCaptureSessionBindingStore(paths: paths).load(
                desktopID: authorization.desktopID,
                sessionID: sessionID
            ) != nil
        }
        let requesterBlocked = await eventually {
            await transport.calls() > 0
        }
        let frame = frameEvent(captureRoot: capture)
        try LiveCaptureJournal.commitAcceptedFrame(frame)
        let started = DispatchTime.now().uptimeNanoseconds
        let frameDisposition = bridge!.frameCommitted(frame)
        let elapsedNanoseconds =
            DispatchTime.now().uptimeNanoseconds - started
        let frameMetadataURL = capture.appendingPathComponent(
            "metadata/live/frames/00000001.json"
        )
        let frameReady = await eventually {
            FileManager.default.fileExists(atPath: frameMetadataURL.path)
        }
        let metadataBytes = try Data(contentsOf: frameMetadataURL)
        let metadata = try LiveStrictJSON.decodeCanonical(
            LiveCaptureFrameMetadata.self,
            from: metadataBytes
        )
        _ = try await eventually {
            try await queueSnapshot(
                paths: paths,
                documents: documents,
                authorization: authorization,
                sessionID: sessionID
            ).queuedFrameCount == 1
        }
        let snapshot = try await queueSnapshot(
            paths: paths,
            documents: documents,
            authorization: authorization,
            sessionID: sessionID
        )

        _ = bridge!.frameCommitted(frame)
        let conflicting = replacingTimestamp(frame, with: 2.0)
        _ = bridge!.frameCommitted(conflicting)
        try await Task.sleep(nanoseconds: 100_000_000)
        let afterDuplicate = try await queueSnapshot(
            paths: paths,
            documents: documents,
            authorization: authorization,
            sessionID: sessionID
        )
        let metadataUnchanged =
            try Data(contentsOf: frameMetadataURL) == metadataBytes

        _ = bridge!.captureFinalized(
            LiveCaptureFinalizedEvent(
                captureRoot: capture,
                finalSequenceID: 2,
                manifestRelativePath: "capture.json",
                manifestSizeBytes: 1,
                manifestSHA256: "sha256:" + String(repeating: "0", count: 64)
            )
        )
        try await Task.sleep(nanoseconds: 50_000_000)
        let beforeManifest = try await queueSnapshot(
            paths: paths,
            documents: documents,
            authorization: authorization,
            sessionID: sessionID
        )
        _ = try write(
            capture,
            path: "capture.json",
            data: Data("{\"schema\":\"capture_splat.capture.v0.1\"}".utf8)
        )
        let secondFrame = frameEvent(
            captureRoot: capture,
            sequenceID: 2
        )
        try LiveCaptureJournal.commitAcceptedFrame(secondFrame)
        let manifest = try LiveCaptureFileEvidence.reference(
            captureRoot: capture,
            relativePath: "capture.json",
            mediaType: "application/json"
        )
        try LiveCaptureJournal.commitFinalization(
            LiveCaptureFinalizedEvent(
                captureRoot: capture,
                finalSequenceID: 2,
                manifestRelativePath: "capture.json",
                manifestSizeBytes: manifest.sizeBytes,
                manifestSHA256: manifest.sha256
            )
        )

        let binding = try LiveCaptureSessionBindingStore(paths: paths).load(
            desktopID: authorization.desktopID,
            sessionID: sessionID
        )
        bridge = nil
        await transport.unblock()
        let restartRandom = FixedRandom(Data(repeating: 0xff, count: 32))
        let restartTransport = BlockingRequester(authorization: authorization)
        let restartConnector = ProbeConnector(
            context: context,
            transport: restartTransport
        )
        let restartBridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: restartConnector,
            random: restartRandom,
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        restartBridge.setPairedDesktopID(authorization.desktopID)
        let restartHandled = await eventually {
            await restartConnector.calls() > 0
        }
        let restoredSnapshot = try await queueSnapshot(
            paths: paths,
            documents: documents,
            authorization: authorization,
            sessionID: sessionID
        )
        let restartedBinding = try LiveCaptureSessionBindingStore(paths: paths).load(
            desktopID: authorization.desktopID,
            sessionID: sessionID
        )
        await restartTransport.unblock()

        let roleValues: [String] = metadata.assets.map { assets in
            var values = ["source"]
            if assets.depth != nil { values.append("depth") }
            if assets.confidence != nil { values.append("confidence") }
            if assets.masks != nil { values.append("mask") }
            return values
        } ?? []
        let roles = Set(roleValues)
        emit([
            "binding_durable": bindingReady && binding == restartedBinding,
            "callback_nonblocking": elapsedNanoseconds < 100_000_000,
            "duplicate_conflict_preserved_one_frame":
                afterDuplicate.queuedFrameCount == 1 && metadataUnchanged,
            "finalization_blocked_without_manifest":
                !beforeManifest.finalizationPending,
            "finalization_restored_from_journal":
                restoredSnapshot.finalizationPending,
            "frame_disposition": frameDisposition.rawValue,
            "frame_metadata_canonical":
                try LiveStrictJSON.canonicalData(metadata) == metadataBytes,
            "frame_ready": frameReady,
            "no_masks": metadata.assets?.masks == nil,
            "queued_assets": roles == Set(["source", "depth", "confidence"]),
            "queued_frame_count": snapshot.queuedFrameCount,
            "requester_blocked": requesterBlocked,
            "restart_restored_unnotified_frame":
                restoredSnapshot.queuedFrameCount == 2,
            "restart_reused_seed":
                restartHandled && restartRandom.callCount() == 0,
            "start_disposition": startDisposition.rawValue,
        ])
        _ = restartBridge
    }

    private static func retryRecovery(root: URL) async throws {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent(
            "capture-retry",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let context = connectionContext(authorization: authorization)
        let seed = Data(0..<32)
        let sessionID = try LiveSenderProgressiveSessionIdentity.sessionID(
            sourceSessionSeedBase64URL:
                LiveAuthEncoding.encodeBase64URL(seed)
        )
        let requester = RecoveringRequester(
            authorization: authorization,
            sessionID: sessionID,
            failureCount: 6
        )
        let connector = RecoveringConnector(
            context: context,
            requester: requester
        )
        let sleeper = RecordingSleeper()
        let bridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: connector,
            random: FixedRandom(seed),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 1,
                initialDelayMilliseconds: 250,
                maximumDelayMilliseconds: 4_000
            ),
            monitorNetwork: false,
            initialNetworkAvailable: true,
            outerRetrySleeper: sleeper
        )
        bridge.setPairedDesktopID(authorization.desktopID)
        let disposition = bridge.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let recovered = await eventually {
            await requester.calls() >= 8
        }
        let recordedDelays = await sleeper.delays()
        let connectorCalls = await connector.calls()
        emit([
            "exact_capped_outer_delays":
                recordedDelays == [250, 500, 1_000, 2_000, 4_000, 4_000],
            "inner_attempt_exhaustion_reentered":
                await requester.calls() == 8,
            "no_new_wake_required":
                recovered && connectorCalls == 8,
            "retry_start_disposition": disposition.rawValue,
            "session_remains_durable":
                try LiveCaptureSessionBindingStore(paths: paths)
                    .loadCurrent()?.session.sessionID == sessionID,
        ])
        _ = bridge
    }

    private static func pairingAndEnvironment(root: URL) async throws {
        let authorization = try fixtureAuthorization()
        let environmentRoot = root.appendingPathComponent(
            "environment-capture",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: environmentRoot,
            withIntermediateDirectories: true
        )
        let state = LiveCaptureSenderEnvironmentState(
            isForeground: false,
            networkAvailable: false,
            thermalState: .nominal,
            pairedDesktopID: nil
        )
        let initial = state.environment(captureRoot: environmentRoot)
        let initiallyUnpaired = state.currentPairedDesktopID() == nil
        state.setForeground(true)
        state.setNetworkAvailable(true)
        state.setThermalStateForTesting(.serious)
        state.setPairedDesktopID(authorization.desktopID)
        let updated = state.environment(captureRoot: environmentRoot)

        let documents = root.appendingPathComponent(
            "Pairing/Documents",
            isDirectory: true
        )
        let capture = documents.appendingPathComponent(
            "capture-pairing",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent(
                "Pairing/Application Support/v0.1"
            )
        )
        let transport = BlockingRequester(authorization: authorization)
        let connector = ProbeConnector(
            context: connectionContext(authorization: authorization),
            transport: transport
        )
        let bridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: connector,
            random: FixedRandom(Data(0..<32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 1,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        let event = LiveCaptureSessionStartedEvent(
            captureRoot: capture,
            createdAt: try LiveAuthTime.parse(
                "2026-07-30T10:00:00.000Z"
            )
        )
        let inactiveDisposition = bridge.captureStarted(event)
        let inactivePending = try LiveCaptureSessionBindingStore(paths: paths)
            .loadPending(documentsRoot: documents)
        let noInactivePointer =
            !FileManager.default.fileExists(
                atPath: paths.pendingCaptureURL.path
            )
            && inactivePending == nil
        bridge.setPairedDesktopID(authorization.desktopID)
        let activeDisposition = bridge.captureStarted(event)
        let pendingWasSynchronous = FileManager.default.fileExists(
            atPath: paths.pendingCaptureURL.path
        )
        let firstRequest = await eventually {
            await transport.calls() == 1
        }
        bridge.setPairedDesktopID(nil)
        try await Task.sleep(nanoseconds: 50_000_000)
        let callsAfterCancellation = await transport.calls()
        try await Task.sleep(nanoseconds: 50_000_000)
        let callsAfterSettling = await transport.calls()
        let cancellationStoppedCurrentDrive =
            firstRequest
            && callsAfterCancellation == 1
            && callsAfterSettling == callsAfterCancellation
        bridge.setPairedDesktopID(authorization.desktopID)
        let pairingWake = await eventually {
            await transport.calls() > callsAfterCancellation
        }

        let foregroundDocuments = root.appendingPathComponent(
            "Foreground/Documents",
            isDirectory: true
        )
        let foregroundCapture = foregroundDocuments.appendingPathComponent(
            "capture-foreground",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: foregroundCapture,
            withIntermediateDirectories: true
        )
        let foregroundPaths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent(
                "Foreground/Application Support/v0.1"
            )
        )
        let foregroundTransport = BlockingRequester(
            authorization: authorization
        )
        let foregroundBridge = try LiveCaptureSenderBridge(
            paths: foregroundPaths,
            documentsRoot: foregroundDocuments,
            connector: ProbeConnector(
                context: connectionContext(authorization: authorization),
                transport: foregroundTransport
            ),
            random: FixedRandom(Data(repeating: 3, count: 32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: true,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 1,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        foregroundBridge.setPairedDesktopID(authorization.desktopID)
        foregroundBridge.setForeground(true)
        _ = foregroundBridge.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: foregroundCapture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let foregroundRequest = await eventually {
            await foregroundTransport.calls() == 1
        }
        foregroundBridge.setForeground(false)
        try await Task.sleep(nanoseconds: 100_000_000)
        let foregroundCallsAfterCancellation =
            await foregroundTransport.calls()
        let foregroundStoppedBeforeNextOperation =
            foregroundRequest && foregroundCallsAfterCancellation == 1

        emit([
            "direct_environment_initial_visible":
                !initial.isForeground
                && !initial.networkAvailable
                && initial.thermalState == .nominal
                && initiallyUnpaired,
            "direct_environment_updates_visible":
                updated.isForeground
                && updated.networkAvailable
                && updated.thermalState == .serious
                && state.currentPairedDesktopID()
                    == authorization.desktopID,
            "foreground_cancellation_before_next_operation":
                foregroundStoppedBeforeNextOperation,
            "inactive_no_pending_pointer": noInactivePointer,
            "inactive_start_disposition": inactiveDisposition.rawValue,
            "pairing_activation_woke_sender": pairingWake,
            "pairing_cancellation_stopped_current_drive":
                cancellationStoppedCurrentDrive,
            "pairing_start_disposition": activeDisposition.rawValue,
            "pending_pointer_synchronous": pendingWasSynchronous,
        ])
        _ = bridge
        _ = foregroundBridge
    }

    private static func pendingCrash(root: URL) async throws {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent(
            "capture-pending",
            isDirectory: true
        )
        let decoy = documents.appendingPathComponent(
            "capture-decoy",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: decoy,
            withIntermediateDirectories: true
        )
        _ = try write(
            decoy,
            path: "do-not-enumerate.txt",
            data: Data("decoy".utf8)
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let context = connectionContext(authorization: authorization)
        let firstRandom = FixedRandom(Data(repeating: 7, count: 32))
        let gatedRequester = BlockingRequester(authorization: authorization)
        let gatedConnector = GatedContextConnector(
            context: context,
            requester: gatedRequester
        )
        var firstBridge: LiveCaptureSenderBridge? = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: gatedConnector,
            random: firstRandom,
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        firstBridge!.setPairedDesktopID(authorization.desktopID)
        let started = LiveCaptureSessionStartedEvent(
            captureRoot: capture,
            createdAt: try LiveAuthTime.parse(
                "2026-07-30T10:00:00.000Z"
            )
        )
        let startDisposition = firstBridge!.captureStarted(started)
        let store = LiveCaptureSessionBindingStore(paths: paths)
        let pendingAfterStart = try store.loadPending(
            documentsRoot: documents
        )
        let sessionMetadataURL = capture.appendingPathComponent(
            "metadata/live/session.json"
        )
        let sessionMetadataBeforeDuplicate = try Data(
            contentsOf: sessionMetadataURL
        )
        let expectedSessionID =
            try LiveSenderProgressiveSessionIdentity.sessionID(
                sourceSessionSeedBase64URL:
                    LiveAuthEncoding.encodeBase64URL(
                        Data(repeating: 7, count: 32)
                    )
            )
        let expectedMetadataReference =
            try LiveCaptureFileEvidence.reference(
                captureRoot: capture,
                relativePath: "metadata/live/session.json",
                mediaType: "application/json"
            )
        let pendingSynchronous =
            FileManager.default.fileExists(
                atPath: paths.pendingCaptureURL.path
            )
            && pendingAfterStart?.event
                .captureRoot.standardizedFileURL
                == started.captureRoot.standardizedFileURL
            && pendingAfterStart?.desktopID
                == authorization.desktopID
            && pendingAfterStart?.sessionID == expectedSessionID
            && pendingAfterStart?.metadata == expectedMetadataReference
            && FileManager.default.fileExists(
                atPath: sessionMetadataURL.path
            )
        let duplicateStartDisposition =
            firstBridge!.captureStarted(started)
        let pendingAfterDuplicate = try store.loadPending(
            documentsRoot: documents
        )
        let sessionMetadataAfterDuplicate = try Data(
            contentsOf: sessionMetadataURL
        )
        let duplicateReusedMetadata =
            duplicateStartDisposition.rawValue
                == LiveCaptureIngressDisposition.accepted.rawValue
            && firstRandom.callCount() == 1
            && sessionMetadataAfterDuplicate
                == sessionMetadataBeforeDuplicate
            && pendingAfterDuplicate?.sessionID == expectedSessionID
            && pendingAfterDuplicate?.metadata
                == expectedMetadataReference
        let connectorWasGated = await eventually {
            await gatedConnector.calls() == 1
        }
        let noCurrentBeforeCrash = try store.loadCurrent() == nil
        firstBridge = nil
        try await Task.sleep(nanoseconds: 50_000_000)

        let restartRandom = FixedRandom(Data(0..<32))
        let restartTransport = BlockingRequester(
            authorization: authorization
        )
        let restartConnector = ProbeConnector(
            context: context,
            transport: restartTransport
        )
        let restartBridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: restartConnector,
            random: restartRandom,
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        restartBridge.setPairedDesktopID(authorization.desktopID)
        let promoted = await eventually {
            do {
                return try store.loadCurrent() != nil
                    && store.loadPending(documentsRoot: documents) == nil
            } catch {
                return false
            }
        }
        let current = try store.loadCurrent()
        let requesterReached = await eventually {
            await restartTransport.calls() > 0
        }
        emit([
            "exact_pending_capture_promoted":
                promoted
                && current?.captureDirectoryName == capture.lastPathComponent,
            "fresh_bridge_needed_no_capture_event":
                requesterReached && restartRandom.callCount() == 0,
            "no_current_before_crash": noCurrentBeforeCrash,
            "other_capture_not_enumerated":
                !FileManager.default.fileExists(
                    atPath: decoy.appendingPathComponent(
                        "metadata/live/session.json"
                    ).path
                ),
            "pending_connector_was_gated": connectorWasGated,
            "pending_exact_session_and_metadata": pendingSynchronous,
            "pending_pointer_synchronous": pendingSynchronous,
            "pending_start_disposition": startDisposition.rawValue,
            "precrash_session_seed_committed":
                firstRandom.callCount() == 1,
            "repeated_start_reused_metadata": duplicateReusedMetadata,
        ])
        _ = restartBridge
    }

    private static func desktopGating(root: URL) async throws {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent(
            "capture-desktop-gate",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let context = connectionContext(authorization: authorization)
        let setupTransport = BlockingRequester(authorization: authorization)
        var setupBridge: LiveCaptureSenderBridge? = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: ProbeConnector(
                context: context,
                transport: setupTransport
            ),
            random: FixedRandom(Data(0..<32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        setupBridge!.setPairedDesktopID(authorization.desktopID)
        _ = setupBridge!.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let store = LiveCaptureSessionBindingStore(paths: paths)
        let sessionDurable = await eventually {
            (try? store.loadCurrent()) != nil
        }
        setupBridge = nil
        await setupTransport.unblock()
        try await Task.sleep(nanoseconds: 50_000_000)

        let gatedTransport = BlockingRequester(authorization: authorization)
        let gatedBridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: ProbeConnector(
                context: context,
                transport: gatedTransport
            ),
            random: FixedRandom(Data(repeating: 0xff, count: 32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: false
        )
        gatedBridge.setPairedDesktopID(nil)
        let state = reflectedEnvironmentState(gatedBridge)
        state?.setNetworkAvailable(true)
        gatedBridge.setForeground(true)
        NotificationCenter.default.post(
            name: ProcessInfo.thermalStateDidChangeNotification,
            object: nil
        )
        try await Task.sleep(nanoseconds: 100_000_000)
        let nilDesktopBlockedAllPositiveWakes =
            await gatedTransport.calls() == 0
        let wrongDesktopID = LiveAuthEncoding.identity(
            prefix: "wsd",
            publicKeyX963: Data(repeating: 9, count: 65)
        )
        gatedBridge.setPairedDesktopID(wrongDesktopID)
        try await Task.sleep(nanoseconds: 100_000_000)
        let wrongDesktopBlocked = await gatedTransport.calls() == 0
        gatedBridge.setPairedDesktopID(authorization.desktopID)
        let exactDesktopRestored = await eventually {
            await gatedTransport.calls() > 0
        }
        emit([
            "exact_desktop_restored_requester":
                exactDesktopRestored,
            "foreground_network_thermal_wakes_blocked_without_pairing":
                nilDesktopBlockedAllPositiveWakes,
            "network_state_positive": state?.environment(
                captureRoot: capture
            ).networkAvailable == true,
            "session_was_durable": sessionDurable,
            "wrong_desktop_blocked_requester": wrongDesktopBlocked,
        ])
        _ = gatedBridge
    }

    private static func emptyAbort(root: URL) async throws {
        let authorization = try fixtureAuthorization()
        let context = connectionContext(authorization: authorization)

        let documents = root.appendingPathComponent(
            "Empty/Documents",
            isDirectory: true
        )
        let capture = documents.appendingPathComponent(
            "capture-empty",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent(
                "Empty/Application Support/v0.1"
            )
        )
        let transport = BlockingRequester(authorization: authorization)
        var bridge: LiveCaptureSenderBridge? = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: ProbeConnector(
                context: context,
                transport: transport
            ),
            random: FixedRandom(Data(0..<32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        bridge!.setPairedDesktopID(authorization.desktopID)
        let startDisposition = bridge!.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let pointerClaimed = try await bridge!.hasPendingTransfer()
        let abortDisposition = bridge!.captureAborted(
            LiveCaptureAbortedEvent(captureRoot: capture)
        )
        let emptyCleared = try await eventually {
            let hasPending = try await bridge!.hasPendingTransfer()
            return !hasPending
        }
        let pointersAbsent =
            !FileManager.default.fileExists(
                atPath: paths.pendingCaptureURL.path
            )
            && !FileManager.default.fileExists(
                atPath: paths.currentSessionURL.path
            )
        bridge = nil
        await transport.unblock()
        try await Task.sleep(nanoseconds: 50_000_000)
        let relaunchTransport = BlockingRequester(
            authorization: authorization
        )
        let relaunchBridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: ProbeConnector(
                context: context,
                transport: relaunchTransport
            ),
            random: FixedRandom(Data(repeating: 4, count: 32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        relaunchBridge.setPairedDesktopID(authorization.desktopID)
        relaunchBridge.setForeground(true)
        try await Task.sleep(nanoseconds: 100_000_000)
        let relaunchHasPending =
            try await relaunchBridge.hasPendingTransfer()
        let relaunchStayedClear =
            await relaunchTransport.calls() == 0
            && !relaunchHasPending

        let acceptedDocuments = root.appendingPathComponent(
            "Accepted/Documents",
            isDirectory: true
        )
        let acceptedCapture = acceptedDocuments.appendingPathComponent(
            "capture-accepted",
            isDirectory: true
        )
        let acceptedFrame = try prepareFrameEvidence(
            capture: acceptedCapture
        )
        try LiveCaptureJournal.commitAcceptedFrame(acceptedFrame)
        let acceptedPaths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent(
                "Accepted/Application Support/v0.1"
            )
        )
        let acceptedTransport = BlockingRequester(
            authorization: authorization
        )
        let acceptedBridge = try LiveCaptureSenderBridge(
            paths: acceptedPaths,
            documentsRoot: acceptedDocuments,
            connector: ProbeConnector(
                context: context,
                transport: acceptedTransport
            ),
            random: FixedRandom(Data(repeating: 5, count: 32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        acceptedBridge.setPairedDesktopID(authorization.desktopID)
        _ = acceptedBridge.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: acceptedCapture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let acceptedStore = LiveCaptureSessionBindingStore(
            paths: acceptedPaths
        )
        let acceptedReady = await eventually {
            (try? acceptedStore.loadCurrent()) != nil
        }
        _ = acceptedBridge.captureAborted(
            LiveCaptureAbortedEvent(captureRoot: acceptedCapture)
        )
        try await Task.sleep(nanoseconds: 100_000_000)
        let acceptedCurrentRemains =
            try acceptedStore.loadCurrent() != nil
        let acceptedPendingRemains =
            try await acceptedBridge.hasPendingTransfer()
        let acceptedAbortRefused =
            acceptedReady
            && acceptedCurrentRemains
            && acceptedPendingRemains
            && FileManager.default.fileExists(
                atPath: acceptedCapture.appendingPathComponent(
                    "metadata/live/accepted-frames/00000001.json"
                ).path
            )

        emit([
            "abort_disposition": abortDisposition.rawValue,
            "accepted_frame_abort_refused": acceptedAbortRefused,
            "empty_abort_cleared_matching_pointer":
                pointerClaimed && emptyCleared && pointersAbsent,
            "empty_start_disposition": startDisposition.rawValue,
            "relaunch_stayed_clear": relaunchStayedClear,
        ])
        _ = relaunchBridge
        _ = acceptedBridge
    }

    private static func abandonPointers(root: URL) async throws {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent(
            "capture-abandon",
            isDirectory: true
        )
        let frame = try prepareFrameEvidence(capture: capture)
        try LiveCaptureJournal.commitAcceptedFrame(frame)
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let transport = BlockingRequester(authorization: authorization)
        let bridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: ProbeConnector(
                context: connectionContext(authorization: authorization),
                transport: transport
            ),
            random: FixedRandom(Data(0..<32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        bridge.setPairedDesktopID(authorization.desktopID)
        _ = bridge.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let store = LiveCaptureSessionBindingStore(paths: paths)
        let currentReady = await eventually {
            (try? store.loadCurrent()) != nil
        }
        guard let binding = try store.loadCurrent() else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        let queueURL = try paths.queueStateURL(
            desktopID: authorization.desktopID,
            sessionID: binding.session.sessionID
        )
        let bindingURL = try paths.sessionBindingURL(
            desktopID: authorization.desktopID,
            sessionID: binding.session.sessionID
        )
        let journalURL = capture.appendingPathComponent(
            "metadata/live/accepted-frames/00000001.json"
        )
        let evidenceURLs = [
            capture,
            journalURL,
            queueURL,
            bindingURL,
            capture.appendingPathComponent("metadata/live/session.json"),
        ]
        try await bridge.abandonPendingTransfer()
        let regularHasPending = try await bridge.hasPendingTransfer()
        let regularPointersCleared =
            currentReady
            && !regularHasPending
            && !FileManager.default.fileExists(
                atPath: paths.currentSessionURL.path
            )
            && !FileManager.default.fileExists(
                atPath: paths.pendingCaptureURL.path
            )

        _ = try write(
            paths.root,
            path: "pending-capture.json",
            data: Data("{corrupt".utf8)
        )
        try await bridge.abandonPendingTransfer()
        let corruptPointerCleared = !FileManager.default.fileExists(
            atPath: paths.pendingCaptureURL.path
        )

        let symlinkTarget = try write(
            root,
            path: "symlink-target.json",
            data: Data("preserve-target".utf8)
        )
        try FileManager.default.createSymbolicLink(
            at: paths.currentSessionURL,
            withDestinationURL: symlinkTarget
        )
        try await bridge.abandonPendingTransfer()
        let symlinkClearedTargetPreserved =
            !FileManager.default.fileExists(
                atPath: paths.currentSessionURL.path
            )
            && FileManager.default.fileExists(
                atPath: symlinkTarget.path
            )

        try FileManager.default.createDirectory(
            at: paths.pendingCaptureURL,
            withIntermediateDirectories: false
        )
        let directoryRejected = await throwsAsyncError {
            try await bridge.abandonPendingTransfer()
        }
        var directoryStatus = stat()
        let directoryPreserved =
            Darwin.lstat(
                paths.pendingCaptureURL.path,
                &directoryStatus
            ) == 0
            && directoryStatus.st_mode & S_IFMT == S_IFDIR
        let evidencePreserved = evidenceURLs.allSatisfy {
            FileManager.default.fileExists(atPath: $0.path)
        }

        emit([
            "corrupt_pointer_cleared": corruptPointerCleared,
            "directory_pointer_failed_safely":
                directoryRejected && directoryPreserved,
            "evidence_preserved": evidencePreserved,
            "regular_pointer_cleared": regularPointersCleared,
            "symlink_pointer_only_cleared":
                symlinkClearedTargetPreserved,
        ])
        _ = bridge
    }

    private enum MetadataTamper {
        case coordinateSystem
        case metadataReference
    }

    private static func tamperedRecovery(root: URL) async throws {
        let coordinateRejected = try await tamperedRecoveryCase(
            root: root.appendingPathComponent(
                "coordinate",
                isDirectory: true
            ),
            tamper: .coordinateSystem
        )
        let referenceRejected = try await tamperedRecoveryCase(
            root: root.appendingPathComponent(
                "reference",
                isDirectory: true
            ),
            tamper: .metadataReference
        )
        emit([
            "tampered_coordinate_system_rejected": coordinateRejected,
            "tampered_metadata_reference_rejected": referenceRejected,
        ])
    }

    private static func tamperedRecoveryCase(
        root: URL,
        tamper: MetadataTamper
    ) async throws -> Bool {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent(
            "capture-tampered",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let context = connectionContext(authorization: authorization)
        let firstTransport = BlockingRequester(
            authorization: authorization
        )
        let gatedConnector = GatedContextConnector(
            context: context,
            requester: firstTransport
        )
        var firstBridge: LiveCaptureSenderBridge? =
            try LiveCaptureSenderBridge(
                paths: paths,
                documentsRoot: documents,
                connector: gatedConnector,
                random: FixedRandom(Data(repeating: 6, count: 32)),
                limits: try queueLimits(),
                policy: try LiveSenderPolicy(
                    minimumAvailableStorageBytes: 0,
                    requiresForeground: false,
                    pausesAtSeriousThermalState: false
                ),
                retryPolicy: try LiveSenderRetryPolicy(),
                monitorNetwork: false,
                initialNetworkAvailable: true
            )
        firstBridge!.setPairedDesktopID(authorization.desktopID)
        let createdAt = try LiveAuthTime.parse(
            "2026-07-30T10:00:00.000Z"
        )
        let disposition = firstBridge!.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: createdAt
            )
        )
        let store = LiveCaptureSessionBindingStore(paths: paths)
        let connectorGated = await eventually {
            await gatedConnector.calls() == 1
        }
        guard disposition.rawValue
                == LiveCaptureIngressDisposition.accepted.rawValue,
              try store.loadPending(documentsRoot: documents) != nil,
              connectorGated else {
            return false
        }
        firstBridge = nil
        try await Task.sleep(nanoseconds: 50_000_000)
        let metadataURL = capture.appendingPathComponent(
            "metadata/live/session.json"
        )
        switch tamper {
        case .coordinateSystem:
            let original = try String(
                decoding: Data(contentsOf: metadataURL),
                as: UTF8.self
            )
            let changed = original.replacingOccurrences(
                of: "\"world_up\":\"+Y\"",
                with: "\"world_up\":\"-Y\""
            )
            guard changed != original else { return false }
            try Data(changed.utf8).write(
                to: metadataURL,
                options: .atomic
            )
        case .metadataReference:
            let replacement = try LiveCaptureMetadataEncoder.session(
                seed: Data(repeating: 0xee, count: 32),
                createdAt: createdAt
            )
            try replacement.data.write(
                to: metadataURL,
                options: .atomic
            )
        }

        let restartTransport = BlockingRequester(
            authorization: authorization
        )
        let restartBridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: ProbeConnector(
                context: context,
                transport: restartTransport
            ),
            random: FixedRandom(Data(repeating: 8, count: 32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        restartBridge.setPairedDesktopID(authorization.desktopID)
        try await Task.sleep(nanoseconds: 150_000_000)
        let currentAfterTamper = try store.loadCurrent()
        let pendingAfterTamper = try store.loadPending(
            documentsRoot: documents
        )
        let restartCalls = await restartTransport.calls()
        let rejected =
            currentAfterTamper == nil
            && pendingAfterTamper != nil
            && restartCalls == 0
        _ = restartBridge
        return rejected
    }

    private static func tinyCapacity(root: URL) async throws {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent(
            "capture-tiny-capacity",
            isDirectory: true
        )
        for sequenceID in 1...3 {
            let frame = try prepareFrameEvidence(
                capture: capture,
                sequenceID: sequenceID
            )
            try LiveCaptureJournal.commitAcceptedFrame(frame)
        }
        _ = try write(
            capture,
            path: "capture.json",
            data: Data(
                "{\"schema\":\"capture_splat.capture.v0.1\"}".utf8
            )
        )
        let manifest = try LiveCaptureFileEvidence.reference(
            captureRoot: capture,
            relativePath: "capture.json",
            mediaType: "application/json"
        )
        try LiveCaptureJournal.commitFinalization(
            LiveCaptureFinalizedEvent(
                captureRoot: capture,
                finalSequenceID: 3,
                manifestRelativePath: "capture.json",
                manifestSizeBytes: manifest.sizeBytes,
                manifestSHA256: manifest.sha256
            )
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let seed = Data(0..<32)
        let sessionID = try LiveSenderProgressiveSessionIdentity.sessionID(
            sourceSessionSeedBase64URL:
                LiveAuthEncoding.encodeBase64URL(seed)
        )
        let requester = SlidingWindowRequester(
            authorization: authorization,
            sessionID: sessionID,
            finalSequenceID: 3
        )
        let bridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: SlidingWindowConnector(
                context: connectionContext(authorization: authorization),
                requester: requester
            ),
            random: FixedRandom(seed),
            limits: try LiveSenderQueueLimits(
                maximumFrames: 1,
                maximumBytes: 16 * 1024 * 1024,
                maximumInFlight: 1
            ),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 1,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        bridge.setPairedDesktopID(authorization.desktopID)
        let disposition = bridge.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let receiverFinalized = await eventually(timeout: 8) {
            await requester.finalized()
        }
        let store = LiveCaptureSessionBindingStore(paths: paths)
        let pointerCleared = await eventually(timeout: 2) {
            (try? store.loadCurrent()) == nil
        }
        let snapshot = try await queueSnapshot(
            paths: paths,
            documents: documents,
            authorization: authorization,
            sessionID: sessionID
        )
        let sequences = await requester.sequences()
        emit([
            "all_journal_evidence_retained":
                (1...3).allSatisfy { sequenceID in
                    FileManager.default.fileExists(
                        atPath: capture.appendingPathComponent(
                            String(
                                format:
                                    "metadata/live/accepted-frames/%08d.json",
                                sequenceID
                            )
                        ).path
                    )
                }
                && FileManager.default.fileExists(
                    atPath: capture.appendingPathComponent(
                        "metadata/live/finalization.json"
                    ).path
                ),
            "finalized_after_refill":
                receiverFinalized
                && pointerCleared
                && snapshot.finalized
                && snapshot.queuedFrameCount == 0,
            "tiny_capacity_drained_in_order":
                sequences == [1, 2, 3],
            "tiny_capacity_start_disposition": disposition.rawValue,
        ])
        _ = bridge
    }

    private static func currentPendingConflict(root: URL) async throws {
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let capture = documents.appendingPathComponent(
            "capture-current-pending-conflict",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("Application Support/v0.1")
        )
        let authorization = try fixtureAuthorization()
        let context = connectionContext(authorization: authorization)
        let firstTransport = BlockingRequester(
            authorization: authorization
        )
        var firstBridge: LiveCaptureSenderBridge? =
            try LiveCaptureSenderBridge(
                paths: paths,
                documentsRoot: documents,
                connector: ProbeConnector(
                    context: context,
                    transport: firstTransport
                ),
                random: FixedRandom(Data(0..<32)),
                limits: try queueLimits(),
                policy: try LiveSenderPolicy(
                    minimumAvailableStorageBytes: 0,
                    requiresForeground: false,
                    pausesAtSeriousThermalState: false
                ),
                retryPolicy: try LiveSenderRetryPolicy(),
                monitorNetwork: false,
                initialNetworkAvailable: true
            )
        firstBridge!.setPairedDesktopID(authorization.desktopID)
        _ = firstBridge!.captureStarted(
            LiveCaptureSessionStartedEvent(
                captureRoot: capture,
                createdAt: try LiveAuthTime.parse(
                    "2026-07-30T10:00:00.000Z"
                )
            )
        )
        let store = LiveCaptureSessionBindingStore(paths: paths)
        let currentReady = await eventually {
            (try? store.loadCurrent()) != nil
        }
        guard currentReady, let binding = try store.loadCurrent() else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        firstBridge = nil
        await firstTransport.unblock()
        try await Task.sleep(nanoseconds: 50_000_000)

        let forged = ForgedPendingPointer(
            schema: "capture_splat.live_capture_pending.v0.1",
            captureDirectoryName: binding.captureDirectoryName,
            createdAt: "2026-07-30T10:00:01.000Z",
            desktopID: authorization.desktopID,
            sessionID: binding.session.sessionID,
            metadata: binding.session.metadata
        )
        let payload = try LiveStrictJSON.canonicalData(forged)
        let envelope = ForgedPendingEnvelope(
            schema:
                "capture_splat.live_capture_pending_envelope.v0.1",
            payloadBase64URL:
                LiveAuthEncoding.encodeBase64URL(payload),
            payloadSHA256: LiveAuthEncoding.sha256(payload)
        )
        try LiveAtomicFile.write(
            LiveStrictJSON.canonicalData(envelope),
            to: paths.pendingCaptureURL
        )
        let forgedPendingLoads =
            try store.loadPending(documentsRoot: documents) != nil

        let restartTransport = BlockingRequester(
            authorization: authorization
        )
        let restartBridge = try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documents,
            connector: ProbeConnector(
                context: context,
                transport: restartTransport
            ),
            random: FixedRandom(Data(repeating: 0xff, count: 32)),
            limits: try queueLimits(),
            policy: try LiveSenderPolicy(
                minimumAvailableStorageBytes: 0,
                requiresForeground: false,
                pausesAtSeriousThermalState: false
            ),
            retryPolicy: try LiveSenderRetryPolicy(),
            monitorNetwork: false,
            initialNetworkAvailable: true
        )
        restartBridge.setPairedDesktopID(authorization.desktopID)
        restartBridge.setForeground(true)
        NotificationCenter.default.post(
            name: ProcessInfo.thermalStateDidChangeNotification,
            object: nil
        )
        try await Task.sleep(nanoseconds: 150_000_000)
        let currentRetained = try store.loadCurrent() == binding
        let pendingRetained =
            try store.loadPending(documentsRoot: documents) != nil
        let requesterCalls = await restartTransport.calls()
        emit([
            "both_pointer_files_retained":
                FileManager.default.fileExists(
                    atPath: paths.currentSessionURL.path
                )
                && FileManager.default.fileExists(
                    atPath: paths.pendingCaptureURL.path
                ),
            "mismatched_pending_was_structurally_valid":
                forgedPendingLoads,
            "recovery_failed_closed":
                currentRetained
                && pendingRetained
                && requesterCalls == 0,
        ])
        _ = restartBridge
    }

    private static func queueSnapshot(
        paths: LiveApplicationSupportPaths,
        documents: URL,
        authorization: LiveSenderAuthorizationBinding,
        sessionID: String
    ) async throws -> LiveSenderQueueSnapshot {
        guard let binding = try LiveCaptureSessionBindingStore(paths: paths).load(
            desktopID: authorization.desktopID,
            sessionID: sessionID
        ) else {
            throw LiveSenderQueueError.sessionNotLoaded
        }
        let queue = try await LiveSenderQueue.open(
            captureRoot: binding.captureRoot(documentsRoot: documents),
            stateURL: try paths.queueStateURL(
                desktopID: authorization.desktopID,
                sessionID: sessionID
            ),
            limits: try queueLimits(),
            session: binding.session
        )
        return try await queue.snapshot()
    }

    private static func reflectedEnvironmentState(
        _ bridge: LiveCaptureSenderBridge
    ) -> LiveCaptureSenderEnvironmentState? {
        guard let stored = Mirror(reflecting: bridge).children.first(
            where: { $0.label == "environmentState" }
        ) else {
            return nil
        }
        if let state = stored.value as? LiveCaptureSenderEnvironmentState {
            return state
        }
        return Mirror(reflecting: stored.value).children.first?.value
            as? LiveCaptureSenderEnvironmentState
    }

    private static func prepareFrameEvidence(
        capture: URL,
        sequenceID: Int = 1
    ) throws -> LiveCaptureFrameCommittedEvent {
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        _ = try writeJPEG(
            root: capture,
            path: String(
                format: "rgb/frame_%06d.jpg",
                sequenceID
            ),
            width: 4,
            height: 3
        )
        _ = try write(
            capture,
            path: String(
                format: "depth/depth_%06d.npy",
                sequenceID
            ),
            data: Data("depth-evidence-\(sequenceID)".utf8)
        )
        _ = try write(
            capture,
            path: String(
                format: "confidence/confidence_%06d.npy",
                sequenceID
            ),
            data: Data("confidence-evidence-\(sequenceID)".utf8)
        )
        return frameEvent(
            captureRoot: capture,
            sequenceID: sequenceID
        )
    }

    private static func frameEvent(
        captureRoot: URL,
        sequenceID: Int = 1
    ) -> LiveCaptureFrameCommittedEvent {
        LiveCaptureFrameCommittedEvent(
            captureRoot: captureRoot,
            sequenceID: sequenceID,
            timestamp: Double(sequenceID) + 0.25,
            sourceRelativePath: String(
                format: "rgb/frame_%06d.jpg",
                sequenceID
            ),
            sourceWidth: 4,
            sourceHeight: 3,
            depthRelativePath: String(
                format: "depth/depth_%06d.npy",
                sequenceID
            ),
            depthWidth: 2,
            depthHeight: 2,
            confidenceRelativePath: String(
                format: "confidence/confidence_%06d.npy",
                sequenceID
            ),
            cameraToWorld: [
                1, 0, 0, 0.25,
                0, 1, 0, 1.5,
                0, 0, 1, -0.75,
                0, 0, 0, 1,
            ],
            flX: 2,
            flY: 2,
            cx: 1,
            cy: 1,
            trackingState: "normal",
            quality: LiveCaptureFrameQualityEvent(
                reason: "useful_keyframe",
                score: 0.91,
                blurScore: 0.012,
                exposureMean: 0.5,
                exposureDelta: 0.01,
                clippedHighlightFraction: 0,
                nearClippedHighlightFraction: 0,
                clippedShadowFraction: 0,
                featureGridCoverage: 0.8,
                parallaxMeters: 0.08,
                angularVelocityDegPerSec: 1.5,
                translationSpeedMetersPerSec: 0.2,
                colmapOverlapScore: 0.75,
                validDepthRatio: 0.84,
                featurePointCount: 42
            )
        )
    }

    private static func connectionContext(
        authorization: LiveSenderAuthorizationBinding
    ) -> LiveCaptureSenderConnectionContext {
        LiveCaptureSenderConnectionContext(
            authorization: authorization,
            discovery: LiveDiscoveryIdentity(
                serviceType: LiveAuthContract.bonjourServiceType,
                serviceName: "World Studio Probe",
                domain: LiveAuthContract.bonjourDomain
            ),
            certificateSHA256: LiveAuthEncoding.sha256(
                Data("certificate".utf8)
            )
        )
    }

    private static func replacingTimestamp(
        _ event: LiveCaptureFrameCommittedEvent,
        with timestamp: Double
    ) -> LiveCaptureFrameCommittedEvent {
        LiveCaptureFrameCommittedEvent(
            captureRoot: event.captureRoot,
            sequenceID: event.sequenceID,
            timestamp: timestamp,
            sourceRelativePath: event.sourceRelativePath,
            sourceWidth: event.sourceWidth,
            sourceHeight: event.sourceHeight,
            depthRelativePath: event.depthRelativePath,
            depthWidth: event.depthWidth,
            depthHeight: event.depthHeight,
            confidenceRelativePath: event.confidenceRelativePath,
            cameraToWorld: event.cameraToWorld,
            flX: event.flX,
            flY: event.flY,
            cx: event.cx,
            cy: event.cy,
            trackingState: event.trackingState,
            quality: event.quality
        )
    }

    private static func queueLimits() throws -> LiveSenderQueueLimits {
        try LiveSenderQueueLimits(
            maximumFrames: 8,
            maximumBytes: 16 * 1024 * 1024,
            maximumInFlight: 1
        )
    }

    private static func fixtureAuthorization() throws -> LiveSenderAuthorizationBinding {
        try LiveSenderAuthorizationBinding(
            desktopID: LiveAuthEncoding.identity(
                prefix: "wsd",
                publicKeyX963: Data(repeating: 1, count: 65)
            ),
            deviceID: LiveAuthEncoding.identity(
                prefix: "csd",
                publicKeyX963: Data(repeating: 2, count: 65)
            )
        )
    }

    private static func reference(
        capture: URL,
        url: URL,
        mediaType: String
    ) throws -> LiveSenderFileReference {
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        guard let size = attributes[.size] as? NSNumber else {
            throw LiveSenderQueueError.sourceMissing(url.path)
        }
        return try LiveSenderFileReference(
            relativePath: String(url.path.dropFirst(capture.path.count + 1)),
            sizeBytes: size.int64Value,
            sha256: try LiveFileDigest.sha256(url: url),
            mediaType: mediaType
        )
    }

    @discardableResult
    private static func write(
        _ root: URL,
        path: String,
        data: Data
    ) throws -> URL {
        let url = root.appendingPathComponent(path)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try data.write(to: url, options: .atomic)
        return url
    }

    private static func writeJPEG(
        root: URL,
        path: String,
        width: Int,
        height: Int
    ) throws -> URL {
        let url = root.appendingPathComponent(path)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let pixels = Data(repeating: 0x7f, count: width * height * 4)
        guard let provider = CGDataProvider(data: pixels as CFData),
              let image = CGImage(
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bitsPerPixel: 32,
                  bytesPerRow: width * 4,
                  space: CGColorSpaceCreateDeviceRGB(),
                  bitmapInfo: CGBitmapInfo(
                      rawValue: CGImageAlphaInfo.premultipliedLast.rawValue
                  ),
                  provider: provider,
                  decode: nil,
                  shouldInterpolate: false,
                  intent: .defaultIntent
              ),
              let destination = CGImageDestinationCreateWithURL(
                  url as CFURL,
                  "public.jpeg" as CFString,
                  1,
                  nil
              ) else {
            throw LiveSenderQueueError.persistenceFailed(
                "could not construct JPEG fixture"
            )
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            throw LiveSenderQueueError.persistenceFailed(
                "could not write JPEG fixture"
            )
        }
        return url
    }

    private static func eventually(
        timeout: TimeInterval = 2,
        condition: @escaping () async throws -> Bool
    ) async rethrows -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            if try await condition() { return true }
            try? await Task.sleep(nanoseconds: 5_000_000)
        } while Date() < deadline
        return try await condition()
    }

    private static func throwsError(_ operation: () throws -> Void) -> Bool {
        do {
            try operation()
            return false
        } catch {
            return true
        }
    }

    private static func throwsAsyncError(
        _ operation: () async throws -> Void
    ) async -> Bool {
        do {
            try await operation()
            return false
        } catch {
            return true
        }
    }

    private static func emit(_ value: [String: Any]) {
        let data = try! JSONSerialization.data(
            withJSONObject: value,
            options: [.sortedKeys]
        )
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0a]))
    }
}

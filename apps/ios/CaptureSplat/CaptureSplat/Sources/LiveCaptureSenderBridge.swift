import Darwin
import Foundation
import ImageIO
import Network

protocol LiveCaptureSenderEventSink: AnyObject {
    @discardableResult
    func captureStarted(_ event: LiveCaptureSessionStartedEvent) -> LiveCaptureIngressDisposition

    @discardableResult
    func frameCommitted(_ event: LiveCaptureFrameCommittedEvent) -> LiveCaptureIngressDisposition

    @discardableResult
    func captureFinalized(_ event: LiveCaptureFinalizedEvent) -> LiveCaptureIngressDisposition

    @discardableResult
    func captureAborted(_ event: LiveCaptureAbortedEvent) -> LiveCaptureIngressDisposition
}

struct LiveCaptureSessionStartedEvent: Sendable {
    let captureRoot: URL
    let createdAt: Date
}

struct LiveCapturePendingStart: Sendable {
    let event: LiveCaptureSessionStartedEvent
    let desktopID: String
    let sessionID: String
    let metadata: LiveSenderFileReference
}

struct LiveCaptureFrameQualityEvent: Sendable {
    let reason: String
    let score: Double
    let blurScore: Double
    let exposureMean: Double
    let exposureDelta: Double
    let clippedHighlightFraction: Double
    let nearClippedHighlightFraction: Double
    let clippedShadowFraction: Double
    let featureGridCoverage: Double
    let parallaxMeters: Double
    let angularVelocityDegPerSec: Double
    let translationSpeedMetersPerSec: Double
    let colmapOverlapScore: Double
    let validDepthRatio: Double
    let featurePointCount: Int
}

struct LiveCaptureFrameCommittedEvent: Sendable {
    let captureRoot: URL
    let sequenceID: Int
    let timestamp: Double
    let sourceRelativePath: String
    let sourceWidth: Int
    let sourceHeight: Int
    let depthRelativePath: String
    let depthWidth: Int
    let depthHeight: Int
    let confidenceRelativePath: String?
    let cameraToWorld: [Double]
    let flX: Double
    let flY: Double
    let cx: Double
    let cy: Double
    let trackingState: String
    let quality: LiveCaptureFrameQualityEvent
}

struct LiveCaptureFinalizedEvent: Sendable {
    let captureRoot: URL
    let finalSequenceID: Int
    let manifestRelativePath: String
    let manifestSizeBytes: Int64
    let manifestSHA256: String
}

struct LiveCaptureAbortedEvent: Sendable {
    let captureRoot: URL
}

enum LiveCaptureIngressDisposition: String, Sendable {
    case accepted
    case overflow
    case disabled
}

struct LiveCaptureSenderConnectionContext: Equatable, Sendable {
    let authorization: LiveSenderAuthorizationBinding
    let discovery: LiveDiscoveryIdentity
    let certificateSHA256: String
}

protocol LiveCaptureSenderConnecting: Sendable {
    func currentContext(now: Date) async throws -> LiveCaptureSenderConnectionContext

    func requester(
        for context: LiveCaptureSenderConnectionContext
    ) async throws -> any LiveAuthenticatedRequesting
}

actor LiveCaptureSenderConnector: LiveCaptureSenderConnecting {
    typealias EndpointResolver = @Sendable (LiveDiscoveryIdentity) async throws
        -> LiveResolvedEndpoint

    private let recoveryStore: LivePairingRecoveryStore
    private let identityStore: LiveDeviceIdentityStore
    private let grantStore: LiveGrantStore
    private let counterStore: LiveRequestCounterStore
    private let resolveEndpoint: EndpointResolver

    init(
        recoveryStore: LivePairingRecoveryStore,
        identityStore: LiveDeviceIdentityStore,
        grantStore: LiveGrantStore,
        counterStore: LiveRequestCounterStore,
        resolveEndpoint: @escaping EndpointResolver = { discovery in
            try await LiveCaptureSenderConnector.resolve(discovery: discovery)
        }
    ) {
        self.recoveryStore = recoveryStore
        self.identityStore = identityStore
        self.grantStore = grantStore
        self.counterStore = counterStore
        self.resolveEndpoint = resolveEndpoint
    }

    func currentContext(now: Date) async throws -> LiveCaptureSenderConnectionContext {
        guard let profile = try await recoveryStore.load(),
              let grant = try await grantStore.load(
                  desktopID: profile.desktopID,
                  currentAt: now
              ) else {
            throw LiveAuthenticatedRequestError.auth(
                code: "grant_unknown",
                retryable: false
            )
        }
        let identity = try await identityStore.publicIdentity()
        let required: Set<LivePermission> = [
            .sessionCreate,
            .sessionResume,
            .framePut,
            .assetPut,
            .sessionFinalize,
        ]
        guard profile.desktopID == grant.payload.desktopID,
              identity.deviceID == grant.payload.deviceID,
              identity.publicKeyBase64URL == grant.payload.devicePublicKeyBase64URL,
              required.isSubset(of: Set(grant.payload.permissions)) else {
            throw LiveAuthenticatedRequestError.auth(
                code: "identity_mismatch",
                retryable: false
            )
        }
        return LiveCaptureSenderConnectionContext(
            authorization: try LiveSenderAuthorizationBinding(
                desktopID: grant.payload.desktopID,
                deviceID: grant.payload.deviceID
            ),
            discovery: grant.payload.liveDiscovery,
            certificateSHA256: grant.payload.tlsCertificateSHA256
        )
    }

    func requester(
        for context: LiveCaptureSenderConnectionContext
    ) async throws -> any LiveAuthenticatedRequesting {
        let endpoint = try await resolveEndpoint(context.discovery)
        try endpoint.validate(discovery: context.discovery)
        return try LiveAuthenticatedHTTPClient.pinned(
            endpoint: endpoint,
            desktopID: context.authorization.desktopID,
            certificateSHA256: context.certificateSHA256,
            identityStore: identityStore,
            grantStore: grantStore,
            counterStore: counterStore
        )
    }

    @MainActor
    private static func resolve(
        discovery: LiveDiscoveryIdentity
    ) async throws -> LiveResolvedEndpoint {
        let resolver = LiveBonjourResolver()
        return try await resolver.resolve(discovery: discovery, timeout: 15)
    }
}

struct LiveCaptureSessionBinding: Codable, Equatable, Sendable {
    let schema: String
    let captureDirectoryName: String
    let session: LiveSenderSessionReference

    enum CodingKeys: String, CodingKey {
        case schema
        case captureDirectoryName = "capture_directory_name"
        case session
    }

    init(captureDirectoryName: String, session: LiveSenderSessionReference) throws {
        schema = "capture_splat.live_capture_session_binding.v0.1"
        self.captureDirectoryName = captureDirectoryName
        self.session = session
        try validate()
    }

    func validate() throws {
        guard schema == "capture_splat.live_capture_session_binding.v0.1",
              LiveAuthValidation.matches(
                  captureDirectoryName,
                  "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
              ),
              session.expectedFrameCount == nil,
              session.metadata.relativePath == "metadata/live/session.json",
              session.metadata.mediaType == "application/json" else {
            throw LiveSenderQueueError.stateCorrupt(
                "live capture session binding is invalid"
            )
        }
        try LiveSenderValidation.sessionID(session.sessionID)
        try LiveSenderValidation.validate(session.metadata)
        try LiveAuthValidation.identity(
            session.authorization.desktopID,
            prefix: "wsd"
        )
        try LiveAuthValidation.identity(
            session.authorization.deviceID,
            prefix: "csd"
        )
    }

    func captureRoot(documentsRoot: URL) -> URL {
        documentsRoot.appendingPathComponent(captureDirectoryName, isDirectory: true)
    }
}

struct LiveCaptureSessionBindingStore: Sendable {
    private struct PendingCapturePointer: Codable, Equatable {
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

        init(pending: LiveCapturePendingStart) throws {
            schema = "capture_splat.live_capture_pending.v0.1"
            captureDirectoryName = pending.event.captureRoot.lastPathComponent
            createdAt = LiveAuthTime.string(pending.event.createdAt)
            desktopID = pending.desktopID
            sessionID = pending.sessionID
            metadata = pending.metadata
            try validate()
        }

        func validate() throws {
            guard schema == "capture_splat.live_capture_pending.v0.1",
                  LiveAuthValidation.matches(
                      captureDirectoryName,
                      "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
                  ),
                  metadata.relativePath == "metadata/live/session.json",
                  metadata.mediaType == "application/json",
                  LiveSenderValidation.isRFC3339DateTime(createdAt) else {
                throw LiveSenderQueueError.stateCorrupt(
                    "live pending-capture pointer is invalid"
                )
            }
            try LiveAuthValidation.identity(desktopID, prefix: "wsd")
            try LiveSenderValidation.sessionID(sessionID)
            try LiveSenderValidation.validate(metadata)
        }

        func pendingStart(
            documentsRoot: URL
        ) throws -> LiveCapturePendingStart {
            try validate()
            return LiveCapturePendingStart(
                event: LiveCaptureSessionStartedEvent(
                    captureRoot: documentsRoot.appendingPathComponent(
                        captureDirectoryName,
                        isDirectory: true
                    ),
                    createdAt: try LiveAuthTime.parse(createdAt)
                ),
                desktopID: desktopID,
                sessionID: sessionID,
                metadata: metadata
            )
        }
    }

    private struct CurrentSessionPointer: Codable, Equatable {
        let schema: String
        let desktopID: String
        let deviceID: String
        let sessionID: String
        let captureDirectoryName: String
        let bindingSHA256: String

        enum CodingKeys: String, CodingKey {
            case schema
            case desktopID = "desktop_id"
            case deviceID = "device_id"
            case sessionID = "session_id"
            case captureDirectoryName = "capture_directory_name"
            case bindingSHA256 = "binding_sha256"
        }

        init(binding: LiveCaptureSessionBinding, bindingSHA256: String) throws {
            try binding.validate()
            guard LiveSenderValidation.isSHA256(bindingSHA256),
                  LiveAuthEncoding.sha256(
                      try LiveStrictJSON.canonicalData(binding)
                  ) == bindingSHA256 else {
                throw LiveSenderQueueError.stateCorrupt(
                    "live current-session binding checksum is invalid"
                )
            }
            schema = "capture_splat.live_capture_current_session.v0.1"
            desktopID = binding.session.authorization.desktopID
            deviceID = binding.session.authorization.deviceID
            sessionID = binding.session.sessionID
            captureDirectoryName = binding.captureDirectoryName
            self.bindingSHA256 = bindingSHA256
        }

        func validate() throws {
            guard schema == "capture_splat.live_capture_current_session.v0.1",
                  LiveSenderValidation.isSHA256(bindingSHA256),
                  LiveAuthValidation.matches(
                      captureDirectoryName,
                      "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
                  ) else {
                throw LiveSenderQueueError.stateCorrupt(
                    "live current-session pointer is invalid"
                )
            }
            try LiveAuthValidation.identity(desktopID, prefix: "wsd")
            try LiveAuthValidation.identity(deviceID, prefix: "csd")
            try LiveSenderValidation.sessionID(sessionID)
        }
    }

    private struct Envelope: Codable {
        let schema: String
        let payloadBase64URL: String
        let payloadSHA256: String

        enum CodingKeys: String, CodingKey {
            case schema
            case payloadBase64URL = "payload_b64u"
            case payloadSHA256 = "payload_sha256"
        }
    }

    private static let maximumPayloadBytes = 64 * 1024
    private static let maximumEnvelopeBytes = 128 * 1024
    private let paths: LiveApplicationSupportPaths

    init(paths: LiveApplicationSupportPaths) {
        self.paths = paths
    }

    func load(desktopID: String, sessionID: String) throws -> LiveCaptureSessionBinding? {
        let url = try paths.sessionBindingURL(
            desktopID: desktopID,
            sessionID: sessionID
        )
        guard FileManager.default.fileExists(atPath: url.path) else {
            return nil
        }
        let payload = try loadPayload(
            url: url,
            envelopeSchema:
                "capture_splat.live_capture_session_binding_envelope.v0.1",
            field: "live capture session binding"
        )
        let binding = try LiveStrictJSON.decodeCanonical(
            LiveCaptureSessionBinding.self,
            from: payload
        )
        try binding.validate()
        guard binding.session.sessionID == sessionID,
              binding.session.authorization.desktopID == desktopID else {
            throw LiveSenderQueueError.stateCorrupt(
                "live capture session binding identity is invalid"
            )
        }
        return binding
    }

    @discardableResult
    func save(_ binding: LiveCaptureSessionBinding) throws -> String {
        try binding.validate()
        let url = try paths.sessionBindingURL(
            desktopID: binding.session.authorization.desktopID,
            sessionID: binding.session.sessionID
        )
        let payload = try LiveStrictJSON.canonicalData(binding)
        guard payload.count <= Self.maximumPayloadBytes else {
            throw LiveSenderQueueError.persistenceFailed(
                "live capture session binding is oversized"
            )
        }
        let payloadSHA256 = LiveAuthEncoding.sha256(payload)
        if let existing = try load(
            desktopID: binding.session.authorization.desktopID,
            sessionID: binding.session.sessionID
        ) {
            guard existing == binding else {
                throw LiveSenderQueueError.sessionConflict
            }
            return payloadSHA256
        }
        let envelope = Envelope(
            schema: "capture_splat.live_capture_session_binding_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(payload),
            payloadSHA256: payloadSHA256
        )
        try LiveAtomicFile.write(
            LiveStrictJSON.canonicalData(envelope),
            to: url
        )
        return payloadSHA256
    }

    func loadCurrent() throws -> LiveCaptureSessionBinding? {
        let url = paths.currentSessionURL
        guard FileManager.default.fileExists(atPath: url.path) else {
            return nil
        }
        let payload = try loadPayload(
            url: url,
            envelopeSchema:
                "capture_splat.live_capture_current_session_envelope.v0.1",
            field: "live current-session pointer"
        )
        let pointer = try LiveStrictJSON.decodeCanonical(
            CurrentSessionPointer.self,
            from: payload
        )
        try pointer.validate()
        guard let binding = try load(
            desktopID: pointer.desktopID,
            sessionID: pointer.sessionID
        ) else {
            throw LiveSenderQueueError.stateCorrupt(
                "live current-session binding is missing"
            )
        }
        let bindingPayload = try LiveStrictJSON.canonicalData(binding)
        guard LiveAuthEncoding.sha256(bindingPayload) == pointer.bindingSHA256,
              binding.captureDirectoryName == pointer.captureDirectoryName,
              binding.session.authorization.desktopID == pointer.desktopID,
              binding.session.authorization.deviceID == pointer.deviceID else {
            throw LiveSenderQueueError.stateCorrupt(
                "live current-session pointer conflicts with its binding"
            )
        }
        return binding
    }

    func loadPending(
        documentsRoot: URL
    ) throws -> LiveCapturePendingStart? {
        let url = paths.pendingCaptureURL
        guard FileManager.default.fileExists(atPath: url.path) else {
            return nil
        }
        let payload = try loadPayload(
            url: url,
            envelopeSchema:
                "capture_splat.live_capture_pending_envelope.v0.1",
            field: "live pending-capture pointer"
        )
        let pointer = try LiveStrictJSON.decodeCanonical(
            PendingCapturePointer.self,
            from: payload
        )
        try pointer.validate()
        let pending = try pointer.pendingStart(
            documentsRoot: documentsRoot.standardizedFileURL
        )
        try validateCaptureRoot(
            pending.event.captureRoot,
            documentsRoot: documentsRoot
        )
        return pending
    }

    func claimPending(
        _ pending: LiveCapturePendingStart,
        documentsRoot: URL
    ) throws {
        try validateCaptureRoot(
            pending.event.captureRoot,
            documentsRoot: documentsRoot
        )
        guard try loadCurrent() == nil else {
            throw LiveSenderQueueError.sessionConflict
        }
        let pointer = try PendingCapturePointer(pending: pending)
        if let existing = try loadPending(documentsRoot: documentsRoot) {
            let existingPointer = try PendingCapturePointer(pending: existing)
            guard existingPointer == pointer else {
                throw LiveSenderQueueError.sessionConflict
            }
            return
        }
        let payload = try LiveStrictJSON.canonicalData(pointer)
        let envelope = Envelope(
            schema: "capture_splat.live_capture_pending_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(payload),
            payloadSHA256: LiveAuthEncoding.sha256(payload)
        )
        try LiveAtomicFile.write(
            LiveStrictJSON.canonicalData(envelope),
            to: paths.pendingCaptureURL
        )
    }

    func clearPending(
        _ pending: LiveCapturePendingStart,
        documentsRoot: URL
    ) throws {
        guard let existing = try loadPending(documentsRoot: documentsRoot) else {
            return
        }
        guard try PendingCapturePointer(pending: existing)
                == PendingCapturePointer(pending: pending) else {
            throw LiveSenderQueueError.sessionConflict
        }
        try removePointer(
            at: paths.pendingCaptureURL,
            description: "live pending-capture pointer"
        )
    }

    func claimCurrent(
        _ binding: LiveCaptureSessionBinding,
        bindingSHA256: String
    ) throws {
        let pointer = try CurrentSessionPointer(
            binding: binding,
            bindingSHA256: bindingSHA256
        )
        if let existing = try loadCurrent() {
            guard existing == binding else {
                throw LiveSenderQueueError.sessionConflict
            }
            return
        }
        let payload = try LiveStrictJSON.canonicalData(pointer)
        let envelope = Envelope(
            schema: "capture_splat.live_capture_current_session_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(payload),
            payloadSHA256: LiveAuthEncoding.sha256(payload)
        )
        try LiveAtomicFile.write(
            LiveStrictJSON.canonicalData(envelope),
            to: paths.currentSessionURL
        )
    }

    func clearCurrent(_ binding: LiveCaptureSessionBinding) throws {
        guard let existing = try loadCurrent() else { return }
        guard existing == binding else {
            throw LiveSenderQueueError.sessionConflict
        }
        try removePointer(
            at: paths.currentSessionURL,
            description: "live current-session pointer"
        )
    }

    func hasTransferPointer() throws -> Bool {
        try [paths.pendingCaptureURL, paths.currentSessionURL].contains {
            var status = stat()
            if Darwin.lstat($0.path, &status) == 0 {
                return true
            }
            guard errno == ENOENT else {
                throw LiveSenderQueueError.persistenceFailed(
                    "live transfer pointer state cannot be inspected"
                )
            }
            return false
        }
    }

    func abandonTransferPointers() throws {
        let urls = [paths.pendingCaptureURL, paths.currentSessionURL]
        for url in urls {
            var status = stat()
            if Darwin.lstat(url.path, &status) == 0 {
                guard status.st_mode & S_IFMT == S_IFREG
                        || status.st_mode & S_IFMT == S_IFLNK else {
                    throw LiveSenderQueueError.persistenceFailed(
                        "live transfer pointer is not a removable file"
                    )
                }
            } else if errno != ENOENT {
                throw LiveSenderQueueError.persistenceFailed(
                    "live transfer pointer state cannot be inspected"
                )
            }
        }
        var removed = false
        for url in urls {
            if Darwin.unlink(url.path) == 0 {
                removed = true
            } else if errno != ENOENT {
                throw LiveSenderQueueError.persistenceFailed(
                    "live transfer pointer could not be abandoned"
                )
            }
        }
        guard removed else { return }
        let directoryFD = Darwin.open(
            paths.root.path,
            O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
        )
        guard directoryFD >= 0 else {
            throw LiveSenderQueueError.persistenceFailed(
                "live transfer pointer directory cannot be opened"
            )
        }
        defer { Darwin.close(directoryFD) }
        guard fsync(directoryFD) == 0 else {
            throw LiveSenderQueueError.persistenceFailed(
                "live transfer pointer abandonment was not synchronized"
            )
        }
    }

    private func validateCaptureRoot(
        _ captureRoot: URL,
        documentsRoot: URL
    ) throws {
        let root = captureRoot.standardizedFileURL
        let documents = documentsRoot.standardizedFileURL
        var status = stat()
        guard root.deletingLastPathComponent() == documents,
              LiveAuthValidation.matches(
                  root.lastPathComponent,
                  "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
              ),
              Darwin.lstat(root.path, &status) == 0,
              status.st_mode & S_IFMT != S_IFLNK,
              status.st_mode & S_IFMT == S_IFDIR else {
            throw LiveSenderQueueError.sourceOutsideCaptureRoot(root.path)
        }
    }

    private func removePointer(
        at url: URL,
        description: String
    ) throws {
        do {
            try FileManager.default.removeItem(at: url)
            let directoryFD = Darwin.open(paths.root.path, O_RDONLY)
            guard directoryFD >= 0 else {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            defer { Darwin.close(directoryFD) }
            guard fsync(directoryFD) == 0 else {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
        } catch {
            throw LiveSenderQueueError.persistenceFailed(
                "could not clear the \(description)"
            )
        }
    }

    private func loadPayload(
        url: URL,
        envelopeSchema: String,
        field: String
    ) throws -> Data {
        let bytes = try LiveBoundedRegularFile.read(
            url: url,
            maximumBytes: Self.maximumEnvelopeBytes,
            field: "\(field) envelope"
        )
        let envelope = try LiveStrictJSON.decodeCanonical(
            Envelope.self,
            from: bytes
        )
        guard envelope.schema == envelopeSchema else {
            throw LiveSenderQueueError.stateCorrupt(
                "\(field) envelope schema is invalid"
            )
        }
        let payload = try LiveAuthEncoding.decodeBase64URL(
            envelope.payloadBase64URL,
            field: "\(field) payload"
        )
        guard payload.count <= Self.maximumPayloadBytes,
              LiveAuthEncoding.sha256(payload) == envelope.payloadSHA256 else {
            throw LiveSenderQueueError.stateCorrupt(
                "\(field) checksum is invalid"
            )
        }
        return payload
    }
}

struct LiveCaptureCoordinateSystem: Codable, Equatable, Sendable {
    let id = "arkit_world"
    let units = "meters"
    let handedness = "right"
    let worldUp = "+Y"
    let cameraForward = "-Z"
    let matrixLayout = "row-major"
    let vectorConvention = "column-vector"

    enum CodingKeys: String, CodingKey {
        case id, units, handedness
        case worldUp = "world_up"
        case cameraForward = "camera_forward"
        case matrixLayout = "matrix_layout"
        case vectorConvention = "vector_convention"
    }
}

struct LiveCaptureSessionMetadata: Codable, Equatable, Sendable {
    let schema: String
    let sessionID: String
    let createdAt: String
    let sourceSessionSeedBase64URL: String
    let expectedFrameCount: Int?
    let coordinateSystem: LiveCaptureCoordinateSystem
    let authority: String

    enum CodingKeys: String, CodingKey {
        case schema
        case sessionID = "session_id"
        case createdAt = "created_at"
        case sourceSessionSeedBase64URL = "source_session_seed_b64u"
        case expectedFrameCount = "expected_frame_count"
        case coordinateSystem = "coordinate_system"
        case authority
    }

    init(seed: Data, createdAt: Date) throws {
        guard seed.count == 32 else {
            throw LiveSenderQueueError.invalidReference(
                "source session seed must contain 32 bytes"
            )
        }
        let encodedSeed = LiveAuthEncoding.encodeBase64URL(seed)
        schema = "capture_splat.live_session.v0.2"
        sessionID = try LiveSenderProgressiveSessionIdentity.sessionID(
            sourceSessionSeedBase64URL: encodedSeed
        )
        self.createdAt = LiveAuthTime.string(createdAt)
        sourceSessionSeedBase64URL = encodedSeed
        expectedFrameCount = nil
        coordinateSystem = LiveCaptureCoordinateSystem()
        authority = "proposal_only"
        try validate()
    }

    func validate() throws {
        guard schema == "capture_splat.live_session.v0.2",
              expectedFrameCount == nil,
              authority == "proposal_only",
              coordinateSystem == LiveCaptureCoordinateSystem(),
              LiveSenderValidation.isRFC3339DateTime(createdAt),
              try LiveSenderProgressiveSessionIdentity.sessionID(
                  sourceSessionSeedBase64URL: sourceSessionSeedBase64URL
              ) == sessionID else {
            throw LiveSenderQueueError.invalidReference(
                "progressive live session metadata is invalid"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(schema, forKey: .schema)
        try container.encode(sessionID, forKey: .sessionID)
        try container.encode(createdAt, forKey: .createdAt)
        try container.encode(sourceSessionSeedBase64URL, forKey: .sourceSessionSeedBase64URL)
        try container.encodeNil(forKey: .expectedFrameCount)
        try container.encode(coordinateSystem, forKey: .coordinateSystem)
        try container.encode(authority, forKey: .authority)
    }
}

struct LiveCaptureFrameAssetMetadata: Codable, Equatable, Sendable {
    let path: String
    let sha256: String
    let sizeBytes: Int64
    let mediaType: String
    let width: Int?
    let height: Int?

    enum CodingKeys: String, CodingKey {
        case path, sha256
        case sizeBytes = "size_bytes"
        case mediaType = "media_type"
        case width, height
    }

    init(
        reference: LiveSenderFileReference,
        width: Int?,
        height: Int?
    ) throws {
        guard (width == nil) == (height == nil),
              width.map({ $0 > 0 }) ?? true,
              height.map({ $0 > 0 }) ?? true else {
            throw LiveSenderQueueError.invalidReference(
                "live asset dimensions are invalid"
            )
        }
        path = reference.relativePath
        sha256 = reference.sha256
        sizeBytes = reference.sizeBytes
        mediaType = reference.mediaType
        self.width = width
        self.height = height
    }
}

struct LiveCaptureFrameMaskMetadata: Codable, Equatable, Sendable {
    let kind: String
    let path: String
    let sha256: String
    let sizeBytes: Int64
    let mediaType: String
    let width: Int?
    let height: Int?

    enum CodingKeys: String, CodingKey {
        case kind, path, sha256
        case sizeBytes = "size_bytes"
        case mediaType = "media_type"
        case width, height
    }
}

struct LiveCaptureFrameAssetsMetadata: Codable, Equatable, Sendable {
    let depth: LiveCaptureFrameAssetMetadata?
    let confidence: LiveCaptureFrameAssetMetadata?
    let masks: [LiveCaptureFrameMaskMetadata]?
}

struct LiveCaptureFrameTimestampMetadata: Codable, Equatable, Sendable {
    let value: Double
    let clockDomain: String

    enum CodingKeys: String, CodingKey {
        case value
        case clockDomain = "clock_domain"
    }
}

struct LiveCaptureFrameIntrinsicsMetadata: Codable, Equatable, Sendable {
    let model: String
    let flX: Double
    let flY: Double
    let cx: Double
    let cy: Double
    let calibrationWidth: Int
    let calibrationHeight: Int
    let appliesTo: String

    enum CodingKeys: String, CodingKey {
        case model, cx, cy
        case flX = "fl_x"
        case flY = "fl_y"
        case calibrationWidth = "calibration_width"
        case calibrationHeight = "calibration_height"
        case appliesTo = "applies_to"
    }
}

struct LiveCaptureFrameTrackingMetadata: Codable, Equatable, Sendable {
    let state: String
}

struct LiveCaptureFrameQualityMetadata: Codable, Equatable, Sendable {
    let accepted: Bool
    let reason: String?
    let score: Double?
    let blurScore: Double?
    let exposureMean: Double?
    let exposureDelta: Double?
    let clippedHighlightFraction: Double?
    let nearClippedHighlightFraction: Double?
    let clippedShadowFraction: Double?
    let featureGridCoverage: Double?
    let parallaxMeters: Double?
    let angularVelocityDegPerSec: Double?
    let translationSpeedMetersPerSec: Double?
    let colmapOverlapScore: Double?
    let validDepthRatio: Double?
    let featurePointCount: Int?

    enum CodingKeys: String, CodingKey {
        case accepted, reason, score
        case blurScore = "blur_score"
        case exposureMean = "exposure_mean"
        case exposureDelta = "exposure_delta"
        case clippedHighlightFraction = "clipped_highlight_fraction"
        case nearClippedHighlightFraction = "near_clipped_highlight_fraction"
        case clippedShadowFraction = "clipped_shadow_fraction"
        case featureGridCoverage = "feature_grid_coverage"
        case parallaxMeters = "parallax_meters"
        case angularVelocityDegPerSec = "angular_velocity_deg_s"
        case translationSpeedMetersPerSec = "translation_speed_m_s"
        case colmapOverlapScore = "colmap_overlap_score"
        case validDepthRatio = "valid_depth_ratio"
        case featurePointCount = "feature_point_count"
    }
}

struct LiveCaptureFrameMetadata: Codable, Equatable, Sendable {
    let schema: String
    let sessionID: String
    let sequenceID: Int
    let timestamp: LiveCaptureFrameTimestampMetadata
    let sourceFrame: LiveCaptureFrameAssetMetadata
    let intrinsics: LiveCaptureFrameIntrinsicsMetadata
    let cameraToWorld: [Double]
    let coordinateFrame: String
    let tracking: LiveCaptureFrameTrackingMetadata
    let quality: LiveCaptureFrameQualityMetadata
    let assets: LiveCaptureFrameAssetsMetadata?

    enum CodingKeys: String, CodingKey {
        case schema
        case sessionID = "session_id"
        case sequenceID = "sequence_id"
        case timestamp
        case sourceFrame = "source_frame"
        case intrinsics
        case cameraToWorld = "camera_to_world"
        case coordinateFrame = "coordinate_frame"
        case tracking, quality, assets
    }
}

enum LiveCaptureMetadataEncoder {
    static func session(seed: Data, createdAt: Date) throws -> (
        metadata: LiveCaptureSessionMetadata,
        data: Data
    ) {
        let metadata = try LiveCaptureSessionMetadata(
            seed: seed,
            createdAt: createdAt
        )
        return (metadata, try LiveStrictJSON.canonicalData(metadata))
    }

    static func frame(
        sessionID: String,
        event: LiveCaptureFrameCommittedEvent,
        source: LiveSenderFileReference,
        sourceDimensions: (width: Int, height: Int),
        depth: LiveSenderFileReference,
        confidence: LiveSenderFileReference?
    ) throws -> LiveCaptureFrameMetadata {
        let finite = [
            event.timestamp,
            event.flX,
            event.flY,
            event.cx,
            event.cy,
            event.quality.score,
            event.quality.blurScore,
            event.quality.exposureMean,
            event.quality.exposureDelta,
            event.quality.clippedHighlightFraction,
            event.quality.nearClippedHighlightFraction,
            event.quality.clippedShadowFraction,
            event.quality.featureGridCoverage,
            event.quality.parallaxMeters,
            event.quality.angularVelocityDegPerSec,
            event.quality.translationSpeedMetersPerSec,
            event.quality.colmapOverlapScore,
            event.quality.validDepthRatio,
        ] + event.cameraToWorld
        guard (1...LiveSenderValidation.maximumSequenceID).contains(event.sequenceID),
              event.timestamp.isFinite,
              event.timestamp >= 0,
              event.cameraToWorld.count == 16,
              finite.allSatisfy(\.isFinite),
              event.flX > 0,
              event.flY > 0,
              event.sourceWidth == sourceDimensions.width,
              event.sourceHeight == sourceDimensions.height,
              event.depthWidth > 0,
              event.depthHeight > 0,
              !event.trackingState.isEmpty,
              !event.quality.reason.isEmpty,
              event.quality.featurePointCount >= 0 else {
            throw LiveSenderQueueError.invalidReference(
                "live frame evidence is invalid"
            )
        }
        return LiveCaptureFrameMetadata(
            schema: "capture_splat.live_frame.v0.1",
            sessionID: sessionID,
            sequenceID: event.sequenceID,
            timestamp: LiveCaptureFrameTimestampMetadata(
                value: event.timestamp,
                clockDomain: "arkit_session"
            ),
            sourceFrame: try LiveCaptureFrameAssetMetadata(
                reference: source,
                width: sourceDimensions.width,
                height: sourceDimensions.height
            ),
            intrinsics: LiveCaptureFrameIntrinsicsMetadata(
                model: "pinhole",
                flX: event.flX,
                flY: event.flY,
                cx: event.cx,
                cy: event.cy,
                calibrationWidth: event.depthWidth,
                calibrationHeight: event.depthHeight,
                appliesTo: "depth"
            ),
            cameraToWorld: event.cameraToWorld,
            coordinateFrame: "arkit_world",
            tracking: LiveCaptureFrameTrackingMetadata(
                state: event.trackingState
            ),
            quality: LiveCaptureFrameQualityMetadata(
                accepted: true,
                reason: event.quality.reason,
                score: event.quality.score,
                blurScore: event.quality.blurScore,
                exposureMean: event.quality.exposureMean,
                exposureDelta: event.quality.exposureDelta,
                clippedHighlightFraction: event.quality.clippedHighlightFraction,
                nearClippedHighlightFraction: event.quality.nearClippedHighlightFraction,
                clippedShadowFraction: event.quality.clippedShadowFraction,
                featureGridCoverage: event.quality.featureGridCoverage,
                parallaxMeters: event.quality.parallaxMeters,
                angularVelocityDegPerSec: event.quality.angularVelocityDegPerSec,
                translationSpeedMetersPerSec: event.quality.translationSpeedMetersPerSec,
                colmapOverlapScore: event.quality.colmapOverlapScore,
                validDepthRatio: event.quality.validDepthRatio,
                featurePointCount: event.quality.featurePointCount
            ),
            assets: LiveCaptureFrameAssetsMetadata(
                depth: try LiveCaptureFrameAssetMetadata(
                    reference: depth,
                    width: event.depthWidth,
                    height: event.depthHeight
                ),
                confidence: try confidence.map {
                    try LiveCaptureFrameAssetMetadata(
                        reference: $0,
                        width: event.depthWidth,
                        height: event.depthHeight
                    )
                },
                masks: nil
            )
        )
    }
}

enum LiveCaptureFileEvidence {
    static func reference(
        captureRoot: URL,
        relativePath: String,
        mediaType: String
    ) throws -> LiveSenderFileReference {
        let evidence = try LiveConfinedFile.inspect(
            captureRoot: captureRoot,
            relativePath: relativePath,
            calculateSHA256: true
        )
        guard let sha256 = evidence.sha256 else {
            throw LiveSenderQueueError.sourceChecksumMismatch(relativePath)
        }
        return try LiveSenderFileReference(
            relativePath: relativePath,
            sizeBytes: evidence.size,
            sha256: sha256,
            mediaType: mediaType
        )
    }

    static func jpegDimensions(url: URL) throws -> (width: Int, height: Int) {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(
                  source,
                  0,
                  nil
              ) as? [CFString: Any],
              let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue,
              let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue,
              width > 0,
              height > 0 else {
            throw LiveSenderQueueError.invalidReference(
                "source JPEG dimensions are unavailable"
            )
        }
        return (width, height)
    }

    static func immutableWrite(
        _ data: Data,
        captureRoot: URL,
        relativePath: String
    ) throws -> URL {
        try LiveSenderValidation.safeRelativePath(relativePath)
        let url = captureRoot.appendingPathComponent(relativePath).standardizedFileURL
        _ = try validatedRoot(captureRoot)
        try ensureParentDirectories(
            captureRoot: captureRoot,
            relativePath: relativePath
        )
        if FileManager.default.fileExists(atPath: url.path) {
            let existing = try LiveConfinedFile.inspect(
                captureRoot: captureRoot,
                relativePath: relativePath,
                calculateSHA256: true
            )
            guard existing.size == Int64(data.count),
                  existing.sha256 == LiveAuthEncoding.sha256(data) else {
                throw LiveSenderQueueError.invalidReference(
                    "immutable live metadata conflicts with existing bytes"
                )
            }
            return existing.url
        }
        try LiveAtomicFile.write(data, to: url)
        let written = try LiveConfinedFile.inspect(
            captureRoot: captureRoot,
            relativePath: relativePath,
            calculateSHA256: true
        )
        guard written.size == Int64(data.count),
              written.sha256 == LiveAuthEncoding.sha256(data) else {
            throw LiveSenderQueueError.sourceChecksumMismatch(relativePath)
        }
        return url
    }

    private static func ensureParentDirectories(
        captureRoot: URL,
        relativePath: String
    ) throws {
        var directoryFD = Darwin.open(
            captureRoot.standardizedFileURL.path,
            O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
        )
        guard directoryFD >= 0 else {
            throw LiveSenderQueueError.sourceOutsideCaptureRoot(relativePath)
        }
        defer {
            if directoryFD >= 0 {
                Darwin.close(directoryFD)
            }
        }
        let components = relativePath.split(separator: "/").map(String.init)
        for component in components.dropLast() {
            var nextFD = component.withCString {
                Darwin.openat(
                    directoryFD,
                    $0,
                    O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
                )
            }
            if nextFD < 0, errno == ENOENT {
                let created = component.withCString {
                    Darwin.mkdirat(directoryFD, $0, 0o700)
                }
                guard created == 0 || errno == EEXIST else {
                    throw LiveSenderQueueError.sourceOutsideCaptureRoot(
                        relativePath
                    )
                }
                guard fsync(directoryFD) == 0 else {
                    throw LiveSenderQueueError.persistenceFailed(
                        "live metadata parent directory was not synchronized"
                    )
                }
                nextFD = component.withCString {
                    Darwin.openat(
                        directoryFD,
                        $0,
                        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
                    )
                }
            }
            guard nextFD >= 0 else {
                throw LiveSenderQueueError.sourceOutsideCaptureRoot(relativePath)
            }
            Darwin.close(directoryFD)
            directoryFD = nextFD
        }
    }

    private static func validatedRoot(_ captureRoot: URL) throws -> URL {
        let root = captureRoot.standardizedFileURL
        var info = stat()
        guard Darwin.lstat(root.path, &info) == 0 else {
            throw LiveSenderQueueError.sourceMissing(root.path)
        }
        guard info.st_mode & S_IFMT != S_IFLNK else {
            throw LiveSenderQueueError.sourceSymlink(root.path)
        }
        guard info.st_mode & S_IFMT == S_IFDIR else {
            throw LiveSenderQueueError.invalidReference(
                "capture root must be a directory"
            )
        }
        return root.resolvingSymlinksInPath()
    }

}

private enum LiveCaptureBridgeEvent: Sendable {
    case restore
    case started(LiveCaptureSessionStartedEvent)
    case frame(LiveCaptureFrameCommittedEvent)
    case finalized(LiveCaptureFinalizedEvent)
    case aborted(LiveCaptureAbortedEvent)
}

final class LiveCaptureSenderEnvironmentState: @unchecked Sendable {
    private let lock = NSLock()
    private var isForeground: Bool
    private var networkAvailable: Bool
    private var thermalState: LiveSenderThermalState
    private var pairedDesktopID: String?

    init(
        isForeground: Bool = false,
        networkAvailable: Bool,
        thermalState: LiveSenderThermalState? = nil,
        pairedDesktopID: String? = nil
    ) {
        self.isForeground = isForeground
        self.networkAvailable = networkAvailable
        self.thermalState = thermalState ?? Self.currentThermalState()
        self.pairedDesktopID = pairedDesktopID
    }

    func setForeground(_ value: Bool) {
        lock.lock()
        isForeground = value
        lock.unlock()
    }

    func setNetworkAvailable(_ value: Bool) {
        lock.lock()
        networkAvailable = value
        lock.unlock()
    }

    func setPairedDesktopID(_ value: String?) {
        lock.lock()
        pairedDesktopID = value
        lock.unlock()
    }

    func currentPairedDesktopID() -> String? {
        lock.lock()
        let value = pairedDesktopID
        lock.unlock()
        return value
    }

    @discardableResult
    func refreshThermalState() -> LiveSenderThermalState {
        let value = Self.currentThermalState()
        lock.lock()
        thermalState = value
        lock.unlock()
        return value
    }

#if CAPTURE_SPLAT_LIVE_TESTING
    func setThermalStateForTesting(_ value: LiveSenderThermalState) {
        lock.lock()
        thermalState = value
        lock.unlock()
    }
#endif

    func environment(captureRoot: URL) -> LiveSenderEnvironment {
        lock.lock()
        let foreground = isForeground
        let network = networkAvailable
        let thermal = thermalState
        lock.unlock()
        let capacity = (
            try? captureRoot.resourceValues(
                forKeys: [.volumeAvailableCapacityForImportantUsageKey]
            ).volumeAvailableCapacityForImportantUsage
        ) ?? 0
        return LiveSenderEnvironment(
            isForeground: foreground,
            networkAvailable: network,
            receiverAvailable: true,
            availableStorageBytes: Int64(capacity),
            thermalState: thermal
        )
    }

    private static func currentThermalState() -> LiveSenderThermalState {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal:
            return .nominal
        case .fair:
            return .fair
        case .serious:
            return .serious
        case .critical:
            return .critical
        @unknown default:
            return .critical
        }
    }
}

private actor LiveCaptureSenderRuntime {
    private struct ActiveSession {
        let binding: LiveCaptureSessionBinding
        let queue: LiveSenderQueue
        let captureRoot: URL
    }

    private static let sessionMetadataPath = "metadata/live/session.json"

    private let paths: LiveApplicationSupportPaths
    private let documentsRoot: URL
    private let connector: any LiveCaptureSenderConnecting
    private let limits: LiveSenderQueueLimits
    private let policy: LiveSenderPolicy
    private let retryPolicy: LiveSenderRetryPolicy
    private let retrySleeper: any LiveSenderSleeping
    private let bindingStore: LiveCaptureSessionBindingStore
    private let environmentState: LiveCaptureSenderEnvironmentState

    private var active: ActiveSession?
    private var lastError: String?
    private var lastSummary: LiveSenderRunSummary?

    init(
        paths: LiveApplicationSupportPaths,
        documentsRoot: URL,
        connector: any LiveCaptureSenderConnecting,
        limits: LiveSenderQueueLimits,
        policy: LiveSenderPolicy,
        retryPolicy: LiveSenderRetryPolicy,
        retrySleeper: any LiveSenderSleeping,
        environmentState: LiveCaptureSenderEnvironmentState
    ) {
        self.paths = paths
        self.documentsRoot = documentsRoot.standardizedFileURL
        self.connector = connector
        self.limits = limits
        self.policy = policy
        self.retryPolicy = retryPolicy
        self.retrySleeper = retrySleeper
        self.environmentState = environmentState
        bindingStore = LiveCaptureSessionBindingStore(paths: paths)
    }

    func handle(_ event: LiveCaptureBridgeEvent) async -> Bool {
        do {
            switch event {
            case .restore:
                try await restore()
                return active != nil
            case .started(let event):
                guard let pending = try bindingStore.loadPending(
                    documentsRoot: documentsRoot
                ), pending.event.captureRoot.standardizedFileURL
                    == event.captureRoot.standardizedFileURL,
                    LiveAuthTime.string(pending.event.createdAt)
                    == LiveAuthTime.string(event.createdAt) else {
                    throw LiveSenderQueueError.sessionConflict
                }
                try await start(pending)
                return active != nil
            case .frame(let event):
                guard active != nil else { return false }
                _ = try await admit(event)
                return true
            case .finalized:
                guard let active else { return false }
                _ = try await restoreCaptureJournal(active)
                return true
            case .aborted(let event):
                try abortEmptyCapture(event)
                return false
            }
        } catch {
            lastError = Self.message(for: error)
            return false
        }
    }

    func drive() async {
        var outerAttempt = 1
        while !Task.isCancelled {
            guard let current = active else { return }
            guard environmentState.currentPairedDesktopID()
                    == current.binding.session.authorization.desktopID else {
                return
            }
            let environment = environmentState.environment(
                captureRoot: current.captureRoot
            )
            guard policy.pauseReason(for: environment) == nil else { return }
            let before: LiveSenderQueueSnapshot
            do {
                before = try await current.queue.snapshot()
                guard environmentState.currentPairedDesktopID()
                        == current.binding.session.authorization.desktopID else {
                    return
                }
                let context = try await connector.currentContext(now: Date())
                guard context.authorization
                        == current.binding.session.authorization,
                      environmentState.currentPairedDesktopID()
                        == context.authorization.desktopID else {
                    throw LiveSenderQueueError.authorizationMismatch
                }
                let requester = try await connector.requester(for: context)
                let sender = LiveSender(
                    queue: current.queue,
                    requester: requester,
                    policy: policy,
                    retryPolicy: retryPolicy
                )
                let root = current.captureRoot
                let execution = await sender.runOnceDetailed(
                    environment: { [environmentState] in
                        environmentState.environment(captureRoot: root)
                    }
                )
                lastSummary = execution.summary
                lastError = execution.summary.lastError
                guard active?.binding.session.sessionID
                        == current.binding.session.sessionID else {
                    return
                }
                let sent = try await current.queue.snapshot()
                if sent.finalized {
                    try bindingStore.clearCurrent(current.binding)
                    active = nil
                    return
                }
                _ = try await restoreCaptureJournal(current)
                let after = try await current.queue.snapshot()
                if after.finalized {
                    try bindingStore.clearCurrent(current.binding)
                    active = nil
                    return
                }
                let refilled =
                    after.queuedFrameCount > sent.queuedFrameCount
                    || after.queuedBytes > sent.queuedBytes
                    || (!sent.finalizationPending && after.finalizationPending)
                switch execution.interruptionDisposition {
                case .none:
                    if refilled {
                        outerAttempt = 1
                        continue
                    }
                    return
                case .blocked, .cancelled:
                    return
                case .retryable:
                    let progressMade =
                        execution.summary.acknowledgedFrameCount > 0
                        || after.queuedFrameCount < before.queuedFrameCount
                        || after.queuedBytes < before.queuedBytes
                    outerAttempt = progressMade ? 1 : min(outerAttempt + 1, 21)
                }
            } catch {
                lastError = Self.message(for: error)
                switch Self.interruptionDisposition(for: error) {
                case .none, .blocked, .cancelled:
                    return
                case .retryable:
                    outerAttempt = min(outerAttempt + 1, 21)
                }
            }
            let delay = retryPolicy.delayMilliseconds(
                afterAttempt: max(outerAttempt - 1, 1)
            )
            do {
                try await retrySleeper.sleep(milliseconds: delay)
            } catch {
                return
            }
        }
    }

    func hasPendingTransfer() throws -> Bool {
        try bindingStore.hasTransferPointer()
    }

    func abandonPendingTransfer() throws {
        try bindingStore.abandonTransferPointers()
        active = nil
        lastError = nil
        lastSummary = nil
    }

    private func restore() async throws {
        guard active == nil else { return }
        if try await restoreCurrentSession() {
            return
        }
        if let pending = try bindingStore.loadPending(
            documentsRoot: documentsRoot
        ) {
            try await start(pending)
        }
    }

    private func abortEmptyCapture(
        _ event: LiveCaptureAbortedEvent
    ) throws {
        let captureRoot = try confinedCaptureRoot(event.captureRoot)
        guard try LiveCaptureJournal.loadAcceptedFrames(
            captureRoot: captureRoot
        ).isEmpty,
            try LiveCaptureJournal.loadFinalization(
                captureRoot: captureRoot
            ) == nil else {
            throw LiveSenderQueueError.finalizationConflict
        }
        let current = try bindingStore.loadCurrent()
        let pending = try bindingStore.loadPending(
            documentsRoot: documentsRoot
        )
        if let current, let pending {
            try validatePending(
                pending,
                binding: current,
                captureRoot: captureRoot
            )
        } else if let pending {
            try validatePendingEvidence(
                pending,
                captureRoot: captureRoot
            )
        }
        if let current {
            guard current.captureRoot(documentsRoot: documentsRoot)
                    .standardizedFileURL == captureRoot else {
                throw LiveSenderQueueError.sessionConflict
            }
            try bindingStore.clearCurrent(current)
        }
        if let pending {
            guard pending.event.captureRoot.standardizedFileURL
                    == captureRoot else {
                throw LiveSenderQueueError.sessionConflict
            }
            try bindingStore.clearPending(
                pending,
                documentsRoot: documentsRoot
            )
        }
        if active?.captureRoot == captureRoot {
            active = nil
        }
        lastError = nil
        lastSummary = nil
    }

    private func validatePending(
        _ pending: LiveCapturePendingStart,
        binding: LiveCaptureSessionBinding,
        captureRoot: URL
    ) throws {
        guard try confinedCaptureRoot(pending.event.captureRoot)
                == captureRoot,
              pending.desktopID
                == binding.session.authorization.desktopID,
              pending.sessionID == binding.session.sessionID,
              pending.metadata == binding.session.metadata else {
            throw LiveSenderQueueError.sessionConflict
        }
        try validatePendingEvidence(
            pending,
            captureRoot: captureRoot
        )
    }

    private func validatePendingEvidence(
        _ pending: LiveCapturePendingStart,
        captureRoot: URL
    ) throws {
        guard try confinedCaptureRoot(pending.event.captureRoot)
                == captureRoot else {
            throw LiveSenderQueueError.sessionConflict
        }
        let metadataURL = captureRoot.appendingPathComponent(
            Self.sessionMetadataPath
        )
        let bytes = try LiveBoundedRegularFile.read(
            url: metadataURL,
            maximumBytes: 256 * 1024,
            field: "live session metadata"
        )
        let metadata = try LiveStrictJSON.decodeCanonical(
            LiveCaptureSessionMetadata.self,
            from: bytes
        )
        try metadata.validate()
        let reference = try LiveCaptureFileEvidence.reference(
            captureRoot: captureRoot,
            relativePath: Self.sessionMetadataPath,
            mediaType: "application/json"
        )
        guard metadata.sessionID == pending.sessionID,
              metadata.createdAt
                == LiveAuthTime.string(pending.event.createdAt),
              reference == pending.metadata else {
            throw LiveSenderQueueError.sessionConflict
        }
    }

    @discardableResult
    private func restoreCurrentSession() async throws -> Bool {
        guard active == nil,
              let binding = try bindingStore.loadCurrent() else {
            return false
        }
        let captureRoot = try confinedCaptureRoot(
            binding.captureRoot(documentsRoot: documentsRoot)
        )
        let queue = try await LiveSenderQueue.open(
            captureRoot: captureRoot,
            stateURL: try paths.queueStateURL(
                desktopID: binding.session.authorization.desktopID,
                sessionID: binding.session.sessionID
            ),
            limits: limits,
            session: binding.session
        )
        if try await queue.snapshot().finalized {
            if let pending = try bindingStore.loadPending(
                documentsRoot: documentsRoot
            ) {
                try validatePending(
                    pending,
                    binding: binding,
                    captureRoot: captureRoot
                )
                try bindingStore.clearPending(
                    pending,
                    documentsRoot: documentsRoot
                )
            }
            try bindingStore.clearCurrent(binding)
            return true
        }
        let candidate = ActiveSession(
            binding: binding,
            queue: queue,
            captureRoot: captureRoot
        )
        _ = try await restoreCaptureJournal(candidate)
        if let pending = try bindingStore.loadPending(
            documentsRoot: documentsRoot
        ) {
            try validatePending(
                pending,
                binding: binding,
                captureRoot: captureRoot
            )
            try bindingStore.clearPending(
                pending,
                documentsRoot: documentsRoot
            )
        }
        active = candidate
        lastError = nil
        return true
    }

    private func start(_ pending: LiveCapturePendingStart) async throws {
        let event = pending.event
        let captureRoot = try confinedCaptureRoot(event.captureRoot)
        if active == nil {
            _ = try await restoreCurrentSession()
        }
        if let existing = active {
            if existing.captureRoot == captureRoot {
                try validatePending(
                    pending,
                    binding: existing.binding,
                    captureRoot: existing.captureRoot
                )
                let context = try await connector.currentContext(now: Date())
                guard context.authorization
                        == existing.binding.session.authorization,
                      context.authorization.desktopID
                        == pending.desktopID else {
                    throw LiveSenderQueueError.authorizationMismatch
                }
                active = nil
                _ = try await restoreCaptureJournal(existing)
                try bindingStore.clearPending(
                    pending,
                    documentsRoot: documentsRoot
                )
                active = existing
                return
            }
            guard try await existing.queue.snapshot().finalized else {
                throw LiveSenderQueueError.sessionConflict
            }
            try bindingStore.clearCurrent(existing.binding)
            active = nil
        }
        let context = try await connector.currentContext(now: Date())
        guard context.authorization.desktopID == pending.desktopID else {
            throw LiveSenderQueueError.authorizationMismatch
        }
        let metadataURL = captureRoot.appendingPathComponent(
            Self.sessionMetadataPath
        )
        let metadataData = try LiveBoundedRegularFile.read(
            url: metadataURL,
            maximumBytes: 256 * 1024,
            field: "live session metadata"
        )
        let metadata = try LiveStrictJSON.decodeCanonical(
            LiveCaptureSessionMetadata.self,
            from: metadataData
        )
        try metadata.validate()
        let metadataReference = try LiveCaptureFileEvidence.reference(
            captureRoot: captureRoot,
            relativePath: Self.sessionMetadataPath,
            mediaType: "application/json"
        )
        guard metadata.sessionID == pending.sessionID,
              metadataReference == pending.metadata,
              LiveAuthTime.string(event.createdAt) == metadata.createdAt else {
            throw LiveSenderQueueError.sessionConflict
        }
        let session = try LiveSenderSessionReference(
            sessionID: metadata.sessionID,
            expectedFrameCount: nil,
            metadata: metadataReference,
            authorization: context.authorization
        )
        let binding = try LiveCaptureSessionBinding(
            captureDirectoryName: captureRoot.lastPathComponent,
            session: session
        )
        let bindingSHA256 = try bindingStore.save(binding)
        let queue = try await LiveSenderQueue.open(
            captureRoot: captureRoot,
            stateURL: try paths.queueStateURL(
                desktopID: context.authorization.desktopID,
                sessionID: session.sessionID
            ),
            limits: limits,
            session: session
        )
        try bindingStore.claimCurrent(
            binding,
            bindingSHA256: bindingSHA256
        )
        let candidate = ActiveSession(
            binding: binding,
            queue: queue,
            captureRoot: captureRoot
        )
        _ = try await restoreCaptureJournal(candidate)
        try bindingStore.clearPending(
            pending,
            documentsRoot: documentsRoot
        )
        active = candidate
        lastError = nil
    }

    private func restoreCaptureJournal(
        _ candidate: ActiveSession
    ) async throws -> Bool {
        let frames = try LiveCaptureJournal.loadAcceptedFrames(
            captureRoot: candidate.captureRoot
        )
        for frame in frames {
            guard try await admit(frame, into: candidate) else {
                return false
            }
        }
        if let finalization = try LiveCaptureJournal.loadFinalization(
            captureRoot: candidate.captureRoot
        ) {
            guard try await finalize(finalization, into: candidate) else {
                throw LiveSenderQueueError.finalizationConflict
            }
        }
        return true
    }

    private func admit(_ event: LiveCaptureFrameCommittedEvent) async throws -> Bool {
        guard let active else { return false }
        return try await admit(event, into: active)
    }

    private func admit(
        _ event: LiveCaptureFrameCommittedEvent,
        into active: ActiveSession
    ) async throws -> Bool {
        let captureRoot = try confinedCaptureRoot(event.captureRoot)
        guard captureRoot == active.captureRoot else {
            throw LiveSenderQueueError.sessionConflict
        }
        let source = try LiveCaptureFileEvidence.reference(
            captureRoot: captureRoot,
            relativePath: event.sourceRelativePath,
            mediaType: "image/jpeg"
        )
        let sourceURL = try await active.queue.verifiedFileURL(for: source)
        let sourceDimensions = try LiveCaptureFileEvidence.jpegDimensions(
            url: sourceURL
        )
        let depth = try LiveCaptureFileEvidence.reference(
            captureRoot: captureRoot,
            relativePath: event.depthRelativePath,
            mediaType: "application/x-npy"
        )
        let confidence = try event.confidenceRelativePath.map {
            try LiveCaptureFileEvidence.reference(
                captureRoot: captureRoot,
                relativePath: $0,
                mediaType: "application/x-npy"
            )
        }
        let metadata = try LiveCaptureMetadataEncoder.frame(
            sessionID: active.binding.session.sessionID,
            event: event,
            source: source,
            sourceDimensions: sourceDimensions,
            depth: depth,
            confidence: confidence
        )
        let metadataPath = String(
            format: "metadata/live/frames/%08d.json",
            event.sequenceID
        )
        _ = try LiveCaptureFileEvidence.immutableWrite(
            LiveStrictJSON.canonicalData(metadata),
            captureRoot: captureRoot,
            relativePath: metadataPath
        )
        let metadataReference = try LiveCaptureFileEvidence.reference(
            captureRoot: captureRoot,
            relativePath: metadataPath,
            mediaType: "application/json"
        )
        var assets = [
            LiveSenderAssetReference(role: .source, file: source),
            LiveSenderAssetReference(role: .depth, file: depth),
        ]
        if let confidence {
            assets.append(
                LiveSenderAssetReference(role: .confidence, file: confidence)
            )
        }
        let frame = try LiveSenderFrameReference(
            sessionID: active.binding.session.sessionID,
            sequenceID: event.sequenceID,
            metadata: metadataReference,
            assets: assets
        )
        let result = try await active.queue.enqueue(frame)
        switch result.disposition {
        case .accepted, .duplicate:
            return true
        case .capacityExceeded:
            lastError = "Live sender queue capacity was reached; source evidence remains local."
            return false
        }
    }

    private func finalize(_ event: LiveCaptureFinalizedEvent) async throws -> Bool {
        guard let active else { return false }
        return try await finalize(event, into: active)
    }

    private func finalize(
        _ event: LiveCaptureFinalizedEvent,
        into active: ActiveSession
    ) async throws -> Bool {
        let captureRoot = try confinedCaptureRoot(event.captureRoot)
        guard captureRoot == active.captureRoot,
              event.manifestRelativePath == "capture.json" else {
            throw LiveSenderQueueError.finalizationConflict
        }
        let manifest = try LiveCaptureFileEvidence.reference(
            captureRoot: captureRoot,
            relativePath: event.manifestRelativePath,
            mediaType: "application/json"
        )
        guard manifest.sizeBytes == event.manifestSizeBytes,
              manifest.sha256 == event.manifestSHA256 else {
            throw LiveSenderQueueError.sourceChecksumMismatch(
                event.manifestRelativePath
            )
        }
        struct ManifestSchema: Decodable {
            let schema: String
        }
        let manifestURL = try await active.queue.verifiedFileURL(for: manifest)
        let evidence = try LiveStrictJSON.decode(
            ManifestSchema.self,
            from: LiveBoundedRegularFile.read(
                url: manifestURL,
                maximumBytes: 64 * 1024 * 1024,
                field: "final capture manifest"
            )
        )
        let sourceManifest = try LiveSenderSourceManifestReference(
            path: event.manifestRelativePath,
            sizeBytes: manifest.sizeBytes,
            sha256: manifest.sha256,
            schema: evidence.schema
        )
        try await active.queue.setFinalization(
            LiveSenderFinalizationReference(
                sessionID: active.binding.session.sessionID,
                finalSequenceID: event.finalSequenceID,
                sourceManifest: sourceManifest
            )
        )
        return true
    }

    private func confinedCaptureRoot(_ candidate: URL) throws -> URL {
        let root = candidate.standardizedFileURL
        let documents = documentsRoot.standardizedFileURL
        let documentsPrefix = documents.path.hasSuffix("/")
            ? documents.path
            : documents.path + "/"
        guard root.deletingLastPathComponent() == documents,
              root.path.hasPrefix(documentsPrefix),
              !root.lastPathComponent.isEmpty else {
            throw LiveSenderQueueError.sourceOutsideCaptureRoot(root.path)
        }
        return root
    }

    private static func interruptionDisposition(
        for error: Error
    ) -> LiveSenderInterruptionDisposition {
        if error is CancellationError {
            return .cancelled
        }
        if let request = error as? LiveAuthenticatedRequestError {
            return request.retryable ? .retryable : .blocked
        }
        if error is LiveBonjourResolverError
            || error is URLError
            || error is NWError {
            return .retryable
        }
        return .blocked
    }

    private static func message(for error: Error) -> String {
        if let localized = error as? LocalizedError,
           let description = localized.errorDescription,
           !description.isEmpty {
            return description
        }
        return String(describing: error)
    }
}

final class LiveCaptureSenderBridge: LiveCaptureSenderEventSink, @unchecked Sendable {
    private static let ingressCapacity = 512

    private final class DriveTaskSlot: @unchecked Sendable {
        private let lock = NSLock()
        private var task: Task<Void, Never>?
        private var cancellationGeneration: UInt64 = 0

        func generation() -> UInt64 {
            lock.lock()
            let value = cancellationGeneration
            lock.unlock()
            return value
        }

        func install(
            _ task: Task<Void, Never>,
            generation: UInt64
        ) {
            lock.lock()
            let cancelled = generation != cancellationGeneration
            if !cancelled {
                self.task = task
            }
            lock.unlock()
            if cancelled {
                task.cancel()
            }
        }

        func clear() {
            lock.lock()
            task = nil
            lock.unlock()
        }

        func cancel() {
            lock.lock()
            cancellationGeneration &+= 1
            let task = task
            lock.unlock()
            task?.cancel()
        }
    }

    private var eventContinuation: AsyncStream<LiveCaptureBridgeEvent>.Continuation?
    private var sendContinuation: AsyncStream<Void>.Continuation?
    private var eventTask: Task<Void, Never>?
    private var sendTask: Task<Void, Never>?
    private var networkMonitor: NWPathMonitor?
    private var thermalObserver: NSObjectProtocol?
    private var environmentState: LiveCaptureSenderEnvironmentState?
    private var recoveryStore: LiveCaptureSessionBindingStore?
    private var documentsRoot: URL?
    private let runtime: LiveCaptureSenderRuntime?
    private let random: (any LiveRandomSource)?
    private let driveTaskSlot = DriveTaskSlot()

    static func application(
        paths: LiveApplicationSupportPaths,
        documentsRoot: URL,
        connector: any LiveCaptureSenderConnecting
    ) throws -> LiveCaptureSenderBridge {
        try LiveCaptureSenderBridge(
            paths: paths,
            documentsRoot: documentsRoot,
            connector: connector,
            random: SystemLiveRandomSource(),
            limits: LiveSenderQueueLimits(
                maximumFrames: 360,
                maximumBytes: 4 * 1024 * 1024 * 1024,
                maximumInFlight: 2
            ),
            policy: LiveSenderPolicy(),
            retryPolicy: LiveSenderRetryPolicy(),
            monitorNetwork: true,
            initialNetworkAvailable: false
        )
    }

    static func disabled() -> LiveCaptureSenderBridge {
        LiveCaptureSenderBridge()
    }

    init(
        paths: LiveApplicationSupportPaths,
        documentsRoot: URL,
        connector: any LiveCaptureSenderConnecting,
        random: any LiveRandomSource,
        limits: LiveSenderQueueLimits,
        policy: LiveSenderPolicy,
        retryPolicy: LiveSenderRetryPolicy,
        monitorNetwork: Bool,
        initialNetworkAvailable: Bool,
        outerRetrySleeper: any LiveSenderSleeping = SystemLiveSenderSleeper()
    ) throws {
        let environmentState = LiveCaptureSenderEnvironmentState(
            networkAvailable: initialNetworkAvailable
        )
        let recoveryStore = LiveCaptureSessionBindingStore(paths: paths)
        let runtime = LiveCaptureSenderRuntime(
            paths: paths,
            documentsRoot: documentsRoot,
            connector: connector,
            limits: limits,
            policy: policy,
            retryPolicy: retryPolicy,
            retrySleeper: outerRetrySleeper,
            environmentState: environmentState
        )
        self.environmentState = environmentState
        self.recoveryStore = recoveryStore
        self.documentsRoot = documentsRoot.standardizedFileURL
        self.runtime = runtime
        self.random = random
        var capturedEvents: AsyncStream<LiveCaptureBridgeEvent>.Continuation?
        let events = AsyncStream(
            bufferingPolicy: .bufferingOldest(Self.ingressCapacity)
        ) {
            capturedEvents = $0
        }
        var capturedSends: AsyncStream<Void>.Continuation?
        let sends = AsyncStream(
            bufferingPolicy: .bufferingNewest(1)
        ) {
            capturedSends = $0
        }
        eventContinuation = capturedEvents
        sendContinuation = capturedSends
        eventTask = Task {
            for await event in events {
                if await runtime.handle(event) {
                    capturedSends?.yield(())
                }
            }
        }
        let taskSlot = driveTaskSlot
        sendTask = Task {
            for await _ in sends {
                let generation = taskSlot.generation()
                let drive = Task {
                    await runtime.drive()
                }
                taskSlot.install(drive, generation: generation)
                await drive.value
                taskSlot.clear()
            }
        }
        thermalObserver = NotificationCenter.default.addObserver(
            forName: ProcessInfo.thermalStateDidChangeNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            let state = self?.environmentState?.refreshThermalState()
            if state == .serious || state == .critical {
                self?.driveTaskSlot.cancel()
            } else {
                self?.wakeSender()
            }
        }
        if monitorNetwork {
            let monitor = NWPathMonitor()
            monitor.pathUpdateHandler = { [weak self] path in
                let available = path.status == .satisfied
                self?.environmentState?.setNetworkAvailable(available)
                if available {
                    self?.wakeSender()
                } else {
                    self?.driveTaskSlot.cancel()
                }
            }
            monitor.start(
                queue: DispatchQueue(
                    label: "capture-splat.live-sender.network"
                )
            )
            networkMonitor = monitor
        }
        _ = capturedEvents?.yield(.restore)
    }

    private init() {
        runtime = nil
        random = nil
    }

    deinit {
        if let thermalObserver {
            NotificationCenter.default.removeObserver(thermalObserver)
        }
        networkMonitor?.cancel()
        driveTaskSlot.cancel()
        eventContinuation?.finish()
        sendContinuation?.finish()
        eventTask?.cancel()
        sendTask?.cancel()
    }

    @discardableResult
    func captureStarted(
        _ event: LiveCaptureSessionStartedEvent
    ) -> LiveCaptureIngressDisposition {
        guard eventContinuation != nil,
              let desktopID = environmentState?.currentPairedDesktopID(),
              let recoveryStore,
              let documentsRoot,
              let random else {
            return .disabled
        }
        do {
            let metadataURL = event.captureRoot.appendingPathComponent(
                "metadata/live/session.json"
            )
            let sessionMetadata: LiveCaptureSessionMetadata
            if FileManager.default.fileExists(atPath: metadataURL.path) {
                let bytes = try LiveBoundedRegularFile.read(
                    url: metadataURL,
                    maximumBytes: 256 * 1024,
                    field: "live session metadata"
                )
                sessionMetadata = try LiveStrictJSON.decodeCanonical(
                    LiveCaptureSessionMetadata.self,
                    from: bytes
                )
                try sessionMetadata.validate()
                guard sessionMetadata.createdAt
                        == LiveAuthTime.string(event.createdAt) else {
                    throw LiveSenderQueueError.sessionConflict
                }
            } else {
                let created = try LiveCaptureMetadataEncoder.session(
                    seed: random.bytes(count: 32),
                    createdAt: event.createdAt
                )
                _ = try LiveCaptureFileEvidence.immutableWrite(
                    created.data,
                    captureRoot: event.captureRoot,
                    relativePath: "metadata/live/session.json"
                )
                sessionMetadata = created.metadata
            }
            let metadata = try LiveCaptureFileEvidence.reference(
                captureRoot: event.captureRoot,
                relativePath: "metadata/live/session.json",
                mediaType: "application/json"
            )
            let pending = LiveCapturePendingStart(
                event: event,
                desktopID: desktopID,
                sessionID: sessionMetadata.sessionID,
                metadata: metadata
            )
            try recoveryStore.claimPending(
                pending,
                documentsRoot: documentsRoot
            )
        } catch {
            return .overflow
        }
        return yield(.started(event))
    }

    @discardableResult
    func frameCommitted(
        _ event: LiveCaptureFrameCommittedEvent
    ) -> LiveCaptureIngressDisposition {
        yield(.frame(event))
    }

    @discardableResult
    func captureFinalized(
        _ event: LiveCaptureFinalizedEvent
    ) -> LiveCaptureIngressDisposition {
        yield(.finalized(event))
    }

    @discardableResult
    func captureAborted(
        _ event: LiveCaptureAbortedEvent
    ) -> LiveCaptureIngressDisposition {
        yield(.aborted(event))
    }

    func hasPendingTransfer() async throws -> Bool {
        guard let runtime else { return false }
        return try await runtime.hasPendingTransfer()
    }

    func abandonPendingTransfer() async throws {
        driveTaskSlot.cancel()
        guard let runtime else { return }
        try await runtime.abandonPendingTransfer()
    }

    func setForeground(_ value: Bool) {
        environmentState?.setForeground(value)
        if value {
            wakeSender()
        } else {
            driveTaskSlot.cancel()
        }
    }

    func setPairedDesktopID(_ value: String?) {
        environmentState?.setPairedDesktopID(value)
        if value != nil {
            _ = eventContinuation?.yield(.restore)
            wakeSender()
        } else {
            driveTaskSlot.cancel()
        }
    }

    private func wakeSender() {
        _ = sendContinuation?.yield(())
    }

    private func yield(
        _ event: LiveCaptureBridgeEvent
    ) -> LiveCaptureIngressDisposition {
        guard let eventContinuation else { return .disabled }
        switch eventContinuation.yield(event) {
        case .enqueued:
            return .accepted
        case .dropped:
            return .overflow
        case .terminated:
            return .disabled
        @unknown default:
            return .disabled
        }
    }
}

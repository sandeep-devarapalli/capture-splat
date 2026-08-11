import CryptoKit
import Foundation
import Network

private final class PairingMemorySecureStore: LiveSecureValueStore, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Data] = [:]
    private var failingRemovals: Set<String> = []

    func read(account: String) throws -> Data? {
        lock.withLock { values[account] }
    }

    func write(_ data: Data, account: String) throws {
        lock.withLock { values[account] = data }
    }

    func remove(account: String) throws {
        let shouldFail = lock.withLock {
            failingRemovals.remove(account) != nil
        }
        if shouldFail {
            throw LiveAuthContractError.invalid("Simulated Keychain removal failure.")
        }
        lock.withLock { _ = values.removeValue(forKey: account) }
    }

    func removeAll() throws {
        lock.withLock { values.removeAll() }
    }

    func failNextRemove(account: String) {
        _ = lock.withLock { failingRemovals.insert(account) }
    }
}

private final class PairingDeterministicRandom: LiveRandomSource, @unchecked Sendable {
    private let lock = NSLock()
    private var next: UInt8

    init(start: UInt8) {
        next = start
    }

    func bytes(count: Int) throws -> Data {
        lock.withLock {
            let bytes = Data((0..<count).map { next &+ UInt8($0 % 223) })
            next &+= UInt8(count % 223)
            return bytes
        }
    }
}

@MainActor
private final class PairingFixedResolver: LiveBonjourResolving {
    let endpoint: LiveResolvedEndpoint
    private(set) var resolveCount = 0
    private(set) var cancelCount = 0

    init(endpoint: LiveResolvedEndpoint) {
        self.endpoint = endpoint
    }

    func resolve(
        discovery: LiveDiscoveryIdentity,
        timeout: TimeInterval
    ) async throws -> LiveResolvedEndpoint {
        resolveCount += 1
        guard discovery == endpoint.discovery, timeout > 0 else {
            throw LiveBonjourResolverError.resolutionFailed
        }
        return endpoint
    }

    func cancel() {
        cancelCount += 1
    }
}

@MainActor
private final class PairingHoldingResolver: LiveBonjourResolving {
    private var continuation: CheckedContinuation<LiveResolvedEndpoint, Error>?
    private(set) var resolveCount = 0
    private(set) var cancelCount = 0

    func resolve(
        discovery: LiveDiscoveryIdentity,
        timeout: TimeInterval
    ) async throws -> LiveResolvedEndpoint {
        resolveCount += 1
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
        }
    }

    func cancel() {
        cancelCount += 1
        continuation?.resume(throwing: CancellationError())
        continuation = nil
    }
}

private actor PairingResponseTransport: LiveHTTPSPerforming {
    let response: LiveHTTPResponse

    init(response: LiveHTTPResponse) {
        self.response = response
    }

    func perform(
        _ request: URLRequest,
        upload: LiveHTTPUpload
    ) async throws -> LiveHTTPResponse {
        response
    }
}

private actor ProbePairingService: LivePairingServicing {
    enum Mode {
        case networkFailure
        case persistThenHold
        case success
    }

    private let client: LivePairingClient
    private let desktopPrivateKey: P256.Signing.PrivateKey
    private let approvalTime: Date
    private var mode: Mode
    private(set) var prepareCount = 0
    private(set) var resumeCount = 0
    private(set) var submitCount = 0
    private(set) var lastDeviceName: String?
    private(set) var persistedGrant = false
    private var releaseContinuation: CheckedContinuation<Void, Never>?
    private var released = false

    init(
        client: LivePairingClient,
        desktopPrivateKey: P256.Signing.PrivateKey,
        approvalTime: Date,
        mode: Mode
    ) {
        self.client = client
        self.desktopPrivateKey = desktopPrivateKey
        self.approvalTime = approvalTime
        self.mode = mode
    }

    func preparePairing(
        invitationURI: String,
        endpoint: LiveResolvedEndpoint,
        deviceName: String,
        appVersion: String,
        now: Date
    ) async throws -> LivePreparedPairing {
        prepareCount += 1
        lastDeviceName = deviceName
        return try await client.prepare(
            invitationURI: invitationURI,
            endpoint: endpoint,
            deviceName: deviceName,
            appVersion: appVersion,
            now: now
        )
    }

    func resumePairing(desktopID: String) async throws -> LivePreparedPairing? {
        resumeCount += 1
        return try await client.resumePending(desktopID: desktopID)
    }

    func submitPairing(_ prepared: LivePreparedPairing) async throws -> LiveStoredGrant {
        submitCount += 1
        if mode == .networkFailure {
            throw LiveAuthenticatedRequestError.network("Simulated lost pairing response.")
        }

        let request = prepared.requestPayload
        let binding = prepared.binding
        let payload = LivePairingGrantPayload(
            schema: "capture_splat.live_pairing_grant_payload.v0.1",
            pairingID: request.pairingID,
            requestID: request.requestID,
            grantID: try LiveAuthEncoding.randomID(
                prefix: "csg",
                bytes: Data(160..<176)
            ),
            pairingEpoch: 1,
            audience: LiveAuthContract.audience,
            desktopID: request.desktopID,
            deviceID: request.deviceID,
            devicePublicKeyBase64URL: request.devicePublicKeyBase64URL,
            permissions: request.requestedPermissions,
            authScheme: LiveAuthContract.authScheme,
            liveDiscovery: binding.discovery,
            tlsCertificateSHA256: binding.tlsCertificateSHA256,
            issuedAt: LiveAuthTime.string(approvalTime),
            notBefore: LiveAuthTime.string(approvalTime),
            expiresAt: LiveAuthTime.string(
                approvalTime.addingTimeInterval(86_400)
            ),
            authority: LiveAuthContract.authority
        )
        let payloadBytes = try LiveStrictJSON.canonicalData(payload)
        let signature = try desktopPrivateKey.signature(
            for: LiveAuthContract.grantSignatureDomain + payloadBytes
        ).rawRepresentation
        let envelope = LivePairingGrantEnvelope(
            schema: "capture_splat.live_pairing_grant_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(payloadBytes),
            desktopSignatureBase64URL: LiveAuthEncoding.encodeBase64URL(signature)
        )
        var body = try LiveStrictJSON.canonicalData(envelope)
        body.append(10)
        let grant = try await client.submitForTesting(
            prepared,
            using: PairingResponseTransport(response: LiveHTTPResponse(
                statusCode: 200,
                body: body,
                headers: [:]
            )),
            clock: { self.approvalTime }
        )
        persistedGrant = true
        if mode == .persistThenHold, !released {
            await withCheckedContinuation { continuation in
                releaseContinuation = continuation
            }
        }
        return grant
    }

    func releasePersistedGrant() {
        released = true
        releaseContinuation?.resume()
        releaseContinuation = nil
    }
}

@main
private enum LivePairingAppProbe {
    static func main() async throws {
        guard CommandLine.arguments.count == 2 else {
            throw LiveAuthContractError.invalid("Expected a working directory.")
        }
        let root = URL(
            fileURLWithPath: CommandLine.arguments[1],
            isDirectory: true
        )
        let result = try await run(root: root)
        let data = try JSONSerialization.data(
            withJSONObject: result,
            options: [.sortedKeys]
        )
        FileHandle.standardOutput.write(data)
    }

    private static func run(root: URL) async throws -> [String: Any] {
        let paths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("state/v0.1", isDirectory: true)
        )
        let secureStore = PairingMemorySecureStore()
        let devicePrivateKey = try P256.Signing.PrivateKey(
            rawRepresentation: Data(repeating: 9, count: 32)
        )
        try secureStore.write(
            devicePrivateKey.rawRepresentation,
            account: "device-p256-private-key"
        )
        let desktopPrivateKey = try P256.Signing.PrivateKey(
            rawRepresentation: Data(repeating: 11, count: 32)
        )
        let desktopPublicKey = desktopPrivateKey.publicKey.x963Representation
        let now = try LiveAuthTime.parse("2026-07-30T12:00:00.000Z")
        let discovery = LiveDiscoveryIdentity(
            serviceType: LiveAuthContract.bonjourServiceType,
            serviceName: "World Studio Probe",
            domain: LiveAuthContract.bonjourDomain
        )
        let invitation = LivePairingInvitation(
            schema: "capture_splat.live_pairing_invitation.v0.1",
            pairingID: try LiveAuthEncoding.randomID(
                prefix: "csp",
                bytes: Data(0..<16)
            ),
            mode: "qr",
            desktopID: LiveAuthEncoding.identity(
                prefix: "wsd",
                publicKeyX963: desktopPublicKey
            ),
            desktopName: "World Studio Probe",
            desktopPublicKeyBase64URL: LiveAuthEncoding.encodeBase64URL(
                desktopPublicKey
            ),
            discovery: discovery,
            tlsCertificateSHA256: "sha256:" + String(repeating: "a", count: 64),
            pairingSecretBase64URL: LiveAuthEncoding.encodeBase64URL(
                Data(32..<64)
            ),
            issuedAt: LiveAuthTime.string(now.addingTimeInterval(-1)),
            expiresAt: LiveAuthTime.string(now.addingTimeInterval(299)),
            permissions: LiveAuthContract.permissions,
            authority: LiveAuthContract.authority
        )
        let invitationURI = try LivePairingClient.invitationURI(invitation)
        let endpoint = LiveResolvedEndpoint(
            host: "world-studio-probe.local",
            port: 43128,
            discovery: discovery
        )
        let firstStores = stores(
            paths: paths,
            secureStore: secureStore,
            randomStart: 1
        )
        let failureService = ProbePairingService(
            client: firstStores.client,
            desktopPrivateKey: desktopPrivateKey,
            approvalTime: now.addingTimeInterval(60),
            mode: .networkFailure
        )
        let firstResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let coordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: firstStores.profileStore,
                recoveryStore: firstStores.recoveryStore,
                grantStore: firstStores.grantStore,
                pendingStore: firstStores.pendingStore,
                pairingService: failureService,
                resolverFactory: { firstResolver },
                deviceName: { String(repeating: "📱", count: 40) },
                appVersion: { "0.1.0" },
                clock: { now }
            )
        }

        await coordinator.beginPairing(invitationURI: invitationURI)
        let noDiscoveryBeforeRestore = await firstResolver.resolveCount == 0
        await coordinator.restore()
        let noDiscoveryOnRestore = await firstResolver.resolveCount == 0
        await coordinator.startScanning()
        let noDiscoveryWhileScanning = await firstResolver.resolveCount == 0
        await coordinator.beginPairing(invitationURI: invitationURI)
        let interrupted = await wait(
            coordinator: coordinator,
            for: .interrupted
        )
        let firstResolveCount = await firstResolver.resolveCount
        let firstSnapshot = await coordinator.snapshot
        let boundedDeviceName = await failureService.lastDeviceName

        let profileBytes = try Data(contentsOf: paths.pairingProfileURL)
        let profileText = String(decoding: profileBytes, as: UTF8.self)
        let noSecretInProfile = !profileText.contains(
            invitation.pairingSecretBase64URL
        )
            && !profileText.contains(invitation.tlsCertificateSHA256)
            && !profileText.contains(LiveAuthContract.qrPrefix)

        let secondStores = stores(
            paths: paths,
            secureStore: secureStore,
            randomStart: 80
        )
        let successService = ProbePairingService(
            client: secondStores.client,
            desktopPrivateKey: desktopPrivateKey,
            approvalTime: now.addingTimeInterval(60),
            mode: .success
        )
        let secondResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let resumedCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: secondStores.profileStore,
                recoveryStore: secondStores.recoveryStore,
                grantStore: secondStores.grantStore,
                pendingStore: secondStores.pendingStore,
                pairingService: successService,
                resolverFactory: { secondResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(60) }
            )
        }
        await resumedCoordinator.restore()
        let restoredPendingSnapshot = await resumedCoordinator.snapshot
        let restoredPending = restoredPendingSnapshot.phase == .interrupted
            && restoredPendingSnapshot.canRetry
        let noDiscoveryOnPendingRestore = await secondResolver.resolveCount == 0
        await resumedCoordinator.retry()
        let paired = await wait(
            coordinator: resumedCoordinator,
            for: .paired
        )
        await resumedCoordinator.startScanning()
        await resumedCoordinator.beginPairing(invitationURI: invitationURI)
        let singlePairingSnapshot = await resumedCoordinator.snapshot
        let singlePairingResolveCount = await secondResolver.resolveCount
        let stillSinglePairing = singlePairingSnapshot.phase == .paired
            && singlePairingResolveCount == 0

        let thirdStores = stores(
            paths: paths,
            secureStore: secureStore,
            randomStart: 120
        )
        let thirdResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let restoredCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: thirdStores.profileStore,
                recoveryStore: thirdStores.recoveryStore,
                grantStore: thirdStores.grantStore,
                pendingStore: thirdStores.pendingStore,
                pairingService: ProbePairingService(
                    client: thirdStores.client,
                    desktopPrivateKey: desktopPrivateKey,
                    approvalTime: now.addingTimeInterval(60),
                    mode: .success
                ),
                resolverFactory: { thirdResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(61) }
            )
        }
        await restoredCoordinator.restore()
        let restoredSnapshot = await restoredCoordinator.snapshot
        let thirdResolveCount = await thirdResolver.resolveCount
        let pairedRestoredWithoutNetwork = restoredSnapshot.phase == .paired
            && thirdResolveCount == 0

        try Data("{\"broken\":true}".utf8).write(to: paths.pairingProfileURL)
        let healedStores = stores(
            paths: paths,
            secureStore: secureStore,
            randomStart: 130
        )
        let healedResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let healedCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: healedStores.profileStore,
                recoveryStore: healedStores.recoveryStore,
                grantStore: healedStores.grantStore,
                pendingStore: healedStores.pendingStore,
                pairingService: ProbePairingService(
                    client: healedStores.client,
                    desktopPrivateKey: desktopPrivateKey,
                    approvalTime: now.addingTimeInterval(60),
                    mode: .success
                ),
                resolverFactory: { healedResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(61) },
                hasPendingLiveTransfer: LivePairingCoordinator
                    .pendingLiveTransferCheck(
                        currentSessionURL: paths.currentSessionURL,
                        pendingCaptureURL: paths.pendingCaptureURL
                    )
            )
        }
        await healedCoordinator.restore()
        let healedSnapshot = await healedCoordinator.snapshot
        let healedProfiles = try await healedStores.profileStore.snapshot()
        let healedResolveCount = await healedResolver.resolveCount
        let corruptProfileRecoveredFromKeychain = healedSnapshot.phase == .paired
            && healedProfiles.current?.desktopID == invitation.desktopID
            && healedResolveCount == 0

        try Data("{}".utf8).write(
            to: paths.currentSessionURL,
            options: .atomic
        )
        await healedCoordinator.clearLocalPairing()
        let pendingForgetSnapshot = await healedCoordinator.snapshot
        let pendingForgetGrant = try await healedStores.grantStore.load(
            desktopID: invitation.desktopID,
            currentAt: now.addingTimeInterval(61)
        )
        let pendingTransferBlocksForget = pendingForgetSnapshot.phase == .failed
            && pendingForgetSnapshot.message.contains(
                "Finish the pending live transfer before forgetting this Mac."
            )
            && pendingForgetGrant != nil
        try FileManager.default.removeItem(at: paths.currentSessionURL)

        await healedCoordinator.clearLocalPairing()
        let forgottenSnapshot = await healedCoordinator.snapshot
        let forgottenProfiles = try await healedStores.profileStore.snapshot()
        let forgottenGrant = try await healedStores.grantStore.load(
            desktopID: invitation.desktopID,
            currentAt: now.addingTimeInterval(61)
        )
        let localForgetDurable = forgottenSnapshot.phase == .off
            && forgottenProfiles.current == nil
            && forgottenGrant == nil

        let exactEndpoint = NWEndpoint.service(
            name: discovery.serviceName,
            type: "\(discovery.serviceType).",
            domain: discovery.domain,
            interface: nil
        )
        let wrongEndpoint = NWEndpoint.service(
            name: "Another Mac",
            type: discovery.serviceType,
            domain: discovery.domain,
            interface: nil
        )
        let exactBonjourMatch = await LiveBonjourResolver.matches(
            endpoint: exactEndpoint,
            discovery: discovery
        )
        let wrongBonjourIgnored = await !LiveBonjourResolver.matches(
            endpoint: wrongEndpoint,
            discovery: discovery
        )

        let racePaths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("grant-race/v0.1", isDirectory: true)
        )
        let raceSecureStore = PairingMemorySecureStore()
        try raceSecureStore.write(
            devicePrivateKey.rawRepresentation,
            account: "device-p256-private-key"
        )
        let raceStores = stores(
            paths: racePaths,
            secureStore: raceSecureStore,
            randomStart: 140
        )
        let raceService = ProbePairingService(
            client: raceStores.client,
            desktopPrivateKey: desktopPrivateKey,
            approvalTime: now.addingTimeInterval(60),
            mode: .persistThenHold
        )
        let raceResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let raceCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: raceStores.profileStore,
                recoveryStore: raceStores.recoveryStore,
                grantStore: raceStores.grantStore,
                pendingStore: raceStores.pendingStore,
                pairingService: raceService,
                resolverFactory: { raceResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(60) }
            )
        }
        await raceCoordinator.restore()
        await raceCoordinator.beginPairing(invitationURI: invitationURI)
        for _ in 0..<2_000 {
            if await raceService.persistedGrant {
                break
            }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        await raceCoordinator.cancel()
        await raceService.releasePersistedGrant()
        let raceSurfaced = await wait(
            coordinator: raceCoordinator,
            for: .paired
        )
        let raceProfiles = try await raceStores.profileStore.snapshot()
        let cancelledDurableGrantSurfaced = raceSurfaced
            && raceProfiles.current?.desktopID == invitation.desktopID

        let expiredPaths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("expired-cancel/v0.1", isDirectory: true)
        )
        let expiredSecureStore = PairingMemorySecureStore()
        try expiredSecureStore.write(
            devicePrivateKey.rawRepresentation,
            account: "device-p256-private-key"
        )
        let expiredStores = stores(
            paths: expiredPaths,
            secureStore: expiredSecureStore,
            randomStart: 150
        )
        let expiredService = ProbePairingService(
            client: expiredStores.client,
            desktopPrivateKey: desktopPrivateKey,
            approvalTime: now.addingTimeInterval(-172_800),
            mode: .persistThenHold
        )
        let expiredResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let expiredCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: expiredStores.profileStore,
                recoveryStore: expiredStores.recoveryStore,
                grantStore: expiredStores.grantStore,
                pendingStore: expiredStores.pendingStore,
                pairingService: expiredService,
                resolverFactory: { expiredResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now }
            )
        }
        await expiredCoordinator.restore()
        await expiredCoordinator.beginPairing(invitationURI: invitationURI)
        for _ in 0..<2_000 {
            if await expiredService.persistedGrant {
                break
            }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        await expiredCoordinator.cancel()
        await expiredService.releasePersistedGrant()
        let expiredGrantFailed = await wait(
            coordinator: expiredCoordinator,
            for: .failed
        )
        let expiredSnapshot = await expiredCoordinator.snapshot
        let expiredGrantNotPaired = expiredGrantFailed
            && expiredSnapshot.phase == .failed
            && expiredSnapshot.desktopID == invitation.desktopID

        let pendingRemovePaths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("pending-remove/v0.1", isDirectory: true)
        )
        let pendingRemoveSecureStore = PairingMemorySecureStore()
        try pendingRemoveSecureStore.write(
            devicePrivateKey.rawRepresentation,
            account: "device-p256-private-key"
        )
        let pendingRemoveStores = stores(
            paths: pendingRemovePaths,
            secureStore: pendingRemoveSecureStore,
            randomStart: 155
        )
        pendingRemoveSecureStore.failNextRemove(
            account: "pending-pairing.\(invitation.desktopID)"
        )
        let pendingRemoveResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let pendingRemoveCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: pendingRemoveStores.profileStore,
                recoveryStore: pendingRemoveStores.recoveryStore,
                grantStore: pendingRemoveStores.grantStore,
                pendingStore: pendingRemoveStores.pendingStore,
                pairingService: ProbePairingService(
                    client: pendingRemoveStores.client,
                    desktopPrivateKey: desktopPrivateKey,
                    approvalTime: now.addingTimeInterval(60),
                    mode: .success
                ),
                resolverFactory: { pendingRemoveResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(60) }
            )
        }
        await pendingRemoveCoordinator.restore()
        await pendingRemoveCoordinator.beginPairing(invitationURI: invitationURI)
        let pendingRemovePaired = await wait(
            coordinator: pendingRemoveCoordinator,
            for: .paired
        )
        let pendingRemoveProfiles = try await pendingRemoveStores.profileStore.snapshot()
        let pendingRemoveFailureReconciled = pendingRemovePaired
            && pendingRemoveProfiles.current?.desktopID == invitation.desktopID

        try Data("{}".utf8).write(
            to: pendingRemovePaths.pendingCaptureURL,
            options: .atomic
        )
        let pendingResetCoordinator = await MainActor.run {
            LivePairingCoordinator.startupFailureForTesting(
                message: "Simulated Application Support failure.",
                recoveryStore: pendingRemoveStores.recoveryStore,
                grantStore: pendingRemoveStores.grantStore,
                pendingStore: pendingRemoveStores.pendingStore,
                hasPendingLiveTransfer: LivePairingCoordinator
                    .pendingLiveTransferCheck(
                        currentSessionURL: pendingRemovePaths.currentSessionURL,
                        pendingCaptureURL: pendingRemovePaths.pendingCaptureURL
                    )
            )
        }
        await pendingResetCoordinator.restore()
        let pendingResetAvailable = await pendingResetCoordinator
            .canResetAllCredentials
        await pendingResetCoordinator.resetAllLocalCredentials()
        let pendingResetSnapshot = await pendingResetCoordinator.snapshot
        let pendingResetGrant = try await pendingRemoveStores.grantStore.load(
            desktopID: invitation.desktopID
        )
        let pendingResetPointer = try await pendingRemoveStores.recoveryStore.load()
        let pendingTransferBlocksCredentialReset = pendingResetAvailable
            && pendingResetSnapshot.message.contains(
                "Finish the pending live transfer before forgetting this Mac."
            )
            && pendingResetGrant != nil
            && pendingResetPointer?.desktopID == invitation.desktopID
        try FileManager.default.removeItem(
            at: pendingRemovePaths.pendingCaptureURL
        )
        let pendingSymlinkTarget = root.appendingPathComponent(
            "pending-pointer-target.json"
        )
        try Data("{}".utf8).write(
            to: pendingSymlinkTarget,
            options: .atomic
        )
        try FileManager.default.createSymbolicLink(
            at: pendingRemovePaths.pendingCaptureURL,
            withDestinationURL: pendingSymlinkTarget
        )
        let pendingSymlinkCheck = await MainActor.run {
            LivePairingCoordinator.pendingLiveTransferCheck(
                currentSessionURL: pendingRemovePaths.currentSessionURL,
                pendingCaptureURL: pendingRemovePaths.pendingCaptureURL
            )
        }
        let pendingSymlinkBlocksPairingClear = try pendingSymlinkCheck()
        try FileManager.default.removeItem(
            at: pendingRemovePaths.pendingCaptureURL
        )

        let startupFailureCoordinator = await MainActor.run {
            LivePairingCoordinator.startupFailureForTesting(
                message: "Simulated Application Support failure.",
                recoveryStore: pendingRemoveStores.recoveryStore,
                grantStore: pendingRemoveStores.grantStore,
                pendingStore: pendingRemoveStores.pendingStore
            )
        }
        await startupFailureCoordinator.restore()
        await startupFailureCoordinator.resetAllLocalCredentials()
        let startupFailureResetSnapshot = await startupFailureCoordinator.snapshot
        let startupFailureGrant = try await pendingRemoveStores.grantStore.load(
            desktopID: invitation.desktopID
        )
        let startupFailurePointer = try await pendingRemoveStores.recoveryStore.load()
        let startupFailureResetFailedClosed =
            startupFailureResetSnapshot.message.contains(
                "Pending live transfer state could not be verified."
            )
            && startupFailureGrant != nil
            && startupFailurePointer?.desktopID == invitation.desktopID

        let promotionFailurePaths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("promotion-failure/v0.1", isDirectory: true)
        )
        let promotionFailureSecureStore = PairingMemorySecureStore()
        try promotionFailureSecureStore.write(
            devicePrivateKey.rawRepresentation,
            account: "device-p256-private-key"
        )
        let promotionFailureStores = stores(
            paths: promotionFailurePaths,
            secureStore: promotionFailureSecureStore,
            randomStart: 165
        )
        let promotionFailureService = ProbePairingService(
            client: promotionFailureStores.client,
            desktopPrivateKey: desktopPrivateKey,
            approvalTime: now.addingTimeInterval(60),
            mode: .persistThenHold
        )
        let promotionFailureResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let promotionFailureCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: promotionFailureStores.profileStore,
                recoveryStore: promotionFailureStores.recoveryStore,
                grantStore: promotionFailureStores.grantStore,
                pendingStore: promotionFailureStores.pendingStore,
                pairingService: promotionFailureService,
                resolverFactory: { promotionFailureResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(60) }
            )
        }
        await promotionFailureCoordinator.restore()
        await promotionFailureCoordinator.beginPairing(invitationURI: invitationURI)
        for _ in 0..<2_000 {
            if await promotionFailureService.persistedGrant {
                break
            }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        let blockedRoot = promotionFailurePaths.root
        let savedRoot = blockedRoot.deletingLastPathComponent()
            .appendingPathComponent("v0.1.saved", isDirectory: true)
        try FileManager.default.moveItem(at: blockedRoot, to: savedRoot)
        guard FileManager.default.createFile(
            atPath: blockedRoot.path,
            contents: Data("blocked".utf8)
        ) else {
            throw LiveAuthContractError.invalid("Could not block the pairing profile root.")
        }
        await promotionFailureService.releasePersistedGrant()
        let promotionFailedVisible = await wait(
            coordinator: promotionFailureCoordinator,
            for: .failed
        )
        await promotionFailureCoordinator.startScanning()
        await promotionFailureCoordinator.beginPairing(invitationURI: invitationURI)
        let blockedSnapshot = await promotionFailureCoordinator.snapshot
        let blockedResolveCount = await promotionFailureResolver.resolveCount
        let durableGrant = try await promotionFailureStores.grantStore.load(
            desktopID: invitation.desktopID,
            currentAt: now.addingTimeInterval(60)
        )
        try FileManager.default.removeItem(at: blockedRoot)
        try FileManager.default.moveItem(at: savedRoot, to: blockedRoot)

        let recoveredStores = stores(
            paths: promotionFailurePaths,
            secureStore: promotionFailureSecureStore,
            randomStart: 175
        )
        let recoveredResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let recoveredCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: recoveredStores.profileStore,
                recoveryStore: recoveredStores.recoveryStore,
                grantStore: recoveredStores.grantStore,
                pendingStore: recoveredStores.pendingStore,
                pairingService: ProbePairingService(
                    client: recoveredStores.client,
                    desktopPrivateKey: desktopPrivateKey,
                    approvalTime: now.addingTimeInterval(60),
                    mode: .success
                ),
                resolverFactory: { recoveredResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(60) }
            )
        }
        await recoveredCoordinator.restore()
        let recoveredSnapshot = await recoveredCoordinator.snapshot
        let recoveredResolveCount = await recoveredResolver.resolveCount
        let promotionFailureRecovered = promotionFailedVisible
            && durableGrant != nil
            && blockedSnapshot.phase == .failed
            && blockedSnapshot.desktopID == invitation.desktopID
            && blockedResolveCount == 1
            && recoveredSnapshot.phase == .paired
            && recoveredResolveCount == 0

        let queueURL = try paths.queueStateURL(
            desktopID: invitation.desktopID,
            sessionID: "capture-session-01"
        )
        var traversalRejected = false
        do {
            _ = try paths.queueStateURL(
                desktopID: invitation.desktopID,
                sessionID: "../escape"
            )
        } catch {
            traversalRejected = true
        }

        var corruptBytes = try Data(contentsOf: paths.pairingProfileURL)
        corruptBytes[corruptBytes.count - 1] = 0x78
        try corruptBytes.write(to: paths.pairingProfileURL)
        try secureStore.write(
            Data("{\"broken\":true}".utf8),
            account: "pairing-profile-pointer"
        )
        let corruptStores = stores(
            paths: paths,
            secureStore: secureStore,
            randomStart: 150
        )
        let corruptResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let corruptCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: corruptStores.profileStore,
                recoveryStore: corruptStores.recoveryStore,
                grantStore: corruptStores.grantStore,
                pendingStore: corruptStores.pendingStore,
                pairingService: ProbePairingService(
                    client: corruptStores.client,
                    desktopPrivateKey: desktopPrivateKey,
                    approvalTime: now.addingTimeInterval(60),
                    mode: .success
                ),
                resolverFactory: { corruptResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(61) }
            )
        }
        await corruptCoordinator.restore()
        let corruptSnapshot = await corruptCoordinator.snapshot
        await corruptCoordinator.startScanning()
        await corruptCoordinator.beginPairing(invitationURI: invitationURI)
        let corruptResolveCount = await corruptResolver.resolveCount
        let resetWasAvailable = await corruptCoordinator.canResetAllCredentials
        await corruptCoordinator.resetAllLocalCredentials()

        let resetStores = stores(
            paths: paths,
            secureStore: secureStore,
            randomStart: 160
        )
        let resetResolver = await MainActor.run {
            PairingFixedResolver(endpoint: endpoint)
        }
        let resetCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: resetStores.profileStore,
                recoveryStore: resetStores.recoveryStore,
                grantStore: resetStores.grantStore,
                pendingStore: resetStores.pendingStore,
                pairingService: ProbePairingService(
                    client: resetStores.client,
                    desktopPrivateKey: desktopPrivateKey,
                    approvalTime: now.addingTimeInterval(60),
                    mode: .success
                ),
                resolverFactory: { resetResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now.addingTimeInterval(61) }
            )
        }
        await resetCoordinator.restore()
        let resetSnapshot = await resetCoordinator.snapshot
        let resetResolveCount = await resetResolver.resolveCount
        let resetCanStart = await resetCoordinator.canStartNewPairing
        let resetRecoveredAfterRestart = resetSnapshot.phase == .off
            && resetCanStart
            && resetResolveCount == 0
        let corruptionFailedClosed = corruptSnapshot.phase == .failed
            && corruptResolveCount == 0
            && resetWasAvailable
            && resetRecoveredAfterRestart

        let cancelPaths = try LiveApplicationSupportPaths(
            root: root.appendingPathComponent("cancel/v0.1", isDirectory: true)
        )
        let cancelSecureStore = PairingMemorySecureStore()
        try cancelSecureStore.write(
            devicePrivateKey.rawRepresentation,
            account: "device-p256-private-key"
        )
        let cancelStores = stores(
            paths: cancelPaths,
            secureStore: cancelSecureStore,
            randomStart: 180
        )
        let holdingResolver = await MainActor.run {
            PairingHoldingResolver()
        }
        let cancelCoordinator = await MainActor.run {
            LivePairingCoordinator(
                profileStore: cancelStores.profileStore,
                recoveryStore: cancelStores.recoveryStore,
                grantStore: cancelStores.grantStore,
                pendingStore: cancelStores.pendingStore,
                pairingService: ProbePairingService(
                    client: cancelStores.client,
                    desktopPrivateKey: desktopPrivateKey,
                    approvalTime: now.addingTimeInterval(60),
                    mode: .success
                ),
                resolverFactory: { holdingResolver },
                deviceName: { "Capture Splat Probe" },
                appVersion: { "0.1.0" },
                clock: { now }
            )
        }
        await cancelCoordinator.restore()
        await cancelCoordinator.beginPairing(invitationURI: invitationURI)
        for _ in 0..<2_000 {
            if await holdingResolver.resolveCount == 1 {
                break
            }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        await cancelCoordinator.handleBackgrounding()
        let backgroundCancelled = await wait(
            coordinator: cancelCoordinator,
            for: .off
        )
        let holdingCancelCount = await holdingResolver.cancelCount
        let cancelSnapshot = await cancelCoordinator.snapshot
        let cancelledProfiles = try await cancelStores.profileStore.snapshot()
        let cancelledPending = try await cancelStores.pendingStore.load(
            desktopID: invitation.desktopID
        )
        let cancellationStoppedDiscovery = holdingCancelCount == 1
            && cancelSnapshot.phase == .off

        guard corruptProfileRecoveredFromKeychain,
              expiredGrantNotPaired,
              pendingRemoveFailureReconciled,
              promotionFailureRecovered,
              pendingTransferBlocksCredentialReset,
              startupFailureResetFailedClosed else {
            throw LiveAuthContractError.invalid(
                "Durable grant reconciliation did not preserve a manageable pairing state."
            )
        }

        return [
            "application_support_root": paths.root.path.hasSuffix(
                "/CaptureSplat/live-sender/v0.1"
            ) || paths.root.path.hasSuffix("/state/v0.1"),
            "bonjour_exact_match": exactBonjourMatch,
            "bonjour_wrong_service_ignored": wrongBonjourIgnored,
            "background_cleanup_durable": backgroundCancelled
                && cancelledProfiles.pending == nil
                && cancelledPending == nil,
            "cancelled_durable_grant_surfaced": cancelledDurableGrantSurfaced,
            "cancellation_stops_discovery": cancellationStoppedDiscovery,
            "corrupt_profile_recovered_from_keychain": corruptProfileRecoveredFromKeychain,
            "corruption_failed_closed": corruptionFailedClosed,
            "expired_cancel_grant_rejected": expiredGrantNotPaired,
            "interrupted_after_lost_response": interrupted
                && firstSnapshot.canRetry
                && firstResolveCount == 1,
            "local_forget_durable": localForgetDurable,
            "multibyte_device_name_bounded": boundedDeviceName?.utf8.count == 80,
            "no_discovery_before_opt_in": noDiscoveryOnRestore
                && noDiscoveryWhileScanning
                && noDiscoveryBeforeRestore,
            "no_secret_in_profile": noSecretInProfile,
            "paired_after_retry": paired,
            "paired_restored_without_network": pairedRestoredWithoutNetwork,
            "pending_restored_without_network": restoredPending
                && noDiscoveryOnPendingRestore,
            "pending_transfer_blocks_credential_reset":
                pendingTransferBlocksCredentialReset,
            "pending_transfer_blocks_forget": pendingTransferBlocksForget,
            "pending_symlink_blocks_pairing_clear":
                pendingSymlinkBlocksPairingClear,
            "queue_path_confined": queueURL.path.hasPrefix(paths.queuesRoot.path),
            "second_pairing_blocked": stillSinglePairing,
            "startup_failure_reset_failed_closed":
                startupFailureResetFailedClosed,
            "traversal_rejected": traversalRejected,
        ]
    }

    private static func stores(
        paths: LiveApplicationSupportPaths,
        secureStore: PairingMemorySecureStore,
        randomStart: UInt8
    ) -> (
        profileStore: LivePairingProfileStore,
        recoveryStore: LivePairingRecoveryStore,
        grantStore: LiveGrantStore,
        pendingStore: LivePendingPairingStore,
        client: LivePairingClient
    ) {
        let grantStore = LiveGrantStore(secureStore: secureStore)
        let pendingStore = LivePendingPairingStore(secureStore: secureStore)
        let client = LivePairingClient(
            identityStore: LiveDeviceIdentityStore(
                secureStore: secureStore,
                random: PairingDeterministicRandom(start: randomStart)
            ),
            grantStore: grantStore,
            pendingStore: pendingStore,
            counterStore: LiveRequestCounterStore(
                stateURL: paths.requestCountersURL
            ),
            random: PairingDeterministicRandom(start: randomStart &+ 16)
        )
        return (
            LivePairingProfileStore(stateURL: paths.pairingProfileURL),
            LivePairingRecoveryStore(secureStore: secureStore),
            grantStore,
            pendingStore,
            client
        )
    }

    @MainActor
    private static func wait(
        coordinator: LivePairingCoordinator,
        for phase: LivePairingPhase
    ) async -> Bool {
        for _ in 0..<2_000 {
            if coordinator.snapshot.phase == phase {
                return true
            }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        return false
    }
}

private extension NSLock {
    func withLock<T>(_ operation: () -> T) -> T {
        lock()
        defer { unlock() }
        return operation()
    }
}

import Combine
import Foundation

protocol LivePairingServicing: Sendable {
    func preparePairing(
        invitationURI: String,
        endpoint: LiveResolvedEndpoint,
        deviceName: String,
        appVersion: String,
        now: Date
    ) async throws -> LivePreparedPairing
    func resumePairing(desktopID: String) async throws -> LivePreparedPairing?
    func submitPairing(_ prepared: LivePreparedPairing) async throws -> LiveStoredGrant
}

extension LivePairingClient: LivePairingServicing {
    func preparePairing(
        invitationURI: String,
        endpoint: LiveResolvedEndpoint,
        deviceName: String,
        appVersion: String,
        now: Date
    ) async throws -> LivePreparedPairing {
        try await prepare(
            invitationURI: invitationURI,
            endpoint: endpoint,
            deviceName: deviceName,
            appVersion: appVersion,
            now: now
        )
    }

    func resumePairing(desktopID: String) async throws -> LivePreparedPairing? {
        try await resumePending(desktopID: desktopID)
    }

    func submitPairing(_ prepared: LivePreparedPairing) async throws -> LiveStoredGrant {
        try await submit(prepared)
    }
}

enum LivePairingPhase: String, Equatable, Sendable {
    case off
    case scanning
    case resolving
    case awaitingApproval = "awaiting_approval"
    case cancelling
    case paired
    case interrupted
    case failed
}

struct LivePairingSnapshot: Equatable, Sendable {
    let phase: LivePairingPhase
    let desktopID: String?
    let desktopName: String?
    let grantExpiresAt: String?
    let message: String
    let canRetry: Bool
    let hasCurrentPairing: Bool

    static let off = LivePairingSnapshot(
        phase: .off,
        desktopID: nil,
        desktopName: nil,
        grantExpiresAt: nil,
        message: "Live transfer is off.",
        canRetry: false,
        hasCurrentPairing: false
    )
}

private enum LivePairingRecoveryState {
    case restoring
    case ready
    case blocked
}

@MainActor
final class LivePairingCoordinator: ObservableObject {
    typealias ResolverFactory = @MainActor () -> any LiveBonjourResolving
    typealias Clock = @Sendable () -> Date

    @Published private(set) var snapshot: LivePairingSnapshot

    private let profileStore: LivePairingProfileStore?
    private let recoveryStore: LivePairingRecoveryStore?
    private let grantStore: LiveGrantStore?
    private let pendingStore: LivePendingPairingStore?
    private let pairingService: (any LivePairingServicing)?
    private let resolverFactory: ResolverFactory
    private let deviceName: @Sendable () -> String
    private let appVersion: @Sendable () -> String
    private let clock: Clock
    private let startupError: String?

    private var currentProfile: LivePairingProfile?
    private var currentGrant: LiveStoredGrant?
    private var pendingProfile: LivePairingProfile?
    private var retryPrepared: LivePreparedPairing?
    private var resolver: (any LiveBonjourResolving)?
    private var pairingTask: Task<Void, Never>?
    private var cleanupTask: Task<Void, Never>?
    private var operationID: UUID?
    private var restored = false
    private var recoveryState = LivePairingRecoveryState.restoring

    static func application(
        deviceName: @escaping @Sendable () -> String,
        appVersion: @escaping @Sendable () -> String
    ) -> LivePairingCoordinator {
        let secureStore = KeychainLiveSecureValueStore()
        let recoveryStore = LivePairingRecoveryStore(secureStore: secureStore)
        let grantStore = LiveGrantStore(secureStore: secureStore)
        let pendingStore = LivePendingPairingStore(secureStore: secureStore)
        do {
            let paths = try LiveApplicationSupportPaths.application()
            let client = LivePairingClient(
                identityStore: LiveDeviceIdentityStore(secureStore: secureStore),
                grantStore: grantStore,
                pendingStore: pendingStore,
                counterStore: LiveRequestCounterStore(
                    stateURL: paths.requestCountersURL
                )
            )
            return LivePairingCoordinator(
                profileStore: LivePairingProfileStore(
                    stateURL: paths.pairingProfileURL
                ),
                recoveryStore: recoveryStore,
                grantStore: grantStore,
                pendingStore: pendingStore,
                pairingService: client,
                resolverFactory: { LiveBonjourResolver() },
                deviceName: deviceName,
                appVersion: appVersion
            )
        } catch {
            return LivePairingCoordinator(
                startupError: Self.message(for: error),
                recoveryStore: recoveryStore,
                grantStore: grantStore,
                pendingStore: pendingStore,
                deviceName: deviceName,
                appVersion: appVersion
            )
        }
    }

    #if CAPTURE_SPLAT_LIVE_TESTING
    static func startupFailureForTesting(
        message: String,
        recoveryStore: LivePairingRecoveryStore,
        grantStore: LiveGrantStore,
        pendingStore: LivePendingPairingStore
    ) -> LivePairingCoordinator {
        LivePairingCoordinator(
            startupError: message,
            recoveryStore: recoveryStore,
            grantStore: grantStore,
            pendingStore: pendingStore,
            deviceName: { "Capture Splat Test" },
            appVersion: { "0.1.0" }
        )
    }
    #endif

    init(
        profileStore: LivePairingProfileStore,
        recoveryStore: LivePairingRecoveryStore,
        grantStore: LiveGrantStore,
        pendingStore: LivePendingPairingStore,
        pairingService: any LivePairingServicing,
        resolverFactory: @escaping ResolverFactory,
        deviceName: @escaping @Sendable () -> String,
        appVersion: @escaping @Sendable () -> String,
        clock: @escaping Clock = { Date() }
    ) {
        self.profileStore = profileStore
        self.recoveryStore = recoveryStore
        self.grantStore = grantStore
        self.pendingStore = pendingStore
        self.pairingService = pairingService
        self.resolverFactory = resolverFactory
        self.deviceName = deviceName
        self.appVersion = appVersion
        self.clock = clock
        startupError = nil
        snapshot = .off
    }

    private init(
        startupError: String,
        recoveryStore: LivePairingRecoveryStore,
        grantStore: LiveGrantStore,
        pendingStore: LivePendingPairingStore,
        deviceName: @escaping @Sendable () -> String,
        appVersion: @escaping @Sendable () -> String
    ) {
        profileStore = nil
        self.recoveryStore = recoveryStore
        self.grantStore = grantStore
        self.pendingStore = pendingStore
        pairingService = nil
        resolverFactory = { LiveBonjourResolver() }
        self.deviceName = deviceName
        self.appVersion = appVersion
        clock = { Date() }
        self.startupError = startupError
        snapshot = LivePairingSnapshot(
            phase: .failed,
            desktopID: nil,
            desktopName: nil,
            grantExpiresAt: nil,
            message: startupError,
            canRetry: false,
            hasCurrentPairing: false
        )
    }

    func restore() async {
        guard !restored else { return }
        restored = true
        guard let recoveryStore,
              let grantStore else {
            recoveryState = .blocked
            return
        }
        guard startupError == nil,
              let profileStore,
              let pairingService else {
            do {
                let profile = try await recoveryStore.load()
                pendingProfile = profile
                if let profile {
                    currentGrant = try await grantStore.load(
                        desktopID: profile.desktopID,
                        currentAt: clock()
                    )
                }
                recoveryState = .blocked
                setSnapshot(
                    phase: .failed,
                    profile: profile,
                    message: "Application Support is unavailable. Keychain pairing "
                        + "state remains visible and can be cleared: "
                        + (startupError ?? "local profile store is unavailable."),
                    canRetry: false
                )
            } catch {
                recoveryState = .blocked
                setSnapshot(
                    phase: .failed,
                    profile: pendingProfile,
                    message: "Keychain pairing recovery is blocked: "
                        + Self.message(for: error),
                    canRetry: false
                )
            }
            return
        }
        do {
            var recoveryProfile = try await recoveryStore.load()
            if let recoveryProfile {
                pendingProfile = recoveryProfile
            }

            let stored: LivePairingProfileSnapshot?
            do {
                stored = try await profileStore.snapshot()
            } catch {
                guard recoveryProfile != nil else {
                    throw error
                }
                stored = nil
            }

            if let stored {
                let storedProfiles = [stored.current, stored.pending].compactMap { $0 }
                if let recoveryProfile {
                    guard storedProfiles.allSatisfy({
                        $0.desktopID == recoveryProfile.desktopID
                    }) else {
                        throw LiveAuthContractError.invalid(
                            "Pairing profile identity does not match Keychain recovery state."
                        )
                    }
                } else if let legacyProfile = stored.current ?? stored.pending {
                    try await recoveryStore.claim(legacyProfile)
                    recoveryProfile = legacyProfile
                    pendingProfile = legacyProfile
                }
            }

            guard let recoveryProfile else {
                currentProfile = nil
                currentGrant = nil
                pendingProfile = nil
                retryPrepared = nil
                recoveryState = .ready
                snapshot = .off
                return
            }

            try await reconcileRecoveredProfile(
                recoveryProfile,
                profileStore: profileStore,
                grantStore: grantStore,
                pairingService: pairingService
            )
        } catch {
            recoveryState = .blocked
            setSnapshot(
                phase: .failed,
                profile: currentProfile ?? pendingProfile,
                message: "Local pairing recovery is blocked: \(Self.message(for: error))",
                canRetry: false
            )
        }
    }

    private func reconcileRecoveredProfile(
        _ profile: LivePairingProfile,
        profileStore: LivePairingProfileStore,
        grantStore: LiveGrantStore,
        pairingService: any LivePairingServicing
    ) async throws {
        pendingProfile = profile
        if let grant = try await grantStore.load(
            desktopID: profile.desktopID,
            currentAt: clock()
        ) {
            try await profileStore.replace(current: profile, pending: nil)
            currentProfile = profile
            currentGrant = grant
            pendingProfile = nil
            retryPrepared = nil
            setPaired(profile: profile, grant: grant)
            return
        }

        if let prepared = try await pairingService.resumePairing(
            desktopID: profile.desktopID
        ) {
            try await profileStore.replace(current: nil, pending: profile)
            currentProfile = nil
            currentGrant = nil
            retryPrepared = prepared
            recoveryState = .ready
            setSnapshot(
                phase: .interrupted,
                profile: profile,
                message: "Pairing was interrupted before World Studio approval.",
                canRetry: true
            )
            return
        }

        recoveryState = .blocked
        currentProfile = nil
        currentGrant = nil
        retryPrepared = nil
        setSnapshot(
            phase: .failed,
            profile: profile,
            message: "Keychain still reserves this Mac, but its grant and pending "
                + "request are missing. Clear local pairing state before continuing.",
            canRetry: false
        )
    }

    func startScanning() {
        guard canStartNewPairing else {
            return
        }
        setSnapshot(
            phase: .scanning,
            profile: currentProfile,
            message: "Scan the short-lived World Studio pairing QR.",
            canRetry: false
        )
    }

    func stopScanning() {
        guard snapshot.phase == .scanning else { return }
        restoreRestingSnapshot()
    }

    func beginPairing(invitationURI: String) {
        guard startupError == nil,
              canStartNewPairing,
              let profileStore,
              recoveryStore != nil,
              let pairingService else {
            return
        }
        do {
            let now = clock()
            let invitation = try LivePairingClient.decodeInvitationURI(
                invitationURI,
                freshAt: now
            )
            let profile = try LivePairingProfile(
                desktopID: invitation.desktopID,
                desktopName: invitation.desktopName
            )
            let operationID = UUID()
            self.operationID = operationID
            pendingProfile = profile
            setSnapshot(
                phase: .resolving,
                profile: profile,
                message: "Finding the exact World Studio service from the QR.",
                canRetry: false
            )
            pairingTask = Task { @MainActor [weak self] in
                guard let self else { return }
                await runPairing(
                    invitationURI: invitationURI,
                    invitation: invitation,
                    profile: profile,
                    profileStore: profileStore,
                    pairingService: pairingService,
                    operationID: operationID
                )
            }
        } catch {
            setSnapshot(
                phase: .failed,
                profile: currentProfile,
                message: Self.message(for: error),
                canRetry: false
            )
        }
    }

    func retry() {
        guard recoveryState == .ready,
              !isBusy,
              let prepared = retryPrepared,
              let profile = pendingProfile,
              let profileStore,
              let pairingService else {
            return
        }
        let operationID = UUID()
        self.operationID = operationID
        setSnapshot(
            phase: .awaitingApproval,
            profile: profile,
            message: "Waiting for approval in World Studio.",
            canRetry: false
        )
        pairingTask = Task { @MainActor [weak self] in
            guard let self else { return }
            await submit(
                prepared: prepared,
                profile: profile,
                profileStore: profileStore,
                pairingService: pairingService,
                operationID: operationID
            )
        }
    }

    var canStartNewPairing: Bool {
        restored
            && recoveryState == .ready
            && !isBusy
            && currentProfile == nil
            && currentGrant == nil
            && pendingProfile == nil
    }

    var canResetAllCredentials: Bool {
        restored
            && recoveryState == .blocked
            && recoveryStore != nil
    }

    func cancel() {
        let pending = pendingProfile
        let activeTask = pairingTask
        guard cleanupTask == nil else { return }
        operationID = nil
        activeTask?.cancel()
        resolver?.cancel()
        resolver = nil

        guard let pending,
              let profileStore,
              let recoveryStore,
              let grantStore,
              let pendingStore else {
            pairingTask = nil
            retryPrepared = nil
            pendingProfile = nil
            restoreRestingSnapshot()
            return
        }

        recoveryState = .blocked
        setSnapshot(
            phase: .cancelling,
            profile: pending,
            message: "Cancelling pairing and reconciling durable grant state.",
            canRetry: false
        )
        cleanupTask = Task { @MainActor [weak self] in
            await activeTask?.value
            guard let self else { return }
            do {
                let reserved = try await recoveryStore.load()
                let grant = try await grantStore.load(
                    desktopID: pending.desktopID,
                    currentAt: clock()
                )
                if let grant {
                    guard reserved?.desktopID == pending.desktopID else {
                        throw LiveAuthContractError.invalid(
                            "Pairing grant has no matching recovery pointer."
                        )
                    }
                    try await profileStore.replace(
                        current: pending,
                        pending: nil
                    )
                    currentProfile = pending
                    currentGrant = grant
                    pendingProfile = nil
                    retryPrepared = nil
                    setPaired(profile: pending, grant: grant)
                } else {
                    if let reserved {
                        guard reserved.desktopID == pending.desktopID else {
                            throw LiveAuthContractError.invalid(
                                "Pairing recovery pointer belongs to another Mac."
                            )
                        }
                    }
                    try await pendingStore.remove(desktopID: pending.desktopID)
                    try await profileStore.reset()
                    try await recoveryStore.remove(desktopID: pending.desktopID)
                    recoveryState = .ready
                    currentProfile = nil
                    currentGrant = nil
                    pendingProfile = nil
                    retryPrepared = nil
                    snapshot = .off
                }
            } catch {
                recoveryState = .blocked
                pendingProfile = pending
                setSnapshot(
                    phase: .failed,
                    profile: pending,
                    message: "Local pairing cancellation is blocked: "
                        + Self.message(for: error),
                    canRetry: false
                )
            }
            pairingTask = nil
            resolver = nil
            cleanupTask = nil
        }
    }

    func handleBackgrounding() {
        if snapshot.phase == .scanning || isBusy {
            cancel()
        }
    }

    func clearLocalPairing() async {
        if isBusy {
            cancel()
        }
        await cleanupTask?.value
        let visibleProfile = currentProfile ?? pendingProfile
        guard let recoveryStore,
              let grantStore,
              let pendingStore else {
            return
        }
        do {
            guard let profile = try await recoveryStore.load() ?? visibleProfile else {
                throw LiveAuthContractError.invalid(
                    "No recoverable pairing identity is available."
                )
            }
            if currentProfile == nil, pendingProfile == nil {
                pendingProfile = profile
            }
            try await grantStore.remove(desktopID: profile.desktopID)
            currentGrant = nil
            try await pendingStore.remove(desktopID: profile.desktopID)
            try await profileStore?.reset()
            try await recoveryStore.remove(desktopID: profile.desktopID)
            currentProfile = nil
            currentGrant = nil
            pendingProfile = nil
            retryPrepared = nil
            if startupError == nil, profileStore != nil {
                recoveryState = .ready
                snapshot = .off
            } else {
                recoveryState = .blocked
                setSnapshot(
                    phase: .failed,
                    profile: nil,
                    message: "Keychain pairing state was cleared. Restart Capture "
                        + "Splat after restoring Application Support.",
                    canRetry: false
                )
            }
        } catch {
            recoveryState = .blocked
            setSnapshot(
                phase: .failed,
                profile: currentProfile ?? pendingProfile ?? visibleProfile,
                message: "Local pairing clear is blocked: \(Self.message(for: error))",
                canRetry: false
            )
        }
    }

    func resetAllLocalCredentials() async {
        await cleanupTask?.value
        guard canResetAllCredentials,
              let recoveryStore else {
            return
        }
        do {
            try await recoveryStore.removeAllCredentials()
            currentProfile = nil
            currentGrant = nil
            pendingProfile = nil
            retryPrepared = nil
            try await profileStore?.reset()
            recoveryState = .blocked
            setSnapshot(
                phase: .failed,
                profile: nil,
                message: "All local live credentials were removed. Restart Capture "
                    + "Splat before pairing again, and revoke the old device in "
                    + "World Studio.",
                canRetry: false
            )
        } catch {
            recoveryState = .blocked
            setSnapshot(
                phase: .failed,
                profile: nil,
                message: "Local credential reset is blocked: \(Self.message(for: error))",
                canRetry: false
            )
        }
    }

    private var isBusy: Bool {
        snapshot.phase == .resolving
            || snapshot.phase == .awaitingApproval
            || snapshot.phase == .cancelling
    }

    private func runPairing(
        invitationURI: String,
        invitation: LivePairingInvitation,
        profile: LivePairingProfile,
        profileStore: LivePairingProfileStore,
        pairingService: any LivePairingServicing,
        operationID: UUID
    ) async {
        var prepared: LivePreparedPairing?
        do {
            guard self.operationID == operationID, !Task.isCancelled else {
                throw CancellationError()
            }
            guard let recoveryStore else {
                throw LiveAuthContractError.invalid(
                    "Pairing recovery store is unavailable."
                )
            }
            try await recoveryStore.claim(profile)
            try await profileStore.replace(current: nil, pending: profile)
            pendingProfile = profile
            guard self.operationID == operationID, !Task.isCancelled else {
                throw CancellationError()
            }

            let remaining = try LiveAuthTime.parse(
                invitation.expiresAt
            ).timeIntervalSince(clock())
            guard remaining > 0 else {
                throw LiveAuthContractError.invalid("The pairing QR has expired.")
            }
            let resolver = resolverFactory()
            self.resolver = resolver
            let endpoint = try await resolver.resolve(
                discovery: invitation.discovery,
                timeout: min(remaining, 30)
            )
            guard self.operationID == operationID, !Task.isCancelled else {
                throw CancellationError()
            }
            try invitation.validate(freshAt: clock())
            try endpoint.validate(against: invitation)
            let built = try await pairingService.preparePairing(
                invitationURI: invitationURI,
                endpoint: endpoint,
                deviceName: normalizedDeviceName(),
                appVersion: normalizedAppVersion(),
                now: clock()
            )
            prepared = built
            retryPrepared = built
            setSnapshot(
                phase: .awaitingApproval,
                profile: profile,
                message: "Waiting for approval in World Studio.",
                canRetry: false
            )
            await submit(
                prepared: built,
                profile: profile,
                profileStore: profileStore,
                pairingService: pairingService,
                operationID: operationID
            )
            return
        } catch is CancellationError {
            return
        } catch {
            await handlePairingFailure(
                error,
                prepared: prepared,
                profile: profile,
                profileStore: profileStore,
                operationID: operationID
            )
        }
        finishOperation(operationID)
    }

    private func submit(
        prepared: LivePreparedPairing,
        profile: LivePairingProfile,
        profileStore: LivePairingProfileStore,
        pairingService: any LivePairingServicing,
        operationID: UUID
    ) async {
        do {
            guard let recoveryStore,
                  let reserved = try await recoveryStore.load(),
                  reserved == profile else {
                throw LiveAuthContractError.invalid(
                    "Pairing recovery pointer changed before submission."
                )
            }
            let grant = try await pairingService.submitPairing(prepared)
            guard let confirmed = try await recoveryStore.load(),
                  confirmed == profile else {
                throw LiveAuthContractError.invalid(
                    "Pairing recovery pointer changed before promotion."
                )
            }
            try await profileStore.replace(current: profile, pending: nil)
            currentProfile = profile
            currentGrant = grant
            pendingProfile = nil
            retryPrepared = nil
            setPaired(profile: profile, grant: grant)
        } catch is CancellationError {
            return
        } catch {
            await handlePairingFailure(
                error,
                prepared: prepared,
                profile: profile,
                profileStore: profileStore,
                operationID: operationID
            )
        }
        finishOperation(operationID)
    }

    private func handlePairingFailure(
        _ error: Error,
        prepared: LivePreparedPairing?,
        profile: LivePairingProfile,
        profileStore: LivePairingProfileStore,
        operationID: UUID
    ) async {
        guard self.operationID == operationID else { return }
        guard let recoveryStore,
              let grantStore,
              let pendingStore else {
            recoveryState = .blocked
            pendingProfile = profile
            setSnapshot(
                phase: .failed,
                profile: profile,
                message: "Pairing recovery services are unavailable.",
                canRetry: false
            )
            return
        }

        do {
            guard let reserved = try await recoveryStore.load() else {
                throw LiveAuthContractError.invalid(
                    "Pairing recovery pointer is missing."
                )
            }
            guard reserved.desktopID == profile.desktopID else {
                pendingProfile = reserved
                throw LiveAuthContractError.invalid(
                    "Pairing recovery pointer belongs to another Mac."
                )
            }

            if let grant = try await grantStore.load(
                desktopID: profile.desktopID,
                currentAt: clock()
            ) {
                do {
                    try await profileStore.replace(
                        current: profile,
                        pending: nil
                    )
                    currentProfile = profile
                    currentGrant = grant
                    pendingProfile = nil
                    retryPrepared = nil
                    setPaired(profile: profile, grant: grant)
                } catch {
                    recoveryState = .blocked
                    currentGrant = grant
                    pendingProfile = profile
                    retryPrepared = nil
                    setSnapshot(
                        phase: .failed,
                        profile: profile,
                        message: "The pairing grant is secure, but its local profile "
                            + "could not be persisted. Restore storage and reopen the "
                            + "app, or clear this Mac.",
                        canRetry: false
                    )
                }
                return
            }

            let retryable = (error as? LiveAuthenticatedRequestError)?.retryable == true
            if retryable, let prepared {
                recoveryState = .ready
                pendingProfile = profile
                retryPrepared = prepared
                setSnapshot(
                    phase: .interrupted,
                    profile: profile,
                    message: Self.message(for: error),
                    canRetry: true
                )
                return
            }

            try await pendingStore.remove(desktopID: profile.desktopID)
            try await profileStore.reset()
            try await recoveryStore.remove(desktopID: profile.desktopID)
            recoveryState = .ready
            currentProfile = nil
            currentGrant = nil
            pendingProfile = nil
            retryPrepared = nil
            setSnapshot(
                phase: .failed,
                profile: nil,
                message: Self.message(for: error),
                canRetry: false
            )
        } catch {
            recoveryState = .blocked
            retryPrepared = nil
            setSnapshot(
                phase: .failed,
                profile: pendingProfile ?? profile,
                message: "Local pairing recovery is blocked: \(Self.message(for: error))",
                canRetry: false
            )
        }
    }

    private func finishOperation(_ id: UUID) {
        guard operationID == id else { return }
        operationID = nil
        pairingTask = nil
        resolver = nil
    }

    private func restoreRestingSnapshot() {
        if let currentProfile, let currentGrant {
            setPaired(profile: currentProfile, grant: currentGrant)
        } else {
            recoveryState = .ready
            snapshot = .off
        }
    }

    private func setPaired(
        profile: LivePairingProfile,
        grant: LiveStoredGrant
    ) {
        recoveryState = .ready
        snapshot = LivePairingSnapshot(
            phase: .paired,
            desktopID: profile.desktopID,
            desktopName: profile.desktopName,
            grantExpiresAt: grant.payload.expiresAt,
            message: "Paired. Recording transfer is not connected yet.",
            canRetry: false,
            hasCurrentPairing: true
        )
    }

    private func setSnapshot(
        phase: LivePairingPhase,
        profile: LivePairingProfile?,
        message: String,
        canRetry: Bool
    ) {
        snapshot = LivePairingSnapshot(
            phase: phase,
            desktopID: profile?.desktopID,
            desktopName: profile?.desktopName,
            grantExpiresAt: currentGrant?.payload.expiresAt,
            message: message,
            canRetry: canRetry,
            hasCurrentPairing: currentProfile != nil && currentGrant != nil
        )
    }

    private func normalizedDeviceName() -> String {
        let value = deviceName().trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return "Capture Splat iPhone" }
        var result = ""
        var byteCount = 0
        for character in value {
            let next = String(character)
            guard byteCount + next.utf8.count <= 80 else { break }
            result.append(character)
            byteCount += next.utf8.count
        }
        return result.isEmpty ? "Capture Splat iPhone" : result
    }

    private func normalizedAppVersion() -> String {
        let value = appVersion().trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? "0.1.0" : String(value.prefix(64))
    }

    private static func message(for error: Error) -> String {
        if let localized = error as? LocalizedError,
           let description = localized.errorDescription,
           !description.isEmpty {
            return description
        }
        return "Live pairing failed closed. Scan a new World Studio QR."
    }
}

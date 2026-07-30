import Foundation

struct LiveApplicationSupportPaths: Sendable {
    let root: URL

    var pairingProfileURL: URL {
        root.appendingPathComponent("pairing-profile.json", isDirectory: false)
    }

    var requestCountersURL: URL {
        root.appendingPathComponent("request-counters.json", isDirectory: false)
    }

    var queuesRoot: URL {
        root.appendingPathComponent("queues", isDirectory: true)
    }

    static func application(
        fileManager: FileManager = .default
    ) throws -> LiveApplicationSupportPaths {
        guard let base = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first else {
            throw LiveAuthContractError.invalid("Application Support is unavailable.")
        }
        return try LiveApplicationSupportPaths(
            root: base
                .appendingPathComponent("CaptureSplat", isDirectory: true)
                .appendingPathComponent("live-sender", isDirectory: true)
                .appendingPathComponent("v0.1", isDirectory: true),
            fileManager: fileManager
        )
    }

    init(root: URL, fileManager: FileManager = .default) throws {
        let standardized = root.standardizedFileURL
        guard standardized.isFileURL,
              standardized.path.hasPrefix("/"),
              standardized.lastPathComponent == "v0.1" else {
            throw LiveAuthContractError.invalid("Live Application Support root is invalid.")
        }
        try fileManager.createDirectory(
            at: standardized,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try fileManager.createDirectory(
            at: standardized.appendingPathComponent("queues", isDirectory: true),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        self.root = standardized
    }

    func queueStateURL(desktopID: String, sessionID: String) throws -> URL {
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        guard !sessionID.isEmpty,
              sessionID.count <= 128,
              sessionID.first?.isASCIIAlphaNumeric == true,
              sessionID.allSatisfy({
                  $0.isASCIIAlphaNumeric || "._-".contains($0)
              }) else {
            throw LiveAuthContractError.invalid("Live queue session ID is invalid.")
        }
        return queuesRoot
            .appendingPathComponent(desktopID, isDirectory: true)
            .appendingPathComponent("\(sessionID).json", isDirectory: false)
    }
}

struct LivePairingProfile: Codable, Equatable, Sendable {
    let schema: String
    let desktopID: String
    let desktopName: String

    enum CodingKeys: String, CodingKey {
        case schema
        case desktopID = "desktop_id"
        case desktopName = "desktop_name"
    }

    init(desktopID: String, desktopName: String) throws {
        schema = "capture_splat.live_pairing_profile.v0.1"
        self.desktopID = desktopID
        self.desktopName = desktopName
        try validate()
    }

    func validate() throws {
        guard schema == "capture_splat.live_pairing_profile.v0.1" else {
            throw LiveAuthContractError.invalid("Pairing profile schema is invalid.")
        }
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        try LiveAuthValidation.utf8String(
            desktopName,
            maximumBytes: 80,
            field: "desktop_name"
        )
    }
}

struct LivePairingProfileSnapshot: Equatable, Sendable {
    let current: LivePairingProfile?
    let pending: LivePairingProfile?
}

actor LivePairingRecoveryStore {
    private let secureStore: any LiveSecureValueStore
    private let account = "pairing-profile-pointer"

    init(secureStore: any LiveSecureValueStore = KeychainLiveSecureValueStore()) {
        self.secureStore = secureStore
    }

    func load() throws -> LivePairingProfile? {
        guard let data = try secureStore.read(account: account) else {
            return nil
        }
        let profile = try LiveStrictJSON.decodeCanonical(
            LivePairingProfile.self,
            from: data
        )
        try profile.validate()
        return profile
    }

    func claim(_ profile: LivePairingProfile) throws {
        try profile.validate()
        if let existing = try load() {
            guard existing == profile else {
                throw LiveAuthContractError.invalid(
                    "Another World Studio Mac is already reserved."
                )
            }
            return
        }
        try secureStore.write(
            LiveStrictJSON.canonicalData(profile),
            account: account
        )
    }

    func remove(desktopID: String) throws {
        if let existing = try load() {
            guard existing.desktopID == desktopID else {
                throw LiveAuthContractError.invalid(
                    "Pairing recovery pointer belongs to another Mac."
                )
            }
        }
        try secureStore.remove(account: account)
    }

    func removeAllCredentials() throws {
        try secureStore.removeAll()
    }
}

actor LivePairingProfileStore {
    private struct State: Codable, Equatable {
        let schema: String
        var current: LivePairingProfile?
        var pending: LivePairingProfile?
    }

    private struct Envelope: Codable, Equatable {
        let schema: String
        let payloadBase64URL: String
        let payloadSHA256: String

        enum CodingKeys: String, CodingKey {
            case schema
            case payloadBase64URL = "payload_b64u"
            case payloadSHA256 = "payload_sha256"
        }
    }

    private let stateURL: URL
    private var loaded: State?

    init(stateURL: URL) {
        self.stateURL = stateURL
    }

    func snapshot() throws -> LivePairingProfileSnapshot {
        let state = try load()
        return LivePairingProfileSnapshot(
            current: state.current,
            pending: state.pending
        )
    }

    func setPending(_ profile: LivePairingProfile) throws {
        try profile.validate()
        var state = try load()
        state.pending = profile
        try persist(state)
    }

    func promote(_ profile: LivePairingProfile) throws {
        try profile.validate()
        var state = try load()
        state.current = profile
        state.pending = nil
        try persist(state)
    }

    func clearPending(desktopID: String? = nil) throws {
        var state = try load()
        if let desktopID, state.pending?.desktopID != desktopID {
            return
        }
        guard state.pending != nil else { return }
        state.pending = nil
        try persist(state)
    }

    func clearCurrent(desktopID: String? = nil) throws {
        var state = try load()
        if let desktopID, state.current?.desktopID != desktopID {
            return
        }
        guard state.current != nil else { return }
        state.current = nil
        try persist(state)
    }

    func replace(
        current: LivePairingProfile?,
        pending: LivePairingProfile?
    ) throws {
        try persist(State(
            schema: "capture_splat.live_pairing_profiles.v0.1",
            current: current,
            pending: pending
        ))
    }

    func reset() throws {
        try replace(current: nil, pending: nil)
    }

    private func load() throws -> State {
        if let loaded { return loaded }
        guard FileManager.default.fileExists(atPath: stateURL.path) else {
            let state = State(
                schema: "capture_splat.live_pairing_profiles.v0.1",
                current: nil,
                pending: nil
            )
            loaded = state
            return state
        }
        let bytes = try Data(contentsOf: stateURL, options: .mappedIfSafe)
        let envelope = try LiveStrictJSON.decodeCanonical(
            Envelope.self,
            from: bytes
        )
        guard envelope.schema == "capture_splat.live_pairing_profile_envelope.v0.1" else {
            throw LiveAuthContractError.invalid("Pairing profile envelope is invalid.")
        }
        try LiveAuthValidation.sha256(envelope.payloadSHA256)
        let payload = try LiveAuthEncoding.decodeBase64URL(
            envelope.payloadBase64URL,
            field: "pairing profile payload"
        )
        guard payload.count <= 16 * 1024,
              LiveAuthEncoding.sha256(payload) == envelope.payloadSHA256 else {
            throw LiveAuthContractError.invalid("Pairing profile checksum is invalid.")
        }
        let state = try LiveStrictJSON.decodeCanonical(State.self, from: payload)
        try validate(state)
        loaded = state
        return state
    }

    private func persist(_ state: State) throws {
        try validate(state)
        let payload = try LiveStrictJSON.canonicalData(state)
        guard payload.count <= 16 * 1024 else {
            throw LiveAuthContractError.invalid("Pairing profile state is oversized.")
        }
        let envelope = Envelope(
            schema: "capture_splat.live_pairing_profile_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(payload),
            payloadSHA256: LiveAuthEncoding.sha256(payload)
        )
        try LiveAtomicFile.write(
            LiveStrictJSON.canonicalData(envelope),
            to: stateURL
        )
        loaded = state
    }

    private func validate(_ state: State) throws {
        guard state.schema == "capture_splat.live_pairing_profiles.v0.1" else {
            throw LiveAuthContractError.invalid("Pairing profile state is invalid.")
        }
        try state.current?.validate()
        try state.pending?.validate()
        guard state.current == nil || state.pending == nil else {
            throw LiveAuthContractError.invalid(
                "Pairing profile cannot be current and pending simultaneously."
            )
        }
    }
}

private extension Character {
    var isASCIIAlphaNumeric: Bool {
        unicodeScalars.count == 1
            && unicodeScalars.first.map {
                (48...57).contains($0.value)
                    || (65...90).contains($0.value)
                    || (97...122).contains($0.value)
            } == true
    }
}

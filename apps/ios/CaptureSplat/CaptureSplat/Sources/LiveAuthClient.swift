import CryptoKit
import Darwin
import Foundation
import Security

protocol LiveRandomSource: Sendable {
    func bytes(count: Int) throws -> Data
}

struct SystemLiveRandomSource: LiveRandomSource {
    func bytes(count: Int) throws -> Data {
        guard count > 0 else {
            throw LiveAuthContractError.invalid("Random byte count must be positive.")
        }
        var data = Data(count: count)
        let status = data.withUnsafeMutableBytes { buffer in
            SecRandomCopyBytes(kSecRandomDefault, count, buffer.baseAddress!)
        }
        guard status == errSecSuccess else {
            throw LiveAuthContractError.invalid("Secure random generation failed.")
        }
        return data
    }
}

protocol LiveSecureValueStore: Sendable {
    func read(account: String) throws -> Data?
    func write(_ data: Data, account: String) throws
    func remove(account: String) throws
    func removeAll() throws
}

extension LiveSecureValueStore {
    func removeAll() throws {
        throw LiveAuthContractError.invalid("Secure store reset is unavailable.")
    }
}

final class KeychainLiveSecureValueStore: LiveSecureValueStore, @unchecked Sendable {
    private let service: String

    init(service: String = "com.capturesplat.live-sender.v0.1") {
        self.service = service
    }

    func read(account: String) throws -> Data? {
        var query = baseQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw LiveAuthContractError.invalid("Keychain value could not be read.")
        }
        return data
    }

    func write(_ data: Data, account: String) throws {
        let query = baseQuery(account: account)
        let updateStatus = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        if updateStatus == errSecSuccess { return }
        guard updateStatus == errSecItemNotFound else {
            throw LiveAuthContractError.invalid("Keychain value could not be updated.")
        }
        var value = query
        value[kSecValueData as String] = data
        value[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let addStatus = SecItemAdd(value as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw LiveAuthContractError.invalid("Keychain value could not be stored.")
        }
    }

    func remove(account: String) throws {
        let status = SecItemDelete(baseQuery(account: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw LiveAuthContractError.invalid("Keychain value could not be removed.")
        }
    }

    func removeAll() throws {
        let status = SecItemDelete([
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrSynchronizable as String: false,
        ] as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw LiveAuthContractError.invalid("Live Keychain values could not be removed.")
        }
    }

    private func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrSynchronizable as String: false,
        ]
    }
}

struct LiveDevicePublicIdentity: Codable, Equatable, Sendable {
    let deviceID: String
    let publicKeyBase64URL: String
}

actor LiveDeviceIdentityStore {
    private let secureStore: any LiveSecureValueStore
    private let random: any LiveRandomSource
    private let account = "device-p256-private-key"
    private var cachedKey: P256.Signing.PrivateKey?

    init(
        secureStore: any LiveSecureValueStore = KeychainLiveSecureValueStore(),
        random: any LiveRandomSource = SystemLiveRandomSource()
    ) {
        self.secureStore = secureStore
        self.random = random
    }

    func publicIdentity() throws -> LiveDevicePublicIdentity {
        let key = try loadOrCreate()
        let publicBytes = key.publicKey.x963Representation
        return LiveDevicePublicIdentity(
            deviceID: LiveAuthEncoding.identity(prefix: "csd", publicKeyX963: publicBytes),
            publicKeyBase64URL: LiveAuthEncoding.encodeBase64URL(publicBytes)
        )
    }

    func signP1363(_ bytes: Data) throws -> Data {
        try loadOrCreate().signature(for: bytes).rawRepresentation
    }

    private func loadOrCreate() throws -> P256.Signing.PrivateKey {
        if let cachedKey { return cachedKey }
        let key: P256.Signing.PrivateKey
        if let stored = try secureStore.read(account: account) {
            guard stored.count == 32,
                  let decoded = try? P256.Signing.PrivateKey(rawRepresentation: stored) else {
                throw LiveAuthContractError.invalid("Stored device identity is corrupt.")
            }
            key = decoded
        } else {
            let seed = try random.bytes(count: 32)
            guard let generated = try? P256.Signing.PrivateKey(rawRepresentation: seed) else {
                let fallback = P256.Signing.PrivateKey()
                try secureStore.write(fallback.rawRepresentation, account: account)
                cachedKey = fallback
                return fallback
            }
            try secureStore.write(generated.rawRepresentation, account: account)
            key = generated
        }
        cachedKey = key
        return key
    }
}

struct LiveStoredGrant: Codable, Equatable, Sendable {
    let schema: String
    let desktopPublicKeyBase64URL: String
    let envelope: LivePairingGrantEnvelope
    let payload: LivePairingGrantPayload

    enum CodingKeys: String, CodingKey {
        case schema
        case desktopPublicKeyBase64URL = "desktop_public_key_b64u"
        case envelope
        case payload
    }

    func validate(currentAt now: Date? = nil) throws {
        guard schema == "capture_splat.live_stored_grant.v0.1" else {
            throw LiveAuthContractError.invalid("Stored grant schema is invalid.")
        }
        let publicBytes = try LiveAuthValidation.p256PublicKey(desktopPublicKeyBase64URL)
        guard LiveAuthEncoding.identity(prefix: "wsd", publicKeyX963: publicBytes) == payload.desktopID else {
            throw LiveAuthContractError.invalid("Stored desktop identity does not match the grant.")
        }
        try payload.validate(currentAt: now)
        let payloadBytes = try LiveAuthEncoding.decodeBase64URL(
            envelope.payloadBase64URL,
            field: "grant payload"
        )
        let decoded = try LiveStrictJSON.decodeCanonical(
            LivePairingGrantPayload.self,
            from: payloadBytes
        )
        guard decoded == payload,
              envelope.schema == "capture_splat.live_pairing_grant_envelope.v0.1" else {
            throw LiveAuthContractError.invalid("Stored grant envelope does not match its payload.")
        }
        let signatureBytes = try LiveAuthValidation.p1363Signature(
            envelope.desktopSignatureBase64URL
        )
        let signature = try P256.Signing.ECDSASignature(rawRepresentation: signatureBytes)
        let publicKey = try P256.Signing.PublicKey(x963Representation: publicBytes)
        guard publicKey.isValidSignature(
            signature,
            for: LiveAuthContract.grantSignatureDomain + payloadBytes
        ) else {
            throw LiveAuthContractError.invalid("Stored desktop grant signature is invalid.")
        }
    }
}

actor LiveGrantStore {
    private let secureStore: any LiveSecureValueStore

    init(secureStore: any LiveSecureValueStore = KeychainLiveSecureValueStore()) {
        self.secureStore = secureStore
    }

    func save(_ grant: LiveStoredGrant) throws {
        try grant.validate()
        try secureStore.write(
            LiveStrictJSON.canonicalData(grant),
            account: account(desktopID: grant.payload.desktopID)
        )
    }

    func load(desktopID: String, currentAt now: Date? = nil) throws -> LiveStoredGrant? {
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        guard let data = try secureStore.read(account: account(desktopID: desktopID)) else {
            return nil
        }
        let grant = try LiveStrictJSON.decodeCanonical(LiveStoredGrant.self, from: data)
        try grant.validate(currentAt: now)
        guard grant.payload.desktopID == desktopID else {
            throw LiveAuthContractError.invalid("Stored grant belongs to another desktop.")
        }
        return grant
    }

    func remove(desktopID: String) throws {
        try secureStore.remove(account: account(desktopID: desktopID))
    }

    private func account(desktopID: String) -> String {
        "grant.\(desktopID)"
    }
}

actor LiveRequestCounterStore {
    private struct State: Codable, Equatable {
        let schema: String
        var counters: [String: String]
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

    private let stateURL: URL
    private var loaded: State?

    init(stateURL: URL) {
        self.stateURL = stateURL
    }

    func register(grantID: String) throws {
        try LiveAuthValidation.identifier(grantID, prefix: "csg")
        var state = try load()
        if state.counters[grantID] == nil {
            state.counters[grantID] = "0"
            try persist(state)
        }
    }

    func next(grantID: String) throws -> UInt64 {
        var state = try load()
        guard let text = state.counters[grantID], let current = UInt64(text) else {
            throw LiveAuthContractError.invalid("Counter state is missing; re-pair before sending.")
        }
        guard current < UInt64.max else {
            throw LiveAuthContractError.invalid("Request counter is exhausted.")
        }
        let next = current + 1
        state.counters[grantID] = String(next)
        try persist(state)
        return next
    }

    func last(grantID: String) throws -> UInt64? {
        let state = try load()
        return state.counters[grantID].flatMap(UInt64.init)
    }

    private func load() throws -> State {
        if let loaded { return loaded }
        guard FileManager.default.fileExists(atPath: stateURL.path) else {
            let state = State(
                schema: "capture_splat.live_request_counters.v0.1",
                counters: [:]
            )
            loaded = state
            return state
        }
        let data = try Data(contentsOf: stateURL, options: .mappedIfSafe)
        let envelope = try LiveStrictJSON.decodeCanonical(Envelope.self, from: data)
        guard envelope.schema == "capture_splat.live_request_counter_envelope.v0.1" else {
            throw LiveAuthContractError.invalid("Counter envelope schema is invalid.")
        }
        try LiveAuthValidation.sha256(envelope.payloadSHA256)
        let payload = try LiveAuthEncoding.decodeBase64URL(
            envelope.payloadBase64URL,
            field: "counter payload"
        )
        guard LiveAuthEncoding.sha256(payload) == envelope.payloadSHA256 else {
            throw LiveAuthContractError.invalid("Counter state checksum is invalid.")
        }
        let state = try LiveStrictJSON.decodeCanonical(State.self, from: payload)
        guard state.schema == "capture_splat.live_request_counters.v0.1",
              state.counters.allSatisfy({ key, value in
                  (try? LiveAuthValidation.identifier(key, prefix: "csg")) != nil
                      && UInt64(value) != nil
                      && (value == "0" || !value.hasPrefix("0"))
              }) else {
            throw LiveAuthContractError.invalid("Counter state is invalid.")
        }
        loaded = state
        return state
    }

    private func persist(_ state: State) throws {
        let payload = try LiveStrictJSON.canonicalData(state)
        let envelope = Envelope(
            schema: "capture_splat.live_request_counter_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(payload),
            payloadSHA256: LiveAuthEncoding.sha256(payload)
        )
        try LiveAtomicFile.write(
            LiveStrictJSON.canonicalData(envelope),
            to: stateURL
        )
        loaded = state
    }
}

struct LiveHTTPResponse: Sendable {
    let statusCode: Int
    let body: Data
    let headers: [String: String]
}

enum LiveHTTPUpload: Sendable {
    case empty
    case data(Data)
    case file(URL)
}

protocol LiveHTTPSPerforming: Sendable {
    func perform(_ request: URLRequest, upload: LiveHTTPUpload) async throws -> LiveHTTPResponse
}

final class LivePinnedURLSessionTransport: NSObject, LiveHTTPSPerforming, URLSessionDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    private let expectedHost: String
    private let expectedPin: String
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 150
        configuration.timeoutIntervalForResource = 180
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        configuration.tlsMinimumSupportedProtocolVersion = .TLSv13
        configuration.tlsMaximumSupportedProtocolVersion = .TLSv13
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    init(endpoint: LiveResolvedEndpoint, certificateSHA256: String) throws {
        try LiveAuthValidation.sha256(certificateSHA256)
        expectedHost = endpoint.host.lowercased()
        expectedPin = certificateSHA256
        super.init()
    }

    func perform(_ request: URLRequest, upload: LiveHTTPUpload) async throws -> LiveHTTPResponse {
        let result: (Data, URLResponse)
        switch upload {
        case .empty:
            result = try await session.data(for: request)
        case .data(let data):
            result = try await session.upload(for: request, from: data)
        case .file(let url):
            result = try await session.upload(for: request, fromFile: url)
        }
        guard let response = result.1 as? HTTPURLResponse,
              result.0.count <= 1_048_576 else {
            throw LiveAuthenticatedRequestError.network("HTTPS response is invalid or oversized.")
        }
        let headers = response.allHeaderFields.reduce(into: [String: String]()) { result, entry in
            if let key = entry.key as? String, let value = entry.value as? String {
                result[key.lowercased()] = value
            }
        }
        return LiveHTTPResponse(
            statusCode: response.statusCode,
            body: result.0,
            headers: headers
        )
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host.lowercased() == expectedHost,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let certificateData = SecCertificateCopyData(leaf) as Data
        guard LiveAuthEncoding.sha256(certificateData) == expectedPin else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        SecTrustSetPolicies(trust, SecPolicyCreateBasicX509())
        SecTrustSetAnchorCertificates(trust, [leaf] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var trustError: CFError?
        guard SecTrustEvaluateWithError(trust, &trustError) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

struct LivePairingBinding: Codable, Equatable, Sendable {
    let pairingID: String
    let desktopID: String
    let desktopPublicKeyBase64URL: String
    let discovery: LiveDiscoveryIdentity
    let tlsCertificateSHA256: String
    let permissions: [LivePermission]
    let authority: String

    enum CodingKeys: String, CodingKey {
        case pairingID = "pairing_id"
        case desktopID = "desktop_id"
        case desktopPublicKeyBase64URL = "desktop_public_key_b64u"
        case discovery
        case tlsCertificateSHA256 = "tls_certificate_sha256"
        case permissions, authority
    }

    init(invitation: LivePairingInvitation) {
        pairingID = invitation.pairingID
        desktopID = invitation.desktopID
        desktopPublicKeyBase64URL = invitation.desktopPublicKeyBase64URL
        discovery = invitation.discovery
        tlsCertificateSHA256 = invitation.tlsCertificateSHA256
        permissions = invitation.permissions
        authority = invitation.authority
    }

    func validate() throws {
        try LiveAuthValidation.identifier(pairingID, prefix: "csp")
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        let publicKey = try LiveAuthValidation.p256PublicKey(desktopPublicKeyBase64URL)
        guard LiveAuthEncoding.identity(prefix: "wsd", publicKeyX963: publicKey) == desktopID,
              authority == LiveAuthContract.authority else {
            throw LiveAuthContractError.invalid("Pending pairing desktop binding is invalid.")
        }
        try discovery.validate()
        try LiveAuthValidation.sha256(tlsCertificateSHA256)
        try LiveAuthValidation.canonicalPermissions(permissions)
    }
}

struct LivePreparedPairing: Codable, Equatable, Sendable {
    let schema: String
    let binding: LivePairingBinding
    let endpoint: LiveResolvedEndpoint
    let requestPayload: LivePairingRequestPayload
    let canonicalRequestBody: Data

    func validate() throws {
        guard schema == "capture_splat.live_pending_pairing.v0.1",
              canonicalRequestBody.count <= 64 * 1024 else {
            throw LiveAuthContractError.invalid("Pending pairing state is invalid.")
        }
        try binding.validate()
        guard endpoint.discovery == binding.discovery else {
            throw LiveAuthContractError.invalid("Pending pairing endpoint is invalid.")
        }
        try requestPayload.validate()
        guard requestPayload.pairingID == binding.pairingID,
              requestPayload.desktopID == binding.desktopID,
              requestPayload.requestedPermissions == binding.permissions,
              requestPayload.authority == binding.authority else {
            throw LiveAuthContractError.invalid("Pending pairing request binding is invalid.")
        }
        let envelope = try LiveStrictJSON.decode(
            LivePairingRequestEnvelope.self,
            from: canonicalRequestBody,
            exactKeys: [
                "schema",
                "payload_b64u",
                "device_signature_b64u",
                "invitation_proof_b64u",
            ]
        )
        guard envelope.schema == "capture_splat.live_pairing_request_envelope.v0.1",
              try LiveStrictJSON.canonicalData(envelope) == canonicalRequestBody else {
            throw LiveAuthContractError.invalid("Pending pairing envelope is not canonical.")
        }
        let payload = try LiveAuthEncoding.decodeBase64URL(
            envelope.payloadBase64URL,
            field: "pending pairing payload"
        )
        guard payload == (try LiveStrictJSON.canonicalData(requestPayload)) else {
            throw LiveAuthContractError.invalid("Pending pairing payload changed.")
        }
        let signatureBytes = try LiveAuthValidation.p1363Signature(
            envelope.deviceSignatureBase64URL
        )
        let signature = try P256.Signing.ECDSASignature(rawRepresentation: signatureBytes)
        let publicBytes = try LiveAuthValidation.p256PublicKey(
            requestPayload.devicePublicKeyBase64URL
        )
        let publicKey = try P256.Signing.PublicKey(x963Representation: publicBytes)
        guard publicKey.isValidSignature(
            signature,
            for: LiveAuthContract.requestSignatureDomain + payload
        ) else {
            throw LiveAuthContractError.invalid("Pending pairing signature is invalid.")
        }
        _ = try LiveAuthEncoding.decodeBase64URL(
            envelope.invitationProofBase64URL,
            expectedBytes: 32,
            field: "pending invitation proof"
        )
    }
}

actor LivePendingPairingStore {
    private let secureStore: any LiveSecureValueStore

    init(secureStore: any LiveSecureValueStore = KeychainLiveSecureValueStore()) {
        self.secureStore = secureStore
    }

    func save(_ prepared: LivePreparedPairing) throws {
        try prepared.validate()
        try secureStore.write(
            LiveStrictJSON.canonicalData(prepared),
            account: account(desktopID: prepared.binding.desktopID)
        )
    }

    func load(desktopID: String) throws -> LivePreparedPairing? {
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        guard let data = try secureStore.read(account: account(desktopID: desktopID)) else {
            return nil
        }
        let prepared = try LiveStrictJSON.decodeCanonical(LivePreparedPairing.self, from: data)
        try prepared.validate()
        guard prepared.binding.desktopID == desktopID else {
            throw LiveAuthContractError.invalid("Pending pairing belongs to another desktop.")
        }
        return prepared
    }

    func remove(desktopID: String) throws {
        try secureStore.remove(account: account(desktopID: desktopID))
    }

    private func account(desktopID: String) -> String {
        "pending-pairing.\(desktopID)"
    }
}

actor LivePairingClient {
    typealias Clock = @Sendable () -> Date

    private let identityStore: LiveDeviceIdentityStore
    private let grantStore: LiveGrantStore
    private let pendingStore: LivePendingPairingStore
    private let counterStore: LiveRequestCounterStore
    private let random: any LiveRandomSource

    init(
        identityStore: LiveDeviceIdentityStore,
        grantStore: LiveGrantStore,
        pendingStore: LivePendingPairingStore = LivePendingPairingStore(),
        counterStore: LiveRequestCounterStore,
        random: any LiveRandomSource = SystemLiveRandomSource()
    ) {
        self.identityStore = identityStore
        self.grantStore = grantStore
        self.pendingStore = pendingStore
        self.counterStore = counterStore
        self.random = random
    }

    func prepare(
        invitationURI: String,
        endpoint: LiveResolvedEndpoint,
        deviceName: String,
        appVersion: String,
        now: Date
    ) async throws -> LivePreparedPairing {
        let invitation = try Self.decodeInvitationURI(invitationURI, freshAt: now)
        try endpoint.validate(against: invitation)
        let identity = try await identityStore.publicIdentity()
        let requestID = try LiveAuthEncoding.randomID(
            prefix: "csr",
            bytes: random.bytes(count: 16)
        )
        let payload = LivePairingRequestPayload(
            schema: "capture_splat.live_pairing_request_payload.v0.1",
            pairingID: invitation.pairingID,
            requestID: requestID,
            desktopID: invitation.desktopID,
            deviceID: identity.deviceID,
            deviceName: deviceName,
            devicePublicKeyBase64URL: identity.publicKeyBase64URL,
            devicePlatform: "ios",
            deviceAppVersion: appVersion,
            clientNonceBase64URL: LiveAuthEncoding.encodeBase64URL(try random.bytes(count: 16)),
            requestedPermissions: invitation.permissions,
            createdAt: LiveAuthTime.string(now),
            authority: LiveAuthContract.authority
        )
        try payload.validate()
        let payloadBytes = try LiveStrictJSON.canonicalData(payload)
        let signature = try await identityStore.signP1363(
            LiveAuthContract.requestSignatureDomain + payloadBytes
        )
        let secret = try LiveAuthEncoding.decodeBase64URL(
            invitation.pairingSecretBase64URL,
            expectedBytes: 32,
            field: "pairing_secret_b64u"
        )
        let proof = Data(HMAC<SHA256>.authenticationCode(
            for: LiveAuthContract.proofDomain + payloadBytes,
            using: SymmetricKey(data: secret)
        ))
        let envelope = LivePairingRequestEnvelope(
            schema: "capture_splat.live_pairing_request_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(payloadBytes),
            deviceSignatureBase64URL: LiveAuthEncoding.encodeBase64URL(signature),
            invitationProofBase64URL: LiveAuthEncoding.encodeBase64URL(proof)
        )
        let prepared = LivePreparedPairing(
            schema: "capture_splat.live_pending_pairing.v0.1",
            binding: LivePairingBinding(invitation: invitation),
            endpoint: endpoint,
            requestPayload: payload,
            canonicalRequestBody: try LiveStrictJSON.canonicalData(envelope)
        )
        try await pendingStore.save(prepared)
        return prepared
    }

    func resumePending(desktopID: String) async throws -> LivePreparedPairing? {
        guard let prepared = try await pendingStore.load(desktopID: desktopID) else {
            return nil
        }
        let identity = try await identityStore.publicIdentity()
        guard prepared.requestPayload.deviceID == identity.deviceID,
              prepared.requestPayload.devicePublicKeyBase64URL == identity.publicKeyBase64URL else {
            throw LiveAuthContractError.invalid(
                "Pending pairing belongs to another Capture Splat identity."
            )
        }
        return prepared
    }

    func submit(
        _ prepared: LivePreparedPairing,
        clock: @escaping Clock = { Date() }
    ) async throws -> LiveStoredGrant {
        let transport = try LivePinnedURLSessionTransport(
            endpoint: prepared.endpoint,
            certificateSHA256: prepared.binding.tlsCertificateSHA256
        )
        return try await submitPrepared(prepared, using: transport, clock: clock)
    }

    #if CAPTURE_SPLAT_LIVE_TESTING
    func submitForTesting(
        _ prepared: LivePreparedPairing,
        using transport: any LiveHTTPSPerforming,
        clock: @escaping Clock = { Date() }
    ) async throws -> LiveStoredGrant {
        try await submitPrepared(prepared, using: transport, clock: clock)
    }
    #endif

    private func submitPrepared(
        _ prepared: LivePreparedPairing,
        using transport: any LiveHTTPSPerforming,
        clock: @escaping Clock
    ) async throws -> LiveStoredGrant {
        try prepared.validate()
        var request = URLRequest(url: try prepared.endpoint.url(
            path: "\(LiveAuthContract.pairingAPIRoot)/requests"
        ))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(
            String(prepared.canonicalRequestBody.count),
            forHTTPHeaderField: "Content-Length"
        )
        let response = try await transport.perform(
            request,
            upload: .data(prepared.canonicalRequestBody)
        )
        guard response.statusCode == 200 else {
            throw try Self.responseError(response)
        }
        let envelope = try LiveStrictJSON.decode(
            LivePairingGrantEnvelope.self,
            from: response.body,
            exactKeys: ["schema", "payload_b64u", "desktop_signature_b64u"]
        )
        let grant = try Self.verifyGrant(
            envelope,
            prepared: prepared,
            currentAt: clock()
        )
        try await counterStore.register(grantID: grant.payload.grantID)
        try await grantStore.save(grant)
        try await pendingStore.remove(desktopID: grant.payload.desktopID)
        return grant
    }

    static func decodeInvitationURI(
        _ uri: String,
        freshAt now: Date? = nil
    ) throws -> LivePairingInvitation {
        guard uri.utf8.count <= LiveAuthContract.maximumQRBytes,
              uri.unicodeScalars.allSatisfy(\.isASCII),
              uri.hasPrefix(LiveAuthContract.qrPrefix),
              !uri.contains("?"),
              !uri.contains("#"),
              !uri.contains("%") else {
            throw LiveAuthContractError.invalid("Pairing QR URI is not canonical.")
        }
        let payload = try LiveAuthEncoding.decodeBase64URL(
            String(uri.dropFirst(LiveAuthContract.qrPrefix.count)),
            field: "pairing QR payload"
        )
        guard payload.count <= LiveAuthContract.maximumPayloadBytes else {
            throw LiveAuthContractError.invalid("Pairing QR payload is oversized.")
        }
        let invitation = try LiveStrictJSON.decodeCanonical(
            LivePairingInvitation.self,
            from: payload
        )
        try invitation.validate(freshAt: now)
        return invitation
    }

    static func invitationURI(_ invitation: LivePairingInvitation) throws -> String {
        try invitation.validate()
        let uri = LiveAuthContract.qrPrefix
            + LiveAuthEncoding.encodeBase64URL(try LiveStrictJSON.canonicalData(invitation))
        guard uri.utf8.count <= LiveAuthContract.maximumQRBytes else {
            throw LiveAuthContractError.invalid("Pairing QR URI is oversized.")
        }
        return uri
    }

    private static func verifyGrant(
        _ envelope: LivePairingGrantEnvelope,
        prepared: LivePreparedPairing,
        currentAt now: Date
    ) throws -> LiveStoredGrant {
        guard envelope.schema == "capture_splat.live_pairing_grant_envelope.v0.1" else {
            throw LiveAuthContractError.invalid("Unexpected pairing grant envelope schema.")
        }
        let payloadBytes = try LiveAuthEncoding.decodeBase64URL(
            envelope.payloadBase64URL,
            field: "grant payload"
        )
        guard payloadBytes.count <= LiveAuthContract.maximumPayloadBytes else {
            throw LiveAuthContractError.invalid("Pairing grant payload is oversized.")
        }
        let payload = try LiveStrictJSON.decodeCanonical(
            LivePairingGrantPayload.self,
            from: payloadBytes
        )
        try payload.validate(currentAt: now)
        let binding = prepared.binding
        let request = prepared.requestPayload
        guard payload.pairingID == request.pairingID,
              payload.requestID == request.requestID,
              payload.desktopID == binding.desktopID,
              payload.deviceID == request.deviceID,
              payload.devicePublicKeyBase64URL == request.devicePublicKeyBase64URL,
              payload.permissions == request.requestedPermissions,
              payload.liveDiscovery == binding.discovery,
              payload.tlsCertificateSHA256 == binding.tlsCertificateSHA256 else {
            throw LiveAuthContractError.invalid("Pairing grant does not match the request and invitation.")
        }
        let signatureData = try LiveAuthValidation.p1363Signature(
            envelope.desktopSignatureBase64URL
        )
        let signature = try P256.Signing.ECDSASignature(rawRepresentation: signatureData)
        let desktopBytes = try LiveAuthValidation.p256PublicKey(
            binding.desktopPublicKeyBase64URL
        )
        let desktopKey = try P256.Signing.PublicKey(x963Representation: desktopBytes)
        guard desktopKey.isValidSignature(
            signature,
            for: LiveAuthContract.grantSignatureDomain + payloadBytes
        ) else {
            throw LiveAuthContractError.invalid("Pairing grant desktop signature is invalid.")
        }
        return LiveStoredGrant(
            schema: "capture_splat.live_stored_grant.v0.1",
            desktopPublicKeyBase64URL: binding.desktopPublicKeyBase64URL,
            envelope: envelope,
            payload: payload
        )
    }

    private static func responseError(_ response: LiveHTTPResponse) throws -> Error {
        if let error = try? LiveAuthErrorBody.decodeStrict(response.body) {
            return LiveAuthenticatedRequestError.auth(code: error.code, retryable: error.retryable)
        }
        return LiveAuthenticatedRequestError.http(status: response.statusCode)
    }
}

enum LiveAuthenticatedBody: Sendable {
    case empty
    case data(Data, contentType: String)
    case file(URL, byteCount: Int64, sha256: String, contentType: String)
}

enum LiveAuthenticatedRequestError: Error, Equatable, LocalizedError {
    case auth(code: String, retryable: Bool)
    case corruptBody(String)
    case http(status: Int)
    case network(String)

    var retryable: Bool {
        switch self {
        case .auth(_, let retryable):
            return retryable
        case .network:
            return true
        case .http(let status):
            return status == 408 || status == 425 || status == 429 || status >= 500
        case .corruptBody:
            return false
        }
    }

    var errorDescription: String? {
        switch self {
        case .auth(let code, _):
            return "Live authentication failed: \(code)."
        case .corruptBody(let message), .network(let message):
            return message
        case .http(let status):
            return "Live receiver returned HTTP \(status)."
        }
    }
}

actor LiveAuthenticatedHTTPClient {
    private let endpoint: LiveResolvedEndpoint
    private let desktopID: String
    private let certificateSHA256: String
    private let identityStore: LiveDeviceIdentityStore
    private let grantStore: LiveGrantStore
    private let counterStore: LiveRequestCounterStore
    private let transport: any LiveHTTPSPerforming
    private let random: any LiveRandomSource

    private init(
        endpoint: LiveResolvedEndpoint,
        desktopID: String,
        certificateSHA256: String,
        identityStore: LiveDeviceIdentityStore,
        grantStore: LiveGrantStore,
        counterStore: LiveRequestCounterStore,
        transport: any LiveHTTPSPerforming,
        random: any LiveRandomSource = SystemLiveRandomSource()
    ) throws {
        try LiveAuthValidation.sha256(certificateSHA256)
        self.endpoint = endpoint
        self.desktopID = desktopID
        self.certificateSHA256 = certificateSHA256
        self.identityStore = identityStore
        self.grantStore = grantStore
        self.counterStore = counterStore
        self.transport = transport
        self.random = random
    }

    static func pinned(
        endpoint: LiveResolvedEndpoint,
        desktopID: String,
        certificateSHA256: String,
        identityStore: LiveDeviceIdentityStore,
        grantStore: LiveGrantStore,
        counterStore: LiveRequestCounterStore,
        random: any LiveRandomSource = SystemLiveRandomSource()
    ) throws -> LiveAuthenticatedHTTPClient {
        try LiveAuthenticatedHTTPClient(
            endpoint: endpoint,
            desktopID: desktopID,
            certificateSHA256: certificateSHA256,
            identityStore: identityStore,
            grantStore: grantStore,
            counterStore: counterStore,
            transport: LivePinnedURLSessionTransport(
                endpoint: endpoint,
                certificateSHA256: certificateSHA256
            ),
            random: random
        )
    }

    #if CAPTURE_SPLAT_LIVE_TESTING
    static func testing(
        endpoint: LiveResolvedEndpoint,
        desktopID: String,
        certificateSHA256: String,
        identityStore: LiveDeviceIdentityStore,
        grantStore: LiveGrantStore,
        counterStore: LiveRequestCounterStore,
        transport: any LiveHTTPSPerforming,
        random: any LiveRandomSource = SystemLiveRandomSource()
    ) throws -> LiveAuthenticatedHTTPClient {
        try LiveAuthenticatedHTTPClient(
            endpoint: endpoint,
            desktopID: desktopID,
            certificateSHA256: certificateSHA256,
            identityStore: identityStore,
            grantStore: grantStore,
            counterStore: counterStore,
            transport: transport,
            random: random
        )
    }
    #endif

    func validateSenderAuthorization(
        now: Date
    ) async throws -> LiveSenderAuthorizationBinding {
        let grant = try await validatedGrant(now: now)
        let required: Set<LivePermission> = [
            .sessionCreate,
            .sessionResume,
            .framePut,
            .assetPut,
            .sessionFinalize,
        ]
        guard required.isSubset(of: Set(grant.payload.permissions)) else {
            throw LiveAuthenticatedRequestError.auth(code: "permission_denied", retryable: false)
        }
        return try LiveSenderAuthorizationBinding(
            desktopID: grant.payload.desktopID,
            deviceID: grant.payload.deviceID
        )
    }

    func perform(
        method: String,
        path: String,
        body: LiveAuthenticatedBody,
        now: Date
    ) async throws -> Data {
        try LiveAuthValidation.canonicalPath(path)
        guard ["GET", "POST", "PUT"].contains(method) else {
            throw LiveAuthContractError.invalid("Authenticated HTTP method is invalid.")
        }
        let grant = try await validatedGrant(now: now)
        let identity = try await identityStore.publicIdentity()
        let requiredPermission = try Self.requiredPermission(method: method, path: path)
        guard grant.payload.permissions.contains(requiredPermission) else {
            throw LiveAuthenticatedRequestError.auth(code: "permission_denied", retryable: false)
        }
        let evidence = try Self.bodyEvidence(body)
        let counter = try await counterStore.next(grantID: grant.payload.grantID)
        let requestID = try LiveAuthEncoding.randomID(
            prefix: "csr",
            bytes: random.bytes(count: 16)
        )
        let timestamp = LiveAuthTime.string(now)
        let canonical = try Self.canonicalRequestBytes(
            desktopID: desktopID,
            deviceID: identity.deviceID,
            grantID: grant.payload.grantID,
            counter: counter,
            requestID: requestID,
            timestamp: timestamp,
            method: method,
            path: path,
            contentType: evidence.contentType,
            contentLength: evidence.byteCount,
            contentSHA256: evidence.sha256
        )
        let signature = try await identityStore.signP1363(canonical)
        var request = URLRequest(url: try endpoint.url(path: path))
        request.httpMethod = method
        request.setValue(identity.deviceID, forHTTPHeaderField: "X-Capture-Splat-Device")
        request.setValue(grant.payload.grantID, forHTTPHeaderField: "X-Capture-Splat-Grant")
        request.setValue(String(counter), forHTTPHeaderField: "X-Capture-Splat-Counter")
        request.setValue(requestID, forHTTPHeaderField: "X-Capture-Splat-Request")
        request.setValue(timestamp, forHTTPHeaderField: "X-Capture-Splat-Time")
        request.setValue(evidence.sha256, forHTTPHeaderField: "X-Capture-Splat-Content-SHA256")
        request.setValue(
            LiveAuthEncoding.encodeBase64URL(signature),
            forHTTPHeaderField: "X-Capture-Splat-Signature"
        )
        request.setValue(String(evidence.byteCount), forHTTPHeaderField: "Content-Length")
        if let contentType = evidence.contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        try Task.checkCancellation()
        let response: LiveHTTPResponse
        do {
            response = try await transport.perform(request, upload: evidence.upload)
        } catch let error as LiveAuthenticatedRequestError {
            throw error
        } catch {
            throw LiveAuthenticatedRequestError.network(error.localizedDescription)
        }
        guard (200...299).contains(response.statusCode) else {
            let requestError = try Self.responseError(response)
            if case .auth(let code, _) = requestError,
               ["grant_expired", "grant_revoked", "grant_unknown", "identity_mismatch"].contains(code) {
                try? await grantStore.remove(desktopID: desktopID)
            }
            throw requestError
        }
        return response.body
    }

    private func validatedGrant(now: Date) async throws -> LiveStoredGrant {
        guard let grant = try await grantStore.load(desktopID: desktopID) else {
            throw LiveAuthenticatedRequestError.auth(code: "grant_unknown", retryable: false)
        }
        let notBefore = try LiveAuthTime.parse(grant.payload.notBefore)
        let expiresAt = try LiveAuthTime.parse(grant.payload.expiresAt)
        guard (notBefore..<expiresAt).contains(now) else {
            try? await grantStore.remove(desktopID: desktopID)
            throw LiveAuthenticatedRequestError.auth(code: "grant_expired", retryable: false)
        }
        let identity = try await identityStore.publicIdentity()
        guard endpoint.discovery == grant.payload.liveDiscovery,
              certificateSHA256 == grant.payload.tlsCertificateSHA256,
              identity.deviceID == grant.payload.deviceID,
              identity.publicKeyBase64URL == grant.payload.devicePublicKeyBase64URL else {
            try? await grantStore.remove(desktopID: desktopID)
            throw LiveAuthenticatedRequestError.auth(code: "identity_mismatch", retryable: false)
        }
        return grant
    }

    private static func requiredPermission(method: String, path: String) throws -> LivePermission {
        if method == "GET",
           (path == "\(LiveAuthContract.liveAPIRoot)/health"
               || path == "\(LiveAuthContract.liveAPIRoot)/status") {
            return .receiverStatus
        }
        if method == "GET", path.hasPrefix("\(LiveAuthContract.liveAPIRoot)/sessions/") {
            return .sessionResume
        }
        if method == "POST", path.hasSuffix("/finalize") {
            return .sessionFinalize
        }
        if method == "PUT", path.contains("/assets/") {
            return .assetPut
        }
        if method == "PUT", path.contains("/frames/") {
            return .framePut
        }
        if method == "PUT", path.hasPrefix("\(LiveAuthContract.liveAPIRoot)/sessions/") {
            return .sessionCreate
        }
        throw LiveAuthContractError.invalid("Authenticated route has no defined permission.")
    }

    static func canonicalRequestBytes(
        desktopID: String,
        deviceID: String,
        grantID: String,
        counter: UInt64,
        requestID: String,
        timestamp: String,
        method: String,
        path: String,
        contentType: String?,
        contentLength: Int64,
        contentSHA256: String
    ) throws -> Data {
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        try LiveAuthValidation.identity(deviceID, prefix: "csd")
        try LiveAuthValidation.identifier(grantID, prefix: "csg")
        try LiveAuthValidation.identifier(requestID, prefix: "csr")
        _ = try LiveAuthTime.parse(timestamp)
        guard ["GET", "POST", "PUT"].contains(method), contentLength >= 0 else {
            throw LiveAuthContractError.invalid("Authenticated request metadata is invalid.")
        }
        try LiveAuthValidation.canonicalPath(path)
        if let contentType { try LiveAuthValidation.mediaType(contentType) }
        try LiveAuthValidation.sha256(contentSHA256)
        return Data([
            LiveAuthContract.authenticatedRequestDomain,
            desktopID,
            deviceID,
            grantID,
            String(counter),
            requestID,
            timestamp,
            method,
            path,
            contentType ?? "-",
            String(contentLength),
            contentSHA256,
            "",
        ].joined(separator: "\n").utf8)
    }

    private static func bodyEvidence(
        _ body: LiveAuthenticatedBody
    ) throws -> (byteCount: Int64, sha256: String, contentType: String?, upload: LiveHTTPUpload) {
        switch body {
        case .empty:
            return (
                0,
                LiveAuthEncoding.sha256(Data()),
                nil,
                .empty
            )
        case .data(let data, let contentType):
            try LiveAuthValidation.mediaType(contentType)
            return (
                Int64(data.count),
                LiveAuthEncoding.sha256(data),
                contentType,
                .data(data)
            )
        case .file(let url, let byteCount, let sha256, let contentType):
            try LiveAuthValidation.sha256(sha256)
            try LiveAuthValidation.mediaType(contentType)
            guard byteCount >= 0,
                  let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
                  values.isRegularFile == true,
                  values.fileSize.map(Int64.init) == byteCount else {
                throw LiveAuthenticatedRequestError.corruptBody("Upload file size changed before sending.")
            }
            let actual = try LiveFileDigest.sha256(url: url)
            guard actual == sha256 else {
                throw LiveAuthenticatedRequestError.corruptBody("Upload file checksum changed before sending.")
            }
            return (byteCount, sha256, contentType, .file(url))
        }
    }

    private static func responseError(_ response: LiveHTTPResponse) throws -> LiveAuthenticatedRequestError {
        if let error = try? LiveAuthErrorBody.decodeStrict(response.body) {
            return .auth(code: error.code, retryable: error.retryable)
        }
        return .http(status: response.statusCode)
    }
}

enum LiveFileDigest {
    static func sha256(url: URL) throws -> String {
        let descriptor = Darwin.open(
            url.path,
            O_RDONLY | O_NOFOLLOW | O_CLOEXEC
        )
        guard descriptor >= 0 else {
            throw LiveAuthenticatedRequestError.corruptBody(
                "Upload file cannot be opened without following symbolic links."
            )
        }
        let handle = FileHandle(
            fileDescriptor: descriptor,
            closeOnDealloc: true
        )
        var before = stat()
        guard Darwin.fstat(descriptor, &before) == 0,
              before.st_mode & S_IFMT == S_IFREG else {
            try? handle.close()
            throw LiveAuthenticatedRequestError.corruptBody(
                "Upload body is not a regular file."
            )
        }
        var digest = SHA256()
        while true {
            try Task.checkCancellation()
            let chunk = try handle.read(upToCount: 1_048_576) ?? Data()
            if chunk.isEmpty { break }
            digest.update(data: chunk)
        }
        var after = stat()
        guard Darwin.fstat(descriptor, &after) == 0,
              before.st_dev == after.st_dev,
              before.st_ino == after.st_ino,
              before.st_size == after.st_size,
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
              before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec else {
            try? handle.close()
            throw LiveAuthenticatedRequestError.corruptBody(
                "Upload file changed while being hashed."
            )
        }
        try handle.close()
        return "sha256:" + digest.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

enum LiveAtomicFile {
    static func write(_ data: Data, to url: URL) throws {
        let manager = FileManager.default
        let directory = url.deletingLastPathComponent()
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        let temporary = directory.appendingPathComponent(
            ".\(url.lastPathComponent).\(UUID().uuidString).incoming"
        )
        guard manager.createFile(atPath: temporary.path, contents: nil, attributes: [
            .posixPermissions: 0o600,
        ]) else {
            throw LiveAuthContractError.invalid("Could not create an atomic state file.")
        }
        do {
            let handle = try FileHandle(forWritingTo: temporary)
            try handle.write(contentsOf: data)
            try handle.synchronize()
            try handle.close()
            let status = temporary.path.withCString { source in
                url.path.withCString { destination in
                    Darwin.rename(source, destination)
                }
            }
            guard status == 0 else {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            let directoryFD = Darwin.open(directory.path, O_RDONLY)
            guard directoryFD >= 0 else {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            defer { Darwin.close(directoryFD) }
            guard fsync(directoryFD) == 0 else {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
        } catch {
            try? manager.removeItem(at: temporary)
            throw error
        }
    }
}

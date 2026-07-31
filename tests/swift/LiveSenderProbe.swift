import CryptoKit
import Foundation

private final class MemorySecureStore: LiveSecureValueStore, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Data] = [:]

    func read(account: String) throws -> Data? {
        lock.withLock { values[account] }
    }

    func write(_ data: Data, account: String) throws {
        lock.withLock { values[account] = data }
    }

    func remove(account: String) throws {
        lock.withLock { _ = values.removeValue(forKey: account) }
    }

    func allValues() -> [Data] {
        lock.withLock { Array(values.values) }
    }
}

private final class DeterministicRandom: LiveRandomSource, @unchecked Sendable {
    private let lock = NSLock()
    private var nextValue: UInt8

    init(start: UInt8 = 1) {
        nextValue = start
    }

    func bytes(count: Int) throws -> Data {
        lock.withLock {
            let result = Data((0..<count).map { nextValue &+ UInt8($0 % 251) })
            nextValue &+= UInt8(count % 251)
            return result
        }
    }
}

private actor ScriptedHTTPS: LiveHTTPSPerforming {
    private var responses: [LiveHTTPResponse]
    private(set) var requests: [URLRequest] = []
    private(set) var uploads: [LiveHTTPUpload] = []

    init(responses: [LiveHTTPResponse]) {
        self.responses = responses
    }

    func perform(_ request: URLRequest, upload: LiveHTTPUpload) async throws -> LiveHTTPResponse {
        requests.append(request)
        uploads.append(upload)
        guard !responses.isEmpty else {
            throw LiveAuthenticatedRequestError.network("No scripted HTTPS response.")
        }
        return responses.removeFirst()
    }

    func capturedRequests() -> [URLRequest] {
        requests
    }

    func capturedUploads() -> [LiveHTTPUpload] {
        uploads
    }
}

private struct ImmediateSleeper: LiveSenderSleeping {
    func sleep(milliseconds: Int) async throws {}
}

private struct PinnedTransportConfiguration: Decodable {
    let port: Int
    let certificateSHA256: String

    enum CodingKeys: String, CodingKey {
        case port
        case certificateSHA256 = "certificate_sha256"
    }
}

private actor ReceiverHarness: LiveAuthenticatedRequesting {
    private let expectedCount: Int
    private let authorization: LiveSenderAuthorizationBinding
    private var received = Set<Int>()
    private var calls: [String] = []
    private var failedAssetOnce = false
    private var finalized = false
    private var dropFinalizationResponses = true
    private var active = 0
    private var maximumActive = 0
    private var finalizePayloadValid = false

    init(expectedCount: Int, authorization: LiveSenderAuthorizationBinding) {
        self.expectedCount = expectedCount
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
        active += 1
        maximumActive = max(maximumActive, active)
        calls.append("\(method) \(path)")
        defer { active -= 1 }
        try await Task.sleep(nanoseconds: 5_000_000)

        guard path.hasPrefix(LiveAuthContract.liveAPIRoot) else {
            throw LiveAuthenticatedRequestError.corruptBody("receiver route is not canonical")
        }
        if path.contains("/assets/source"), !failedAssetOnce {
            failedAssetOnce = true
            throw LiveAuthenticatedRequestError.network("simulated lost ACK")
        }
        let operation: LiveSenderAcknowledgement.Operation
        let status: LiveSenderAcknowledgement.Status
        var sequenceID: Int?
        var assetRole: LiveSenderAssetRole?
        if method == "GET" {
            operation = .resume
            status = finalized ? .finalized : .accepted
        } else if path.hasSuffix("/finalize") {
            let expectedBody = Data(
                "{\"final_sequence_id\":\(expectedCount),\"schema\":\"capture_splat.live_finalize.v0.1\",\"session_id\":\"sender-fixture-01\"}".utf8
            )
            guard case .data(let data, let contentType) = body,
                  contentType == "application/json",
                  data == expectedBody,
                  let document = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  Set(document.keys) == Set(["schema", "session_id", "final_sequence_id"]),
                  document["schema"] as? String == "capture_splat.live_finalize.v0.1",
                  document["session_id"] as? String == "sender-fixture-01",
                  document["final_sequence_id"] as? Int == expectedCount else {
                throw LiveAuthenticatedRequestError.corruptBody(
                    "v0.1 finalization payload is invalid"
                )
            }
            finalizePayloadValid = true
            finalized = true
            if dropFinalizationResponses {
                throw LiveAuthenticatedRequestError.network("simulated lost finalization ACK")
            }
            operation = .finalize
            status = .finalized
        } else if let sequence = sequence(from: path) {
            sequenceID = sequence
            if path.contains("/assets/") {
                operation = .asset
                status = .accepted
                assetRole = .source
                received.insert(sequence)
            } else {
                operation = .frame
                status = .incomplete
            }
        } else {
            operation = .session
            status = finalized ? .duplicate : .accepted
        }
        let missing = try missingRanges()
        let contiguous = contiguousCount()
        let acknowledgement = try LiveSenderAcknowledgement(
            sessionID: "sender-fixture-01",
            operation: operation,
            status: status,
            sequenceID: sequenceID,
            assetRole: assetRole,
            receivedCount: received.count,
            contiguousCount: contiguous,
            pendingCount: received.count - contiguous,
            expectedFrameCount: expectedCount,
            nextExpectedSequenceID: contiguous + 1,
            missingRanges: missing,
            finalized: finalized
        )
        return try LiveStrictJSON.canonicalData(acknowledgement)
    }

    func allowFinalizationResponses() {
        dropFinalizationResponses = false
    }

    func evidence() -> (calls: [String], maximumActive: Int, finalizePayloadValid: Bool) {
        (calls, maximumActive, finalizePayloadValid)
    }

    private func sequence(from path: String) -> Int? {
        let parts = path.split(separator: "/")
        guard let frames = parts.firstIndex(of: "frames"), frames + 1 < parts.count else {
            return nil
        }
        return Int(parts[frames + 1])
    }

    private func contiguousCount() -> Int {
        var value = 0
        while received.contains(value + 1) { value += 1 }
        return value
    }

    private func missingRanges() throws -> [LiveSenderMissingRange] {
        var result: [LiveSenderMissingRange] = []
        var start: Int?
        for sequence in 1...expectedCount {
            if !received.contains(sequence) {
                start = start ?? sequence
            } else if let rangeStart = start {
                result.append(try LiveSenderMissingRange(start: rangeStart, end: sequence - 1))
                start = nil
            }
        }
        if let start {
            result.append(try LiveSenderMissingRange(start: start, end: expectedCount))
        }
        return result
    }
}

private actor ProgressiveReceiverHarness: LiveAuthenticatedRequesting {
    private let sessionID: String
    private let expectedCount: Int
    private let authorization: LiveSenderAuthorizationBinding
    private let sourceManifest: LiveSenderSourceManifestReference?
    private var finalized = false
    private var droppedFinalizationACK = false
    private var calls: [String] = []
    private var finalizePayloadValid = false

    init(
        sessionID: String,
        expectedCount: Int,
        authorization: LiveSenderAuthorizationBinding,
        sourceManifest: LiveSenderSourceManifestReference? = nil
    ) {
        self.sessionID = sessionID
        self.expectedCount = expectedCount
        self.authorization = authorization
        self.sourceManifest = sourceManifest
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
        calls.append("\(method) \(path)")
        let operation: LiveSenderAcknowledgement.Operation
        let status: LiveSenderAcknowledgement.Status
        if path.hasSuffix("/finalize") {
            operation = .finalize
            status = .finalized
            guard let sourceManifest,
                  case .data(let data, let contentType) = body,
                  contentType == "application/json",
                  let document = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  Set(document.keys) == Set([
                      "schema",
                      "session_id",
                      "final_sequence_id",
                      "source_manifest",
                  ]),
                  document["schema"] as? String == "capture_splat.live_finalize.v0.2",
                  document["session_id"] as? String == sessionID,
                  document["final_sequence_id"] as? Int == expectedCount,
                  let source = document["source_manifest"] as? [String: Any],
                  Set(source.keys) == Set(["path", "schema", "sha256", "size_bytes"]),
                  source["path"] as? String == sourceManifest.path,
                  source["schema"] as? String == sourceManifest.schema,
                  source["sha256"] as? String == sourceManifest.sha256,
                  (source["size_bytes"] as? NSNumber)?.int64Value
                      == sourceManifest.sizeBytes else {
                throw LiveAuthenticatedRequestError.corruptBody(
                    "progressive finalization payload is invalid"
                )
            }
            finalizePayloadValid = true
            finalized = true
            if !droppedFinalizationACK {
                droppedFinalizationACK = true
                throw LiveAuthenticatedRequestError.network(
                    "simulated lost progressive finalization ACK"
                )
            }
        } else if method == "GET" {
            operation = .resume
            status = finalized ? .finalized : .accepted
        } else {
            operation = .session
            status = finalized ? .duplicate : .accepted
        }
        let acknowledgement = try LiveSenderAcknowledgement(
            sessionID: sessionID,
            operation: operation,
            status: status,
            receivedCount: expectedCount,
            contiguousCount: expectedCount,
            pendingCount: 0,
            expectedFrameCount: finalized ? expectedCount : nil,
            nextExpectedSequenceID: expectedCount + 1,
            missingRanges: [],
            finalized: finalized
        )
        return try LiveStrictJSON.canonicalData(acknowledgement)
    }

    func evidence() -> (calls: [String], finalizePayloadValid: Bool) {
        (calls, finalizePayloadValid)
    }
}

@main
private struct LiveSenderProbe {
    static func main() async throws {
        guard CommandLine.arguments.count == 3 else {
            throw LiveAuthContractError.invalid("Expected a scenario and working directory.")
        }
        let scenario = CommandLine.arguments[1]
        let root = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let result: [String: Any]
        switch scenario {
        case "auth-vectors":
            result = try authVectors(repositoryRoot: root)
        case "pairing":
            result = try await pairing(root: root)
        case "pinned-transport":
            result = try await pinnedTransport(root: root)
        case "queue":
            result = try await queue(root: root)
        case "progressive":
            result = try await progressive(root: root)
        case "engine":
            result = try await engine(root: root)
        case "policy":
            result = try policy()
        default:
            throw LiveAuthContractError.invalid("Unknown probe scenario.")
        }
        let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
    }

    private static func pinnedTransport(root: URL) async throws -> [String: Any] {
        let configurationData = try Data(
            contentsOf: root.appendingPathComponent("transport.json")
        )
        let configuration = try LiveStrictJSON.decode(
            PinnedTransportConfiguration.self,
            from: configurationData,
            exactKeys: ["port", "certificate_sha256"]
        )
        let endpoint = LiveResolvedEndpoint(
            host: "127.0.0.1",
            port: configuration.port,
            discovery: LiveDiscoveryIdentity(
                serviceType: LiveAuthContract.bonjourServiceType,
                serviceName: "Pinned Transport Probe",
                domain: LiveAuthContract.bonjourDomain
            )
        )
        var request = URLRequest(url: try endpoint.url(path: "/health"))
        request.httpMethod = "GET"
        request.setValue("0", forHTTPHeaderField: "Content-Length")

        let transport = try LivePinnedURLSessionTransport(
            endpoint: endpoint,
            certificateSHA256: configuration.certificateSHA256
        )
        let response = try await transport.perform(request, upload: .empty)

        var wrongPinRejected = false
        do {
            let wrongTransport = try LivePinnedURLSessionTransport(
                endpoint: endpoint,
                certificateSHA256: "sha256:\(String(repeating: "0", count: 64))"
            )
            _ = try await wrongTransport.perform(request, upload: .empty)
        } catch {
            wrongPinRejected = true
        }

        return [
            "body_match": response.body == Data("{\"ok\":true}".utf8),
            "status_code": response.statusCode,
            "tls_pin_accepted": true,
            "wrong_pin_rejected": wrongPinRejected,
        ]
    }

    private static func authVectors(repositoryRoot: URL) throws -> [String: Any] {
        let fixtures = repositoryRoot
            .appendingPathComponent("contracts/live-auth/v0.1/fixtures", isDirectory: true)
        let invitationData = try Data(contentsOf: fixtures.appendingPathComponent(
            "valid_pairing_invitation.json"
        ))
        let invitation = try LiveStrictJSON.decode(
            LivePairingInvitation.self,
            from: invitationData
        )
        try invitation.validate()
        let uri = try LivePairingClient.invitationURI(invitation)
        let decoded = try LivePairingClient.decodeInvitationURI(
            uri,
            freshAt: try LiveAuthTime.parse("2026-07-29T10:32:00.000Z")
        )

        let requestPayloadData = try Data(contentsOf: fixtures.appendingPathComponent(
            "valid_pairing_request_payload.json"
        ))
        let requestPayload = try LiveStrictJSON.decode(
            LivePairingRequestPayload.self,
            from: requestPayloadData
        )
        try requestPayload.validate()
        let payloadBytes = try LiveStrictJSON.canonicalData(requestPayload)
        let secret = try LiveAuthEncoding.decodeBase64URL(
            invitation.pairingSecretBase64URL,
            expectedBytes: 32,
            field: "pairing secret"
        )
        let proof = Data(HMAC<SHA256>.authenticationCode(
            for: LiveAuthContract.proofDomain + payloadBytes,
            using: SymmetricKey(data: secret)
        ))

        let authenticated = try JSONSerialization.jsonObject(
            with: Data(contentsOf: fixtures.appendingPathComponent(
                "valid_authenticated_request.json"
            ))
        ) as! [String: Any]
        let canonical = try LiveAuthenticatedHTTPClient.canonicalRequestBytes(
            desktopID: authenticated["desktop_id"] as! String,
            deviceID: authenticated["device_id"] as! String,
            grantID: authenticated["grant_id"] as! String,
            counter: UInt64(authenticated["counter"] as! Int),
            requestID: authenticated["request_id"] as! String,
            timestamp: authenticated["timestamp"] as! String,
            method: authenticated["method"] as! String,
            path: authenticated["path"] as! String,
            contentType: authenticated["content_type"] as? String,
            contentLength: Int64(authenticated["content_length"] as! Int),
            contentSHA256: authenticated["content_sha256"] as! String
        )
        let expectedCanonical = try LiveAuthEncoding.decodeBase64URL(
            authenticated["canonical_ascii_b64u"] as! String,
            field: "canonical request"
        )

        var duplicateRejected = false
        do {
            _ = try LiveStrictJSON.decode(
                LiveAuthErrorBody.self,
                from: Data(#"{"code":"invalid_request","code":"invalid_request","retryable":false,"schema":"capture_splat.live_auth_error.v0.1"}"#.utf8)
            )
        } catch {
            duplicateRejected = true
        }
        var noncanonicalBase64Rejected = false
        do {
            _ = try LiveAuthEncoding.decodeBase64URL("AB", field: "bad")
        } catch {
            noncanonicalBase64Rejected = true
        }
        var nonfiniteJSONRejected = false
        do {
            _ = try LiveStrictJSON.decode(
                LiveAuthErrorBody.self,
                from: Data(#"{"code":"invalid_request","message":NaN,"retryable":false,"schema":"capture_splat.live_auth_error.v0.1"}"#.utf8)
            )
        } catch {
            nonfiniteJSONRejected = true
        }
        var extraAuthErrorRejected = false
        do {
            _ = try LiveAuthErrorBody.decodeStrict(Data(
                #"{"code":"invalid_request","extra":true,"retryable":false,"schema":"capture_splat.live_auth_error.v0.1"}"#.utf8
            ))
        } catch {
            extraAuthErrorRejected = true
        }
        var zeroSignatureRejected = false
        do {
            _ = try LiveAuthValidation.p1363Signature(
                LiveAuthEncoding.encodeBase64URL(Data(repeating: 0, count: 64))
            )
        } catch {
            zeroSignatureRejected = true
        }
        var expiryBoundaryRejected = false
        do {
            try invitation.validate(freshAt: try LiveAuthTime.parse(invitation.expiresAt))
        } catch {
            expiryBoundaryRejected = true
        }
        return [
            "qr_round_trip": decoded == invitation,
            "pairing_proof": LiveAuthEncoding.encodeBase64URL(proof),
            "request_vector_match": canonical == expectedCanonical,
            "request_has_final_newline": canonical.last == 10,
            "duplicate_json_rejected": duplicateRejected,
            "extra_auth_error_rejected": extraAuthErrorRejected,
            "expiry_boundary_rejected": expiryBoundaryRejected,
            "noncanonical_base64_rejected": noncanonicalBase64Rejected,
            "nonfinite_json_rejected": nonfiniteJSONRejected,
            "zero_signature_rejected": zeroSignatureRejected,
        ]
    }

    private static func pairing(root: URL) async throws -> [String: Any] {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let secureStore = MemorySecureStore()
        let devicePrivate = try P256.Signing.PrivateKey(
            rawRepresentation: Data(repeating: 7, count: 32)
        )
        try secureStore.write(
            devicePrivate.rawRepresentation,
            account: "device-p256-private-key"
        )
        let identityStore = LiveDeviceIdentityStore(
            secureStore: secureStore,
            random: DeterministicRandom()
        )
        let grantStore = LiveGrantStore(secureStore: secureStore)
        let pendingStore = LivePendingPairingStore(secureStore: secureStore)
        let counterStore = LiveRequestCounterStore(
            stateURL: root.appendingPathComponent("counters.json")
        )
        let random = DeterministicRandom(start: 32)
        let client = LivePairingClient(
            identityStore: identityStore,
            grantStore: grantStore,
            pendingStore: pendingStore,
            counterStore: counterStore,
            random: random
        )
        let desktopPrivate = P256.Signing.PrivateKey()
        let desktopPublic = desktopPrivate.publicKey.x963Representation
        let now = try LiveAuthTime.parse("2026-07-30T10:00:00.000Z")
        let discovery = LiveDiscoveryIdentity(
            serviceType: LiveAuthContract.bonjourServiceType,
            serviceName: "World Studio Test",
            domain: LiveAuthContract.bonjourDomain
        )
        let invitation = LivePairingInvitation(
            schema: "capture_splat.live_pairing_invitation.v0.1",
            pairingID: try LiveAuthEncoding.randomID(
                prefix: "csp",
                bytes: Data(0..<16)
            ),
            mode: "qr",
            desktopID: LiveAuthEncoding.identity(prefix: "wsd", publicKeyX963: desktopPublic),
            desktopName: "World Studio Test",
            desktopPublicKeyBase64URL: LiveAuthEncoding.encodeBase64URL(desktopPublic),
            discovery: discovery,
            tlsCertificateSHA256: "sha256:" + String(repeating: "f", count: 64),
            pairingSecretBase64URL: LiveAuthEncoding.encodeBase64URL(Data(32..<64)),
            issuedAt: LiveAuthTime.string(now.addingTimeInterval(-1)),
            expiresAt: LiveAuthTime.string(now.addingTimeInterval(299)),
            permissions: LiveAuthContract.permissions,
            authority: LiveAuthContract.authority
        )
        let endpoint = LiveResolvedEndpoint(
            host: "192.168.1.20",
            port: 43128,
            discovery: discovery
        )
        let prepared = try await client.prepare(
            invitationURI: LivePairingClient.invitationURI(invitation),
            endpoint: endpoint,
            deviceName: "Capture Splat Test",
            appVersion: "0.1.0",
            now: now
        )
        let reloadedClient = LivePairingClient(
            identityStore: identityStore,
            grantStore: grantStore,
            pendingStore: LivePendingPairingStore(secureStore: secureStore),
            counterStore: counterStore,
            random: DeterministicRandom(start: 200)
        )
        let resumedPrepared = try await reloadedClient.resumePending(
            desktopID: invitation.desktopID
        )
        let identity = try await identityStore.publicIdentity()
        let approvalTime = now.addingTimeInterval(60)
        let grantPayload = LivePairingGrantPayload(
            schema: "capture_splat.live_pairing_grant_payload.v0.1",
            pairingID: invitation.pairingID,
            requestID: prepared.requestPayload.requestID,
            grantID: try LiveAuthEncoding.randomID(prefix: "csg", bytes: Data(64..<80)),
            pairingEpoch: 1,
            audience: LiveAuthContract.audience,
            desktopID: invitation.desktopID,
            deviceID: identity.deviceID,
            devicePublicKeyBase64URL: identity.publicKeyBase64URL,
            permissions: LiveAuthContract.permissions,
            authScheme: LiveAuthContract.authScheme,
            liveDiscovery: discovery,
            tlsCertificateSHA256: invitation.tlsCertificateSHA256,
            issuedAt: LiveAuthTime.string(approvalTime),
            notBefore: LiveAuthTime.string(approvalTime),
            expiresAt: LiveAuthTime.string(approvalTime.addingTimeInterval(86_400)),
            authority: LiveAuthContract.authority
        )
        let grantBytes = try LiveStrictJSON.canonicalData(grantPayload)
        let signature = try desktopPrivate.signature(
            for: LiveAuthContract.grantSignatureDomain + grantBytes
        ).rawRepresentation
        let envelope = LivePairingGrantEnvelope(
            schema: "capture_splat.live_pairing_grant_envelope.v0.1",
            payloadBase64URL: LiveAuthEncoding.encodeBase64URL(grantBytes),
            desktopSignatureBase64URL: LiveAuthEncoding.encodeBase64URL(signature)
        )
        var responseBody = try LiveStrictJSON.canonicalData(envelope)
        responseBody.append(10)
        let transport = ScriptedHTTPS(responses: [
            LiveHTTPResponse(statusCode: 200, body: responseBody, headers: [:]),
            LiveHTTPResponse(statusCode: 200, body: responseBody, headers: [:]),
        ])
        let stored = try await client.submitForTesting(
            resumedPrepared!,
            using: transport,
            clock: { approvalTime }
        )
        _ = try await client.submitForTesting(
            prepared,
            using: transport,
            clock: { now.addingTimeInterval(600) }
        )
        let reloaded = try await grantStore.load(
            desktopID: invitation.desktopID,
            currentAt: approvalTime
        )
        let pairingUploads = await transport.capturedUploads()
        let requestBodiesStable = pairingUploads.count == 2 && pairingUploads.allSatisfy {
            guard case .data(let body) = $0 else { return false }
            return body == prepared.canonicalRequestBody
        }

        let revokedBody = try LiveStrictJSON.canonicalData(LiveAuthErrorBody(
            schema: "capture_splat.live_auth_error.v0.1",
            code: "grant_revoked",
            retryable: false,
            message: "Grant was revoked."
        ))
        let authenticatedTransport = ScriptedHTTPS(responses: [
            LiveHTTPResponse(statusCode: 200, body: Data("{}".utf8), headers: [:]),
            LiveHTTPResponse(statusCode: 200, body: Data("{}".utf8), headers: [:]),
            LiveHTTPResponse(statusCode: 401, body: revokedBody, headers: [:]),
        ])
        let authenticatedClient = try LiveAuthenticatedHTTPClient.testing(
            endpoint: endpoint,
            desktopID: invitation.desktopID,
            certificateSHA256: invitation.tlsCertificateSHA256,
            identityStore: identityStore,
            grantStore: grantStore,
            counterStore: counterStore,
            transport: authenticatedTransport,
            random: DeterministicRandom(start: 96)
        )
        let authenticatedPath = "/api/capture-splat/live/v0.1/sessions/sender-fixture-01"
        let requestTime = approvalTime.addingTimeInterval(1)
        _ = try await authenticatedClient.validateSenderAuthorization(now: requestTime)
        _ = try await authenticatedClient.perform(
            method: "GET",
            path: authenticatedPath,
            body: .empty,
            now: requestTime
        )
        _ = try await authenticatedClient.perform(
            method: "GET",
            path: authenticatedPath,
            body: .empty,
            now: requestTime
        )
        var revocationRejected = false
        do {
            _ = try await authenticatedClient.perform(
                method: "GET",
                path: authenticatedPath,
                body: .empty,
                now: requestTime
            )
        } catch LiveAuthenticatedRequestError.auth(code: "grant_revoked", retryable: false) {
            revocationRejected = true
        }
        let authenticatedRequests = await authenticatedTransport.capturedRequests()
        let counters = authenticatedRequests.compactMap {
            $0.value(forHTTPHeaderField: "X-Capture-Splat-Counter")
        }
        let requestIDs = authenticatedRequests.compactMap {
            $0.value(forHTTPHeaderField: "X-Capture-Splat-Request")
        }
        let emptyGETs = authenticatedRequests.allSatisfy {
            $0.httpMethod == "GET"
                && $0.value(forHTTPHeaderField: "Content-Length") == "0"
                && $0.value(forHTTPHeaderField: "Content-Type") == nil
        }
        let lastCounter = try await counterStore.last(grantID: stored.payload.grantID)
        let revokedGrantRemoved = try await grantStore.load(
            desktopID: invitation.desktopID,
            currentAt: requestTime
        ) == nil

        try await grantStore.save(stored)
        let expiredTransport = ScriptedHTTPS(responses: [])
        let expiredClient = try LiveAuthenticatedHTTPClient.testing(
            endpoint: endpoint,
            desktopID: invitation.desktopID,
            certificateSHA256: invitation.tlsCertificateSHA256,
            identityStore: identityStore,
            grantStore: grantStore,
            counterStore: counterStore,
            transport: expiredTransport,
            random: DeterministicRandom(start: 128)
        )
        var expiryRejected = false
        do {
            _ = try await expiredClient.perform(
                method: "GET",
                path: authenticatedPath,
                body: .empty,
                now: try LiveAuthTime.parse(stored.payload.expiresAt)
            )
        } catch LiveAuthenticatedRequestError.auth(code: "grant_expired", retryable: false) {
            expiryRejected = true
        }
        let expiredGrantRemoved = try await grantStore.load(
            desktopID: invitation.desktopID
        ) == nil

        try await grantStore.save(stored)
        let mismatchedPinClient = try LiveAuthenticatedHTTPClient.testing(
            endpoint: endpoint,
            desktopID: invitation.desktopID,
            certificateSHA256: "sha256:" + String(repeating: "0", count: 64),
            identityStore: identityStore,
            grantStore: grantStore,
            counterStore: counterStore,
            transport: ScriptedHTTPS(responses: []),
            random: DeterministicRandom(start: 160)
        )
        var pinMismatchRejected = false
        do {
            _ = try await mismatchedPinClient.perform(
                method: "GET",
                path: authenticatedPath,
                body: .empty,
                now: requestTime
            )
        } catch LiveAuthenticatedRequestError.auth(code: "identity_mismatch", retryable: false) {
            pinMismatchRejected = true
        }
        let secretBytes = Data(invitation.pairingSecretBase64URL.utf8)
        let secretPersisted = secureStore.allValues().contains { $0.range(of: secretBytes) != nil }
        let pendingCleared = try await pendingStore.load(desktopID: invitation.desktopID) == nil
        return [
            "stable_device_id": identity == (try await identityStore.publicIdentity()),
            "grant_reloaded": reloaded == stored,
            "durable_counters": counters == ["1", "2", "3"] && lastCounter == 3,
            "fresh_retry_request_ids": Set(requestIDs).count == 3,
            "empty_get_rules": emptyGETs,
            "expired_grant_removed": expiredGrantRemoved,
            "expiry_rejected": expiryRejected,
            "revocation_rejected": revocationRejected,
            "revoked_grant_removed": revokedGrantRemoved,
            "identical_pairing_retry": requestBodiesStable,
            "pending_pairing_recovered": resumedPrepared?.canonicalRequestBody
                == prepared.canonicalRequestBody,
            "pending_pairing_cleared": pendingCleared,
            "pairing_secret_persisted": secretPersisted,
            "pin_mismatch_rejected": pinMismatchRejected,
        ]
    }

    private static func queue(root: URL) async throws -> [String: Any] {
        let capture = root.appendingPathComponent("capture", isDirectory: true)
        let state = root.appendingPathComponent("state/queue.json")
        try FileManager.default.createDirectory(at: capture, withIntermediateDirectories: true)
        let manifest = try write(
            capture,
            path: "capture.json",
            bytes: Data(#"{"schema":"capture_splat.v0.3"}"#.utf8)
        )
        let source1 = try write(capture, path: "rgb/frame-1.jpg", bytes: Data(repeating: 1, count: 8))
        let source2 = try write(capture, path: "rgb/frame-2.jpg", bytes: Data(repeating: 2, count: 8))
        let source3 = try write(capture, path: "rgb/frame-3.jpg", bytes: Data(repeating: 3, count: 8))
        let sessionFile = try write(
            capture,
            path: "live/session.json",
            bytes: try sessionMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                expectedCount: 2,
                manifest: manifest
            )
        )
        let frame1Metadata = try write(
            capture,
            path: "live/frame-1.json",
            bytes: try frameMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                sequence: 1,
                source: source1
            )
        )
        let frame2Metadata = try write(
            capture,
            path: "live/frame-2.json",
            bytes: try frameMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                sequence: 2,
                source: source2
            )
        )
        let frame3Metadata = try write(
            capture,
            path: "live/frame-3.json",
            bytes: try frameMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                sequence: 3,
                source: source3
            )
        )
        let session = try LiveSenderSessionReference(
            sessionID: "sender-fixture-01",
            expectedFrameCount: 2,
            metadata: try reference(capture, url: sessionFile, mediaType: "application/json"),
            authorization: try fixtureAuthorization()
        )
        let limits = try LiveSenderQueueLimits(
            maximumFrames: 2,
            maximumBytes: 2 * 1024 * 1024,
            maximumInFlight: 2
        )
        let senderQueue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: state,
            limits: limits,
            session: session
        )
        let frame1 = try frame(capture, sequence: 1, metadata: frame1Metadata, source: source1)
        let frame2 = try frame(capture, sequence: 2, metadata: frame2Metadata, source: source2)
        let frame3 = try frame(capture, sequence: 3, metadata: frame3Metadata, source: source3)
        let second = try await senderQueue.enqueue(frame2)
        let first = try await senderQueue.enqueue(frame1)
        let duplicate = try await senderQueue.enqueue(frame2)
        _ = try await senderQueue.setFinalization(
            LiveSenderFinalizationReference(sessionID: session.sessionID, finalSequenceID: 2)
        )

        let gapACK = try LiveSenderAcknowledgement(
            sessionID: session.sessionID,
            operation: .resume,
            status: .accepted,
            receivedCount: 1,
            contiguousCount: 0,
            pendingCount: 1,
            expectedFrameCount: 2,
            nextExpectedSequenceID: 1,
            missingRanges: [try LiveSenderMissingRange(start: 1, end: 1)],
            finalized: false
        )
        let gapResult = try await senderQueue.reconcile(gapACK)
        let missingFinalizationBlocked = try await senderQueue.finalizationForSend() == nil
        let reloaded = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: state,
            limits: limits,
            session: session
        )
        let restartPending = try await reloaded.snapshot().pendingSequenceIDs
        let acknowledgedDuplicate = try await reloaded.enqueue(frame2)
        let completeACK = try LiveSenderAcknowledgement(
            sessionID: session.sessionID,
            operation: .resume,
            status: .accepted,
            receivedCount: 2,
            contiguousCount: 2,
            pendingCount: 0,
            expectedFrameCount: 2,
            nextExpectedSequenceID: 3,
            missingRanges: [],
            finalized: false
        )
        _ = try await reloaded.reconcile(completeACK)
        let acknowledgedReloaded = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: state,
            limits: limits,
            session: session
        )
        let identicalAcknowledgedRetry = try await acknowledgedReloaded.enqueue(frame1)
        let conflictingSource = try write(
            capture,
            path: "rgb/frame-1-conflict.jpg",
            bytes: Data(repeating: 9, count: 8)
        )
        let conflictingMetadata = try write(
            capture,
            path: "live/frame-1-conflict.json",
            bytes: try frameMetadata(
                capture: capture,
                sessionID: session.sessionID,
                sequence: 1,
                source: conflictingSource
            )
        )
        var conflictingAcknowledgedRetryRejected = false
        do {
            _ = try await acknowledgedReloaded.enqueue(
                frame(
                    capture,
                    sequence: 1,
                    metadata: conflictingMetadata,
                    source: conflictingSource
                )
            )
        } catch LiveSenderQueueError.frameConflict(1) {
            conflictingAcknowledgedRetryRejected = true
        }
        let staleResult = try await reloaded.reconcile(gapACK)
        let finalizationReady = try await reloaded.finalizationForSend() != nil

        var contradictoryACKRejected = false
        do {
            _ = try await reloaded.reconcile(LiveSenderAcknowledgement(
                sessionID: session.sessionID,
                operation: .resume,
                status: .accepted,
                receivedCount: 1,
                contiguousCount: 1,
                pendingCount: 0,
                expectedFrameCount: 2,
                nextExpectedSequenceID: 2,
                missingRanges: [try LiveSenderMissingRange(start: 1, end: 1)],
                finalized: false
            ))
        } catch LiveSenderQueueError.invalidAcknowledgement {
            contradictoryACKRejected = true
        }
        var falseFinalizationRejected = false
        do {
            _ = try await reloaded.reconcile(LiveSenderAcknowledgement(
                sessionID: session.sessionID,
                operation: .resume,
                status: .finalized,
                receivedCount: 0,
                contiguousCount: 0,
                pendingCount: 0,
                expectedFrameCount: nil,
                nextExpectedSequenceID: 1,
                missingRanges: [],
                finalized: true
            ))
        } catch LiveSenderQueueError.invalidAcknowledgement {
            falseFinalizationRejected = true
        }
        var overflowACKRejected = false
        do {
            _ = try await reloaded.reconcile(LiveSenderAcknowledgement(
                sessionID: session.sessionID,
                operation: .resume,
                status: .accepted,
                receivedCount: Int.max,
                contiguousCount: Int.max,
                pendingCount: 0,
                expectedFrameCount: nil,
                nextExpectedSequenceID: Int.max,
                missingRanges: [],
                finalized: false
            ))
        } catch LiveSenderQueueError.invalidAcknowledgement {
            overflowACKRejected = true
        }

        let openSessionFile = try write(
            capture,
            path: "live/session-open.json",
            bytes: try sessionMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                expectedCount: nil,
                manifest: manifest
            )
        )
        let openSession = try LiveSenderSessionReference(
            sessionID: "sender-fixture-01",
            expectedFrameCount: nil,
            metadata: try reference(
                capture,
                url: openSessionFile,
                mediaType: "application/json"
            ),
            authorization: session.authorization
        )
        let frameBeforeFinalization = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: root.appendingPathComponent("final-before/queue.json"),
            limits: try LiveSenderQueueLimits(
                maximumFrames: 4,
                maximumBytes: 2 * 1024 * 1024,
                maximumInFlight: 2
            ),
            session: openSession
        )
        _ = try await frameBeforeFinalization.enqueue(frame3)
        var existingSequenceAboveFinalRejected = false
        do {
            try await frameBeforeFinalization.setFinalization(
                LiveSenderFinalizationReference(
                    sessionID: session.sessionID,
                    finalSequenceID: 2
                )
            )
        } catch LiveSenderQueueError.finalizationConflict {
            existingSequenceAboveFinalRejected = true
        }
        let frameAfterFinalization = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: root.appendingPathComponent("final-after/queue.json"),
            limits: try LiveSenderQueueLimits(
                maximumFrames: 4,
                maximumBytes: 2 * 1024 * 1024,
                maximumInFlight: 2
            ),
            session: openSession
        )
        try await frameAfterFinalization.setFinalization(
            LiveSenderFinalizationReference(
                sessionID: session.sessionID,
                finalSequenceID: 2
            )
        )
        var laterSequenceAboveFinalRejected = false
        do {
            _ = try await frameAfterFinalization.enqueue(frame3)
        } catch LiveSenderQueueError.finalizationConflict {
            laterSequenceAboveFinalRejected = true
        }

        let capacityQueue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: root.appendingPathComponent("capacity/queue.json"),
            limits: try LiveSenderQueueLimits(
                maximumFrames: 1,
                maximumBytes: 2 * 1024 * 1024,
                maximumInFlight: 1
            ),
            session: session
        )
        _ = try await capacityQueue.enqueue(frame1)
        let capacity = try await capacityQueue.enqueue(frame2)

        let conflictMetadata = try write(
            capture,
            path: "live/frame-2-conflict.json",
            bytes: try frameMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                sequence: 2,
                source: source2,
                timestamp: 99
            )
        )
        var conflictRejected = false
        do {
            _ = try await senderQueue.enqueue(
                frame(capture, sequence: 2, metadata: conflictMetadata, source: source2)
            )
        } catch LiveSenderQueueError.frameConflict(2) {
            conflictRejected = true
        }

        let unsafePaths = [
            "../escape",
            "/absolute/path",
            "file:///capture/frame.jpg",
            "rgb\\frame.jpg",
        ]
        var rejectedUnsafePaths: [String] = []
        for path in unsafePaths {
            do {
                _ = try LiveSenderFileReference(
                    relativePath: path,
                    sizeBytes: 1,
                    sha256: "sha256:" + String(repeating: "0", count: 64),
                    mediaType: "image/jpeg"
                )
            } catch {
                rejectedUnsafePaths.append(path)
            }
        }
        let outside = try write(root, path: "outside.bin", bytes: Data([1]))
        let link = capture.appendingPathComponent("rgb/link.bin")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: outside)
        var symlinkRejected = false
        do {
            let linked = try LiveSenderFileReference(
                relativePath: "rgb/link.bin",
                sizeBytes: 1,
                sha256: LiveFileDigest.sha256(url: outside),
                mediaType: "application/octet-stream"
            )
            _ = try await reloaded.verifiedFileURL(for: linked)
        } catch {
            symlinkRejected = true
        }

        let tamperSource = try write(
            capture,
            path: "rgb/frame-tamper.jpg",
            bytes: Data(repeating: 8, count: 8)
        )
        let tamperMetadata = try write(
            capture,
            path: "live/frame-tamper.json",
            bytes: try frameMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                sequence: 1,
                source: tamperSource
            )
        )
        let tamperQueue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: root.appendingPathComponent("tamper/queue.json"),
            limits: limits,
            session: session
        )
        _ = try await tamperQueue.enqueue(
            frame(capture, sequence: 1, metadata: tamperMetadata, source: tamperSource)
        )
        try Data(repeating: 9, count: 8).write(to: tamperSource, options: .atomic)
        var checksumMismatchRejected = false
        do {
            _ = try await tamperQueue.pendingSelection()
        } catch LiveSenderQueueError.sourceChecksumMismatch {
            checksumMismatchRejected = true
        }

        let corruptState = root.appendingPathComponent("corrupt/queue.json")
        let corruptQueue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: corruptState,
            limits: limits,
            session: session
        )
        _ = try await corruptQueue.enqueue(frame1)
        try Data(#"{"truncated":"#.utf8).write(to: corruptState, options: .atomic)
        var corruptStateRejected = false
        do {
            _ = try await LiveSenderQueue.open(
                captureRoot: capture,
                stateURL: corruptState,
                limits: limits,
                session: session
            )
        } catch LiveSenderQueueError.stateCorrupt {
            corruptStateRejected = true
        }

        let finalizedACK = try LiveSenderAcknowledgement(
            sessionID: session.sessionID,
            operation: .finalize,
            status: .finalized,
            receivedCount: 2,
            contiguousCount: 2,
            pendingCount: 0,
            expectedFrameCount: 2,
            nextExpectedSequenceID: 3,
            missingRanges: [],
            finalized: true
        )
        _ = try await reloaded.reconcile(finalizedACK)
        var postFinalizationRejected = false
        do {
            _ = try await reloaded.enqueue(frame1)
        } catch LiveSenderQueueError.queueFinalized {
            postFinalizationRejected = true
        }
        let finalSnapshot = try await reloaded.snapshot()
        return [
            "out_of_order_accepted": second.disposition.rawValue == "accepted"
                && first.disposition.rawValue == "accepted",
            "capacity_disposition": capacity.disposition.rawValue,
            "duplicate_disposition": duplicate.disposition.rawValue,
            "gap_acknowledged": gapResult.acknowledgedSequenceIDs,
            "restart_pending": restartPending,
            "acknowledged_retry_disposition": acknowledgedDuplicate.disposition.rawValue,
            "stale_progress_ignored": staleResult.snapshot.receiverMissingRanges.isEmpty,
            "contradictory_ack_rejected": contradictoryACKRejected,
            "false_finalization_rejected": falseFinalizationRejected,
            "final_sequence_bound_enforced": existingSequenceAboveFinalRejected
                && laterSequenceAboveFinalRejected,
            "overflow_ack_rejected": overflowACKRejected,
            "missing_finalization_blocked": missingFinalizationBlocked,
            "finalization_ready": finalizationReady,
            "unsafe_paths_rejected": rejectedUnsafePaths.count == unsafePaths.count,
            "symlink_rejected": symlinkRejected,
            "checksum_mismatch_rejected": checksumMismatchRejected,
            "corrupt_state_rejected": corruptStateRejected,
            "conflict_rejected": conflictRejected,
            "conflicting_acknowledged_retry_rejected": conflictingAcknowledgedRetryRejected,
            "source_preserved": FileManager.default.fileExists(atPath: source1.path)
                && FileManager.default.fileExists(atPath: source2.path),
            "identical_acknowledged_retry_disposition": identicalAcknowledgedRetry.disposition.rawValue,
            "finalized": finalSnapshot.finalized,
            "post_finalization_rejected": postFinalizationRejected,
        ]
    }

    private static func progressive(root: URL) async throws -> [String: Any] {
        let capture = root.appendingPathComponent("progressive-capture", isDirectory: true)
        let stateURL = root.appendingPathComponent("progressive-state/queue.json")
        try FileManager.default.createDirectory(at: capture, withIntermediateDirectories: true)
        let seed = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
        let sessionID = try LiveSenderProgressiveSessionIdentity.sessionID(
            sourceSessionSeedBase64URL: seed
        )
        let expectedSessionID = "csl_SMOhjzjH7dE8x3yB5A0KBAo4YL6A4IzY1U570kVX_D8"
        let sessionURL = try write(
            capture,
            path: "live/session.json",
            bytes: try progressiveSessionMetadata(sessionID: sessionID, seed: seed)
        )
        let authorization = try fixtureAuthorization()
        let session = try LiveSenderSessionReference(
            sessionID: sessionID,
            expectedFrameCount: nil,
            metadata: try reference(capture, url: sessionURL, mediaType: "application/json"),
            authorization: authorization
        )
        let limits = try LiveSenderQueueLimits(
            maximumFrames: 4,
            maximumBytes: 4 * 1024 * 1024,
            maximumInFlight: 2
        )
        let manifestAbsentAtOpen = !FileManager.default.fileExists(
            atPath: capture.appendingPathComponent("capture.json").path
        )
        let queue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: stateURL,
            limits: limits,
            session: session
        )
        let preManifestReceiver = ProgressiveReceiverHarness(
            sessionID: sessionID,
            expectedCount: 0,
            authorization: authorization
        )
        let preManifestRun = await LiveSender(
            queue: queue,
            requester: preManifestReceiver,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 1,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            sleeper: ImmediateSleeper()
        ).runOnce(environment: {
            LiveSenderEnvironment(
                isForeground: true,
                networkAvailable: true,
                receiverAvailable: true,
                availableStorageBytes: 1_000,
                thermalState: .nominal
            )
        })
        let preManifestCalls = await preManifestReceiver.evidence().calls
        let preManifestSessionSent = preManifestRun.status == .awaitingFrames
            && preManifestCalls.count == 2
            && preManifestCalls[0].hasPrefix("PUT ")
            && preManifestCalls[1].hasPrefix("GET ")

        let sessionBytes = try Data(contentsOf: sessionURL)
        try Data(#"{"schema":"tampered"}"#.utf8).write(to: sessionURL, options: .atomic)
        var immutableSessionRejected = false
        do {
            _ = try await queue.sessionForSend()
        } catch LiveSenderQueueError.sourceSizeMismatch {
            immutableSessionRejected = true
        } catch LiveSenderQueueError.sourceChecksumMismatch {
            immutableSessionRejected = true
        }
        try sessionBytes.write(to: sessionURL, options: .atomic)

        let invalidSessionURL = try write(
            capture,
            path: "live/invalid-session.json",
            bytes: try progressiveSessionMetadata(
                sessionID: sessionID,
                seed: "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA"
            )
        )
        let invalidSession = try LiveSenderSessionReference(
            sessionID: sessionID,
            expectedFrameCount: nil,
            metadata: try reference(
                capture,
                url: invalidSessionURL,
                mediaType: "application/json"
            ),
            authorization: authorization
        )
        var mismatchedSeedRejected = false
        do {
            _ = try await LiveSenderQueue.open(
                captureRoot: capture,
                stateURL: root.appendingPathComponent("invalid-progressive/queue.json"),
                limits: limits,
                session: invalidSession
            )
        } catch LiveSenderQueueError.invalidReference {
            mismatchedSeedRejected = true
        }

        let absentManifest = try LiveSenderSourceManifestReference(
            path: "capture.json",
            sizeBytes: 1,
            sha256: "sha256:" + String(repeating: "0", count: 64),
            schema: "capture_splat.v0.3"
        )
        var missingManifestRejected = false
        do {
            try await queue.setFinalization(LiveSenderFinalizationReference(
                sessionID: sessionID,
                finalSequenceID: 2,
                sourceManifest: absentManifest
            ))
        } catch LiveSenderQueueError.sourceMissing {
            missingManifestRejected = true
        }

        let manifestBytes = Data(#"{"frames":[],"schema":"capture_splat.v0.3"}"#.utf8)
        let manifestURL = try write(
            capture,
            path: "capture.json",
            bytes: manifestBytes
        )
        let manifestFile = try reference(
            capture,
            url: manifestURL,
            mediaType: "application/json"
        )
        let manifest = try LiveSenderSourceManifestReference(
            path: manifestFile.relativePath,
            sizeBytes: manifestFile.sizeBytes,
            sha256: manifestFile.sha256,
            schema: "capture_splat.v0.3"
        )
        var corruptManifestRejected = true
        for invalidBytes in [
            Data(#"{"schema":"capture_splat.v0.3""#.utf8),
            Data(#"{"schema":"capture_splat.v0.3","schema":"capture_splat.v0.3"}"#.utf8),
            Data(#"{"schema":"capture_splat.v0.3","value":NaN}"#.utf8),
        ] {
            try invalidBytes.write(to: manifestURL, options: .atomic)
            let invalidFile = try reference(
                capture,
                url: manifestURL,
                mediaType: "application/json"
            )
            do {
                try await queue.setFinalization(LiveSenderFinalizationReference(
                    sessionID: sessionID,
                    finalSequenceID: 2,
                    sourceManifest: try LiveSenderSourceManifestReference(
                        path: invalidFile.relativePath,
                        sizeBytes: invalidFile.sizeBytes,
                        sha256: invalidFile.sha256,
                        schema: "capture_splat.v0.3"
                    )
                ))
                corruptManifestRejected = false
            } catch LiveSenderQueueError.invalidReference {
                continue
            }
        }
        try manifestBytes.write(to: manifestURL, options: .atomic)
        var manifestSchemaMismatchRejected = false
        do {
            try await queue.setFinalization(LiveSenderFinalizationReference(
                sessionID: sessionID,
                finalSequenceID: 2,
                sourceManifest: try LiveSenderSourceManifestReference(
                    path: manifestFile.relativePath,
                    sizeBytes: manifestFile.sizeBytes,
                    sha256: manifestFile.sha256,
                    schema: "capture_splat.v0.4"
                )
            ))
        } catch LiveSenderQueueError.invalidReference {
            manifestSchemaMismatchRejected = true
        }
        try await queue.setFinalization(LiveSenderFinalizationReference(
            sessionID: sessionID,
            finalSequenceID: 2,
            sourceManifest: manifest
        ))
        var conflictingBindingRejected = false
        do {
            try await queue.setFinalization(LiveSenderFinalizationReference(
                sessionID: sessionID,
                finalSequenceID: 2,
                sourceManifest: LiveSenderSourceManifestReference(
                    path: "capture.json",
                    sizeBytes: manifest.sizeBytes,
                    sha256: manifest.sha256,
                    schema: "capture_splat.v0.4"
                )
            ))
        } catch LiveSenderQueueError.finalizationConflict {
            conflictingBindingRejected = true
        }

        try Data(#"{"schema":"capture_splat.v0.3""#.utf8)
            .write(to: manifestURL, options: .atomic)
        var corruptManifestRestartRejected = false
        do {
            _ = try await LiveSenderQueue.open(
                captureRoot: capture,
                stateURL: stateURL,
                limits: limits,
                session: session
            )
        } catch LiveSenderQueueError.invalidReference {
            corruptManifestRestartRejected = true
        } catch LiveSenderQueueError.sourceSizeMismatch {
            corruptManifestRestartRejected = true
        } catch LiveSenderQueueError.sourceChecksumMismatch {
            corruptManifestRestartRejected = true
        }
        try manifestBytes.write(to: manifestURL, options: .atomic)
        let reopened = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: stateURL,
            limits: limits,
            session: session
        )
        let reopenedSession = try await reopened.sessionForSend()
        let reopenedSessionURL = try await reopened.verifiedFileURL(
            for: reopenedSession.metadata
        )
        let reopenedSessionBytes = try Data(contentsOf: reopenedSessionURL)
        let restartPreservedBinding = reopenedSession == session
            && reopenedSessionBytes == sessionBytes
        let nilExpectedACK = try LiveSenderAcknowledgement(
            sessionID: sessionID,
            operation: .resume,
            status: .accepted,
            receivedCount: 2,
            contiguousCount: 2,
            pendingCount: 0,
            expectedFrameCount: nil,
            nextExpectedSequenceID: 3,
            missingRanges: [],
            finalized: false
        )
        _ = try await reopened.reconcile(nilExpectedACK)
        let durableExpectedACK = try LiveSenderAcknowledgement(
            sessionID: sessionID,
            operation: .resume,
            status: .accepted,
            receivedCount: 2,
            contiguousCount: 2,
            pendingCount: 0,
            expectedFrameCount: 2,
            nextExpectedSequenceID: 3,
            missingRanges: [],
            finalized: false
        )
        _ = try await reopened.reconcile(durableExpectedACK)
        let staleNilResult = try await reopened.reconcile(LiveSenderAcknowledgement(
            sessionID: sessionID,
            operation: .resume,
            status: .accepted,
            receivedCount: 1,
            contiguousCount: 1,
            pendingCount: 0,
            expectedFrameCount: nil,
            nextExpectedSequenceID: 2,
            missingRanges: [],
            finalized: false
        ))
        let confirmedSource = try write(
            capture,
            path: "rgb/confirmed-frame-2.jpg",
            bytes: Data(repeating: 2, count: 8)
        )
        let confirmedMetadata = try write(
            capture,
            path: "live/confirmed-frame-2.json",
            bytes: try frameMetadata(
                capture: capture,
                sessionID: sessionID,
                sequence: 2,
                source: confirmedSource
            )
        )
        let staleNilDisposition = try await reopened.enqueue(frame(
            capture,
            sessionID: sessionID,
            sequence: 2,
            metadata: confirmedMetadata,
            source: confirmedSource
        )).disposition
        var differingExpectedRejected = false
        do {
            _ = try await reopened.reconcile(LiveSenderAcknowledgement(
                sessionID: sessionID,
                operation: .resume,
                status: .accepted,
                receivedCount: 2,
                contiguousCount: 2,
                pendingCount: 0,
                expectedFrameCount: 3,
                nextExpectedSequenceID: 3,
                missingRanges: [try LiveSenderMissingRange(start: 3, end: 3)],
                finalized: false
            ))
        } catch LiveSenderQueueError.invalidAcknowledgement {
            differingExpectedRejected = true
        }

        try Data(#"{"frames":[],"schema":"capture_splat.v0.4"}"#.utf8)
            .write(to: manifestURL, options: .atomic)
        var manifestReverifiedBeforeSend = false
        do {
            _ = try await reopened.finalizationForSend()
        } catch LiveSenderQueueError.sourceChecksumMismatch {
            manifestReverifiedBeforeSend = true
        }
        try manifestBytes.write(to: manifestURL, options: .atomic)
        let finalizationReadyAfterRestore = try await reopened.finalizationForSend() != nil

        let receiver = ProgressiveReceiverHarness(
            sessionID: sessionID,
            expectedCount: 2,
            authorization: authorization,
            sourceManifest: manifest
        )
        let environment = LiveSenderEnvironment(
            isForeground: true,
            networkAvailable: true,
            receiverAvailable: true,
            availableStorageBytes: 1_000,
            thermalState: .nominal
        )
        let first = await LiveSender(
            queue: reopened,
            requester: receiver,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 1,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            sleeper: ImmediateSleeper()
        ).runOnce(environment: { environment })
        let restarted = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: stateURL,
            limits: limits,
            session: session
        )
        let recovered = await LiveSender(
            queue: restarted,
            requester: receiver,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy(),
            sleeper: ImmediateSleeper()
        ).runOnce(environment: { environment })
        let callsBeforeIdempotentRun = await receiver.evidence().calls.count
        let finalizedQueue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: stateURL,
            limits: limits,
            session: session
        )
        let idempotent = await LiveSender(
            queue: finalizedQueue,
            requester: receiver,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy(),
            sleeper: ImmediateSleeper()
        ).runOnce(environment: { environment })
        let evidence = await receiver.evidence()

        return [
            "derived_session_id_matches": sessionID == expectedSessionID,
            "opened_before_manifest": manifestAbsentAtOpen,
            "immutable_session_rejected": immutableSessionRejected,
            "mismatched_seed_rejected": mismatchedSeedRejected,
            "missing_manifest_rejected": missingManifestRejected,
            "conflicting_binding_rejected": conflictingBindingRejected,
            "corrupt_manifest_rejected": corruptManifestRejected,
            "corrupt_manifest_restart_rejected": corruptManifestRestartRejected,
            "manifest_schema_mismatch_rejected": manifestSchemaMismatchRejected,
            "restart_preserved_binding": restartPreservedBinding,
            "pre_manifest_session_sent": preManifestSessionSent,
            "expected_count_promoted": finalizationReadyAfterRestore,
            "stale_nil_ignored": staleNilResult.snapshot.finalizationPending
                && staleNilDisposition == .duplicate,
            "differing_expected_rejected": differingExpectedRejected,
            "manifest_reverified_before_send": manifestReverifiedBeforeSend,
            "lost_finalize_ack_interrupted": first.status == .interrupted,
            "restart_resumed_finalization": recovered.status == .finalized && recovered.finalized,
            "finalize_payload_valid": evidence.finalizePayloadValid,
            "idempotent_after_finalize": idempotent.status == .finalized
                && evidence.calls.count == callsBeforeIdempotentRun,
        ]
    }

    private static func engine(root: URL) async throws -> [String: Any] {
        let capture = root.appendingPathComponent("capture", isDirectory: true)
        try FileManager.default.createDirectory(at: capture, withIntermediateDirectories: true)
        let manifest = try write(
            capture,
            path: "capture.json",
            bytes: Data(#"{"schema":"capture_splat.v0.3"}"#.utf8)
        )
        let sessionURL = try write(
            capture,
            path: "live/session.json",
            bytes: try sessionMetadata(
                capture: capture,
                sessionID: "sender-fixture-01",
                expectedCount: 2,
                manifest: manifest
            )
        )
        let session = try LiveSenderSessionReference(
            sessionID: "sender-fixture-01",
            expectedFrameCount: 2,
            metadata: try reference(capture, url: sessionURL, mediaType: "application/json"),
            authorization: try fixtureAuthorization()
        )
        let queue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: root.appendingPathComponent("state/queue.json"),
            limits: try LiveSenderQueueLimits(
                maximumFrames: 4,
                maximumBytes: 4 * 1024 * 1024,
                maximumInFlight: 2
            ),
            session: session
        )
        for sequence in 1...2 {
            let source = try write(
                capture,
                path: "rgb/frame-\(sequence).jpg",
                bytes: Data(repeating: UInt8(sequence), count: 16)
            )
            let metadata = try write(
                capture,
                path: "live/frame-\(sequence).json",
                bytes: try frameMetadata(
                    capture: capture,
                    sessionID: "sender-fixture-01",
                    sequence: sequence,
                    source: source
                )
            )
            _ = try await queue.enqueue(
                try frame(capture, sequence: sequence, metadata: metadata, source: source)
            )
        }
        try await queue.setFinalization(
            LiveSenderFinalizationReference(sessionID: session.sessionID, finalSequenceID: 2)
        )
        let authorization = try fixtureAuthorization()
        let receiver = ReceiverHarness(expectedCount: 2, authorization: authorization)
        let wrongDesktop = ReceiverHarness(
            expectedCount: 2,
            authorization: try fixtureAuthorization(desktopSeed: 3)
        )
        let wrongDevice = ReceiverHarness(
            expectedCount: 2,
            authorization: try fixtureAuthorization(deviceSeed: 4)
        )
        let wrongDesktopSummary = await LiveSender(
            queue: queue,
            requester: wrongDesktop,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy()
        ).runOnce(
            environment: { LiveSenderEnvironment(
                isForeground: true,
                networkAvailable: true,
                receiverAvailable: true,
                availableStorageBytes: 1_000,
                thermalState: .nominal
            ) }
        )
        let wrongDeviceSummary = await LiveSender(
            queue: queue,
            requester: wrongDevice,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy()
        ).runOnce(
            environment: { LiveSenderEnvironment(
                isForeground: true,
                networkAvailable: true,
                receiverAvailable: true,
                availableStorageBytes: 1_000,
                thermalState: .nominal
            ) }
        )
        let wrongDesktopEvidence = await wrongDesktop.evidence()
        let wrongDeviceEvidence = await wrongDevice.evidence()
        let sender = LiveSender(
            queue: queue,
            requester: receiver,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 2,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            sleeper: ImmediateSleeper()
        )
        let competingSender = LiveSender(
            queue: queue,
            requester: receiver,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy(
                maximumAttempts: 2,
                initialDelayMilliseconds: 0,
                maximumDelayMilliseconds: 0
            ),
            sleeper: ImmediateSleeper()
        )
        let environment = LiveSenderEnvironment(
            isForeground: true,
            networkAvailable: true,
            receiverAvailable: true,
            availableStorageBytes: 1_000,
            thermalState: .nominal
        )
        async let firstRun = sender.runOnceDetailed(
            environment: { environment },
            clock: { try! LiveAuthTime.parse("2026-07-30T10:00:00.000Z") }
        )
        async let competingRun = competingSender.runOnceDetailed(
            environment: { environment },
            clock: { try! LiveAuthTime.parse("2026-07-30T10:00:00.000Z") }
        )
        let initialResults = await (firstRun, competingRun)
        let initialSummaries = (initialResults.0.summary, initialResults.1.summary)
        await receiver.allowFinalizationResponses()
        let recovery = await sender.runOnce(
            environment: { environment },
            clock: { try! LiveAuthTime.parse("2026-07-30T10:00:01.000Z") }
        )
        let callsBeforeFinalizedRerun = await receiver.evidence().calls.count
        let reopenedFinalizedQueue = try await LiveSenderQueue.open(
            captureRoot: capture,
            stateURL: root.appendingPathComponent("state/queue.json"),
            limits: try LiveSenderQueueLimits(
                maximumFrames: 4,
                maximumBytes: 4 * 1024 * 1024,
                maximumInFlight: 2
            ),
            session: session
        )
        let finalizedRerun = await LiveSender(
            queue: reopenedFinalizedQueue,
            requester: receiver,
            policy: try LiveSenderPolicy(minimumAvailableStorageBytes: 1),
            retryPolicy: try LiveSenderRetryPolicy()
        ).runOnce(
            environment: { environment },
            clock: { try! LiveAuthTime.parse("2026-07-30T10:00:02.000Z") }
        )
        let evidence = await receiver.evidence()
        let active = [initialSummaries.0, initialSummaries.1].first {
            $0.status != .idle
        }!
        let activeResult = [initialResults.0, initialResults.1].first {
            $0.summary.status != .idle
        }!
        return [
            "initial_statuses": [initialSummaries.0.status.rawValue, initialSummaries.1.status.rawValue]
                .sorted(),
            "recovery_status": recovery.status.rawValue,
            "finalized": recovery.finalized,
            "queued_frames": recovery.queuedFrameCount,
            "attempted_frames": active.attemptedFrameCount,
            "acknowledged_frames": active.acknowledgedFrameCount,
            "maximum_concurrency": evidence.maximumActive,
            "lost_ack_retried": evidence.calls.filter { $0.contains("/assets/source") }.count == 3,
            "lost_finalization_ack_resumed": evidence.calls.filter {
                $0.hasSuffix("/finalize")
            }.count == 2,
            "post_finalization_idempotent": finalizedRerun.status == .finalized
                && finalizedRerun.finalized
                && evidence.calls.count == callsBeforeFinalizedRerun,
            "v0_1_finalize_payload_valid": evidence.finalizePayloadValid,
            "resume_before_frames": evidence.calls.firstIndex(where: { $0.hasPrefix("GET ") })!
                < evidence.calls.firstIndex(where: { $0.contains("/frames/") })!,
            "authorization_owner_enforced": wrongDesktopSummary.status == .interrupted
                && wrongDeviceSummary.status == .interrupted
                && wrongDesktopEvidence.calls.isEmpty
                && wrongDeviceEvidence.calls.isEmpty,
            "interruption_disposition": activeResult.interruptionDisposition.rawValue,
        ]
    }

    private static func policy() throws -> [String: Any] {
        let policy = try LiveSenderPolicy(minimumAvailableStorageBytes: 100)
        func reason(
            foreground: Bool = true,
            network: Bool = true,
            receiver: Bool = true,
            storage: Int64 = 1_000,
            thermal: LiveSenderThermalState = .nominal
        ) -> String {
            policy.pauseReason(for: LiveSenderEnvironment(
                isForeground: foreground,
                networkAvailable: network,
                receiverAvailable: receiver,
                availableStorageBytes: storage,
                thermalState: thermal
            ))?.rawValue ?? "ready"
        }
        return [
            "ready": reason(),
            "background": reason(foreground: false),
            "network": reason(network: false),
            "receiver": reason(receiver: false),
            "storage": reason(storage: 99),
            "serious": reason(thermal: .serious),
            "critical": reason(thermal: .critical),
            "retryable_error": LiveSender.testInterruptionDisposition(
                for: LiveAuthenticatedRequestError.auth(
                    code: "receiver_busy",
                    retryable: true
                )
            ).rawValue,
            "blocked_error": LiveSender.testInterruptionDisposition(
                for: LiveAuthenticatedRequestError.auth(
                    code: "grant_revoked",
                    retryable: false
                )
            ).rawValue,
            "queue_error": LiveSender.testInterruptionDisposition(
                for: LiveSenderQueueError.sourceChecksumMismatch("rgb/frame.jpg")
            ).rawValue,
            "contract_error": LiveSender.testInterruptionDisposition(
                for: LiveAuthContractError.invalid("invalid contract")
            ).rawValue,
            "cancelled_error": LiveSender.testInterruptionDisposition(
                for: CancellationError()
            ).rawValue,
            "unknown_error": LiveSender.testInterruptionDisposition(
                for: NSError(domain: "probe", code: 1)
            ).rawValue,
            "failure_priority": LiveSender.testPreferredFailureDisposition([
                .retryable,
                .blocked,
            ])?.rawValue ?? "missing",
        ]
    }

    private static func sessionMetadata(
        capture: URL,
        sessionID: String,
        expectedCount: Int?,
        manifest: URL
    ) throws -> Data {
        let manifestReference = try reference(
            capture,
            url: manifest,
            mediaType: "application/json"
        )
        var document: [String: Any] = [
            "schema": "capture_splat.live_session.v0.1",
            "session_id": sessionID,
            "created_at": "2026-07-30T10:00:00.000Z",
            "source_manifest": [
                "path": manifestReference.relativePath,
                "sha256": manifestReference.sha256,
                "size_bytes": manifestReference.sizeBytes,
                "schema": "capture_splat.v0.3",
            ],
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
        ]
        if let expectedCount {
            document["expected_frame_count"] = expectedCount
        }
        return try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
    }

    private static func progressiveSessionMetadata(
        sessionID: String,
        seed: String
    ) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schema": "capture_splat.live_session.v0.2",
            "session_id": sessionID,
            "created_at": "2026-07-30T10:00:00.000Z",
            "source_session_seed_b64u": seed,
            "expected_frame_count": NSNull(),
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

    private static func frameMetadata(
        capture: URL,
        sessionID: String,
        sequence: Int,
        source: URL,
        timestamp: Double? = nil
    ) throws -> Data {
        let sourceReference = try reference(capture, url: source, mediaType: "image/jpeg")
        return try JSONSerialization.data(withJSONObject: [
            "schema": "capture_splat.live_frame.v0.1",
            "session_id": sessionID,
            "sequence_id": sequence,
            "timestamp": [
                "value": timestamp ?? Double(sequence),
                "clock_domain": "arkit_session",
            ],
            "source_frame": [
                "path": sourceReference.relativePath,
                "sha256": sourceReference.sha256,
                "size_bytes": sourceReference.sizeBytes,
                "media_type": sourceReference.mediaType,
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
                1.0, 0.0, 0.0, Double(sequence),
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "coordinate_frame": "arkit_world",
            "tracking": ["state": "normal"],
            "quality": [
                "accepted": true,
                "score": 0.9,
                "reason": "useful_keyframe",
            ],
        ], options: [.sortedKeys])
    }

    private static func frame(
        _ capture: URL,
        sessionID: String = "sender-fixture-01",
        sequence: Int,
        metadata: URL,
        source: URL
    ) throws -> LiveSenderFrameReference {
        try LiveSenderFrameReference(
            sessionID: sessionID,
            sequenceID: sequence,
            metadata: reference(capture, url: metadata, mediaType: "application/json"),
            assets: [
                LiveSenderAssetReference(
                    role: .source,
                    file: try reference(capture, url: source, mediaType: "image/jpeg")
                ),
            ]
        )
    }

    private static func fixtureAuthorization(
        desktopSeed: UInt8 = 1,
        deviceSeed: UInt8 = 2
    ) throws -> LiveSenderAuthorizationBinding {
        try LiveSenderAuthorizationBinding(
            desktopID: LiveAuthEncoding.identity(
                prefix: "wsd",
                publicKeyX963: Data(repeating: desktopSeed, count: 65)
            ),
            deviceID: LiveAuthEncoding.identity(
                prefix: "csd",
                publicKeyX963: Data(repeating: deviceSeed, count: 65)
            )
        )
    }

    private static func reference(
        _ capture: URL,
        url: URL,
        mediaType: String
    ) throws -> LiveSenderFileReference {
        let relative = String(url.path.dropFirst(capture.path.count + 1))
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        guard let size = attributes[.size] as? NSNumber else {
            throw LiveSenderQueueError.sourceMissing(relative)
        }
        return try LiveSenderFileReference(
            relativePath: relative,
            sizeBytes: size.int64Value,
            sha256: LiveFileDigest.sha256(url: url),
            mediaType: mediaType
        )
    }

    @discardableResult
    private static func write(_ root: URL, path: String, bytes: Data) throws -> URL {
        let url = root.appendingPathComponent(path)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try bytes.write(to: url, options: .atomic)
        return url
    }
}

private extension NSLock {
    func withLock<T>(_ operation: () -> T) -> T {
        lock()
        defer { unlock() }
        return operation()
    }
}

import CryptoKit
import Foundation

enum LiveAuthContract {
    static let authScheme = "p256-sha256-ieee-p1363-v0.1"
    static let audience = "capture_splat.live.v0.1"
    static let authority = "proposal_only"
    static let bonjourServiceType = "_capturesplat._tcp"
    static let bonjourDomain = "local."
    static let pairingAPIRoot = "/api/capture-splat/pairing/v0.1"
    static let liveAPIRoot = "/api/capture-splat/live/v0.1"
    static let qrPrefix = "capture-splat://pair/"
    static let maximumQRBytes = 4096
    static let maximumPayloadBytes = 8192
    static let requestSignatureDomain = Data("CAPTURE-SPLAT-PAIRING-REQUEST-V1".utf8) + Data([0])
    static let proofDomain = Data("CAPTURE-SPLAT-PAIRING-PROOF-V1".utf8) + Data([0])
    static let grantSignatureDomain = Data("CAPTURE-SPLAT-PAIRING-GRANT-V1".utf8) + Data([0])
    static let authenticatedRequestDomain = "CAPTURE-SPLAT-AUTH-V1"
    static let permissions: [LivePermission] = [
        .receiverStatus,
        .sessionCreate,
        .sessionResume,
        .framePut,
        .assetPut,
        .sessionFinalize,
    ]
}

enum LiveAuthContractError: Error, Equatable, LocalizedError {
    case invalid(String)

    var errorDescription: String? {
        switch self {
        case .invalid(let message):
            return message
        }
    }
}

enum LivePermission: String, Codable, CaseIterable, Sendable {
    case receiverStatus = "receiver:status"
    case sessionCreate = "session:create"
    case sessionResume = "session:resume"
    case framePut = "frame:put"
    case assetPut = "asset:put"
    case sessionFinalize = "session:finalize"
}

struct LiveDiscoveryIdentity: Codable, Equatable, Sendable {
    let serviceType: String
    let serviceName: String
    let domain: String

    enum CodingKeys: String, CodingKey {
        case serviceType = "service_type"
        case serviceName = "service_name"
        case domain
    }

    func validate() throws {
        guard serviceType == LiveAuthContract.bonjourServiceType else {
            throw LiveAuthContractError.invalid("Unexpected Bonjour service type.")
        }
        guard domain == LiveAuthContract.bonjourDomain else {
            throw LiveAuthContractError.invalid("Unexpected Bonjour domain.")
        }
        try LiveAuthValidation.utf8String(serviceName, maximumBytes: 63, field: "service_name")
    }
}

struct LivePairingInvitation: Codable, Equatable, Sendable {
    let schema: String
    let pairingID: String
    let mode: String
    let desktopID: String
    let desktopName: String
    let desktopPublicKeyBase64URL: String
    let discovery: LiveDiscoveryIdentity
    let tlsCertificateSHA256: String
    let pairingSecretBase64URL: String
    let issuedAt: String
    let expiresAt: String
    let permissions: [LivePermission]
    let authority: String

    enum CodingKeys: String, CodingKey {
        case schema
        case pairingID = "pairing_id"
        case mode
        case desktopID = "desktop_id"
        case desktopName = "desktop_name"
        case desktopPublicKeyBase64URL = "desktop_public_key_b64u"
        case discovery
        case tlsCertificateSHA256 = "tls_certificate_sha256"
        case pairingSecretBase64URL = "pairing_secret_b64u"
        case issuedAt = "issued_at"
        case expiresAt = "expires_at"
        case permissions
        case authority
    }

    func validate(freshAt now: Date? = nil) throws {
        guard schema == "capture_splat.live_pairing_invitation.v0.1" else {
            throw LiveAuthContractError.invalid("Unexpected pairing invitation schema.")
        }
        try LiveAuthValidation.identifier(pairingID, prefix: "csp")
        guard mode == "qr", authority == LiveAuthContract.authority else {
            throw LiveAuthContractError.invalid("Pairing invitation mode or authority is invalid.")
        }
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        try LiveAuthValidation.utf8String(desktopName, maximumBytes: 80, field: "desktop_name")
        let publicKey = try LiveAuthValidation.p256PublicKey(desktopPublicKeyBase64URL)
        guard LiveAuthEncoding.identity(prefix: "wsd", publicKeyX963: publicKey) == desktopID else {
            throw LiveAuthContractError.invalid("Desktop identity does not match its public key.")
        }
        try discovery.validate()
        try LiveAuthValidation.sha256(tlsCertificateSHA256)
        _ = try LiveAuthEncoding.decodeBase64URL(
            pairingSecretBase64URL,
            expectedBytes: 32,
            field: "pairing_secret_b64u"
        )
        let issued = try LiveAuthTime.parse(issuedAt)
        let expires = try LiveAuthTime.parse(expiresAt)
        let duration = expires.timeIntervalSince(issued)
        guard duration > 0, duration <= 300 else {
            throw LiveAuthContractError.invalid("Pairing invitation validity is outside 300 seconds.")
        }
        try LiveAuthValidation.canonicalPermissions(permissions)
        if let now, !(issued..<expires).contains(now) {
            throw LiveAuthContractError.invalid("Pairing invitation is not currently valid.")
        }
    }
}

struct LivePairingRequestPayload: Codable, Equatable, Sendable {
    let schema: String
    let pairingID: String
    let requestID: String
    let desktopID: String
    let deviceID: String
    let deviceName: String
    let devicePublicKeyBase64URL: String
    let devicePlatform: String
    let deviceAppVersion: String
    let clientNonceBase64URL: String
    let requestedPermissions: [LivePermission]
    let createdAt: String
    let authority: String

    enum CodingKeys: String, CodingKey {
        case schema
        case pairingID = "pairing_id"
        case requestID = "request_id"
        case desktopID = "desktop_id"
        case deviceID = "device_id"
        case deviceName = "device_name"
        case devicePublicKeyBase64URL = "device_public_key_b64u"
        case devicePlatform = "device_platform"
        case deviceAppVersion = "device_app_version"
        case clientNonceBase64URL = "client_nonce_b64u"
        case requestedPermissions = "requested_permissions"
        case createdAt = "created_at"
        case authority
    }

    func validate() throws {
        guard schema == "capture_splat.live_pairing_request_payload.v0.1" else {
            throw LiveAuthContractError.invalid("Unexpected pairing request schema.")
        }
        try LiveAuthValidation.identifier(pairingID, prefix: "csp")
        try LiveAuthValidation.identifier(requestID, prefix: "csr")
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        try LiveAuthValidation.identity(deviceID, prefix: "csd")
        try LiveAuthValidation.utf8String(deviceName, maximumBytes: 80, field: "device_name")
        let publicKey = try LiveAuthValidation.p256PublicKey(devicePublicKeyBase64URL)
        guard LiveAuthEncoding.identity(prefix: "csd", publicKeyX963: publicKey) == deviceID else {
            throw LiveAuthContractError.invalid("Device identity does not match its public key.")
        }
        guard devicePlatform == "ios",
              LiveAuthValidation.matches(deviceAppVersion, "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"),
              authority == LiveAuthContract.authority else {
            throw LiveAuthContractError.invalid("Pairing request platform, version, or authority is invalid.")
        }
        _ = try LiveAuthEncoding.decodeBase64URL(
            clientNonceBase64URL,
            expectedBytes: 16,
            field: "client_nonce_b64u"
        )
        try LiveAuthValidation.canonicalPermissions(requestedPermissions)
        _ = try LiveAuthTime.parse(createdAt)
    }
}

struct LivePairingRequestEnvelope: Codable, Equatable, Sendable {
    let schema: String
    let payloadBase64URL: String
    let deviceSignatureBase64URL: String
    let invitationProofBase64URL: String

    enum CodingKeys: String, CodingKey {
        case schema
        case payloadBase64URL = "payload_b64u"
        case deviceSignatureBase64URL = "device_signature_b64u"
        case invitationProofBase64URL = "invitation_proof_b64u"
    }
}

struct LivePairingGrantPayload: Codable, Equatable, Sendable {
    let schema: String
    let pairingID: String
    let requestID: String
    let grantID: String
    let pairingEpoch: Int
    let audience: String
    let desktopID: String
    let deviceID: String
    let devicePublicKeyBase64URL: String
    let permissions: [LivePermission]
    let authScheme: String
    let liveDiscovery: LiveDiscoveryIdentity
    let tlsCertificateSHA256: String
    let issuedAt: String
    let notBefore: String
    let expiresAt: String
    let authority: String

    enum CodingKeys: String, CodingKey {
        case schema
        case pairingID = "pairing_id"
        case requestID = "request_id"
        case grantID = "grant_id"
        case pairingEpoch = "pairing_epoch"
        case audience
        case desktopID = "desktop_id"
        case deviceID = "device_id"
        case devicePublicKeyBase64URL = "device_public_key_b64u"
        case permissions
        case authScheme = "auth_scheme"
        case liveDiscovery = "live_discovery"
        case tlsCertificateSHA256 = "tls_certificate_sha256"
        case issuedAt = "issued_at"
        case notBefore = "not_before"
        case expiresAt = "expires_at"
        case authority
    }

    func validate(currentAt now: Date? = nil) throws {
        guard schema == "capture_splat.live_pairing_grant_payload.v0.1",
              audience == LiveAuthContract.audience,
              authScheme == LiveAuthContract.authScheme,
              authority == LiveAuthContract.authority else {
            throw LiveAuthContractError.invalid("Pairing grant protocol fields are invalid.")
        }
        try LiveAuthValidation.identifier(pairingID, prefix: "csp")
        try LiveAuthValidation.identifier(requestID, prefix: "csr")
        try LiveAuthValidation.identifier(grantID, prefix: "csg")
        guard (1...9_007_199_254_740_991).contains(pairingEpoch) else {
            throw LiveAuthContractError.invalid("Pairing epoch is outside the JSON-safe range.")
        }
        try LiveAuthValidation.identity(desktopID, prefix: "wsd")
        try LiveAuthValidation.identity(deviceID, prefix: "csd")
        let publicKey = try LiveAuthValidation.p256PublicKey(devicePublicKeyBase64URL)
        guard LiveAuthEncoding.identity(prefix: "csd", publicKeyX963: publicKey) == deviceID else {
            throw LiveAuthContractError.invalid("Grant device identity does not match its public key.")
        }
        try LiveAuthValidation.canonicalPermissions(permissions)
        try liveDiscovery.validate()
        try LiveAuthValidation.sha256(tlsCertificateSHA256)
        let issued = try LiveAuthTime.parse(issuedAt)
        let starts = try LiveAuthTime.parse(notBefore)
        let expires = try LiveAuthTime.parse(expiresAt)
        guard issued <= starts,
              expires > starts,
              expires.timeIntervalSince(starts) <= 30 * 24 * 60 * 60 else {
            throw LiveAuthContractError.invalid("Pairing grant validity interval is invalid.")
        }
        if let now, !(starts..<expires).contains(now) {
            throw LiveAuthContractError.invalid("Pairing grant is not currently valid.")
        }
    }
}

struct LivePairingGrantEnvelope: Codable, Equatable, Sendable {
    let schema: String
    let payloadBase64URL: String
    let desktopSignatureBase64URL: String

    enum CodingKeys: String, CodingKey {
        case schema
        case payloadBase64URL = "payload_b64u"
        case desktopSignatureBase64URL = "desktop_signature_b64u"
    }
}

struct LiveAuthErrorBody: Codable, Equatable, Sendable {
    let schema: String
    let code: String
    let retryable: Bool
    let message: String?

    private static let codes: Set<String> = [
        "body_digest_mismatch",
        "desktop_signature_invalid",
        "device_signature_invalid",
        "grant_expired",
        "grant_revoked",
        "grant_unknown",
        "identity_mismatch",
        "invalid_request",
        "pairing_consumed",
        "pairing_expired",
        "pairing_proof_invalid",
        "permission_denied",
        "receiver_not_paired",
        "request_replayed",
        "request_signature_invalid",
        "request_stale",
        "session_owner_mismatch",
        "tls_required",
    ]

    static func decodeStrict(_ data: Data) throws -> LiveAuthErrorBody {
        let error = try LiveStrictJSON.decode(LiveAuthErrorBody.self, from: data)
        let value = try JSONSerialization.jsonObject(with: data)
        guard let object = value as? [String: Any] else {
            throw LiveAuthContractError.invalid("Authentication error must be a JSON object.")
        }
        let required: Set<String> = ["schema", "code", "retryable"]
        let allowed = required.union(["message"])
        guard required.isSubset(of: object.keys),
              Set(object.keys).isSubset(of: allowed),
              error.schema == "capture_splat.live_auth_error.v0.1",
              codes.contains(error.code) else {
            throw LiveAuthContractError.invalid("Authentication error does not match the contract.")
        }
        if let message = error.message {
            try LiveAuthValidation.utf8String(message, maximumBytes: 256, field: "message")
        }
        return error
    }
}

struct LiveResolvedEndpoint: Codable, Equatable, Sendable {
    let host: String
    let port: Int
    let discovery: LiveDiscoveryIdentity

    func validate(against invitation: LivePairingInvitation) throws {
        try validate(discovery: invitation.discovery)
    }

    func validate(discovery expectedDiscovery: LiveDiscoveryIdentity) throws {
        guard !host.isEmpty,
              host.utf8.count <= 253,
              (1...65_535).contains(port),
              discovery == expectedDiscovery else {
            throw LiveAuthContractError.invalid("Resolved service does not match the QR invitation.")
        }
        if host.contains("/") || host.contains("\\") || host.contains("%") || host.contains("@") {
            throw LiveAuthContractError.invalid("Resolved host is not canonical.")
        }
    }

    func url(path: String) throws -> URL {
        try LiveAuthValidation.canonicalPath(path)
        var components = URLComponents()
        components.scheme = "https"
        components.host = host
        components.port = port
        components.path = path
        guard let url = components.url, url.query == nil, url.fragment == nil else {
            throw LiveAuthContractError.invalid("Could not construct the pinned HTTPS endpoint.")
        }
        return url
    }
}

enum LiveStrictJSON {
    static func canonicalData<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        do {
            return try encoder.encode(value)
        } catch {
            throw LiveAuthContractError.invalid("Value cannot be encoded as canonical JSON.")
        }
    }

    static func decode<T: Decodable>(
        _ type: T.Type,
        from data: Data,
        exactKeys: Set<String>? = nil
    ) throws -> T {
        try LiveJSONDuplicateKeyValidator.validate(data)
        let value: Any
        do {
            value = try JSONSerialization.jsonObject(with: data)
        } catch {
            throw LiveAuthContractError.invalid("JSON is malformed or contains non-finite data.")
        }
        if let exactKeys {
            guard let object = value as? [String: Any],
                  Set(object.keys) == exactKeys else {
                throw LiveAuthContractError.invalid("JSON has missing or additional fields.")
            }
        }
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw LiveAuthContractError.invalid("JSON does not match the live contract.")
        }
    }

    static func decodeCanonical<T: Codable>(
        _ type: T.Type,
        from data: Data
    ) throws -> T {
        let value = try decode(type, from: data)
        guard try canonicalData(value) == data else {
            throw LiveAuthContractError.invalid("Payload is not exact canonical JSON.")
        }
        return value
    }
}

enum LiveAuthEncoding {
    static func encodeBase64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func decodeBase64URL(
        _ value: String,
        expectedBytes: Int? = nil,
        field: String
    ) throws -> Data {
        guard !value.isEmpty,
              !value.contains("="),
              LiveAuthValidation.matches(value, "^[A-Za-z0-9_-]+$") else {
            throw LiveAuthContractError.invalid("\(field) is not unpadded Base64URL.")
        }
        var base64 = value.replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        guard let decoded = Data(base64Encoded: base64),
              encodeBase64URL(decoded) == value,
              expectedBytes == nil || decoded.count == expectedBytes else {
            throw LiveAuthContractError.invalid("\(field) is not canonical Base64URL.")
        }
        return decoded
    }

    static func identity(prefix: String, publicKeyX963: Data) -> String {
        "\(prefix)_\(encodeBase64URL(Data(SHA256.hash(data: publicKeyX963))))"
    }

    static func randomID(prefix: String, bytes: Data) throws -> String {
        guard ["csp", "csr", "csg"].contains(prefix), bytes.count == 16 else {
            throw LiveAuthContractError.invalid("Live identifier input is invalid.")
        }
        return "\(prefix)_\(encodeBase64URL(bytes))"
    }

    static func sha256(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

enum LiveAuthTime {
    static func string(_ date: Date) -> String {
        formatter.string(from: date)
    }

    static func parse(_ value: String) throws -> Date {
        guard LiveAuthValidation.matches(
            value,
            "^(?!0000)[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z$"
        ), let date = formatter.date(from: value), formatter.string(from: date) == value else {
            throw LiveAuthContractError.invalid("Timestamp must be real UTC RFC 3339 with milliseconds.")
        }
        return date
    }

    private static let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
        return formatter
    }()
}

enum LiveAuthValidation {
    static func matches(_ value: String, _ pattern: String) -> Bool {
        value.range(of: pattern, options: .regularExpression) != nil
    }

    static func utf8String(_ value: String, maximumBytes: Int, field: String) throws {
        guard !value.isEmpty, value.utf8.count <= maximumBytes else {
            throw LiveAuthContractError.invalid("\(field) is empty or exceeds its UTF-8 limit.")
        }
    }

    static func identifier(_ value: String, prefix: String) throws {
        let pattern = "^\(prefix)_[A-Za-z0-9_-]{21}[AQgw]$"
        guard matches(value, pattern),
              (try? LiveAuthEncoding.decodeBase64URL(
                String(value.dropFirst(4)),
                expectedBytes: 16,
                field: value
              )) != nil else {
            throw LiveAuthContractError.invalid("\(prefix) identifier is not canonical.")
        }
    }

    static func identity(_ value: String, prefix: String) throws {
        let pattern = "^\(prefix)_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
        guard matches(value, pattern),
              (try? LiveAuthEncoding.decodeBase64URL(
                String(value.dropFirst(4)),
                expectedBytes: 32,
                field: value
              )) != nil else {
            throw LiveAuthContractError.invalid("\(prefix) identity is not canonical.")
        }
    }

    static func p256PublicKey(_ value: String) throws -> Data {
        let data = try LiveAuthEncoding.decodeBase64URL(
            value,
            expectedBytes: 65,
            field: "P-256 public key"
        )
        guard data.first == 4,
              (try? P256.Signing.PublicKey(x963Representation: data)) != nil else {
            throw LiveAuthContractError.invalid("Public key is not uncompressed P-256 X9.63.")
        }
        return data
    }

    static func p1363Signature(_ value: String) throws -> Data {
        let data = try LiveAuthEncoding.decodeBase64URL(
            value,
            expectedBytes: 64,
            field: "P-256 signature"
        )
        let order: [UInt8] = [
            0xff, 0xff, 0xff, 0xff, 0x00, 0x00, 0x00, 0x00,
            0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
            0xbc, 0xe6, 0xfa, 0xad, 0xa7, 0x17, 0x9e, 0x84,
            0xf3, 0xb9, 0xca, 0xc2, 0xfc, 0x63, 0x25, 0x51,
        ]
        let bytes = Array(data)
        let r = Array(bytes[0..<32])
        let s = Array(bytes[32..<64])
        guard validP256Scalar(r, order: order),
              validP256Scalar(s, order: order),
              (try? P256.Signing.ECDSASignature(rawRepresentation: data)) != nil else {
            throw LiveAuthContractError.invalid("P-256 signature is not IEEE-P1363.")
        }
        return data
    }

    private static func validP256Scalar(_ scalar: [UInt8], order: [UInt8]) -> Bool {
        guard scalar.contains(where: { $0 != 0 }) else { return false }
        return scalar.lexicographicallyPrecedes(order)
    }

    static func sha256(_ value: String) throws {
        guard matches(value, "^sha256:[0-9a-f]{64}$") else {
            throw LiveAuthContractError.invalid("SHA-256 value is not canonical.")
        }
    }

    static func canonicalPermissions(_ value: [LivePermission]) throws {
        guard !value.isEmpty,
              value.count == Set(value).count,
              value == value.sorted(by: {
                  LiveAuthContract.permissions.firstIndex(of: $0)!
                      < LiveAuthContract.permissions.firstIndex(of: $1)!
              }) else {
            throw LiveAuthContractError.invalid("Permissions are not unique and canonically ordered.")
        }
    }

    static func canonicalPath(_ path: String) throws {
        guard matches(path, "^/[A-Za-z0-9._/-]+$"),
              !path.contains("//"),
              !path.contains("\\"),
              !path.contains("%"),
              !path.contains("?"),
              !path.contains("#"),
              !path.hasSuffix("/"),
              !path.split(separator: "/", omittingEmptySubsequences: false).dropFirst()
                .contains(where: { $0 == "." || $0 == ".." || $0.isEmpty }) else {
            throw LiveAuthContractError.invalid("Request path is not canonical.")
        }
    }

    static func mediaType(_ value: String) throws {
        guard matches(
            value,
            "^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
        ) else {
            throw LiveAuthContractError.invalid("Media type is not canonical.")
        }
    }
}

private struct LiveJSONDuplicateKeyValidator {
    private let bytes: [UInt8]
    private var index = 0

    private init(_ data: Data) {
        bytes = Array(data)
    }

    static func validate(_ data: Data) throws {
        var parser = LiveJSONDuplicateKeyValidator(data)
        try parser.skipWhitespace()
        try parser.parseValue()
        try parser.skipWhitespace()
        guard parser.index == parser.bytes.count else {
            throw LiveAuthContractError.invalid("JSON has trailing data.")
        }
    }

    private mutating func parseValue() throws {
        guard index < bytes.count else { throw malformed() }
        switch bytes[index] {
        case 0x7B:
            try parseObject()
        case 0x5B:
            try parseArray()
        case 0x22:
            _ = try parseString()
        case 0x74:
            try parseLiteral("true")
        case 0x66:
            try parseLiteral("false")
        case 0x6E:
            try parseLiteral("null")
        case 0x2D, 0x30...0x39:
            try parseNumber()
        default:
            throw malformed()
        }
    }

    private mutating func parseObject() throws {
        index += 1
        try skipWhitespace()
        if consume(0x7D) { return }
        var keys = Set<String>()
        while true {
            guard index < bytes.count, bytes[index] == 0x22 else { throw malformed() }
            let key = try parseString()
            guard keys.insert(key).inserted else {
                throw LiveAuthContractError.invalid("JSON contains a duplicate object key.")
            }
            try skipWhitespace()
            guard consume(0x3A) else { throw malformed() }
            try skipWhitespace()
            try parseValue()
            try skipWhitespace()
            if consume(0x7D) { return }
            guard consume(0x2C) else { throw malformed() }
            try skipWhitespace()
        }
    }

    private mutating func parseArray() throws {
        index += 1
        try skipWhitespace()
        if consume(0x5D) { return }
        while true {
            try parseValue()
            try skipWhitespace()
            if consume(0x5D) { return }
            guard consume(0x2C) else { throw malformed() }
            try skipWhitespace()
        }
    }

    private mutating func parseString() throws -> String {
        let start = index
        index += 1
        while index < bytes.count {
            let byte = bytes[index]
            index += 1
            if byte == 0x22 {
                let token = Data(bytes[start..<index])
                do {
                    return try JSONDecoder().decode(String.self, from: token)
                } catch {
                    throw malformed()
                }
            }
            if byte == 0x5C {
                guard index < bytes.count else { throw malformed() }
                if bytes[index] == 0x75 {
                    index += 1
                    guard index + 4 <= bytes.count else { throw malformed() }
                    index += 4
                } else {
                    index += 1
                }
            }
        }
        throw malformed()
    }

    private mutating func parseLiteral(_ literal: String) throws {
        let data = Array(literal.utf8)
        guard index + data.count <= bytes.count,
              Array(bytes[index..<(index + data.count)]) == data else {
            throw malformed()
        }
        index += data.count
    }

    private mutating func parseNumber() throws {
        while index < bytes.count,
              (bytes[index] == 0x2D
                  || bytes[index] == 0x2B
                  || bytes[index] == 0x2E
                  || bytes[index] == 0x45
                  || bytes[index] == 0x65
                  || (0x30...0x39).contains(bytes[index])) {
            index += 1
        }
    }

    private mutating func skipWhitespace() throws {
        while index < bytes.count, [0x20, 0x09, 0x0A, 0x0D].contains(bytes[index]) {
            index += 1
        }
    }

    private mutating func consume(_ byte: UInt8) -> Bool {
        guard index < bytes.count, bytes[index] == byte else { return false }
        index += 1
        return true
    }

    private func malformed() -> LiveAuthContractError {
        .invalid("JSON is malformed.")
    }
}

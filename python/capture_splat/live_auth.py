from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable

PAIRING_INVITATION_SCHEMA = "capture_splat.live_pairing_invitation.v0.1"
PAIRING_REQUEST_PAYLOAD_SCHEMA = "capture_splat.live_pairing_request_payload.v0.1"
PAIRING_REQUEST_ENVELOPE_SCHEMA = "capture_splat.live_pairing_request_envelope.v0.1"
PAIRING_GRANT_PAYLOAD_SCHEMA = "capture_splat.live_pairing_grant_payload.v0.1"
PAIRING_GRANT_ENVELOPE_SCHEMA = "capture_splat.live_pairing_grant_envelope.v0.1"
LIVE_AUTH_ERROR_SCHEMA = "capture_splat.live_auth_error.v0.1"
LIVE_AUTH_RECEIPT_SCHEMA = "capture_splat.live_auth_receipt.v0.1"

AUTH_SCHEME = "p256-sha256-ieee-p1363-v0.1"
AUTH_AUDIENCE = "capture_splat.live.v0.1"
BONJOUR_SERVICE_TYPE = "_capturesplat._tcp"
BONJOUR_DOMAIN = "local."
QR_MODE = "qr"
QR_PREFIX = "capture-splat://pair/"
MAX_QR_URI_BYTES = 4096
MAX_ENVELOPE_PAYLOAD_BYTES = 8192
MAX_INVITATION_TTL_SECONDS = 300
MAX_GRANT_TTL_SECONDS = 30 * 24 * 60 * 60
UINT64_MAX = (1 << 64) - 1
JSON_SAFE_INTEGER_MAX = (1 << 53) - 1

PAIRING_REQUEST_SIGNATURE_DOMAIN = b"CAPTURE-SPLAT-PAIRING-REQUEST-V1\x00"
PAIRING_INVITATION_PROOF_DOMAIN = b"CAPTURE-SPLAT-PAIRING-PROOF-V1\x00"
PAIRING_GRANT_SIGNATURE_DOMAIN = b"CAPTURE-SPLAT-PAIRING-GRANT-V1\x00"
AUTH_REQUEST_DOMAIN = "CAPTURE-SPLAT-AUTH-V1"

PERMISSIONS = (
    "receiver:status",
    "session:create",
    "session:resume",
    "frame:put",
    "asset:put",
    "session:finalize",
)
AUTH_ERROR_CODES = {
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
}

BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DESKTOP_ID_PATTERN = re.compile(r"^wsd_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
DEVICE_ID_PATTERN = re.compile(r"^csd_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
PAIRING_ID_PATTERN = re.compile(r"^csp_[A-Za-z0-9_-]{21}[AQgw]$")
REQUEST_ID_PATTERN = re.compile(r"^csr_[A-Za-z0-9_-]{21}[AQgw]$")
GRANT_ID_PATTERN = re.compile(r"^csg_[A-Za-z0-9_-]{21}[AQgw]$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RFC3339_MILLISECONDS_PATTERN = re.compile(
    r"^(?!0000)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
MEDIA_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
)
CANONICAL_REQUEST_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
APP_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

P256_PRIME = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _ensure_finite(value: Any, field: str = "$") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
    elif isinstance(value, dict):
        for key, item in value.items():
            _ensure_finite(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_finite(item, f"{field}[{index}]")


def _exact_keys(
    value: Any,
    required: set[str],
    optional: set[str],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing:
        raise ValueError(f"{field} missing keys: {sorted(missing)}")
    if extra:
        raise ValueError(f"{field} has unexpected keys: {sorted(extra)}")
    return value


def _string(value: Any, field: str, *, max_utf8_bytes: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    if max_utf8_bytes is not None and len(encoded) > max_utf8_bytes:
        raise ValueError(f"{field} exceeds {max_utf8_bytes} UTF-8 bytes")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return value


def _pattern(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{field} has an invalid format")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_MILLISECONDS_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must use RFC 3339 UTC with exactly milliseconds")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid RFC 3339 timestamp") from exc
    return parsed


def _validate_time_window(
    issued_at: Any,
    expires_at: Any,
    field: str,
    *,
    maximum_seconds: int,
) -> None:
    issued = _timestamp(issued_at, f"{field}.issued_at")
    expires = _timestamp(expires_at, f"{field}.expires_at")
    duration = (expires - issued).total_seconds()
    if duration <= 0 or duration > maximum_seconds:
        raise ValueError(f"{field} expiry must be positive and at most {maximum_seconds} seconds")


def encode_base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_base64url(value: Any, field: str, *, expected_bytes: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value or not BASE64URL_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be unpadded Base64URL")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{field} must be unpadded Base64URL") from exc
    if encode_base64url(decoded) != value:
        raise ValueError(f"{field} must use canonical unpadded Base64URL")
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise ValueError(f"{field} must decode to {expected_bytes} bytes")
    return decoded


def canonical_json_bytes(value: Any) -> bytes:
    _ensure_finite(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = text.encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("value is not canonical JSON data") from exc
    return encoded


def load_strict_json_bytes(data: bytes, field: str = "JSON") -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{field} must be UTF-8") from exc
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be strict JSON") from exc
    _ensure_finite(value)
    return value


def _validate_p256_public_key(value: Any, field: str) -> bytes:
    raw = decode_base64url(value, field, expected_bytes=65)
    if raw[0] != 0x04:
        raise ValueError(f"{field} must be an uncompressed P-256 X9.63 public key")
    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:65], "big")
    if x >= P256_PRIME or y >= P256_PRIME:
        raise ValueError(f"{field} is not a P-256 point")
    if pow(y, 2, P256_PRIME) != (pow(x, 3, P256_PRIME) - 3 * x + P256_B) % P256_PRIME:
        raise ValueError(f"{field} is not a P-256 point")
    return raw


def _derive_identity_id(public_key_b64u: str, prefix: str) -> str:
    raw = _validate_p256_public_key(public_key_b64u, "public_key_b64u")
    return f"{prefix}_{encode_base64url(hashlib.sha256(raw).digest())}"


def derive_desktop_id(public_key_b64u: str) -> str:
    return _derive_identity_id(public_key_b64u, "wsd")


def derive_device_id(public_key_b64u: str) -> str:
    return _derive_identity_id(public_key_b64u, "csd")


def derive_128bit_id(prefix: str, value: bytes) -> str:
    if prefix not in {"csp", "csr", "csg"}:
        raise ValueError("identifier prefix must be csp, csr, or csg")
    if not isinstance(value, bytes) or len(value) != 16:
        raise ValueError("identifier source must be exactly 16 bytes")
    return f"{prefix}_{encode_base64url(value)}"


def validate_ieee_p1363_signature(value: Any, field: str) -> bytes:
    raw = decode_base64url(value, field, expected_bytes=64)
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    if not 1 <= r < P256_ORDER or not 1 <= s < P256_ORDER:
        raise ValueError(f"{field} must contain valid P-256 r and s scalars")
    return raw


def _validate_permissions(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    if any(not isinstance(item, str) or item not in PERMISSIONS for item in value):
        raise ValueError(f"{field} contains an unsupported permission")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must contain unique permissions")
    order = {permission: index for index, permission in enumerate(PERMISSIONS)}
    if value != sorted(value, key=order.__getitem__):
        raise ValueError(f"{field} must use canonical permission order")
    return value


def _validate_discovery(value: Any, field: str) -> dict[str, Any]:
    discovery = _exact_keys(
        value,
        {"service_type", "service_name", "domain"},
        set(),
        field,
    )
    if discovery["service_type"] != BONJOUR_SERVICE_TYPE:
        raise ValueError(f"{field}.service_type must be {BONJOUR_SERVICE_TYPE}")
    _string(discovery["service_name"], f"{field}.service_name", max_utf8_bytes=63)
    if discovery["domain"] != BONJOUR_DOMAIN:
        raise ValueError(f"{field}.domain must be {BONJOUR_DOMAIN}")
    return discovery


def validate_pairing_invitation(value: Any) -> dict[str, Any]:
    _ensure_finite(value)
    invitation = _exact_keys(
        value,
        {
            "schema",
            "pairing_id",
            "mode",
            "desktop_id",
            "desktop_name",
            "desktop_public_key_b64u",
            "discovery",
            "tls_certificate_sha256",
            "pairing_secret_b64u",
            "issued_at",
            "expires_at",
            "permissions",
            "authority",
        },
        set(),
        "invitation",
    )
    if invitation["schema"] != PAIRING_INVITATION_SCHEMA:
        raise ValueError(f"invitation.schema must be {PAIRING_INVITATION_SCHEMA}")
    _pattern(invitation["pairing_id"], PAIRING_ID_PATTERN, "invitation.pairing_id")
    if invitation["mode"] != QR_MODE:
        raise ValueError("invitation.mode must be qr")
    _pattern(invitation["desktop_id"], DESKTOP_ID_PATTERN, "invitation.desktop_id")
    _string(invitation["desktop_name"], "invitation.desktop_name", max_utf8_bytes=80)
    public_key = _string(
        invitation["desktop_public_key_b64u"],
        "invitation.desktop_public_key_b64u",
    )
    if derive_desktop_id(public_key) != invitation["desktop_id"]:
        raise ValueError("invitation.desktop_id does not match desktop_public_key_b64u")
    _validate_discovery(invitation["discovery"], "invitation.discovery")
    _sha256(invitation["tls_certificate_sha256"], "invitation.tls_certificate_sha256")
    decode_base64url(
        invitation["pairing_secret_b64u"],
        "invitation.pairing_secret_b64u",
        expected_bytes=32,
    )
    _validate_time_window(
        invitation["issued_at"],
        invitation["expires_at"],
        "invitation",
        maximum_seconds=MAX_INVITATION_TTL_SECONDS,
    )
    _validate_permissions(invitation["permissions"], "invitation.permissions")
    if invitation["authority"] != "proposal_only":
        raise ValueError("invitation.authority must be proposal_only")
    return invitation


def validate_pairing_request_payload(value: Any) -> dict[str, Any]:
    _ensure_finite(value)
    payload = _exact_keys(
        value,
        {
            "schema",
            "pairing_id",
            "request_id",
            "desktop_id",
            "device_id",
            "device_name",
            "device_public_key_b64u",
            "device_platform",
            "device_app_version",
            "client_nonce_b64u",
            "requested_permissions",
            "created_at",
            "authority",
        },
        set(),
        "request_payload",
    )
    if payload["schema"] != PAIRING_REQUEST_PAYLOAD_SCHEMA:
        raise ValueError(f"request_payload.schema must be {PAIRING_REQUEST_PAYLOAD_SCHEMA}")
    _pattern(payload["pairing_id"], PAIRING_ID_PATTERN, "request_payload.pairing_id")
    _pattern(payload["request_id"], REQUEST_ID_PATTERN, "request_payload.request_id")
    _pattern(payload["desktop_id"], DESKTOP_ID_PATTERN, "request_payload.desktop_id")
    _pattern(payload["device_id"], DEVICE_ID_PATTERN, "request_payload.device_id")
    _string(payload["device_name"], "request_payload.device_name", max_utf8_bytes=80)
    public_key = _string(
        payload["device_public_key_b64u"],
        "request_payload.device_public_key_b64u",
    )
    if derive_device_id(public_key) != payload["device_id"]:
        raise ValueError("request_payload.device_id does not match device_public_key_b64u")
    if payload["device_platform"] != "ios":
        raise ValueError("request_payload.device_platform must be ios")
    _pattern(
        payload["device_app_version"],
        APP_VERSION_PATTERN,
        "request_payload.device_app_version",
    )
    decode_base64url(
        payload["client_nonce_b64u"],
        "request_payload.client_nonce_b64u",
        expected_bytes=16,
    )
    _validate_permissions(
        payload["requested_permissions"],
        "request_payload.requested_permissions",
    )
    _timestamp(payload["created_at"], "request_payload.created_at")
    if payload["authority"] != "proposal_only":
        raise ValueError("request_payload.authority must be proposal_only")
    return payload


def _decode_canonical_payload(
    value: Any,
    field: str,
    validator: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    payload_bytes = decode_base64url(value, field)
    if len(payload_bytes) > MAX_ENVELOPE_PAYLOAD_BYTES:
        raise ValueError(f"{field} exceeds {MAX_ENVELOPE_PAYLOAD_BYTES} bytes")
    payload = load_strict_json_bytes(payload_bytes, field)
    validator(payload)
    if canonical_json_bytes(payload) != payload_bytes:
        raise ValueError(f"{field} must contain exact canonical JSON bytes")
    return payload


def validate_pairing_request_envelope(value: Any) -> dict[str, Any]:
    _ensure_finite(value)
    envelope = _exact_keys(
        value,
        {
            "schema",
            "payload_b64u",
            "device_signature_b64u",
            "invitation_proof_b64u",
        },
        set(),
        "request_envelope",
    )
    if envelope["schema"] != PAIRING_REQUEST_ENVELOPE_SCHEMA:
        raise ValueError(
            f"request_envelope.schema must be {PAIRING_REQUEST_ENVELOPE_SCHEMA}"
        )
    _decode_canonical_payload(
        envelope["payload_b64u"],
        "request_envelope.payload_b64u",
        validate_pairing_request_payload,
    )
    validate_ieee_p1363_signature(
        envelope["device_signature_b64u"],
        "request_envelope.device_signature_b64u",
    )
    decode_base64url(
        envelope["invitation_proof_b64u"],
        "request_envelope.invitation_proof_b64u",
        expected_bytes=32,
    )
    return envelope


def validate_pairing_grant_payload(value: Any) -> dict[str, Any]:
    _ensure_finite(value)
    payload = _exact_keys(
        value,
        {
            "schema",
            "pairing_id",
            "request_id",
            "grant_id",
            "pairing_epoch",
            "audience",
            "desktop_id",
            "device_id",
            "device_public_key_b64u",
            "permissions",
            "auth_scheme",
            "live_discovery",
            "tls_certificate_sha256",
            "issued_at",
            "not_before",
            "expires_at",
            "authority",
        },
        set(),
        "grant_payload",
    )
    if payload["schema"] != PAIRING_GRANT_PAYLOAD_SCHEMA:
        raise ValueError(f"grant_payload.schema must be {PAIRING_GRANT_PAYLOAD_SCHEMA}")
    _pattern(payload["pairing_id"], PAIRING_ID_PATTERN, "grant_payload.pairing_id")
    _pattern(payload["request_id"], REQUEST_ID_PATTERN, "grant_payload.request_id")
    _pattern(payload["grant_id"], GRANT_ID_PATTERN, "grant_payload.grant_id")
    _integer(
        payload["pairing_epoch"],
        "grant_payload.pairing_epoch",
        minimum=1,
        maximum=JSON_SAFE_INTEGER_MAX,
    )
    if payload["audience"] != AUTH_AUDIENCE:
        raise ValueError(f"grant_payload.audience must be {AUTH_AUDIENCE}")
    _pattern(payload["desktop_id"], DESKTOP_ID_PATTERN, "grant_payload.desktop_id")
    _pattern(payload["device_id"], DEVICE_ID_PATTERN, "grant_payload.device_id")
    public_key = _string(
        payload["device_public_key_b64u"],
        "grant_payload.device_public_key_b64u",
    )
    if derive_device_id(public_key) != payload["device_id"]:
        raise ValueError("grant_payload.device_id does not match device_public_key_b64u")
    _validate_permissions(payload["permissions"], "grant_payload.permissions")
    if payload["auth_scheme"] != AUTH_SCHEME:
        raise ValueError(f"grant_payload.auth_scheme must be {AUTH_SCHEME}")
    _validate_discovery(payload["live_discovery"], "grant_payload.live_discovery")
    _sha256(
        payload["tls_certificate_sha256"],
        "grant_payload.tls_certificate_sha256",
    )
    issued_at = _timestamp(payload["issued_at"], "grant_payload.issued_at")
    not_before = _timestamp(payload["not_before"], "grant_payload.not_before")
    expires_at = _timestamp(payload["expires_at"], "grant_payload.expires_at")
    if issued_at > not_before:
        raise ValueError("grant_payload.issued_at must be at or before not_before")
    duration = (expires_at - not_before).total_seconds()
    if duration <= 0 or duration > MAX_GRANT_TTL_SECONDS:
        raise ValueError(
            "grant_payload expiry from not_before must be positive and at most "
            f"{MAX_GRANT_TTL_SECONDS} seconds"
        )
    if payload["authority"] != "proposal_only":
        raise ValueError("grant_payload.authority must be proposal_only")
    return payload


def validate_pairing_grant_envelope(value: Any) -> dict[str, Any]:
    _ensure_finite(value)
    envelope = _exact_keys(
        value,
        {"schema", "payload_b64u", "desktop_signature_b64u"},
        set(),
        "grant_envelope",
    )
    if envelope["schema"] != PAIRING_GRANT_ENVELOPE_SCHEMA:
        raise ValueError(
            f"grant_envelope.schema must be {PAIRING_GRANT_ENVELOPE_SCHEMA}"
        )
    _decode_canonical_payload(
        envelope["payload_b64u"],
        "grant_envelope.payload_b64u",
        validate_pairing_grant_payload,
    )
    validate_ieee_p1363_signature(
        envelope["desktop_signature_b64u"],
        "grant_envelope.desktop_signature_b64u",
    )
    return envelope


def validate_live_auth_error(value: Any) -> dict[str, Any]:
    _ensure_finite(value)
    error = _exact_keys(
        value,
        {"schema", "code", "retryable"},
        {"message"},
        "auth_error",
    )
    if error["schema"] != LIVE_AUTH_ERROR_SCHEMA:
        raise ValueError(f"auth_error.schema must be {LIVE_AUTH_ERROR_SCHEMA}")
    if error["code"] not in AUTH_ERROR_CODES:
        raise ValueError("auth_error.code is invalid")
    if not isinstance(error["retryable"], bool):
        raise ValueError("auth_error.retryable must be a boolean")
    if "message" in error:
        _string(error["message"], "auth_error.message", max_utf8_bytes=256)
    return error


def validate_live_auth_receipt(value: Any) -> dict[str, Any]:
    _ensure_finite(value)
    receipt = _exact_keys(
        value,
        {
            "schema",
            "session_id",
            "desktop_id",
            "device_id",
            "grant_id",
            "pairing_epoch",
            "permissions",
            "auth_scheme",
            "tls_certificate_sha256",
            "authenticated_at",
            "grant_expires_at",
            "authority",
        },
        set(),
        "auth_receipt",
    )
    if receipt["schema"] != LIVE_AUTH_RECEIPT_SCHEMA:
        raise ValueError(f"auth_receipt.schema must be {LIVE_AUTH_RECEIPT_SCHEMA}")
    _pattern(receipt["session_id"], SESSION_ID_PATTERN, "auth_receipt.session_id")
    _pattern(receipt["desktop_id"], DESKTOP_ID_PATTERN, "auth_receipt.desktop_id")
    _pattern(receipt["device_id"], DEVICE_ID_PATTERN, "auth_receipt.device_id")
    _pattern(receipt["grant_id"], GRANT_ID_PATTERN, "auth_receipt.grant_id")
    _integer(
        receipt["pairing_epoch"],
        "auth_receipt.pairing_epoch",
        minimum=1,
        maximum=JSON_SAFE_INTEGER_MAX,
    )
    _validate_permissions(receipt["permissions"], "auth_receipt.permissions")
    if receipt["auth_scheme"] != AUTH_SCHEME:
        raise ValueError(f"auth_receipt.auth_scheme must be {AUTH_SCHEME}")
    _sha256(
        receipt["tls_certificate_sha256"],
        "auth_receipt.tls_certificate_sha256",
    )
    authenticated = _timestamp(
        receipt["authenticated_at"],
        "auth_receipt.authenticated_at",
    )
    expires = _timestamp(
        receipt["grant_expires_at"],
        "auth_receipt.grant_expires_at",
    )
    if authenticated >= expires:
        raise ValueError("auth_receipt must authenticate before grant expiry")
    if receipt["authority"] != "proposal_only":
        raise ValueError("auth_receipt.authority must be proposal_only")
    return receipt


def pairing_request_signature_bytes(payload_bytes: bytes) -> bytes:
    return PAIRING_REQUEST_SIGNATURE_DOMAIN + payload_bytes


def pairing_grant_signature_bytes(payload_bytes: bytes) -> bytes:
    return PAIRING_GRANT_SIGNATURE_DOMAIN + payload_bytes


def pairing_invitation_proof(
    pairing_secret_b64u: str,
    request_payload_bytes: bytes,
) -> str:
    secret = decode_base64url(
        pairing_secret_b64u,
        "pairing_secret_b64u",
        expected_bytes=32,
    )
    digest = hmac.new(
        secret,
        PAIRING_INVITATION_PROOF_DOMAIN + request_payload_bytes,
        hashlib.sha256,
    ).digest()
    return encode_base64url(digest)


def verify_pairing_invitation_proof(
    pairing_secret_b64u: str,
    request_payload_bytes: bytes,
    proof_b64u: str,
) -> bool:
    decode_base64url(proof_b64u, "proof_b64u", expected_bytes=32)
    return hmac.compare_digest(
        pairing_invitation_proof(pairing_secret_b64u, request_payload_bytes),
        proof_b64u,
    )


def build_pairing_request_envelope(
    payload: dict[str, Any],
    *,
    device_signature_b64u: str,
    pairing_secret_b64u: str,
) -> dict[str, Any]:
    validate_pairing_request_payload(payload)
    payload_bytes = canonical_json_bytes(payload)
    envelope = {
        "schema": PAIRING_REQUEST_ENVELOPE_SCHEMA,
        "payload_b64u": encode_base64url(payload_bytes),
        "device_signature_b64u": device_signature_b64u,
        "invitation_proof_b64u": pairing_invitation_proof(
            pairing_secret_b64u,
            payload_bytes,
        ),
    }
    return validate_pairing_request_envelope(envelope)


def build_pairing_grant_envelope(
    payload: dict[str, Any],
    *,
    desktop_signature_b64u: str,
) -> dict[str, Any]:
    validate_pairing_grant_payload(payload)
    envelope = {
        "schema": PAIRING_GRANT_ENVELOPE_SCHEMA,
        "payload_b64u": encode_base64url(canonical_json_bytes(payload)),
        "desktop_signature_b64u": desktop_signature_b64u,
    }
    return validate_pairing_grant_envelope(envelope)


def encode_pairing_qr_uri(invitation: dict[str, Any]) -> str:
    validate_pairing_invitation(invitation)
    uri = QR_PREFIX + encode_base64url(canonical_json_bytes(invitation))
    if len(uri.encode("ascii")) > MAX_QR_URI_BYTES:
        raise ValueError(f"pairing QR URI exceeds {MAX_QR_URI_BYTES} bytes")
    return uri


def decode_pairing_qr_uri(uri: Any) -> dict[str, Any]:
    if not isinstance(uri, str):
        raise ValueError("pairing QR URI must be a string")
    try:
        encoded_uri = uri.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("pairing QR URI must be ASCII") from exc
    if len(encoded_uri) > MAX_QR_URI_BYTES:
        raise ValueError(f"pairing QR URI exceeds {MAX_QR_URI_BYTES} bytes")
    if not uri.startswith(QR_PREFIX) or "?" in uri or "#" in uri or "%" in uri:
        raise ValueError("pairing QR URI must use the exact capture-splat://pair/ scheme")
    payload_bytes = decode_base64url(uri[len(QR_PREFIX):], "pairing QR payload")
    invitation = load_strict_json_bytes(payload_bytes, "pairing QR payload")
    validate_pairing_invitation(invitation)
    if canonical_json_bytes(invitation) != payload_bytes:
        raise ValueError("pairing QR payload must contain exact canonical JSON bytes")
    return invitation


def body_sha256(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def validate_canonical_request_path(path: Any) -> str:
    if (
        not isinstance(path, str)
        or not CANONICAL_REQUEST_PATH_PATTERN.fullmatch(path)
        or "?" in path
        or "#" in path
        or "%" in path
        or "\\" in path
        or "//" in path
        or path.endswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/")[1:])
    ):
        raise ValueError("request path must be a raw canonical absolute path")
    return path


def canonical_authenticated_request_bytes(
    *,
    desktop_id: str,
    device_id: str,
    grant_id: str,
    counter: int,
    request_id: str,
    timestamp: str,
    method: str,
    path: str,
    content_type: str | None,
    content_length: int,
    content_sha256: str,
) -> bytes:
    _pattern(desktop_id, DESKTOP_ID_PATTERN, "desktop_id")
    _pattern(device_id, DEVICE_ID_PATTERN, "device_id")
    _pattern(grant_id, GRANT_ID_PATTERN, "grant_id")
    _integer(counter, "counter", maximum=UINT64_MAX)
    _pattern(request_id, REQUEST_ID_PATTERN, "request_id")
    _timestamp(timestamp, "timestamp")
    if method not in {"GET", "POST", "PUT"}:
        raise ValueError("method must be GET, POST, or PUT")
    validate_canonical_request_path(path)
    if content_type is None:
        canonical_content_type = "-"
    elif not isinstance(content_type, str) or not MEDIA_TYPE_PATTERN.fullmatch(content_type):
        raise ValueError("content_type must be a lowercase MIME type or None")
    else:
        canonical_content_type = content_type
    _integer(content_length, "content_length", maximum=UINT64_MAX)
    _sha256(content_sha256, "content_sha256")
    values = (
        AUTH_REQUEST_DOMAIN,
        desktop_id,
        device_id,
        grant_id,
        str(counter),
        request_id,
        timestamp,
        method,
        path,
        canonical_content_type,
        str(content_length),
        content_sha256,
        "",
    )
    return "\n".join(values).encode("ascii")

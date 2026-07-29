from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from capture_splat.live_auth import (
    AUTH_REQUEST_DOMAIN,
    MAX_GRANT_TTL_SECONDS,
    MAX_QR_URI_BYTES,
    PAIRING_GRANT_SIGNATURE_DOMAIN,
    PAIRING_INVITATION_PROOF_DOMAIN,
    PAIRING_REQUEST_SIGNATURE_DOMAIN,
    QR_PREFIX,
    UINT64_MAX,
    body_sha256,
    build_pairing_grant_envelope,
    build_pairing_request_envelope,
    canonical_authenticated_request_bytes,
    canonical_json_bytes,
    decode_base64url,
    decode_pairing_qr_uri,
    derive_128bit_id,
    derive_desktop_id,
    derive_device_id,
    encode_base64url,
    encode_pairing_qr_uri,
    load_strict_json_bytes,
    pairing_grant_signature_bytes,
    pairing_invitation_proof,
    pairing_request_signature_bytes,
    validate_ieee_p1363_signature,
    validate_live_auth_error,
    validate_live_auth_receipt,
    validate_pairing_grant_envelope,
    validate_pairing_grant_payload,
    validate_pairing_invitation,
    validate_pairing_request_envelope,
    validate_pairing_request_payload,
    verify_pairing_invitation_proof,
)

CONTRACT_ROOT = Path(__file__).parents[1] / "contracts" / "live-auth" / "v0.1"
PAIRING_SECRET = "QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8"
FIXTURE_SIGNATURE = (
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
    "MDEyMzQ1Njc4OTo7PD0-Pw"
)
FINGERPRINTS = {
    "fixtures/valid_auth_error.json": "5e0c16464f0ca82c5abbe7f06d8f329d2d0462a4c0c61a128b3610fcc1869aec",
    "fixtures/valid_auth_receipt.json": "9242b65c8fc7583937de0c7a0e2310248d7eb93ffc36117c7612996ee8bd374f",
    "fixtures/valid_authenticated_request.json": "32435e32f7fd7381035b3b60ba98acc359de4c11011491679222a836f57bd91e",
    "fixtures/valid_pairing_grant_envelope.json": "3034cf282dd10157676cc64df4fe4c6d231045e5f1728ace2a1c0fe576eb5321",
    "fixtures/valid_pairing_grant_payload.json": "536674a84a72ccf57bf152f684fbec4c4e1929845403887c1c6fac1fbeab19ab",
    "fixtures/valid_pairing_invitation.json": "44fc84990bbea158f82484a4840c16292e7b1ca4e9b8573142a671796e7b570d",
    "fixtures/valid_pairing_request_envelope.json": "f3041e6e62c29beff7ad0d29c9e18a277894a70c46f3629511d8202ca83b7442",
    "fixtures/valid_pairing_request_payload.json": "d91635044981019fcc531a79df5c8aa3a3bc5c6766904df74041c7cc2098e1d6",
    "schemas/capture_splat.live_auth_error.v0.1.schema.json": "e03617e66b9fd4ac868fa1794625210269c38d248f8943fb6a7ef88026d206b8",
    "schemas/capture_splat.live_auth_receipt.v0.1.schema.json": "68b279511da9cc377968022aad6b9475c3144d7d031bf26284d44f11a31e9fc5",
    "schemas/capture_splat.live_pairing_grant_envelope.v0.1.schema.json": "0c5ae83baea553afa3892b30cd31a5f8cc91279f53a6e2a6936f23e333488e11",
    "schemas/capture_splat.live_pairing_grant_payload.v0.1.schema.json": "02ca6fb726f703daac47d440f889a114cd8e44066cb5e94bda2ac28a5c8cd7a3",
    "schemas/capture_splat.live_pairing_invitation.v0.1.schema.json": "146cec88f1a689c47d80e22dc20c6960d301aa6d52bde858aa96bb5c537a21b2",
    "schemas/capture_splat.live_pairing_request_envelope.v0.1.schema.json": "ede4a9f9c030f5529a65e95f88792f3961b30953a5e032e9ec7243a0c31a4a58",
    "schemas/capture_splat.live_pairing_request_payload.v0.1.schema.json": "58676e2c777cffba0cf2faee6487bef8d844a8c23fcaccf6d3d957f3d05bd58b",
}


def _fixture(name: str) -> dict:
    value = load_strict_json_bytes((CONTRACT_ROOT / "fixtures" / name).read_bytes())
    assert isinstance(value, dict)
    return value


def test_contract_files_have_pinned_fingerprints_and_strict_objects() -> None:
    actual = {
        relative: hashlib.sha256((CONTRACT_ROOT / relative).read_bytes()).hexdigest()
        for relative in FINGERPRINTS
    }
    assert actual == FINGERPRINTS
    for relative in FINGERPRINTS:
        if not relative.startswith("schemas/"):
            continue
        pending = [load_strict_json_bytes((CONTRACT_ROOT / relative).read_bytes())]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if value.get("type") == "object":
                    assert value.get("additionalProperties") is False
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)


def test_schema_patterns_require_canonical_base64url_tail_bits() -> None:
    invitation_schema = json.loads(
        (
            CONTRACT_ROOT
            / "schemas/capture_splat.live_pairing_invitation.v0.1.schema.json"
        ).read_text()
    )
    request_payload_schema = json.loads(
        (
            CONTRACT_ROOT
            / "schemas/capture_splat.live_pairing_request_payload.v0.1.schema.json"
        ).read_text()
    )
    request_envelope_schema = json.loads(
        (
            CONTRACT_ROOT
            / "schemas/capture_splat.live_pairing_request_envelope.v0.1.schema.json"
        ).read_text()
    )
    grant_envelope_schema = json.loads(
        (
            CONTRACT_ROOT
            / "schemas/capture_splat.live_pairing_grant_envelope.v0.1.schema.json"
        ).read_text()
    )

    bytes16_pattern = r"^[A-Za-z0-9_-]{21}[AQgw]$"
    pairing_id_pattern = r"^csp_[A-Za-z0-9_-]{21}[AQgw]$"
    desktop_id_pattern = r"^wsd_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
    public_key_pattern = r"^[A-Za-z0-9_-]{86}[AEIMQUYcgkosw048]$"
    signature_pattern = r"^[A-Za-z0-9_-]{85}[AQgw]$"
    payload_pattern = (
        r"^(?:[A-Za-z0-9_-]{4})*"
        r"(?:[A-Za-z0-9_-][AQgw]|[A-Za-z0-9_-]{2}[AEIMQUYcgkosw048])?$"
    )

    assert invitation_schema["properties"]["pairing_id"]["$ref"] == "#/$defs/pairingId"
    assert invitation_schema["$defs"]["pairingId"]["pattern"] == pairing_id_pattern
    assert invitation_schema["properties"]["desktop_id"]["$ref"] == "#/$defs/desktopId"
    assert invitation_schema["$defs"]["desktopId"]["pattern"] == desktop_id_pattern
    assert (
        invitation_schema["$defs"]["p256PublicKey"]["pattern"]
        == public_key_pattern
    )
    assert (
        request_payload_schema["$defs"]["requestId"]["pattern"]
        == r"^csr_[A-Za-z0-9_-]{21}[AQgw]$"
    )
    assert (
        request_payload_schema["$defs"]["bytes16"]["pattern"]
        == bytes16_pattern
    )
    assert (
        request_envelope_schema["$defs"]["signature"]["pattern"]
        == signature_pattern
    )
    assert (
        request_envelope_schema["properties"]["payload_b64u"]["pattern"]
        == payload_pattern
    )
    assert (
        grant_envelope_schema["$defs"]["signature"]["pattern"]
        == signature_pattern
    )
    assert (
        grant_envelope_schema["properties"]["payload_b64u"]["pattern"]
        == payload_pattern
    )

    invitation = _fixture("valid_pairing_invitation.json")
    for field, pattern in (
        ("pairing_id", pairing_id_pattern),
        ("desktop_id", desktop_id_pattern),
        ("desktop_public_key_b64u", public_key_pattern),
    ):
        assert re.fullmatch(pattern, invitation[field])
        assert not re.fullmatch(pattern, invitation[field][:-1] + "x")
    for fixture_name in (
        "valid_pairing_request_envelope.json",
        "valid_pairing_grant_envelope.json",
    ):
        assert re.fullmatch(payload_pattern, _fixture(fixture_name)["payload_b64u"])
    for invalid in ("A", "AB", "AAB"):
        assert not re.fullmatch(payload_pattern, invalid)


def test_valid_contract_fixtures_and_authenticated_request_vector() -> None:
    validate_pairing_invitation(_fixture("valid_pairing_invitation.json"))
    validate_pairing_request_payload(_fixture("valid_pairing_request_payload.json"))
    validate_pairing_request_envelope(_fixture("valid_pairing_request_envelope.json"))
    validate_pairing_grant_payload(_fixture("valid_pairing_grant_payload.json"))
    validate_pairing_grant_envelope(_fixture("valid_pairing_grant_envelope.json"))
    validate_live_auth_error(_fixture("valid_auth_error.json"))
    validate_live_auth_receipt(_fixture("valid_auth_receipt.json"))

    vector = _fixture("valid_authenticated_request.json")
    body = decode_base64url(vector.pop("body_b64u"), "body")
    expected = decode_base64url(vector.pop("canonical_ascii_b64u"), "canonical")
    assert len(body) == vector["content_length"]
    assert body_sha256(body) == vector["content_sha256"]
    assert canonical_authenticated_request_bytes(**vector) == expected
    assert expected.endswith(b"\n")


def test_identity_and_128_bit_identifier_derivation() -> None:
    invitation = _fixture("valid_pairing_invitation.json")
    request = _fixture("valid_pairing_request_payload.json")
    assert (
        derive_desktop_id(invitation["desktop_public_key_b64u"])
        == invitation["desktop_id"]
    )
    assert derive_device_id(request["device_public_key_b64u"]) == request["device_id"]
    assert derive_128bit_id("csp", bytes(range(16))) == invitation["pairing_id"]
    assert derive_128bit_id("csr", bytes(range(16, 32))) == request["request_id"]
    assert derive_128bit_id("csg", bytes(range(32, 48))) == _fixture(
        "valid_pairing_grant_payload.json"
    )["grant_id"]
    with pytest.raises(ValueError, match="prefix"):
        derive_128bit_id("req", bytes(16))
    with pytest.raises(ValueError, match="exactly 16"):
        derive_128bit_id("csr", bytes(15))


def test_identifiers_reject_noncanonical_base64url_tail_bits() -> None:
    invitation = _fixture("valid_pairing_invitation.json")
    invitation["pairing_id"] = invitation["pairing_id"][:-1] + "x"
    with pytest.raises(ValueError, match="invalid format"):
        validate_pairing_invitation(invitation)

    request = _fixture("valid_pairing_request_payload.json")
    request["request_id"] = request["request_id"][:-1] + "x"
    with pytest.raises(ValueError, match="invalid format"):
        validate_pairing_request_payload(request)

    grant = _fixture("valid_pairing_grant_payload.json")
    grant["grant_id"] = grant["grant_id"][:-1] + "x"
    with pytest.raises(ValueError, match="invalid format"):
        validate_pairing_grant_payload(grant)

    for field in ("desktop_id", "device_id"):
        receipt = _fixture("valid_auth_receipt.json")
        receipt[field] = receipt[field][:-1] + "B"
        with pytest.raises(ValueError, match="invalid format"):
            validate_live_auth_receipt(receipt)


def test_base64url_public_key_and_signature_shapes_fail_closed() -> None:
    assert decode_base64url(encode_base64url(b"\x00\xff"), "value") == b"\x00\xff"
    for invalid in ("AQ==", "A+", "A/", "AB"):
        with pytest.raises(ValueError, match="Base64URL"):
            decode_base64url(invalid, "value")
    with pytest.raises(ValueError, match="P-256 point"):
        derive_device_id(encode_base64url(b"\x04" + bytes(64)))
    with pytest.raises(ValueError, match="uncompressed P-256"):
        derive_device_id(encode_base64url(b"\x03" + bytes(64)))
    with pytest.raises(ValueError, match="64 bytes"):
        validate_ieee_p1363_signature(encode_base64url(bytes(63)), "signature")
    with pytest.raises(ValueError, match="r and s"):
        validate_ieee_p1363_signature(encode_base64url(bytes(64)), "signature")
    assert validate_ieee_p1363_signature(FIXTURE_SIGNATURE, "signature")


def test_strict_json_rejects_duplicates_nonfinite_and_noncanonical_values() -> None:
    assert canonical_json_bytes({"b": [1, True, None], "a": "é"}) == (
        '{"a":"é","b":[1,true,null]}'.encode()
    )
    for invalid in (
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b'{"a":1} trailing',
        b"\xff",
    ):
        with pytest.raises(ValueError):
            load_strict_json_bytes(invalid)
    for invalid in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            canonical_json_bytes({"value": invalid})
    with pytest.raises(ValueError, match="canonical JSON data"):
        canonical_json_bytes({"value": "\ud800"})


def test_invitation_rejects_fields_identity_permissions_timestamps_and_ttl() -> None:
    invitation = _fixture("valid_pairing_invitation.json")
    invalid = copy.deepcopy(invitation)
    invalid["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    del invalid["pairing_id"]
    with pytest.raises(ValueError, match="missing"):
        validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    invalid["desktop_id"] = "wsd_" + "A" * 43
    with pytest.raises(ValueError, match="does not match"):
        validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    invalid["permissions"].reverse()
    with pytest.raises(ValueError, match="canonical permission order"):
        validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    invalid["permissions"].append("session:delete")
    with pytest.raises(ValueError, match="unsupported permission"):
        validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    invalid["expires_at"] = "2026-07-29T10:35:00.001Z"
    with pytest.raises(ValueError, match="at most 300"):
        validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    invalid["expires_at"] = invitation["issued_at"]
    with pytest.raises(ValueError, match="positive"):
        validate_pairing_invitation(invalid)
    for timestamp in (
        "0000-01-01T00:00:00.000Z",
        "2026-07-29T10:30:00Z",
        "2026-07-29T10:30:00.00Z",
        "2026-07-29 10:30:00.000Z",
        "2026-07-29T10:30:00.000+00:00",
    ):
        invalid = copy.deepcopy(invitation)
        invalid["issued_at"] = timestamp
        with pytest.raises(ValueError, match="exactly milliseconds"):
            validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    invalid["desktop_name"] = "\ud800"
    with pytest.raises(ValueError, match="valid UTF-8"):
        validate_pairing_invitation(invalid)
    invalid = copy.deepcopy(invitation)
    invalid["unexpected"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        validate_pairing_invitation(invalid)


def test_request_grant_and_receipt_enforce_bindings_and_validity_window() -> None:
    request = _fixture("valid_pairing_request_payload.json")
    invalid_request = copy.deepcopy(request)
    invalid_request["request_id"] = "req_EBESExQVFhcYGRobHB0eHw"
    with pytest.raises(ValueError, match="invalid format"):
        validate_pairing_request_payload(invalid_request)
    invalid_request = copy.deepcopy(request)
    invalid_request["device_id"] = "csd_" + "A" * 43
    with pytest.raises(ValueError, match="does not match"):
        validate_pairing_request_payload(invalid_request)

    grant = _fixture("valid_pairing_grant_payload.json")
    for key, value, match in (
        ("pairing_epoch", 0, "at least 1"),
        ("pairing_epoch", True, "at least 1"),
        ("pairing_epoch", 9007199254740992, "at most 9007199254740991"),
        ("audience", "capture_splat.live.v0.2", "audience"),
        ("device_id", "csd_" + "A" * 43, "does not match"),
        ("not_before", "2026-07-29T10:31:00.999Z", "at or before"),
        ("expires_at", "2026-07-29T10:31:01.000Z", "positive"),
        ("expires_at", "2026-08-28T10:31:01.001Z", str(MAX_GRANT_TTL_SECONDS)),
    ):
        invalid_grant = copy.deepcopy(grant)
        invalid_grant[key] = value
        with pytest.raises(ValueError, match=match):
            validate_pairing_grant_payload(invalid_grant)

    receipt = _fixture("valid_auth_receipt.json")
    invalid_receipt = copy.deepcopy(receipt)
    invalid_receipt["pairing_epoch"] = 0
    with pytest.raises(ValueError, match="at least 1"):
        validate_live_auth_receipt(invalid_receipt)
    invalid_receipt = copy.deepcopy(receipt)
    invalid_receipt["pairing_epoch"] = 9007199254740992
    with pytest.raises(ValueError, match="at most 9007199254740991"):
        validate_live_auth_receipt(invalid_receipt)
    invalid_receipt = copy.deepcopy(receipt)
    invalid_receipt["authenticated_at"] = receipt["grant_expires_at"]
    with pytest.raises(ValueError, match="before grant expiry"):
        validate_live_auth_receipt(invalid_receipt)


def test_request_envelope_covers_exact_canonical_payload_and_invitation_proof() -> None:
    payload = _fixture("valid_pairing_request_payload.json")
    fixture = _fixture("valid_pairing_request_envelope.json")
    assert build_pairing_request_envelope(
        payload,
        device_signature_b64u=FIXTURE_SIGNATURE,
        pairing_secret_b64u=PAIRING_SECRET,
    ) == fixture
    payload_bytes = canonical_json_bytes(payload)
    assert decode_base64url(fixture["payload_b64u"], "payload") == payload_bytes
    assert pairing_request_signature_bytes(payload_bytes) == (
        PAIRING_REQUEST_SIGNATURE_DOMAIN + payload_bytes
    )
    assert pairing_invitation_proof(PAIRING_SECRET, payload_bytes) == (
        "twUi3bjTleWDkaZRduwOY4Q5Cn_DIGwR4fhkR5hlQU4"
    )
    assert verify_pairing_invitation_proof(
        PAIRING_SECRET,
        payload_bytes,
        fixture["invitation_proof_b64u"],
    )
    assert not verify_pairing_invitation_proof(
        PAIRING_SECRET,
        payload_bytes + b" ",
        fixture["invitation_proof_b64u"],
    )
    assert PAIRING_INVITATION_PROOF_DOMAIN.endswith(b"\x00")

    invalid = copy.deepcopy(fixture)
    invalid["payload_b64u"] = encode_base64url(
        json.dumps(payload, indent=2, sort_keys=True).encode()
    )
    with pytest.raises(ValueError, match="exact canonical"):
        validate_pairing_request_envelope(invalid)
    invalid = copy.deepcopy(fixture)
    duplicated = payload_bytes.replace(
        b'{"authority":"proposal_only"',
        b'{"authority":"proposal_only","authority":"proposal_only"',
        1,
    )
    invalid["payload_b64u"] = encode_base64url(duplicated)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        validate_pairing_request_envelope(invalid)
    invalid = copy.deepcopy(fixture)
    invalid["device_signature_b64u"] = encode_base64url(bytes(64))
    with pytest.raises(ValueError, match="r and s"):
        validate_pairing_request_envelope(invalid)


def test_grant_envelope_covers_exact_canonical_payload() -> None:
    payload = _fixture("valid_pairing_grant_payload.json")
    fixture = _fixture("valid_pairing_grant_envelope.json")
    assert build_pairing_grant_envelope(
        payload,
        desktop_signature_b64u=FIXTURE_SIGNATURE,
    ) == fixture
    payload_bytes = canonical_json_bytes(payload)
    assert decode_base64url(fixture["payload_b64u"], "payload") == payload_bytes
    assert pairing_grant_signature_bytes(payload_bytes) == (
        PAIRING_GRANT_SIGNATURE_DOMAIN + payload_bytes
    )
    invalid = copy.deepcopy(fixture)
    changed = copy.deepcopy(payload)
    changed["pairing_epoch"] = 2
    assert pairing_grant_signature_bytes(canonical_json_bytes(changed)) != (
        pairing_grant_signature_bytes(payload_bytes)
    )
    invalid["payload_b64u"] = encode_base64url(
        json.dumps(payload, indent=2, sort_keys=True).encode()
    )
    with pytest.raises(ValueError, match="exact canonical"):
        validate_pairing_grant_envelope(invalid)


def test_pairing_qr_uses_exact_bounded_scheme_and_canonical_invitation() -> None:
    invitation = _fixture("valid_pairing_invitation.json")
    uri = encode_pairing_qr_uri(invitation)
    assert uri.startswith(QR_PREFIX)
    assert len(uri.encode("ascii")) <= MAX_QR_URI_BYTES
    assert decode_pairing_qr_uri(uri) == invitation
    for invalid in (
        uri.replace(QR_PREFIX, "https://pair/", 1),
        uri + "?alias=1",
        uri + "#fragment",
        uri.replace(QR_PREFIX, QR_PREFIX + "%41", 1),
        QR_PREFIX + "A" * MAX_QR_URI_BYTES,
    ):
        with pytest.raises(ValueError):
            decode_pairing_qr_uri(invalid)
    noncanonical = QR_PREFIX + encode_base64url(
        json.dumps(invitation, indent=2, sort_keys=True).encode()
    )
    with pytest.raises(ValueError, match="exact canonical"):
        decode_pairing_qr_uri(noncanonical)


def test_authenticated_request_vector_is_exact_and_mutation_sensitive() -> None:
    vector = _fixture("valid_authenticated_request.json")
    body = decode_base64url(vector.pop("body_b64u"), "body")
    expected = decode_base64url(vector.pop("canonical_ascii_b64u"), "canonical")
    assert expected.decode() == (
        f"{AUTH_REQUEST_DOMAIN}\n"
        f"{vector['desktop_id']}\n"
        f"{vector['device_id']}\n"
        f"{vector['grant_id']}\n"
        "7\n"
        f"{vector['request_id']}\n"
        "2026-07-29T10:32:00.000Z\n"
        "PUT\n"
        "/api/capture-splat/live/v0.1/sessions/fixture-live-session-01/frames/1\n"
        "application/json\n"
        "17\n"
        f"{body_sha256(body)}\n"
    )
    for key, value in (
        ("counter", 8),
        ("request_id", "csr_Hx4dHBsaGRgXFhUUExIREA"),
        ("method", "POST"),
        ("content_sha256", body_sha256(body + b"!")),
    ):
        changed = dict(vector)
        changed[key] = value
        assert canonical_authenticated_request_bytes(**changed) != expected


@pytest.mark.parametrize(
    "path",
    (
        "/",
        "api/live",
        "/api/live/",
        "/api//live",
        "/api/./live",
        "/api/../live",
        "/api/live?x=1",
        "/api/live#x",
        "/api/%6cive",
        r"/api\live",
    ),
)
def test_authenticated_request_rejects_path_aliases(path: str) -> None:
    vector = _fixture("valid_authenticated_request.json")
    vector.pop("body_b64u")
    vector.pop("canonical_ascii_b64u")
    vector["path"] = path
    with pytest.raises(ValueError, match="raw canonical"):
        canonical_authenticated_request_bytes(**vector)


def test_authenticated_request_enforces_uint64_ids_media_type_and_empty_body() -> None:
    vector = _fixture("valid_authenticated_request.json")
    vector.pop("body_b64u")
    vector.pop("canonical_ascii_b64u")
    for counter in (-1, True, UINT64_MAX + 1):
        invalid = dict(vector)
        invalid["counter"] = counter
        with pytest.raises(ValueError):
            canonical_authenticated_request_bytes(**invalid)
    invalid = dict(vector)
    invalid["request_id"] = "req_EBESExQVFhcYGRobHB0eHw"
    with pytest.raises(ValueError, match="invalid format"):
        canonical_authenticated_request_bytes(**invalid)
    invalid = dict(vector)
    invalid["content_type"] = "Application/JSON"
    with pytest.raises(ValueError, match="lowercase MIME"):
        canonical_authenticated_request_bytes(**invalid)
    empty = dict(vector)
    empty.update(
        method="GET",
        content_type=None,
        content_length=0,
        content_sha256=body_sha256(b""),
    )
    assert b"\n-\n0\n" in canonical_authenticated_request_bytes(**empty)


def test_grants_receipts_and_errors_never_serialize_pairing_secret() -> None:
    invitation = _fixture("valid_pairing_invitation.json")
    assert invitation["pairing_secret_b64u"] == PAIRING_SECRET
    secret_free = (
        _fixture("valid_pairing_request_payload.json"),
        _fixture("valid_pairing_request_envelope.json"),
        _fixture("valid_pairing_grant_payload.json"),
        _fixture("valid_pairing_grant_envelope.json"),
        _fixture("valid_auth_receipt.json"),
        _fixture("valid_auth_error.json"),
    )
    for value in secret_free:
        serialized = canonical_json_bytes(value)
        assert b"pairing_secret" not in serialized
        assert PAIRING_SECRET.encode() not in serialized

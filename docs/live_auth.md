# Live pairing and authentication v0.1

This is the canonical Capture Splat contract for pairing one iPhone with one
World Studio Mac and authenticating the existing live-session protocol over a
local network. It is the security boundary before a future bounded iPhone
sender. This change does not enable phone networking or modify the capture
loop.

World Studio must keep the live receiver on loopback until an unexpired pairing
invitation succeeds. Bonjour is discovery only. Every LAN connection must use
TLS, match the certificate pin in the invitation and grant, and authenticate
each request. A discovered service name, IP address, or possession of an old
session ID is not authorization.

## Canonical contract

Capture Splat owns the byte-identical schemas and valid fixtures in
`contracts/live-auth/v0.1/`. World Studio mirrors that directory and pins every
file fingerprint.

The strict schemas are:

- `capture_splat.live_pairing_invitation.v0.1`
- `capture_splat.live_pairing_request_payload.v0.1`
- `capture_splat.live_pairing_request_envelope.v0.1`
- `capture_splat.live_pairing_grant_payload.v0.1`
- `capture_splat.live_pairing_grant_envelope.v0.1`
- `capture_splat.live_auth_error.v0.1`
- `capture_splat.live_auth_receipt.v0.1`

Every object rejects additional properties. JSON rejects duplicate keys,
`NaN`, positive or negative infinity, and lone UTF-16 surrogates that cannot be
encoded as well-formed UTF-8. Timestamps are real UTC RFC 3339 dates in years
`0001` through `9999` with exactly three fractional digits, for example
`2026-07-29T10:30:00.000Z`. Binary values use canonical unpadded Base64URL:
decoding and re-encoding must preserve the exact text. SHA-256 asset and
certificate digests use `sha256:` followed by 64 lowercase hexadecimal digits.
The JSON Schemas close the wire shape and canonical encodings; the shared
runtime validators additionally enforce relationships that JSON Schema cannot
prove, including identity hashes, curve points, permission order, validity
intervals, canonical payload bytes, and signatures.

Canonical JSON is UTF-8 with object keys sorted lexicographically, no
insignificant whitespace, and the JSON literals `true`, `false`, and `null`.
Pairing envelopes decode `payload_b64u`, parse it as strict JSON, validate its
payload schema, and require the decoded bytes to equal a fresh canonical
serialization. Signing or accepting a reserialized equivalent is forbidden.

## Identities and keys

Long-lived World Studio and Capture Splat identities use uncompressed P-256
X9.63 public keys: exactly 65 bytes beginning with `0x04`, encoded as unpadded
Base64URL. Implementations must verify that the coordinates are a real P-256
point.

The identity is the Base64URL SHA-256 digest of those exact 65 public-key bytes:

```text
desktop_id = "wsd_" + base64url(sha256(desktop_public_key_x963))
device_id  = "csd_" + base64url(sha256(device_public_key_x963))
```

Pairing, request, and grant IDs are canonical Base64URL encodings of exactly
128 random bits:

```text
csp_<22 Base64URL characters>
csr_<22 Base64URL characters>
csg_<22 Base64URL characters>
```

ECDSA signatures use SHA-256 and the 64-byte IEEE-P1363 `r || s` form, not ASN.1
DER. Both scalars must be nonzero and less than the P-256 order. This repository
defines structural validation plus deterministic canonical-byte and domain
vectors; iPhone key generation and signing remain future sender work.

## Pairing invitation and QR

World Studio creates a single-use invitation with:

- its identity, public key, display name, and TLS certificate pin;
- one `_capturesplat._tcp` Bonjour service name in `local.`;
- a 32-byte random invitation secret;
- all requested permissions, permanent `proposal_only` authority, and an
  expiry no more than 300 seconds after issue.

The QR payload is the canonical invitation object itself:

```text
capture-splat://pair/<base64url(canonical invitation JSON)>
```

The URI is ASCII, at most 4096 bytes, and permits no query, fragment,
percent-encoding, or alternate scheme. Bonjour results are matched to the QR
service identity, then TLS is checked against the QR certificate pin before any
pairing payload is sent.

The iPhone pairing request binds its generated device key and identity to the
invitation. The request envelope has exactly:

```json
{
  "schema": "capture_splat.live_pairing_request_envelope.v0.1",
  "payload_b64u": "...",
  "device_signature_b64u": "...",
  "invitation_proof_b64u": "..."
}
```

The device signature covers:

```text
ASCII("CAPTURE-SPLAT-PAIRING-REQUEST-V1") || 0x00 || payload_bytes
```

The invitation proof is:

```text
HMAC-SHA256(
  pairing_secret,
  ASCII("CAPTURE-SPLAT-PAIRING-PROOF-V1") || 0x00 || payload_bytes
)
```

World Studio accepts an invitation only once and never stores or returns its
raw secret after pairing. The signed grant envelope has exactly:

```json
{
  "schema": "capture_splat.live_pairing_grant_envelope.v0.1",
  "payload_b64u": "...",
  "desktop_signature_b64u": "..."
}
```

The desktop signature covers:

```text
ASCII("CAPTURE-SPLAT-PAIRING-GRANT-V1") || 0x00 || payload_bytes
```

The grant binds the desktop and device identities, device public key,
certificate pin, `capture_splat.live.v0.1` audience, pairing epoch, scopes,
Bonjour service, and validity window. It requires
`issued_at <= not_before < expires_at`; the lifetime from `not_before` is at
most 30 days.

The allowed scopes, always serialized in this order, are:

1. `receiver:status`
2. `session:create`
3. `session:resume`
4. `frame:put`
5. `asset:put`
6. `session:finalize`

Re-pairing or revocation advances the receiver-owned positive, JSON-safe
pairing epoch (maximum `9007199254740991`). A request
using a grant from any earlier epoch fails closed. Revocation, expiry, identity
mismatch, certificate mismatch, wrong audience, or missing scope must not fall
back to unauthenticated LAN access.

The pre-approval LAN listener exposes only:

```text
GET  /api/capture-splat/pairing/v0.1/health
POST /api/capture-splat/pairing/v0.1/requests
```

The request remains pending until the user explicitly approves that device on
the Mac. An exact identical retry of an approved request returns the same
durable grant, including after receiver restart. Rejected or cancelled requests
consume the invitation and later return `pairing_consumed`; a changed envelope
using the same pairing or request identity is a conflict. Before approval, the
listener exposes no live-session route.

## Authenticated live requests

The request body digest is computed over the raw transmitted bytes. The device
signs these exact ASCII bytes, including the final newline:

```text
CAPTURE-SPLAT-AUTH-V1
<desktop_id>
<device_id>
<grant_id>
<counter>
<request_id>
<timestamp>
<METHOD>
<raw canonical path>
<content-type-or-dash>
<content-length>
<sha256:hex>

```

The counter is canonical unsigned 64-bit decimal with no sign or leading
aliases. The request ID is a fresh `csr_` plus 128 random bits. Method is
uppercase `GET`, `POST`, or `PUT`. The raw path begins with `/`, contains no
query, fragment, percent encoding, backslash, repeated slash, dot segment, or
trailing slash. Content type is one lowercase media type, or `-` when absent.
Content length is canonical unsigned 64-bit decimal. The digest is
`sha256:` plus lowercase SHA-256 of the body bytes.

The signed values are carried by these exact HTTP headers:

| Canonical field | HTTP source |
|---|---|
| `device_id` | `X-Capture-Splat-Device` |
| `grant_id` | `X-Capture-Splat-Grant` |
| `counter` | `X-Capture-Splat-Counter` |
| `request_id` | `X-Capture-Splat-Request` |
| `timestamp` | `X-Capture-Splat-Time` |
| body digest | `X-Capture-Splat-Content-SHA256` |
| P1363 signature | `X-Capture-Splat-Signature` |
| content type | canonical `Content-Type`, or absent |
| content length | exact `Content-Length` |

`desktop_id` comes from the TLS receiver identity and selected grant; it is not
accepted as a caller-controlled alias. All
`/api/capture-splat/live/v0.1/...` routes, including health/status, require this
authentication after pairing.

The receiver verifies TLS, grant validity, current pairing epoch,
identity/key binding, required scope, request signature, and timestamp
freshness before dispatching to the existing live-session handler. It commits
the counter to a durable 256-bit replay window before streaming the body:
previously unseen out-of-order counters inside that window are accepted once,
while duplicate or older counters fail after restart. A body length or digest
failure leaves that counter consumed. `request_id` is fresh correlation
metadata generated by the sender, not replay authority. A lost response is
retried through the live-session protocol's existing idempotent resource
semantics under a new counter; the same authenticated request is never
accepted twice.

`capture_splat.live_auth_receipt.v0.1` records the authenticated identity,
grant, epoch, expiry, scopes, certificate pin, session, and permanent
`proposal_only` authority. It contains no private key, signature, invitation
secret, or reusable bearer credential. Errors use the strict
`capture_splat.live_auth_error.v0.1` code set and must not echo secrets.

## Capture-loop and sender boundary

This contract deliberately stops before iPhone sender implementation. The next
change must:

1. create immutable session identity before the first live session request;
2. enqueue only after source RGB and every declared sidecar are atomically
   durable;
3. retain paths, sizes, checksums, sequence IDs, and persistent resume state,
   never `ARFrame` or pixel buffers;
4. enforce queue byte and frame caps, limited in-flight uploads, retry budgets,
   and backpressure independently of keyframe acceptance;
5. remove only acknowledged queue records, never source capture evidence;
6. pause networking first during low disk, thermal pressure, backgrounding,
   receiver loss, or network failure;
7. require explicit LAN opt-in and a current paired grant.

`capture.json` is a finalized manifest and does not exist while the capture is
still in progress. The future sender must therefore atomically persist an
immutable random source-session seed before its first session request, then
bind the finalized `capture.json` checksum to that same identity at
finalization. It must never hash a mutable or not-yet-created manifest and call
that source identity.

Optional reconstruction workers remain isolated external processes. They may
consume checksum-bound received evidence and emit proposals, but they receive
no pairing secret or private key and cannot mutate the receiver store or source
capture.

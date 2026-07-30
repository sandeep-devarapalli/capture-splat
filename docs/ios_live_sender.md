# iOS Live Sender M1B

M1B-1 established the tested, dormant Swift foundation for authenticated
iPhone-to-World Studio evidence transfer. The additive progressive-session
contract now supplies its pre-manifest identity and final-manifest binding. It
does not start networking, change the capture loop, or claim physical-device
acceptance.

## Implemented boundary

The iOS target now contains four isolated components:

- `LiveAuthContract.swift` strictly decodes QR invitations, grants, auth errors,
  and canonical request vectors. It rejects duplicate keys, non-finite numbers,
  noncanonical Base64URL, malformed identifiers, and additional fields.
- `LiveAuthClient.swift` provides a persistent P-256 device identity,
  Keychain-backed grants, durable monotonic request counters, exact pairing
  retries, signed live requests, TLS 1.3 leaf-DER certificate pinning, redirect
  rejection, and grant removal after revocation, expiry, or identity failure.
- `LiveSenderQueue.swift` persists a checksummed canonical queue outside the
  source capture. Each queue is permanently bound to its paired desktop/device
  identities. Records contain only that authorization binding,
  session/sequence identity, and confined relative file paths, sizes,
  checksums, media types, and roles. Queue limits cover frames, bytes, and
  in-flight work.
- `LiveSender.swift` PUTs immutable session/frame metadata and assets, resumes
  before upload, reconciles durable ACK and missing-range state, retries
  idempotent resources under fresh signed request identities, and finalizes only
  after the receiver reports every frame durable.

The sender policy pauses new network work in the background, under serious or
critical thermal pressure, below the configured storage floor, or when the
network/receiver is unavailable. Pausing transport never changes keyframe
acceptance or deletes capture evidence.

## Persistence and retry rules

- Generate one device P-256 identity and keep its private key in Keychain.
- Verify every grant against the QR desktop key, device identity, discovery
  identity, scopes, TLS pin, pairing epoch, and validity window before storage.
- Discard the invitation secret after pairing; never write it to durable state.
- Reserve and atomically persist a counter before starting each live request.
- Generate and atomically persist one random 32-byte source-session seed before
  the first v0.2 session request. A restart must reload the exact seed rather
  than replace it.
- Retry a pairing request with the exact same body and request identity.
- Retry a live resource with the same bytes/checksum/path but a fresh counter,
  request ID, timestamp, and signature.
- Remove a queue record only when an ACK or resume response proves that sequence
  durable. Never remove or rewrite the referenced source file.
- Retain each acknowledged sequence's exact reference hash until finalization so
  retries remain byte-identical; the capped state envelope fails closed before
  that integrity ledger can grow beyond its bound.
- Reject a retained queue when the validated grant belongs to another desktop
  or device; grant rotation is allowed only for the same identity pair.
- Treat malformed state, altered files, unsafe paths, symlinks, inconsistent
  ACKs, missing frames, and post-finalization writes as fail-closed errors.

The integration layer must place queue/counter state under the app's
Application Support directory, never inside a capture bundle.
The dormant foundation does not yet generate or write the seed-bearing v0.2
session metadata. Its caller must first atomically write that immutable file;
only then may `LiveSenderQueue.open` validate its bytes and seed-derived ID and
durably bind the file path, size, and checksum in queue state. Application
Support ownership and creation wiring remain the next integration PR.

## Deliberately not activated

This change does not add:

- QR scanner, pairing, queue, or transfer UI;
- Bonjour browsing or local-network permission declarations;
- a `CaptureController` callback or any change to frame acceptance/writers;
- background transfer entitlement or scheduling;
- physical TLS/Bonjour/firewall validation;
- two iPhone-to-Mac capture cycles;
- progressive reconstruction workers.

The host probe compiles the production Swift sources, uses deterministic
receiver actors, and performs a real TLS 1.3 loopback handshake with positive
and negative leaf-certificate pins. It proves bounded deterministic behavior
and transport/contract interoperability, not a physical LAN transfer.

## Progressive contract bridge

The v0.1 replay session identifies a session with the size and checksum of
finalized `capture.json`. That manifest does not exist while recording, and
World Studio correctly treats accepted session metadata as immutable.

The mirrored v0.2 contract resolves that timing boundary:

1. `capture_splat.live_session.v0.2` carries an immutable random 32-byte
   source-session seed and derives a stable `csl_...` session identity from it;
2. its expected frame count remains explicitly null during progressive upload;
3. `capture_splat.live_finalize.v0.2` binds the final sequence and completed
   `capture.json` path, schema, size, and checksum during fail-closed
   finalization; and
4. the v0.1 replay session and finalization bodies remain unchanged.

The receiver binds the authenticated manifest reference but does not receive
the raw `capture.json` bytes in this phase. That proof boundary must remain
visible in the finalized handoff.

The exact acknowledged-frame hash ledger remains authoritative until verified
finalization. Its long-session size and latency gate is tracked in
[issue #35](https://github.com/sandeep-devarapalli/capture-splat/issues/35).
Do not prune hashes or raise the 48 MiB state limit; if the benchmark fails,
land the specified chunked checksummed index before capture-loop integration.

## Next integration order

1. Add explicit LAN opt-in plus QR scan and exact Bonjour service resolution.
2. Place queue/counter state under Application Support and expose queue limits.
3. Add one nonblocking callback only after each declared frame file is
   atomically durable. Do not wait for optional sidecars that are still writing.
4. Run two physical iPhone-to-Mac cycles, including receiver restart and Wi-Fi
   interruption, while measuring memory, storage, thermal state, writer drops,
   throughput, recovery, and finalization.

Live frames, depth, masks, meshes, and any later reconstruction-worker output
remain proposals. They are not measurement, collision, navigation, semantic,
or physics authority.

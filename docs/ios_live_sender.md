# iOS Live Sender M1B

M1B-1 established the Swift foundation for authenticated iPhone-to-World Studio
evidence transfer. The capture binding now connects it through one long-lived
bounded serial bridge while preserving local-first capture. This is an
implemented code path, not physical-device LAN acceptance.

## Product status

Manual Export is the production iPhone path. Live transfer is disabled by
default, remains opt-in and experimental, and is not required by capture,
reconstruction, or World Studio package review.

In a physical iPhone trial on 2026-08-11, the capture began at fair thermal
state and reached serious state after 15.4 seconds with live transfer enabled.
The sender recorded zero upload attempts and zero completed sender runs, so the
trial does not isolate network upload as the cause; camera, LiDAR, mesh
guidance, video encoding, and pre-send work were also active. The result holds
iPhone live-transfer promotion and removes it as a product dependency. The
strict acceptance harness remains useful for future devices and controlled
thermal experiments.

## Implemented boundary

The iOS target now contains six isolated components:

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
- `LiveCaptureJournal.swift` commits one bounded, canonical accepted-frame
  record after each required atomic source write and an exact finalization
  marker only after atomic `capture.json`. It rejects gaps, altered evidence,
  symlinks, noncanonical records, and conflicting retries. A frame commit is an
  O(1)-per-frame operation; it does not rewrite a growing capture manifest.
- `LiveCaptureSenderBridge.swift` serializes capture events through one bounded
  stream, uses strict production encoders for canonical v0.2 session and v0.1
  frame metadata, persists checksummed pending/current recovery pointers plus
  the paired binding and queue before upload, restores the exact durable
  journal, incrementally refills its bounded send window, and invokes
  `LiveSender` outside the capture queues. It owns one event consumer and one
  send worker rather than creating a task per frame.

The sender policy pauses new network work in the background, under serious or
critical thermal pressure, below the configured storage floor, or when the
network/receiver is unavailable. Pausing transport never changes keyframe
acceptance or deletes capture evidence. Background, unavailable-network,
inactive-pairing, and serious/critical thermal transitions cancel the current
transport task immediately. The lock-backed gate is updated directly rather
than waiting behind hashing or queue work. Restoring the exact paired desktop,
returning to foreground, regaining network, or leaving serious/critical thermal
state wakes the same bounded worker; receiver-retry backoff continues without a
new frame event while those gates remain open.

At serious or critical thermal pressure, the bridge also defers live frame
hashing, metadata construction, and queue admission. The accepted-frame journal
is already durable, so cooling or relaunch can backfill the exact same evidence
before resume. Local capture and `capture.json` finalization remain independent;
after finalization the operator may manually export from Projects without
clearing the durable live transfer.

The capture callback runs only after RGB, depth, and enabled-confidence files
are atomically written and the immutable accepted-frame journal record is
durable. It returns without waiting for checksum work, networking, ACKs,
optional masks, point-cloud previews, or other sidecars. The bridge retains
only immutable file references and value metadata, never `ARFrame` or pixel
buffers. A journal failure disables live publication for that capture but does
not reject or delete the locally accepted frame.

## Persistence and retry rules

- Generate one device P-256 identity and keep its private key in Keychain.
- Verify every grant against the QR desktop key, device identity, discovery
  identity, scopes, TLS pin, pairing epoch, and validity window before storage.
- Discard the invitation secret after pairing; never write it to durable state.
- Reserve and atomically persist a counter before starting each live request.
- Before `captureStarted` can return accepted, synchronously generate and
  atomically persist canonical `metadata/live/session.json` with one random
  32-byte source-session seed, derive its stable session ID, inspect its exact
  path/size/SHA-256 reference, and commit all three to the pending pointer. A
  restart must reload those exact bytes rather than replace the seed or
  identity.
- Retry a pairing request with the exact same body and request identity.
- Retry a live resource with the same bytes/checksum/path but a fresh counter,
  request ID, timestamp, and signature.
- Remove a queue record only when an ACK or resume response proves that sequence
  durable. Never remove or rewrite the referenced source file.
- Treat queue frame/byte limits as the current send window, not a maximum
  durable-journal length. After ACKs drain queue records, re-read the journal
  idempotently and admit the next records that fit; enqueue finalization only
  after every preceding journal frame has passed through the window.
- Retain each acknowledged sequence's exact reference hash until finalization so
  retries remain byte-identical; the capped state envelope fails closed before
  that integrity ledger can grow beyond its bound.
- Reject a retained queue when the validated grant belongs to another desktop
  or device; grant rotation is allowed only for the same identity pair.
- Treat malformed state, altered files, unsafe paths, symlinks, inconsistent
  ACKs, missing frames, and post-finalization writes as fail-closed errors.

Queue, counter, and session-binding state lives under
`Application Support/CaptureSplat/live-sender/v0.1`, never inside a capture
bundle. Canonical session and frame metadata are atomically written under the
source bundle's `metadata/live/` directory before its queue record becomes
eligible for upload. The capture also owns one immutable canonical record per
accepted live frame under `metadata/live/accepted-frames/` and, after local
finalization, `metadata/live/finalization.json`.

Before returning an accepted capture-start disposition, the bridge
synchronously writes or validates the seed-bearing canonical session metadata,
computes its exact file reference, and claims a checksummed
`pending-capture.json` containing the capture, desktop, session ID, and metadata
reference. Only then does it enqueue the asynchronous start event. A process
failure before the first accepted frame therefore restarts from the same
session identity and immutable metadata bytes.

After sender binding and queue state exist, the bridge atomically writes
`current-session.json` with the exact desktop/device/session/capture binding
before clearing the pending pointer. On restart it follows only one of those
exact pointers, validates the authorization binding, and replays the durable
journal into the idempotent queue. It never enumerates captures looking for
work. Forgetting or resetting the paired Mac is blocked while either pointer
represents an unfinished transfer.

Queue capacity is a sliding upload window. Journal records that do not
currently fit are not rejected as capture evidence or deleted; they remain
durable in the capture. After a sender pass removes ACK-proven records, the same
worker reloads the journal, fills newly available frame/byte capacity, and
continues without requiring a new capture event. Finalization enters the queue
only when every prior accepted record can be reconciled. The separate
360-frame product and exact-ledger caps remain unchanged.

App launch starts no unsolicited Bonjour discovery. If the user previously
authorized a pending transfer and its pending-capture or current-session
pointer, Keychain grant, and pinned identity all validate, the foreground app
may automatically resume that same transfer under the network, thermal,
storage, and pairing policy. Recovery does not create a new session, rewrite
source evidence, synthesize a missing `capture.json`, or re-finalize an
interrupted local capture.

Durable recovery is fail-closed behind four independent wake gates:

1. the current paired desktop ID must exactly equal the desktop stored in the
   pending pointer or current session authorization, including a second check
   around connection creation;
2. the app must be foreground;
3. the network path must be available; and
4. thermal state must remain below the configured serious/critical pause.

An allowed transition wakes the one sender worker. A disallowed transition
cancels its current drive before another operation while preserving the
pointer, journal, queue, binding, and source files for resume.

## Evidence-preserving recovery and abandonment

If atomic `capture.json` publication fails for a capture with zero accepted
frames, CaptureController emits an abort event. The bridge clears that
capture's pending/current pointers only after reloading the accepted-frame
journal, proving it is empty, and confirming no finalization marker exists.
This narrow zero-frame case never infers success and does not delete session
metadata, queue, binding, or capture files.

If any accepted-frame journal record exists, or manifest/finalization
publication fails after evidence was accepted, the pending/current pointer
remains protected. The sender neither abandons nor fabricates finalization.
The user may recover the exact transfer or use the pairing sheet's explicit
**Abandon Pending Live Transfer** flow.

Abandonment requires a second confirmation and synchronously removes only these
two fixed Application Support files:

- `pending-capture.json`
- `current-session.json`

It does not delete or rewrite the capture directory, source RGB/depth/confidence
files, accepted-frame journal, live metadata, queue, session binding, or
acknowledged-reference ledger. Those retained artifacts remain available for
inspection, but automatic sender resume stops because its authoritative pointer
was removed.

## Remaining proof boundaries

This change does not add or claim:

- transfer-progress UI or progressive reconstruction-worker UI;
- any change to keyframe acceptance or the existing atomic source writers;
- background transfer entitlement or scheduling;
- physical TLS/Bonjour/firewall validation;
- two iPhone-to-Mac capture cycles;
- progressive reconstruction workers.

Host-probe coverage is scoped to the production Swift sources, deterministic
receiver actors, and a TLS 1.3 loopback handshake with positive and negative
leaf-certificate pins. Even a passing host probe is host evidence only and
cannot establish physical LAN transfer.

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
visible in the finalized handoff. On the phone, a bare `capture.json` is not
enough to authorize live finalization: only the strict journal marker committed
after atomic manifest publication, with the exact path, byte size, and SHA-256,
can enqueue the v0.2 finalization.

The exact acknowledged-frame hash ledger remains authoritative until verified
finalization. Its checksum-bound long-session gate passed on the designated
iPhone 16 Pro Max and closed
[issue #35](https://github.com/sandeep-devarapalli/capture-splat/issues/35)
without a storage redesign. The fixed matrix, numeric budgets, result fields,
and proof boundary are in the
[ACK-index benchmark protocol](ios_live_ack_index_benchmark.md). Retain the
exact ledger and current 360-frame product cap; do not prune hashes or raise
the 48 MiB state limit.

## Pairing application wiring

The iPhone app now exposes pairing as an explicit sheet:

- its VisionKit scanner accepts only the canonical `capture-splat://pair/`
  invitation and is unavailable while a capture is active;
- it browses only after a QR or paste action, filters the exact Bonjour
  service name/type/domain from that invitation, and resolves only that match;
- the existing client then performs pinned TLS, signed pairing, explicit Mac
  approval, scoped grant validation, and durable request-counter registration;
- private keys, grants, pending signed requests, and an authoritative one-Mac
  recovery pointer stay in Keychain; and
- a checksummed rebuildable desktop cache plus request counters live under
  `Application Support/CaptureSplat/live-sender/v0.1`.

Restoring pairing state starts no browser or listener. A retry resubmits the
exact Keychain-backed pending pairing request.
Pairing cancellation or backgrounding stops discovery and the in-flight
request, then reconciles Keychain before declaring cancellation complete so a
grant issued during that race remains visible. The app exposes one current Mac
at a time; pair another only after locally forgetting and remotely revoking the
first. Local forget removes only the phone-side grant, so the user must also
revoke the device in World Studio for immediate Mac-side invalidation.
If the rebuildable cache is corrupt, the Keychain pointer restores the known
Mac without discovery. If even that pointer is unreadable, pairing remains
blocked until the user explicitly resets the entire local live Keychain service
and restarts the app; World Studio revocation is still required. That reset and
local forget remain blocked while a pending/current live transfer pointer
exists.

Pairing recovery itself does not discover at app launch. The bounded sender may
connect automatically only to resume an exact previously user-authorized
pending transfer represented by its pending-capture or current-session pointer,
when the current Keychain grant, pinned desktop identity, replay counters, and
pressure policy validate.

## Next validation order

1. The checksum-bound
   [Release benchmark](ios_live_ack_index_benchmark.md) passed at 360 and 720
   accepted-frame identities; retain the exact ledger and 360-frame cap.
2. Run two physical iPhone-to-Mac cycles, including receiver restart and Wi-Fi
   interruption, while measuring memory, storage, thermal state, writer drops,
   throughput, recovery, and finalization.
3. Keep progressive evidence UI and optional reconstruction-worker integration
   as separate follow-up work.

Live frames, depth, masks, meshes, and any later reconstruction-worker output
remain proposals. They are not measurement, collision, navigation, semantic,
or physics authority.

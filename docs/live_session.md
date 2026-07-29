# Live Session Phase 1

Phase 1 is a replay-first, loopback-only evidence path from Capture Splat to an
explicitly listening World Studio. It transports source frames, camera poses,
capture quality, and optional depth/confidence/masks. It does not train,
incrementally update, or claim a live Gaussian reconstruction.

Phase 1 is the completed Capture Splat **Live Session Foundation** milestone. The
authenticated sender and progressive-world direction is specified by the
[World Studio World Compiler Blueprint](https://github.com/sandeep-devarapalli/world-studio/tree/main/docs/blueprints/world-compiler-v0.1).
The active contract in this repository remains canonical; archived proposal schemas in
the blueprint are provenance only.

The next boundary is the strict [live pairing and authentication
contract](live_auth.md). It adds QR-bound device identity, TLS pinning, scoped
grants, revocation epochs, and per-request replay protection without changing
the Phase 1 evidence schemas or enabling the iPhone sender.

## Canonical contract

Capture Splat owns the byte-canonical schemas and fixtures under
`contracts/live-session/v0.1/`. World Studio mirrors this directory exactly and
checks the file fingerprints in tests.

- `capture_splat.live_session.v0.1` binds a session ID to the source
  `capture.json` checksum, expected frame count, coordinate convention, and
  permanent `proposal_only` authority.
- `capture_splat.live_frame.v0.1` uses one-based contiguous sequence IDs. It
  records timestamp and clock domain, actual source RGB dimensions and SHA-256,
  independently dimensioned pinhole calibration, row-major 4x4
  `camera_to_world`, tracking/quality evidence, and optional depth, confidence,
  and typed person/valid/object mask references.
- `capture_splat.live_ack.v0.1` reports durable received and contiguous counts,
  out-of-order pending count, next expected sequence, and missing ranges.

All objects reject additional properties. JSON is parsed with non-finite values
disabled. Checksums have the form `sha256:` plus 64 lowercase hexadecimal
characters. Asset references are safe POSIX-relative paths; replay rejects
absolute paths, URI-like paths, backslashes, traversal, missing files, and
symlinks whose real path leaves the capture root.

The iPhone currently records full-resolution RGB and depth-grid intrinsics.
Replay preserves those original focal values and their calibration width and
height. It records whether that calibration applies to depth, confidence, the
source frame, or is unknown. World Studio may scale a copy only when it builds
an RGB display camera; the transferred evidence is not rewritten.

## Loopback transport

The default receiver is `http://127.0.0.1:43127`. The CLI accepts only HTTP on
`127.0.0.1`, `::1`, or `localhost`. The receiver remains stopped until the user
explicitly starts it in World Studio.

The dependency-free HTTP/1.1 protocol uses:

```text
GET  /api/capture-splat/live/v0.1/health
PUT  /api/capture-splat/live/v0.1/sessions/{session-id}
GET  /api/capture-splat/live/v0.1/sessions/{session-id}
PUT  /api/capture-splat/live/v0.1/sessions/{session-id}/frames/{sequence-id}
PUT  /api/capture-splat/live/v0.1/sessions/{session-id}/frames/{sequence-id}/assets/{role}
POST /api/capture-splat/live/v0.1/sessions/{session-id}/finalize
```

Asset roles are `source`, `depth`, `confidence`, `mask-person`, `mask-valid`,
and `mask-object`. The receiver acknowledges an asset only after its declared
byte length and SHA-256 pass. A frame becomes received only after its strict
metadata and every declared asset are durable. Identical retries are
duplicates; changed metadata or bytes for the same identity are conflicts.
Valid out-of-order frames are durable immediately. Missing sequence IDs remain
gaps in the camera trajectory.

Finalization declares the last sequence ID, requires every frame through it,
rehashes all committed assets, and then atomically seals the session. Repeating
the same finalization is idempotent. Missing frames, corrupt assets, or changed
final state fail closed.

## Replay and recovery

Start the World Studio receiver, then run:

```bash
capture-splat replay-live-session \
  --capture /path/to/capture \
  --receiver http://127.0.0.1:43127 \
  --session-id room-replay-01 \
  --delay-ms 100
```

The receiver may also be set with `CAPTURE_SPLAT_LIVE_RECEIVER`. Use a stable
`--session-id` for cross-process recovery:

```bash
capture-splat replay-live-session \
  --capture /path/to/capture \
  --session-id room-replay-01 \
  --resume
```

`--resume` re-creates the HTTP connection, queries durable receiver state, and
sends only missing sequences. Without it, a requested simulated disconnect
prints an `interrupted` strict summary and exits nonzero. The following knobs
are deterministic test tools:

- `--shuffle --seed N` changes frame send order without changing sequence IDs;
- `--duplicate-every N` repeats every Nth primary frame;
- `--disconnect-after N` closes the persistent connection after N newly
  acknowledged frames;
- `--disconnect-seconds S` controls the recovery pause.

Replay creates no queue, checkpoint, or generated session in the capture
folder. World Studio owns durable receiver state outside Git.

## Future iOS bounded store-and-forward sender

The future phone sender must remain downstream of capture persistence:

1. Enqueue a frame only after RGB and all declared sidecars have completed
   atomic local writes. Queue records reference file paths and immutable
   checksums; they never retain `ARFrame`, `CVPixelBuffer`, or other capture
   buffers.
2. Use one bounded sender, a small fixed queue, explicit backpressure, and a
   limited number of in-flight uploads. Network work must not create one task
   per AR frame or compete with the capture writer.
3. Remove only acknowledged queue copies/records. Never delete or rewrite the
   source capture evidence as a side effect of transport.
4. Persist the session ID, one-based sequence identity, and acknowledgement
   watermark. On reconnect or app relaunch, query receiver missing ranges and
   send only missing durable files.
5. Prefer local capture correctness under low disk, thermal pressure,
   backgrounding, receiver loss, or network failure. Pause or shed optional
   transmission before reducing the capture gate or losing source evidence.
6. Keep the sender off by default. Any transport beyond loopback must implement
   the [live pairing and authentication contract](live_auth.md), including
   explicit LAN opt-in, QR-bound receiver identity, TLS pinning, scoped grants,
   request signatures, anti-replay state, and credential revocation. Plain LAN
   HTTP is not an acceptable extension of Phase 1.

These requirements describe future work only. No iPhone capture-loop source is
changed by Phase 1.

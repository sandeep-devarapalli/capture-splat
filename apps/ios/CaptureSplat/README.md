# Capture Splat iOS App

Capture Splat is a native iPhone capture app for recording Video 3DGS input. It writes `capture_splat.v0.3` export folders for the host pipeline in this repository.

## Build

Open `CaptureSplat.xcodeproj` in Xcode, select a physical iPhone, and run the
`CaptureSplat` scheme. For physical-device signing, copy
`CaptureSplat/Config/CaptureSplat.local.xcconfig.example` to
`CaptureSplat/Config/CaptureSplat.local.xcconfig`, then set a unique bundle
identifier and your development team. The local file is ignored by Git, so
device signing no longer dirties the shared Xcode project.

The simulator cannot validate camera, LiDAR, motion, or real capture timing.
Use the [iPhone Xcode diagnostics](../../../docs/iphone_xcode_diagnostics.md)
playbook for crash triage and the two-cycle physical-device stability smoke.

## Export

The app writes session folders under the app Documents directory:

```text
capture.json
rgb/
depth/
confidence/
imu.csv
gps.csv
metadata/
room_plan/
geometry/
masks/person/
```

Stopping a capture waits for the continuous video and queued RGB-D writes before
publishing `capture.json`. `metadata/finalization_report.json` records whether
the bundle finalized cleanly, while `capture_policy.json` and
`sensor_capabilities.json` preserve the active quality policy and device
fallback evidence.

The **Projects** tab scans saved `capture_splat_*` folders on launch, foreground,
and after finalization. It lists finalized, partial, and malformed bundles
separately, opens an available LiDAR point preview, reports RoomPlan
availability, and shares the selected folder. Partial folders remain recovery
evidence; the app does not claim it can reconstruct missing in-memory frame
state or re-finalize them after relaunch.

On supported devices the quality-first capture configuration also samples
non-empty ARKit person stencils at up to 5 Hz and snapshots a capped classified
ARKit mesh during finalization. These derived sidecars are mask/geometry
proposals for host processing and review; they are not 3DGS, collision, metric,
semantic, or navigation authority. Unsupported devices keep recording the core
RGB-D stream and report the fallback in `sensor_capabilities.json` and
`session_events.jsonl`.

Video 3DGS Max is the default capture path. The app records dense, sharp RGB-D
keyframes with camera metadata and quality reports; the host pipeline handles
COLMAP and VkSplat training. Use the capture intent menu to tag the pass as
Desk / Cluster, Room Walkthrough, Object Orbit, Corridor / Passage, Wall /
Facade, Outdoor Object, RoomPlan + 3DGS, or Detail Repair without changing the
underlying quality gates.

Only Object Orbit uses a subject target. The user must tap **Lock Subject**
after the app reports stable center depth, and Record remains disabled until
that explicit lock succeeds. Record never locks a subject automatically. All
other capture intents remain full-scene passes. Video 3DGS Max always uses
Smart quality-gated keyframes; there is no timed capture fallback. Object Orbit
readiness requires low, middle, and high-angle support, and its per-frame mask
follows the projected target depth as the camera moves.

Supported LiDAR iPhones can open Room Plan review during the same video capture
workflow and export `room_plan/room.usdz`, `room_plan/room_plan_report.json`,
and `room_plan/room_semantics.json`. This is capture guidance and semantic
proposal evidence for layout coverage, not a 3DGS quality claim.

`metadata/spatial_guidance_report.json` records live-guidance coverage and
performance evidence. Version 0.2 distinguishes intentional anchor-update
throttling from actual drops, records processing-budget overruns, and reports
time spent with mesh, map-only, pose-only, or hidden guidance under each thermal
policy.

## World Studio pairing and bounded sender

M1B compiles the bounded sender into the app target. It
implements QR invitation parsing, P-256 device identity, Keychain-backed grants,
TLS 1.3 leaf-certificate pinning, signed requests with durable counters, a
checksummed frame/byte-bounded queue, ACK/resume reconciliation, limited
in-flight uploads, retry, paired desktop/device queue binding, and
thermal/storage/background/network pause policy.

The World Studio pairing sheet is now active only after an explicit user
action. It scans QR codes, resolves the exact `_capturesplat._tcp` service
identity from that invitation, pins TLS, waits for Mac approval, and recovers a
pending or current grant after restart without performing discovery at app
launch. Device keys, grants, pending signed requests, and an authoritative
one-Mac recovery pointer remain in Keychain. A checksummed rebuildable desktop
cache and durable request counters live under
`Application Support/CaptureSplat/live-sender/v0.1`.
The app exposes one current Mac at a time; revoke and forget it before pairing
another.

One long-lived bounded serial bridge now receives capture events. A frame event
is emitted only after its RGB, depth, and enabled-confidence files are
atomically written and one bounded immutable accepted-frame record is committed
under `metadata/live/accepted-frames/`. This O(1)-per-frame journal write
precedes the callback; hashing, networking, ACKs, masks, optional previews, and
other sidecars remain off the capture queues. The bridge never retains
`ARFrame` or pixel buffers. It writes canonical live metadata and checksummed
Application Support binding/queue state before upload.

The additive v0.2 contract derives a stable session ID from an atomically
persisted random seed before `capture.json` exists. Before capture start returns
accepted, the app synchronously writes canonical `metadata/live/session.json`,
derives its stable session ID, inspects the file's exact path, size, and
SHA-256, and records that immutable reference in `pending-capture.json`. A
first-frame crash therefore reloads the same seed and identity. Atomic
`capture.json` publication is followed by an exact path, byte-size, and SHA-256
journal marker; only that marker triggers the final schema binding. The
checksummed
`pending-capture.json` and `current-session.json` Application Support pointers
restore only the exact previously user-authorized transfer and journal after
restart. This performs no unsolicited Bonjour discovery, never synthesizes a
missing `capture.json`, and cannot re-finalize a partial local capture.
Backgrounding, network loss, loss of pairing authorization, and serious or
critical thermal transitions cancel active transport while source capture
continues independently. Recovery can wake the one sender worker only when the
currently paired desktop exactly matches the durable pointer/binding, the app
is foreground, the network is available, and thermal policy permits transfer.

A zero-frame manifest failure automatically clears the transfer pointers only
after the accepted-frame journal is verified empty and no finalization marker
exists. Any nonempty journal or manifest/finalization failure remains protected
for explicit recovery. The pairing sheet then offers a confirmed **Abandon
Pending Live Transfer** action. It removes only Application Support's fixed
`pending-capture.json` and `current-session.json` pointers; the capture source,
journal, queue, session binding, and metadata remain intact.

The queue's frame/byte limits bound a sliding send window. Durable journal
records beyond current capacity stay local; after ACKs drain queued records,
the one sender worker incrementally refills the window and eventually enqueues
the finalization marker. A capture need not fit in the queue at once. The exact
ledger still retains its separate 360-frame product cap.
Two physical iPhone-to-Mac cycles with receiver restart and Wi-Fi interruption
remain required before any physical LAN/device acceptance claim. Live and
progressive evidence remains `proposal_only`, never measurement, collision,
navigation, semantic, or physics authority. See
[iOS Live Sender M1B](../../../docs/ios_live_sender.md).

Saved frames include `capture_quality` metadata. The host pipeline uses accepted
keyframes for ingest and COLMAP export, so rejected candidates remain diagnostic
evidence rather than trainer input.

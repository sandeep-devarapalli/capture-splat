# Portal and Route Evidence

`capture-splat validate-portal-route-evidence` is a producer-side evidence
gate. It validates a future open-door Room-01 capture without modifying the
capture, the v0.3 World Studio handoff, the unsimplified hybrid surface, or the
held reduced collider.

## Derive a capture diagnostic

Use the prepared capture to measure whether its RoomPlan proposal, full video
trajectory, and retained RGB-D frames could support a future evidence package:

```bash
capture-splat derive-portal-route-evidence \
  --prepared-capture runs/room_01/prepare/frames/capture.json \
  --out runs/room_01/portal_route_derivation
```

After SfM, add `--sfm-package /path/to/reconstruction`. The package must contain
`images/` and `sparse/0/images.txt`. A registered name counts only when its
canonical, case-sensitive path selects regular non-symlink bytes whose size and
SHA-256 exactly match the prepared RGB. The report binds those matches with a
deterministic aggregate parity digest. This is not a metric
RoomPlan-to-COLMAP registration receipt.

Every consumed path component is opened relative to a held directory
descriptor and must match its physical spelling exactly, including on
case-insensitive APFS. Casefold aliases, inode aliases, parent/leaf swaps,
non-positive or duplicate COLMAP image IDs, non-positive camera IDs, and
non-finite or non-unit quaternions reject. JSON, trajectory, `images.txt`, and
record counts are bounded. Directory enumeration streams through a held
descriptor and rejects above 200,000 entries or 64 MiB of UTF-8 names per
directory. Each confined root additionally rejects above 128 simultaneously
held directories, 512 scans, 1,000,000 aggregate scanned entries, 256 MiB of
aggregate scanned UTF-8 names, or 128 path components. Paired SfM/prepared
images are limited to 64 MiB each and 16 GiB of combined bytes; the command
compares both secure file sizes before reading and hashing either member of a
pair. Crossing totals remain exact while only a bounded diagnostic prefix is
retained in the report. The output report is reserved exclusively outside both
immutable inputs and is never overwritten. Its exact bytes and SHA-256 are read
back through the held descriptor, and the bound input/output state is checked
again through context exit, including rejected reports. A host without
descriptor-relative directory opens/scanning and `O_NOFOLLOW` support rejects
this command instead of falling back to path-based reads.

All CLI arguments must use physical canonical paths, not symlink aliases. On
macOS, use `/private/tmp/...` and `/private/var/...` instead of `/tmp/...` or
`/var/...`; those convenience paths traverse symlink components and therefore
reject by design.

The command streams and hashes the complete `0..video_frame_count-1`
trajectory, verifies prepared video and retained RGB-D
pose/timestamp/intrinsics bindings plus prepared asset presence, and selects a
portal only when exactly one RoomPlan proposal has a bounded, contiguous
normal-tracking crossing inside its rectangle. `--portal-id` records operator
intent but does not select that proposal unless it is the same uniquely crossed
portal. It never fabricates RGB-D, free
space, a threshold, a route, or a closed-state control. Its deterministic
`capture_splat.portal_route_derivation.v0.1` report is diagnostic and always
held with all authority false; it is not a
`capture_splat.portal_route_evidence.v0.1` producer package.

### 450-frame CPU and memory evidence (2026-08-23)

Performance contract: preserve the held diagnostic above on the exact prepared
capture checksum
`sha256:8b10e7b74c42f299357143e5b9d1c0cb0f831f3ee96cae58684f790b7379e83a`
without materially regressing one-process latency or resident memory. The
workload has 450 prepared frames (204 accepted RGB-D and 246 continuous-video),
3,208 contiguous trajectory records, two RoomPlan portal proposals, and no SfM
package. The host was an Apple M2 Max MacBook Pro with 12 CPU cores and 64 GB
RAM, macOS 26.6 arm64, Python 3.14.6. The capture remained on external APFS, so
these timings are same-condition engineering evidence, not a production timing
claim.

Each lane used one excluded warmup followed by five fresh-process repetitions:

```bash
PYTHONPATH=python /usr/bin/time -lp .venv/bin/python -c \
  'import sys; from pathlib import Path; from capture_splat.portal_route_derivation import derive_portal_route_evidence; r=derive_portal_route_evidence(Path(sys.argv[1]), Path(sys.argv[2])); assert r["decision"] == "hold" and r["frame_bindings"]["prepared_frame_count"] == 450 and r["trajectory"]["sample_count"] == 3208 and r["portal_analysis"]["selected_portal_id"] == "door_1"' \
  "$ROOM01_CAPTURE" "$FRESH_OUTPUT"
```

The baseline is commit `f72c4c8`; the hardened lane is the descriptor-pinned
working tree. CPU is `user + sys`. RSS is `/usr/bin/time` maximum resident set
size in bytes.

| Lane | Run | Wall s | CPU s | Max RSS bytes | Correctness |
|---|---:|---:|---:|---:|---|
| baseline | 1 | 0.34 | 0.32 | 43,614,208 | pass |
| baseline | 2 | 0.34 | 0.32 | 43,712,512 | pass |
| baseline | 3 | 0.37 | 0.33 | 43,286,528 | pass |
| baseline | 4 | 0.34 | 0.33 | 44,154,880 | pass |
| baseline | 5 | 0.34 | 0.33 | 43,778,048 | pass |
| hardened | 1 | 0.31 | 0.30 | 44,859,392 | pass |
| hardened | 2 | 0.31 | 0.31 | 45,072,384 | pass |
| hardened | 3 | 0.30 | 0.30 | 44,154,880 | pass |
| hardened | 4 | 0.31 | 0.30 | 45,400,064 | pass |
| hardened | 5 | 0.31 | 0.30 | 44,761,088 | pass |

| Lane | Wall mean / median | Wall population SD / CV | CPU mean | RSS mean |
|---|---|---|---:|---:|
| baseline | 0.346 / 0.340 s | 0.012 s / 3.47% | 0.326 s | 43,709,235 bytes |
| hardened | 0.308 / 0.310 s | 0.004 s / 1.30% | 0.302 s | 44,849,562 bytes |

The first secure prototype repeatedly rebuilt casefold listings for each asset
and had an observed 0.406 s mean wall time, but that intermediate observation
did not retain the full repeated raw wall/CPU/RSS record. The final-versus-
`f72c4c8` table also compares the complete descriptor hardening, not an isolated
cache-only change. It therefore records a 10.98% lower mean wall time, 7.36%
lower mean CPU time, and 1,140,326-byte (2.61%) higher mean RSS for the compound
candidate only; it does not attribute those differences to directory indexing.
Every run retained the exact input checksum, 450/3,208 counts, unique `door_1`
crossing, held decision, and false authority. Decision: `hold` the cache
performance attribution until identical hardened-with/without-cache repeated
A/B evidence exists. Security and correctness remain separate gates.

### Post-review publication-integrity lane (2026-08-23)

After adding exact report read-back, full input/output exit revalidation, and
aggregate traversal budgets, the same real 450-frame no-SfM workload ran once
for warmup and five fresh processes. A separate long-running reconstruction was
active, so these measurements screen for a gross regression but remain
resource-confounded timing evidence. All five measured reports were
byte-identical at
`sha256:a7405a516f367a3527f588d9090da3459f9b9c93c76ad880b0d893f2ff867cbf`
and retained 450 prepared frames, 3,208 trajectory samples, the unique
`door_1` crossing, `hold`, and all authority false.

| Run | Wall s | CPU s | Max RSS bytes | Correctness |
|---:|---:|---:|---:|---|
| 1 | 0.32 | 0.31 | 48,955,392 | pass |
| 2 | 0.32 | 0.31 | 49,692,672 | pass |
| 3 | 0.32 | 0.31 | 49,496,064 | pass |
| 4 | 0.33 | 0.32 | 49,840,128 | pass |
| 5 | 0.32 | 0.31 | 49,659,904 | pass |

Mean/median wall time was 0.322/0.320 s (0.004 s population SD,
1.24% CV), mean CPU was 0.312 s, and mean RSS was 49,528,832 bytes.
Decision: `promote` the exact-byte and bounded-resource enforcement on the
passing adversarial correctness evidence; `hold` timing and cache-efficiency
claims until an unloaded, identical-condition repeated A/B is run.

The final review then removed one redundant rejected-report root scan, enforced
aggregate counters inside enumeration so exhaustion stops at the first
over-limit entry, and made directory path identity depend on device, inode, and
mode rather than content-derived size/mtime/ctime. Exact spelling,
descriptor-relative `O_NOFOLLOW`/`O_DIRECTORY`, file identities, and output
identities remain strict. One excluded warmup and five fresh-process
repetitions on that exact final working tree produced the same report hash and
correctness tuple. The measured wall times were 0.78, 0.83, 0.86, 0.53, and
0.89 s; CPU times were 0.75, 0.77, 0.81, 0.51, and 0.72 s; and maximum RSS
values were 48,857,088, 49,168,384, 50,298,880, 49,397,760, and 49,496,064
bytes. Mean/median wall time was 0.778/0.830 s (0.129 s population SD, 16.61%
CV), mean CPU was 0.712 s, and mean RSS was 49,443,635 bytes. The high
run-to-run variance under the concurrent workload reinforces the existing
`hold`; it is correctness smoke evidence, not a performance comparison or
cache attribution.

### 450-entry SfM-bound fixture evidence (2026-08-23)

No real Room-01 SfM package was present on the dedicated test or project
storage volumes. The bounded fallback fixture therefore copied the real 450
prepared JPEGs into two independent local inodes, retained the exact prepared
capture (`sha256:8b10e7b74c42f299357143e5b9d1c0cb0f831f3ee96cae58684f790b7379e83a`),
3,208-record trajectory, RoomPlan metadata, and 450 physical image paths, and
generated a valid 450-record COLMAP `images.txt`
(`sha256:382802dbf1372fa8dfb7d6f626d32f4defd7cd65e180ad21ac38c623b0024c7a`).
The 204 depth and confidence pairs were zero-byte presence placeholders because
this diagnostic does not parse them. This is a real-shaped byte-parity fixture,
not reconstructed-camera evidence.

The two image roots each contained 151,707,921 bytes. Every run proved 450/450
registered prepared images, 303,415,842 combined bytes hashed, parity digest
`sha256:6b125962980811dfa0c1337d56d78e419b3a62be721aeb875cf72b7260bd1983`,
the unique `door_1` crossing, a held decision, and false authority. All five
hardened reports were byte-identical at
`sha256:7c4f5d02fc95ca920f719c40fbc8620cd8fbc3d3e3269cea69fe5fa5b24da160`.

On the same Apple M2 Max host and Python runtime, one warmup per lane was
excluded, then five fresh-process baseline/hardened pairs were interleaved with
warm OS file caches. The baseline was `f72c4c8`; the hardened lane included all
descriptor, enumeration, and parity bounds. CPU is user plus system time.

```bash
PYTHONPATH=python /usr/bin/time -lp .venv/bin/python -c \
  'import sys; from pathlib import Path; from capture_splat.portal_route_derivation import derive_portal_route_evidence; r=derive_portal_route_evidence(Path(sys.argv[1]), Path(sys.argv[2]), sfm_package=Path(sys.argv[3])); p=r["colmap_registration"]["registered_prepared_image_parity"]; assert r["decision"] == "hold" and r["frame_bindings"]["prepared_frame_count"] == 450 and r["trajectory"]["sample_count"] == 3208 and r["portal_analysis"]["selected_portal_id"] == "door_1" and r["colmap_registration"]["registered_image_count"] == 450 and r["colmap_registration"]["registered_prepared_image_count"] == 450 and p["count"] == 450 and not any(r["authority"].values())' \
  "$LOCAL_CAPTURE" "$FRESH_OUTPUT" "$LOCAL_SFM_PACKAGE"
```

| Lane | Run | Wall s | CPU s | Max RSS bytes | Correctness |
|---|---:|---:|---:|---:|---|
| baseline | 1 | 0.56 | 0.55 | 44,974,080 | pass |
| hardened | 1 | 0.54 | 0.53 | 46,366,720 | pass |
| baseline | 2 | 0.56 | 0.55 | 45,268,992 | pass |
| hardened | 2 | 0.55 | 0.54 | 46,071,808 | pass |
| baseline | 3 | 0.58 | 0.54 | 45,252,608 | pass |
| hardened | 3 | 0.59 | 0.56 | 46,317,568 | pass |
| baseline | 4 | 0.55 | 0.54 | 44,990,464 | pass |
| hardened | 4 | 0.56 | 0.55 | 46,530,560 | pass |
| baseline | 5 | 0.55 | 0.54 | 45,203,456 | pass |
| hardened | 5 | 0.55 | 0.53 | 46,874,624 | pass |

| Lane | Wall mean / median | Wall population SD / CV | CPU mean | RSS mean |
|---|---|---|---:|---:|
| baseline | 0.560 / 0.560 s | 0.011 s / 1.96% | 0.544 s | 45,137,920 bytes |
| hardened | 0.558 / 0.550 s | 0.017 s / 3.08% | 0.542 s | 46,432,256 bytes |

Mean wall and CPU differences were both below 0.4%, while mean RSS increased
1,294,336 bytes (2.87%). Decision: `promote` the fail-closed bounds on security
and correctness evidence, and `hold` any broad performance claim because this
was a local, warm-cache generated SfM fixture rather than a real reconstruction
package or cross-platform workload.

Running without `--evidence` is intentional. It writes
`capture_splat_portal_route_validation_report.json` with every missing
RoomPlan, portal, route, free-space, registered-RGB-D, and closed-control rail
named explicitly. The current closed-door Room-01 package must use this path;
it is not traversable evidence.

## Evidence package

The strict top-level schema is
`capture_splat.portal_route_evidence.v0.1`. Its `source_handoff` size and
SHA-256 must bind the exact supplied `capture-splat.world-studio.json` v0.3
file. All referenced paths are relative to the evidence package, must remain
inside it, and must resolve to regular, non-symlink files with the declared
size and SHA-256.

The package requires:

- `coordinate_contract`: `arkit_world`, meters, scale 1, +Y world up, and
  positive position, dimension, and plane-residual uncertainties.
- `roomplan`: the exact handoff `room_plan` asset and a checksum-bound
  `capture_splat.roomplan_arkit_registration.v0.1` receipt with a rigid
  transform into `arkit_world`, scale, uncertainty, method, and provenance. A
  raw unregistered RoomPlan asset alone is insufficient.
- `portal`: an ordered finite convex quadrilateral on a unit-normal vertical plane,
  clear width and height consistent with that polygon, and a finite threshold
  segment on its lower edge.
- `free_space`: a checksum-bound
  `capture_splat.portal_free_space_evidence.v0.1` JSON artifact. Every sample
  carries a metric position, horizontal and vertical clearance, and exact
  supporting capture-frame indices. The declared count and bounded sample
  spacing must match the artifact.
- `route_corridor`: a checksum-bound
  `capture_splat.portal_route_corridor_evidence.v0.1` JSON artifact. Its finite
  centerline must start and end on opposite portal sides and intersect inside
  the portal polygon. The validator samples every segment at the declared
  spacing; each sample must have nearby observed free-space support with enough
  horizontal and vertical clearance.
- `registered_rgbd_support`: a checksum-bound
  `capture_splat.portal_rgbd_support.v0.1` JSON inventory. Every observation
  selects an exact v0.3 capture-manifest frame whose regular RGB, depth, and
  confidence files are hash-bound by the handoff inventory. The frame must
  retain finite intrinsics, timestamp, rigid ARKit pose, and a name present in
  the handoff's exact COLMAP `images.txt`. At least one observation is required
  on side A, inside the through-opening band, and on side B. Its metric
  registration digest must match the handoff, and route free-space samples may
  cite only these selected frame indices.
- `prior_closed_state_control`: checksum-bound copies of the prior closed-state
  v0.3 handoff, reduced candidate, held reducer report, and held software-probe
  report. The reducer's top-level candidate and the probe's reduced-collider
  input must bind the same exact file; the probe must retain a closed-door
  result. All authority stays false. The control may remain held; binding it is
  not a claim that the old reduction passed.
- non-empty capture and measurement provenance, with every authority field
  false.

## Receipt boundary

A structurally valid package sets only
`outcome.producer_contract_valid=true` and
`outcome.evidence_complete_for_future_reduction_design=true`. Overall decision
remains `hold`, while `reduction_started`, `traversable`, and
`collision_candidate_promoted` remain false. World Studio must separately run
the source and reduced collider doorway probes, reset/route test, and physical
clearance validation before any downstream promotion. Raw Gaussian splats and
this receipt never become collision or Newton authority.

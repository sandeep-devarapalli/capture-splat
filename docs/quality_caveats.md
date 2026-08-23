# Quality Caveats

High-quality 3DGS needs high-quality input. Common failure modes include blur, low texture, poor overlap, dynamic objects, exposure shifts, bad COLMAP registration, and unstable trainer output.

The host tools should report these as quality blockers rather than hiding them behind a generated `.ply`.

Perspective views from one equirectangular panorama share one optical center.
Their recorded rotations are projection provenance, not recovered camera
motion or triangulation evidence. Use `sfm-360-rig` so their fixed rotations
and zero translations are preserved while motion between panorama frames is
estimated. Successful registration still does not establish metric scale or
visual quality.

SPZ compression is lossy and coordinate-system aware. A finite reverse
conversion with low sampled coordinate/color error is necessary but does not
prove that a target viewer used the intended up axis or source cameras.
`export-spz` therefore stays held until checksum-bound viewer checks pass.

## Lessons From Field Validation

- Alignment proof is not visual-quality proof.
- Longer training does not fix weak supervision by itself.
- Duplicate weighting can help targeted frames, but too much duplicate weighting can regress other views.
- Raw render canvases should be used for source/render metrics; full UI screenshots are useful evidence, but they are not clean metric inputs.
- Radius clamps are safety guardrails for viewer stability, not proof of better reconstruction.
- Frame-level tails matter. A good mean PSNR can still hide failed frames that make the splat feel soft or smeared.

## Command Decisions

`capture-splat train-vksplat-ladder` and optional `capture-splat train-gsplat-ladder` report `promote`, `hold`, or `reject` per
rung. Treat `hold` as useful evidence that is not sufficient for a quality
claim. `promote` means the configured proxies improved or stayed within
thresholds for the supplied evidence; it still does not prove metric geometry,
collision geometry, or general scene correctness.

`capture-splat qa-render-source` should be run on raw render canvases matched to
source frames. Full screenshots remain useful visual records, but they are not
clean metric inputs.

`capture-splat sanitize-ply` is a finite-output repair, not a quality upgrade.
It may turn an otherwise usable trainer output into a viewer-loadable candidate
by dropping non-finite vertex rows, but visual quality still needs render/source
QA, viewer inspection, and comparison against the source frames.

RoomPlan exports from the iPhone app are room-layout guidance. A RoomPlan USDZ
or area estimate can help the operator cover walls, openings, floors, and large
objects, but it does not replace source/render QA, COLMAP registration evidence,
finite PLY checks, or viewer inspection.


Bilateral-grid training compensates per-frame exposure/white balance during
optimization. The exported PLY colors are the splat appearance model, so
render/source QA still compares fairly, but per-frame scores can shift
versus non-bilagrid runs; compare ladders with the same recipe.

Photometric correction cannot recover evidence that preparation dropped.
Capture Splat therefore carries exposure, ISO, white balance, lens state,
tracking, timestamps, and per-frame intrinsics into the SfM package. Camera and
photometric reports expose missing or non-finite values before training. These
reports establish evidence continuity, not reconstruction quality.

White-valid masks are derived proposals. They can prevent people and unsupported
desk background from contributing features or training loss, but an incomplete
or dimension-mismatched mask set must not be silently applied. Compare masked
and unmasked runs on the same fixed cameras before claiming improvement.

Caspar timing, global registration, finite geometry, and render/source QA are
separate gates. A faster post-global bundle adjustment is useful only if it
preserves registration and stays within the same render-QA thresholds.

The background sphere and scene-transform sidecar are packaging aids: seeds
and coordinate metadata, not reconstruction improvements.

Metric input evidence does not imply meter-native trainer output. The
normalization decision must be checksum-bound to the current sparse model and
recorded by the trainer summary. Capture Splat blocks an unsupported explicit
no-normalization request instead of writing an identity transform claim.

Likewise, preserved LiDAR depth and derived normals do not imply that a trainer
used them. `metadata/training_supervision.json` binds those sidecars by checksum
and records coverage. Trainer summaries separately record available,
supported, and applied states. `required` blocks unsupported backends; `auto`
does not silently substitute sparse COLMAP point-depth loss for metric sensor
supervision.

CUDA fallback is a backend choice, not a shortcut around evidence. If gsplat runs where Vulkan is unavailable, keep the same conservative language: finite output, render/source QA decision, weak-frame count, and explicit hold/reject/promote.

The live spatial-guidance wireframe and map are also capture evidence only.
Visible or colored cells do not prove complete surface coverage, accurate
geometry, registration, measurement, collision safety, navigation, or a sharp
3DGS result. Thermal downgrades and unsupported sensors are recorded in
`metadata/spatial_guidance_report.json` rather than silently overstated.
Guidance callbacks intentionally coalesced to the configured update rate are
reported separately from real drops and processing-budget overruns; do not
interpret a large throttled-update count as lost capture evidence.

World Studio handoffs may carry a checksum-bound metric seed in
`metric_colmap_world` with meter units. That continuity proves neither surface
accuracy nor measurement readiness. Capture Splat therefore holds measurement
eligibility until a physical known-distance validation is recorded.

A simplified classified ARKit mesh is only a collision candidate. Preserved
spatial cells, floor/wall labels, finite triangles, and correct meter units are
software prerequisites; they do not prove watertight floors, retained walls,
splat registration, safe movement, or navigation authority.

Likewise, an Open3D-reduced hybrid collider that meets its triangle budget and
software ray probes is still a held candidate. Nearest-surface class transfer,
bidirectional distance checks, and component/topology comparisons do not prove
an unobserved opening is clear. The open-door Room-01 capture contains RoomPlan
and one `door_1` trajectory crossing, but its accepted RGB-D evidence is only on
one spatial side of that portal. The positive-ID repair lets native binding
complete without changing that spatial limitation. Capture Splat preserves the
held evidence; World Studio may form a hypothesis-tagged experimental proxy
with unknown space no-go, but doorway traversal, physical collision behavior,
and Newton authority remain unavailable without their independent gates.

AprilTag scale validation checks one measured target against registered camera
geometry. A passing tag report validates that checksum-bound artifact and scale
threshold only. It does not prove every surface is accurate, remove camera-pose
error, or make the result survey-grade.

# Field Validation Learnings

These notes capture the practical lessons from the first iPhone Video 3DGS validation ladder. They are intentionally conservative: a finite `.ply`, a passing viewer load, or a good alignment proof is necessary evidence, but not the same thing as high visual quality.

## Current Baseline

- Default local backend: Vulkan/VkSplat.
- Fallback Apple-local backend: OpenSplat/MPS, useful for comparison but less stable in the tested fixed-step room/video paths.
- SfM/pose baseline: COLMAP-refined image packages remain the strongest starting point for iPhone video captures.
- Output target: standard Gaussian Splatting `.ply` plus metadata summaries, not a proprietary viewer format.
- Authority stance: generated splats are visual proposals, not metric geometry, collision geometry, semantic ground truth, or planning authority.

## What Worked

- Video-style iPhone capture produced the strongest training input compared with sparse still/image-only attempts.
- A connected COLMAP model matters more than raw frame count. One validated video capture registered `99 / 125` images with about `30k` sparse points and `137k` observations, which became the strongest trainer input candidate.
- VkSplat/Vulkan stayed finite across bounded runs on Apple hardware and is suitable as the default local baseline when the same-input evidence passes.
- Controlled training ladders are useful. Short runs at `100`, `500`, or `2000` steps are smoke checks, not quality checks; useful visual-quality movement appeared only after controlled `3000 -> 7000 -> 15000 -> 30000` gates.
- A retained `30000` run improved dense proof-frame proxy metrics over `15000`, while still remaining visibly soft. This is a useful improvement, not a solved-quality claim.
- Raw render/source-canvas comparison is better than comparing full app screenshots. Full UI screenshots can confuse image matching because they include source panes, labels, and inspector UI.
- App-native proof is valuable when paired with strict JSON summaries: finite PLY check, radius/outlier check, source-frame match, reprojection support, and per-frame render/source metrics.

## What Did Not Work Yet

- Longer training alone did not solve softness. If input supervision, pose support, or frame selection is weak, longer training can produce a better-trained blur.
- Naive duplicate weighting can regress quality. A package that over-weighted weak frames `1`, `32`, `40`, and `48` stayed finite and aligned at `7000` steps, but mean dense-frame PSNR/SSIM/MAE regressed versus the prior package and the retained `30000` run.
- Alignment proof is not a quality proof. We can have `13 / 13` aligned proof frames and still have soft or smeared rendered views.
- Radius clamps are guardrails, not quality levers. They prevent giant-splat outliers from polluting the viewer, but they did not by themselves improve the actual render.
- Full-room captures with background-heavy images are difficult for object-quality splats unless the capture flow and package builder preserve foreground/object supervision.

## Quality Gates To Keep

Every serious training run should record and reject on these before claiming progress:

- strict capture/package JSON parsing with non-finite values rejected;
- COLMAP registration count, sparse point count, observation count, and weak-frame track counts;
- finite PLY validation and non-finite float count;
- Gaussian radius/outlier summary before and after any clamp;
- app/viewer proof against selected frames;
- raw render canvas versus source-frame similarity, not full-window screenshot similarity;
- per-frame PSNR, SSIM, MAE, normalized correlation, edge density, and blur/sharpness metrics;
- explicit list of tail frames and why they failed;
- clear decision: promote, hold, or reject the run.

## Capture UX Lessons

- Users need guidance about where to move the phone, not just dots on screen.
- Object Orbit requires an explicit stable subject lock before Record. Room,
  Desk, Corridor, Wall, Outdoor, Semantic Room, and Repair remain full-scene
  captures and never auto-lock a center-depth sample.
- Auto-capture should trigger on useful keyframes: overlap, parallax, blur, exposure, tracking, and coverage contribution, not a fixed timer alone.
- Haptics are useful when a frame is accepted, but they must correspond to logged accepted keyframes.
- The real camera view must stay readable. Dense telemetry belongs in compact, collapsible, or secondary panels.
- Coverage should be explained as a capture-quality signal, not as proof that the scene is reconstructed well.
- Saved captures need durable status after relaunch. Projects now distinguishes
  finalized, partial, and malformed folders and shares preserved evidence
  without pretending missing in-memory state can be recovered.

## Pipeline Lessons

- Preserve source images and generated training packages separately. Do not overwrite evidence.
- Prefer COLMAP-refined packages for training, but record when ARKit pose/depth is used as a carrier or fallback.
- Keep duplicate/weighted packages traceable. If images are duplicated for supervision, the metadata must show the source frame, role, copy index, and non-authoritative status.
- Compare backends on the exact same input package before drawing conclusions.
- VkSplat should be the default baseline for this repo, but OpenSplat/MPS remains useful as a sanity-check backend on Apple machines.
- Integrated global COLMAP is the default mapper. HLOC retrieval is the scaling
  path for larger packages; Caspar remains an explicit post-global BA
  experiment rather than the global solver.

## Carry-Forward From The vid2scene Comparison (2026-07-08)

- Frame budget dominates: production video->3DGS pipelines extract hundreds
  of frames per capture. Capture Splat now records quality-gated RGB-D
  keyframes plus indexed continuous video, and `prepare-capture` selects the
  intent budget while preserving timestamped camera evidence. The remaining
  gate is whether a specific capture provides enough sharp, connected
  viewpoints for strong registration; raw frame count alone is not progress.
- Gsplat ladder rungs must compress the whole schedule (`steps_scaler`),
  not truncate a 30000-step one; truncated rungs under-train refine/reset
  behavior and mislead comparisons.
- Bilateral-grid training absorbs iPhone auto-exposure drift; it is a
  mitigation, not a substitute for locking exposure at capture.
- Orientation alignment (`model_orientation_aligner`) plus a persisted
  scene transform sidecar removes viewer up-axis guessing.
- Alpha pruning is viewer hygiene, not reconstruction improvement: on the
  retained 20000-step room run, 34.5% of splats (999,720 -> 654,488) sat
  below alpha 12/255 and rendered as fog.
- First `capture-splat sfm` run on the retained room package (218 frames,
  COLMAP sequential, 2026-07-08): 112/218 registered (51.4%), 38,702
  points, 196,781 observations, mean track 5.08, mean reprojection 0.846,
  orientation aligned. The registration gate returned `reject` - a denser
  model than the historical 99/125 baseline, but the ratio honestly flags
  weak coverage across the full frame set.
- A/B training comparison (2026-07-08, JarvisLabs L4, gsplat v1.5.3 mcmc,
  identical recipe per package: steps_scaler 7000/30000, bilateral grid,
  random background, 1M cap): the raw ARKit-pose package versus the new
  `sfm` COLMAP-refined orientation-aligned package, same source images.
  Held-out validation (every 8th registered frame):
  ARKit-pose package: PSNR 20.62, SSIM 0.829, LPIPS 0.313.
  sfm package: PSNR 21.47, SSIM 0.854, LPIPS 0.239.
  The sfm package improved every proxy (+0.85 dB PSNR, -23% LPIPS) while
  training on fewer registered views, and its splat is gravity-aligned
  for viewers. Caveats: the two runs hold out different frame subsets
  (each package's own registration), and these are quality proxies on one
  capture, not a general claim. Decision: promote the sfm path as the
  default reconstruction route; the capture itself remains the limiting
  factor (51% registration) until continuous-video captures land.
  Artifacts: runs/video3dgs_first_device/c1_gsplat_ab_20260708.
- Exhaustive-matching follow-up (2026-07-09, same recipe and rung). GPU
  COLMAP (CUDA 4.1) exhaustive matching on the same 218 frames registered
  213/218 (97.7%, single connected model, 69,211 points, decision
  `promote`) - correcting the earlier caveat: sequential matching, not
  the capture, was the limiting factor. Training that package at the same
  rung: PSNR 20.52, SSIM 0.831, LPIPS 0.305 (color-corrected 22.90 /
  0.844 / 0.281 versus ARKit 22.92 / 0.842 / 0.290). Reading this
  honestly: the three runs hold out different frame subsets. The
  sequential package's higher proxies come from validating only on the
  112 frames it could register - a self-selected, easier subset - so they
  do not mean it reconstructs the room better. On near-matched held-out
  coverage (213 vs 218 registered frames), the exhaustive package's
  proxies are on par with ARKit poses while adding SfM-refined,
  orientation-alignable geometry and full-orbit coverage. Decision:
  prefer exhaustive matching on GPU COLMAP for room orbits (now the
  `sfm` default; CPU runs require an explicit recorded override). A
  matched-holdout, longer-rung ladder comparison remains open.

## Current Evidence Gates

The reusable QA, weak-frame, controlled-ladder, capture-guidance, and
reconstruction orchestration commands above are implemented. Remaining work is
evidence-bound:

1. Complete a controlled physical RoomPlan + 3DGS capture and verify shared
   coordinates, live-guidance throughput, thermal downgrade, and finalization.
2. Benchmark checksum-bound iPhone depth/normal evidence on a trainer with
   dedicated metric-sensor supervision before claiming a quality benefit.
3. Validate rig-constrained `sfm-360-rig` on a real moving 360 sequence.
4. Validate AprilTag scale on a measured physical target before making World
   Studio measurement eligible.
5. Validate SPZ orientation, color, cameras, and mobile/browser load before
   promoting distribution.
6. Run release-level startup, long-session thermal, and two-cycle finalization
   checks across supported LiDAR iPhones, then complete TestFlight packaging.

VGGT preview, splat-to-mesh, SOG/tiled LOD, and Caspar post-global BA remain
optional experiments. They are not prerequisites for the default public
capture-to-3DGS path.

## Physical Desk Capture (2026-07-11)

A physical Desk / Cluster capture closed the earlier video-writer crash gate
and exposed the next two operational issues:

- The finalized bundle contained 6,179 HEVC frames, 93 accepted RGB-D
  keyframes, 132 person masks, and a finite ARKit mesh. The video writer
  completed with zero reported drops.
- Capture QA returned `promote`: accepted-frame blur, parallax, overlap, and
  depth proxies cleared their configured thresholds. This is safe-to-try-SfM
  evidence, not a reconstruction-quality claim.
- Xcode reported that the ARSession delegate retained 11-12 `ARFrame` objects.
  The continuous recorder had passed ARKit-owned pixel buffers directly to the
  encoder. The recorder now copies each frame into an app-owned buffer before
  append.
- The device reached a `serious` thermal state during the roughly 226-second
  pass. Shorter connected passes and a cool starting device remain preferable;
  quality gates are not relaxed under thermal pressure.
- The Desk intent locked a nearby point at about 0.37 m and only one of 93
  keyframes retained strong support for that small extent. Desk / Cluster is
  now a full-scene recipe; strict target locking and object masks are reserved
  for Object Orbit. Record never acquires a target lock in any intent; Object
  Orbit requires a separate, explicit **Lock Subject** action.
- The first 300-frame preparation attempt exceeded FFmpeg's practical
  expression size. Chunked extraction now preserves the exact frame mapping
  while avoiding one unbounded selector.
- The corrected full-resolution package reached `ready` with 300 frames:
  93 accepted RGB-D frames plus 207 sharp continuous-video supplements. All
  393 extraction candidates matched timestamped camera metadata; camera,
  photometric, and valid-mask reports passed with no warnings.

The next evidence gate is global SfM registration on this prepared package,
followed by the 3000-step VkSplat rung and fixed-camera render/source QA.

## Corrected Desk and Object Orbit Captures (2026-07-11)

Two fresh physical captures finalized without the previous recorder crash and
preserved zero-drop full-resolution HEVC streams. Both reached a `serious`
thermal state, so shorter connected passes and a cool starting device remain
important.

- The 91-second Desk / Cluster pass saved 360 RGB-D frames, but the removable
  Auto control had been disabled and produced fixed-interval diagnostic frames.
  Capture QA correctly held on mean parallax (`0.036 m`). Quality-ranked
  temporal preparation raised the selected 120-frame mean to about `0.049 m`,
  still just below the gate. Video 3DGS Max now makes Smart quality-gated
  keyframes mandatory instead of exposing a timed fallback.
- The 167-second Object Orbit pass saved 75 accepted RGB-D frames and promoted
  at the capture gate, with mean parallax around `0.120 m`. It covered 8 of 12
  azimuth sectors but was heavily biased toward high views: only two mid-angle
  frames and no low-angle frame were recorded. Object readiness now requires
  low, middle, and high-angle support.
- The object was explicitly locked at about `0.374 m`, but its projected depth
  later moved beyond the original static depth band. Pose-adjusted depth bands
  now follow per-frame optical depth. Re-preparing the real export produced 75
  of 75 white-valid masks with no missing frames.
- Strict Object Orbit preparation now caps itself at accepted RGB-D evidence
  and never fills a masked package with unmasked continuous-video frames. The
  repaired real package contains 75 prepared frames, zero video supplements,
  complete camera/depth/confidence/object-mask sidecars, and a `ready` decision.

These are capture and preparation results. They do not establish COLMAP
registration or 3DGS appearance quality. The next reconstruction gate remains
global SfM, then the 3000-step VkSplat rung and fixed-camera render/source QA.

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
- Object mode should lock the subject before capture. Room mode should lock the room/perimeter intent before capture.
- Auto-capture should trigger on useful keyframes: overlap, parallax, blur, exposure, tracking, and coverage contribution, not a fixed timer alone.
- Haptics are useful when a frame is accepted, but they must correspond to logged accepted keyframes.
- The real camera view must stay readable. Dense telemetry belongs in compact, collapsible, or secondary panels.
- Coverage should be explained as a capture-quality signal, not as proof that the scene is reconstructed well.

## Pipeline Lessons

- Preserve source images and generated training packages separately. Do not overwrite evidence.
- Prefer COLMAP-refined packages for training, but record when ARKit pose/depth is used as a carrier or fallback.
- Keep duplicate/weighted packages traceable. If images are duplicated for supervision, the metadata must show the source frame, role, copy index, and non-authoritative status.
- Compare backends on the exact same input package before drawing conclusions.
- VkSplat should be the default baseline for this repo, but OpenSplat/MPS remains useful as a sanity-check backend on Apple machines.
- COLMAP 4.1+ should be evaluated for faster/refined reconstruction paths, but only after the current package gates are reproducible.

## Carry-Forward From The vid2scene Comparison (2026-07-08)

- Frame budget dominates: production video->3DGS pipelines extract hundreds
  of frames per capture; our ~24 accepted keyframes are the first
  bottleneck. `extract-frames` closes this on the host; app-side continuous
  video capture is the remaining unlock.
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

## Immediate Carry-Forward Work

1. Add a public sample-capture QA ladder to this repo with small fixtures first, then real captures.
2. Implement raw-canvas render/source comparison as a first-class CLI report.
3. Add training-ladder commands for `3000`, `7000`, `15000`, and `30000` with finite PLY, radius, and per-frame quality gates.
4. Add weak-frame diagnostics without automatically increasing duplicate weight.
5. Investigate why non-target frames can regress after weak-frame weighting.
6. Improve the iPhone app guidance around object/room lock, keyframe acceptance, haptics, and low-intrusion UI.
7. Publish example reports that separate startup success, alignment success, and actual visual quality.

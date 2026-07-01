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

## Immediate Carry-Forward Work

1. Add a public sample-capture QA ladder to this repo with small fixtures first, then real captures.
2. Implement raw-canvas render/source comparison as a first-class CLI report.
3. Add training-ladder commands for `3000`, `7000`, `15000`, and `30000` with finite PLY, radius, and per-frame quality gates.
4. Add weak-frame diagnostics without automatically increasing duplicate weight.
5. Investigate why non-target frames can regress after weak-frame weighting.
6. Improve the iPhone app guidance around object/room lock, keyframe acceptance, haptics, and low-intrusion UI.
7. Publish example reports that separate startup success, alignment success, and actual visual quality.

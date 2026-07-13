# Roadmap

## Current Carry-Forward Focus

- Preserve VkSplat/Vulkan as the default baseline while keeping OpenSplat/MPS as a comparison backend.
- Turn field validation learnings into reproducible CLI reports: finite PLY, radius/outlier checks, raw-canvas source matching, and per-frame quality summaries.
- Do not promote longer training runs unless the same input package passes alignment and per-frame quality gates.
- Improve iPhone capture guidance so accepted frames are driven by overlap, parallax, blur, exposure, tracking, and coverage contribution.
- Compare observable outputs from Capture Splat, SplatKing, KIRI Engine, and similar apps to improve iPhone-level capture guidance without claiming proprietary internals.

## Capture Splat App

- v0.1: Video 3DGS capture, export folder, basic blur/exposure/motion feedback.
- v0.2: Guided capture with haptics, coverage prompts, loop guidance, and less intrusive UI.
- v0.3: Video 3DGS capture intents, explicit Object Orbit lock, live LiDAR/trajectory guidance, shared-session RoomPlan semantics, and conservative completeness reports.
- v0.4: TestFlight-ready distribution and project library.
- v1.0: Reliable capture scoring, export presets, and on-device preview checks.

## 3DGS Pipeline

- v0.1: Capture ingest, COLMAP package generation, VkSplat training, finite `.ply` validation.
- v0.2: Linux, Windows, and cloud NVIDIA setup maturity.
- v0.3: Quality gates for sharpness, overlap, registration, render/source correspondence, and weak-frame filtering.
- v0.4: Integrated global COLMAP, per-frame camera evidence, canonical masks, depth/pose priors, loop-closure diagnostics, and controlled training ladders.
- v1.0: Benchmarked sample captures, viewer-ready release packages, and adapters for common 3DGS tools.

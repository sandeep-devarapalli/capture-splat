# Contributing to Capture Splat

Capture Splat turns iPhone RGB-D and camera evidence into validated reconstruction
packages and conservative 3D Gaussian Splat proposals. Contributions are welcome across
iPhone capture, host preparation, SfM, training backends, QA, interoperability, and
device validation.

## Start With An Issue

Use the issue form that matches the work:

- **Implementation:** one bounded code or documentation outcome.
- **Validation evidence:** reproducible results for a capability or promotion gate.
- **Upstream evaluation:** a license and capability review of an external project.
- **Roadmap proposal:** a new outcome or change to milestone scope.

Keep pull requests focused. Separate implementation from generated evidence, dependency
adoption, and broad roadmap changes when they can be reviewed independently.

## Evidence And Authority

- Source RGB, depth, poses, intrinsics, masks, mesh, and telemetry are capture evidence.
- COLMAP models, metric seeds, meshes, semantics, and trained splats remain proposals
  until their declared gates pass.
- Finite output, successful viewer load, alignment, render QA, metric scale, collision,
  navigation, semantics, and physics are separate claims.
- Passive Capture Splat recording does not establish mass, inertia, friction,
  restitution, stiffness, force, torque, or physics authority.
- Use `promote`, `hold`, or `reject` according to the measured gate. Do not describe a
  finite PLY or plausible render as high quality without the required evidence.

The cross-repository roadmap is the
[World Studio World Compiler Blueprint](https://github.com/sandeep-devarapalli/world-studio/tree/main/docs/blueprints/world-compiler-v0.1).

## Reproducible Validation

Record the commit, clean/dirty state, OS, device, GPU, tool versions, input provenance,
exact commands, configured thresholds, measured results, warnings, and checksums for
external artifacts. Keep generated captures, frames, models, splats, renders, simulator
outputs, and logs outside Git.

Run the checks relevant to the change. The full public validation set is:

```bash
python -m compileall python scripts tests
PYTHONPATH=python .venv/bin/python -m pytest
xcodebuild -project apps/ios/CaptureSplat/CaptureSplat.xcodeproj \
  -scheme CaptureSplat \
  -sdk iphoneos \
  -configuration Debug \
  CODE_SIGNING_ALLOWED=NO \
  build
git diff --check
```

Also run the release checklist's public-path/private-string scan. Report checks that were
not run and why. Physical-device behavior requires physical-device evidence; a successful
unsigned build is not device acceptance.

## Upstream Boundaries

Do not vendor VkSplat, gsplat, COLMAP, or research repositories. Before adopting an external
tool, record its canonical source, exact revision, license, intended role, platform and
distribution impact, and explicit acceptance gates. Keep optional trainers and converters
outside the shipped source tree.

## Pull Requests

A pull request should:

1. Link its issue and Capture Splat milestone.
2. State the bounded outcome and non-goals.
3. Identify changed schemas, files, backends, and compatibility.
4. Report objective acceptance gates and reproducible evidence.
5. Separate supported claims from proposal-only or evidence-blocked claims.
6. Avoid unrelated refactors and generated artifacts.

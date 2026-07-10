# Spirulae Splat Learnings

Capture Splat reviewed `harry7557558/spirulae-splat` at commit
`543b1748329ccdafc136b4534c3c661d72ae16d0` as a clean-room research
reference. The reference is GPL-3.0. Capture Splat does not vendor it, import
it, or copy its implementation. The notes below describe independently
implemented data contracts and later experiments only.

## Transferable Mathematics

For a seed point with distances `d_j` to its four nearest neighbors, a useful
isotropic initialization is:

```text
s_0 = alpha * sqrt(mean_j(d_j^2))
```

The reviewed reference uses `alpha = 0.5` by default. Capture Splat does not
change trainer initialization in this release; the idea is retained for a
controlled seed-initialization experiment.

A common image objective combines L1 and SSIM:

```text
L_rgb = (1 - lambda) * mean(abs(I_render - I_source))
        + lambda * (1 - SSIM(I_render, I_source))
```

Full-resolution multiscale evaluation may help high-resolution iPhone frames,
but it must be tested on fixed cameras. A lower training loss alone is not a
quality claim.

Robust densification should keep optimization and Gaussian allocation
separate. One possible allocation weight is Tukey's biweight:

```text
w(r) = (1 - (r / c)^2)^2, when abs(r) < c
w(r) = 0,                 otherwise
```

Here `r` is a luminance residual and `c` is a per-image residual quantile.
Combining this with an edge response can allocate detail near persistent
residual edges without letting people, reflections, or clipped highlights pull
new Gaussians into transient regions. The base RGB loss remains unchanged.

Depth and normal supervision should be confidence weighted. For metric LiDAR
depth `D`, rendered depth `D_hat`, and validity confidence `q`:

```text
L_depth = sum(q * rho(D_hat - D)) / max(sum(q), epsilon)
```

Derived normals are proposals from valid depth neighborhoods. They must not
override COLMAP-refined cameras or be treated as collision authority.

## Implemented Evidence Continuity

- iPhone frame telemetry now includes achieved white-balance gains, lens
  position, camera adjustment states, pixel-buffer color attachments, and
  projection/calibration availability.
- Preparation carries timestamps, tracking, exposure, ISO, white balance,
  lens state, and per-frame intrinsics into each prepared frame.
- Strict camera and photometric reports expose intrinsics variation, principal
  point movement, missing/non-finite metadata, and exposure span.
- Canonical masks use white for valid pixels. Room masks remove people when
  evidence exists; desk/object masks require object support and remove people.
- Capture Splat prepared packages use per-frame ARKit pinhole priors. Imported
  camera models and real distortion coefficients remain explicit rather than
  being replaced by invented values.
- Integrated COLMAP `global_mapper` is the default. View-graph calibration runs
  on a copied database only when complete prepared ARKit intrinsics are absent.
- gsplat capability probing recognizes modern
  `post_processing=bilateral_grid|ppisp` and legacy bilateral-grid flags.
- Every completed SfM package gets one deterministic fixed-camera evaluation
  set for backend comparisons.

## Deferred Experiments

After a fresh Desk capture establishes a baseline, evaluate these one at a
time against the same cameras:

1. Full-resolution multiscale L1/SSIM.
2. Confidence-weighted metric LiDAR depth and derived-normal supervision.
3. Robust residual-and-edge-guided densification.
4. Long-axis splitting for oversized anisotropic Gaussians.
5. A controlled 1M versus 2M Gaussian cap comparison.

Direct-distortion 3DGUT training, full PPISP, quantized optimizers, and
second-order optimization remain later experiments. They require finite
standard PLY output, camera/orientation round trips, and fixed-camera render QA
before adoption.

## COLMAP And Caspar Boundary

Caspar is an experimental GPU bundle-adjustment backend, not the global SfM
solver. It is disabled in COLMAP unless built with `-DCASPAR_ENABLED=ON`, only
supports `PINHOLE` and `SIMPLE_RADIAL`, does not support pose priors, and is not
selectable inside `global_mapper`.

Capture Splat therefore defaults to integrated `global_mapper` with no
post-global BA. `--post-ba-backend caspar` is an explicit experiment that
blocks when the build or camera models are unsupported. Faster BA, finite
geometry, and improved render QA remain separate evidence.

## Sources

- [Spirulae Splat](https://github.com/harry7557558/spirulae-splat)
- [COLMAP global mapper CLI](https://colmap.github.io/cli.html)
- [COLMAP bundle-adjustment guidance](https://colmap.github.io/faq.html#speedup-bundle-adjustment)

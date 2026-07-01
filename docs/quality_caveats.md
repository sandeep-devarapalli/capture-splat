# Quality Caveats

High-quality 3DGS needs high-quality input. Common failure modes include blur, low texture, poor overlap, dynamic objects, exposure shifts, bad COLMAP registration, and unstable trainer output.

The host tools should report these as quality blockers rather than hiding them behind a generated `.ply`.

## Lessons From Field Validation

- Alignment proof is not visual-quality proof.
- Longer training does not fix weak supervision by itself.
- Duplicate weighting can help targeted frames, but too much duplicate weighting can regress other views.
- Raw render canvases should be used for source/render metrics; full UI screenshots are useful evidence, but they are not clean metric inputs.
- Radius clamps are safety guardrails for viewer stability, not proof of better reconstruction.
- Frame-level tails matter. A good mean PSNR can still hide failed frames that make the splat feel soft or smeared.

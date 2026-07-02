# App Comparison Protocol

Use this protocol when comparing Capture Splat with third-party iPhone 3DGS apps
such as SplatKing or KIRI Engine. The goal is to learn from observable evidence:
capture UX, exported artifacts, metadata, and rendered views. Do not infer or
claim proprietary internal implementation details.

## Capture Setup

Use the same room or object, lighting, and scan intent for every app. Save:

- capture UI screenshots or a screen recording;
- exported 3D artifacts such as `.ply`, `.splat`, `.ksplat`, `.spz`, meshes, or archives;
- any app settings, metadata, or processing notes the app exposes;
- 5-10 rendered views from matching angles when possible;
- source iPhone video/images if the app makes them available.

For room scans, include paths that test overlap, loop closure, blank walls,
tables, thin edges, shelves, reflective objects, and small foreground details.

## CLI Summary

Place each app output in a separate folder, then run:

```bash
capture-splat compare-app-output \
  --capture-splat runs/comparison/capture_splat \
  --splatking runs/comparison/splatking \
  --kiri runs/comparison/kiri \
  --out runs/comparison/report
```

The command writes `capture_splat_app_output_comparison.json`. It reports file
counts, image counts, metadata files, observable 3D artifacts, finite PLY stats
when parseable, and whether existing render/source QA summaries are present.

If another app exports RGB frames with `transforms.json`, convert that export
into a Capture Splat package before running the usual host checks:

```bash
capture-splat import-transforms \
  --input runs/comparison/roomly_export \
  --out runs/comparison/roomly_capture
```

The importer preserves referenced depth files such as `.exr` or `.npy` when
available. It does not infer proprietary capture logic from the app.

## Interpretation

Use the report to decide what to inspect next:

- If another app has sharper thin objects, check whether its capture path forced
  more overlap, side angles, or slower movement.
- If Capture Splat has fewer accepted frames, inspect `capture_quality` and
  `metadata/keyframe_report.json` before changing trainer settings.
- If outputs differ but raw render/source QA is missing, export raw canvases
  first; full UI screenshots are not metric inputs.
- If an app output is an opaque format, keep the comparison at artifact presence,
  render views, and user-visible capture behavior.

This comparison is a quality proxy. It is not metric geometry, collision
geometry, or proof that one app's internal reconstruction method is known.

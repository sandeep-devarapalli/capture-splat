# Viewer Compatibility

Capture Splat emits standard Gaussian Splatting `.ply` output from VkSplat. Compatible viewers may include SuperSplat, PlayCanvas-based viewers, Spark-compatible viewers, and other tools that accept trained 3DGS PLY files.

Viewer compatibility does not imply metric, collision, planning, or survey authority.

## Raw Render Canvas Export

`capture-splat qa-render-source` expects raw render canvases, not full viewer or
app screenshots. The exported image should contain only the rendered scene for a
known camera/view, with no UI panes, labels, buttons, rulers, debug overlays, or
source-image thumbnails.

Use the same width and height as the matched source frame. If a viewer exports a
different size, crop or render at the source-frame resolution before running QA;
the command rejects dimension mismatches instead of resizing silently.

Recommended layout:

```text
runs/scan/colmap_package/images/000001.jpg
runs/scan/render_canvases/step_0003000/000001.png
```

When filenames do not match, provide an explicit pairs file:

```json
{
  "pairs": [
    {
      "frame_id": "000001",
      "source": "000001.jpg",
      "render": "view_000001.png"
    }
  ]
}
```

Then run:

```bash
capture-splat qa-render-source \
  --source-dir runs/scan/colmap_package/images \
  --render-dir runs/scan/render_canvases/step_0003000 \
  --pairs-json runs/scan/render_canvases/step_0003000_pairs.json \
  --out runs/scan/render_qa/step_0003000
```

The report is a quality proxy. A passing canvas comparison can support a ladder
promotion, but it does not prove metric geometry or scene correctness.

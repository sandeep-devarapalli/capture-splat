# Pipeline

```text
iPhone Video 3DGS capture
  -> capture.json + images + metadata
  -> strict host validation
  -> Nerfstudio-style transforms.json
  -> COLMAP text package
  -> COLMAP refinement or triangulation as needed
  -> VkSplat/Vulkan training
  -> standard splat.ply
```

COLMAP registration and trainer health are quality gates. A successful file export is not the same as a high-quality reconstruction.

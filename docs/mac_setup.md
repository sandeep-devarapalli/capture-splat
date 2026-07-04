# macOS Setup

Use macOS when you want to build the iPhone app locally.

```bash
scripts/setup_macos.sh
scripts/setup_vksplat.sh external/vksplat
capture-splat doctor --vksplat-root external/vksplat
```

Set your Apple development team in Xcode before installing the app on a device.


## Optional Viewer Runtime Candidate

`scripts/setup_external_3dgs_candidates.sh external` can clone 3DGS.cpp for macOS/Vulkan viewer-runtime evaluation. It uses the Vulkan SDK through MoltenVK and is not a Capture Splat training backend yet because upstream lists training as TODO.

The optional gsplat backend is CUDA-based and should be treated as a Linux/cloud NVIDIA training path, not a Mac training path.

# Cloud GPU Setup

Cloud users can process exported iPhone capture folders without owning a Mac GPU workstation. Prefer a Linux NVIDIA instance with Vulkan support for the default VkSplat path. If the instance exposes CUDA but Vulkan is blocked, use the optional gsplat CUDA path instead.

```bash
docker build -f docker/Dockerfile.linux-nvidia -t capture-splat:nvidia .
docker run --gpus all -it --rm -v /path/to/capture:/capture -v $PWD/runs:/runs capture-splat:nvidia
```

Inside the container, run `scripts/setup_vksplat.sh external/vksplat` and then the README pipeline commands.

For CUDA-only cloud images:

```bash
scripts/setup_gsplat.sh external/gsplat
capture-splat doctor --gsplat-root external/gsplat
capture-splat train-gsplat-ladder \
  --package runs/scan/colmap_package \
  --out runs/scan/gsplat_ladder \
  --gsplat-root external/gsplat
```

This is a fallback training path, not a quality claim. Run finite PLY checks, render/source QA, and weak-frame diagnostics before promoting a rung.

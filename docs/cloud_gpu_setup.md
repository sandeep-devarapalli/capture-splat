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

Before compiling gsplat, compare `nvcc --version` with
`python -c 'import torch; print(torch.version.cuda)'`. They must use the same
CUDA major/minor version. Ubuntu images also need the GLM headers, typically
from `apt-get install libglm-dev`. Images whose base Python leaks a different
Torch into PEP 517 build isolation can use the already-verified active
environment explicitly:

```bash
python -m pip install --upgrade pip ninja jaxtyping nvtx
BUILD_3DGUT=0 BUILD_2DGS=0 BUILD_3DGS=1 \
  CAPTURE_SPLAT_GSPLAT_NO_BUILD_ISOLATION=1 \
  scripts/setup_gsplat.sh external/gsplat
```

Do this only after the active Torch build matches `nvcc`; disabling build
isolation does not repair a CUDA mismatch. The build flags compile only the
3DGS kernels used by this backend and avoid spending cloud time on unused 2DGS
and 3DGUT extensions.

This is a fallback training path, not a quality claim. Run finite PLY checks, render/source QA, and weak-frame diagnostics before promoting a rung.

# Third-Party Notices

Capture Splat does not vendor training backends in this repository. Setup scripts download or use external tools in the user's environment.

| Component | Role | License | Source |
| --- | --- | --- | --- |
| VkSplat | Vulkan 3DGS training backend | Apache-2.0 | https://github.com/harry7557558/vksplat |
| gsplat | Optional CUDA 3DGS training backend | Apache-2.0 | https://github.com/nerfstudio-project/gsplat |
| 3DGS.cpp | Optional Vulkan viewer/runtime candidate | LGPL | https://github.com/shg8/3DGS.cpp |
| AndrewBoessen/3DGS | Experimental CUDA 13 C++ trainer candidate | See upstream | https://github.com/AndrewBoessen/3DGS |
| COLMAP | SfM and camera-pose refinement | BSD | https://github.com/colmap/colmap |
| Nerfstudio | Optional dataset/tooling compatibility reference | Apache-2.0 | https://github.com/nerfstudio-project/nerfstudio |
| transparent-background / InSPyReNet | Optional object-matting backend | MIT | https://github.com/plemeri/transparent-background |
| NumPy | Numeric processing | BSD-style | https://numpy.org |
| Pillow | Image metadata and fixture support | HPND | https://python-pillow.org |

Generated captures, datasets, trained PLY files, and third-party clones should stay outside git unless a future release explicitly adds small fixtures with clear licenses.

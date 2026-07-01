# Linux Setup

Install Python, COLMAP, Vulkan drivers/tools, and a working GPU driver. Then:

```bash
scripts/setup_linux.sh
scripts/setup_vksplat.sh external/vksplat
capture-splat doctor --vksplat-root external/vksplat
```

Linux users process exported iPhone capture folders; they do not build the iOS app unless they also have access to macOS/Xcode.

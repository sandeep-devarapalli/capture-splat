# Cloud GPU Setup

Cloud users can process exported iPhone capture folders without owning a Mac GPU workstation. Use a Linux NVIDIA instance with Vulkan support.

```bash
docker build -f docker/Dockerfile.linux-nvidia -t capture-splat:nvidia .
docker run --gpus all -it --rm -v /path/to/capture:/capture -v $PWD/runs:/runs capture-splat:nvidia
```

Inside the container, run `scripts/setup_vksplat.sh external/vksplat` and then the README pipeline commands.

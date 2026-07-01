# Linux/NVIDIA Docker

This image is intended for users who already have an exported iPhone capture folder and want to run the host pipeline on a Linux cloud GPU.

```bash
docker build -f docker/Dockerfile.linux-nvidia -t capture-splat:nvidia .
docker run --gpus all -it --rm -v /path/to/capture:/capture -v $PWD/runs:/runs capture-splat:nvidia
```

Inside the container, install or mount VkSplat, then run `capture-splat doctor` and the pipeline commands from the README.

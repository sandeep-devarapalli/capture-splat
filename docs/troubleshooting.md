# Troubleshooting

- `capture.json missing`: export the full capture folder, not only images.
- `COLMAP text file missing`: run `capture-splat colmap-export` before training.
- `simple_trainer.py not found`: pass the correct `--vksplat-root`.
- `vksplat import error`: run `scripts/setup_vksplat.sh` in the active Python environment.
- Poor visual result: inspect blur, overlap, COLMAP registration, and source/render correspondence before increasing training steps.

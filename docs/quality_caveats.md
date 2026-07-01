# Quality Caveats

High-quality 3DGS needs high-quality input. Common failure modes include blur, low texture, poor overlap, dynamic objects, exposure shifts, bad COLMAP registration, and unstable trainer output.

The host tools should report these as quality blockers rather than hiding them behind a generated `.ply`.

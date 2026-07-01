# Capture Splat iOS App

Capture Splat is a native iPhone capture app for recording Video 3DGS input. It writes `capture_splat.v0.1` export folders for the host pipeline in this repository.

## Build

Open `CaptureSplat.xcodeproj` in Xcode, select a physical iPhone, set your development team, and run the `CaptureSplat` scheme.

The simulator cannot validate camera, LiDAR, motion, or real capture timing.

## Export

The app writes session folders under the app Documents directory:

```text
capture.json
rgb/
depth/
confidence/
imu.csv
gps.csv
metadata/
```

Use Video 3DGS mode for trainer input. The app records sharp RGB-D keyframes with camera metadata and quality reports; the host pipeline handles COLMAP and VkSplat training.

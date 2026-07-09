# Capture Splat iOS App

Capture Splat is a native iPhone capture app for recording Video 3DGS input. It writes `capture_splat.v0.2` export folders for the host pipeline in this repository.

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
room_plan/
```

Video 3DGS Max is the default capture path. The app records dense, sharp RGB-D
keyframes with camera metadata and quality reports; the host pipeline handles
COLMAP and VkSplat training. Use the capture intent menu to tag the pass as
Desk / Cluster, Room Walkthrough, Object Orbit, Corridor / Passage, Wall /
Facade, Outdoor Object, RoomPlan + 3DGS, or Detail Repair without changing the
underlying quality gates.

Supported LiDAR iPhones can open Room Plan review during the same video capture
workflow and export `room_plan/room.usdz`, `room_plan/room_plan_report.json`,
and `room_plan/room_semantics.json`. This is capture guidance and semantic
proposal evidence for layout coverage, not a 3DGS quality claim.

Saved frames include `capture_quality` metadata. The host pipeline uses accepted
keyframes for ingest and COLMAP export, so rejected candidates remain diagnostic
evidence rather than trainer input.

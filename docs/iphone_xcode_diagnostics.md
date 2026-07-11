# iPhone Xcode Diagnostics

Use a physical LiDAR iPhone for runtime diagnosis. A simulator build can catch
Swift compile errors, but it cannot validate ARKit, LiDAR, camera timing, video
recording, or capture finalization.

## First-Time Xcode Setup

1. Open `apps/ios/CaptureSplat/CaptureSplat.xcodeproj`.
2. Select the `CaptureSplat` scheme and the connected iPhone.
3. Add an **All Objective-C Exceptions** breakpoint in the Breakpoint navigator.
4. Open the Debug navigator and console before starting a capture.
5. In the console, filter for `video-recorder` or `arkit-session` to isolate
   Capture Splat lifecycle messages.

When the app stops, inspect the first app-owned frame on the crashed thread.
The last console line is often Apple framework cleanup noise rather than the
cause.

## Evidence To Save

For a reproducible report, keep:

- the exception or fatal-error text;
- the crashed thread and first Capture Splat stack frame;
- the preceding `video-recorder` and `arkit-session` messages;
- device model, iOS version, Xcode version, and capture intent;
- whether the failure happened at launch, Record, Stop, finalization, or Share;
- the partial capture folder when one was created.

Do not delete a partial folder before inspecting
`metadata/finalization_report.json` and `metadata/session_events.jsonl`.

## Known Log Classes

Treat these as app-owned and actionable:

- Objective-C exceptions with a Capture Splat frame in the crashed thread;
- `video-recorder` start, append, or finish failures;
- `arkit-session` failure or interruption messages;
- invalid SF Symbol warnings emitted by Capture Splat;
- repeated session-enable warnings caused by app session configuration.
- retained-`ARFrame` warnings, especially when the count rises across frames.

The video writer adaptor must be created before its input starts writing.
Creating it after `AVAssetWriter.startWriting()` raises an Objective-C
exception instead of a recoverable Swift error.

Continuous video must append app-owned pixel buffers. Passing ARKit's
`capturedImage` directly to an asynchronous encoder can retain enough camera
buffers to stop frame delivery even when the writer itself reports no drops.

The following messages can occur inside Apple frameworks and are not alone
evidence of a Capture Splat bug:

- managed-preferences access warnings for CoreMotion;
- RealityKit material-resolution messages;
- Metal Performance Shaders or VideoLightSpill prewarm messages;
- FigCaptureSourceRemote or Fig capture-service messages;
- brief SLAM initialization messages before ARKit tracking becomes normal.

Escalate them only when they consistently coincide with an app-visible failure,
a Capture Splat error event, lost output, or a reproducible crash.

## Physical-Device Stability Smoke

After every capture-lifecycle change:

1. Launch the app and wait for normal tracking.
2. Record 10-15 seconds with at least three accepted keyframes.
3. Stop and wait for finalization to complete.
4. Confirm the app remains responsive and Projects opens the capture.
5. Confirm `capture.json`, `video/capture.mov`,
   `metadata/frame_index.jsonl`, `metadata/finalization_report.json`, and
   `metadata/session_events.jsonl` exist.
6. Repeat Record and Stop once without relaunching the app.
7. Confirm no Objective-C exception, video-writer failure, duplicate-session
   warning, or retained-ARFrame warning appeared.

A successful build or launch is not this stability proof. The gate closes only
after both record/finalize cycles succeed on the physical iPhone.

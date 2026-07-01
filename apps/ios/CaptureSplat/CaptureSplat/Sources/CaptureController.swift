import ARKit
import CoreImage
import CoreLocation
import CoreMotion
import Foundation
import UIKit

struct ScanGuidancePoint: Identifiable {
    let id: Int
    let normalizedX: Double
    let normalizedY: Double
    let depthMeters: Double
}

struct ObjectExtentOverlay {
    let normalizedX: Double
    let normalizedY: Double
    let normalizedWidth: Double
    let normalizedHeight: Double
}

private struct KeyframeDecision {
    let shouldCapture: Bool
    let reason: String
    let score: Double
    let sectorIndex: Int
    let blurScore: Double
    let exposureMean: Double
    let exposureDelta: Double
    let parallaxMeters: Double
    let pathLengthMeters: Double
    let distanceFromStartMeters: Double?
    let loopClosureCandidate: Bool
    let sectorDeltaFromLastAccepted: Int?
    let colmapOverlapScore: Double
    let roomFragmentRisk: Bool

    init(
        shouldCapture: Bool,
        reason: String,
        score: Double,
        sectorIndex: Int,
        frameQuality: FrameQualityEstimate? = nil,
        sectorDeltaFromLastAccepted: Int? = nil,
        colmapOverlapScore: Double = 1.0,
        roomFragmentRisk: Bool = false
    ) {
        self.shouldCapture = shouldCapture
        self.reason = reason
        self.score = score
        self.sectorIndex = sectorIndex
        self.blurScore = frameQuality?.blurScore ?? 0
        self.exposureMean = frameQuality?.exposureMean ?? 0
        self.exposureDelta = frameQuality?.exposureDelta ?? 0
        self.parallaxMeters = frameQuality?.parallaxMeters ?? 0
        self.pathLengthMeters = frameQuality?.pathLengthMeters ?? 0
        self.distanceFromStartMeters = frameQuality?.distanceFromStartMeters
        self.loopClosureCandidate = frameQuality?.loopClosureCandidate ?? false
        self.sectorDeltaFromLastAccepted = sectorDeltaFromLastAccepted
        self.colmapOverlapScore = colmapOverlapScore
        self.roomFragmentRisk = roomFragmentRisk
    }
}

private struct FrameQualityEstimate {
    let blurScore: Double
    let exposureMean: Double
    let exposureDelta: Double
    let parallaxMeters: Double
    let pathLengthMeters: Double
    let distanceFromStartMeters: Double?
    let loopClosureCandidate: Bool
}

private struct ObjectExtentProposal {
    let overlay: ObjectExtentOverlay
    let centerDepthMeters: Double
    let depthMinMeters: Double
    let depthMaxMeters: Double
    let foregroundSampleCount: Int
    let validSampleCount: Int
    let depthImageWidth: Int
    let depthImageHeight: Int
    let approximateWidthMeters: Double
    let approximateHeightMeters: Double
    let approximateRadiusMeters: Double
    let timestamp: TimeInterval
}

final class CaptureController: NSObject, ObservableObject {
    @Published var isRecording = false
    @Published var statusText = "Ready"
    @Published var rgbFrames = 0
    @Published var depthFrames = 0
    @Published var imuRows = 0
    @Published var gpsRows = 0
    @Published var currentSessionDirectory: URL?
    @Published var isRGBEnabled = true
    @Published var isDepthEnabled = true
    @Published var isConfidenceEnabled = true
    @Published var isIMUEnabled = true
    @Published var isGPSEnabled = true
    @Published var rgbRate = 0.0
    @Published var depthRate = 0.0
    @Published var imuRate = 0.0
    @Published var gpsRate = 0.0
    @Published var droppedFrames = 0
    @Published var trackingStatus = "waiting"
    @Published var validDepthRatio = 0.0
    @Published var storageFreeText = "--"
    @Published var thermalStateText = "--"
    @Published var batteryText = "--"
    @Published var guidanceText = "Start with a slow orbit and keep overlap between views."
    @Published var guidancePoints: [ScanGuidancePoint] = []
    @Published var coverageSectors = Array(repeating: 0.0, count: 12)
    @Published var coverageHintText = "Coverage 0/12"
    @Published var readinessState = "Not ready"
    @Published var nextAction = "Start a slow orbit"
    @Published var missingSectorCount = 12
    @Published var backgroundWarning = "Depth dots show samples, not scan quality."
    @Published var guidanceArrowSystemImage = "arrow.up.circle"
    @Published var isSmartAutoCaptureEnabled = true
    @Published var acceptedKeyframes = 0
    @Published var skippedKeyframes = 0
    @Published var lastKeyframeDecision = "Waiting"
    @Published var keyframeScore = 0.0
    @Published var captureQualityText = "Quality waiting"
    @Published var roomQualityText = "Walk the room perimeter"
    @Published var roomLoopText = "Loop open"
    @Published var roomOverlapText = "COLMAP chain pending"
    @Published var roomColmapHintText = "Keep adjacent views overlapping"
    @Published var colmapCoachStatus = "Coach waiting"
    @Published var colmapCoachAction = "Lock Room to start"
    @Published var colmapCoachDetail = "Keep textured walls, corners, and edges in view."
    @Published var colmapCoachScore = 0.0
    @Published var colmapFeatureText = "Features --"
    @Published var captureBlockerStatus = "Clear"
    @Published var captureBlockerDetail = "Accepted keyframes are enabled."
    @Published var lastAcceptedViewHint = "No accepted view yet."
    @Published var captureProfileText = "RGB-D keyframes"
    @Published var captureProfileDetail = "Quality-gated RGB, depth, pose, IMU, and GNSS bundle."
    @Published var hapticAcceptedCount = 0
    @Published var currentCoverageSector = 0
    @Published var targetCoverageSector = 0
    @Published var coverageNavigationText = "Target sector will appear while scanning."
    @Published var scanTargetMode = "object"
    @Published var isObjectTargetLocked = false
    @Published var isRoomTargetLocked = false
    @Published var targetLockStatus = "Lock object before recording"
    @Published var targetLockDetail = "Center the object, then tap Lock Object."
    @Published var targetLockDistanceText = "--"
    @Published var isObjectMaskEnabled = true
    @Published var isObjectExtentLocked = false
    @Published var objectExtentStatus = "Lock extent after object"
    @Published var objectExtentDetail = "Use LiDAR depth near the reticle to propose foreground bounds."
    @Published var objectExtentOverlay: ObjectExtentOverlay?
    @Published var objectExtentSizeText = "--"

    private let motion = CMMotionManager()
    private let location = CLLocationManager()
    private let ciContext = CIContext()
    private let acceptedHaptic = UIImpactFeedbackGenerator(style: .light)
    private let writeQueue = DispatchQueue(label: "capture-splat.writer")
    private var frames: [CapturedFrame] = []
    private var session: ARSession?
    private var lastFrameTimestamp: TimeInterval = 0
    private var activeIntrinsics: CameraIntrinsics?
    private var activeResolution = Resolution(w: 0, h: 0)
    private var firstFrameTimestamp: TimeInterval?
    private let minimumKeyframeInterval: TimeInterval = 0.5
    private let videoMinimumKeyframeInterval: TimeInterval = 0.25
    private let maxCapturedFrames = 120
    private let maxVideoCapturedFrames = 240
    private var lastScheduledFrameTimestamp: TimeInterval = -.infinity
    private var lastCandidateFrameTimestamp: TimeInterval = -.infinity
    private var lastAcceptedSectorIndex: Int?
    private var lastAcceptedCameraPosition: SIMD3<Float>?
    private var scheduledFrameCount = 0
    private var isWritingFrame = false
    private var rgbRateSamples: [TimeInterval] = []
    private var depthRateSamples: [TimeInterval] = []
    private var imuRateSamples: [TimeInterval] = []
    private var gpsRateSamples: [TimeInterval] = []
    private var healthTimer: Timer?
    private var coverageSectorCounts = Array(repeating: 0, count: 12)
    private let coverageObservationTarget = 5
    private let maxCoverageHistorySamples = 720
    private var coverageHistory: [[String: Any]] = []
    private var coverageHistoryWasTruncated = false
    private let maxKeyframeEventSamples = 720
    private var keyframeEvents: [[String: Any]] = []
    private var keyframeEventsWereTruncated = false
    private var keyframeSkipReasonCounts: [String: Int] = [:]
    private let minBlurScore = 0.006
    private let minExposureMean = 0.08
    private let maxExposureMean = 0.92
    private let maxExposureJump = 0.28
    private let minObjectParallaxMeters = 0.08
    private let minRoomParallaxMeters = 0.12
    private let minVideoParallaxMeters = 0.05
    private let maxRoomConnectedStepMeters = 0.85
    private let maxRoomConnectedSectorJump = 2
    private let minRoomOverlapScore = 0.45
    private let minRoomFeatureCount = 80
    private var lastAcceptedExposureMean: Double?
    private var latestFeaturePointCount = 0
    private var roomStartPosition: SIMD3<Float>?
    private var roomLastAcceptedPosition: SIMD3<Float>?
    private var roomPathLengthMeters = 0.0
    private var roomMaxDistanceFromStartMeters = 0.0
    private var roomLoopClosed = false
    private var roomOverlapChainLength = 0
    private var roomLongestOverlapChainLength = 0
    private var roomFragmentRiskCount = 0
    private var roomReconnectHold = false
    private let maxRoomQualityEventSamples = 720
    private var roomQualityEvents: [[String: Any]] = []
    private var roomQualityEventsWereTruncated = false
    private let maxObjectMatteFrameSamples = 720
    private var objectMatteFrameRecords: [[String: Any]] = []
    private var objectMatteFrameRecordsWereTruncated = false
    private var objectMatteSupportCounts: [String: Int] = [:]
    private var latestCameraTransform: simd_float4x4?
    private var latestTargetCandidateWorldPosition: SIMD3<Float>?
    private var latestTargetCandidateDistance: Float?
    private var lockedObjectWorldPosition: SIMD3<Float>?
    private var lockedRoomWorldTransform: simd_float4x4?
    private var latestObjectExtentProposal: ObjectExtentProposal?
    private var lockedObjectExtentProposal: ObjectExtentProposal?

    deinit {
        healthTimer?.invalidate()
    }

    func prepareSensors() {
        UIDevice.current.isBatteryMonitoringEnabled = true
        location.delegate = self
        location.desiredAccuracy = kCLLocationAccuracyBest
        location.requestWhenInUseAuthorization()
        location.startUpdatingLocation()

        if motion.isDeviceMotionAvailable {
            motion.deviceMotionUpdateInterval = 0.01
        }
        refreshHealth()
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.refreshHealth()
        }
    }

    func attach(session: ARSession) {
        self.session = session
        session.delegate = self
    }

    func setScanTargetMode(_ mode: String) {
        scanTargetMode = mode
        updateCaptureProfileText()
        refreshTargetLockStatus()
        updateGuidance()
    }

    func lockObjectTarget() {
        guard let position = latestTargetCandidateWorldPosition,
              let distance = latestTargetCandidateDistance,
              distance.isFinite,
              distance > 0 else {
            isObjectTargetLocked = false
            targetLockStatus = "Object lock needs LiDAR depth"
            targetLockDetail = "Center the object and wait for valid depth."
            return
        }
        lockedObjectWorldPosition = position
        isObjectTargetLocked = true
        isObjectExtentLocked = false
        lockedObjectExtentProposal = nil
        targetLockStatus = "Object locked"
        targetLockDetail = "Orbit around the locked object center."
        targetLockDistanceText = String(format: "%.2f m", distance)
        refreshObjectExtentStatus()
        updateGuidance()
    }

    func lockObjectExtent() {
        guard isObjectTargetLocked else {
            objectExtentStatus = "Lock object first"
            objectExtentDetail = "Center the subject and tap Lock Object before confirming extent."
            return
        }
        guard isObjectMaskEnabled else {
            objectExtentStatus = "Mask disabled"
            objectExtentDetail = "Enable Mask before locking an object extent."
            return
        }
        guard let proposal = latestObjectExtentProposal else {
            objectExtentStatus = "Extent needs LiDAR depth"
            objectExtentDetail = "Keep the object centered until a foreground depth box appears."
            return
        }
        lockedObjectExtentProposal = proposal
        isObjectExtentLocked = true
        objectExtentOverlay = proposal.overlay
        objectExtentStatus = "Object extent locked"
        objectExtentDetail = "Foreground bounds saved as proposal metadata."
        objectExtentSizeText = String(
            format: "%.2fm x %.2fm",
            proposal.approximateWidthMeters,
            proposal.approximateHeightMeters
        )
        updateGuidance()
    }

    func lockRoomTarget() {
        guard let transform = latestCameraTransform else {
            isRoomTargetLocked = false
            targetLockStatus = "Room lock needs ARKit pose"
            targetLockDetail = "Move slowly until tracking is normal."
            return
        }
        lockedRoomWorldTransform = transform
        roomStartPosition = cameraPosition(transform)
        isRoomTargetLocked = true
        targetLockStatus = "Room locked"
        targetLockDetail = "Walk the perimeter and scan walls/corners."
        targetLockDistanceText = "world origin"
        updateRoomQualityText()
        updateGuidance()
    }

    func clearTargetLock() {
        isObjectTargetLocked = false
        isRoomTargetLocked = false
        lockedObjectWorldPosition = nil
        lockedRoomWorldTransform = nil
        roomStartPosition = nil
        roomLastAcceptedPosition = nil
        roomPathLengthMeters = 0
        roomMaxDistanceFromStartMeters = 0
        roomLoopClosed = false
        roomOverlapChainLength = 0
        roomLongestOverlapChainLength = 0
        roomFragmentRiskCount = 0
        latestObjectExtentProposal = nil
        lockedObjectExtentProposal = nil
        isObjectExtentLocked = false
        objectExtentOverlay = nil
        objectExtentSizeText = "--"
        targetLockDistanceText = "--"
        refreshTargetLockStatus()
        refreshObjectExtentStatus()
        updateGuidance()
    }

    func startRecording() {
        guard !isRecording else { return }
        guard canStartForCurrentTargetMode else {
            statusText = targetLockStatus
            return
        }
        do {
            let directory = try makeSessionDirectory()
            try makeExportFolders(in: directory)
            currentSessionDirectory = directory
            try writeCSVHeader("imu.csv", columns: [
                "timestamp", "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
                "quat_w", "quat_x", "quat_y", "quat_z",
            ])
            try writeCSVHeader("gps.csv", columns: [
                "timestamp", "latitude", "longitude", "altitude", "horizontal_accuracy", "vertical_accuracy",
                "course", "speed",
            ])
            frames.removeAll()
            firstFrameTimestamp = nil
            lastFrameTimestamp = 0
            lastScheduledFrameTimestamp = -.infinity
            lastCandidateFrameTimestamp = -.infinity
            lastAcceptedSectorIndex = nil
            lastAcceptedCameraPosition = nil
            scheduledFrameCount = 0
            isWritingFrame = false
            rgbFrames = 0
            depthFrames = 0
            imuRows = 0
            gpsRows = 0
            rgbRate = 0
            depthRate = 0
            imuRate = 0
            gpsRate = 0
            droppedFrames = 0
            validDepthRatio = 0
            guidancePoints.removeAll()
            coverageSectorCounts = Array(repeating: 0, count: coverageSectorCounts.count)
            coverageSectors = Array(repeating: 0.0, count: coverageSectors.count)
            coverageHintText = "Coverage 0/\(coverageSectors.count)"
            readinessState = "Not ready"
            nextAction = "Move slowly around the subject"
            missingSectorCount = coverageSectors.count
            backgroundWarning = "Keep the subject centered; avoid sweeping large background planes."
            guidanceArrowSystemImage = "arrow.up.circle"
            acceptedKeyframes = 0
            skippedKeyframes = 0
            lastKeyframeDecision = "Waiting"
            keyframeScore = 0
            captureQualityText = "Quality waiting"
            roomQualityText = scanTargetMode == "room" ? "Walk the room perimeter" : "Room quality applies to room mode"
            roomLoopText = scanTargetMode == "room" ? "Loop open" : "Loop not used"
            roomOverlapText = scanTargetMode == "room" ? "COLMAP chain 0 kept" : "COLMAP chain not used"
            roomColmapHintText = scanTargetMode == "room" ? "Keep adjacent views overlapping" : "Room overlap applies to room mode"
            colmapCoachStatus = scanTargetMode == "room" ? "Coach ready" : "Room coach idle"
            colmapCoachAction = scanTargetMode == "room" ? "Build an overlap chain" : "Switch to Room for COLMAP coach"
            colmapCoachDetail = "Keep textured walls, corners, and edges in view."
            colmapCoachScore = 0
            colmapFeatureText = "Features --"
            captureBlockerStatus = "Clear"
            captureBlockerDetail = "Accepted keyframes are enabled."
            lastAcceptedViewHint = "No accepted view yet."
            updateCaptureProfileText()
            hapticAcceptedCount = 0
            currentCoverageSector = 0
            targetCoverageSector = 0
            coverageNavigationText = "Target sector will appear while scanning."
            coverageHistory.removeAll()
            coverageHistoryWasTruncated = false
            keyframeEvents.removeAll()
            keyframeEventsWereTruncated = false
            keyframeSkipReasonCounts.removeAll()
            lastAcceptedExposureMean = nil
            roomStartPosition = lockedRoomWorldTransform.map { cameraPosition($0) }
            roomLastAcceptedPosition = nil
            roomPathLengthMeters = 0
            roomMaxDistanceFromStartMeters = 0
            roomLoopClosed = false
            roomOverlapChainLength = 0
            roomLongestOverlapChainLength = 0
            roomFragmentRiskCount = 0
            roomReconnectHold = false
            latestFeaturePointCount = 0
            roomQualityEvents.removeAll()
            roomQualityEventsWereTruncated = false
            objectMatteFrameRecords.removeAll()
            objectMatteFrameRecordsWereTruncated = false
            objectMatteSupportCounts.removeAll()
            rgbRateSamples.removeAll()
            depthRateSamples.removeAll()
            imuRateSamples.removeAll()
            gpsRateSamples.removeAll()
            isRecording = true
            statusText = "Recording"
            guidanceText = scanTargetMode == "video_3dgs"
                ? "Record a slow video-style orbit. Haptics mark accepted sharp frames."
                : "Move slowly around the subject. Favor side steps over panning."
            acceptedHaptic.prepare()
            if isIMUEnabled {
                startMotion()
            }
            if isGPSEnabled {
                location.startUpdatingLocation()
            }
        } catch {
            statusText = "Start failed: \(error.localizedDescription)"
        }
    }

    func stopRecording() {
        guard isRecording else { return }
        isRecording = false
        motion.stopDeviceMotionUpdates()
        writeMetadata()
        do {
            let directory = try writeCaptureManifest()
            statusText = "Stopped and finalized \(directory.lastPathComponent)"
        } catch {
            statusText = "Stopped. Finalize failed: \(error.localizedDescription)"
        }
    }

    func finalizeSession() {
        do {
            let directory = try writeCaptureManifest()
            statusText = "Finalized \(directory.lastPathComponent)"
        } catch {
            statusText = "Finalize failed: \(error.localizedDescription)"
        }
    }

    @discardableResult
    private func writeCaptureManifest() throws -> URL {
        guard let directory = currentSessionDirectory,
              let intrinsics = activeIntrinsics ?? frames.last?.intrinsics,
              !frames.isEmpty else {
            throw CocoaError(.fileNoSuchFile)
        }
        let manifest = CaptureManifest(
            device: DeviceInfo(
                model: UIDevice.current.model,
                osVersion: UIDevice.current.systemVersion,
                appVersion: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0",
                buildNumber: Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
            ),
            captureMode: captureModeLabel(),
            depthMode: "sceneDepth",
            rgb: StreamReport(
                format: "jpeg",
                requestedFPS: 30,
                requestedResolution: Resolution(w: 1920, h: 1080),
                achievedFPS: estimatedFPS(),
                achievedResolution: activeResolution,
                units: nil
            ),
            depth: StreamReport(
                format: nil,
                requestedFPS: 30,
                requestedResolution: Resolution(w: 256, h: 192),
                achievedFPS: estimatedFPS(),
                achievedResolution: Resolution(w: intrinsics.w, h: intrinsics.h),
                units: "meters"
            ),
            intrinsics: intrinsics,
            frames: frames,
            authority: Authority()
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(manifest)
        try data.write(to: directory.appendingPathComponent("capture.json"), options: .atomic)
        return directory
    }

    private func makeSessionDirectory() throws -> URL {
        let root = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withDashSeparatorInDate, .withColonSeparatorInTime]
        let stamp = formatter.string(from: Date()).replacingOccurrences(of: ":", with: "")
        let directory = root.appendingPathComponent("capture_splat_\(stamp)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func makeExportFolders(in directory: URL) throws {
        for folder in ["rgb", "depth", "confidence", "pointcloud_preview", "metadata"] {
            try FileManager.default.createDirectory(
                at: directory.appendingPathComponent(folder, isDirectory: true),
                withIntermediateDirectories: true
            )
        }
    }

    private func writeCSVHeader(_ name: String, columns: [String]) throws {
        guard let directory = currentSessionDirectory else { return }
        try (columns.joined(separator: ",") + "\n").write(
            to: directory.appendingPathComponent(name),
            atomically: true,
            encoding: .utf8
        )
    }

    private func appendCSV(_ name: String, values: [String]) {
        guard let directory = currentSessionDirectory else { return }
        writeQueue.async {
            guard let handle = try? FileHandle(forWritingTo: directory.appendingPathComponent(name)) else { return }
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            if let data = (values.joined(separator: ",") + "\n").data(using: .utf8) {
                try? handle.write(contentsOf: data)
            }
        }
    }

    private func startMotion() {
        guard motion.isDeviceMotionAvailable else { return }
        motion.startDeviceMotionUpdates(to: .main) { [weak self] deviceMotion, _ in
            guard let self, self.isRecording, let deviceMotion else { return }
            let q = deviceMotion.attitude.quaternion
            self.appendCSV("imu.csv", values: [
                String(format: "%.6f", deviceMotion.timestamp),
                String(format: "%.9f", deviceMotion.userAcceleration.x),
                String(format: "%.9f", deviceMotion.userAcceleration.y),
                String(format: "%.9f", deviceMotion.userAcceleration.z),
                String(format: "%.9f", deviceMotion.rotationRate.x),
                String(format: "%.9f", deviceMotion.rotationRate.y),
                String(format: "%.9f", deviceMotion.rotationRate.z),
                String(format: "%.9f", q.w),
                String(format: "%.9f", q.x),
                String(format: "%.9f", q.y),
                String(format: "%.9f", q.z),
            ])
            self.imuRows += 1
            self.imuRate = self.recordRateSample(&self.imuRateSamples, at: deviceMotion.timestamp)
        }
    }

    private func writeMetadata() {
        guard let directory = currentSessionDirectory else { return }
        let metadata = directory.appendingPathComponent("metadata", isDirectory: true)
        writeJSON([
            "rgb_frames": rgbFrames,
            "depth_frames": depthFrames,
            "imu_rows": imuRows,
            "gps_rows": gpsRows,
            "dropped_frames": droppedFrames,
            "rgb_rate_hz": rgbRate,
            "depth_rate_hz": depthRate,
            "imu_rate_hz": imuRate,
            "gps_rate_hz": gpsRate,
        ], to: metadata.appendingPathComponent("session_report.json"))
        writeJSON(["capture_clock": "ARFrame.timestamp and CoreMotion/CoreLocation timestamps"], to: metadata.appendingPathComponent("sync_report.json"))
        writeJSON([
            "scene_depth_supported": ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth),
            "tracking_state": trackingStatus,
            "valid_depth_ratio": validDepthRatio,
            "storage_free": storageFreeText,
            "thermal_state": thermalStateText,
            "battery": batteryText,
            "readiness_state": readinessState,
            "next_action": nextAction,
            "missing_sector_count": missingSectorCount,
            "background_warning": backgroundWarning,
        ], to: metadata.appendingPathComponent("sensor_health.json"))
        writeJSON(coverageReport(), to: metadata.appendingPathComponent("coverage_report.json"))
        writeJSON(keyframeReport(), to: metadata.appendingPathComponent("keyframe_report.json"))
        writeJSON(roomCaptureQualityReport(), to: metadata.appendingPathComponent("room_capture_quality_report.json"))
        writeJSON(captureProfileReport(), to: metadata.appendingPathComponent("capture_profile_report.json"))
        writeJSON(targetLockReport(), to: metadata.appendingPathComponent("target_lock_report.json"))
        writeJSON(objectExtentReport(), to: metadata.appendingPathComponent("object_extent_report.json"))
        writeJSON(objectMatteReport(), to: metadata.appendingPathComponent("object_matte_report.json"))
    }

    private func writeJSON(_ object: [String: Any], to url: URL) {
        if JSONSerialization.isValidJSONObject(object),
           let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url, options: .atomic)
        }
    }

    private func estimatedFPS() -> Double {
        guard let firstFrameTimestamp, lastFrameTimestamp > firstFrameTimestamp, rgbFrames > 1 else { return 0 }
        return Double(rgbFrames - 1) / (lastFrameTimestamp - firstFrameTimestamp)
    }

    private var activeMinimumKeyframeInterval: TimeInterval {
        scanTargetMode == "video_3dgs" ? videoMinimumKeyframeInterval : minimumKeyframeInterval
    }

    private var activeMaxCapturedFrames: Int {
        scanTargetMode == "video_3dgs" ? maxVideoCapturedFrames : maxCapturedFrames
    }

    private func captureModeLabel() -> String {
        switch scanTargetMode {
        case "room":
            return "Rear LiDAR Room Reconstruction"
        case "video_3dgs":
            return "Video to 3DGS RGB-D Keyframes"
        default:
            return "Rear LiDAR Object Reconstruction"
        }
    }

    private func updateCaptureProfileText() {
        switch scanTargetMode {
        case "room":
            captureProfileText = "Room COLMAP keyframes"
            captureProfileDetail = "Strict overlap, parallax, blur, and reconnect guidance for room 3DGS input."
        case "video_3dgs":
            captureProfileText = "Video to 3DGS"
            captureProfileDetail = "Denser sharp RGB-D keyframes with ARKit poses for later COLMAP/OpenSplat gates."
        default:
            captureProfileText = "Object RGB-D keyframes"
            captureProfileDetail = "Object-locked RGB, LiDAR depth, pose, and foreground proposal metadata."
        }
    }

    private func refreshHealth() {
        storageFreeText = availableStorageText()
        thermalStateText = thermalStateLabel(ProcessInfo.processInfo.thermalState)
        let battery = UIDevice.current.batteryLevel
        batteryText = battery < 0 ? "--" : "\(Int((battery * 100).rounded()))%"
    }

    private func availableStorageText() -> String {
        guard let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first,
              let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
              let bytes = values.volumeAvailableCapacityForImportantUsage else {
            return "--"
        }
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useGB, .useMB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: Int64(bytes))
    }

    private func thermalStateLabel(_ state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal:
            return "nominal"
        case .fair:
            return "fair"
        case .serious:
            return "serious"
        case .critical:
            return "critical"
        @unknown default:
            return "unknown"
        }
    }

    private func recordRateSample(_ samples: inout [TimeInterval], at timestamp: TimeInterval) -> Double {
        samples.append(timestamp)
        let cutoff = timestamp - 5
        samples.removeAll { $0 < cutoff }
        guard let first = samples.first, let last = samples.last, last > first else { return 0 }
        return Double(samples.count - 1) / (last - first)
    }

    private func updateGuidance() {
        let covered = coverageSectors.filter { $0 >= 1 }.count
        let partial = partialSectorCount()
        let missing = missingSectorCountValue()
        let mostCovered = coverageSectorCounts.max() ?? 0
        let concentrationRatio = rgbFrames > 0 ? Double(mostCovered) / Double(max(rgbFrames, 1)) : 0
        missingSectorCount = missing
        refreshTargetLockStatus()
        refreshObjectExtentStatus()
        updateRoomQualityText()
        updateCoverageNavigationText()
        if !isRecording {
            readinessState = "Not ready"
            if scanTargetMode == "video_3dgs" {
                nextAction = "Start Video 3DGS capture"
            } else if scanTargetMode == "object", isObjectTargetLocked, isObjectMaskEnabled, !isObjectExtentLocked {
                nextAction = "Lock extent for cleaner object focus"
            } else {
                nextAction = canStartForCurrentTargetMode ? "Start a slow orbit" : targetLockStatus
            }
            backgroundWarning = "Depth dots show samples, not scan quality."
            guidanceArrowSystemImage = scanTargetMode == "video_3dgs" ? "video.circle" : "arrow.up.circle"
            guidanceText = scanTargetMode == "video_3dgs"
                ? "Use Video 3DGS for denser sharp frames; keep motion slow and continuous."
                : (canStartForCurrentTargetMode ? "Start with a slow orbit and keep overlap between views." : targetLockDetail)
        } else if trackingStatus != "normal" {
            readinessState = "Hold"
            nextAction = "Pause until tracking is normal"
            backgroundWarning = "Tracking is limited; do not rely on this pass."
            guidanceArrowSystemImage = "hand.raised.circle"
            guidanceText = "Pause and move slowly until tracking returns to normal."
        } else if validDepthRatio > 0 && validDepthRatio < 0.35 {
            readinessState = "Hold"
            nextAction = "Move closer to textured depth"
            backgroundWarning = "Low LiDAR coverage; avoid dark, glassy, or far surfaces."
            guidanceArrowSystemImage = "arrow.down.forward.circle"
            guidanceText = "Aim at nearer textured surfaces; LiDAR depth coverage is low."
        } else if droppedFrames > 0 && rgbRate < 1.5 {
            readinessState = "Hold"
            nextAction = "Slow down"
            backgroundWarning = "Capture is skipping frames; move more slowly."
            guidanceArrowSystemImage = "tortoise.circle"
            guidanceText = "Slow down. The device is dropping or skipping capture frames."
        } else if scanTargetMode == "room", roomReconnectHold || lastKeyframeDecision.contains("room_overlap_gap") || lastKeyframeDecision.contains("room_reconnect_hold") {
            readinessState = "Hold"
            nextAction = "Reconnect with the last view"
            backgroundWarning = "COLMAP needs adjacent overlapping frames, not disconnected jumps."
            guidanceArrowSystemImage = "arrow.uturn.backward.circle"
            guidanceText = "Step back toward the last accepted view, then move in smaller side steps."
        } else if lastKeyframeDecision.contains("reconnected_to_last_view") {
            readinessState = "Hold"
            nextAction = "Now side-step 20-40 cm"
            backgroundWarning = "You are back near the last accepted view; add a small translation next."
            guidanceArrowSystemImage = "arrow.left.and.right.circle"
            guidanceText = "Side-step slowly while keeping the same wall or object in view."
        } else if scanTargetMode == "room", lastKeyframeDecision.contains("needs_translation_not_pan") {
            readinessState = "Hold"
            nextAction = "Side-step instead of panning"
            backgroundWarning = "Room training needs translation and overlap, not only rotation."
            guidanceArrowSystemImage = "arrow.left.and.right.circle"
            guidanceText = "Move sideways along the perimeter before the next accepted keyframe."
        } else if lastKeyframeDecision.contains("low_blur_score") {
            readinessState = "Hold"
            nextAction = "Slow down and hold steady"
            backgroundWarning = "The last candidate had weak detail or motion blur."
            guidanceArrowSystemImage = "camera.metering.center.weighted"
            guidanceText = "Hold steadier on textured surfaces before moving again."
        } else if lastKeyframeDecision.contains("exposure") {
            readinessState = "Hold"
            nextAction = "Stabilize exposure"
            backgroundWarning = "Avoid fast swings across bright windows or dark corners."
            guidanceArrowSystemImage = "sun.max.trianglebadge.exclamationmark"
            guidanceText = "Keep exposure stable before accepting another keyframe."
        } else if scanTargetMode == "video_3dgs", rgbFrames < 24 {
            readinessState = rgbFrames < 8 ? "Not ready" : "Almost"
            nextAction = "Continue slow video orbit"
            backgroundWarning = "Accepted haptics are the frames that matter; avoid fast pans."
            guidanceArrowSystemImage = "video.circle"
            guidanceText = "Keep the subject or room edge in view and move smoothly for more overlap."
        } else if covered < 4 {
            readinessState = "Not ready"
            nextAction = missingDirectionHint()
            backgroundWarning = "Dots mean depth samples only. Keep orbiting until more sectors turn green."
            guidanceArrowSystemImage = missingDirectionArrow()
            guidanceText = "\(nextAction). \(missing) angles still missing."
        } else if rgbFrames < 8 {
            readinessState = "Not ready"
            nextAction = "Begin a wider side-step arc"
            backgroundWarning = "Avoid standing still and panning; use side steps."
            guidanceArrowSystemImage = "arrow.left.and.right.circle"
            guidanceText = "Begin a wide arc with side steps; avoid just panning."
        } else if partial > 0 || covered < 8 {
            readinessState = "Almost"
            nextAction = missingDirectionHint()
            backgroundWarning = concentrationRatio > 0.35 ? "One angle is dominating; move to a new side." : "Add parallax before finalizing."
            if scanTargetMode == "object", isObjectMaskEnabled, !isObjectExtentLocked {
                backgroundWarning = "Object extent is not locked; background may dominate the cloud."
            }
            guidanceArrowSystemImage = missingDirectionArrow()
            guidanceText = "\(nextAction). Add a higher or lower angle for parallax."
        } else if rgbFrames < 18 {
            readinessState = "Almost"
            nextAction = "Add high and low angles"
            backgroundWarning = "Coverage is improving; do not stop on one height."
            guidanceArrowSystemImage = "arrow.up.and.down.circle"
            guidanceText = "Add a higher or lower angle so the scene has more parallax."
        } else {
            readinessState = covered >= 10 ? "Ready" : "Good"
            nextAction = covered >= 10 ? "Finalize or close the loop" : "Close the loop near the start"
            backgroundWarning = covered >= 10 ? "Ready is guidance only, not reconstruction quality." : "One more side pass may reduce fuzzy shells."
            guidanceArrowSystemImage = covered >= 10 ? "checkmark.circle" : "arrow.triangle.2.circlepath.circle"
            guidanceText = "Good coverage start. Close the loop near where you began."
        }
        updateColmapCoachText()
    }

    private func updateLiveGuidance(from frame: ARFrame, depthMap: CVPixelBuffer) {
        let points = sampleGuidancePoints(from: depthMap)
        guidancePoints = points
        currentCoverageSector = coverageSectorIndex(for: frame.camera.transform)
        targetCoverageSector = targetMissingSectorIndex(from: currentCoverageSector)
        updateCoverageNavigationText()
        guard isRecording, trackingStatus == "normal", !points.isEmpty else {
            updateCoverageHint()
            return
        }
        updateCoverageHint()
    }

    private var canStartForCurrentTargetMode: Bool {
        switch scanTargetMode {
        case "object":
            return isObjectTargetLocked
        case "room":
            return isRoomTargetLocked
        case "video_3dgs":
            return true
        default:
            return true
        }
    }

    private func refreshTargetLockStatus() {
        switch scanTargetMode {
        case "object":
            if isObjectTargetLocked {
                targetLockStatus = "Object locked"
                targetLockDetail = "Orbit around the locked object center."
            } else {
                targetLockStatus = "Lock object before recording"
                targetLockDetail = latestTargetCandidateDistance == nil
                    ? "Center the object and wait for LiDAR depth."
                    : "Center the object, then tap Lock Object."
            }
        case "room":
            if isRoomTargetLocked {
                targetLockStatus = "Room locked"
                targetLockDetail = "Walk the perimeter and scan walls/corners."
            } else {
                targetLockStatus = "Lock room before recording"
                targetLockDetail = "Face the room center, then tap Lock Room."
            }
        case "video_3dgs":
            targetLockStatus = "Video 3DGS mode"
            targetLockDetail = "Move slowly like recording video; haptics mark sharp accepted frames."
        default:
            targetLockStatus = "Target lock optional"
            targetLockDetail = "Outdoor/diagnostic modes do not require target lock."
        }
    }

    private func updateTargetCandidate(from frame: ARFrame, depthMap: CVPixelBuffer) {
        latestCameraTransform = frame.camera.transform
        guard let centerDepth = centerDepthMeters(from: depthMap), centerDepth > 0 else {
            latestTargetCandidateWorldPosition = nil
            latestTargetCandidateDistance = nil
            latestObjectExtentProposal = nil
            if !isObjectExtentLocked {
                objectExtentOverlay = nil
            }
            targetLockDistanceText = isObjectTargetLocked ? targetLockDistanceText : "--"
            refreshObjectExtentStatus()
            return
        }
        let cameraPosition = cameraPosition(frame.camera.transform)
        let forward = cameraForward(frame.camera.transform)
        latestTargetCandidateWorldPosition = cameraPosition + forward * centerDepth
        latestTargetCandidateDistance = centerDepth
        if !isObjectTargetLocked, scanTargetMode == "object" {
            targetLockDistanceText = String(format: "%.2f m", centerDepth)
        }
        latestObjectExtentProposal = makeObjectExtentProposal(from: frame, depthMap: depthMap, centerDepth: centerDepth)
        if !isObjectExtentLocked, isObjectMaskEnabled, scanTargetMode == "object" {
            objectExtentOverlay = latestObjectExtentProposal?.overlay
        }
        refreshObjectExtentStatus()
    }

    private func centerDepthMeters(from pixelBuffer: CVPixelBuffer) -> Float? {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        guard width > 0,
              height > 0,
              let base = CVPixelBufferGetBaseAddress(pixelBuffer)?.assumingMemoryBound(to: Float32.self) else {
            return nil
        }
        let centerX = width / 2
        let centerY = height / 2
        let radius = 2
        var values: [Float] = []
        for y in max(centerY - radius, 0)...min(centerY + radius, height - 1) {
            for x in max(centerX - radius, 0)...min(centerX + radius, width - 1) {
                let value = base[y * width + x]
                if value.isFinite, value > 0 {
                    values.append(value)
                }
            }
        }
        guard !values.isEmpty else { return nil }
        values.sort()
        return values[values.count / 2]
    }

    private func recordAcceptedCoverage(from frame: ARFrame, sectorIndex: Int) {
        let index = min(max(sectorIndex, 0), coverageSectorCounts.count - 1)
        let previousCount = coverageSectorCounts[index]
        coverageSectorCounts[index] = min(previousCount + 1, coverageObservationTarget)
        coverageSectors = coverageSectorCounts.map { Double($0) / Double(coverageObservationTarget) }
        if coverageSectorCounts[index] != previousCount {
            appendCoverageHistorySample(timestamp: frame.timestamp, sectorIndex: index, guidancePointCount: guidancePoints.count)
        }
        updateCoverageHint()
    }

    private func updateCoverageHint() {
        let covered = coveredSectorCount()
        let partial = partialSectorCount()
        let missing = missingSectorCountValue()
        missingSectorCount = missing
        coverageHintText = "Coverage \(covered)/\(coverageSectors.count)"
        if partial > 0 && covered < coverageSectors.count {
            coverageHintText += " + \(partial) partial"
        }
        coverageHintText += " | \(readinessState)"
    }

    private func missingDirectionHint() -> String {
        let target = targetMissingSectorIndex(from: currentCoverageSector)
        guard coverageSectors.indices.contains(target), coverageSectors[target] < 1 else {
            return "Close the loop near the start"
        }
        return relativeMoveHint(from: currentCoverageSector, to: target)
    }

    private func missingDirectionArrow() -> String {
        let target = targetMissingSectorIndex(from: currentCoverageSector)
        return relativeArrow(from: currentCoverageSector, to: target)
    }

    private func targetMissingSectorIndex(from current: Int) -> Int {
        let missing = coverageSectors.enumerated().filter { $0.element < 1 }.map(\.offset)
        guard !missing.isEmpty else { return current }
        return missing.min { left, right in
            circularDistance(from: current, to: left) < circularDistance(from: current, to: right)
        } ?? current
    }

    private func circularDistance(from current: Int, to target: Int) -> Int {
        let count = max(coverageSectors.count, 1)
        let forward = (target - current + count) % count
        let backward = (current - target + count) % count
        return min(forward, backward)
    }

    private func signedSectorDelta(from current: Int, to target: Int) -> Int {
        let count = max(coverageSectors.count, 1)
        let forward = (target - current + count) % count
        let backward = (current - target + count) % count
        return forward <= backward ? forward : -backward
    }

    private func relativeMoveHint(from current: Int, to target: Int) -> String {
        let delta = signedSectorDelta(from: current, to: target)
        if delta == 0 {
            return "Take a wider step for parallax"
        }
        if abs(delta) >= 5 {
            return "Walk around to the opposite side"
        }
        return delta > 0 ? "Orbit left toward the missing sector" : "Orbit right toward the missing sector"
    }

    private func relativeArrow(from current: Int, to target: Int) -> String {
        let delta = signedSectorDelta(from: current, to: target)
        if delta == 0 {
            return "arrow.left.and.right.circle"
        }
        if abs(delta) >= 5 {
            return "arrow.turn.up.left.circle"
        }
        return delta > 0 ? "arrow.left.circle" : "arrow.right.circle"
    }

    private func updateCoverageNavigationText() {
        let current = min(max(currentCoverageSector, 0), max(coverageSectors.count - 1, 0))
        let target = targetMissingSectorIndex(from: current)
        currentCoverageSector = current
        targetCoverageSector = target
        guard coverageSectors.indices.contains(target), coverageSectors[target] < 1 else {
            coverageNavigationText = "All coarse sectors have saved keyframes."
            return
        }
        coverageNavigationText = "Current sector \(current + 1)/\(coverageSectors.count) -> target \(target + 1)/\(coverageSectors.count)"
    }

    private func updateColmapCoachText() {
        colmapFeatureText = latestFeaturePointCount > 0 ? "Features \(latestFeaturePointCount)" : "Features --"
        guard scanTargetMode == "room" else {
            colmapCoachStatus = "Room coach idle"
            colmapCoachAction = "Switch to Room for COLMAP coach"
            colmapCoachDetail = "Object mode uses object lock and extent guidance."
            colmapCoachScore = 0
            return
        }
        guard isRoomTargetLocked else {
            colmapCoachStatus = "Lock room first"
            colmapCoachAction = "Tap Lock Room"
            colmapCoachDetail = "Stand at the start point with corners and edges visible."
            colmapCoachScore = 0
            return
        }
        guard isRecording else {
            colmapCoachStatus = "Ready for room path"
            colmapCoachAction = "Start Auto Capture"
            colmapCoachDetail = "Walk a connected perimeter; keep the last wall or corner visible."
            colmapCoachScore = 0.2
            return
        }
        if trackingStatus != "normal" {
            colmapCoachStatus = "Tracking hold"
            colmapCoachAction = "Slow down"
            colmapCoachDetail = "Pause until ARKit tracking returns to normal."
            colmapCoachScore = 0.1
        } else if roomReconnectHold {
            colmapCoachStatus = "Reconnect path"
            colmapCoachAction = "Step back to last accepted view"
            colmapCoachDetail = lastAcceptedViewHint
            colmapCoachScore = 0.2
        } else if latestFeaturePointCount > 0 && latestFeaturePointCount < minRoomFeatureCount {
            colmapCoachStatus = "Weak visual tracks"
            colmapCoachAction = "Point at corners or texture"
            colmapCoachDetail = "Avoid blank walls; include shelves, posters, edges, or floor-wall seams."
            colmapCoachScore = 0.25
        } else if lastKeyframeDecision.contains("room_overlap_gap") {
            colmapCoachStatus = "Reconnect path"
            colmapCoachAction = "Step back toward last accepted view"
            colmapCoachDetail = "Keep the previous wall or corner in frame, then move in smaller steps."
            colmapCoachScore = 0.3
        } else if lastKeyframeDecision.contains("needs_translation_not_pan") {
            colmapCoachStatus = "Needs parallax"
            colmapCoachAction = "Side-step 20-40 cm"
            colmapCoachDetail = "Keep the same wall visible while moving sideways; do not only rotate."
            colmapCoachScore = 0.4
        } else if lastKeyframeDecision.contains("low_blur_score") {
            colmapCoachStatus = "Motion blur"
            colmapCoachAction = "Hold steady for haptic"
            colmapCoachDetail = "Let one sharp frame land before moving again."
            colmapCoachScore = 0.35
        } else if lastKeyframeDecision.contains("exposure") {
            colmapCoachStatus = "Exposure jump"
            colmapCoachAction = "Avoid bright/dark swings"
            colmapCoachDetail = "Pan away from windows slowly and keep overlap with the previous view."
            colmapCoachScore = 0.35
        } else if roomOverlapChainLength < 8 {
            colmapCoachStatus = "Building overlap"
            colmapCoachAction = "Move along the nearest wall"
            colmapCoachDetail = "Take small side steps and keep the same corner visible across frames."
            colmapCoachScore = 0.55
        } else if !roomLoopClosed {
            colmapCoachStatus = "Good chain"
            colmapCoachAction = "Continue toward the start point"
            colmapCoachDetail = "Close the loop while preserving overlap with the previous wall."
            colmapCoachScore = 0.75
        } else {
            colmapCoachStatus = "Loop ready"
            colmapCoachAction = "Add corners or finish"
            colmapCoachDetail = "Capture any missed corners with small overlapping side steps."
            colmapCoachScore = 0.9
        }
    }

    private func updateRoomQualityText() {
        guard scanTargetMode == "room" else {
            roomQualityText = "Room quality applies to room mode"
            roomLoopText = "Loop not used"
            roomOverlapText = "COLMAP chain not used"
            roomColmapHintText = "Room overlap applies to room mode"
            return
        }
        guard isRoomTargetLocked else {
            roomQualityText = "Lock room before recording"
            roomLoopText = "Loop open"
            roomOverlapText = "COLMAP chain pending"
            roomColmapHintText = "Lock Room, then walk a connected perimeter"
            return
        }
        if roomReconnectHold || lastKeyframeDecision.contains("room_overlap_gap") {
            roomQualityText = "Reconnect with previous view"
        } else if lastKeyframeDecision.contains("needs_translation_not_pan") {
            roomQualityText = "Side-step along the perimeter"
        } else if lastKeyframeDecision.contains("low_blur_score") {
            roomQualityText = "Hold steadier on textured surfaces"
        } else if lastKeyframeDecision.contains("exposure") {
            roomQualityText = "Keep brightness stable"
        } else if roomLoopClosed {
            roomQualityText = "Loop closed; add corners if needed"
        } else if roomPathLengthMeters < 1.0 {
            roomQualityText = "Start walking the room perimeter"
        } else {
            roomQualityText = "Continue perimeter and return near start"
        }
        roomLoopText = String(
            format: "%@ | %.1fm path | %.1fm max",
            roomLoopClosed ? "Loop closed" : "Loop open",
            roomPathLengthMeters,
            roomMaxDistanceFromStartMeters
        )
        if roomFragmentRiskCount > 0 {
            roomOverlapText = String(
                format: "COLMAP chain %d kept | %d reconnect prompts",
                roomOverlapChainLength,
                roomFragmentRiskCount
            )
        } else {
            roomOverlapText = String(
                format: "COLMAP chain %d kept | best %d",
                roomOverlapChainLength,
                roomLongestOverlapChainLength
            )
        }
        if roomLoopClosed {
            roomColmapHintText = "Loop closed; add corners with overlap"
        } else if roomOverlapChainLength < 8 {
            roomColmapHintText = "Build a connected overlap chain"
        } else {
            roomColmapHintText = "Keep small side steps until loop closes"
        }
    }

    private func coverageSectorIndex(for transform: simd_float4x4) -> Int {
        let sectorCount = max(coverageSectorCounts.count, 1)
        if scanTargetMode == "object", let target = lockedObjectWorldPosition {
            let fromTarget = cameraPosition(transform) - target
            let yaw = atan2(Double(fromTarget.x), Double(fromTarget.z))
            let normalized = (yaw + .pi) / (2 * .pi)
            return min(max(Int(normalized * Double(sectorCount)), 0), sectorCount - 1)
        }
        let forward = SIMD3<Float>(-transform.columns.2.x, -transform.columns.2.y, -transform.columns.2.z)
        let yaw = atan2(Double(forward.x), Double(forward.z))
        let normalized = (yaw + .pi) / (2 * .pi)
        return min(max(Int(normalized * Double(sectorCount)), 0), sectorCount - 1)
    }

    private func appendCoverageHistorySample(timestamp: TimeInterval, sectorIndex: Int, guidancePointCount: Int) {
        if coverageHistory.count >= maxCoverageHistorySamples {
            coverageHistoryWasTruncated = true
            coverageHistory.removeFirst(coverageHistory.count - maxCoverageHistorySamples + 1)
        }
        coverageHistory.append([
            "timestamp": timestamp,
            "sector_index": sectorIndex,
            "sector_observation_count": coverageSectorCounts[sectorIndex],
            "sector_progress": coverageSectors[sectorIndex],
            "covered_sector_count": coveredSectorCount(),
            "partial_sector_count": partialSectorCount(),
            "guidance_point_count": guidancePointCount,
            "tracking_state": trackingStatus,
        ])
    }

    private func coveredSectorCount() -> Int {
        coverageSectors.filter { $0 >= 1 }.count
    }

    private func partialSectorCount() -> Int {
        coverageSectors.filter { $0 > 0 && $0 < 1 }.count
    }

    private func missingSectorCountValue() -> Int {
        coverageSectors.filter { $0 < 1 }.count
    }

    private func coverageReport() -> [String: Any] {
        [
            "schema": "capture_splat.coverage_report.v0.1",
            "coverage_model": scanTargetMode == "object" && isObjectTargetLocked
                ? "object_relative_orbit_sectors_v0.1"
                : "coarse_yaw_sector_capture_guidance",
            "sector_count": coverageSectors.count,
            "observation_target_per_sector": coverageObservationTarget,
            "covered_sector_count": coveredSectorCount(),
            "partial_sector_count": partialSectorCount(),
            "missing_sector_count": missingSectorCount,
            "coverage_hint": coverageHintText,
            "readiness_state": readinessState,
            "next_action": nextAction,
            "background_warning": backgroundWarning,
            "current_sector_index": currentCoverageSector,
            "target_sector_index": targetCoverageSector,
            "coverage_navigation": coverageNavigationText,
            "sector_observation_counts": coverageSectorCounts,
            "sector_progress": coverageSectors,
            "history_sample_count": coverageHistory.count,
            "history_truncated": coverageHistoryWasTruncated,
            "history": coverageHistory,
            "authority": [
                "capture_guidance_only": true,
                "surface_coverage_authority": false,
                "metric_authority": false,
                "collision_geometry": false,
                "planning_authority": false,
                "semantic_authority": false,
            ],
        ]
    }

    private func keyframeReport() -> [String: Any] {
        [
            "schema": "capture_splat.keyframe_report.v0.1",
            "capture_model": isSmartAutoCaptureEnabled ? "quality_gated_smart_keyframes" : "fixed_interval_diagnostic",
            "minimum_keyframe_interval_seconds": activeMinimumKeyframeInterval,
            "max_captured_frames": activeMaxCapturedFrames,
            "accepted_keyframes": acceptedKeyframes,
            "skipped_keyframe_candidates": skippedKeyframes,
            "last_keyframe_decision": lastKeyframeDecision,
            "last_keyframe_score": keyframeScore,
            "skip_reason_counts": keyframeSkipReasonCounts,
            "event_sample_count": keyframeEvents.count,
            "events_truncated": keyframeEventsWereTruncated,
            "events": keyframeEvents,
            "authority": [
                "capture_guidance_only": true,
                "image_quality_authority": false,
                "surface_coverage_authority": false,
                "metric_authority": false,
                "planning_authority": false,
            ],
        ]
    }

    private func captureProfileReport() -> [String: Any] {
        let profileName: String
        let profileModel: String
        switch scanTargetMode {
        case "room":
            profileName = "room_colmap_keyframes"
            profileModel = "room_overlap_blur_parallax_reconnect_gate_v0.2"
        case "video_3dgs":
            profileName = "video_to_3dgs"
            profileModel = "video_style_rgbd_keyframe_stream_v0.1"
        default:
            profileName = "object_rgbd_keyframes"
            profileModel = "object_lock_extent_foreground_support_v0.1"
        }
        return [
            "schema": "capture_splat.profile_report.v0.1",
            "scan_target_mode": scanTargetMode,
            "capture_mode": captureModeLabel(),
            "profile_name": profileName,
            "profile_model": profileModel,
            "profile_text": captureProfileText,
            "profile_detail": captureProfileDetail,
            "minimum_keyframe_interval_seconds": activeMinimumKeyframeInterval,
            "max_captured_frames": activeMaxCapturedFrames,
            "accepted_keyframes": acceptedKeyframes,
            "skipped_keyframe_candidates": skippedKeyframes,
            "capture_blocker_status": captureBlockerStatus,
            "capture_blocker_detail": captureBlockerDetail,
            "last_accepted_view_hint": lastAcceptedViewHint,
            "outputs": [
                "rgb_jpegs",
                "lidar_depth_npy",
                "arkit_camera_poses",
                "imu_csv",
                "gps_csv",
                "host_nerfstudio_transforms_gate",
                "host_colmap_opensplat_gate",
            ],
            "authority": [
                "capture_guidance_only": true,
                "trainer_input_authority": false,
                "metric_authority": false,
                "collision_geometry": false,
                "planning_authority": false,
                "semantic_authority": false,
                "training_result": false,
            ],
        ]
    }

    private func roomCaptureQualityReport() -> [String: Any] {
        [
            "schema": "capture_splat.room_quality_report.v0.1",
            "quality_model": "room_keyframe_quality_gate_v0.1",
            "colmap_guidance_model": "room_colmap_overlap_chain_v0.1",
            "scan_target_mode": scanTargetMode,
            "room_locked": isRoomTargetLocked,
            "quality_hint": roomQualityText,
            "colmap_hint": roomColmapHintText,
            "colmap_coach_status": colmapCoachStatus,
            "colmap_coach_action": colmapCoachAction,
            "colmap_coach_detail": colmapCoachDetail,
            "colmap_coach_score": colmapCoachScore,
            "latest_feature_point_count": latestFeaturePointCount,
            "loop_status": roomLoopText,
            "overlap_status": roomOverlapText,
            "loop_closed": roomLoopClosed,
            "path_length_meters": roomPathLengthMeters,
            "max_distance_from_start_meters": roomMaxDistanceFromStartMeters,
            "overlap_chain_length": roomOverlapChainLength,
            "longest_overlap_chain_length": roomLongestOverlapChainLength,
            "fragment_risk_candidate_count": roomFragmentRiskCount,
            "reconnect_hold_active": roomReconnectHold,
            "capture_blocker_status": captureBlockerStatus,
            "capture_blocker_detail": captureBlockerDetail,
            "last_accepted_view_hint": lastAcceptedViewHint,
            "accepted_keyframes": acceptedKeyframes,
            "skipped_keyframe_candidates": skippedKeyframes,
            "haptic_accepted_count": hapticAcceptedCount,
            "last_keyframe_decision": lastKeyframeDecision,
            "last_keyframe_score": keyframeScore,
            "last_capture_quality": captureQualityText,
            "skip_reason_counts": keyframeSkipReasonCounts,
            "thresholds": [
                "min_blur_score": minBlurScore,
                "min_exposure_mean": minExposureMean,
                "max_exposure_mean": maxExposureMean,
                "max_exposure_jump": maxExposureJump,
                "min_room_parallax_meters": minRoomParallaxMeters,
                "min_object_parallax_meters": minObjectParallaxMeters,
                "min_video_parallax_meters": minVideoParallaxMeters,
                "max_room_connected_step_meters": maxRoomConnectedStepMeters,
                "max_room_connected_sector_jump": maxRoomConnectedSectorJump,
                "min_room_overlap_score": minRoomOverlapScore,
            ],
            "event_sample_count": roomQualityEvents.count,
            "events_truncated": roomQualityEventsWereTruncated,
            "events": roomQualityEvents,
            "authority": [
                "capture_guidance_only": true,
                "image_quality_authority": false,
                "surface_coverage_authority": false,
                "metric_authority": false,
                "collision_geometry": false,
                "planning_authority": false,
                "semantic_authority": false,
                "training_result": false,
            ],
        ]
    }

    private func targetLockReport() -> [String: Any] {
        var objectPosition: Any = NSNull()
        if let position = lockedObjectWorldPosition {
            objectPosition = [position.x, position.y, position.z]
        }
        return [
            "schema": "capture_splat.target_lock_report.v0.1",
            "scan_target_mode": scanTargetMode,
            "object_locked": isObjectTargetLocked,
            "room_locked": isRoomTargetLocked,
            "target_lock_status": targetLockStatus,
            "target_lock_detail": targetLockDetail,
            "target_lock_distance": targetLockDistanceText,
            "object_world_position": objectPosition,
            "object_extent_locked": isObjectExtentLocked,
            "coverage_model": scanTargetMode == "object" && isObjectTargetLocked
                ? "object_relative_orbit_sectors_v0.1"
                : "coarse_yaw_sector_capture_guidance",
            "authority": [
                "capture_guidance_only": true,
                "object_identity_authority": false,
                "object_extent_authority": false,
                "room_boundary_authority": false,
                "metric_authority": false,
                "planning_authority": false,
            ],
        ]
    }

    private func objectExtentReport() -> [String: Any] {
        var report: [String: Any] = [
            "schema": "capture_splat.object_extent_report.v0.1",
            "extent_model": "center_depth_band_foreground_proposal_v0.1",
            "scan_target_mode": scanTargetMode,
            "object_locked": isObjectTargetLocked,
            "object_extent_locked": isObjectExtentLocked,
            "object_mask_enabled": isObjectMaskEnabled,
            "object_extent_status": objectExtentStatus,
            "object_extent_detail": objectExtentDetail,
            "object_extent_size": objectExtentSizeText,
            "raw_rgb_depth_preserved": true,
            "mask_applied_to_raw_frames": false,
            "authority": [
                "capture_guidance_only": true,
                "foreground_mask_authority": false,
                "object_identity_authority": false,
                "object_extent_authority": false,
                "metric_authority": false,
                "collision_geometry": false,
                "planning_authority": false,
                "semantic_authority": false,
            ],
        ]
        guard let proposal = lockedObjectExtentProposal else {
            report["extent_available"] = false
            return report
        }
        report["extent_available"] = true
        report["normalized_bbox"] = [
            "x": proposal.overlay.normalizedX,
            "y": proposal.overlay.normalizedY,
            "w": proposal.overlay.normalizedWidth,
            "h": proposal.overlay.normalizedHeight,
        ]
        report["center_depth_meters"] = proposal.centerDepthMeters
        report["depth_band_meters"] = [
            "min": proposal.depthMinMeters,
            "max": proposal.depthMaxMeters,
        ]
        report["foreground_sample_count"] = proposal.foregroundSampleCount
        report["valid_sample_count"] = proposal.validSampleCount
        report["depth_image_resolution"] = [
            "w": proposal.depthImageWidth,
            "h": proposal.depthImageHeight,
        ]
        report["approximate_size_meters"] = [
            "w": proposal.approximateWidthMeters,
            "h": proposal.approximateHeightMeters,
            "radius": proposal.approximateRadiusMeters,
        ]
        report["timestamp"] = proposal.timestamp
        return report
    }

    private func objectMatteReport() -> [String: Any] {
        var objectPosition: Any = NSNull()
        if let position = lockedObjectWorldPosition {
            objectPosition = [position.x, position.y, position.z]
        }
        var report: [String: Any] = [
            "schema": "capture_splat.object_matte_report.v0.1",
            "matte_model": "locked_object_extent_depth_band_pose_support_v0.1",
            "scan_target_mode": scanTargetMode,
            "object_locked": isObjectTargetLocked,
            "object_extent_locked": isObjectExtentLocked,
            "object_mask_enabled": isObjectMaskEnabled,
            "object_matte_available": isObjectTargetLocked && isObjectExtentLocked && isObjectMaskEnabled,
            "object_world_position": objectPosition,
            "frame_record_count": objectMatteFrameRecords.count,
            "frame_records_truncated": objectMatteFrameRecordsWereTruncated,
            "support_counts": objectMatteSupportCounts,
            "frame_records": objectMatteFrameRecords,
            "raw_rgb_depth_preserved": true,
            "mask_applied_to_raw_frames": false,
            "authority": [
                "capture_guidance_only": true,
                "foreground_proposal": true,
                "foreground_mask_authority": false,
                "object_identity_authority": false,
                "object_extent_authority": false,
                "metric_authority": false,
                "collision_geometry": false,
                "planning_authority": false,
                "semantic_authority": false,
                "training_result": false,
            ],
        ]
        if let proposal = lockedObjectExtentProposal {
            report["locked_extent"] = [
                "normalized_bbox": [
                    "x": proposal.overlay.normalizedX,
                    "y": proposal.overlay.normalizedY,
                    "w": proposal.overlay.normalizedWidth,
                    "h": proposal.overlay.normalizedHeight,
                ],
                "depth_band_meters": [
                    "min": proposal.depthMinMeters,
                    "max": proposal.depthMaxMeters,
                ],
                "approximate_size_meters": [
                    "w": proposal.approximateWidthMeters,
                    "h": proposal.approximateHeightMeters,
                    "radius": proposal.approximateRadiusMeters,
                ],
                "timestamp": proposal.timestamp,
            ]
        }
        return report
    }

    private func evaluateKeyframeCandidate(
        depthValidRatio: Double,
        sectorIndex: Int,
        frameQuality: FrameQualityEstimate
    ) -> KeyframeDecision {
        guard isSmartAutoCaptureEnabled else {
            return KeyframeDecision(
                shouldCapture: true,
                reason: "fixed_interval",
                score: 1.0,
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard trackingStatus == "normal" else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "tracking_not_normal",
                score: 0.1,
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard depthValidRatio >= 0.35 else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "low_depth_coverage",
                score: depthValidRatio,
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard frameQuality.exposureMean >= minExposureMean else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "underexposed_frame",
                score: frameQuality.exposureMean,
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard frameQuality.exposureMean <= maxExposureMean else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "overexposed_frame",
                score: 1.0 - frameQuality.exposureMean,
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard frameQuality.blurScore >= minBlurScore else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "low_blur_score",
                score: frameQuality.blurScore,
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard scheduledFrameCount > 0 else {
            return KeyframeDecision(
                shouldCapture: true,
                reason: "first_keyframe",
                score: 1.0,
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard frameQuality.exposureDelta <= maxExposureJump else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "exposure_jump",
                score: max(0, 1.0 - frameQuality.exposureDelta),
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }

        let boundedSector = min(max(sectorIndex, 0), coverageSectors.count - 1)
        let sectorProgress = coverageSectors[boundedSector]
        let moved = frameQuality.parallaxMeters
        let requiredParallax: Double
        if scanTargetMode == "room" {
            requiredParallax = minRoomParallaxMeters
        } else if scanTargetMode == "video_3dgs" {
            requiredParallax = minVideoParallaxMeters
        } else {
            requiredParallax = minObjectParallaxMeters
        }
        let repeatedSector = lastAcceptedSectorIndex == boundedSector
        let sectorDelta = scanTargetMode == "room"
            ? lastAcceptedSectorIndex.map { circularSectorDistance(from: $0, to: boundedSector, count: coverageSectors.count) }
            : nil
        let overlapScore = roomColmapOverlapScore(
            parallaxMeters: moved,
            sectorDelta: sectorDelta,
            loopClosureCandidate: frameQuality.loopClosureCandidate
        )
        let roomFragmentRisk = scanTargetMode == "room"
            && scheduledFrameCount > 3
            && (overlapScore < minRoomOverlapScore || moved > maxRoomConnectedStepMeters * 1.4)
        var score = 0.35 + min(max(depthValidRatio, 0), 1) * 0.25

        score += sectorProgress < 1 ? 0.25 : 0.05
        score += min(frameQuality.blurScore / 0.04, 1.0) * 0.10
        if scanTargetMode == "room" {
            score += overlapScore * 0.10
        }
        if moved >= requiredParallax {
            score += 0.15
        }
        if scanTargetMode == "room", roomReconnectHold {
            let connectedSector = (sectorDelta ?? 0) <= maxRoomConnectedSectorJump
            let connectedStep = moved <= maxRoomConnectedStepMeters
            let enoughFeatures = latestFeaturePointCount == 0 || latestFeaturePointCount >= minRoomFeatureCount
            if connectedSector, connectedStep, enoughFeatures {
                return KeyframeDecision(
                    shouldCapture: false,
                    reason: "reconnected_to_last_view",
                    score: min(max(score, 0.55), 0.7),
                    sectorIndex: boundedSector,
                    frameQuality: frameQuality,
                    sectorDeltaFromLastAccepted: sectorDelta,
                    colmapOverlapScore: overlapScore,
                    roomFragmentRisk: false
                )
            }
            return KeyframeDecision(
                shouldCapture: false,
                reason: "room_reconnect_hold",
                score: min(score, overlapScore),
                sectorIndex: boundedSector,
                frameQuality: frameQuality,
                sectorDeltaFromLastAccepted: sectorDelta,
                colmapOverlapScore: overlapScore,
                roomFragmentRisk: true
            )
        }
        guard moved >= requiredParallax else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: scanTargetMode == "room" ? "needs_translation_not_pan" : "too_similar_to_last_keyframe",
                score: score,
                sectorIndex: boundedSector,
                frameQuality: frameQuality,
                sectorDeltaFromLastAccepted: sectorDelta,
                colmapOverlapScore: overlapScore,
                roomFragmentRisk: roomFragmentRisk
            )
        }
        if roomFragmentRisk {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "room_overlap_gap",
                score: min(score, overlapScore),
                sectorIndex: boundedSector,
                frameQuality: frameQuality,
                sectorDeltaFromLastAccepted: sectorDelta,
                colmapOverlapScore: overlapScore,
                roomFragmentRisk: true
            )
        }
        if repeatedSector && moved < requiredParallax * 1.4 {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "too_similar_to_last_keyframe",
                score: score,
                sectorIndex: boundedSector,
                frameQuality: frameQuality,
                sectorDeltaFromLastAccepted: sectorDelta,
                colmapOverlapScore: overlapScore,
                roomFragmentRisk: roomFragmentRisk
            )
        }
        if sectorProgress >= 1, missingSectorCount > 0, moved < 0.18 {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "angle_already_covered",
                score: score,
                sectorIndex: boundedSector,
                frameQuality: frameQuality,
                sectorDeltaFromLastAccepted: sectorDelta,
                colmapOverlapScore: overlapScore,
                roomFragmentRisk: roomFragmentRisk
            )
        }
        return KeyframeDecision(
            shouldCapture: score >= 0.72,
            reason: score >= 0.72 ? "useful_keyframe" : "score_below_threshold",
            score: score,
            sectorIndex: boundedSector,
            frameQuality: frameQuality,
            sectorDeltaFromLastAccepted: sectorDelta,
            colmapOverlapScore: overlapScore,
            roomFragmentRisk: roomFragmentRisk
        )
    }

    private func circularSectorDistance(from previous: Int, to current: Int, count: Int) -> Int {
        guard count > 0 else { return 0 }
        let boundedPrevious = ((previous % count) + count) % count
        let boundedCurrent = ((current % count) + count) % count
        let direct = abs(boundedCurrent - boundedPrevious)
        return min(direct, count - direct)
    }

    private func roomColmapOverlapScore(
        parallaxMeters: Double,
        sectorDelta: Int?,
        loopClosureCandidate: Bool
    ) -> Double {
        guard scanTargetMode == "room", let sectorDelta else { return 1.0 }
        let sectorScore: Double
        if sectorDelta <= 1 {
            sectorScore = 1.0
        } else if sectorDelta <= maxRoomConnectedSectorJump {
            sectorScore = 0.7
        } else {
            sectorScore = 0.2
        }

        let stepScore: Double
        if parallaxMeters <= maxRoomConnectedStepMeters * 0.55 {
            stepScore = 1.0
        } else if parallaxMeters <= maxRoomConnectedStepMeters {
            stepScore = 0.7
        } else {
            stepScore = 0.2
        }

        let loopBoost = loopClosureCandidate ? 0.15 : 0.0
        return min(1.0, sectorScore * 0.6 + stepScore * 0.4 + loopBoost)
    }

    private func cameraPosition(_ transform: simd_float4x4) -> SIMD3<Float> {
        SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
    }

    private func cameraForward(_ transform: simd_float4x4) -> SIMD3<Float> {
        let forward = SIMD3<Float>(-transform.columns.2.x, -transform.columns.2.y, -transform.columns.2.z)
        let length = simd_length(forward)
        guard length > 0 else { return SIMD3<Float>(0, 0, -1) }
        return forward / length
    }

    private func recordKeyframeEvent(
        accepted: Bool,
        decision: KeyframeDecision,
        timestamp: TimeInterval,
        depthValidRatio: Double
    ) {
        updateCaptureBlocker(accepted: accepted, decision: decision)
        if keyframeEvents.count >= maxKeyframeEventSamples {
            keyframeEventsWereTruncated = true
            keyframeEvents.removeFirst(keyframeEvents.count - maxKeyframeEventSamples + 1)
        }
        keyframeEvents.append([
            "timestamp": timestamp,
            "accepted": accepted,
            "reason": decision.reason,
            "score": decision.score,
            "sector_index": decision.sectorIndex,
            "sector_delta_from_last_accepted": decision.sectorDeltaFromLastAccepted ?? NSNull(),
            "valid_depth_ratio": depthValidRatio,
            "feature_point_count": latestFeaturePointCount,
            "blur_score": decision.blurScore,
            "exposure_mean": decision.exposureMean,
            "exposure_delta": decision.exposureDelta,
            "parallax_meters": decision.parallaxMeters,
            "colmap_overlap_score": decision.colmapOverlapScore,
            "room_fragment_risk": decision.roomFragmentRisk,
            "room_reconnect_hold": roomReconnectHold,
            "capture_blocker_status": captureBlockerStatus,
            "capture_blocker_detail": captureBlockerDetail,
            "last_accepted_view_hint": lastAcceptedViewHint,
            "covered_sector_count": coveredSectorCount(),
            "missing_sector_count": missingSectorCount,
            "tracking_state": trackingStatus,
        ])
        keyframeScore = decision.score
        lastKeyframeDecision = accepted ? "Accepted: \(decision.reason)" : "Skipped: \(decision.reason)"
        captureQualityText = String(
            format: "blur %.3f | exp %.2f | move %.2fm",
            decision.blurScore,
            decision.exposureMean,
            decision.parallaxMeters
        )
        recordRoomQualityEvent(accepted: accepted, decision: decision, timestamp: timestamp, depthValidRatio: depthValidRatio)
        if !accepted {
            skippedKeyframes += 1
            keyframeSkipReasonCounts[decision.reason, default: 0] += 1
        }
    }

    private func updateCaptureBlocker(accepted: Bool, decision: KeyframeDecision) {
        if scanTargetMode == "room" {
            if decision.reason == "room_overlap_gap" || decision.reason == "room_reconnect_hold" {
                roomReconnectHold = true
            } else if accepted || decision.reason == "reconnected_to_last_view" {
                roomReconnectHold = false
            }
        }

        if accepted {
            captureBlockerStatus = "Clear"
            captureBlockerDetail = "Accepted keyframe. Continue with small overlapping steps."
            if scanTargetMode == "room" {
                lastAcceptedViewHint = "Last good view: sector \(decision.sectorIndex + 1), chain \(roomOverlapChainLength)."
            } else {
                lastAcceptedViewHint = "Last accepted sector \(decision.sectorIndex + 1)."
            }
            return
        }

        switch decision.reason {
        case "room_overlap_gap", "room_reconnect_hold":
            captureBlockerStatus = "Reconnect path"
            captureBlockerDetail = "No keyframes accepted. Step back until the last wall or corner is visible again."
            lastAcceptedViewHint = String(
                format: "Last good view is %.2fm away; target <= %.2fm and keep the same corner in frame.",
                decision.parallaxMeters,
                maxRoomConnectedStepMeters
            )
        case "reconnected_to_last_view":
            captureBlockerStatus = "Side-step now"
            captureBlockerDetail = "Back near the last accepted view. Move sideways 20-40 cm while keeping overlap."
            lastAcceptedViewHint = "Reconnect cleared; next haptic should come after a small side-step."
        case "needs_translation_not_pan":
            captureBlockerStatus = "Needs translation"
            captureBlockerDetail = "Do not only rotate. Side-step while keeping the same subject or wall in view."
        case "low_blur_score":
            captureBlockerStatus = "Motion blur"
            captureBlockerDetail = "Hold steady on textured detail until you feel a haptic."
        case "exposure_jump", "underexposed_frame", "overexposed_frame":
            captureBlockerStatus = "Exposure hold"
            captureBlockerDetail = "Avoid sudden bright or dark swings; move slowly across windows and dark corners."
        case "low_depth_coverage":
            captureBlockerStatus = "Weak LiDAR"
            captureBlockerDetail = "Move closer to textured surfaces and avoid glass, dark fabric, or far blank areas."
        case "tracking_not_normal":
            captureBlockerStatus = "Tracking hold"
            captureBlockerDetail = "Pause until ARKit tracking returns to normal."
        default:
            captureBlockerStatus = "Waiting"
            captureBlockerDetail = "Waiting for a sharper, more useful keyframe."
        }
    }

    private func recordRoomQualityEvent(
        accepted: Bool,
        decision: KeyframeDecision,
        timestamp: TimeInterval,
        depthValidRatio: Double
    ) {
        guard scanTargetMode == "room" || isRoomTargetLocked else { return }
        if !accepted && decision.roomFragmentRisk {
            roomFragmentRiskCount += 1
        }
        let distanceFromStart: Any = decision.distanceFromStartMeters ?? NSNull()
        if roomQualityEvents.count >= maxRoomQualityEventSamples {
            roomQualityEventsWereTruncated = true
            roomQualityEvents.removeFirst(roomQualityEvents.count - maxRoomQualityEventSamples + 1)
        }
        roomQualityEvents.append([
            "timestamp": timestamp,
            "accepted": accepted,
            "reason": decision.reason,
            "score": decision.score,
            "sector_index": decision.sectorIndex,
            "sector_delta_from_last_accepted": decision.sectorDeltaFromLastAccepted ?? NSNull(),
            "valid_depth_ratio": depthValidRatio,
            "feature_point_count": latestFeaturePointCount,
            "blur_score": decision.blurScore,
            "exposure_mean": decision.exposureMean,
            "exposure_delta": decision.exposureDelta,
            "parallax_meters": decision.parallaxMeters,
            "colmap_overlap_score": decision.colmapOverlapScore,
            "room_fragment_risk": decision.roomFragmentRisk,
            "room_reconnect_hold": roomReconnectHold,
            "capture_blocker_status": captureBlockerStatus,
            "capture_blocker_detail": captureBlockerDetail,
            "overlap_chain_length": roomOverlapChainLength,
            "longest_overlap_chain_length": roomLongestOverlapChainLength,
            "fragment_risk_count": roomFragmentRiskCount,
            "path_length_meters": decision.pathLengthMeters,
            "distance_from_start_meters": distanceFromStart,
            "loop_closure_candidate": decision.loopClosureCandidate,
            "loop_closed": roomLoopClosed,
            "haptic_fired": accepted,
        ])
        updateRoomQualityText()
    }

    private func recordAcceptedRoomOverlap(decision: KeyframeDecision) {
        guard scanTargetMode == "room" || isRoomTargetLocked else { return }
        if decision.roomFragmentRisk {
            roomFragmentRiskCount += 1
            roomOverlapChainLength = max(roomOverlapChainLength, 1)
        } else {
            roomOverlapChainLength += 1
        }
        roomLongestOverlapChainLength = max(roomLongestOverlapChainLength, roomOverlapChainLength)
        updateRoomQualityText()
    }

    private func recordAcceptedRoomPath(position: SIMD3<Float>) {
        guard scanTargetMode == "room" || isRoomTargetLocked else { return }
        if roomStartPosition == nil {
            roomStartPosition = lockedRoomWorldTransform.map { cameraPosition($0) } ?? position
        }
        if let previous = roomLastAcceptedPosition {
            roomPathLengthMeters += Double(simd_distance(position, previous))
        }
        roomLastAcceptedPosition = position
        if let start = roomStartPosition {
            let distanceFromStart = Double(simd_distance(position, start))
            roomMaxDistanceFromStartMeters = max(roomMaxDistanceFromStartMeters, distanceFromStart)
            if roomPathLengthMeters >= 1.5, distanceFromStart <= 0.75 {
                roomLoopClosed = true
            }
        }
        updateRoomQualityText()
    }

    private func playAcceptedKeyframeHaptic() {
        acceptedHaptic.impactOccurred(intensity: 0.65)
        acceptedHaptic.prepare()
        hapticAcceptedCount += 1
    }

    private func appendObjectMatteFrameRecord(_ record: [String: Any]?) {
        guard let record else { return }
        if objectMatteFrameRecords.count >= maxObjectMatteFrameSamples {
            objectMatteFrameRecordsWereTruncated = true
            objectMatteFrameRecords.removeFirst(objectMatteFrameRecords.count - maxObjectMatteFrameSamples + 1)
        }
        objectMatteFrameRecords.append(record)
        let status = record["support_status"] as? String ?? "unknown"
        objectMatteSupportCounts[status, default: 0] += 1
    }

    private func makeObjectMatteFrameRecord(
        frame: ARFrame,
        depthMap: CVPixelBuffer,
        confidenceMap: CVPixelBuffer?,
        intrinsics: CameraIntrinsics,
        frameNumber: Int,
        rgbPath: String,
        depthPath: String,
        confidencePath: String?,
        sectorIndex: Int,
        depthValidRatio: Double
    ) -> [String: Any]? {
        guard scanTargetMode == "object",
              isObjectTargetLocked,
              isObjectExtentLocked,
              isObjectMaskEnabled,
              let objectCenter = lockedObjectWorldPosition,
              let extent = lockedObjectExtentProposal else {
            return nil
        }

        let projection = projectObjectCenter(
            objectCenter,
            transform: frame.camera.transform,
            intrinsics: intrinsics
        )
        let depthSupport = objectMatteDepthSupport(
            depthMap: depthMap,
            confidenceMap: confidenceMap,
            extent: extent,
            projection: projection
        )
        let camera = cameraPosition(frame.camera.transform)
        let objectDistance = simd_distance(camera, objectCenter)
        let previousDistance = lastAcceptedCameraPosition.map { simd_distance(camera, $0) }
        let previousBaseline: Any = previousDistance.map { Double($0) } ?? NSNull()
        let verticalDelta = objectDistance > 0 ? Double((camera.y - objectCenter.y) / objectDistance) : 0
        let elevation = asin(max(-1.0, min(1.0, verticalDelta))) * 180.0 / .pi
        let supportStatus = depthSupport["support_status"] as? String ?? "unknown"

        return [
            "frame_number": frameNumber,
            "rgb": rgbPath,
            "depth": depthPath,
            "confidence": confidencePath ?? NSNull(),
            "timestamp": frame.timestamp,
            "tracking_state": trackingStateText(frame.camera.trackingState),
            "sector_index": sectorIndex,
            "valid_depth_ratio": depthValidRatio,
            "support_status": supportStatus,
            "projection": projection,
            "depth_support": depthSupport,
            "pose_support": [
                "camera_position_world": [camera.x, camera.y, camera.z],
                "object_distance_meters": objectDistance,
                "elevation_angle_degrees": elevation,
                "previous_keyframe_baseline_meters": previousBaseline,
            ],
            "authority": [
                "foreground_proposal": true,
                "foreground_mask_authority": false,
                "object_identity_authority": false,
                "object_extent_authority": false,
                "metric_authority": false,
                "planning_authority": false,
                "training_result": false,
            ],
        ]
    }

    private func projectObjectCenter(
        _ objectCenter: SIMD3<Float>,
        transform: simd_float4x4,
        intrinsics: CameraIntrinsics
    ) -> [String: Any] {
        let cameraPoint = simd_inverse(transform) * SIMD4<Float>(objectCenter.x, objectCenter.y, objectCenter.z, 1)
        let cameraZ = Double(cameraPoint.z)
        guard cameraZ.isFinite, abs(cameraZ) > 1e-6 else {
            return [
                "status": "not_projectable",
                "projected_center_px": NSNull(),
                "projected_center_normalized": NSNull(),
                "optical_depth_meters": NSNull(),
                "projection_convention": "none",
                "inside_depth_image": false,
            ]
        }
        let opticalDepth = cameraZ > 0 ? cameraZ : -cameraZ
        let u = Double(intrinsics.flX) * Double(cameraPoint.x) / opticalDepth + Double(intrinsics.cx)
        let v = cameraZ < 0
            ? Double(intrinsics.cy) - Double(intrinsics.flY) * Double(cameraPoint.y) / opticalDepth
            : Double(intrinsics.flY) * Double(cameraPoint.y) / opticalDepth + Double(intrinsics.cy)
        guard u.isFinite, v.isFinite, opticalDepth.isFinite else {
            return [
                "status": "non_finite_projection",
                "projected_center_px": NSNull(),
                "projected_center_normalized": NSNull(),
                "optical_depth_meters": NSNull(),
                "projection_convention": cameraZ < 0 ? "arkit_negative_z" : "opencv_positive_z",
                "inside_depth_image": false,
            ]
        }
        let width = max(intrinsics.w, 1)
        let height = max(intrinsics.h, 1)
        let inside = u >= 0 && u < Double(width) && v >= 0 && v < Double(height)
        return [
            "status": inside ? "inside_depth_image" : "outside_depth_image",
            "projected_center_px": [u, v],
            "projected_center_normalized": [u / Double(width), v / Double(height)],
            "optical_depth_meters": opticalDepth,
            "projection_convention": cameraZ < 0 ? "arkit_negative_z" : "opencv_positive_z",
            "inside_depth_image": inside,
        ]
    }

    private func objectMatteDepthSupport(
        depthMap: CVPixelBuffer,
        confidenceMap: CVPixelBuffer?,
        extent: ObjectExtentProposal,
        projection: [String: Any]
    ) -> [String: Any] {
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        if let confidenceMap {
            CVPixelBufferLockBaseAddress(confidenceMap, .readOnly)
        }
        defer {
            if let confidenceMap {
                CVPixelBufferUnlockBaseAddress(confidenceMap, .readOnly)
            }
            CVPixelBufferUnlockBaseAddress(depthMap, .readOnly)
        }

        let width = CVPixelBufferGetWidth(depthMap)
        let height = CVPixelBufferGetHeight(depthMap)
        guard width > 0,
              height > 0,
              let depthBase = CVPixelBufferGetBaseAddress(depthMap)?.assumingMemoryBound(to: Float32.self) else {
            return ["support_status": "unsupported", "reason": "missing_depth_base"]
        }
        let confidenceBase = confidenceMap.flatMap {
            CVPixelBufferGetBaseAddress($0)?.assumingMemoryBound(to: UInt8.self)
        }

        let projected = projection["projected_center_px"] as? [Double]
        let fallbackCenterX = (extent.overlay.normalizedX + extent.overlay.normalizedWidth * 0.5) * Double(width)
        let fallbackCenterY = (extent.overlay.normalizedY + extent.overlay.normalizedHeight * 0.5) * Double(height)
        let centerX = projected?.first ?? fallbackCenterX
        let centerY = projected?.dropFirst().first ?? fallbackCenterY
        let boxWidth = max(4, Int(round(extent.overlay.normalizedWidth * Double(width))))
        let boxHeight = max(4, Int(round(extent.overlay.normalizedHeight * Double(height))))
        let x0 = max(0, min(width - 1, Int(floor(centerX - Double(boxWidth) * 0.5))))
        let y0 = max(0, min(height - 1, Int(floor(centerY - Double(boxHeight) * 0.5))))
        let x1 = max(x0 + 1, min(width, Int(ceil(centerX + Double(boxWidth) * 0.5))))
        let y1 = max(y0 + 1, min(height, Int(ceil(centerY + Double(boxHeight) * 0.5))))
        let depthMargin = max(0.03, (extent.depthMaxMeters - extent.depthMinMeters) * 0.10)
        let minDepth = max(0.01, extent.depthMinMeters - depthMargin)
        let maxDepth = extent.depthMaxMeters + depthMargin
        let sampleStride = 2
        var validSamples = 0
        var confidenceSamples = 0
        var foregroundSamples = 0

        for y in stride(from: y0, to: y1, by: sampleStride) {
            for x in stride(from: x0, to: x1, by: sampleStride) {
                let offset = y * width + x
                let value = Double(depthBase[offset])
                guard value.isFinite, value > 0 else { continue }
                validSamples += 1
                if let confidenceBase, confidenceBase[offset] >= 1 {
                    confidenceSamples += 1
                }
                guard value >= minDepth, value <= maxDepth else { continue }
                if let confidenceBase {
                    if confidenceBase[offset] >= 1 {
                        foregroundSamples += 1
                    }
                } else {
                    foregroundSamples += 1
                }
            }
        }

        let foregroundFraction = validSamples > 0 ? Double(foregroundSamples) / Double(validSamples) : 0.0
        let projectionInside = projection["inside_depth_image"] as? Bool ?? false
        let supportStatus: String
        if projectionInside, foregroundSamples >= 20, foregroundFraction >= 0.10 {
            supportStatus = "supported"
        } else if foregroundSamples > 0 || validSamples > 0 {
            supportStatus = "weak"
        } else {
            supportStatus = "unsupported"
        }
        return [
            "support_status": supportStatus,
            "depth_bbox_px": ["x0": x0, "y0": y0, "x1": x1, "y1": y1, "w": width, "h": height],
            "depth_band_meters": ["min": minDepth, "max": maxDepth],
            "valid_sample_count": validSamples,
            "confidence_sample_count": confidenceSamples,
            "foreground_sample_count": foregroundSamples,
            "foreground_fraction_of_valid": foregroundFraction,
            "sample_stride": sampleStride,
        ]
    }

    private func sampleGuidancePoints(from pixelBuffer: CVPixelBuffer) -> [ScanGuidancePoint] {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        guard width > 0,
              height > 0,
              let base = CVPixelBufferGetBaseAddress(pixelBuffer)?.assumingMemoryBound(to: Float32.self) else {
            return []
        }

        let columns = 8
        let rows = 6
        var points: [ScanGuidancePoint] = []
        points.reserveCapacity(columns * rows)

        for row in 0..<rows {
            for column in 0..<columns {
                let x = min(max((column * width) / columns + width / (columns * 2), 0), width - 1)
                let y = min(max((row * height) / rows + height / (rows * 2), 0), height - 1)
                let depth = base[y * width + x]
                guard depth.isFinite, depth > 0 else { continue }
                points.append(ScanGuidancePoint(
                    id: row * columns + column,
                    normalizedX: Double(x) / Double(max(width - 1, 1)),
                    normalizedY: Double(y) / Double(max(height - 1, 1)),
                    depthMeters: Double(depth)
                ))
            }
        }
        return points
    }

    private func estimateFrameQuality(from frame: ARFrame) -> FrameQualityEstimate {
        let imageQuality = estimateImageQuality(from: frame.capturedImage)
        let position = cameraPosition(frame.camera.transform)
        let parallax = lastAcceptedCameraPosition.map { Double(simd_distance(position, $0)) } ?? 0
        let exposureDelta = lastAcceptedExposureMean.map { abs(imageQuality.exposureMean - $0) } ?? 0
        let start = roomStartPosition ?? lockedRoomWorldTransform.map { cameraPosition($0) }
        let distanceFromStart = start.map { Double(simd_distance(position, $0)) }
        let projectedPath = roomPathLengthMeters + (roomLastAcceptedPosition.map { Double(simd_distance(position, $0)) } ?? 0)
        let loopCandidate = scanTargetMode == "room"
            && projectedPath >= 1.5
            && (distanceFromStart ?? .greatestFiniteMagnitude) <= 0.75
        return FrameQualityEstimate(
            blurScore: imageQuality.blurScore,
            exposureMean: imageQuality.exposureMean,
            exposureDelta: exposureDelta,
            parallaxMeters: parallax,
            pathLengthMeters: projectedPath,
            distanceFromStartMeters: distanceFromStart,
            loopClosureCandidate: loopCandidate
        )
    }

    private func estimateImageQuality(from pixelBuffer: CVPixelBuffer) -> (blurScore: Double, exposureMean: Double) {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        let width: Int
        let height: Int
        let bytesPerRow: Int
        let baseAddress: UnsafeMutableRawPointer?
        if CVPixelBufferIsPlanar(pixelBuffer) {
            width = CVPixelBufferGetWidthOfPlane(pixelBuffer, 0)
            height = CVPixelBufferGetHeightOfPlane(pixelBuffer, 0)
            bytesPerRow = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0)
            baseAddress = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0)
        } else {
            width = CVPixelBufferGetWidth(pixelBuffer)
            height = CVPixelBufferGetHeight(pixelBuffer)
            bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
            baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer)
        }
        guard width > 2,
              height > 2,
              bytesPerRow > 0,
              let baseAddress else {
            return (0, 0.5)
        }

        let base = baseAddress.assumingMemoryBound(to: UInt8.self)
        let stepX = max(width / 32, 8)
        let stepY = max(height / 32, 8)
        var sampleCount = 0
        var exposureSum = 0.0
        var edgeSum = 0.0

        for y in stride(from: stepY, to: max(height - 1, stepY + 1), by: stepY) {
            for x in stride(from: stepX, to: max(width - 1, stepX + 1), by: stepX) {
                let offset = y * bytesPerRow + x
                let value = Double(base[offset])
                let right = Double(base[y * bytesPerRow + min(x + 1, width - 1)])
                let down = Double(base[min(y + 1, height - 1) * bytesPerRow + x])
                exposureSum += value / 255.0
                edgeSum += (abs(value - right) + abs(value - down)) / 510.0
                sampleCount += 1
            }
        }

        guard sampleCount > 0 else { return (0, 0.5) }
        return (edgeSum / Double(sampleCount), exposureSum / Double(sampleCount))
    }

    private func makeObjectExtentProposal(
        from frame: ARFrame,
        depthMap: CVPixelBuffer,
        centerDepth: Float
    ) -> ObjectExtentProposal? {
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }

        let width = CVPixelBufferGetWidth(depthMap)
        let height = CVPixelBufferGetHeight(depthMap)
        guard width > 0,
              height > 0,
              centerDepth.isFinite,
              centerDepth > 0,
              let base = CVPixelBufferGetBaseAddress(depthMap)?.assumingMemoryBound(to: Float32.self) else {
            return nil
        }

        let tolerance = max(0.08, min(0.30, centerDepth * 0.18))
        let minDepth = max(0.01, centerDepth - tolerance)
        let maxDepth = centerDepth + tolerance
        let xStart = width / 5
        let xEnd = min(width - 1, width - xStart)
        let yStart = height / 6
        let yEnd = min(height - 1, height - yStart)
        let sampleStride = 2
        var validSamples = 0
        var foregroundSamples = 0
        var minX = width
        var minY = height
        var maxX = 0
        var maxY = 0

        for y in stride(from: yStart, through: yEnd, by: sampleStride) {
            for x in stride(from: xStart, through: xEnd, by: sampleStride) {
                let value = base[y * width + x]
                guard value.isFinite, value > 0 else { continue }
                validSamples += 1
                guard value >= minDepth, value <= maxDepth else { continue }
                foregroundSamples += 1
                minX = min(minX, x)
                maxX = max(maxX, x)
                minY = min(minY, y)
                maxY = max(maxY, y)
            }
        }

        guard foregroundSamples >= 20,
              minX <= maxX,
              minY <= maxY else {
            return nil
        }

        let imageSize = frame.camera.imageResolution
        let cameraIntrinsics = frame.camera.intrinsics
        let scaleX = Float(width) / Float(imageSize.width)
        let scaleY = Float(height) / Float(imageSize.height)
        let flX = Double(cameraIntrinsics[0, 0] * scaleX)
        let flY = Double(cameraIntrinsics[1, 1] * scaleY)
        let boxWidthPixels = Double(maxX - minX + 1)
        let boxHeightPixels = Double(maxY - minY + 1)
        let depth = Double(centerDepth)
        let approximateWidth = flX > 0 ? boxWidthPixels * depth / flX : 0
        let approximateHeight = flY > 0 ? boxHeightPixels * depth / flY : 0

        return ObjectExtentProposal(
            overlay: ObjectExtentOverlay(
                normalizedX: Double(minX) / Double(max(width - 1, 1)),
                normalizedY: Double(minY) / Double(max(height - 1, 1)),
                normalizedWidth: boxWidthPixels / Double(width),
                normalizedHeight: boxHeightPixels / Double(height)
            ),
            centerDepthMeters: depth,
            depthMinMeters: Double(minDepth),
            depthMaxMeters: Double(maxDepth),
            foregroundSampleCount: foregroundSamples,
            validSampleCount: validSamples,
            depthImageWidth: width,
            depthImageHeight: height,
            approximateWidthMeters: approximateWidth,
            approximateHeightMeters: approximateHeight,
            approximateRadiusMeters: max(approximateWidth, approximateHeight) * 0.5,
            timestamp: frame.timestamp
        )
    }

    private func refreshObjectExtentStatus() {
        guard scanTargetMode == "object" else {
            objectExtentStatus = "Extent not used"
            objectExtentDetail = "Object extent applies to object and flip modes."
            return
        }
        guard isObjectMaskEnabled else {
            objectExtentStatus = "Mask disabled"
            objectExtentDetail = "Enable Mask to save object extent metadata."
            objectExtentOverlay = nil
            return
        }
        guard isObjectTargetLocked else {
            objectExtentStatus = "Lock object first"
            objectExtentDetail = "Center the object, then tap Lock Object."
            return
        }
        if isObjectExtentLocked {
            objectExtentStatus = "Object extent locked"
            objectExtentDetail = "Foreground bounds saved as proposal metadata."
            return
        }
        if let proposal = latestObjectExtentProposal {
            objectExtentStatus = "Extent ready"
            objectExtentDetail = "Tap Lock Extent to save the foreground proposal."
            objectExtentSizeText = String(
                format: "%.2fm x %.2fm",
                proposal.approximateWidthMeters,
                proposal.approximateHeightMeters
            )
        } else {
            objectExtentStatus = "Extent needs LiDAR depth"
            objectExtentDetail = "Keep the object centered until a foreground depth box appears."
            objectExtentSizeText = "--"
        }
    }
}

extension CaptureController: ARSessionDelegate {
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        trackingStatus = trackingStateText(frame.camera.trackingState)
        latestFeaturePointCount = frame.rawFeaturePoints?.points.count ?? 0
        guard let sceneDepth = frame.sceneDepth else {
            guidancePoints.removeAll()
            if isRecording {
                droppedFrames += 1
                statusText = "Waiting for LiDAR scene depth"
            }
            updateGuidance()
            return
        }
        let candidateDepthValidRatio = measureValidDepthRatio(sceneDepth.depthMap)
        validDepthRatio = candidateDepthValidRatio
        updateTargetCandidate(from: frame, depthMap: sceneDepth.depthMap)
        updateLiveGuidance(from: frame, depthMap: sceneDepth.depthMap)
        updateGuidance()
        guard isRecording, let directory = currentSessionDirectory else { return }
        guard isRGBEnabled, isDepthEnabled else {
            statusText = "RGB and depth are required for Capture Splat export."
            return
        }
        guard !isWritingFrame else {
            droppedFrames += 1
            return
        }
        guard frame.timestamp - lastCandidateFrameTimestamp >= activeMinimumKeyframeInterval else { return }
        lastCandidateFrameTimestamp = frame.timestamp
        guard scheduledFrameCount < activeMaxCapturedFrames else {
            DispatchQueue.main.async {
                self.stopRecording()
                self.statusText = "Captured \(self.activeMaxCapturedFrames) frames. Tap Finalize."
            }
            return
        }
        let sectorIndex = coverageSectorIndex(for: frame.camera.transform)
        let frameQuality = estimateFrameQuality(from: frame)
        let keyframeDecision = evaluateKeyframeCandidate(
            depthValidRatio: candidateDepthValidRatio,
            sectorIndex: sectorIndex,
            frameQuality: frameQuality
        )
        guard keyframeDecision.shouldCapture else {
            recordKeyframeEvent(
                accepted: false,
                decision: keyframeDecision,
                timestamp: frame.timestamp,
                depthValidRatio: candidateDepthValidRatio
            )
            updateGuidance()
            return
        }
        if firstFrameTimestamp == nil { firstFrameTimestamp = frame.timestamp }
        lastFrameTimestamp = frame.timestamp

        scheduledFrameCount += 1
        lastScheduledFrameTimestamp = frame.timestamp
        isWritingFrame = true

        let index = scheduledFrameCount
        let rgbName = String(format: "frame_%06d.jpg", index)
        let depthName = String(format: "depth_%06d.npy", index)
        let confidenceName = String(format: "confidence_%06d.npy", index)
        let intrinsics = makeIntrinsics(frame: frame, depthMap: sceneDepth.depthMap)
        activeIntrinsics = intrinsics

        let rgbURL = directory.appendingPathComponent("rgb").appendingPathComponent(rgbName)
        let depthURL = directory.appendingPathComponent("depth").appendingPathComponent(depthName)
        let confidenceURL = directory.appendingPathComponent("confidence").appendingPathComponent(confidenceName)

        let rgbBuffer = frame.capturedImage
        let depthBuffer = sceneDepth.depthMap
        let confidenceBuffer = isConfidenceEnabled ? sceneDepth.confidenceMap : nil
        let transform = frame.camera.transform
        let trackingState = trackingStateText(frame.camera.trackingState)
        let timestamp = frame.timestamp
        let imageResolution = frame.camera.imageResolution
        activeResolution = Resolution(w: Int(imageResolution.width), h: Int(imageResolution.height))
        let objectMatteRecord = makeObjectMatteFrameRecord(
            frame: frame,
            depthMap: depthBuffer,
            confidenceMap: confidenceBuffer,
            intrinsics: intrinsics,
            frameNumber: index,
            rgbPath: "rgb/\(rgbName)",
            depthPath: "depth/\(depthName)",
            confidencePath: confidenceBuffer == nil ? nil : "confidence/\(confidenceName)",
            sectorIndex: keyframeDecision.sectorIndex,
            depthValidRatio: candidateDepthValidRatio
        )

        writeQueue.async { [weak self] in
            guard let self else { return }
            var writeError: Error?
            autoreleasepool {
                do {
                    try self.writeJPEG(from: rgbBuffer, to: rgbURL)
                    try self.writeDepth(depthBuffer, to: depthURL)
                    if let confidenceBuffer {
                        try self.writeConfidence(confidenceBuffer, to: confidenceURL)
                    }
                } catch {
                    writeError = error
                }
            }
            if let writeError {
                DispatchQueue.main.async {
                    self.isWritingFrame = false
                    self.statusText = "Frame write failed: \(writeError.localizedDescription)"
                }
                return
            }
            DispatchQueue.main.async {
                self.frames.append(CapturedFrame(
                    rgb: "rgb/\(rgbName)",
                    depth: "depth/\(depthName)",
                    confidence: confidenceBuffer == nil ? nil : "confidence/\(confidenceName)",
                    timestamp: timestamp,
                    transformMatrix: transform.rows,
                    intrinsics: intrinsics,
                    trackingState: trackingState
                ))
                self.rgbFrames += 1
                self.depthFrames += 1
                self.rgbRate = self.recordRateSample(&self.rgbRateSamples, at: timestamp)
                self.depthRate = self.recordRateSample(&self.depthRateSamples, at: timestamp)
                self.validDepthRatio = candidateDepthValidRatio
                self.acceptedKeyframes += 1
                self.recordAcceptedRoomOverlap(decision: keyframeDecision)
                self.lastAcceptedSectorIndex = keyframeDecision.sectorIndex
                let acceptedPosition = self.cameraPosition(transform)
                self.recordAcceptedRoomPath(position: acceptedPosition)
                self.lastAcceptedCameraPosition = acceptedPosition
                self.lastAcceptedExposureMean = keyframeDecision.exposureMean
                self.recordAcceptedCoverage(from: frame, sectorIndex: keyframeDecision.sectorIndex)
                self.recordKeyframeEvent(
                    accepted: true,
                    decision: keyframeDecision,
                    timestamp: timestamp,
                    depthValidRatio: candidateDepthValidRatio
                )
                self.appendObjectMatteFrameRecord(objectMatteRecord)
                self.playAcceptedKeyframeHaptic()
                self.isWritingFrame = false
                self.statusText = "Recording \(self.rgbFrames) smart frames"
                self.updateGuidance()
            }
        }
    }

    private func makeIntrinsics(frame: ARFrame, depthMap: CVPixelBuffer) -> CameraIntrinsics {
        let cameraIntrinsics = frame.camera.intrinsics
        let imageSize = frame.camera.imageResolution
        let depthWidth = CVPixelBufferGetWidth(depthMap)
        let depthHeight = CVPixelBufferGetHeight(depthMap)
        let scaleX = Float(depthWidth) / Float(imageSize.width)
        let scaleY = Float(depthHeight) / Float(imageSize.height)
        return CameraIntrinsics(
            w: depthWidth,
            h: depthHeight,
            flX: cameraIntrinsics[0, 0] * scaleX,
            flY: cameraIntrinsics[1, 1] * scaleY,
            cx: cameraIntrinsics[2, 0] * scaleX,
            cy: cameraIntrinsics[2, 1] * scaleY
        )
    }

    private func trackingStateText(_ state: ARCamera.TrackingState) -> String {
        switch state {
        case .normal:
            return "normal"
        case .notAvailable:
            return "not_available"
        case .limited(let reason):
            return "limited_\(reason)"
        }
    }

    private func writeJPEG(from pixelBuffer: CVPixelBuffer, to url: URL) throws {
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        guard let cgImage = ciContext.createCGImage(image, from: image.extent),
              let data = UIImage(cgImage: cgImage).jpegData(compressionQuality: 0.92) else {
            throw CocoaError(.fileWriteUnknown)
        }
        try data.write(to: url, options: .atomic)
    }

    private func writeDepth(_ pixelBuffer: CVPixelBuffer, to url: URL) throws {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let count = width * height
        guard let base = CVPixelBufferGetBaseAddress(pixelBuffer)?.assumingMemoryBound(to: Float32.self) else {
            throw CocoaError(.fileReadUnknown)
        }
        try NPYWriter.writeFloat32(Array(UnsafeBufferPointer(start: base, count: count)), shape: [height, width], to: url)
    }

    private func writeConfidence(_ pixelBuffer: CVPixelBuffer, to url: URL) throws {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let count = width * height
        guard let base = CVPixelBufferGetBaseAddress(pixelBuffer)?.assumingMemoryBound(to: UInt8.self) else {
            throw CocoaError(.fileReadUnknown)
        }
        try NPYWriter.writeUInt8(Array(UnsafeBufferPointer(start: base, count: count)), shape: [height, width], to: url)
    }

    private func measureValidDepthRatio(_ pixelBuffer: CVPixelBuffer) -> Double {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let count = width * height
        guard count > 0,
              let base = CVPixelBufferGetBaseAddress(pixelBuffer)?.assumingMemoryBound(to: Float32.self) else {
            return 0
        }
        var valid = 0
        for value in UnsafeBufferPointer(start: base, count: count) where value.isFinite && value > 0 {
            valid += 1
        }
        return Double(valid) / Double(count)
    }
}

extension CaptureController: CLLocationManagerDelegate {
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard isRecording, isGPSEnabled, let location = locations.last else { return }
        appendCSV("gps.csv", values: [
            String(format: "%.6f", location.timestamp.timeIntervalSince1970),
            String(format: "%.9f", location.coordinate.latitude),
            String(format: "%.9f", location.coordinate.longitude),
            String(format: "%.6f", location.altitude),
            String(format: "%.6f", location.horizontalAccuracy),
            String(format: "%.6f", location.verticalAccuracy),
            String(format: "%.6f", location.course),
            String(format: "%.6f", location.speed),
        ])
        gpsRows += 1
        gpsRate = recordRateSample(&gpsRateSamples, at: location.timestamp.timeIntervalSince1970)
    }
}

private extension simd_float4x4 {
    var rows: [[Float]] {
        [
            [columns.0.x, columns.1.x, columns.2.x, columns.3.x],
            [columns.0.y, columns.1.y, columns.2.y, columns.3.y],
            [columns.0.z, columns.1.z, columns.2.z, columns.3.z],
            [columns.0.w, columns.1.w, columns.2.w, columns.3.w],
        ]
    }
}

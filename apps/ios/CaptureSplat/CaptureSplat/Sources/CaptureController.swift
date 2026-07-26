import ARKit
import AVFoundation
import CoreImage
import CoreLocation
import CoreMotion
import Foundation
import OSLog
import RoomPlan
import UIKit

enum CapturePackageState: String {
    case idle
    case recording
    case finalizing
    case ready
    case partial
}

struct CaptureIntentOption: Identifiable {
    let id: String
    let title: String
    let shortTitle: String
    let detail: String
    let guidance: String
    let systemImage: String
    let requiresSubjectLock: Bool
}

struct ScanGuidancePoint: Identifiable {
    let id: Int
    let normalizedX: Double
    let normalizedY: Double
    let depthMeters: Double
}

private struct YCbCrSampler {
    let width: Int
    let height: Int
    let lumaBytesPerRow: Int
    let chromaWidth: Int
    let chromaHeight: Int
    let chromaBytesPerRow: Int
    let luma: UnsafePointer<UInt8>
    let chroma: UnsafePointer<UInt8>

    func colorAt(normalizedX: Double, normalizedY: Double) -> (UInt8, UInt8, UInt8) {
        let x = min(max(Int((normalizedX * Double(width - 1)).rounded()), 0), width - 1)
        let y = min(max(Int((normalizedY * Double(height - 1)).rounded()), 0), height - 1)
        let chromaX = min(max(x / 2, 0), chromaWidth - 1)
        let chromaY = min(max(y / 2, 0), chromaHeight - 1)
        let yValue = Float(luma[y * lumaBytesPerRow + x])
        let chromaOffset = chromaY * chromaBytesPerRow + chromaX * 2
        let cb = Float(chroma[chromaOffset]) - 128
        let cr = Float(chroma[chromaOffset + 1]) - 128
        return (
            channel(yValue + 1.402 * cr),
            channel(yValue - 0.3441 * cb - 0.7141 * cr),
            channel(yValue + 1.772 * cb)
        )
    }

    private func channel(_ value: Float) -> UInt8 {
        UInt8(min(max(Int(value.rounded()), 0), 255))
    }
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
    let clippedHighlightFraction: Double
    let nearClippedHighlightFraction: Double
    let clippedShadowFraction: Double
    let featureGridCoverage: Double
    let parallaxMeters: Double
    let angularVelocityDegPerSec: Double
    let translationSpeedMetersPerSec: Double
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
        self.clippedHighlightFraction = frameQuality?.clippedHighlightFraction ?? 0
        self.nearClippedHighlightFraction = frameQuality?.nearClippedHighlightFraction ?? 0
        self.clippedShadowFraction = frameQuality?.clippedShadowFraction ?? 0
        self.featureGridCoverage = frameQuality?.featureGridCoverage ?? 0
        self.parallaxMeters = frameQuality?.parallaxMeters ?? 0
        self.angularVelocityDegPerSec = frameQuality?.angularVelocityDegPerSec ?? 0
        self.translationSpeedMetersPerSec = frameQuality?.translationSpeedMetersPerSec ?? 0
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
    let clippedHighlightFraction: Double
    let nearClippedHighlightFraction: Double
    let clippedShadowFraction: Double
    let featureGridCoverage: Double
    let parallaxMeters: Double
    let angularVelocityDegPerSec: Double
    let translationSpeedMetersPerSec: Double
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

private struct TargetCandidateObservation {
    let timestamp: TimeInterval
    let worldPosition: SIMD3<Float>
    let distanceMeters: Float
}

private struct PersonMaskSnapshot {
    let bytes: Data
    let width: Int
    let height: Int
    let bytesPerRow: Int
    let personFraction: Double
}

private struct MeshExportResult {
    let plyWritten: Bool
    let status: String
    let error: String?
}

private struct MeshAnchorExportPlan {
    let anchor: ARMeshAnchor
    let sourceVertexCount: Int
    let sourceTriangleCount: Int
    let spatialCell: SIMD3<Int32>
    var triangleQuota = 0
    var nextTriangle = 0
    var exportedTriangleCount = 0
}

final class CaptureController: NSObject, ObservableObject {
    private let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "capture_splat",
        category: "arkit-session"
    )
    @Published var isRecording = false
    @Published var isStarting = false
    @Published var isFinalizing = false
    @Published private(set) var capturePackageState: CapturePackageState = .idle
    @Published var statusText = "Ready"
    @Published private(set) var captureCompletionNotice: String?
    @Published var rgbFrames = 0
    @Published var depthFrames = 0
    @Published var imuRows = 0
    @Published var gpsRows = 0
    @Published var currentSessionDirectory: URL?
    @Published var isRGBEnabled = true
    @Published var isDepthEnabled = true
    @Published var isConfidenceEnabled = true
    @Published var isIMUEnabled = true
    @Published var isGPSEnabled = false
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
    @Published var captureIntent = "scene_cluster"
    @Published var hapticAcceptedCount = 0
    @Published var currentCoverageSector = 0
    @Published var targetCoverageSector = 0
    @Published var coverageNavigationText = "Target sector will appear while scanning."
    @Published var scanTargetMode = "video_3dgs"
    @Published var isCaptureLockEnabled = true
    private let videoRecorder = CaptureVideoRecorder()
    private var planeAnchors: [UUID: ARPlaneAnchor] = [:]
    private var meshAnchors: [UUID: ARMeshAnchor] = [:]
    private var recordedMeshAnchorIDs: Set<UUID> = []
    @Published var isObjectTargetLocked = false
    @Published var isSubjectTargetReady = false
    @Published var isRoomTargetLocked = false
    @Published var targetLockStatus = "Lock object before recording"
    @Published var targetLockDetail = "Center the object, then tap Lock Subject."
    @Published var targetLockDistanceText = "--"
    @Published var isObjectMaskEnabled = true
    @Published var isObjectExtentLocked = false
    @Published var objectExtentStatus = "Lock extent after object"
    @Published var objectExtentDetail = "Use LiDAR depth near the reticle to propose foreground bounds."
    @Published var objectExtentOverlay: ObjectExtentOverlay?
    @Published var objectExtentSizeText = "--"
    @Published var pointCloudPreviewPointCount = 0
    @Published var pointCloudPreviewFile: URL?
    @Published var roomPlanStatus = "Room plan waiting"
    @Published var roomPlanDetail = "Use Room Plan to inspect walls and layout before Mac validation."
    @Published var roomPlanSummaryText = "No RoomPlan export"
    @Published var roomPlanFile: URL?
    @Published var roomPlanReportFile: URL?
    @Published var roomPlanSemanticsFile: URL?
    @Published var isSpatialGuidanceVisible = true
    @Published private(set) var spatialGuidanceMode = "pose_only"
    @Published private(set) var spatialGuidanceStatus = "RGB tracking"
    @Published private(set) var spatialGuidanceFaceBudget = 0
    @Published private(set) var spatialGuidanceUpdateHz = 0.0
    @Published private(set) var spatialGuidanceShowsMesh = false
    @Published private(set) var spatialGuidanceCells: [SpatialGuidancePoint] = []
    @Published private(set) var spatialGuidancePath: [SpatialGuidancePathPoint] = []
    @Published private(set) var spatialGuidancePose: SpatialGuidancePose?

    var spatialGuidanceThermalNotice: String? {
        guard isRecording,
              isSpatialGuidanceVisible,
              !spatialGuidanceShowsMesh,
              spatialGuidanceMode == "lidar_mesh" || spatialGuidanceMode == "roomplan_shared" else {
            return nil
        }
        switch thermalStateText {
        case "serious":
            return "Mesh hidden to cool the phone. Capture continues."
        case "critical":
            return "Live surface guidance paused. Recording continues."
        default:
            return nil
        }
    }

    static let captureIntentOptions: [CaptureIntentOption] = [
        CaptureIntentOption(
            id: "scene_cluster",
            title: "Desk / Cluster",
            shortTitle: "Desk",
            detail: "For a work desk, shelf, bed/table area, or kitchen counter. Stay close, use side steps, and cover the cluster from 2-3 heights.",
            guidance: "Circle the desk cluster slowly. Add side angles, top-down angles, and close texture passes.",
            systemImage: "rectangle.3.group",
            requiresSubjectLock: false
        ),
        CaptureIntentOption(
            id: "room_walkthrough",
            title: "Room Walkthrough",
            shortTitle: "Room",
            detail: "For bedrooms, offices, and halls. Walk a connected perimeter, add cross-room passes, and revisit corners/doorways.",
            guidance: "Walk the room perimeter slowly. Keep corners, doors, and furniture edges overlapping.",
            systemImage: "door.left.hand.open",
            requiresSubjectLock: false
        ),
        CaptureIntentOption(
            id: "object_orbit",
            title: "Object Orbit",
            shortTitle: "Object",
            detail: "For a single object. Orbit around it at low, middle, and high angles with the subject centered.",
            guidance: "Orbit the object at 2-3 heights. Keep the object centered and avoid fast pans.",
            systemImage: "cube.transparent",
            requiresSubjectLock: true
        ),
        CaptureIntentOption(
            id: "corridor_passage",
            title: "Corridor / Passage",
            shortTitle: "Corridor",
            detail: "For hallways and narrow paths. Move forward with side glances and return along the path if possible.",
            guidance: "Move forward slowly, add side glances, and return along the path for overlap.",
            systemImage: "arrow.forward.to.line",
            requiresSubjectLock: false
        ),
        CaptureIntentOption(
            id: "facade_wall",
            title: "Wall / Facade",
            shortTitle: "Wall",
            detail: "For flat walls, posters, cabinets, doors, and windows. Use sideways sweeps plus oblique angles.",
            guidance: "Side-step along the wall. Add oblique angles; do not just stand still and pan.",
            systemImage: "rectangle.split.3x1",
            requiresSubjectLock: false
        ),
        CaptureIntentOption(
            id: "outdoor_object",
            title: "Outdoor Object",
            shortTitle: "Outdoor",
            detail: "For gardens, vehicles, planters, and statues. Orbit the subject and watch sunlight, wind, and moving shadows.",
            guidance: "Orbit slowly and add wider establishing views. Avoid moving leaves and harsh exposure jumps.",
            systemImage: "sun.max",
            requiresSubjectLock: false
        ),
        CaptureIntentOption(
            id: "full_room_semantic",
            title: "RoomPlan + 3DGS",
            shortTitle: "Semantic",
            detail: "For VR room review. Capture appearance with Video 3DGS and export RoomPlan for walls, doors, windows, and furniture proposals.",
            guidance: "Capture the room, then run RoomPlan so layout semantics travel with the 3DGS evidence.",
            systemImage: "map",
            requiresSubjectLock: false
        ),
        CaptureIntentOption(
            id: "detail_repair",
            title: "Detail Repair",
            shortTitle: "Repair",
            detail: "For a weak corner, shiny table, window, shelf, bed edge, or dark area. Make a short focused follow-up pass.",
            guidance: "Focus on the weak area. Move slowly with close, overlapping side steps.",
            systemImage: "wrench.and.screwdriver",
            requiresSubjectLock: false
        ),
    ]

    private let motion = CMMotionManager()
    private let location = CLLocationManager()
    private let ciContext = CIContext()
    private let acceptedHaptic = UIImpactFeedbackGenerator(style: .light)
    private let completionHaptic = UINotificationFeedbackGenerator()
    private let writeQueue = DispatchQueue(label: "capture-splat.writer")
    private let maskWriteQueue = DispatchQueue(label: "capture-splat.person-mask-writer")
    private var csvHandles: [String: FileHandle] = [:]
    private var frames: [CapturedFrame] = []
    private var session: ARSession?
    private var lastFrameTimestamp: TimeInterval = 0
    private var activeIntrinsics: CameraIntrinsics?
    private var activeResolution = Resolution(w: 0, h: 0)
    private var firstFrameTimestamp: TimeInterval?
    private let minimumKeyframeInterval: TimeInterval = 0.5
    private let videoMinimumKeyframeInterval: TimeInterval = 0.2
    private let maxCapturedFrames = 120
    private let maxVideoCapturedFrames = 360
    private var lastScheduledFrameTimestamp: TimeInterval = -.infinity
    private var lastCandidateFrameTimestamp: TimeInterval = -.infinity
    private var lastAcceptedSectorIndex: Int?
    private var lastAcceptedCameraPosition: SIMD3<Float>?
    private var scheduledFrameCount = 0
    private var isWritingFrame = false
    private var automaticStopReason: String?
    private var isAutomaticStopScheduled = false
    private var rgbRateSamples: [TimeInterval] = []
    private var depthRateSamples: [TimeInterval] = []
    private var imuRateSamples: [TimeInterval] = []
    private var gpsRateSamples: [TimeInterval] = []
    private var healthTimer: Timer?
    private var lastStorageRefreshUptime: TimeInterval = -.infinity
    private var lastLiveGuidanceTimestamp: TimeInterval = -.infinity
    private var accumulatedIMURows = 0
    private var lastIMUUIUpdateTimestamp: TimeInterval = -.infinity
    private var coverageSectorCounts = Array(repeating: 0, count: 12)
    private var targetElevationBandCounts = Array(repeating: 0, count: 3)
    private var targetDistanceBandCounts = Array(repeating: 0, count: 3)
    private let coverageObservationTarget = 5
    private let maxCoverageHistorySamples = 720
    private var coverageHistory: [[String: Any]] = []
    private var coverageHistoryWasTruncated = false
    private let maxKeyframeEventSamples = 720
    private var keyframeEvents: [[String: Any]] = []
    private var keyframeEventsWereTruncated = false
    private var keyframeSkipReasonCounts: [String: Int] = [:]
    private let minBlurScore = 0.006
    private let maxAngularVelocityDegPerSec = 40.0
    private let maxTranslationSpeedMetersPerSec = 0.8
    private let minExposureMean = 0.08
    private let maxExposureMean = 0.82
    private let maxExposureJump = 0.28
    private let maxClippedHighlightFraction = 0.25
    private let maxNearClippedHighlightFraction = 0.50
    private let maxClippedShadowFraction = 0.30
    private let minFeatureGridCoverage = 0.25
    private let minObjectParallaxMeters = 0.08
    private let minRoomParallaxMeters = 0.12
    private let minVideoParallaxMeters = 0.05
    private let keyframeScoreThreshold = 0.72
    private let videoKeyframeScoreThreshold = 0.68
    private let maxRoomConnectedStepMeters = 0.85
    private let maxRoomConnectedSectorJump = 2
    private let minRoomOverlapScore = 0.45
    private let minRoomFeatureCount = 80
    private var lastAcceptedExposureMean: Double?
    private var lastMotionSampleTransform: simd_float4x4?
    private var lastMotionSampleTimestamp: TimeInterval?
    private var smoothedAngularVelocityDegPerSec = 0.0
    private var smoothedTranslationSpeedMetersPerSec = 0.0
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
    private var pointCloudPreviewSamples: [[String: Any]] = []
    private let maxPointCloudPreviewSamples = 6000
    private let pointCloudPreviewCheckpointInterval = 20
    private var latestCameraTransform: simd_float4x4?
    private var latestTargetCandidateWorldPosition: SIMD3<Float>?
    private var latestTargetCandidateDistance: Float?
    private var targetCandidateObservations: [TargetCandidateObservation] = []
    private var lockedObjectWorldPosition: SIMD3<Float>?
    private var lockedObjectDistanceMeters: Float?
    private var targetLockTimestamp: TimeInterval?
    private var targetLockAcquisition = "none"
    private var targetLockSampleCount = 0
    private var targetLockDepthSpreadMeters: Float?
    private var lastPersonMaskSampleTimestamp: TimeInterval = -.infinity
    private var personMaskScheduledCount = 0
    private var personMaskWrittenCount = 0
    private var personMaskDroppedCount = 0
    private var isWritingPersonMask = false
    private var personMaskRecords: [[String: Any]] = []
    private let personMaskMinimumInterval: TimeInterval = 0.2
    private let maxPersonMasks = 900
    private var sessionEvents: [[String: Any]] = []
    private let maxSessionEvents = 2000
    private var lastRecordedTrackingState: String?
    private var lastRecordedThermalState: String?
    private var arkitMeshAvailable = false
    private var lockedRoomWorldTransform: simd_float4x4?
    private var latestObjectExtentProposal: ObjectExtentProposal?
    private var lockedObjectExtentProposal: ObjectExtentProposal?
    private var spatialAnchorCells: [UUID: SpatialGuidancePoint] = [:]
    private var spatialPlaneCells: [UUID: SpatialGuidancePoint] = [:]
    private var spatialCellObservationCounts: [String: Int] = [:]
    private var spatialPathPointID = 0
    private var lastSpatialPathPosition: SIMD2<Float>?
    private var lastSpatialPoseTimestamp: TimeInterval = -.infinity
    private var spatialGuidanceUpdateDurationsMs: [Double] = []
    private var spatialGuidanceReceivedUpdateCount = 0
    private var spatialGuidanceUpdateCount = 0
    private var spatialGuidanceDroppedUpdateCount = 0
    private var spatialGuidanceThrottledUpdateCount = 0
    private var spatialGuidancePolicyDisabledUpdateCount = 0
    private var spatialGuidanceOverBudgetProcessingCount = 0
    private var lastSpatialAnchorUpdateTimestamp: [UUID: TimeInterval] = [:]
    private var spatialGuidanceThermalTransitions: [[String: Any]] = []
    private var spatialGuidanceCaptureStartUptime: TimeInterval?
    private var spatialGuidanceCaptureEndUptime: TimeInterval?
    private var spatialGuidancePolicyStartUptime: TimeInterval?
    private var spatialGuidanceThermalStartUptime: TimeInterval?
    private var spatialGuidanceRenderStateStartUptime: TimeInterval?
    private var spatialGuidancePolicyDurationsSeconds: [String: Double] = [:]
    private var spatialGuidanceThermalDurationsSeconds: [String: Double] = [:]
    private var spatialGuidanceRenderStateDurationsSeconds: [String: Double] = [:]
    private var currentSpatialGuidanceThermalState: String?
    private var currentSpatialGuidanceRenderState: String?
    private var lastSpatialGuidanceMeshPauseReason = "not_started"
    private var lastSpatialGuidancePolicy = ""
    private let spatialGuidanceCellSizeMeters: Float = 0.5
    private let maxSpatialGuidancePathPoints = 240
    private let maxSpatialGuidanceDurationSamples = 600
    private let spatialGuidanceUpdateBudgetMs = 12.0
    private var sharedRoomCaptureSession: RoomCaptureSession?
    private var sharedRoomPlanGeneration: UUID?
    private var sharedRoomPlanDirectory: URL?
    private var sharedRoomPlanStopCompletion: (() -> Void)?
    private var sharedRoomPlanTimeoutWorkItem: DispatchWorkItem?

    deinit {
        healthTimer?.invalidate()
    }

    func prepareSensors() {
        UIDevice.current.isBatteryMonitoringEnabled = true
        location.delegate = self
        location.desiredAccuracy = kCLLocationAccuracyBest

        if motion.isDeviceMotionAvailable {
            motion.deviceMotionUpdateInterval = 0.02
        }
        refreshHealth()
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            self?.refreshHealth()
        }
    }

    func attach(session: ARSession) {
        self.session = session
        session.delegate = self
        session.delegateQueue = .main
        refreshSpatialGuidanceCapability()
        logger.info("Attached configured AR session on the main delegate queue")
    }

    var showsObjectExtentGuidance: Bool {
        requiresSubjectTarget && isObjectMaskEnabled && objectExtentOverlay != nil
    }

    func setSpatialGuidanceVisible(_ visible: Bool) {
        isSpatialGuidanceVisible = visible
        refreshSpatialGuidanceCapability()
    }

    private func refreshSpatialGuidanceCapability() {
        let supportsMesh = ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
        let supportsDepth = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
        let supportsPlanes = session != nil
        if captureIntent == "full_room_semantic", RoomCaptureSession.isSupported, sharedRoomCaptureSession != nil {
            spatialGuidanceMode = "roomplan_shared"
            spatialGuidanceStatus = "Room semantics"
        } else if supportsMesh {
            spatialGuidanceMode = "lidar_mesh"
            spatialGuidanceStatus = "LiDAR mesh"
        } else if supportsDepth {
            spatialGuidanceMode = "depth_points"
            spatialGuidanceStatus = "Depth points"
        } else if supportsPlanes {
            spatialGuidanceMode = "planes_and_features"
            spatialGuidanceStatus = "Planes"
        } else {
            spatialGuidanceMode = "pose_only"
            spatialGuidanceStatus = "RGB tracking"
        }
        applySpatialGuidanceThermalPolicy(ProcessInfo.processInfo.thermalState)
    }

    func setScanTargetMode(_ mode: String) {
        scanTargetMode = mode
        updateCaptureProfileText()
        refreshTargetLockStatus()
        updateGuidance()
    }

    func setCaptureIntent(_ intent: String) {
        guard Self.captureIntentOptions.contains(where: { $0.id == intent }) else { return }
        if captureIntent != intent {
            captureCompletionNotice = nil
            if scanTargetMode == "video_3dgs" {
                clearTargetLock()
            }
        }
        captureIntent = intent
        isGPSEnabled = intent == "outdoor_object"
        updateCaptureProfileText()
        updateGuidance()
        refreshSpatialGuidanceCapability()
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
        applyObjectTargetLock(
            position: position,
            distance: distance,
            acquisition: "manual_latest_depth",
            sampleCount: 1,
            depthSpread: nil,
            timestamp: targetCandidateObservations.last?.timestamp
        )
    }

    @discardableResult
    func lockSubjectTargetIfStable() -> Bool {
        guard requiresSubjectTarget else {
            targetLockStatus = "Subject lock unavailable"
            targetLockDetail = "Subject locking is only used for Object Orbit."
            return false
        }
        let recent = targetCandidateObservations
        guard recent.count >= 3 else {
            targetLockStatus = "Center subject"
            targetLockDetail = "Hold still until three stable LiDAR depth samples are ready."
            return false
        }
        let distances = recent.map(\.distanceMeters)
        guard let minDistance = distances.min(), let maxDistance = distances.max() else { return false }
        let meanDistance = distances.reduce(0, +) / Float(distances.count)
        let spread = maxDistance - minDistance
        let allowedSpread = max(0.10, meanDistance * 0.10)
        guard meanDistance.isFinite, meanDistance > 0, spread <= allowedSpread else {
            targetLockStatus = "Target depth unstable"
            targetLockDetail = "Hold still with the subject centered before recording."
            return false
        }
        let meanPosition = recent.reduce(SIMD3<Float>.zero) { $0 + $1.worldPosition } / Float(recent.count)
        applyObjectTargetLock(
            position: meanPosition,
            distance: meanDistance,
            acquisition: "manual_stable_center_depth",
            sampleCount: recent.count,
            depthSpread: spread,
            timestamp: recent.last?.timestamp
        )
        return true
    }

    private func applyObjectTargetLock(
        position: SIMD3<Float>,
        distance: Float,
        acquisition: String,
        sampleCount: Int,
        depthSpread: Float?,
        timestamp: TimeInterval?
    ) {
        lockedObjectWorldPosition = position
        lockedObjectDistanceMeters = distance
        targetLockTimestamp = timestamp
        targetLockAcquisition = acquisition
        targetLockSampleCount = sampleCount
        targetLockDepthSpreadMeters = depthSpread
        isObjectTargetLocked = true
        isObjectExtentLocked = false
        lockedObjectExtentProposal = nil
        targetLockStatus = scanTargetMode == "video_3dgs" ? "Subject locked" : "Object locked"
        targetLockDetail = "Orbit around the locked subject center."
        targetLockDistanceText = String(format: "%.2f m", distance)
        if isObjectMaskEnabled, let proposal = latestObjectExtentProposal {
            lockedObjectExtentProposal = proposal
            isObjectExtentLocked = true
            objectExtentOverlay = proposal.overlay
            objectExtentSizeText = String(
                format: "%.2fm x %.2fm",
                proposal.approximateWidthMeters,
                proposal.approximateHeightMeters
            )
        }
        if isRecording {
            appendSessionEvent("target_locked", arTimestamp: timestamp, details: [
                "acquisition": acquisition,
                "sample_count": sampleCount,
            ])
        }
        refreshObjectExtentStatus()
        updateGuidance()
    }

    func lockObjectExtent() {
        guard isObjectTargetLocked else {
            objectExtentStatus = "Lock object first"
            objectExtentDetail = "Center the subject and tap Lock Subject before confirming extent."
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
        lockedObjectDistanceMeters = nil
        targetLockTimestamp = nil
        targetLockAcquisition = "none"
        targetLockSampleCount = 0
        targetLockDepthSpreadMeters = nil
        targetCandidateObservations.removeAll()
        isSubjectTargetReady = false
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
        guard !isRecording, !isStarting, !isFinalizing else { return }
        captureCompletionNotice = nil
        if requiresSubjectTarget, !isObjectTargetLocked {
            statusText = "Lock the subject before recording."
            return
        }
        guard canStartForCurrentTargetMode else {
            statusText = targetLockStatus
            return
        }
        isStarting = true
        statusText = "Starting capture"
        let requestedAt = ProcessInfo.processInfo.systemUptime
        DispatchQueue.main.async { [weak self] in
            self?.beginRecording(requestedAt: requestedAt)
        }
    }

    private func beginRecording(requestedAt: TimeInterval) {
        guard isStarting, !isRecording, !isFinalizing else {
            isStarting = false
            return
        }
        defer { isStarting = false }
        captureCompletionNotice = nil
        automaticStopReason = nil
        isAutomaticStopScheduled = false
        completionHaptic.prepare()
        do {
            let directory = try makeSessionDirectory()
            try makeExportFolders(in: directory)
            currentSessionDirectory = directory
            videoRecorder.setTargetFPS(videoTargetFPS(for: ProcessInfo.processInfo.thermalState))
            try videoRecorder.start(in: directory)
            if isCaptureLockEnabled {
                applyCaptureLocks(true)
            }
            try writeCSVHeader("imu.csv", columns: [
                "timestamp", "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
                "quat_w", "quat_x", "quat_y", "quat_z",
            ])
            try writeCSVHeader("gps.csv", columns: [
                "timestamp", "latitude", "longitude", "altitude", "horizontal_accuracy", "vertical_accuracy",
                "course", "speed",
            ])
            frames.removeAll()
            personMaskRecords.removeAll()
            personMaskScheduledCount = 0
            personMaskWrittenCount = 0
            personMaskDroppedCount = 0
            lastPersonMaskSampleTimestamp = -.infinity
            isWritingPersonMask = false
            sessionEvents.removeAll()
            lastRecordedTrackingState = nil
            lastRecordedThermalState = nil
            arkitMeshAvailable = false
            recordedMeshAnchorIDs.removeAll()
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
            accumulatedIMURows = 0
            lastIMUUIUpdateTimestamp = -.infinity
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
            targetElevationBandCounts = Array(repeating: 0, count: targetElevationBandCounts.count)
            targetDistanceBandCounts = Array(repeating: 0, count: targetDistanceBandCounts.count)
            coverageHintText = "Coverage 0/\(coverageSectors.count)"
            readinessState = "Not ready"
            nextAction = scanTargetMode == "video_3dgs"
                ? "Move slowly with overlapping views"
                : "Move slowly around the subject"
            missingSectorCount = coverageSectors.count
            backgroundWarning = scanTargetMode == "video_3dgs"
                ? "Stay with the selected capture intent; room-wide headings are not required."
                : "Keep the subject centered; avoid sweeping large background planes."
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
            pointCloudPreviewSamples.removeAll()
            pointCloudPreviewPointCount = 0
            pointCloudPreviewFile = directory.appendingPathComponent("pointcloud_preview").appendingPathComponent("preview.json")
            roomPlanFile = nil
            roomPlanReportFile = nil
            roomPlanSemanticsFile = nil
            spatialGuidancePath.removeAll()
            spatialGuidancePose = nil
            spatialPathPointID = 0
            lastSpatialPathPosition = nil
            lastSpatialPoseTimestamp = -.infinity
            lastLiveGuidanceTimestamp = -.infinity
            spatialCellObservationCounts.removeAll()
            spatialGuidanceUpdateDurationsMs.removeAll()
            spatialGuidanceReceivedUpdateCount = 0
            spatialGuidanceUpdateCount = 0
            spatialGuidanceDroppedUpdateCount = 0
            spatialGuidanceThrottledUpdateCount = 0
            spatialGuidancePolicyDisabledUpdateCount = 0
            spatialGuidanceOverBudgetProcessingCount = 0
            lastSpatialAnchorUpdateTimestamp.removeAll()
            spatialGuidanceThermalTransitions.removeAll()
            spatialGuidancePolicyDurationsSeconds.removeAll()
            spatialGuidanceThermalDurationsSeconds.removeAll()
            spatialGuidanceRenderStateDurationsSeconds.removeAll()
            spatialGuidancePolicyStartUptime = nil
            spatialGuidanceThermalStartUptime = nil
            spatialGuidanceRenderStateStartUptime = nil
            currentSpatialGuidanceThermalState = nil
            currentSpatialGuidanceRenderState = nil
            lastSpatialGuidanceMeshPauseReason = "not_started"
            spatialGuidanceCaptureStartUptime = ProcessInfo.processInfo.systemUptime
            spatialGuidanceCaptureEndUptime = nil
            publishSpatialGuidanceCells()
            rgbRateSamples.removeAll()
            depthRateSamples.removeAll()
            imuRateSamples.removeAll()
            gpsRateSamples.removeAll()
            isRecording = true
            lastSpatialGuidancePolicy = ""
            applySpatialGuidanceThermalPolicy(ProcessInfo.processInfo.thermalState)
            capturePackageState = .recording
            statusText = "Recording"
            let startupLatency = ProcessInfo.processInfo.systemUptime - requestedAt
            appendSessionEvent("capture_started", details: [
                "capture_intent": captureIntent,
                "capture_profile": captureProfileLabel(),
                "startup_latency_seconds": startupLatency,
            ])
            logger.info("Capture recording started after \(startupLatency, privacy: .public) seconds")
            if isObjectTargetLocked {
                appendSessionEvent("target_locked", details: [
                    "acquisition": targetLockAcquisition,
                    "sample_count": targetLockSampleCount,
                ])
            }
            if !ARWorldTrackingConfiguration.supportsFrameSemantics(.personSegmentationWithDepth) {
                appendSessionEvent("sensor_fallback", details: ["sensor": "person_segmentation_with_depth"])
            }
            if !ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
                appendSessionEvent("sensor_fallback", details: ["sensor": "scene_reconstruction_mesh"])
            }
            guidanceText = scanTargetMode == "video_3dgs"
                ? currentCaptureIntentOption.guidance
                : "Move slowly around the subject. Favor side steps over panning."
            acceptedHaptic.prepare()
            if isIMUEnabled {
                startMotion()
            }
            if isGPSEnabled {
                location.requestWhenInUseAuthorization()
                location.startUpdatingLocation()
            }
            startSharedRoomPlanIfNeeded(in: directory)
        } catch {
            capturePackageState = currentSessionDirectory == nil ? .idle : .partial
            statusText = "Start failed: \(error.localizedDescription)"
        }
    }

    func stopRecording() {
        guard isRecording, !isFinalizing else { return }
        let stoppedAt = ProcessInfo.processInfo.systemUptime
        finishSpatialGuidanceSegments(at: stoppedAt)
        spatialGuidanceCaptureEndUptime = stoppedAt
        isRecording = false
        isFinalizing = true
        capturePackageState = .finalizing
        statusText = automaticStopReason == "frame_limit"
            ? "Useful-frame limit reached. Finalizing capture"
            : "Finalizing capture"
        appendSessionEvent("finalization_started", arTimestamp: lastFrameTimestamp)
        motion.stopDeviceMotionUpdates()
        imuRows = accumulatedIMURows
        location.stopUpdatingLocation()
        applyCaptureLocks(false)
        let meshSnapshot = recordedMeshAnchorIDs.compactMap { meshAnchors[$0] }
        stopSharedRoomPlanIfNeeded { [weak self] in
            guard let self else { return }
            self.videoRecorder.finish { [weak self] videoResult in
                guard let self else { return }
                self.writeQueue.async { [weak self] in
                    guard let self else { return }
                    self.closeCSVHandles()
                    let previewStatus = self.finalizePointCloudPreview()
                    let meshResult = self.writeARKitMesh(anchors: meshSnapshot)
                    self.maskWriteQueue.async { [weak self] in
                        DispatchQueue.main.async {
                            guard let self else { return }
                            self.pointCloudPreviewPointCount = previewStatus.count
                            self.pointCloudPreviewFile = previewStatus.url
                            self.completeCaptureFinalization(videoResult: videoResult, meshResult: meshResult)
                        }
                    }
                }
            }
        }
    }

    private func completeCaptureFinalization(
        videoResult: CaptureVideoRecorder.FinishResult,
        meshResult: MeshExportResult
    ) {
        arkitMeshAvailable = meshResult.plyWritten
        appendSessionEvent("arkit_mesh_export", details: [
            "status": meshResult.status,
            "ply_written": meshResult.plyWritten,
            "error": meshResult.error ?? NSNull(),
        ])
        writeMetadata()
        writeSessionSidecars()
        do {
            let directory = try writeCaptureManifest()
            appendSessionEvent("finalization_completed", details: [
                "video_status": videoResult.status,
                "mesh_status": meshResult.status,
                "stop_reason": automaticStopReason ?? "manual",
            ])
            writeSessionSidecars()
            writeFinalizationReport(
                status: videoResult.succeeded ? "finalized" : "finalized_with_video_error",
                videoStatus: videoResult.status,
                videoError: videoResult.error,
                manifestWritten: true,
                finalizationError: nil
            )
            statusText = videoResult.succeeded
                ? "Stopped and finalized \(directory.lastPathComponent)"
                : "Finalized with video warning: \(videoResult.error ?? videoResult.status)"
            captureCompletionNotice = videoResult.succeeded
                ? "Capture complete: \(acceptedKeyframes) useful frames saved."
                : "Capture saved with a video warning. Review the export before reconstruction."
            completionHaptic.notificationOccurred(videoResult.succeeded ? .success : .warning)
            capturePackageState = .ready
        } catch {
            appendSessionEvent("finalization_failed", details: ["error": error.localizedDescription])
            writeSessionSidecars()
            writeFinalizationReport(
                status: "failed",
                videoStatus: videoResult.status,
                videoError: videoResult.error,
                manifestWritten: false,
                finalizationError: error.localizedDescription
            )
            statusText = "Stopped. Finalize failed: \(error.localizedDescription)"
            captureCompletionNotice = "Capture stopped, but finalization needs attention."
            completionHaptic.notificationOccurred(.error)
            capturePackageState = .partial
        }
        isFinalizing = false
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
            captureProfile: captureProfileLabel(),
            captureIntent: captureIntent,
            depthMode: "sceneDepth",
            sessionConfig: makeSessionConfig(),
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
            spatialGuidanceReportFile: "metadata/spatial_guidance_report.json",
            personMaskIndexFile: personMaskWrittenCount > 0 ? "metadata/person_mask_index.jsonl" : nil,
            arkitMeshFile: arkitMeshAvailable ? "geometry/arkit_mesh.ply" : nil,
            videoFile: videoRecorder.appendedFrameCount > 0 ? CaptureVideoRecorder.videoRelativePath : nil,
            frameIndexFile: videoRecorder.appendedFrameCount > 0 ? CaptureVideoRecorder.frameIndexRelativePath : nil,
            videoFrameCount: videoRecorder.appendedFrameCount > 0 ? videoRecorder.appendedFrameCount : nil,
            roomPlanFile: roomPlanFile == nil ? nil : "room_plan/room.usdz",
            roomPlanReportFile: roomPlanReportFile == nil ? nil : "room_plan/room_plan_report.json",
            roomPlanSemanticsFile: roomPlanSemanticsFile == nil ? nil : "room_plan/room_semantics.json",
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
        for folder in ["rgb", "depth", "confidence", "pointcloud_preview", "room_plan", "metadata", "geometry", "masks/person"] {
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
        writeQueue.async { [weak self] in
            guard let self else { return }
            let handle: FileHandle
            if let cached = self.csvHandles[name] {
                handle = cached
            } else {
                guard let opened = try? FileHandle(
                    forWritingTo: directory.appendingPathComponent(name)
                ) else { return }
                _ = try? opened.seekToEnd()
                self.csvHandles[name] = opened
                handle = opened
            }
            if let data = (values.joined(separator: ",") + "\n").data(using: .utf8) {
                try? handle.write(contentsOf: data)
            }
        }
    }

    private func closeCSVHandles() {
        for handle in csvHandles.values {
            try? handle.close()
        }
        csvHandles.removeAll()
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
            self.accumulatedIMURows += 1
            if deviceMotion.timestamp - self.lastIMUUIUpdateTimestamp >= 0.5 {
                self.lastIMUUIUpdateTimestamp = deviceMotion.timestamp
                self.imuRows = self.accumulatedIMURows
                self.imuRate = self.recordRateSample(&self.imuRateSamples, at: deviceMotion.timestamp)
            }
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
            "person_masks_written": personMaskWrittenCount,
            "person_masks_dropped": personMaskDroppedCount,
            "arkit_mesh_available": arkitMeshAvailable,
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
        writeJSON(planesReport(), to: metadata.appendingPathComponent("planes.json"))
        writeJSON(coverageReport(), to: metadata.appendingPathComponent("coverage_report.json"))
        writeJSON(keyframeReport(), to: metadata.appendingPathComponent("keyframe_report.json"))
        writeJSON(roomCaptureQualityReport(), to: metadata.appendingPathComponent("room_capture_quality_report.json"))
        writeJSON(captureProfileReport(), to: metadata.appendingPathComponent("capture_profile_report.json"))
        writeJSON(targetLockReport(), to: metadata.appendingPathComponent("target_lock_report.json"))
        writeJSON(objectExtentReport(), to: metadata.appendingPathComponent("object_extent_report.json"))
        writeJSON(objectMatteReport(), to: metadata.appendingPathComponent("object_matte_report.json"))
        writeJSON(capturePolicyReport(), to: metadata.appendingPathComponent("capture_policy.json"))
        writeJSON(sensorCapabilitiesReport(), to: metadata.appendingPathComponent("sensor_capabilities.json"))
        writeJSON(spatialGuidanceReport(), to: metadata.appendingPathComponent("spatial_guidance_report.json"))
    }

    private func writeFinalizationReport(
        status: String,
        videoStatus: String,
        videoError: String?,
        manifestWritten: Bool,
        finalizationError: String?
    ) {
        guard let directory = currentSessionDirectory else { return }
        var report: [String: Any] = [
            "schema": "capture_splat.finalization_report.v0.1",
            "status": status,
            "video_writer_status": videoStatus,
            "video_frame_count": videoRecorder.appendedFrameCount,
            "video_dropped_frame_count": videoRecorder.droppedFrameCount,
            "video_target_fps_at_finalize": videoRecorder.targetFPS,
            "video_sampling_policy": "thermal_adaptive_15_10_6",
            "accepted_keyframe_count": frames.count,
            "person_mask_written_count": personMaskWrittenCount,
            "person_mask_dropped_count": personMaskDroppedCount,
            "arkit_mesh_written": arkitMeshAvailable,
            "manifest_written": manifestWritten,
            "partial_artifacts_preserved": true,
        ]
        if let videoError { report["video_error"] = videoError }
        if let finalizationError { report["finalization_error"] = finalizationError }
        writeJSON(report, to: directory.appendingPathComponent("metadata/finalization_report.json"))
    }

    private func writeJSON(_ object: [String: Any], to url: URL) {
        if JSONSerialization.isValidJSONObject(object),
           let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url, options: .atomic)
        }
    }

    private func writeJSONLines(_ records: [[String: Any]], to url: URL) {
        var output = Data()
        for record in records where JSONSerialization.isValidJSONObject(record) {
            guard let data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]) else { continue }
            output.append(data)
            output.append(Data("\n".utf8))
        }
        try? output.write(to: url, options: .atomic)
    }

    private func appendSessionEvent(
        _ event: String,
        arTimestamp: TimeInterval? = nil,
        details: [String: Any] = [:]
    ) {
        if sessionEvents.count >= maxSessionEvents {
            sessionEvents.removeFirst(sessionEvents.count - maxSessionEvents + 1)
        }
        var record = details
        record["event"] = event
        record["wall_time_unix"] = Date().timeIntervalSince1970
        if let arTimestamp { record["ar_timestamp"] = arTimestamp }
        sessionEvents.append(record)
    }

    private func writeSessionSidecars() {
        guard let directory = currentSessionDirectory else { return }
        let metadata = directory.appendingPathComponent("metadata", isDirectory: true)
        writeJSONLines(sessionEvents, to: metadata.appendingPathComponent("session_events.jsonl"))
        writeJSONLines(personMaskRecords, to: metadata.appendingPathComponent("person_mask_index.jsonl"))
    }

    private func writeARKitMesh(anchors: [ARMeshAnchor]) -> MeshExportResult {
        guard let directory = currentSessionDirectory else {
            return MeshExportResult(plyWritten: false, status: "missing_capture_directory", error: nil)
        }
        let reportURL = directory.appendingPathComponent("geometry/arkit_mesh_report.json")
        let plyURL = directory.appendingPathComponent("geometry/arkit_mesh.ply")
        let maxVertices = 200_000
        let maxTriangles = 300_000
        let spatialCellSizeMeters: Float = 0.5
        var vertices: [SIMD3<Float>] = []
        var faces: [(UInt32, UInt32, UInt32, UInt8)] = []
        var classificationCounts: [String: Int] = [:]
        var sourceClassificationCounts: [String: Int] = [:]
        var nonFiniteVertexCount = 0
        var invalidFaceCount = 0
        var degenerateFaceCount = 0
        var vertexBudgetSkippedTriangleCount = 0
        var plans: [MeshAnchorExportPlan] = []

        for anchor in anchors {
            let geometry = anchor.geometry
            let elements = geometry.faces
            guard elements.primitiveType == .triangle,
                  elements.indexCountPerPrimitive == 3,
                  elements.count > 0 else { continue }
            for localIndex in 0..<geometry.vertices.count {
                if meshWorldVertex(geometry.vertices, at: localIndex, transform: anchor.transform) == nil {
                    nonFiniteVertexCount += 1
                }
            }
            for faceIndex in 0..<elements.count {
                let classification = meshClassification(geometry.classification, faceIndex: faceIndex)
                sourceClassificationCounts[meshClassificationLabel(classification), default: 0] += 1
            }
            let origin = anchor.transform.columns.3
            guard origin.x.isFinite, origin.y.isFinite, origin.z.isFinite else { continue }
            plans.append(MeshAnchorExportPlan(
                anchor: anchor,
                sourceVertexCount: geometry.vertices.count,
                sourceTriangleCount: elements.count,
                spatialCell: SIMD3<Int32>(
                    Int32(floor(origin.x / spatialCellSizeMeters)),
                    Int32(floor(origin.y / spatialCellSizeMeters)),
                    Int32(floor(origin.z / spatialCellSizeMeters))
                )
            ))
        }

        plans.sort { lhs, rhs in
            if lhs.spatialCell.x != rhs.spatialCell.x { return lhs.spatialCell.x < rhs.spatialCell.x }
            if lhs.spatialCell.y != rhs.spatialCell.y { return lhs.spatialCell.y < rhs.spatialCell.y }
            if lhs.spatialCell.z != rhs.spatialCell.z { return lhs.spatialCell.z < rhs.spatialCell.z }
            return lhs.anchor.identifier.uuidString < rhs.anchor.identifier.uuidString
        }
        let sourceVertexCount = plans.reduce(0) { $0 + $1.sourceVertexCount }
        let sourceTriangleCount = plans.reduce(0) { $0 + $1.sourceTriangleCount }
        let quotas = meshTriangleQuotas(
            counts: plans.map(\.sourceTriangleCount),
            budget: maxTriangles
        )
        for index in plans.indices {
            plans[index].triangleQuota = quotas[index]
        }

        var localToGlobal = plans.map {
            Array(repeating: Int32(-1), count: $0.sourceVertexCount)
        }
        var activePlans = plans.indices.filter { plans[$0].triangleQuota > 0 }
        while !activePlans.isEmpty, faces.count < maxTriangles {
            var nextActivePlans: [Int] = []
            for planIndex in activePlans {
                let plan = plans[planIndex]
                let faceIndex = meshSampleIndex(
                    ordinal: plan.nextTriangle,
                    sourceCount: plan.sourceTriangleCount,
                    sampleCount: plan.triangleQuota
                )
                plans[planIndex].nextTriangle += 1
                let geometry = plan.anchor.geometry
                let elements = geometry.faces
                let baseIndex = faceIndex * elements.indexCountPerPrimitive
                guard let localA = meshIndex(elements, at: baseIndex),
                      let localB = meshIndex(elements, at: baseIndex + 1),
                      let localC = meshIndex(elements, at: baseIndex + 2),
                      localA != localB, localA != localC, localB != localC,
                      Int(localA) < plan.sourceVertexCount,
                      Int(localB) < plan.sourceVertexCount,
                      Int(localC) < plan.sourceVertexCount else {
                    invalidFaceCount += 1
                    if plans[planIndex].nextTriangle < plan.triangleQuota {
                        nextActivePlans.append(planIndex)
                    }
                    continue
                }
                guard let worldA = meshWorldVertex(geometry.vertices, at: Int(localA), transform: plan.anchor.transform),
                      let worldB = meshWorldVertex(geometry.vertices, at: Int(localB), transform: plan.anchor.transform),
                      let worldC = meshWorldVertex(geometry.vertices, at: Int(localC), transform: plan.anchor.transform) else {
                    invalidFaceCount += 1
                    if plans[planIndex].nextTriangle < plan.triangleQuota {
                        nextActivePlans.append(planIndex)
                    }
                    continue
                }
                let areaSquared = simd_length_squared(simd_cross(worldB - worldA, worldC - worldA))
                guard areaSquared.isFinite, areaSquared > 1e-12 else {
                    degenerateFaceCount += 1
                    if plans[planIndex].nextTriangle < plan.triangleQuota {
                        nextActivePlans.append(planIndex)
                    }
                    continue
                }

                let localIndices = [Int(localA), Int(localB), Int(localC)]
                let worldVertices = [worldA, worldB, worldC]
                let newVertexCount = localIndices.reduce(0) {
                    $0 + (localToGlobal[planIndex][$1] < 0 ? 1 : 0)
                }
                guard vertices.count + newVertexCount <= maxVertices else {
                    vertexBudgetSkippedTriangleCount += 1
                    if plans[planIndex].nextTriangle < plan.triangleQuota {
                        nextActivePlans.append(planIndex)
                    }
                    continue
                }
                for (localIndex, world) in zip(localIndices, worldVertices) {
                    if localToGlobal[planIndex][localIndex] < 0 {
                        localToGlobal[planIndex][localIndex] = Int32(vertices.count)
                        vertices.append(world)
                    }
                }
                let classification = meshClassification(geometry.classification, faceIndex: faceIndex)
                classificationCounts[meshClassificationLabel(classification), default: 0] += 1
                faces.append((
                    UInt32(localToGlobal[planIndex][Int(localA)]),
                    UInt32(localToGlobal[planIndex][Int(localB)]),
                    UInt32(localToGlobal[planIndex][Int(localC)]),
                    classification
                ))
                plans[planIndex].exportedTriangleCount += 1
                if plans[planIndex].nextTriangle < plan.triangleQuota {
                    nextActivePlans.append(planIndex)
                }
            }
            activePlans = nextActivePlans
        }

        let sourceSpatialCells = Set(plans.map(meshSpatialCellKey))
        let exportedPlans = plans.filter { $0.exportedTriangleCount > 0 }
        let exportedSpatialCells = Set(exportedPlans.map(meshSpatialCellKey))
        let anchorCoverageRatio = plans.isEmpty ? 0.0 : Double(exportedPlans.count) / Double(plans.count)
        let spatialCoverageRatio = sourceSpatialCells.isEmpty
            ? 0.0
            : Double(exportedSpatialCells.count) / Double(sourceSpatialCells.count)
        let coveragePreserving = !plans.isEmpty
            && exportedPlans.count == plans.count
            && exportedSpatialCells.count == sourceSpatialCells.count
        let triangleBudgetApplied = sourceTriangleCount > maxTriangles
        let vertexBudgetApplied = vertexBudgetSkippedTriangleCount > 0
        let budgetLimited = triangleBudgetApplied || vertexBudgetApplied
        let classificationCoverage = sourceClassificationCounts.reduce(into: [String: Double]()) { result, entry in
            result[entry.key] = entry.value > 0
                ? Double(classificationCounts[entry.key, default: 0]) / Double(entry.value)
                : 0.0
        }

        guard !vertices.isEmpty, !faces.isEmpty else {
            writeJSON([
                "schema": "capture_splat.arkit_mesh_report.v0.2",
                "status": anchors.isEmpty ? "no_mesh_anchors" : "no_finite_triangles",
                "anchor_count": anchors.count,
                "eligible_anchor_count": plans.count,
                "source_vertex_count": sourceVertexCount,
                "source_triangle_count": sourceTriangleCount,
                "vertex_count": vertices.count,
                "triangle_count": faces.count,
                "non_finite_vertex_count": nonFiniteVertexCount,
                "invalid_face_count": invalidFaceCount,
                "degenerate_face_count": degenerateFaceCount,
                "selection_policy": "anchor_spatial_stratified_even_faces_v1",
                "ply_written": false,
                "authority": meshAuthorityReport(),
            ], to: reportURL)
            return MeshExportResult(
                plyWritten: false,
                status: anchors.isEmpty ? "no_mesh_anchors" : "no_finite_triangles",
                error: nil
            )
        }

        var data = Data("""
        ply
        format binary_little_endian 1.0
        comment Capture Splat ARKit mesh sidecar; capture evidence only
        element vertex \(vertices.count)
        property float x
        property float y
        property float z
        element face \(faces.count)
        property list uchar uint vertex_indices
        property uchar classification
        end_header

        """.utf8)
        data.reserveCapacity(data.count + vertices.count * 12 + faces.count * 14)
        for vertex in vertices {
            appendLittleEndian(vertex.x, to: &data)
            appendLittleEndian(vertex.y, to: &data)
            appendLittleEndian(vertex.z, to: &data)
        }
        for face in faces {
            data.append(3)
            appendLittleEndian(face.0, to: &data)
            appendLittleEndian(face.1, to: &data)
            appendLittleEndian(face.2, to: &data)
            data.append(face.3)
        }

        do {
            try data.write(to: plyURL, options: .atomic)
            writeJSON([
                "schema": "capture_splat.arkit_mesh_report.v0.2",
                "status": "finite_mesh_written",
                "anchor_count": anchors.count,
                "eligible_anchor_count": plans.count,
                "exported_anchor_count": exportedPlans.count,
                "source_vertex_count": sourceVertexCount,
                "source_triangle_count": sourceTriangleCount,
                "vertex_count": vertices.count,
                "triangle_count": faces.count,
                "non_finite_vertex_count": nonFiniteVertexCount,
                "invalid_face_count": invalidFaceCount,
                "degenerate_face_count": degenerateFaceCount,
                "vertex_budget_skipped_triangle_count": vertexBudgetSkippedTriangleCount,
                "max_vertex_count": maxVertices,
                "max_triangle_count": maxTriangles,
                "budget_limited": budgetLimited,
                "triangle_budget_applied": triangleBudgetApplied,
                "vertex_budget_applied": vertexBudgetApplied,
                "truncated": budgetLimited,
                "selection_policy": "anchor_spatial_stratified_even_faces_v1",
                "coverage_preserving": coveragePreserving,
                "anchor_coverage_ratio": anchorCoverageRatio,
                "spatial_cell_size_meters": spatialCellSizeMeters,
                "source_spatial_cell_count": sourceSpatialCells.count,
                "exported_spatial_cell_count": exportedSpatialCells.count,
                "spatial_cell_coverage_ratio": spatialCoverageRatio,
                "source_classification_counts": sourceClassificationCounts,
                "classification_counts": classificationCounts,
                "classification_coverage": classificationCoverage,
                "ply_file": "geometry/arkit_mesh.ply",
                "ply_written": true,
                "authority": meshAuthorityReport(),
            ], to: reportURL)
            return MeshExportResult(plyWritten: true, status: "finite_mesh_written", error: nil)
        } catch {
            writeJSON([
                "schema": "capture_splat.arkit_mesh_report.v0.2",
                "status": "write_failed",
                "error": error.localizedDescription,
                "ply_written": false,
                "authority": meshAuthorityReport(),
            ], to: reportURL)
            return MeshExportResult(plyWritten: false, status: "write_failed", error: error.localizedDescription)
        }
    }

    private func meshTriangleQuotas(counts: [Int], budget: Int) -> [Int] {
        guard budget > 0 else { return Array(repeating: 0, count: counts.count) }
        let total = counts.reduce(0, +)
        guard total > budget else { return counts }
        let active = counts.indices.filter { counts[$0] > 0 }
        var result = Array(repeating: 0, count: counts.count)
        if budget < active.count {
            for ordinal in 0..<budget {
                let sampled = ((2 * ordinal + 1) * active.count) / (2 * budget)
                result[active[min(sampled, active.count - 1)]] = 1
            }
            return result
        }
        for index in active { result[index] = 1 }
        let remaining = budget - active.count
        guard remaining > 0 else { return result }
        let capacities = counts.indices.map { max(0, counts[$0] - result[$0]) }
        let totalCapacity = capacities.reduce(0, +)
        guard totalCapacity > 0 else { return result }
        var remainders: [(index: Int, value: Int)] = []
        var distributed = 0
        for index in counts.indices where capacities[index] > 0 {
            let weighted = remaining * capacities[index]
            let share = min(capacities[index], weighted / totalCapacity)
            result[index] += share
            distributed += share
            remainders.append((index, weighted % totalCapacity))
        }
        remainders.sort {
            $0.value == $1.value ? $0.index < $1.index : $0.value > $1.value
        }
        var leftover = remaining - distributed
        for entry in remainders where leftover > 0 {
            guard result[entry.index] < counts[entry.index] else { continue }
            result[entry.index] += 1
            leftover -= 1
        }
        return result
    }

    private func meshSampleIndex(ordinal: Int, sourceCount: Int, sampleCount: Int) -> Int {
        guard sampleCount < sourceCount else { return ordinal }
        return min(sourceCount - 1, ((2 * ordinal + 1) * sourceCount) / (2 * sampleCount))
    }

    private func meshWorldVertex(
        _ source: ARGeometrySource,
        at index: Int,
        transform: simd_float4x4
    ) -> SIMD3<Float>? {
        guard index >= 0, index < source.count else { return nil }
        let address = source.buffer.contents().advanced(by: source.offset + index * source.stride)
        let values = address.assumingMemoryBound(to: Float.self)
        let local = SIMD3<Float>(values[0], values[1], values[2])
        let world4 = transform * SIMD4<Float>(local.x, local.y, local.z, 1)
        let world = SIMD3<Float>(world4.x, world4.y, world4.z)
        return world.x.isFinite && world.y.isFinite && world.z.isFinite ? world : nil
    }

    private func meshSpatialCellKey(_ plan: MeshAnchorExportPlan) -> String {
        "\(plan.spatialCell.x):\(plan.spatialCell.y):\(plan.spatialCell.z)"
    }

    private func meshIndex(_ element: ARGeometryElement, at index: Int) -> UInt32? {
        let address = element.buffer.contents().advanced(by: index * element.bytesPerIndex)
        switch element.bytesPerIndex {
        case 2:
            return UInt32(address.load(as: UInt16.self))
        case 4:
            return address.load(as: UInt32.self)
        default:
            return nil
        }
    }

    private func meshClassification(_ source: ARGeometrySource?, faceIndex: Int) -> UInt8 {
        guard let source, faceIndex < source.count else { return 0 }
        let address = source.buffer.contents().advanced(by: source.offset + faceIndex * source.stride)
        return address.load(as: UInt8.self)
    }

    private func meshClassificationLabel(_ value: UInt8) -> String {
        switch value {
        case 1: return "wall"
        case 2: return "floor"
        case 3: return "ceiling"
        case 4: return "table"
        case 5: return "seat"
        case 6: return "window"
        case 7: return "door"
        default: return "none"
        }
    }

    private func meshAuthorityReport() -> [String: Bool] {
        [
            "capture_guidance_only": true,
            "metric_authority": false,
            "collision_geometry": false,
            "planning_authority": false,
            "semantic_authority": false,
            "training_result": false,
        ]
    }

    private func appendLittleEndian(_ value: Float, to data: inout Data) {
        var bits = value.bitPattern.littleEndian
        withUnsafeBytes(of: &bits) { data.append(contentsOf: $0) }
    }

    private func appendLittleEndian(_ value: UInt32, to data: inout Data) {
        var littleEndian = value.littleEndian
        withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
    }

    @available(iOS 16.0, *)
    func updateRoomPlanPreview(room: CapturedRoom) {
        let summary = roomPlanSummary(room)
        roomPlanStatus = summary.status
        roomPlanDetail = summary.detail
        roomPlanSummaryText = summary.shortText
    }

    @available(iOS 16.0, *)
    func noteRoomPlanInstruction(_ instruction: RoomCaptureSession.Instruction) {
        roomPlanStatus = "RoomPlan guidance"
        roomPlanDetail = roomPlanInstructionText(instruction)
    }

    @available(iOS 16.0, *)
    func noteRoomPlanFailure(_ message: String) {
        roomPlanStatus = "RoomPlan held"
        roomPlanDetail = message
    }

    private func startSharedRoomPlanIfNeeded(in directory: URL) {
        guard captureIntent == "full_room_semantic" else {
            refreshSpatialGuidanceCapability()
            return
        }
        guard RoomCaptureSession.isSupported, let session else {
            noteRoomPlanFailure("Shared RoomPlan is unavailable. Video 3DGS capture will continue.")
            appendSessionEvent("room_plan_shared_held", details: ["reason": "unsupported_or_missing_ar_session"])
            refreshSpatialGuidanceCapability()
            return
        }

        let generation = UUID()
        let roomSession = RoomCaptureSession(arSession: session)
        roomSession.delegate = self
        sharedRoomCaptureSession = roomSession
        sharedRoomPlanGeneration = generation
        sharedRoomPlanDirectory = directory
        var configuration = RoomCaptureSession.Configuration()
        configuration.isCoachingEnabled = false
        roomPlanStatus = "RoomPlan scanning"
        roomPlanDetail = "Room semantics share the Video 3DGS camera session."
        spatialGuidanceMode = "roomplan_shared"
        spatialGuidanceStatus = "Room semantics"
        applySpatialGuidanceThermalPolicy(ProcessInfo.processInfo.thermalState)
        roomSession.run(configuration: configuration)
        appendSessionEvent("room_plan_shared_started", details: ["generation": generation.uuidString])
    }

    private func stopSharedRoomPlanIfNeeded(completion: @escaping () -> Void) {
        guard let roomSession = sharedRoomCaptureSession else {
            completion()
            return
        }
        sharedRoomPlanStopCompletion = completion
        roomPlanStatus = "RoomPlan processing"
        roomPlanDetail = "Building layout evidence in the capture coordinate frame."

        let generation = sharedRoomPlanGeneration
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, self.sharedRoomPlanGeneration == generation else { return }
            self.noteRoomPlanFailure("RoomPlan processing timed out; RGB-D capture evidence was preserved.")
            self.appendSessionEvent("room_plan_shared_held", details: ["reason": "processing_timeout"])
            self.finishSharedRoomPlan()
        }
        sharedRoomPlanTimeoutWorkItem = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 12, execute: timeout)
        roomSession.stop(pauseARSession: false)
    }

    private func finishSharedRoomPlan() {
        sharedRoomPlanTimeoutWorkItem?.cancel()
        sharedRoomPlanTimeoutWorkItem = nil
        sharedRoomCaptureSession?.delegate = nil
        sharedRoomCaptureSession = nil
        sharedRoomPlanGeneration = nil
        sharedRoomPlanDirectory = nil
        let completion = sharedRoomPlanStopCompletion
        sharedRoomPlanStopCompletion = nil
        completion?()
    }

    @available(iOS 16.0, *)
    func exportRoomPlan(room: CapturedRoom) {
        do {
            let directory = try prepareRoomPlanDirectory()
            try writeRoomPlanAssets(room: room, directory: directory)
            if !frames.isEmpty {
                _ = try? writeCaptureManifest()
            }
            let summary = roomPlanSummary(room)
            roomPlanStatus = "RoomPlan exported"
            roomPlanDetail = summary.detail
            roomPlanSummaryText = summary.shortText
        } catch {
            roomPlanStatus = "RoomPlan export failed"
            roomPlanDetail = error.localizedDescription
        }
    }

    @available(iOS 16.0, *)
    private func writeRoomPlanAssets(room: CapturedRoom, directory: URL) throws {
        let roomPlanDirectory = directory.appendingPathComponent("room_plan", isDirectory: true)
        try FileManager.default.createDirectory(at: roomPlanDirectory, withIntermediateDirectories: true)
        let usdzURL = roomPlanDirectory.appendingPathComponent("room.usdz")
        let reportURL = roomPlanDirectory.appendingPathComponent("room_plan_report.json")
        let semanticsURL = roomPlanDirectory.appendingPathComponent("room_semantics.json")
        try room.export(to: usdzURL, exportOptions: .mesh)
        writeJSON(roomPlanReport(room: room), to: reportURL)
        writeJSON(roomPlanSemanticsReport(room: room), to: semanticsURL)
        roomPlanFile = usdzURL
        roomPlanReportFile = reportURL
        roomPlanSemanticsFile = semanticsURL
    }

    private func prepareRoomPlanDirectory() throws -> URL {
        let directory: URL
        if let currentSessionDirectory {
            directory = currentSessionDirectory
        } else {
            directory = try makeSessionDirectory()
            currentSessionDirectory = directory
            capturePackageState = .partial
        }
        try makeExportFolders(in: directory)
        return directory
    }

    @available(iOS 16.0, *)
    private func roomPlanSummary(_ room: CapturedRoom) -> (status: String, detail: String, shortText: String) {
        let area = roomPlanAreaEstimate(room)
        let areaText = area.map { String(format: "%.1f m2", $0) } ?? "area pending"
        let short = "\(room.walls.count) walls | \(room.objects.count) objects | \(areaText)"
        return (
            status: "RoomPlan guidance",
            detail: "\(short). Layout is capture guidance, not 3DGS quality proof.",
            shortText: short
        )
    }

    @available(iOS 16.0, *)
    private func roomPlanReport(room: CapturedRoom) -> [String: Any] {
        var report: [String: Any] = [
            "schema": "capture_splat.room_plan_report.v0.1",
            "room_plan_file": "room_plan/room.usdz",
            "room_semantics_file": "room_plan/room_semantics.json",
            "walls": room.walls.count,
            "doors": room.doors.count,
            "windows": room.windows.count,
            "openings": room.openings.count,
            "objects": room.objects.count,
            "authority": [
                "capture_guidance_only": true,
                "metric_authority": false,
                "collision_geometry": false,
                "quality_proof": false,
            ],
        ]
        report["area_estimate_square_meters"] = roomPlanAreaEstimate(room) ?? NSNull()
        if #available(iOS 17.0, *) {
            report["floors"] = room.floors.count
            report["sections"] = room.sections.count
            report["story"] = room.story
            report["version"] = room.version
        }
        return report
    }

    @available(iOS 16.0, *)
    private func roomPlanSemanticsReport(room: CapturedRoom) -> [String: Any] {
        [
            "schema": "capture_splat.room_semantics.v0.1",
            "room_plan_file": "room_plan/room.usdz",
            "semantic_source": "Apple RoomPlan CapturedRoom",
            "walls": room.walls.enumerated().map {
                roomSurfaceSemantic(kind: "wall", index: $0.offset, dimensions: $0.element.dimensions, transform: $0.element.transform)
            },
            "doors": room.doors.enumerated().map {
                roomSurfaceSemantic(kind: "door", index: $0.offset, dimensions: $0.element.dimensions, transform: $0.element.transform)
            },
            "windows": room.windows.enumerated().map {
                roomSurfaceSemantic(kind: "window", index: $0.offset, dimensions: $0.element.dimensions, transform: $0.element.transform)
            },
            "openings": room.openings.enumerated().map {
                roomSurfaceSemantic(kind: "opening", index: $0.offset, dimensions: $0.element.dimensions, transform: $0.element.transform)
            },
            "objects": room.objects.enumerated().map {
                roomObjectSemantic(index: $0.offset, object: $0.element)
            },
            "authority": [
                "capture_guidance_only": true,
                "room_semantic_proposal": true,
                "metric_authority": false,
                "collision_geometry": false,
                "planning_authority": false,
                "semantic_authority": false,
                "quality_proof": false,
            ],
        ]
    }

    private func roomSurfaceSemantic(
        kind: String,
        index: Int,
        dimensions: SIMD3<Float>,
        transform: simd_float4x4
    ) -> [String: Any] {
        [
            "id": "\(kind)_\(index)",
            "kind": kind,
            "dimensions_meters": vectorReport(dimensions),
            "transform_matrix": transform.rows,
            "center_meters": vectorReport(SIMD3<Float>(
                transform.columns.3.x,
                transform.columns.3.y,
                transform.columns.3.z
            )),
        ]
    }

    @available(iOS 16.0, *)
    private func roomObjectSemantic(index: Int, object: CapturedRoom.Object) -> [String: Any] {
        [
            "id": "object_\(index)",
            "kind": "object",
            "category": String(describing: object.category),
            "dimensions_meters": vectorReport(object.dimensions),
            "transform_matrix": object.transform.rows,
            "center_meters": vectorReport(SIMD3<Float>(
                object.transform.columns.3.x,
                object.transform.columns.3.y,
                object.transform.columns.3.z
            )),
        ]
    }

    private func vectorReport(_ vector: SIMD3<Float>) -> [String: Float] {
        [
            "x": vector.x,
            "y": vector.y,
            "z": vector.z,
        ]
    }

    @available(iOS 16.0, *)
    private func roomPlanAreaEstimate(_ room: CapturedRoom) -> Double? {
        guard #available(iOS 17.0, *) else { return nil }
        let area = room.floors.reduce(0.0) { partial, floor in
            partial + Double(abs(floor.dimensions.x * floor.dimensions.y))
        }
        return area > 0 ? area : nil
    }

    @available(iOS 16.0, *)
    private func roomPlanInstructionText(_ instruction: RoomCaptureSession.Instruction) -> String {
        switch instruction {
        case .moveCloseToWall:
            return "Move closer to walls so RoomPlan can refine boundaries."
        case .moveAwayFromWall:
            return "Step back to keep more room structure in view."
        case .slowDown:
            return "Slow down to avoid weak room layout observations."
        case .turnOnLight:
            return "Add light before continuing the room layout pass."
        case .lowTexture:
            return "Find textured corners, posters, furniture edges, or door frames."
        case .normal:
            return "Continue sweeping walls, corners, openings, and large objects."
        @unknown default:
            return "Continue the RoomPlan scan slowly."
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

    private var activeKeyframeScoreThreshold: Double {
        scanTargetMode == "video_3dgs" ? videoKeyframeScoreThreshold : keyframeScoreThreshold
    }

    var currentCaptureIntentOption: CaptureIntentOption {
        Self.captureIntentOptions.first { $0.id == captureIntent } ?? Self.captureIntentOptions[0]
    }

    var requiresSubjectTarget: Bool {
        guard scanTargetMode == "video_3dgs" else { return false }
        return currentCaptureIntentOption.requiresSubjectLock
    }

    var usesAngularCoverageDisplay: Bool {
        scanTargetMode == "object" || (scanTargetMode == "video_3dgs" && captureIntent == "object_orbit")
    }

    var coverageDisplayTitle: String {
        switch captureIntent {
        case "scene_cluster": return "Desk Useful Frames"
        case "room_walkthrough", "full_room_semantic": return "Room Useful Frames"
        case "object_orbit": return "Orbit Coverage"
        case "corridor_passage": return "Path Useful Frames"
        case "facade_wall": return "Wall Useful Frames"
        case "outdoor_object": return "Outdoor Useful Frames"
        case "detail_repair": return "Repair Useful Frames"
        default: return "Useful Frames"
        }
    }

    var coverageDisplayScores: [Double] {
        guard !usesAngularCoverageDisplay else { return coverageSectors }
        let segmentCount = 6
        let filledSegments = min(
            Double(acceptedKeyframes) / Double(max(acceptedFrameGuidanceTarget, 1)) * Double(segmentCount),
            Double(segmentCount)
        )
        return (0..<segmentCount).map { index in
            min(max(filledSegments - Double(index), 0), 1)
        }
    }

    var coverageDisplayHintText: String {
        guard !usesAngularCoverageDisplay else { return coverageHintText }
        if acceptedKeyframes >= acceptedFrameGuidanceTarget {
            return "\(acceptedKeyframes) useful | ready to stop"
        }
        return "\(acceptedKeyframes)/\(acceptedFrameGuidanceTarget) useful | \(readinessState)"
    }

    var coverageDisplayCountText: String {
        if usesAngularCoverageDisplay {
            return "\(coveredSectorCount())/\(coverageSectors.count)"
        }
        return "\(acceptedKeyframes)/\(acceptedFrameGuidanceTarget)"
    }

    var coverageDisplayStatusText: String {
        usesAngularCoverageDisplay ? "\(missingSectorCount) missing" : coverageDisplayCountText
    }

    var coverageDisplayNavigationText: String {
        guard !usesAngularCoverageDisplay else { return coverageNavigationText }
        if acceptedKeyframes >= acceptedFrameGuidanceTarget {
            return "Useful-frame target reached. Stop unless a visible gap remains."
        }
        switch captureIntent {
        case "scene_cluster":
            return "Stay around the desk; add side, high, and low views."
        case "room_walkthrough", "full_room_semantic":
            return "Keep a connected room path and revisit corners and openings."
        case "corridor_passage":
            return "Continue forward with side glances, then return along the path."
        case "facade_wall":
            return "Side-step along the wall and add oblique views."
        case "outdoor_object":
            return "Keep the outdoor subject stable and add wider views."
        case "detail_repair":
            return "Stay on the weak area and add an opposite-side view."
        default:
            return currentCaptureIntentOption.guidance
        }
    }

    private var acceptedFrameGuidanceTarget: Int {
        switch captureIntent {
        case "scene_cluster": return 120
        case "room_walkthrough", "full_room_semantic": return 180
        case "corridor_passage", "outdoor_object": return 120
        case "facade_wall": return 90
        case "detail_repair": return 60
        default: return 120
        }
    }

    var isCapturePackageReady: Bool {
        capturePackageState == .ready
    }

    var hasRecoverablePartialCapture: Bool {
        capturePackageState == .partial && currentSessionDirectory != nil
    }

    private var usesSubjectTargetGuidance: Bool {
        scanTargetMode == "object" || requiresSubjectTarget
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

    private func captureProfileLabel() -> String {
        switch scanTargetMode {
        case "room":
            return "room_interior"
        case "video_3dgs":
            return "video_3dgs_max"
        default:
            return "object"
        }
    }

    private func makeSessionConfig() -> SessionConfig {
        let device = Self.primaryCaptureDevice
        return SessionConfig(
            aeLock: device?.exposureMode == .locked,
            awbLock: device?.whiteBalanceMode == .locked,
            focusLock: device?.focusMode == .locked,
            videoFormat: videoRecorder.appendedFrameCount > 0 ? "hevc" : nil,
            videoTargetFPS: videoRecorder.appendedFrameCount > 0
                ? Int(videoRecorder.targetFPS.rounded())
                : nil
        )
    }

    private static var primaryCaptureDevice: AVCaptureDevice? {
        ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera
    }

    private func applyCaptureLocks(_ locked: Bool) {
        guard let device = Self.primaryCaptureDevice else {
            if locked {
                statusText = "Exposure lock unavailable on this device."
            }
            return
        }
        do {
            try device.lockForConfiguration()
            if locked {
                if device.isExposureModeSupported(.locked) { device.exposureMode = .locked }
                if device.isWhiteBalanceModeSupported(.locked) { device.whiteBalanceMode = .locked }
                if device.isFocusModeSupported(.locked) { device.focusMode = .locked }
            } else {
                if device.isExposureModeSupported(.continuousAutoExposure) { device.exposureMode = .continuousAutoExposure }
                if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) { device.whiteBalanceMode = .continuousAutoWhiteBalance }
                if device.isFocusModeSupported(.continuousAutoFocus) { device.focusMode = .continuousAutoFocus }
            }
            device.unlockForConfiguration()
            if isRecording {
                appendSessionEvent("camera_controls_changed", details: [
                    "requested_locked": locked,
                    "ae_locked": device.exposureMode == .locked,
                    "awb_locked": device.whiteBalanceMode == .locked,
                    "focus_locked": device.focusMode == .locked,
                ])
            }
        } catch {
            statusText = "Capture lock change failed: \(error.localizedDescription)"
        }
    }

    private func planesReport() -> [String: Any] {
        var floorY: Float?
        let planes = planeAnchors.values.map { anchor -> [String: Any] in
            let center = simd_mul(anchor.transform, simd_float4(anchor.center, 1))
            let width = anchor.planeExtent.width
            let height = anchor.planeExtent.height
            let isHorizontal = anchor.alignment == .horizontal
            if isHorizontal, width * height > 0.5, floorY == nil || center.y < floorY! {
                floorY = center.y
            }
            return [
                "alignment": isHorizontal ? "horizontal" : "vertical",
                "classification": planeClassificationLabel(anchor.classification),
                "center_world": [center.x, center.y, center.z],
                "extent": [width, height],
            ]
        }
        return [
            "plane_count": planes.count,
            "floor_y_estimate": floorY as Any,
            "planes": planes,
            "authority": "capture_guidance_only",
        ]
    }

    private func planeClassificationLabel(_ classification: ARPlaneAnchor.Classification) -> String {
        switch classification {
        case .floor: return "floor"
        case .ceiling: return "ceiling"
        case .wall: return "wall"
        case .table: return "table"
        case .seat: return "seat"
        case .door: return "door"
        case .window: return "window"
        default: return "unknown"
        }
    }

    private func updateCaptureProfileText() {
        switch scanTargetMode {
        case "room":
            captureProfileText = "Room COLMAP keyframes"
            captureProfileDetail = "Strict overlap, parallax, blur, and reconnect guidance for room 3DGS input."
        case "video_3dgs":
            captureProfileText = "Video 3DGS Max - \(currentCaptureIntentOption.shortTitle)"
            captureProfileDetail = currentCaptureIntentOption.detail
        default:
            captureProfileText = "Object RGB-D keyframes"
            captureProfileDetail = "Object-locked RGB, LiDAR depth, pose, and foreground proposal metadata."
        }
    }

    private func refreshHealth() {
        let uptime = ProcessInfo.processInfo.systemUptime
        if storageFreeText == "--" || uptime - lastStorageRefreshUptime >= 30 {
            storageFreeText = availableStorageText()
            lastStorageRefreshUptime = uptime
        }
        let thermalState = ProcessInfo.processInfo.thermalState
        let currentThermalState = thermalStateLabel(thermalState)
        thermalStateText = currentThermalState
        videoRecorder.setTargetFPS(videoTargetFPS(for: thermalState))
        applySpatialGuidanceThermalPolicy(thermalState)
        if isRecording, currentThermalState != lastRecordedThermalState {
            appendSessionEvent("thermal_state_changed", details: ["thermal_state": currentThermalState])
            lastRecordedThermalState = currentThermalState
        }
        let battery = UIDevice.current.batteryLevel
        batteryText = battery < 0 ? "--" : "\(Int((battery * 100).rounded()))%"
    }

    private func videoTargetFPS(for state: ProcessInfo.ThermalState) -> Double {
        switch state {
        case .nominal, .fair:
            return 15
        case .serious:
            return 10
        case .critical:
            return 6
        @unknown default:
            return 10
        }
    }

    private func liveGuidanceInterval(for state: ProcessInfo.ThermalState) -> TimeInterval {
        switch state {
        case .nominal, .fair:
            return 0.2
        case .serious:
            return 0.5
        case .critical:
            return 1
        @unknown default:
            return 0.5
        }
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

    private func applySpatialGuidanceThermalPolicy(_ state: ProcessInfo.ThermalState) {
        let policy: String
        switch state {
        case .nominal, .fair:
            policy = "full"
            spatialGuidanceFaceBudget = 60_000
            spatialGuidanceUpdateHz = 5
            spatialGuidanceShowsMesh = isSpatialGuidanceVisible
                && (spatialGuidanceMode == "lidar_mesh" || spatialGuidanceMode == "roomplan_shared")
        case .serious:
            policy = "map_only"
            spatialGuidanceFaceBudget = 0
            spatialGuidanceUpdateHz = 2
            spatialGuidanceShowsMesh = false
        case .critical:
            policy = "pose_only"
            spatialGuidanceFaceBudget = 0
            spatialGuidanceUpdateHz = 0
            spatialGuidanceShowsMesh = false
        @unknown default:
            policy = "reduced"
            spatialGuidanceFaceBudget = 30_000
            spatialGuidanceUpdateHz = 2
            spatialGuidanceShowsMesh = false
        }
        let now = ProcessInfo.processInfo.systemUptime
        let thermalState = thermalStateLabel(state)
        let renderState = spatialGuidanceRenderState(for: policy)
        lastSpatialGuidanceMeshPauseReason = spatialGuidanceMeshPauseReason(
            policy: policy,
            thermalState: thermalState
        )
        if isRecording {
            transitionSpatialGuidancePolicy(to: policy, at: now)
            transitionSpatialGuidanceThermalState(to: thermalState, at: now)
            transitionSpatialGuidanceRenderState(to: renderState, at: now)
        }
        guard policy != lastSpatialGuidancePolicy else { return }
        let previousPolicy = lastSpatialGuidancePolicy.isEmpty ? nil : lastSpatialGuidancePolicy
        lastSpatialGuidancePolicy = policy
        if isRecording {
            let transition: [String: Any] = [
                "policy": policy,
                "previous_policy": previousPolicy ?? NSNull(),
                "thermal_state": thermalState,
                "face_budget": spatialGuidanceFaceBudget,
                "update_hz": spatialGuidanceUpdateHz,
                "video_target_fps": videoRecorder.targetFPS,
                "capture_elapsed_seconds": spatialGuidanceCaptureStartUptime.map {
                    max(0, now - $0)
                } ?? 0,
                "render_state": renderState,
                "mesh_pause_reason": lastSpatialGuidanceMeshPauseReason,
            ]
            spatialGuidanceThermalTransitions.append(transition)
            appendSessionEvent("spatial_guidance_policy_changed", details: transition)
        }
    }

    private func spatialGuidanceRenderState(for policy: String) -> String {
        guard isSpatialGuidanceVisible else { return "hidden" }
        if spatialGuidanceShowsMesh { return "mesh_visible" }
        if policy == "pose_only" { return "pose_only" }
        if policy == "map_only" { return "map_only" }
        if spatialGuidanceMode == "depth_points" { return "depth_points" }
        if spatialGuidanceFaceBudget > 0 { return "features_and_map" }
        return "map_only"
    }

    private func spatialGuidanceMeshPauseReason(policy: String, thermalState: String) -> String {
        guard isSpatialGuidanceVisible else { return "guidance_hidden_by_user" }
        guard spatialGuidanceMode == "lidar_mesh" || spatialGuidanceMode == "roomplan_shared" else {
            return "mesh_unavailable_for_capability_mode"
        }
        if spatialGuidanceShowsMesh { return "none" }
        if thermalState == "serious" || policy == "map_only" {
            return "serious_thermal_map_only"
        }
        if thermalState == "critical" || policy == "pose_only" {
            return "critical_thermal_pose_only"
        }
        return "mesh_disabled_by_guidance_policy"
    }

    private func transitionSpatialGuidancePolicy(to policy: String, at now: TimeInterval) {
        guard lastSpatialGuidancePolicy != policy || spatialGuidancePolicyStartUptime == nil else { return }
        if !lastSpatialGuidancePolicy.isEmpty, let startedAt = spatialGuidancePolicyStartUptime {
            spatialGuidancePolicyDurationsSeconds[lastSpatialGuidancePolicy, default: 0] += max(0, now - startedAt)
        }
        spatialGuidancePolicyStartUptime = now
    }

    private func transitionSpatialGuidanceThermalState(to state: String, at now: TimeInterval) {
        guard currentSpatialGuidanceThermalState != state || spatialGuidanceThermalStartUptime == nil else { return }
        if let current = currentSpatialGuidanceThermalState,
           let startedAt = spatialGuidanceThermalStartUptime {
            spatialGuidanceThermalDurationsSeconds[current, default: 0] += max(0, now - startedAt)
        }
        currentSpatialGuidanceThermalState = state
        spatialGuidanceThermalStartUptime = now
    }

    private func transitionSpatialGuidanceRenderState(to state: String, at now: TimeInterval) {
        guard currentSpatialGuidanceRenderState != state || spatialGuidanceRenderStateStartUptime == nil else { return }
        if let current = currentSpatialGuidanceRenderState,
           let startedAt = spatialGuidanceRenderStateStartUptime {
            spatialGuidanceRenderStateDurationsSeconds[current, default: 0] += max(0, now - startedAt)
        }
        currentSpatialGuidanceRenderState = state
        spatialGuidanceRenderStateStartUptime = now
    }

    private func finishSpatialGuidanceSegments(at now: TimeInterval) {
        if !lastSpatialGuidancePolicy.isEmpty, let startedAt = spatialGuidancePolicyStartUptime {
            spatialGuidancePolicyDurationsSeconds[lastSpatialGuidancePolicy, default: 0] += max(0, now - startedAt)
        }
        if let current = currentSpatialGuidanceThermalState,
           let startedAt = spatialGuidanceThermalStartUptime {
            spatialGuidanceThermalDurationsSeconds[current, default: 0] += max(0, now - startedAt)
        }
        if let current = currentSpatialGuidanceRenderState,
           let startedAt = spatialGuidanceRenderStateStartUptime {
            spatialGuidanceRenderStateDurationsSeconds[current, default: 0] += max(0, now - startedAt)
        }
        spatialGuidancePolicyStartUptime = nil
        spatialGuidanceThermalStartUptime = nil
        spatialGuidanceRenderStateStartUptime = nil
    }

    private func updateSpatialGuidancePose(from frame: ARFrame) {
        let interval = liveGuidanceInterval(for: ProcessInfo.processInfo.thermalState)
        guard frame.timestamp - lastSpatialPoseTimestamp >= interval else { return }
        lastSpatialPoseTimestamp = frame.timestamp
        let transform = frame.camera.transform
        let position = SIMD2<Float>(transform.columns.3.x, transform.columns.3.z)
        let forward = cameraForward(transform)
        spatialGuidancePose = SpatialGuidancePose(
            x: position.x,
            z: position.y,
            headingRadians: atan2(forward.x, forward.z)
        )
        if lastSpatialPathPosition.map({ simd_distance($0, position) >= 0.1 }) ?? true {
            spatialGuidancePath.append(SpatialGuidancePathPoint(
                id: spatialPathPointID,
                x: position.x,
                z: position.y
            ))
            spatialPathPointID += 1
            lastSpatialPathPosition = position
            if spatialGuidancePath.count > maxSpatialGuidancePathPoints {
                spatialGuidancePath.removeFirst(spatialGuidancePath.count - maxSpatialGuidancePathPoints)
            }
        }
    }

    private func recordSpatialGuidanceAnchor(_ anchor: ARAnchor) {
        guard anchor is ARMeshAnchor || anchor is ARPlaneAnchor else { return }
        spatialGuidanceReceivedUpdateCount += 1
        guard spatialGuidanceUpdateHz > 0 else {
            spatialGuidancePolicyDisabledUpdateCount += 1
            return
        }
        let now = ProcessInfo.processInfo.systemUptime
        if let lastUpdate = lastSpatialAnchorUpdateTimestamp[anchor.identifier],
           now - lastUpdate < 1 / spatialGuidanceUpdateHz {
            spatialGuidanceThrottledUpdateCount += 1
            return
        }
        lastSpatialAnchorUpdateTimestamp[anchor.identifier] = now
        let started = ProcessInfo.processInfo.systemUptime
        if let mesh = anchor as? ARMeshAnchor {
            let point = spatialGuidancePoint(
                id: mesh.identifier,
                transform: mesh.transform,
                classification: dominantMeshClassification(mesh.geometry)
            )
            spatialAnchorCells[mesh.identifier] = point
        } else if let plane = anchor as? ARPlaneAnchor {
            let center = plane.transform * SIMD4<Float>(plane.center.x, plane.center.y, plane.center.z, 1)
            var transform = matrix_identity_float4x4
            transform.columns.3 = SIMD4<Float>(center.x, center.y, center.z, 1)
            spatialPlaneCells[plane.identifier] = spatialGuidancePoint(
                id: plane.identifier,
                transform: transform,
                classification: planeClassificationLabel(plane.classification)
            )
        } else {
            return
        }
        publishSpatialGuidanceCells()
        let duration = (ProcessInfo.processInfo.systemUptime - started) * 1_000
        spatialGuidanceUpdateCount += 1
        if duration > spatialGuidanceUpdateBudgetMs {
            spatialGuidanceOverBudgetProcessingCount += 1
        }
        spatialGuidanceUpdateDurationsMs.append(duration)
        if spatialGuidanceUpdateDurationsMs.count > maxSpatialGuidanceDurationSamples {
            spatialGuidanceUpdateDurationsMs.removeFirst()
        }
    }

    private func spatialGuidancePoint(
        id: UUID,
        transform: simd_float4x4,
        classification: String
    ) -> SpatialGuidancePoint {
        let x = transform.columns.3.x
        let z = transform.columns.3.z
        let key = spatialGuidanceCellKey(x: x, z: z)
        return SpatialGuidancePoint(
            id: "\(id.uuidString):\(key)",
            x: x,
            z: z,
            classification: classification,
            covered: spatialCellObservationCounts[key, default: 0] > 0
        )
    }

    private func spatialGuidanceCellKey(x: Float, z: Float) -> String {
        "\(Int(floor(x / spatialGuidanceCellSizeMeters))):\(Int(floor(z / spatialGuidanceCellSizeMeters)))"
    }

    private func publishSpatialGuidanceCells() {
        var cells: [String: SpatialGuidancePoint] = [:]
        for point in Array(spatialAnchorCells.values) + Array(spatialPlaneCells.values) {
            let key = spatialGuidanceCellKey(x: point.x, z: point.z)
            let covered = spatialCellObservationCounts[key, default: 0] > 0
            if cells[key] == nil || cells[key]?.classification == "none" {
                cells[key] = SpatialGuidancePoint(
                    id: key,
                    x: point.x,
                    z: point.z,
                    classification: point.classification,
                    covered: covered
                )
            }
        }
        spatialGuidanceCells = cells.values.sorted { $0.id < $1.id }
    }

    private func markSpatialGuidanceCoverage(at position: SIMD3<Float>, forward: SIMD3<Float>) {
        let rawForward = SIMD2<Float>(forward.x, forward.z)
        let forwardLength = simd_length(rawForward)
        guard forwardLength.isFinite, forwardLength > 0.001 else { return }
        let forwardXZ = rawForward / forwardLength
        for point in spatialGuidanceCells {
            let offset = SIMD2<Float>(point.x - position.x, point.z - position.z)
            let distance = simd_length(offset)
            guard distance >= 0.2, distance <= 2.5 else { continue }
            let direction = offset / distance
            guard simd_dot(forwardXZ, direction) >= 0.35 else { continue }
            let key = spatialGuidanceCellKey(x: point.x, z: point.z)
            spatialCellObservationCounts[key, default: 0] += 1
        }
        publishSpatialGuidanceCells()
    }

    private func dominantMeshClassification(_ geometry: ARMeshGeometry) -> String {
        let source = geometry.classification
        let faceCount = geometry.faces.count
        guard faceCount > 0 else { return "none" }
        let stride = max(1, faceCount / 256)
        var counts: [UInt8: Int] = [:]
        for index in Swift.stride(from: 0, to: faceCount, by: stride) {
            let value = meshClassification(source, faceIndex: index)
            counts[value, default: 0] += 1
        }
        return meshClassificationLabel(counts.max { $0.value < $1.value }?.key ?? 0)
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
                ? currentCaptureIntentOption.guidance
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
        } else if lastKeyframeDecision.contains("fast_motion") {
            readinessState = "Hold"
            nextAction = "Slow your movement"
            backgroundWarning = "The camera was moving too fast for a sharp keyframe."
            guidanceArrowSystemImage = "tortoise.circle"
            guidanceText = "Slow down; smooth, slow motion keeps frames sharp."
        } else if lastKeyframeDecision.contains("low_blur_score") {
            readinessState = "Hold"
            nextAction = "Slow down and hold steady"
            backgroundWarning = "The last candidate had weak detail or motion blur."
            guidanceArrowSystemImage = "camera.metering.center.weighted"
            guidanceText = "Hold steadier on textured surfaces before moving again."
        } else if lastKeyframeDecision.contains("weak_feature_distribution") {
            readinessState = "Hold"
            nextAction = "Find textured edges"
            backgroundWarning = "COLMAP needs features spread across the view, not one small patch."
            guidanceArrowSystemImage = "square.grid.3x3"
            guidanceText = "Aim at corners, shelves, posters, table edges, or textured objects before moving."
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
        } else if scanTargetMode == "video_3dgs", !usesAngularCoverageDisplay {
            updateIntentFrameGuidance()
        } else if covered < 4 {
            readinessState = "Not ready"
            nextAction = missingDirectionHint()
            backgroundWarning = "Dots mean depth samples only. Keep orbiting until more sectors turn green."
            guidanceArrowSystemImage = missingDirectionArrow()
            guidanceText = "\(nextAction). \(missing) angles still missing."
        } else if requiresSubjectTarget, let missingBand = missingTargetElevationBand() {
            readinessState = "Almost"
            switch missingBand {
            case 0:
                nextAction = "Add an object-level pass"
                guidanceArrowSystemImage = "arrow.down.circle"
                guidanceText = "Lower the phone near the object height and continue the orbit."
            case 1:
                nextAction = "Add a mid-angle pass"
                guidanceArrowSystemImage = "arrow.left.and.right.circle"
                guidanceText = "Orbit at a moderate angle before returning overhead."
            default:
                nextAction = "Add a high-angle pass"
                guidanceArrowSystemImage = "arrow.up.circle"
                guidanceText = "Raise the phone for an overhead view while keeping the object centered."
            }
            backgroundWarning = "Object Orbit needs low, middle, and high views before it is ready."
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

    private func updateIntentFrameGuidance() {
        let target = acceptedFrameGuidanceTarget
        let needsFinalPass = acceptedKeyframes >= target / 2
        if acceptedKeyframes >= target {
            readinessState = "Ready"
            nextAction = "Ready to stop"
            backgroundWarning = "Useful-frame readiness is guidance only, not reconstruction quality."
            guidanceArrowSystemImage = "checkmark.circle"
            guidanceText = "Stop unless you can name a visible gap around the selected subject."
            return
        }

        readinessState = needsFinalPass ? "Good" : "Almost"
        backgroundWarning = "Stay with the selected subject; room-wide headings are not required."
        guidanceArrowSystemImage = needsFinalPass ? "arrow.up.and.down.circle" : "arrow.left.and.right.circle"
        guidanceText = currentCaptureIntentOption.guidance
        switch captureIntent {
        case "scene_cluster":
            nextAction = needsFinalPass ? "Add one high and low desk pass" : "Continue desk side views"
        case "room_walkthrough", "full_room_semantic":
            nextAction = needsFinalPass ? "Revisit corners and openings" : "Continue the connected room path"
        case "corridor_passage":
            nextAction = needsFinalPass ? "Return along the corridor" : "Continue with side glances"
        case "facade_wall":
            nextAction = needsFinalPass ? "Add oblique wall views" : "Continue the lateral wall sweep"
        case "outdoor_object":
            nextAction = needsFinalPass ? "Add wider establishing views" : "Continue the slow outdoor orbit"
        case "detail_repair":
            nextAction = needsFinalPass ? "Add one opposite-side view" : "Continue the focused repair pass"
        default:
            nextAction = needsFinalPass ? "Add final high and low views" : "Continue slow overlapping views"
        }
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
            return !requiresSubjectTarget || isObjectTargetLocked
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
                    : "Center the object, then tap Lock Subject."
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
            if requiresSubjectTarget {
                targetLockStatus = isObjectTargetLocked ? "Subject locked" : "Center subject"
                targetLockDetail = isObjectTargetLocked
                    ? "Orbit around the locked subject center."
                    : "Hold the object in the reticle, then tap Lock Subject."
            } else {
                targetLockStatus = "Video 3DGS mode"
                targetLockDetail = "Move slowly like recording video; haptics mark sharp accepted frames."
            }
        default:
            targetLockStatus = "Target lock optional"
            targetLockDetail = "Outdoor/diagnostic modes do not require target lock."
        }
    }

    private func updateTargetCandidate(from frame: ARFrame, depthMap: CVPixelBuffer) {
        latestCameraTransform = frame.camera.transform
        guard usesSubjectTargetGuidance else {
            latestTargetCandidateWorldPosition = nil
            latestTargetCandidateDistance = nil
            targetCandidateObservations.removeAll()
            isSubjectTargetReady = false
            latestObjectExtentProposal = nil
            if !isObjectExtentLocked {
                objectExtentOverlay = nil
            }
            return
        }
        guard let centerDepth = centerDepthMeters(from: depthMap), centerDepth > 0 else {
            latestTargetCandidateWorldPosition = nil
            latestTargetCandidateDistance = nil
            targetCandidateObservations.removeAll { frame.timestamp - $0.timestamp > 0.6 }
            isSubjectTargetReady = false
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
        let candidatePosition = cameraPosition + forward * centerDepth
        latestTargetCandidateWorldPosition = candidatePosition
        latestTargetCandidateDistance = centerDepth
        if !isObjectTargetLocked, usesSubjectTargetGuidance {
            targetCandidateObservations.append(TargetCandidateObservation(
                timestamp: frame.timestamp,
                worldPosition: candidatePosition,
                distanceMeters: centerDepth
            ))
            targetCandidateObservations.removeAll { frame.timestamp - $0.timestamp > 0.6 }
            targetLockDistanceText = String(format: "%.2f m", centerDepth)
            let distances = targetCandidateObservations.map(\.distanceMeters)
            let spread = (distances.max() ?? centerDepth) - (distances.min() ?? centerDepth)
            let mean = distances.isEmpty ? centerDepth : distances.reduce(0, +) / Float(distances.count)
            isSubjectTargetReady = distances.count >= 3 && spread <= max(0.10, mean * 0.10)
            if isSubjectTargetReady {
                targetLockStatus = "Ready to lock"
                targetLockDetail = "Tap Lock Subject, then Record to begin a slow connected orbit."
            }
        }
        latestObjectExtentProposal = makeObjectExtentProposal(from: frame, depthMap: depthMap, centerDepth: centerDepth)
        if !isObjectExtentLocked, isObjectMaskEnabled, usesSubjectTargetGuidance {
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

    private func recordAcceptedCoverage(
        timestamp: TimeInterval,
        transform: simd_float4x4,
        sectorIndex: Int
    ) {
        let index = min(max(sectorIndex, 0), coverageSectorCounts.count - 1)
        let previousCount = coverageSectorCounts[index]
        coverageSectorCounts[index] = min(previousCount + 1, coverageObservationTarget)
        coverageSectors = coverageSectorCounts.map { Double($0) / Double(coverageObservationTarget) }
        if coverageSectorCounts[index] != previousCount {
            appendCoverageHistorySample(timestamp: timestamp, sectorIndex: index, guidancePointCount: guidancePoints.count)
        }
        if let bands = targetCoverageBands(for: transform) {
            targetElevationBandCounts[bands.elevation] += 1
            targetDistanceBandCounts[bands.distance] += 1
        }
        updateCoverageHint()
    }

    private func targetCoverageBands(for transform: simd_float4x4) -> (elevation: Int, distance: Int)? {
        guard usesSubjectTargetGuidance,
              let target = lockedObjectWorldPosition,
              let lockedDistance = lockedObjectDistanceMeters,
              lockedDistance > 0 else {
            return nil
        }
        let offset = cameraPosition(transform) - target
        let distance = simd_length(offset)
        guard distance.isFinite, distance > 0 else { return nil }
        let elevationDegrees = abs(asin(Double(offset.y / distance)) * 180 / .pi)
        let elevationBand = elevationDegrees < 10 ? 0 : (elevationDegrees <= 30 ? 1 : 2)
        let distanceRatio = distance / lockedDistance
        let distanceBand = distanceRatio < 0.85 ? 0 : (distanceRatio <= 1.25 ? 1 : 2)
        return (elevationBand, distanceBand)
    }

    private func missingTargetElevationBand() -> Int? {
        targetElevationBandCounts.firstIndex(where: { $0 == 0 })
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
        let targetBandHint: String
        if usesSubjectTargetGuidance, isObjectTargetLocked {
            if targetElevationBandCounts[0] == 0 {
                targetBandHint = " | add an object-level angle"
            } else if targetElevationBandCounts[1] == 0 {
                targetBandHint = " | add a mid angle"
            } else if targetElevationBandCounts[2] == 0 {
                targetBandHint = " | add a high angle"
            } else if targetDistanceBandCounts[2] == 0 {
                targetBandHint = " | add a wider view"
            } else {
                targetBandHint = ""
            }
        } else {
            targetBandHint = ""
        }
        guard coverageSectors.indices.contains(target), coverageSectors[target] < 1 else {
            coverageNavigationText = "All coarse sectors have saved keyframes.\(targetBandHint)"
            return
        }
        coverageNavigationText = "Current sector \(current + 1)/\(coverageSectors.count) -> target \(target + 1)/\(coverageSectors.count)\(targetBandHint)"
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
        } else if lastKeyframeDecision.contains("fast_motion") {
            colmapCoachStatus = "Fast motion"
            colmapCoachAction = "Slow down"
            colmapCoachDetail = "Move and pan slowly so sharp overlapping frames can land."
            colmapCoachScore = 0.35
        } else if lastKeyframeDecision.contains("low_blur_score") {
            colmapCoachStatus = "Motion blur"
            colmapCoachAction = "Hold steady for haptic"
            colmapCoachDetail = "Let one sharp frame land before moving again."
            colmapCoachScore = 0.35
        } else if lastKeyframeDecision.contains("weak_feature_distribution") {
            colmapCoachStatus = "Weak feature spread"
            colmapCoachAction = "Aim at more texture"
            colmapCoachDetail = "Include corners, shelves, posters, table edges, or textured objects across the frame."
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
        } else if lastKeyframeDecision.contains("fast_motion") {
            roomQualityText = "Slow down for sharp frames"
        } else if lastKeyframeDecision.contains("low_blur_score") {
            roomQualityText = "Hold steadier on textured surfaces"
        } else if lastKeyframeDecision.contains("weak_feature_distribution") {
            roomQualityText = "Aim at spread-out texture"
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
        if usesSubjectTargetGuidance, let target = lockedObjectWorldPosition {
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
            "coverage_model": usesSubjectTargetGuidance && isObjectTargetLocked
                ? "target_relative_azimuth_elevation_distance_v0.2"
                : "coarse_yaw_sector_capture_guidance",
            "sector_count": coverageSectors.count,
            "observation_target_per_sector": coverageObservationTarget,
            "covered_sector_count": coveredSectorCount(),
            "partial_sector_count": partialSectorCount(),
            "missing_sector_count": missingSectorCount,
            "coverage_hint": coverageHintText,
            "capture_intent": captureIntent,
            "capture_intent_label": currentCaptureIntentOption.title,
            "readiness_state": readinessState,
            "next_action": nextAction,
            "background_warning": backgroundWarning,
            "current_sector_index": currentCoverageSector,
            "target_sector_index": targetCoverageSector,
            "coverage_navigation": coverageNavigationText,
            "sector_observation_counts": coverageSectorCounts,
            "sector_progress": coverageSectors,
            "target_relative_coverage": [
                "enabled": usesSubjectTargetGuidance && isObjectTargetLocked,
                "azimuth_sector_count": coverageSectors.count,
                "elevation_band_labels": ["under_10_deg", "10_to_30_deg", "over_30_deg"],
                "elevation_band_observations": targetElevationBandCounts,
                "distance_band_labels": ["close_under_0_85x", "normal_0_85x_to_1_25x", "wide_over_1_25x"],
                "distance_band_observations": targetDistanceBandCounts,
                "guidance_only": true,
            ],
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
            "capture_model": "quality_gated_smart_keyframes",
            "minimum_keyframe_interval_seconds": activeMinimumKeyframeInterval,
            "max_captured_frames": activeMaxCapturedFrames,
            "continuous_video_target_fps": videoRecorder.targetFPS,
            "continuous_video_sampling_policy": "thermal_adaptive_15_10_6",
            "keyframe_score_threshold": activeKeyframeScoreThreshold,
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
            profileName = "video_3dgs_max"
            profileModel = "video_style_rgbd_max_keyframe_stream_v0.2"
        default:
            profileName = "object_rgbd_keyframes"
            profileModel = "object_lock_extent_foreground_support_v0.1"
        }
        return [
            "schema": "capture_splat.profile_report.v0.1",
            "scan_target_mode": scanTargetMode,
            "capture_mode": captureModeLabel(),
            "capture_intent": captureIntent,
            "capture_intent_label": currentCaptureIntentOption.title,
            "capture_intent_guidance": currentCaptureIntentOption.guidance,
            "profile_name": profileName,
            "profile_model": profileModel,
            "profile_text": captureProfileText,
            "profile_detail": captureProfileDetail,
            "minimum_keyframe_interval_seconds": activeMinimumKeyframeInterval,
            "max_captured_frames": activeMaxCapturedFrames,
            "keyframe_score_threshold": activeKeyframeScoreThreshold,
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

    private func capturePolicyReport() -> [String: Any] {
        [
            "schema": "capture_splat.capture_policy.v0.1",
            "profile": captureProfileLabel(),
            "capture_intent": captureIntent,
            "accepted_frame_target": activeMaxCapturedFrames,
            "minimum_keyframe_interval_seconds": activeMinimumKeyframeInterval,
            "continuous_video_target_fps": videoRecorder.targetFPS,
            "continuous_video_sampling_policy": "thermal_adaptive_15_10_6",
            "quality_gate": [
                "keyframe_score_threshold": activeKeyframeScoreThreshold,
                "min_blur_score": minBlurScore,
                "min_feature_grid_coverage": minFeatureGridCoverage,
                "min_exposure_mean": minExposureMean,
                "max_exposure_mean": maxExposureMean,
                "max_exposure_jump": maxExposureJump,
                "max_clipped_highlight_fraction": maxClippedHighlightFraction,
                "max_near_clipped_highlight_fraction": maxNearClippedHighlightFraction,
                "max_clipped_shadow_fraction": maxClippedShadowFraction,
                "max_angular_velocity_deg_s": maxAngularVelocityDegPerSec,
                "max_translation_speed_m_s": maxTranslationSpeedMetersPerSec,
            ],
            "selection_policy": "quality_gated_rgbd_keyframes_plus_continuous_video",
            "rejected_frames_are_trainer_input": false,
            "authority": [
                "capture_guidance_only": true,
                "quality_claim": false,
            ],
        ]
    }

    private func sensorCapabilitiesReport() -> [String: Any] {
        [
            "schema": "capture_splat.sensor_capabilities.v0.1",
            "scene_depth": [
                "supported": ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth),
                "requested": true,
            ],
            "person_segmentation_with_depth": [
                "supported": ARWorldTrackingConfiguration.supportsFrameSemantics(.personSegmentationWithDepth),
                "requested": true,
                "status": ARWorldTrackingConfiguration.supportsFrameSemantics(.personSegmentationWithDepth)
                    ? "enabled_quality_first"
                    : "unsupported_rgbd_fallback",
                "sample_rate_cap_hz": 5,
                "mask_count_cap": maxPersonMasks,
            ],
            "scene_reconstruction": [
                "mesh_supported": ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh),
                "mesh_classification_supported": ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification),
                "requested": true,
                "status": ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
                    ? "enabled_quality_first"
                    : "unsupported_rgbd_fallback",
                "vertex_count_cap": 200_000,
                "triangle_count_cap": 300_000,
            ],
            "plane_detection": [
                "horizontal_requested": true,
                "vertical_requested": true,
            ],
            "fallback_policy": "preserve_rgbd_capture_and_report_unavailable_optional_sensors",
        ]
    }

    private func spatialGuidanceReport() -> [String: Any] {
        let path = spatialGuidancePath.filter { $0.x.isFinite && $0.z.isFinite }
        let pathLength = zip(path, path.dropFirst()).reduce(0.0) { partial, pair in
            partial + Double(simd_distance(
                SIMD2<Float>(pair.0.x, pair.0.z),
                SIMD2<Float>(pair.1.x, pair.1.z)
            ))
        }
        let loopClosed: Bool
        if let first = path.first, let last = path.last, pathLength >= 3 {
            loopClosed = simd_distance(
                SIMD2<Float>(first.x, first.z),
                SIMD2<Float>(last.x, last.z)
            ) <= 1
        } else {
            loopClosed = false
        }
        let coveredCells = spatialGuidanceCells.filter(\.covered)
        var classCoverage: [String: [String: Int]] = [:]
        for cell in spatialGuidanceCells {
            classCoverage[cell.classification, default: ["observed": 0, "covered": 0]]["observed", default: 0] += 1
            if cell.covered {
                classCoverage[cell.classification, default: ["observed": 0, "covered": 0]]["covered", default: 0] += 1
            }
        }
        let durations = spatialGuidanceUpdateDurationsMs.filter(\.isFinite).sorted()
        let averageDuration = durations.isEmpty ? 0 : durations.reduce(0, +) / Double(durations.count)
        let p95Index = durations.isEmpty ? 0 : min(Int((Double(durations.count - 1) * 0.95).rounded(.up)), durations.count - 1)
        let p95Duration = durations.isEmpty ? 0 : durations[p95Index]
        let sourceTriangleCount = meshAnchors.values.reduce(0) { $0 + $1.geometry.faces.count }

        return [
            "schema": "capture_splat.spatial_guidance.v0.2",
            "capture_intent": captureIntent,
            "capability_mode": spatialGuidanceMode,
            "guidance_visible": isSpatialGuidanceVisible,
            "capabilities": [
                "lidar_scene_depth": ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth),
                "scene_mesh": ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh),
                "mesh_classification": ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification),
                "plane_detection": session != nil,
                "room_plan": RoomCaptureSession.isSupported,
            ],
            "geometry": [
                "source_mesh_anchor_count": meshAnchors.count,
                "source_plane_anchor_count": planeAnchors.count,
                "source_triangle_count": sourceTriangleCount,
                "preview_renderer": "realitykit_scene_understanding",
                "preview_face_budget": spatialGuidanceFaceBudget,
                "renderer_managed_triangle_count": true,
            ],
            "coverage": [
                "cell_size_meters": spatialGuidanceCellSizeMeters,
                "observed_cell_count": spatialGuidanceCells.count,
                "accepted_keyframe_covered_cell_count": coveredCells.count,
                "accepted_keyframe_coverage_ratio": spatialGuidanceCells.isEmpty
                    ? 0
                    : Double(coveredCells.count) / Double(spatialGuidanceCells.count),
                "surface_classes": classCoverage,
            ],
            "trajectory": [
                "sample_count": path.count,
                "length_meters": pathLength,
                "loop_closed": loopClosed,
            ],
            "performance": [
                "target_update_hz": spatialGuidanceUpdateHz,
                "received_anchor_update_count": spatialGuidanceReceivedUpdateCount,
                "update_count": spatialGuidanceUpdateCount,
                "dropped_update_count": spatialGuidanceDroppedUpdateCount,
                "throttled_update_count": spatialGuidanceThrottledUpdateCount,
                "policy_disabled_update_count": spatialGuidancePolicyDisabledUpdateCount,
                "over_budget_processing_count": spatialGuidanceOverBudgetProcessingCount,
                "processing_budget_ms": spatialGuidanceUpdateBudgetMs,
                "average_update_ms": averageDuration,
                "p95_update_ms": p95Duration,
            ],
            "thermal_summary": [
                "capture_duration_seconds": max(
                    0,
                    (spatialGuidanceCaptureEndUptime ?? ProcessInfo.processInfo.systemUptime)
                        - (spatialGuidanceCaptureStartUptime ?? ProcessInfo.processInfo.systemUptime)
                ),
                "thermal_state_seconds": spatialGuidanceThermalDurationsSeconds,
                "guidance_policy_seconds": spatialGuidancePolicyDurationsSeconds,
                "render_state_seconds": spatialGuidanceRenderStateDurationsSeconds,
                "mesh_preview_visible_seconds": spatialGuidanceRenderStateDurationsSeconds["mesh_visible", default: 0],
                "map_only_seconds": spatialGuidanceRenderStateDurationsSeconds["map_only", default: 0],
                "final_render_state": currentSpatialGuidanceRenderState ?? "unavailable",
                "mesh_pause_reason": lastSpatialGuidanceMeshPauseReason,
            ],
            "thermal_transitions": spatialGuidanceThermalTransitions,
            "room_plan": [
                "status": roomPlanFile == nil ? roomPlanStatus : "exported",
                "usdz_written": roomPlanFile != nil,
                "semantics_written": roomPlanSemanticsFile != nil,
            ],
            "authority": [
                "measurement": false,
                "collision": false,
                "semantic_ground_truth": false,
                "navigation": false,
                "quality": false,
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
                "max_angular_velocity_deg_s": maxAngularVelocityDegPerSec,
                "max_translation_speed_m_s": maxTranslationSpeedMetersPerSec,
                "min_exposure_mean": minExposureMean,
                "max_exposure_mean": maxExposureMean,
                "max_exposure_jump": maxExposureJump,
                "max_clipped_highlight_fraction": maxClippedHighlightFraction,
                "max_near_clipped_highlight_fraction": maxNearClippedHighlightFraction,
                "max_clipped_shadow_fraction": maxClippedShadowFraction,
                "min_feature_grid_coverage": minFeatureGridCoverage,
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
        let lockTimestamp: Any = targetLockTimestamp.map { $0 } ?? NSNull()
        let depthSpread: Any = targetLockDepthSpreadMeters.map { $0 } ?? NSNull()
        let lockedDistance: Any = lockedObjectDistanceMeters.map { $0 } ?? NSNull()
        return [
            "schema": "capture_splat.target_lock_report.v0.1",
            "scan_target_mode": scanTargetMode,
            "object_locked": isObjectTargetLocked,
            "room_locked": isRoomTargetLocked,
            "target_lock_status": targetLockStatus,
            "target_lock_detail": targetLockDetail,
            "target_lock_distance": targetLockDistanceText,
            "object_world_position": objectPosition,
            "target_lock_acquisition": targetLockAcquisition,
            "target_lock_timestamp": lockTimestamp,
            "target_lock_sample_count": targetLockSampleCount,
            "target_lock_depth_spread_meters": depthSpread,
            "locked_distance_meters": lockedDistance,
            "object_extent_locked": isObjectExtentLocked,
            "coverage_model": usesSubjectTargetGuidance && isObjectTargetLocked
                ? "target_relative_azimuth_elevation_distance_v0.2"
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
            "matte_model": "pose_adjusted_object_extent_depth_band_v0.2",
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
        guard frameQuality.angularVelocityDegPerSec <= maxAngularVelocityDegPerSec,
              frameQuality.translationSpeedMetersPerSec <= maxTranslationSpeedMetersPerSec else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "fast_motion",
                score: max(0, 1.0 - frameQuality.angularVelocityDegPerSec / (maxAngularVelocityDegPerSec * 2)),
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
        let worstClippedFraction = max(frameQuality.clippedHighlightFraction, frameQuality.clippedShadowFraction)
        guard frameQuality.clippedHighlightFraction <= maxClippedHighlightFraction,
              frameQuality.clippedShadowFraction <= maxClippedShadowFraction else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "clipped_exposure",
                score: max(0, 1.0 - worstClippedFraction),
                sectorIndex: sectorIndex,
                frameQuality: frameQuality
            )
        }
        guard frameQuality.nearClippedHighlightFraction <= maxNearClippedHighlightFraction else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "near_clipped_highlights",
                score: max(0, 1.0 - frameQuality.nearClippedHighlightFraction),
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
        guard frameQuality.featureGridCoverage >= minFeatureGridCoverage else {
            return KeyframeDecision(
                shouldCapture: false,
                reason: "weak_feature_distribution",
                score: frameQuality.featureGridCoverage,
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
        if scanTargetMode != "video_3dgs", sectorProgress >= 1, missingSectorCount > 0, moved < 0.18 {
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
        let scoreThreshold = activeKeyframeScoreThreshold
        return KeyframeDecision(
            shouldCapture: score >= scoreThreshold,
            reason: score >= scoreThreshold ? "useful_keyframe" : "score_below_threshold",
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
            "clipped_highlight_fraction": decision.clippedHighlightFraction,
            "near_clipped_highlight_fraction": decision.nearClippedHighlightFraction,
            "clipped_shadow_fraction": decision.clippedShadowFraction,
            "feature_grid_coverage": decision.featureGridCoverage,
            "parallax_meters": decision.parallaxMeters,
            "angular_velocity_deg_s": decision.angularVelocityDegPerSec,
            "translation_speed_m_s": decision.translationSpeedMetersPerSec,
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
            format: "blur %.3f | exp %.2f | move %.2fm | rot %.0f deg/s",
            decision.blurScore,
            decision.exposureMean,
            decision.parallaxMeters,
            decision.angularVelocityDegPerSec
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
        case "fast_motion":
            captureBlockerStatus = "Fast motion"
            captureBlockerDetail = "Slow down; let one sharp frame land before moving on."
        case "low_blur_score":
            captureBlockerStatus = "Motion blur"
            captureBlockerDetail = "Hold steady on textured detail until you feel a haptic."
        case "weak_feature_distribution":
            captureBlockerStatus = "Weak feature spread"
            captureBlockerDetail = "Aim at textured edges, corners, shelves, or objects; blank areas need nearby detail."
        case "clipped_exposure", "near_clipped_highlights":
            captureBlockerStatus = "Clipped exposure"
            captureBlockerDetail = "Angle away from bright windows or dark corners; clipped areas have no texture."
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
            "clipped_highlight_fraction": decision.clippedHighlightFraction,
            "near_clipped_highlight_fraction": decision.nearClippedHighlightFraction,
            "clipped_shadow_fraction": decision.clippedShadowFraction,
            "feature_grid_coverage": decision.featureGridCoverage,
            "parallax_meters": decision.parallaxMeters,
            "angular_velocity_deg_s": decision.angularVelocityDegPerSec,
            "translation_speed_m_s": decision.translationSpeedMetersPerSec,
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
            if !roomLoopClosed, roomPathLengthMeters >= 1.5, distanceFromStart <= 0.75 {
                roomLoopClosed = true
                appendSessionEvent("loop_closed", details: [
                    "path_length_meters": roomPathLengthMeters,
                    "distance_from_start_meters": distanceFromStart,
                ])
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
        guard usesSubjectTargetGuidance,
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
        let projectedDepth = projection["optical_depth_meters"] as? Double
        let currentCenterDepth = projectedDepth.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
            ?? extent.centerDepthMeters
        let nearRadius = max(0.01, extent.centerDepthMeters - extent.depthMinMeters)
        let farRadius = max(0.01, extent.depthMaxMeters - extent.centerDepthMeters)
        let minDepth = max(0.01, currentCenterDepth - nearRadius - depthMargin)
        let maxDepth = currentCenterDepth + farRadius + depthMargin
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
            "depth_band_model": projectedDepth == nil
                ? "locked_center_fallback"
                : "pose_adjusted_projected_center",
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

    private func updateMotionRate(from frame: ARFrame) {
        defer {
            lastMotionSampleTransform = frame.camera.transform
            lastMotionSampleTimestamp = frame.timestamp
        }
        guard let previousTransform = lastMotionSampleTransform,
              let previousTimestamp = lastMotionSampleTimestamp else { return }
        let dt = frame.timestamp - previousTimestamp
        guard dt > 0.001, dt <= 0.5 else { return }
        let delta = simd_quatf(rotationMatrix(frame.camera.transform)) * simd_quatf(rotationMatrix(previousTransform)).inverse
        var angleRadians = Double(delta.angle)
        guard angleRadians.isFinite else { return }
        if angleRadians > .pi {
            angleRadians = 2 * .pi - angleRadians
        }
        let angularVelocity = angleRadians * 180.0 / .pi / dt
        let translationSpeed = Double(simd_distance(
            cameraPosition(frame.camera.transform),
            cameraPosition(previousTransform)
        )) / dt
        guard angularVelocity.isFinite, translationSpeed.isFinite else { return }
        let alpha = 0.3
        smoothedAngularVelocityDegPerSec += alpha * (angularVelocity - smoothedAngularVelocityDegPerSec)
        smoothedTranslationSpeedMetersPerSec += alpha * (translationSpeed - smoothedTranslationSpeedMetersPerSec)
    }

    private func rotationMatrix(_ transform: simd_float4x4) -> simd_float3x3 {
        simd_float3x3(
            SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            SIMD3<Float>(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
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
            clippedHighlightFraction: imageQuality.clippedHighlightFraction,
            nearClippedHighlightFraction: imageQuality.nearClippedHighlightFraction,
            clippedShadowFraction: imageQuality.clippedShadowFraction,
            featureGridCoverage: imageQuality.featureGridCoverage,
            parallaxMeters: parallax,
            angularVelocityDegPerSec: smoothedAngularVelocityDegPerSec,
            translationSpeedMetersPerSec: smoothedTranslationSpeedMetersPerSec,
            pathLengthMeters: projectedPath,
            distanceFromStartMeters: distanceFromStart,
            loopClosureCandidate: loopCandidate
        )
    }

    private func estimateImageQuality(
        from pixelBuffer: CVPixelBuffer
    ) -> (
        blurScore: Double,
        exposureMean: Double,
        clippedHighlightFraction: Double,
        nearClippedHighlightFraction: Double,
        clippedShadowFraction: Double,
        featureGridCoverage: Double
    ) {
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
            return (0, 0.5, 0, 0, 0, 0)
        }

        let base = baseAddress.assumingMemoryBound(to: UInt8.self)
        let stepX = max(width / 32, 8)
        let stepY = max(height / 32, 8)
        let gridColumns = 4
        let gridRows = 4
        var sampleCount = 0
        var exposureSum = 0.0
        var edgeSum = 0.0
        var clippedHighlightCount = 0
        var nearClippedHighlightCount = 0
        var clippedShadowCount = 0
        var featureGridCells = Array(repeating: false, count: gridColumns * gridRows)

        for y in stride(from: stepY, to: max(height - 1, stepY + 1), by: stepY) {
            for x in stride(from: stepX, to: max(width - 1, stepX + 1), by: stepX) {
                let offset = y * bytesPerRow + x
                let value = Double(base[offset])
                let normalizedValue = value / 255.0
                let right = Double(base[y * bytesPerRow + min(x + 1, width - 1)])
                let down = Double(base[min(y + 1, height - 1) * bytesPerRow + x])
                let edgeSignal = (abs(value - right) + abs(value - down)) / 510.0
                exposureSum += normalizedValue
                edgeSum += edgeSignal
                if edgeSignal >= 0.025 {
                    let cellX = min((x * gridColumns) / max(width, 1), gridColumns - 1)
                    let cellY = min((y * gridRows) / max(height, 1), gridRows - 1)
                    featureGridCells[cellY * gridColumns + cellX] = true
                }
                if normalizedValue > 0.98 {
                    clippedHighlightCount += 1
                }
                if normalizedValue > 0.95 {
                    nearClippedHighlightCount += 1
                }
                if normalizedValue < 0.02 {
                    clippedShadowCount += 1
                }
                sampleCount += 1
            }
        }

        guard sampleCount > 0 else { return (0, 0.5, 0, 0, 0, 0) }
        let samples = Double(sampleCount)
        let coveredCells = featureGridCells.filter { $0 }.count
        return (
            edgeSum / samples,
            exposureSum / samples,
            Double(clippedHighlightCount) / samples,
            Double(nearClippedHighlightCount) / samples,
            Double(clippedShadowCount) / samples,
            Double(coveredCells) / Double(featureGridCells.count)
        )
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
        guard usesSubjectTargetGuidance else {
            objectExtentStatus = "Extent not used"
            objectExtentDetail = "Object extent applies to subject-focused captures."
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
            objectExtentDetail = "Center the object, then tap Lock Subject."
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

    private func schedulePersonMask(from frame: ARFrame) {
        guard isRecording,
              frame.timestamp - lastPersonMaskSampleTimestamp >= personMaskMinimumInterval,
              personMaskScheduledCount < maxPersonMasks else {
            return
        }
        lastPersonMaskSampleTimestamp = frame.timestamp
        guard let buffer = frame.segmentationBuffer,
              let snapshot = copyPersonMask(buffer),
              snapshot.personFraction >= 0.001 else {
            return
        }
        guard !isWritingPersonMask, let directory = currentSessionDirectory else {
            personMaskDroppedCount += 1
            return
        }
        isWritingPersonMask = true
        personMaskScheduledCount += 1
        let maskNumber = personMaskScheduledCount
        let relativePath = String(format: "masks/person/%06d.png", maskNumber)
        let url = directory.appendingPathComponent(relativePath)
        let timestamp = frame.timestamp
        let videoFrameIndex = max(videoRecorder.appendedFrameCount - 1, 0)

        maskWriteQueue.async { [weak self] in
            guard let self else { return }
            let pngData = self.personMaskPNGData(snapshot)
            let wrote = pngData.map { data in
                (try? data.write(to: url, options: .atomic)) != nil
            } ?? false
            DispatchQueue.main.async {
                self.isWritingPersonMask = false
                if wrote {
                    self.personMaskWrittenCount += 1
                    self.personMaskRecords.append([
                        "path": relativePath,
                        "ar_timestamp": timestamp,
                        "width": snapshot.width,
                        "height": snapshot.height,
                        "person_fraction": snapshot.personFraction,
                        "nearest_video_frame_idx": videoFrameIndex,
                        "authority": "mask_proposal",
                    ])
                } else {
                    self.personMaskDroppedCount += 1
                }
            }
        }
    }

    private func copyPersonMask(_ pixelBuffer: CVPixelBuffer) -> PersonMaskSnapshot? {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
        guard width > 0,
              height > 0,
              bytesPerRow >= width,
              let baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer) else {
            return nil
        }
        let bytes = Data(bytes: baseAddress, count: bytesPerRow * height)
        let pointer = baseAddress.assumingMemoryBound(to: UInt8.self)
        var personPixels = 0
        for y in 0..<height {
            let row = pointer.advanced(by: y * bytesPerRow)
            for x in 0..<width where row[x] > 0 {
                personPixels += 1
            }
        }
        return PersonMaskSnapshot(
            bytes: bytes,
            width: width,
            height: height,
            bytesPerRow: bytesPerRow,
            personFraction: Double(personPixels) / Double(width * height)
        )
    }

    private func personMaskPNGData(_ snapshot: PersonMaskSnapshot) -> Data? {
        guard let provider = CGDataProvider(data: snapshot.bytes as CFData),
              let image = CGImage(
                width: snapshot.width,
                height: snapshot.height,
                bitsPerComponent: 8,
                bitsPerPixel: 8,
                bytesPerRow: snapshot.bytesPerRow,
                space: CGColorSpaceCreateDeviceGray(),
                bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                provider: provider,
                decode: nil,
                shouldInterpolate: false,
                intent: .defaultIntent
              ) else {
            return nil
        }
        return UIImage(cgImage: image).pngData()
    }

    private func nearestPersonMaskPath(to timestamp: TimeInterval) -> String? {
        for record in personMaskRecords.reversed() {
            guard let maskTimestamp = record["ar_timestamp"] as? TimeInterval else { continue }
            if abs(maskTimestamp - timestamp) <= 0.12 {
                return record["path"] as? String
            }
            if timestamp - maskTimestamp > 0.12 { break }
        }
        return nil
    }
}

extension CaptureController: RoomCaptureSessionDelegate {
    func captureSession(_ session: RoomCaptureSession, didUpdate room: CapturedRoom) {
        DispatchQueue.main.async { [weak self, weak session] in
            guard let self, let session, session === self.sharedRoomCaptureSession else { return }
            self.updateRoomPlanPreview(room: room)
        }
    }

    func captureSession(_ session: RoomCaptureSession, didProvide instruction: RoomCaptureSession.Instruction) {
        DispatchQueue.main.async { [weak self, weak session] in
            guard let self, let session, session === self.sharedRoomCaptureSession else { return }
            self.noteRoomPlanInstruction(instruction)
        }
    }

    func captureSession(
        _ session: RoomCaptureSession,
        didEndWith data: CapturedRoomData,
        error: Error?
    ) {
        DispatchQueue.main.async { [weak self, weak session] in
            guard let self, let session, session === self.sharedRoomCaptureSession,
                  let generation = self.sharedRoomPlanGeneration,
                  let directory = self.sharedRoomPlanDirectory else { return }
            if let error {
                self.noteRoomPlanFailure(error.localizedDescription)
                self.appendSessionEvent("room_plan_shared_held", details: ["reason": error.localizedDescription])
                self.finishSharedRoomPlan()
                return
            }

            Task { @MainActor [weak self] in
                guard let self else { return }
                do {
                    let builder = RoomBuilder(options: [.beautifyObjects])
                    let room = try await builder.capturedRoom(from: data)
                    guard self.sharedRoomPlanGeneration == generation else { return }
                    try self.writeRoomPlanAssets(room: room, directory: directory)
                    let summary = self.roomPlanSummary(room)
                    self.roomPlanStatus = "RoomPlan exported"
                    self.roomPlanDetail = summary.detail
                    self.roomPlanSummaryText = summary.shortText
                    self.appendSessionEvent("room_plan_shared_exported", details: [
                        "walls": room.walls.count,
                        "objects": room.objects.count,
                    ])
                } catch {
                    guard self.sharedRoomPlanGeneration == generation else { return }
                    self.noteRoomPlanFailure(error.localizedDescription)
                    self.appendSessionEvent("room_plan_shared_held", details: ["reason": error.localizedDescription])
                }
                self.finishSharedRoomPlan()
            }
        }
    }
}

extension CaptureController: ARSessionDelegate {
    func session(_ session: ARSession, didFailWithError error: Error) {
        logger.error("AR session failed: \(error.localizedDescription, privacy: .public)")
        appendSessionEvent("arkit_session_failed", details: [
            "error": error.localizedDescription,
        ])
        statusText = "AR session failed. Finalizing available capture evidence."
        if isRecording {
            stopRecording()
        }
    }

    func sessionWasInterrupted(_ session: ARSession) {
        logger.notice("AR session interrupted")
        appendSessionEvent("arkit_session_interrupted", arTimestamp: lastFrameTimestamp)
        if isRecording {
            captureBlockerStatus = "Hold"
            captureBlockerDetail = "AR tracking was interrupted. Hold still while tracking recovers."
        }
    }

    func sessionInterruptionEnded(_ session: ARSession) {
        logger.notice("AR session interruption ended; waiting for tracking recovery")
        appendSessionEvent("arkit_session_interruption_ended", arTimestamp: lastFrameTimestamp)
        if isRecording {
            captureBlockerStatus = "Hold"
            captureBlockerDetail = "Tracking is recovering. Keep the phone still until guidance clears."
        }
    }

    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) {
        for anchor in anchors {
            if let plane = anchor as? ARPlaneAnchor {
                planeAnchors[plane.identifier] = plane
            }
            if let mesh = anchor as? ARMeshAnchor {
                meshAnchors[mesh.identifier] = mesh
                if isRecording { recordedMeshAnchorIDs.insert(mesh.identifier) }
            }
            recordSpatialGuidanceAnchor(anchor)
        }
    }

    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) {
        for anchor in anchors {
            if let plane = anchor as? ARPlaneAnchor {
                planeAnchors[plane.identifier] = plane
            }
            if let mesh = anchor as? ARMeshAnchor {
                meshAnchors[mesh.identifier] = mesh
                if isRecording { recordedMeshAnchorIDs.insert(mesh.identifier) }
            }
            recordSpatialGuidanceAnchor(anchor)
        }
    }

    func session(_ session: ARSession, didRemove anchors: [ARAnchor]) {
        for anchor in anchors {
            if anchor is ARPlaneAnchor {
                planeAnchors.removeValue(forKey: anchor.identifier)
                spatialPlaneCells.removeValue(forKey: anchor.identifier)
            }
            if anchor is ARMeshAnchor {
                meshAnchors.removeValue(forKey: anchor.identifier)
                recordedMeshAnchorIDs.remove(anchor.identifier)
                spatialAnchorCells.removeValue(forKey: anchor.identifier)
            }
            lastSpatialAnchorUpdateTimestamp.removeValue(forKey: anchor.identifier)
        }
        publishSpatialGuidanceCells()
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let currentTrackingState = trackingStateText(frame.camera.trackingState)
        if trackingStatus != currentTrackingState {
            trackingStatus = currentTrackingState
        }
        if isRecording, currentTrackingState != lastRecordedTrackingState {
            appendSessionEvent("tracking_state_changed", arTimestamp: frame.timestamp, details: [
                "tracking_state": currentTrackingState,
            ])
            lastRecordedTrackingState = currentTrackingState
        }
        latestFeaturePointCount = frame.rawFeaturePoints?.points.count ?? 0
        updateMotionRate(from: frame)
        if isRecording, currentSessionDirectory != nil {
            videoRecorder.append(frame: frame, captureDevice: Self.primaryCaptureDevice)
            schedulePersonMask(from: frame)
        }
        let baseGuidanceInterval = liveGuidanceInterval(for: ProcessInfo.processInfo.thermalState)
        let guidanceInterval = usesSubjectTargetGuidance && !isObjectTargetLocked
            ? min(baseGuidanceInterval, 0.2)
            : baseGuidanceInterval
        let shouldRefreshLiveGuidance = frame.timestamp - lastLiveGuidanceTimestamp >= guidanceInterval
        if shouldRefreshLiveGuidance {
            lastLiveGuidanceTimestamp = frame.timestamp
            updateSpatialGuidancePose(from: frame)
        }
        guard let sceneDepth = frame.sceneDepth else {
            if shouldRefreshLiveGuidance {
                guidancePoints.removeAll()
                if isRecording {
                    droppedFrames += 1
                    statusText = "Waiting for LiDAR scene depth"
                }
                updateGuidance()
            }
            return
        }
        var candidateDepthValidRatio = validDepthRatio
        if shouldRefreshLiveGuidance {
            candidateDepthValidRatio = measureValidDepthRatio(sceneDepth.depthMap)
            validDepthRatio = candidateDepthValidRatio
            updateTargetCandidate(from: frame, depthMap: sceneDepth.depthMap)
            updateLiveGuidance(from: frame, depthMap: sceneDepth.depthMap)
            updateGuidance()
        }
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
        if !shouldRefreshLiveGuidance {
            candidateDepthValidRatio = measureValidDepthRatio(sceneDepth.depthMap)
            validDepthRatio = candidateDepthValidRatio
        }
        guard scheduledFrameCount < activeMaxCapturedFrames else {
            stopAtUsefulFrameLimit()
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
            var previewStatus: (count: Int, url: URL?)?
            autoreleasepool {
                do {
                    try self.writeJPEG(from: rgbBuffer, to: rgbURL)
                    try self.writeDepth(depthBuffer, to: depthURL)
                    if let confidenceBuffer {
                        try self.writeConfidence(confidenceBuffer, to: confidenceURL)
                    }
                    let previewSamples = self.makePointCloudPreviewSamples(
                        depthMap: depthBuffer,
                        rgbBuffer: rgbBuffer,
                        transform: transform,
                        intrinsics: intrinsics,
                        frameIndex: index
                    )
                    previewStatus = self.appendPointCloudPreviewSamples(
                        previewSamples,
                        directory: directory,
                        frameIndex: index
                    )
                } catch {
                    writeError = error
                }
            }
            if let writeError {
                DispatchQueue.main.async {
                    self.scheduledFrameCount = max(self.scheduledFrameCount - 1, 0)
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
                    trackingState: trackingState,
                    captureQuality: CaptureFrameQuality(
                        accepted: true,
                        reason: keyframeDecision.reason,
                        score: keyframeDecision.score,
                        blurScore: keyframeDecision.blurScore,
                        exposureMean: keyframeDecision.exposureMean,
                        exposureDelta: keyframeDecision.exposureDelta,
                        clippedHighlightFraction: keyframeDecision.clippedHighlightFraction,
                        nearClippedHighlightFraction: keyframeDecision.nearClippedHighlightFraction,
                        clippedShadowFraction: keyframeDecision.clippedShadowFraction,
                        featureGridCoverage: keyframeDecision.featureGridCoverage,
                        parallaxMeters: keyframeDecision.parallaxMeters,
                        angularVelocityDegPerSec: keyframeDecision.angularVelocityDegPerSec,
                        translationSpeedMetersPerSec: keyframeDecision.translationSpeedMetersPerSec,
                        colmapOverlapScore: keyframeDecision.colmapOverlapScore,
                        validDepthRatio: candidateDepthValidRatio,
                        featurePointCount: self.latestFeaturePointCount
                    ),
                    personMask: self.nearestPersonMaskPath(to: timestamp)
                ))
                self.rgbFrames += 1
                self.depthFrames += 1
                if let previewStatus {
                    self.pointCloudPreviewPointCount = previewStatus.count
                    self.pointCloudPreviewFile = previewStatus.url
                }
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
                self.recordAcceptedCoverage(
                    timestamp: timestamp,
                    transform: transform,
                    sectorIndex: keyframeDecision.sectorIndex
                )
                self.markSpatialGuidanceCoverage(
                    at: acceptedPosition,
                    forward: self.cameraForward(transform)
                )
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
                if self.acceptedKeyframes >= self.activeMaxCapturedFrames {
                    self.stopAtUsefulFrameLimit()
                }
            }
        }
    }

    private func stopAtUsefulFrameLimit() {
        guard !isAutomaticStopScheduled, isRecording, !isFinalizing else { return }
        isAutomaticStopScheduled = true
        automaticStopReason = "frame_limit"
        captureCompletionNotice = "\(acceptedKeyframes) useful frames reached. Finalizing..."
        statusText = "Useful-frame limit reached. Finalizing capture"
        appendSessionEvent("capture_auto_stopped", arTimestamp: lastFrameTimestamp, details: [
            "reason": "frame_limit",
            "accepted_frame_count": acceptedKeyframes,
            "max_captured_frames": activeMaxCapturedFrames,
        ])
        completionHaptic.notificationOccurred(.warning)
        stopRecording()
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

    private func makePointCloudPreviewSamples(
        depthMap: CVPixelBuffer,
        rgbBuffer: CVPixelBuffer,
        transform: simd_float4x4,
        intrinsics: CameraIntrinsics,
        frameIndex: Int
    ) -> [[String: Any]] {
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }

        let width = CVPixelBufferGetWidth(depthMap)
        let height = CVPixelBufferGetHeight(depthMap)
        guard width > 0,
              height > 0,
              intrinsics.flX != 0,
              intrinsics.flY != 0,
              let base = CVPixelBufferGetBaseAddress(depthMap)?.assumingMemoryBound(to: Float32.self) else {
            return []
        }

        let rgbLocked = CVPixelBufferLockBaseAddress(rgbBuffer, .readOnly) == kCVReturnSuccess
        defer {
            if rgbLocked {
                CVPixelBufferUnlockBaseAddress(rgbBuffer, .readOnly)
            }
        }
        let sampler = rgbLocked ? makeYCbCrSampler(fromLocked: rgbBuffer) : nil
        let columns = 12
        let rows = 9
        var samples: [[String: Any]] = []
        samples.reserveCapacity(columns * rows)

        for row in 0..<rows {
            for column in 0..<columns {
                let x = min(max((column * width) / columns + width / (columns * 2), 0), width - 1)
                let y = min(max((row * height) / rows + height / (rows * 2), 0), height - 1)
                let depth = base[y * width + x]
                guard depth.isFinite, depth > 0 else { continue }

                let cameraX = (Float(x) - intrinsics.cx) / intrinsics.flX * depth
                let cameraY = -((Float(y) - intrinsics.cy) / intrinsics.flY * depth)
                let world = transform * SIMD4<Float>(cameraX, cameraY, -depth, 1)
                guard world.x.isFinite, world.y.isFinite, world.z.isFinite else { continue }

                let normalizedX = Double(x) / Double(max(width - 1, 1))
                let normalizedY = Double(y) / Double(max(height - 1, 1))
                let color = sampler?.colorAt(normalizedX: normalizedX, normalizedY: normalizedY)
                    ?? fallbackPreviewColor(depth: Double(depth))
                samples.append([
                    "x": Double(world.x),
                    "y": Double(world.y),
                    "z": Double(world.z),
                    "r": Int(color.0),
                    "g": Int(color.1),
                    "b": Int(color.2),
                    "frame_index": frameIndex,
                ])
            }
        }
        return samples
    }

    private func appendPointCloudPreviewSamples(
        _ samples: [[String: Any]],
        directory: URL,
        frameIndex: Int
    ) -> (count: Int, url: URL?) {
        guard !samples.isEmpty else {
            return (pointCloudPreviewSamples.count, pointCloudPreviewFile)
        }
        pointCloudPreviewSamples.append(contentsOf: samples)
        if pointCloudPreviewSamples.count > maxPointCloudPreviewSamples {
            pointCloudPreviewSamples.removeFirst(pointCloudPreviewSamples.count - maxPointCloudPreviewSamples)
        }
        let url = directory.appendingPathComponent("pointcloud_preview", isDirectory: true).appendingPathComponent("preview.json")
        guard frameIndex.isMultiple(of: pointCloudPreviewCheckpointInterval) else {
            return (pointCloudPreviewSamples.count, pointCloudPreviewFile)
        }
        writePointCloudPreview(to: url)
        return (pointCloudPreviewSamples.count, url)
    }

    private func writePointCloudPreview(to url: URL) {
        writeJSON([
            "schema": "capture_splat.pointcloud_preview.v0.1",
            "point_count": pointCloudPreviewSamples.count,
            "max_point_count": maxPointCloudPreviewSamples,
            "coordinate_frame": "arkit_world_preview",
            "color_source": "rgb_sampled_when_available",
            "capture_guidance_only": true,
            "points": pointCloudPreviewSamples,
        ], to: url)
    }

    private func finalizePointCloudPreview() -> (count: Int, url: URL?) {
        guard let directory = currentSessionDirectory, !pointCloudPreviewSamples.isEmpty else {
            return (pointCloudPreviewSamples.count, pointCloudPreviewFile)
        }
        let url = directory
            .appendingPathComponent("pointcloud_preview", isDirectory: true)
            .appendingPathComponent("preview.json")
        writePointCloudPreview(to: url)
        return (pointCloudPreviewSamples.count, url)
    }

    private func makeYCbCrSampler(fromLocked pixelBuffer: CVPixelBuffer) -> YCbCrSampler? {
        guard CVPixelBufferGetPlaneCount(pixelBuffer) >= 2,
              let luma = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0)?
                .assumingMemoryBound(to: UInt8.self),
              let chroma = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 1)?
                .assumingMemoryBound(to: UInt8.self) else {
            return nil
        }
        return YCbCrSampler(
            width: CVPixelBufferGetWidthOfPlane(pixelBuffer, 0),
            height: CVPixelBufferGetHeightOfPlane(pixelBuffer, 0),
            lumaBytesPerRow: CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0),
            chromaWidth: CVPixelBufferGetWidthOfPlane(pixelBuffer, 1),
            chromaHeight: CVPixelBufferGetHeightOfPlane(pixelBuffer, 1),
            chromaBytesPerRow: CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 1),
            luma: UnsafePointer(luma),
            chroma: UnsafePointer(chroma)
        )
    }

    private func fallbackPreviewColor(depth: Double) -> (UInt8, UInt8, UInt8) {
        if depth < 1.25 {
            return (80, 220, 180)
        }
        if depth < 3.0 {
            return (80, 190, 255)
        }
        return (245, 170, 80)
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

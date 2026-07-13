import Foundation

struct CapturedFrame: Encodable {
    let rgb: String
    let depth: String
    let confidence: String?
    let timestamp: Double
    let transformMatrix: [[Float]]
    let intrinsics: CameraIntrinsics
    let trackingState: String
    let captureQuality: CaptureFrameQuality
    let personMask: String?

    enum CodingKeys: String, CodingKey {
        case rgb, depth, confidence, timestamp, intrinsics
        case transformMatrix = "transform_matrix"
        case trackingState = "tracking_state"
        case captureQuality = "capture_quality"
        case personMask = "person_mask"
    }
}

struct CaptureFrameQuality: Encodable {
    let accepted: Bool
    let reason: String
    let score: Double
    let blurScore: Double
    let exposureMean: Double
    let exposureDelta: Double
    let clippedHighlightFraction: Double
    let clippedShadowFraction: Double
    let featureGridCoverage: Double
    let parallaxMeters: Double
    let angularVelocityDegPerSec: Double
    let translationSpeedMetersPerSec: Double
    let colmapOverlapScore: Double
    let validDepthRatio: Double
    let featurePointCount: Int

    enum CodingKeys: String, CodingKey {
        case accepted, reason, score
        case blurScore = "blur_score"
        case exposureMean = "exposure_mean"
        case exposureDelta = "exposure_delta"
        case clippedHighlightFraction = "clipped_highlight_fraction"
        case clippedShadowFraction = "clipped_shadow_fraction"
        case featureGridCoverage = "feature_grid_coverage"
        case parallaxMeters = "parallax_meters"
        case angularVelocityDegPerSec = "angular_velocity_deg_s"
        case translationSpeedMetersPerSec = "translation_speed_m_s"
        case colmapOverlapScore = "colmap_overlap_score"
        case validDepthRatio = "valid_depth_ratio"
        case featurePointCount = "feature_point_count"
    }
}

struct CameraIntrinsics: Encodable {
    let w: Int
    let h: Int
    let flX: Float
    let flY: Float
    let cx: Float
    let cy: Float

    enum CodingKeys: String, CodingKey {
        case w, h, cx, cy
        case flX = "fl_x"
        case flY = "fl_y"
    }
}

struct SpatialGuidancePoint: Identifiable, Equatable {
    let id: String
    let x: Float
    let z: Float
    let classification: String
    let covered: Bool
}

struct SpatialGuidancePose: Equatable {
    let x: Float
    let z: Float
    let headingRadians: Float
}

struct SpatialGuidancePathPoint: Identifiable, Equatable {
    let id: Int
    let x: Float
    let z: Float
}

struct CaptureManifest: Encodable {
    let schema = "capture_splat.v0.3"
    let device: DeviceInfo
    let captureMode: String
    let captureProfile: String
    let captureIntent: String
    let depthMode: String
    let source = "CaptureSplat"
    let depthScale = 1.0
    let sessionConfig: SessionConfig
    let rgb: StreamReport
    let depth: StreamReport
    let intrinsics: CameraIntrinsics
    let imuFile = "imu.csv"
    let gpsFile = "gps.csv"
    let objectMatteFile = "metadata/object_matte_report.json"
    let roomCaptureQualityFile = "metadata/room_capture_quality_report.json"
    let captureProfileFile = "metadata/capture_profile_report.json"
    let capturePolicyFile = "metadata/capture_policy.json"
    let sensorCapabilitiesFile = "metadata/sensor_capabilities.json"
    let finalizationReportFile = "metadata/finalization_report.json"
    let sessionEventsFile = "metadata/session_events.jsonl"
    let pointCloudPreviewFile = "pointcloud_preview/preview.json"
    let spatialGuidanceReportFile: String?
    let personMaskIndexFile: String?
    let arkitMeshFile: String?
    let arkitMeshReportFile = "geometry/arkit_mesh_report.json"
    let videoFile: String?
    let frameIndexFile: String?
    let videoFrameCount: Int?
    let roomPlanFile: String?
    let roomPlanReportFile: String?
    let roomPlanSemanticsFile: String?
    let frames: [CapturedFrame]
    let authority: Authority

    enum CodingKeys: String, CodingKey {
        case schema, device, source, rgb, depth, intrinsics, frames, authority
        case captureMode = "capture_mode"
        case captureProfile = "capture_profile"
        case captureIntent = "capture_intent"
        case depthMode = "depth_mode"
        case depthScale = "depth_scale"
        case sessionConfig = "session_config"
        case imuFile = "imu_file"
        case gpsFile = "gps_file"
        case objectMatteFile = "object_matte_file"
        case roomCaptureQualityFile = "room_capture_quality_file"
        case captureProfileFile = "capture_profile_file"
        case capturePolicyFile = "capture_policy_file"
        case sensorCapabilitiesFile = "sensor_capabilities_file"
        case finalizationReportFile = "finalization_report_file"
        case sessionEventsFile = "session_events_file"
        case pointCloudPreviewFile = "pointcloud_preview_file"
        case spatialGuidanceReportFile = "spatial_guidance_report_file"
        case personMaskIndexFile = "person_mask_index_file"
        case arkitMeshFile = "arkit_mesh_file"
        case arkitMeshReportFile = "arkit_mesh_report_file"
        case videoFile = "video_file"
        case frameIndexFile = "frame_index_file"
        case videoFrameCount = "video_frame_count"
        case roomPlanFile = "room_plan_file"
        case roomPlanReportFile = "room_plan_report_file"
        case roomPlanSemanticsFile = "room_plan_semantics_file"
    }
}

struct SessionConfig: Encodable {
    let worldAlignment = "gravity"
    let upAxis: [Float] = [0, 1, 0]
    let scaleAuthority = "arkit_vio_metric"
    let aeLock: Bool
    let awbLock: Bool
    let focusLock: Bool
    let videoFormat: String?
    let videoTargetFPS: Int?
    let lens = "wide"

    enum CodingKeys: String, CodingKey {
        case lens
        case worldAlignment = "world_alignment"
        case upAxis = "up_axis"
        case scaleAuthority = "scale_authority"
        case aeLock = "ae_lock"
        case awbLock = "awb_lock"
        case focusLock = "focus_lock"
        case videoFormat = "video_format"
        case videoTargetFPS = "video_target_fps"
    }
}

struct DeviceInfo: Encodable {
    let model: String
    let osVersion: String
    let appVersion: String
    let buildNumber: String

    enum CodingKeys: String, CodingKey {
        case model
        case osVersion = "os_version"
        case appVersion = "app_version"
        case buildNumber = "build_number"
    }
}

struct StreamReport: Encodable {
    let format: String?
    let requestedFPS: Int
    let requestedResolution: Resolution
    let achievedFPS: Double
    let achievedResolution: Resolution
    let units: String?

    enum CodingKeys: String, CodingKey {
        case format, units
        case requestedFPS = "requested_fps"
        case requestedResolution = "requested_resolution"
        case achievedFPS = "achieved_fps"
        case achievedResolution = "achieved_resolution"
    }
}

struct Resolution: Encodable {
    let w: Int
    let h: Int
}

struct Authority: Encodable {
    let proposalOnly = true
    let metricAuthority = false
    let collisionGeometry = false
    let planningAuthority = false
    let semanticAuthority = false

    enum CodingKeys: String, CodingKey {
        case proposalOnly = "proposal_only"
        case metricAuthority = "metric_authority"
        case collisionGeometry = "collision_geometry"
        case planningAuthority = "planning_authority"
        case semanticAuthority = "semantic_authority"
    }
}

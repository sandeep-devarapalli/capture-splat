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

    enum CodingKeys: String, CodingKey {
        case rgb, depth, confidence, timestamp, intrinsics
        case transformMatrix = "transform_matrix"
        case trackingState = "tracking_state"
        case captureQuality = "capture_quality"
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

struct CaptureManifest: Encodable {
    let schema = "capture_splat.v0.1"
    let device: DeviceInfo
    let captureMode: String
    let depthMode: String
    let source = "CaptureSplat"
    let depthScale = 1.0
    let rgb: StreamReport
    let depth: StreamReport
    let intrinsics: CameraIntrinsics
    let imuFile = "imu.csv"
    let gpsFile = "gps.csv"
    let objectMatteFile = "metadata/object_matte_report.json"
    let roomCaptureQualityFile = "metadata/room_capture_quality_report.json"
    let captureProfileFile = "metadata/capture_profile_report.json"
    let pointCloudPreviewFile = "pointcloud_preview/preview.json"
    let roomPlanFile: String?
    let roomPlanReportFile: String?
    let frames: [CapturedFrame]
    let authority: Authority

    enum CodingKeys: String, CodingKey {
        case schema, device, source, rgb, depth, intrinsics, frames, authority
        case captureMode = "capture_mode"
        case depthMode = "depth_mode"
        case depthScale = "depth_scale"
        case imuFile = "imu_file"
        case gpsFile = "gps_file"
        case objectMatteFile = "object_matte_file"
        case roomCaptureQualityFile = "room_capture_quality_file"
        case captureProfileFile = "capture_profile_file"
        case pointCloudPreviewFile = "pointcloud_preview_file"
        case roomPlanFile = "room_plan_file"
        case roomPlanReportFile = "room_plan_report_file"
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

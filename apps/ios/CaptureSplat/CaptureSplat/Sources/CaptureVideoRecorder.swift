import ARKit
import AVFoundation
import CoreVideo
import Foundation
import OSLog

/// Records the continuous ARKit RGB stream to video/capture.mov and writes
/// metadata/frame_index.jsonl with one line per appended video frame so host
/// tools can extract hundreds of training frames with device pose priors.
final class CaptureVideoRecorder {
    struct FinishResult {
        let succeeded: Bool
        let status: String
        let error: String?
    }

    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var indexHandle: FileHandle?
    private var startTimestamp: TimeInterval?
    private var lastAppendedTimestamp: TimeInterval = -.infinity
    private let minimumFrameInterval: TimeInterval
    private let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "capture_splat",
        category: "video-recorder"
    )
    private(set) var appendedFrameCount = 0
    private(set) var droppedFrameCount = 0

    static let videoRelativePath = "video/capture.mov"
    static let frameIndexRelativePath = "metadata/frame_index.jsonl"

    init(targetFPS: Double = 30) {
        minimumFrameInterval = targetFPS > 0 ? (1.0 / targetFPS) * 0.9 : 0
    }

    var isActive: Bool { writer != nil }

    func start(in directory: URL) throws {
        guard writer == nil else {
            throw CocoaError(.fileWriteUnknown)
        }
        let videoDirectory = directory.appendingPathComponent("video", isDirectory: true)
        try FileManager.default.createDirectory(at: videoDirectory, withIntermediateDirectories: true)
        let indexURL = directory.appendingPathComponent(Self.frameIndexRelativePath)
        FileManager.default.createFile(atPath: indexURL.path, contents: nil)
        indexHandle = try FileHandle(forWritingTo: indexURL)
        let movURL = directory.appendingPathComponent(Self.videoRelativePath)
        try? FileManager.default.removeItem(at: movURL)
        writer = try AVAssetWriter(outputURL: movURL, fileType: .mov)
        startTimestamp = nil
        lastAppendedTimestamp = -.infinity
        appendedFrameCount = 0
        droppedFrameCount = 0
        logger.info("Video recorder prepared")
    }

    func append(frame: ARFrame, captureDevice: AVCaptureDevice?) {
        guard let writer else { return }
        let timestamp = frame.timestamp
        guard timestamp - lastAppendedTimestamp >= minimumFrameInterval else { return }
        let pixelBuffer = frame.capturedImage
        if input == nil {
            let settings: [String: Any] = [
                AVVideoCodecKey: AVVideoCodecType.hevc,
                AVVideoWidthKey: CVPixelBufferGetWidth(pixelBuffer),
                AVVideoHeightKey: CVPixelBufferGetHeight(pixelBuffer),
            ]
            let newInput = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
            newInput.expectsMediaDataInRealTime = true
            guard writer.canAdd(newInput) else {
                droppedFrameCount += 1
                logger.error("Asset writer rejected the video input")
                return
            }
            writer.add(newInput)
            let newAdaptor = AVAssetWriterInputPixelBufferAdaptor(
                assetWriterInput: newInput,
                sourcePixelBufferAttributes: nil
            )
            guard writer.startWriting() else {
                droppedFrameCount += 1
                logger.error("Asset writer failed to start: \(writer.error?.localizedDescription ?? "unknown", privacy: .public)")
                return
            }
            writer.startSession(atSourceTime: .zero)
            input = newInput
            adaptor = newAdaptor
            startTimestamp = timestamp
            logger.info(
                "Video writer started at \(CVPixelBufferGetWidth(pixelBuffer))x\(CVPixelBufferGetHeight(pixelBuffer))"
            )
        }
        guard let input, let adaptor, let startTimestamp else { return }
        guard input.isReadyForMoreMediaData else {
            droppedFrameCount += 1
            return
        }
        let relative = timestamp - startTimestamp
        let presentation = CMTime(seconds: relative, preferredTimescale: 600)
        guard adaptor.append(pixelBuffer, withPresentationTime: presentation) else {
            droppedFrameCount += 1
            if writer.status == .failed {
                logger.error("Video append failed: \(writer.error?.localizedDescription ?? "unknown", privacy: .public)")
            }
            return
        }
        lastAppendedTimestamp = timestamp
        writeIndexLine(frame: frame, relativeTimestamp: relative, captureDevice: captureDevice)
        appendedFrameCount += 1
    }

    func finish(completion: @escaping (FinishResult) -> Void) {
        try? indexHandle?.close()
        indexHandle = nil
        guard let writer else {
            completion(FinishResult(succeeded: true, status: "not_active", error: nil))
            return
        }

        let complete: () -> Void = { [weak self] in
            let succeeded = writer.status == .completed || writer.status == .unknown
            let result = FinishResult(
                succeeded: succeeded,
                status: Self.writerStatusLabel(writer.status),
                error: writer.error?.localizedDescription
            )
            self?.writer = nil
            self?.input = nil
            self?.adaptor = nil
            self?.startTimestamp = nil
            self?.logger.info(
                "Video writer finished with status \(result.status, privacy: .public), appended \(self?.appendedFrameCount ?? 0), dropped \(self?.droppedFrameCount ?? 0)"
            )
            completion(result)
        }

        if let input, writer.status == .writing {
            input.markAsFinished()
            writer.finishWriting(completionHandler: complete)
        } else {
            complete()
        }
    }

    private static func writerStatusLabel(_ status: AVAssetWriter.Status) -> String {
        switch status {
        case .unknown:
            return "no_video_frames"
        case .writing:
            return "writing"
        case .completed:
            return "completed"
        case .failed:
            return "failed"
        case .cancelled:
            return "cancelled"
        @unknown default:
            return "unknown"
        }
    }

    private func writeIndexLine(frame: ARFrame, relativeTimestamp: TimeInterval, captureDevice: AVCaptureDevice?) {
        guard let indexHandle else { return }
        let transform = frame.camera.transform
        let cameraToWorld = (0..<4).map { row in
            (0..<4).map { column in Double(transform[column][row]) }
        }
        let intrinsicsMatrix = frame.camera.intrinsics
        let resolution = frame.camera.imageResolution
        var entry: [String: Any] = [
            "video_frame_idx": appendedFrameCount,
            "timestamp": relativeTimestamp,
            "ar_timestamp": frame.timestamp,
            "camera_to_world": cameraToWorld,
            "intrinsics": [
                "fl_x": Double(intrinsicsMatrix[0][0]),
                "fl_y": Double(intrinsicsMatrix[1][1]),
                "cx": Double(intrinsicsMatrix[2][0]),
                "cy": Double(intrinsicsMatrix[2][1]),
                "w": Double(resolution.width),
                "h": Double(resolution.height),
            ],
            "tracking_state": trackingStateLabel(frame.camera.trackingState),
            "exposure_duration": frame.camera.exposureDuration,
        ]
        if frame.camera.exposureOffset.isFinite {
            entry["exposure_offset"] = Double(frame.camera.exposureOffset)
        }
        if let device = captureDevice {
            entry["iso"] = Double(device.iso)
            let gains = device.deviceWhiteBalanceGains
            if gains.redGain.isFinite, gains.greenGain.isFinite, gains.blueGain.isFinite {
                entry["white_balance_gains"] = [
                    "red": Double(gains.redGain),
                    "green": Double(gains.greenGain),
                    "blue": Double(gains.blueGain),
                ]
            }
            if device.lensPosition.isFinite {
                entry["lens_position"] = Double(device.lensPosition)
            }
            if device.exposureTargetBias.isFinite {
                entry["exposure_target_bias"] = Double(device.exposureTargetBias)
            }
            entry["is_adjusting_exposure"] = device.isAdjustingExposure
            entry["is_adjusting_white_balance"] = device.isAdjustingWhiteBalance
            entry["is_adjusting_focus"] = device.isAdjustingFocus
            entry["exposure_mode"] = String(describing: device.exposureMode)
            entry["white_balance_mode"] = String(describing: device.whiteBalanceMode)
            entry["focus_mode"] = String(describing: device.focusMode)
        }
        let pixelBuffer = frame.capturedImage
        entry["pixel_format"] = Self.pixelFormatLabel(CVPixelBufferGetPixelFormatType(pixelBuffer))
        entry["color_primaries"] = Self.attachmentString(pixelBuffer, kCVImageBufferColorPrimariesKey)
        entry["transfer_function"] = Self.attachmentString(pixelBuffer, kCVImageBufferTransferFunctionKey)
        entry["ycbcr_matrix"] = Self.attachmentString(pixelBuffer, kCVImageBufferYCbCrMatrixKey)
        let projection = frame.camera.projectionMatrix
        let projectionMatrix = (0..<4).map { row in
            (0..<4).map { column in Double(projection[column][row]) }
        }
        let projectionAvailable = projectionMatrix.flatMap { $0 }.allSatisfy(\.isFinite)
        var projectionReport: [String: Any] = [
            "model": "arkit_pinhole_intrinsics",
            "matrix_available": projectionAvailable,
            "camera_calibration_data_available": frame.capturedDepthData?.cameraCalibrationData != nil,
            "distortion_coefficients_available": false,
        ]
        if projectionAvailable {
            projectionReport["matrix"] = projectionMatrix
        }
        entry["projection"] = projectionReport
        if let light = frame.lightEstimate {
            entry["ambient_intensity"] = light.ambientIntensity
            entry["ambient_color_temperature_k"] = light.ambientColorTemperature
        }
        guard JSONSerialization.isValidJSONObject(entry),
              let data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]) else {
            return
        }
        indexHandle.write(data)
        indexHandle.write(Data("\n".utf8))
    }

    private static func attachmentString(_ buffer: CVPixelBuffer, _ key: CFString) -> String? {
        guard let value = CVBufferCopyAttachment(buffer, key, nil) else { return nil }
        return value as? String ?? String(describing: value)
    }

    private static func pixelFormatLabel(_ value: OSType) -> String {
        let bytes: [UInt8] = [
            UInt8((value >> 24) & 0xff),
            UInt8((value >> 16) & 0xff),
            UInt8((value >> 8) & 0xff),
            UInt8(value & 0xff),
        ]
        return String(bytes: bytes, encoding: .ascii) ?? String(value)
    }

    private func trackingStateLabel(_ state: ARCamera.TrackingState) -> String {
        switch state {
        case .normal:
            return "normal"
        case .notAvailable:
            return "not_available"
        case .limited(.excessiveMotion):
            return "limited_excessive_motion"
        case .limited(.insufficientFeatures):
            return "limited_insufficient_features"
        case .limited(.initializing):
            return "limited_initializing"
        case .limited(.relocalizing):
            return "limited_relocalizing"
        case .limited:
            return "limited"
        }
    }
}

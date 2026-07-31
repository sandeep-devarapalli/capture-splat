import Darwin
import Foundation

enum LiveCaptureJournalError: Error, LocalizedError {
    case corrupt(String)
    case conflict(String)
    case invalid(String)

    var errorDescription: String? {
        switch self {
        case let .corrupt(message):
            return "Live capture journal is corrupt: \(message)"
        case let .conflict(message):
            return "Live capture journal conflicts with accepted evidence: \(message)"
        case let .invalid(message):
            return "Live capture journal record is invalid: \(message)"
        }
    }
}

private struct LiveCaptureJournalQuality: Codable, Equatable {
    let reason: String
    let score: Double
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
    let colmapOverlapScore: Double
    let validDepthRatio: Double
    let featurePointCount: Int

    enum CodingKeys: String, CodingKey {
        case reason, score
        case blurScore = "blur_score"
        case exposureMean = "exposure_mean"
        case exposureDelta = "exposure_delta"
        case clippedHighlightFraction = "clipped_highlight_fraction"
        case nearClippedHighlightFraction = "near_clipped_highlight_fraction"
        case clippedShadowFraction = "clipped_shadow_fraction"
        case featureGridCoverage = "feature_grid_coverage"
        case parallaxMeters = "parallax_meters"
        case angularVelocityDegPerSec = "angular_velocity_deg_s"
        case translationSpeedMetersPerSec = "translation_speed_m_s"
        case colmapOverlapScore = "colmap_overlap_score"
        case validDepthRatio = "valid_depth_ratio"
        case featurePointCount = "feature_point_count"
    }

    init(_ quality: LiveCaptureFrameQualityEvent) {
        reason = quality.reason
        score = quality.score
        blurScore = quality.blurScore
        exposureMean = quality.exposureMean
        exposureDelta = quality.exposureDelta
        clippedHighlightFraction = quality.clippedHighlightFraction
        nearClippedHighlightFraction = quality.nearClippedHighlightFraction
        clippedShadowFraction = quality.clippedShadowFraction
        featureGridCoverage = quality.featureGridCoverage
        parallaxMeters = quality.parallaxMeters
        angularVelocityDegPerSec = quality.angularVelocityDegPerSec
        translationSpeedMetersPerSec = quality.translationSpeedMetersPerSec
        colmapOverlapScore = quality.colmapOverlapScore
        validDepthRatio = quality.validDepthRatio
        featurePointCount = quality.featurePointCount
    }

    var event: LiveCaptureFrameQualityEvent {
        LiveCaptureFrameQualityEvent(
            reason: reason,
            score: score,
            blurScore: blurScore,
            exposureMean: exposureMean,
            exposureDelta: exposureDelta,
            clippedHighlightFraction: clippedHighlightFraction,
            nearClippedHighlightFraction: nearClippedHighlightFraction,
            clippedShadowFraction: clippedShadowFraction,
            featureGridCoverage: featureGridCoverage,
            parallaxMeters: parallaxMeters,
            angularVelocityDegPerSec: angularVelocityDegPerSec,
            translationSpeedMetersPerSec: translationSpeedMetersPerSec,
            colmapOverlapScore: colmapOverlapScore,
            validDepthRatio: validDepthRatio,
            featurePointCount: featurePointCount
        )
    }
}

private struct LiveCaptureAcceptedFrameRecord: Codable, Equatable {
    let schema: String
    let sequenceID: Int
    let timestamp: Double
    let sourceRelativePath: String
    let sourceWidth: Int
    let sourceHeight: Int
    let depthRelativePath: String
    let depthWidth: Int
    let depthHeight: Int
    let confidenceRelativePath: String?
    let cameraToWorld: [Double]
    let flX: Double
    let flY: Double
    let cx: Double
    let cy: Double
    let trackingState: String
    let quality: LiveCaptureJournalQuality

    enum CodingKeys: String, CodingKey {
        case schema, timestamp, quality
        case sequenceID = "sequence_id"
        case sourceRelativePath = "source_relative_path"
        case sourceWidth = "source_width"
        case sourceHeight = "source_height"
        case depthRelativePath = "depth_relative_path"
        case depthWidth = "depth_width"
        case depthHeight = "depth_height"
        case confidenceRelativePath = "confidence_relative_path"
        case cameraToWorld = "camera_to_world"
        case flX = "fl_x"
        case flY = "fl_y"
        case cx, cy
        case trackingState = "tracking_state"
    }

    init(_ event: LiveCaptureFrameCommittedEvent) {
        schema = "capture_splat.live_capture_accepted_frame.v0.1"
        sequenceID = event.sequenceID
        timestamp = event.timestamp
        sourceRelativePath = event.sourceRelativePath
        sourceWidth = event.sourceWidth
        sourceHeight = event.sourceHeight
        depthRelativePath = event.depthRelativePath
        depthWidth = event.depthWidth
        depthHeight = event.depthHeight
        confidenceRelativePath = event.confidenceRelativePath
        cameraToWorld = event.cameraToWorld
        flX = event.flX
        flY = event.flY
        cx = event.cx
        cy = event.cy
        trackingState = event.trackingState
        quality = LiveCaptureJournalQuality(event.quality)
    }

    func event(captureRoot: URL) -> LiveCaptureFrameCommittedEvent {
        LiveCaptureFrameCommittedEvent(
            captureRoot: captureRoot,
            sequenceID: sequenceID,
            timestamp: timestamp,
            sourceRelativePath: sourceRelativePath,
            sourceWidth: sourceWidth,
            sourceHeight: sourceHeight,
            depthRelativePath: depthRelativePath,
            depthWidth: depthWidth,
            depthHeight: depthHeight,
            confidenceRelativePath: confidenceRelativePath,
            cameraToWorld: cameraToWorld,
            flX: flX,
            flY: flY,
            cx: cx,
            cy: cy,
            trackingState: trackingState,
            quality: quality.event
        )
    }
}

private struct LiveCaptureFinalizationRecord: Codable, Equatable {
    let schema: String
    let finalSequenceID: Int
    let manifestRelativePath: String
    let manifestSizeBytes: Int64
    let manifestSHA256: String

    enum CodingKeys: String, CodingKey {
        case schema
        case finalSequenceID = "final_sequence_id"
        case manifestRelativePath = "manifest_relative_path"
        case manifestSizeBytes = "manifest_size_bytes"
        case manifestSHA256 = "manifest_sha256"
    }

    init(_ event: LiveCaptureFinalizedEvent) {
        schema = "capture_splat.live_capture_finalization.v0.1"
        finalSequenceID = event.finalSequenceID
        manifestRelativePath = event.manifestRelativePath
        manifestSizeBytes = event.manifestSizeBytes
        manifestSHA256 = event.manifestSHA256
    }

    func event(captureRoot: URL) -> LiveCaptureFinalizedEvent {
        LiveCaptureFinalizedEvent(
            captureRoot: captureRoot,
            finalSequenceID: finalSequenceID,
            manifestRelativePath: manifestRelativePath,
            manifestSizeBytes: manifestSizeBytes,
            manifestSHA256: manifestSHA256
        )
    }
}

enum LiveCaptureJournal {
    private static let acceptedFramesPath = "metadata/live/accepted-frames"
    private static let finalizationPath = "metadata/live/finalization.json"
    private static let maximumRecordBytes = 256 * 1024

    static func commitAcceptedFrame(_ event: LiveCaptureFrameCommittedEvent) throws {
        let captureRoot = try validatedCaptureRoot(event.captureRoot)
        let record = LiveCaptureAcceptedFrameRecord(event)
        try validate(record, captureRoot: captureRoot)
        guard try loadFinalizationRecord(captureRoot: captureRoot) == nil else {
            throw LiveCaptureJournalError.conflict("the capture is already finalized")
        }
        let acceptedDirectory = captureRoot.appendingPathComponent(
            acceptedFramesPath,
            isDirectory: true
        )
        if try fileStatusIfPresent(acceptedDirectory) != nil {
            try validateDirectoryHierarchy(
                captureRoot: captureRoot,
                relativePath: acceptedFramesPath
            )
        }

        let data = try LiveStrictJSON.canonicalData(record)
        guard data.count <= maximumRecordBytes else {
            throw LiveCaptureJournalError.invalid("accepted-frame record is oversized")
        }
        let relativePath = acceptedFramePath(sequenceID: event.sequenceID)
        let url = captureRoot.appendingPathComponent(relativePath)
        if try fileStatusIfPresent(url) != nil {
            let existingData = try readRegularFile(url, maximumBytes: maximumRecordBytes)
            let existingRecord = try decodeAcceptedFrameRecord(
                existingData,
                sequenceID: event.sequenceID,
                captureRoot: captureRoot
            )
            guard existingRecord == record,
                  existingData == data else {
                throw LiveCaptureJournalError.conflict(
                    "sequence \(event.sequenceID) has different immutable bytes"
                )
            }
            return
        }
        if event.sequenceID > 1 {
            _ = try loadAcceptedFrameRecord(
                sequenceID: event.sequenceID - 1,
                captureRoot: captureRoot
            )
        }
        let nextURL = captureRoot.appendingPathComponent(
            acceptedFramePath(sequenceID: event.sequenceID + 1)
        )
        guard try fileStatusIfPresent(nextURL) == nil else {
            throw LiveCaptureJournalError.corrupt(
                "sequence \(event.sequenceID + 1) exists before sequence \(event.sequenceID)"
            )
        }

        _ = try ensureJournalDirectory(
            captureRoot: captureRoot,
            relativePath: acceptedFramesPath
        )
        try LiveAtomicFile.write(data, to: url)
        guard try readRegularFile(url, maximumBytes: maximumRecordBytes) == data else {
            throw LiveCaptureJournalError.corrupt(
                "sequence \(event.sequenceID) did not persist exact canonical bytes"
            )
        }
    }

    static func loadAcceptedFrames(captureRoot: URL) throws -> [LiveCaptureFrameCommittedEvent] {
        let root = try validatedCaptureRoot(captureRoot)
        return try loadAcceptedFrameRecords(captureRoot: root).map {
            $0.event(captureRoot: root)
        }
    }

    static func commitFinalization(_ event: LiveCaptureFinalizedEvent) throws {
        let captureRoot = try validatedCaptureRoot(event.captureRoot)
        let record = LiveCaptureFinalizationRecord(event)
        try validate(record, captureRoot: captureRoot)
        let accepted = try loadAcceptedFrameRecords(captureRoot: captureRoot)
        guard accepted.count == event.finalSequenceID else {
            throw LiveCaptureJournalError.corrupt(
                "final sequence \(event.finalSequenceID) does not match \(accepted.count) accepted records"
            )
        }

        let data = try LiveStrictJSON.canonicalData(record)
        guard data.count <= maximumRecordBytes else {
            throw LiveCaptureJournalError.invalid("finalization record is oversized")
        }
        let url = captureRoot.appendingPathComponent(finalizationPath)
        if let existing = try loadFinalizationRecord(captureRoot: captureRoot) {
            guard existing == record,
                  try readRegularFile(url, maximumBytes: maximumRecordBytes) == data else {
                throw LiveCaptureJournalError.conflict(
                    "finalization has different immutable bytes"
                )
            }
            return
        }
        _ = try ensureJournalDirectory(
            captureRoot: captureRoot,
            relativePath: "metadata/live"
        )
        try LiveAtomicFile.write(data, to: url)
        guard try readRegularFile(url, maximumBytes: maximumRecordBytes) == data else {
            throw LiveCaptureJournalError.corrupt(
                "finalization did not persist exact canonical bytes"
            )
        }
    }

    static func loadFinalization(captureRoot: URL) throws -> LiveCaptureFinalizedEvent? {
        let root = try validatedCaptureRoot(captureRoot)
        guard let record = try loadFinalizationRecord(captureRoot: root) else {
            return nil
        }
        let accepted = try loadAcceptedFrameRecords(captureRoot: root)
        guard accepted.count == record.finalSequenceID else {
            throw LiveCaptureJournalError.corrupt(
                "finalization does not match the accepted-frame journal"
            )
        }
        return record.event(captureRoot: root)
    }

    private static func loadAcceptedFrameRecords(
        captureRoot: URL
    ) throws -> [LiveCaptureAcceptedFrameRecord] {
        let directory = captureRoot.appendingPathComponent(acceptedFramesPath)
        guard try fileStatusIfPresent(directory) != nil else {
            return []
        }
        try validateDirectoryHierarchy(
            captureRoot: captureRoot,
            relativePath: acceptedFramesPath
        )
        let names: [String]
        do {
            names = try FileManager.default.contentsOfDirectory(atPath: directory.path)
        } catch {
            throw LiveCaptureJournalError.corrupt("accepted-frame directory is unreadable")
        }

        var recordsBySequence: [Int: LiveCaptureAcceptedFrameRecord] = [:]
        for name in names {
            let url = directory.appendingPathComponent(name)
            let status = try fileStatus(url)
            guard status.st_mode & S_IFMT != S_IFLNK else {
                throw LiveCaptureJournalError.corrupt(
                    "accepted-frame directory contains a symlink"
                )
            }
            if isCrashIncomingFile(name) {
                guard status.st_mode & S_IFMT == S_IFREG else {
                    throw LiveCaptureJournalError.corrupt(
                        "crash incoming entry is not a regular file"
                    )
                }
                continue
            }
            guard status.st_mode & S_IFMT == S_IFREG,
                  let sequenceID = sequenceID(from: name) else {
                throw LiveCaptureJournalError.corrupt(
                    "accepted-frame directory contains an unexpected entry"
                )
            }
            let data = try readRegularFile(url, maximumBytes: maximumRecordBytes)
            let record = try decodeAcceptedFrameRecord(
                data,
                sequenceID: sequenceID,
                captureRoot: captureRoot
            )
            guard recordsBySequence.updateValue(record, forKey: sequenceID) == nil else {
                throw LiveCaptureJournalError.corrupt(
                    "sequence \(sequenceID) appears more than once"
                )
            }
        }

        let ordered = recordsBySequence.keys.sorted()
        for (offset, sequenceID) in ordered.enumerated() where sequenceID != offset + 1 {
            throw LiveCaptureJournalError.corrupt(
                "accepted-frame records are not contiguous from sequence 1"
            )
        }
        return ordered.compactMap { recordsBySequence[$0] }
    }

    private static func loadAcceptedFrameRecord(
        sequenceID: Int,
        captureRoot: URL
    ) throws -> LiveCaptureAcceptedFrameRecord {
        let url = captureRoot.appendingPathComponent(
            acceptedFramePath(sequenceID: sequenceID)
        )
        let data = try readRegularFile(url, maximumBytes: maximumRecordBytes)
        return try decodeAcceptedFrameRecord(
            data,
            sequenceID: sequenceID,
            captureRoot: captureRoot
        )
    }

    private static func decodeAcceptedFrameRecord(
        _ data: Data,
        sequenceID: Int,
        captureRoot: URL
    ) throws -> LiveCaptureAcceptedFrameRecord {
        let record: LiveCaptureAcceptedFrameRecord
        do {
            record = try LiveStrictJSON.decodeCanonical(
                LiveCaptureAcceptedFrameRecord.self,
                from: data
            )
        } catch {
            throw LiveCaptureJournalError.corrupt(
                "sequence \(sequenceID) is not exact canonical JSON"
            )
        }
        guard record.sequenceID == sequenceID else {
            throw LiveCaptureJournalError.corrupt(
                "sequence \(sequenceID) does not match its filename"
            )
        }
        try validate(record, captureRoot: captureRoot)
        return record
    }

    private static func loadFinalizationRecord(
        captureRoot: URL
    ) throws -> LiveCaptureFinalizationRecord? {
        let url = captureRoot.appendingPathComponent(finalizationPath)
        guard try fileStatusIfPresent(url) != nil else {
            return nil
        }
        try validateDirectoryHierarchy(
            captureRoot: captureRoot,
            relativePath: "metadata/live"
        )
        let data = try readRegularFile(url, maximumBytes: maximumRecordBytes)
        let record: LiveCaptureFinalizationRecord
        do {
            record = try LiveStrictJSON.decodeCanonical(
                LiveCaptureFinalizationRecord.self,
                from: data
            )
        } catch {
            throw LiveCaptureJournalError.corrupt(
                "finalization is not exact canonical JSON"
            )
        }
        try validate(record, captureRoot: captureRoot)
        return record
    }

    private static func validate(
        _ record: LiveCaptureAcceptedFrameRecord,
        captureRoot: URL
    ) throws {
        let finite = [
            record.timestamp,
            record.flX,
            record.flY,
            record.cx,
            record.cy,
            record.quality.score,
            record.quality.blurScore,
            record.quality.exposureMean,
            record.quality.exposureDelta,
            record.quality.clippedHighlightFraction,
            record.quality.nearClippedHighlightFraction,
            record.quality.clippedShadowFraction,
            record.quality.featureGridCoverage,
            record.quality.parallaxMeters,
            record.quality.angularVelocityDegPerSec,
            record.quality.translationSpeedMetersPerSec,
            record.quality.colmapOverlapScore,
            record.quality.validDepthRatio,
        ] + record.cameraToWorld
        guard record.schema == "capture_splat.live_capture_accepted_frame.v0.1",
              (1...LiveSenderValidation.maximumSequenceID).contains(record.sequenceID),
              record.timestamp >= 0,
              record.sourceWidth > 0,
              record.sourceHeight > 0,
              record.depthWidth > 0,
              record.depthHeight > 0,
              record.cameraToWorld.count == 16,
              record.flX > 0,
              record.flY > 0,
              record.quality.featurePointCount >= 0,
              finite.allSatisfy(\.isFinite),
              validText(record.trackingState, maximumLength: 256),
              validText(record.quality.reason, maximumLength: 512) else {
            throw LiveCaptureJournalError.invalid(
                "accepted-frame fields do not match the strict contract"
            )
        }
        let paths = [
            record.sourceRelativePath,
            record.depthRelativePath,
        ] + [record.confidenceRelativePath].compactMap { $0 }
        guard Set(paths).count == paths.count else {
            throw LiveCaptureJournalError.invalid("asset paths must be unique")
        }
        for path in paths {
            guard path.count <= 4096 else {
                throw LiveCaptureJournalError.invalid("asset path is oversized")
            }
            do {
                try LiveSenderValidation.safeRelativePath(path)
                try validateEvidenceFile(captureRoot: captureRoot, relativePath: path)
            } catch {
                throw LiveCaptureJournalError.invalid(
                    "asset path is unsafe, missing, or symbolic"
                )
            }
        }
    }

    private static func validate(
        _ record: LiveCaptureFinalizationRecord,
        captureRoot: URL
    ) throws {
        guard record.schema == "capture_splat.live_capture_finalization.v0.1",
              (1...LiveSenderValidation.maximumSequenceID).contains(record.finalSequenceID),
              record.manifestRelativePath == "capture.json",
              record.manifestSizeBytes > 0,
              LiveSenderValidation.isSHA256(record.manifestSHA256) else {
            throw LiveCaptureJournalError.invalid(
                "finalization fields do not match the strict contract"
            )
        }
        do {
            try LiveSenderValidation.safeRelativePath(record.manifestRelativePath)
            try validateEvidenceFile(
                captureRoot: captureRoot,
                relativePath: record.manifestRelativePath
            )
            let manifest = try LiveConfinedFile.inspect(
                captureRoot: captureRoot,
                relativePath: record.manifestRelativePath,
                calculateSHA256: true
            )
            guard manifest.size == record.manifestSizeBytes,
                  manifest.sha256 == record.manifestSHA256 else {
                throw LiveCaptureJournalError.invalid(
                    "final capture manifest bytes do not match the journal"
                )
            }
        } catch {
            throw LiveCaptureJournalError.invalid(
                "final capture manifest is unsafe, missing, or symbolic"
            )
        }
    }

    private static func validText(_ value: String, maximumLength: Int) -> Bool {
        !value.isEmpty
            && value.count <= maximumLength
            && !value.unicodeScalars.contains(where: {
                CharacterSet.controlCharacters.contains($0)
            })
    }

    private static func acceptedFramePath(sequenceID: Int) -> String {
        "\(acceptedFramesPath)/\(String(format: "%08d", sequenceID)).json"
    }

    private static func sequenceID(from name: String) -> Int? {
        guard name.count == 13,
              name.hasSuffix(".json") else {
            return nil
        }
        let digits = name.prefix(8)
        guard digits.allSatisfy({ $0.isNumber }),
              let sequenceID = Int(digits),
              (1...LiveSenderValidation.maximumSequenceID).contains(sequenceID),
              String(format: "%08d.json", sequenceID) == name else {
            return nil
        }
        return sequenceID
    }

    private static func isCrashIncomingFile(_ name: String) -> Bool {
        guard name.hasPrefix("."),
              name.hasSuffix(".incoming") else {
            return false
        }
        let suffix = ".incoming"
        let body = String(name.dropFirst().dropLast(suffix.count))
        let parts = body.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3,
              parts[0].count == 8,
              parts[0].allSatisfy({ $0.isNumber }),
              parts[1] == "json",
              let sequenceID = Int(parts[0]),
              (1...LiveSenderValidation.maximumSequenceID).contains(sequenceID) else {
            return false
        }
        return UUID(uuidString: String(parts[2])) != nil
    }

    private static func validatedCaptureRoot(_ captureRoot: URL) throws -> URL {
        let root = captureRoot.standardizedFileURL
        let status = try fileStatus(root)
        guard status.st_mode & S_IFMT != S_IFLNK,
              status.st_mode & S_IFMT == S_IFDIR else {
            throw LiveCaptureJournalError.invalid(
                "capture root must be a real directory"
            )
        }
        return root
    }

    private static func ensureJournalDirectory(
        captureRoot: URL,
        relativePath: String
    ) throws -> URL {
        var current = captureRoot
        for component in relativePath.split(separator: "/") {
            current.appendPathComponent(String(component), isDirectory: true)
            if let status = try fileStatusIfPresent(current) {
                guard status.st_mode & S_IFMT != S_IFLNK,
                      status.st_mode & S_IFMT == S_IFDIR else {
                    throw LiveCaptureJournalError.corrupt(
                        "journal directory hierarchy is symbolic or invalid"
                    )
                }
            } else {
                do {
                    try FileManager.default.createDirectory(
                        at: current,
                        withIntermediateDirectories: false,
                        attributes: [.posixPermissions: 0o700]
                    )
                } catch {
                    throw LiveCaptureJournalError.corrupt(
                        "journal directory hierarchy cannot be created"
                    )
                }
                let status = try fileStatus(current)
                guard status.st_mode & S_IFMT != S_IFLNK,
                      status.st_mode & S_IFMT == S_IFDIR else {
                    throw LiveCaptureJournalError.corrupt(
                        "created journal directory is invalid"
                    )
                }
            }
        }
        return current
    }

    private static func validateDirectoryHierarchy(
        captureRoot: URL,
        relativePath: String
    ) throws {
        var current = captureRoot
        for component in relativePath.split(separator: "/") {
            current.appendPathComponent(String(component), isDirectory: true)
            let status = try fileStatus(current)
            guard status.st_mode & S_IFMT != S_IFLNK,
                  status.st_mode & S_IFMT == S_IFDIR else {
                throw LiveCaptureJournalError.corrupt(
                    "journal directory hierarchy is symbolic or invalid"
                )
            }
        }
    }

    private static func validateEvidenceFile(
        captureRoot: URL,
        relativePath: String
    ) throws {
        _ = try LiveConfinedFile.inspect(
            captureRoot: captureRoot,
            relativePath: relativePath,
            calculateSHA256: false
        )
    }

    private static func readRegularFile(_ url: URL, maximumBytes: Int) throws -> Data {
        let descriptor = Darwin.open(url.path, O_RDONLY | O_NOFOLLOW)
        guard descriptor >= 0 else {
            throw LiveCaptureJournalError.corrupt("journal record cannot be opened")
        }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        var status = stat()
        guard Darwin.fstat(descriptor, &status) == 0,
              status.st_mode & S_IFMT == S_IFREG,
              status.st_size >= 0,
              status.st_size <= off_t(maximumBytes) else {
            try? handle.close()
            throw LiveCaptureJournalError.corrupt(
                "journal record is not a bounded regular file"
            )
        }
        do {
            let data = try handle.readToEnd() ?? Data()
            var after = stat()
            guard Darwin.fstat(descriptor, &after) == 0,
                  status.st_dev == after.st_dev,
                  status.st_ino == after.st_ino,
                  status.st_size == after.st_size,
                  status.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
                  status.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
                  data.count == Int(after.st_size) else {
                try? handle.close()
                throw LiveCaptureJournalError.corrupt(
                    "journal record changed while being read"
                )
            }
            try handle.close()
            return data
        } catch let error as LiveCaptureJournalError {
            throw error
        } catch {
            throw LiveCaptureJournalError.corrupt("journal record cannot be read")
        }
    }

    private static func fileStatus(_ url: URL) throws -> stat {
        var status = stat()
        guard Darwin.lstat(url.path, &status) == 0 else {
            throw LiveCaptureJournalError.corrupt(
                "required journal or evidence path is missing"
            )
        }
        return status
    }

    private static func fileStatusIfPresent(_ url: URL) throws -> stat? {
        var status = stat()
        if Darwin.lstat(url.path, &status) == 0 {
            return status
        }
        guard errno == ENOENT else {
            throw LiveCaptureJournalError.corrupt(
                "journal path cannot be inspected"
            )
        }
        return nil
    }
}

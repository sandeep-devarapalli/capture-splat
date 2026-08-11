import Foundation

struct LiveCaptureFrameQualityEvent: Sendable {
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
}

struct LiveCaptureFrameCommittedEvent: Sendable {
    let captureRoot: URL
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
    let quality: LiveCaptureFrameQualityEvent
}

struct LiveCaptureFinalizedEvent: Sendable {
    let captureRoot: URL
    let finalSequenceID: Int
    let manifestRelativePath: String
    let manifestSizeBytes: Int64
    let manifestSHA256: String
}

@main
private enum LiveCaptureJournalProbe {
    static func main() throws {
        guard CommandLine.arguments.count == 3 else {
            throw ProbeError.invalidArguments
        }
        let root = URL(
            fileURLWithPath: CommandLine.arguments[2],
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: root,
            withIntermediateDirectories: true
        )

        let result: [String: Bool]
        switch CommandLine.arguments[1] {
        case "lifecycle":
            result = try lifecycle(root: root)
        case "invalid":
            result = try invalidRecords(root: root)
        case "corruption":
            result = try corruption(root: root)
        case "finalization":
            result = try finalization(root: root)
        default:
            throw ProbeError.invalidArguments
        }
        let data = try JSONSerialization.data(
            withJSONObject: result,
            options: [.sortedKeys]
        )
        FileHandle.standardOutput.write(data)
    }

    private static func lifecycle(root: URL) throws -> [String: Bool] {
        let capture = try makeCapture(root: root, name: "lifecycle", sequences: [1])
        let first = frame(capture: capture, sequenceID: 1)
        try LiveCaptureJournal.commitAcceptedFrame(first)

        let reopened = try LiveCaptureJournal.loadAcceptedFrames(captureRoot: capture)
        let persisted = reopened.count == 1
            && reopened[0].sequenceID == 1
            && reopened[0].sourceRelativePath == first.sourceRelativePath
            && reopened[0].cameraToWorld == first.cameraToWorld

        let recordURL = acceptedRecord(capture: capture, sequenceID: 1)
        let firstBytes = try Data(contentsOf: recordURL)
        try LiveCaptureJournal.commitAcceptedFrame(first)
        let duplicateBytes = try Data(contentsOf: recordURL)

        let conflict = frame(
            capture: capture,
            sequenceID: 1,
            timestamp: first.timestamp + 1
        )
        return [
            "commit_reopen": persisted,
            "identical_duplicate_is_byte_identical": firstBytes == duplicateBytes,
            "conflicting_duplicate_rejected":
                isConflict { try LiveCaptureJournal.commitAcceptedFrame(conflict) },
        ]
    }

    private static func invalidRecords(root: URL) throws -> [String: Bool] {
        let gapCapture = try makeCapture(root: root, name: "gap", sequences: [2])
        let gapRejected = isCorrupt {
            try LiveCaptureJournal.commitAcceptedFrame(
                frame(capture: gapCapture, sequenceID: 2)
            )
        }
        let gapLeftNoRecord = try LiveCaptureJournal.loadAcceptedFrames(
            captureRoot: gapCapture
        ).isEmpty

        let nanCapture = try makeCapture(root: root, name: "nan", sequences: [1])
        let nanRejected = isInvalid {
            try LiveCaptureJournal.commitAcceptedFrame(
                frame(capture: nanCapture, sequenceID: 1, timestamp: .nan)
            )
        }
        let infinityRejected = isInvalid {
            let quality = frameQuality(score: .infinity)
            try LiveCaptureJournal.commitAcceptedFrame(
                frame(capture: nanCapture, sequenceID: 1, quality: quality)
            )
        }

        let traversalCapture = try makeCapture(
            root: root,
            name: "traversal",
            sequences: [1]
        )
        let traversalRejected = isInvalid {
            try LiveCaptureJournal.commitAcceptedFrame(
                frame(
                    capture: traversalCapture,
                    sequenceID: 1,
                    sourceRelativePath: "../outside.jpg"
                )
            )
        }

        let symlinkCapture = try makeCapture(
            root: root,
            name: "symlink",
            sequences: [1]
        )
        let outside = root.appendingPathComponent("outside.jpg")
        try Data("outside".utf8).write(to: outside)
        let link = symlinkCapture.appendingPathComponent("rgb/link.jpg")
        try FileManager.default.createSymbolicLink(
            at: link,
            withDestinationURL: outside
        )
        let symlinkRejected = isInvalid {
            try LiveCaptureJournal.commitAcceptedFrame(
                frame(
                    capture: symlinkCapture,
                    sequenceID: 1,
                    sourceRelativePath: "rgb/link.jpg"
                )
            )
        }

        return [
            "gap_rejected": gapRejected,
            "gap_left_no_record": gapLeftNoRecord,
            "nan_rejected": nanRejected,
            "infinity_rejected": infinityRejected,
            "traversal_rejected": traversalRejected,
            "symlink_rejected": symlinkRejected,
        ]
    }

    private static func corruption(root: URL) throws -> [String: Bool] {
        let noncanonical = try committedCapture(
            root: root,
            name: "noncanonical"
        )
        let noncanonicalURL = acceptedRecord(capture: noncanonical, sequenceID: 1)
        var noncanonicalBytes = try Data(contentsOf: noncanonicalURL)
        noncanonicalBytes.append(0x0a)
        try noncanonicalBytes.write(to: noncanonicalURL, options: .atomic)
        let noncanonicalRejected = isCorrupt {
            _ = try LiveCaptureJournal.loadAcceptedFrames(captureRoot: noncanonical)
        }

        let truncated = try committedCapture(root: root, name: "truncated")
        let truncatedURL = acceptedRecord(capture: truncated, sequenceID: 1)
        let complete = try Data(contentsOf: truncatedURL)
        try Data(complete.prefix(complete.count / 2)).write(
            to: truncatedURL,
            options: .atomic
        )
        let truncationRejected = isCorrupt {
            _ = try LiveCaptureJournal.loadAcceptedFrames(captureRoot: truncated)
        }

        let oversized = try committedCapture(root: root, name: "oversized")
        try Data(repeating: 0x20, count: 256 * 1024 + 1).write(
            to: acceptedRecord(capture: oversized, sequenceID: 1),
            options: .atomic
        )
        let oversizeRejected = isCorrupt {
            _ = try LiveCaptureJournal.loadAcceptedFrames(captureRoot: oversized)
        }

        let unexpected = try committedCapture(root: root, name: "unexpected")
        try Data("unexpected".utf8).write(
            to: acceptedDirectory(capture: unexpected)
                .appendingPathComponent("notes.txt")
        )
        let unexpectedRejected = isCorrupt {
            _ = try LiveCaptureJournal.loadAcceptedFrames(captureRoot: unexpected)
        }

        let stale = try committedCapture(root: root, name: "stale-incoming")
        let uuid = "01234567-89AB-CDEF-0123-456789ABCDEF"
        try Data("partial".utf8).write(
            to: acceptedDirectory(capture: stale).appendingPathComponent(
                ".00000002.json.\(uuid).incoming"
            )
        )
        let exactIncomingIgnored =
            try LiveCaptureJournal.loadAcceptedFrames(captureRoot: stale).count == 1

        let lookalike = try committedCapture(root: root, name: "incoming-lookalike")
        try Data("partial".utf8).write(
            to: acceptedDirectory(capture: lookalike).appendingPathComponent(
                ".00000002.json.not-a-uuid.incoming"
            )
        )
        let incomingLookalikeRejected = isCorrupt {
            _ = try LiveCaptureJournal.loadAcceptedFrames(captureRoot: lookalike)
        }

        let linkedIncoming = try committedCapture(
            root: root,
            name: "incoming-symlink"
        )
        let outside = root.appendingPathComponent("incoming-target")
        try Data("partial".utf8).write(to: outside)
        try FileManager.default.createSymbolicLink(
            at: acceptedDirectory(capture: linkedIncoming).appendingPathComponent(
                ".00000002.json.\(uuid).incoming"
            ),
            withDestinationURL: outside
        )
        let incomingSymlinkRejected = isCorrupt {
            _ = try LiveCaptureJournal.loadAcceptedFrames(captureRoot: linkedIncoming)
        }

        return [
            "noncanonical_rejected": noncanonicalRejected,
            "truncation_rejected": truncationRejected,
            "oversize_rejected": oversizeRejected,
            "unexpected_entry_rejected": unexpectedRejected,
            "exact_stale_incoming_ignored": exactIncomingIgnored,
            "incoming_lookalike_rejected": incomingLookalikeRejected,
            "incoming_symlink_rejected": incomingSymlinkRejected,
        ]
    }

    private static func finalization(root: URL) throws -> [String: Bool] {
        let countMismatch = try committedCapture(root: root, name: "count-mismatch")
        let countManifest = try writeManifest(capture: countMismatch, marker: "one")
        let countReference = try finalizationEvent(
            capture: countMismatch,
            finalSequenceID: 2,
            manifest: countManifest
        )
        let countMismatchRejected = isCorrupt {
            try LiveCaptureJournal.commitFinalization(countReference)
        }

        let wrongReference = try committedCapture(root: root, name: "wrong-reference")
        let wrongManifest = try writeManifest(capture: wrongReference, marker: "one")
        let correctReference = try finalizationEvent(
            capture: wrongReference,
            finalSequenceID: 1,
            manifest: wrongManifest
        )
        let wrongSize = LiveCaptureFinalizedEvent(
            captureRoot: wrongReference,
            finalSequenceID: 1,
            manifestRelativePath: "capture.json",
            manifestSizeBytes: correctReference.manifestSizeBytes + 1,
            manifestSHA256: correctReference.manifestSHA256
        )
        let wrongSHA = LiveCaptureFinalizedEvent(
            captureRoot: wrongReference,
            finalSequenceID: 1,
            manifestRelativePath: "capture.json",
            manifestSizeBytes: correctReference.manifestSizeBytes,
            manifestSHA256: "sha256:" + String(repeating: "0", count: 64)
        )

        let finalized = try committedCapture(root: root, name: "finalized")
        let manifest = try writeManifest(capture: finalized, marker: "original")
        let final = try finalizationEvent(
            capture: finalized,
            finalSequenceID: 1,
            manifest: manifest
        )
        try LiveCaptureJournal.commitFinalization(final)
        let finalBytesURL = finalized.appendingPathComponent(
            "metadata/live/finalization.json"
        )
        let firstFinalBytes = try Data(contentsOf: finalBytesURL)
        try LiveCaptureJournal.commitFinalization(final)
        let identicalFinalBytes = try Data(contentsOf: finalBytesURL)
        let reopenedFinal = try LiveCaptureJournal.loadFinalization(
            captureRoot: finalized
        )
        let exactFinalReopened = reopenedFinal?.finalSequenceID == 1
            && reopenedFinal?.manifestSizeBytes == final.manifestSizeBytes
            && reopenedFinal?.manifestSHA256 == final.manifestSHA256

        try makeEvidence(capture: finalized, sequenceID: 2)
        let postFinalRejected = isConflict {
            try LiveCaptureJournal.commitAcceptedFrame(
                frame(capture: finalized, sequenceID: 2)
            )
        }

        try Data("{\"marker\":\"mutated\"}".utf8).write(
            to: manifest,
            options: .atomic
        )
        let manifestMutationRejected = isInvalid {
            _ = try LiveCaptureJournal.loadFinalization(captureRoot: finalized)
        }
        let changedReference = try finalizationEvent(
            capture: finalized,
            finalSequenceID: 1,
            manifest: manifest
        )
        let changedFinalRejected = rejects {
            try LiveCaptureJournal.commitFinalization(changedReference)
        }

        return [
            "final_count_mismatch_rejected": countMismatchRejected,
            "wrong_manifest_size_rejected":
                isInvalid { try LiveCaptureJournal.commitFinalization(wrongSize) },
            "wrong_manifest_sha_rejected":
                isInvalid { try LiveCaptureJournal.commitFinalization(wrongSHA) },
            "identical_finalization_is_byte_identical":
                firstFinalBytes == identicalFinalBytes,
            "exact_finalization_reopened": exactFinalReopened,
            "post_final_write_rejected": postFinalRejected,
            "manifest_mutation_rejected": manifestMutationRejected,
            "changed_final_reference_rejected": changedFinalRejected,
        ]
    }

    private static func committedCapture(root: URL, name: String) throws -> URL {
        let capture = try makeCapture(root: root, name: name, sequences: [1])
        try LiveCaptureJournal.commitAcceptedFrame(
            frame(capture: capture, sequenceID: 1)
        )
        return capture
    }

    private static func makeCapture(
        root: URL,
        name: String,
        sequences: [Int]
    ) throws -> URL {
        let capture = root.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(
            at: capture,
            withIntermediateDirectories: true
        )
        for sequenceID in sequences {
            try makeEvidence(capture: capture, sequenceID: sequenceID)
        }
        return capture
    }

    private static func makeEvidence(capture: URL, sequenceID: Int) throws {
        let suffix = String(format: "%06d", sequenceID)
        try write(
            capture: capture,
            path: "rgb/frame_\(suffix).jpg",
            bytes: "rgb-\(sequenceID)"
        )
        try write(
            capture: capture,
            path: "depth/depth_\(suffix).npy",
            bytes: "depth-\(sequenceID)"
        )
        try write(
            capture: capture,
            path: "confidence/confidence_\(suffix).npy",
            bytes: "confidence-\(sequenceID)"
        )
    }

    private static func write(
        capture: URL,
        path: String,
        bytes: String
    ) throws {
        let url = capture.appendingPathComponent(path)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data(bytes.utf8).write(to: url)
    }

    private static func frame(
        capture: URL,
        sequenceID: Int,
        timestamp: Double? = nil,
        sourceRelativePath: String? = nil,
        quality: LiveCaptureFrameQualityEvent = frameQuality()
    ) -> LiveCaptureFrameCommittedEvent {
        let suffix = String(format: "%06d", sequenceID)
        return LiveCaptureFrameCommittedEvent(
            captureRoot: capture,
            sequenceID: sequenceID,
            timestamp: timestamp ?? Double(sequenceID),
            sourceRelativePath:
                sourceRelativePath ?? "rgb/frame_\(suffix).jpg",
            sourceWidth: 1920,
            sourceHeight: 1440,
            depthRelativePath: "depth/depth_\(suffix).npy",
            depthWidth: 256,
            depthHeight: 192,
            confidenceRelativePath: "confidence/confidence_\(suffix).npy",
            cameraToWorld: [
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ],
            flX: 1200,
            flY: 1200,
            cx: 960,
            cy: 720,
            trackingState: "normal",
            quality: quality
        )
    }

    private static func frameQuality(
        score: Double = 0.9
    ) -> LiveCaptureFrameQualityEvent {
        LiveCaptureFrameQualityEvent(
            reason: "accepted",
            score: score,
            blurScore: 0.8,
            exposureMean: 0.5,
            exposureDelta: 0.1,
            clippedHighlightFraction: 0.01,
            nearClippedHighlightFraction: 0.02,
            clippedShadowFraction: 0.01,
            featureGridCoverage: 0.75,
            parallaxMeters: 0.1,
            angularVelocityDegPerSec: 2,
            translationSpeedMetersPerSec: 0.2,
            colmapOverlapScore: 0.7,
            validDepthRatio: 0.8,
            featurePointCount: 120
        )
    }

    private static func writeManifest(
        capture: URL,
        marker: String
    ) throws -> URL {
        let url = capture.appendingPathComponent("capture.json")
        try Data("{\"marker\":\"\(marker)\"}".utf8).write(
            to: url,
            options: .atomic
        )
        return url
    }

    private static func finalizationEvent(
        capture: URL,
        finalSequenceID: Int,
        manifest: URL
    ) throws -> LiveCaptureFinalizedEvent {
        let data = try Data(contentsOf: manifest)
        return LiveCaptureFinalizedEvent(
            captureRoot: capture,
            finalSequenceID: finalSequenceID,
            manifestRelativePath: "capture.json",
            manifestSizeBytes: Int64(data.count),
            manifestSHA256: LiveAuthEncoding.sha256(data)
        )
    }

    private static func acceptedDirectory(capture: URL) -> URL {
        capture.appendingPathComponent(
            "metadata/live/accepted-frames",
            isDirectory: true
        )
    }

    private static func acceptedRecord(capture: URL, sequenceID: Int) -> URL {
        acceptedDirectory(capture: capture).appendingPathComponent(
            String(format: "%08d.json", sequenceID)
        )
    }

    private static func rejects(_ operation: () throws -> Void) -> Bool {
        do {
            try operation()
            return false
        } catch {
            return true
        }
    }

    private static func isInvalid(_ operation: () throws -> Void) -> Bool {
        do {
            try operation()
            return false
        } catch LiveCaptureJournalError.invalid {
            return true
        } catch {
            return false
        }
    }

    private static func isConflict(_ operation: () throws -> Void) -> Bool {
        do {
            try operation()
            return false
        } catch LiveCaptureJournalError.conflict {
            return true
        } catch {
            return false
        }
    }

    private static func isCorrupt(_ operation: () throws -> Void) -> Bool {
        do {
            try operation()
            return false
        } catch LiveCaptureJournalError.corrupt {
            return true
        } catch {
            return false
        }
    }
}

private enum ProbeError: Error {
    case invalidArguments
}

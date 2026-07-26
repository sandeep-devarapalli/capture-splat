import Foundation

enum CaptureLibraryState: String {
    case ready
    case partial
    case invalid
}

struct CaptureLibraryItem: Identifiable, Equatable {
    let directory: URL
    let state: CaptureLibraryState
    let statusDetail: String
    let modifiedAt: Date
    let acceptedFrameCount: Int
    let captureIntent: String?
    let pointCloudPreviewFile: URL?
    let pointCloudPointCount: Int
    let roomPlanFile: URL?

    var id: String { directory.path }
    var name: String { directory.lastPathComponent }
    var shareLabel: String {
        switch state {
        case .ready: return "Share Capture"
        case .partial: return "Share Partial"
        case .invalid: return "Share Recovery"
        }
    }
}

struct CaptureLibraryScanner {
    private let fileManager: FileManager

    init(fileManager: FileManager = .default) {
        self.fileManager = fileManager
    }

    func scan(root: URL) -> [CaptureLibraryItem] {
        let keys: [URLResourceKey] = [.isDirectoryKey, .contentModificationDateKey]
        let directories = (try? fileManager.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: keys,
            options: [.skipsHiddenFiles]
        )) ?? []

        return directories.compactMap { url in
            guard url.lastPathComponent.hasPrefix("capture_splat_"),
                  (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true else {
                return nil
            }
            return inspect(directory: url)
        }
        .sorted { lhs, rhs in
            if lhs.modifiedAt == rhs.modifiedAt {
                return lhs.name > rhs.name
            }
            return lhs.modifiedAt > rhs.modifiedAt
        }
    }

    private func inspect(directory: URL) -> CaptureLibraryItem {
        let modifiedAt = (try? directory.resourceValues(
            forKeys: [.contentModificationDateKey]
        ).contentModificationDate) ?? .distantPast
        let manifestURL = directory.appendingPathComponent("capture.json")

        guard fileManager.fileExists(atPath: manifestURL.path) else {
            return item(
                directory: directory,
                state: .partial,
                detail: "capture.json is missing; preserved files can still be shared for recovery.",
                modifiedAt: modifiedAt
            )
        }

        let manifest: [String: Any]
        do {
            manifest = try readJSONObject(at: manifestURL)
        } catch {
            return item(
                directory: directory,
                state: .invalid,
                detail: "capture.json is unreadable.",
                modifiedAt: modifiedAt
            )
        }

        let acceptedFrameCount = (manifest["frames"] as? [Any])?.count ?? 0
        let captureIntent = manifest["capture_intent"] as? String

        let finalizationResult = resolveAsset(
            manifest["finalization_report_file"] as? String ?? "metadata/finalization_report.json",
            within: directory
        )
        guard case let .success(finalizationURL) = finalizationResult else {
            return item(
                directory: directory,
                state: .invalid,
                detail: "Finalization report path escapes the capture folder.",
                modifiedAt: modifiedAt,
                acceptedFrameCount: acceptedFrameCount,
                captureIntent: captureIntent
            )
        }

        let report: [String: Any]?
        if fileManager.fileExists(atPath: finalizationURL.path) {
            do {
                report = try readJSONObject(at: finalizationURL)
            } catch {
                return item(
                    directory: directory,
                    state: .invalid,
                    detail: "Finalization report is unreadable.",
                    modifiedAt: modifiedAt,
                    acceptedFrameCount: acceptedFrameCount,
                    captureIntent: captureIntent
                )
            }
        } else {
            report = nil
        }

        let previewResult = resolveOptionalAsset(
            manifest["pointcloud_preview_file"] as? String,
            fallback: "pointcloud_preview/preview.json",
            within: directory
        )
        let roomPlanResult = resolveOptionalAsset(
            manifest["room_plan_file"] as? String,
            fallback: nil,
            within: directory
        )
        guard case let .success(previewURL) = previewResult,
              case let .success(roomPlanURL) = roomPlanResult else {
            return item(
                directory: directory,
                state: .invalid,
                detail: "An asset path escapes the capture folder.",
                modifiedAt: modifiedAt,
                acceptedFrameCount: acceptedFrameCount,
                captureIntent: captureIntent
            )
        }

        let availablePreview = existingFile(previewURL)
        let availableRoomPlan = existingFile(roomPlanURL)
        let pointCount = availablePreview.flatMap(readPointCount) ?? 0
        let reportFrameCount = report?["accepted_keyframe_count"] as? Int
        let finalFrameCount = reportFrameCount ?? acceptedFrameCount

        guard let report else {
            return item(
                directory: directory,
                state: .partial,
                detail: "Finalization report is missing; the capture remains recovery evidence.",
                modifiedAt: modifiedAt,
                acceptedFrameCount: finalFrameCount,
                captureIntent: captureIntent,
                pointCloudPreviewFile: availablePreview,
                pointCloudPointCount: pointCount,
                roomPlanFile: availableRoomPlan
            )
        }

        let manifestWritten = report["manifest_written"] as? Bool ?? false
        let status = report["status"] as? String ?? "unknown"
        let state: CaptureLibraryState
        let detail: String
        if manifestWritten && status == "finalized" {
            state = .ready
            detail = "Finalized capture bundle."
        } else if manifestWritten && status == "finalized_with_video_error" {
            state = .ready
            detail = "Finalized with a video warning; inspect the report before reconstruction."
        } else {
            state = .partial
            detail = report["finalization_error"] as? String
                ?? "Finalization status: \(status). Preserved files can still be shared."
        }

        return item(
            directory: directory,
            state: state,
            detail: detail,
            modifiedAt: modifiedAt,
            acceptedFrameCount: finalFrameCount,
            captureIntent: captureIntent,
            pointCloudPreviewFile: availablePreview,
            pointCloudPointCount: pointCount,
            roomPlanFile: availableRoomPlan
        )
    }

    private func item(
        directory: URL,
        state: CaptureLibraryState,
        detail: String,
        modifiedAt: Date,
        acceptedFrameCount: Int = 0,
        captureIntent: String? = nil,
        pointCloudPreviewFile: URL? = nil,
        pointCloudPointCount: Int = 0,
        roomPlanFile: URL? = nil
    ) -> CaptureLibraryItem {
        CaptureLibraryItem(
            directory: directory,
            state: state,
            statusDetail: detail,
            modifiedAt: modifiedAt,
            acceptedFrameCount: acceptedFrameCount,
            captureIntent: captureIntent,
            pointCloudPreviewFile: pointCloudPreviewFile,
            pointCloudPointCount: pointCloudPointCount,
            roomPlanFile: roomPlanFile
        )
    }

    private func readJSONObject(at url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return object
    }

    private func readPointCount(at url: URL) -> Int? {
        (try? readJSONObject(at: url))?["point_count"] as? Int
    }

    private func existingFile(_ url: URL?) -> URL? {
        guard let url, fileManager.fileExists(atPath: url.path) else { return nil }
        return url
    }

    private func resolveOptionalAsset(
        _ relativePath: String?,
        fallback: String?,
        within directory: URL
    ) -> Result<URL?, Error> {
        guard let path = relativePath ?? fallback else {
            return .success(nil)
        }
        switch resolveAsset(path, within: directory) {
        case let .success(url):
            return .success(url)
        case let .failure(error):
            return .failure(error)
        }
    }

    private func resolveAsset(_ relativePath: String, within directory: URL) -> Result<URL, Error> {
        let components = relativePath.split(separator: "/", omittingEmptySubsequences: false)
        guard !relativePath.isEmpty,
              !relativePath.hasPrefix("/"),
              !components.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else {
            return .failure(CocoaError(.fileReadInvalidFileName))
        }
        let candidate = directory.appendingPathComponent(relativePath).standardizedFileURL
        if fileManager.fileExists(atPath: candidate.path) {
            let resolvedRoot = directory.standardizedFileURL.resolvingSymlinksInPath()
            let resolvedCandidate = candidate.resolvingSymlinksInPath()
            let resolvedPrefix = resolvedRoot.path.hasSuffix("/")
                ? resolvedRoot.path
                : resolvedRoot.path + "/"
            guard resolvedCandidate.path.hasPrefix(resolvedPrefix) else {
                return .failure(CocoaError(.fileReadNoPermission))
            }
        }
        return .success(candidate)
    }
}

import Foundation

@main
struct CaptureLibraryProbe {
    static func main() throws {
        guard CommandLine.arguments.count == 2 else {
            throw CocoaError(.fileReadInvalidFileName)
        }
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let records = CaptureLibraryScanner().scan(root: root).map { item in
            [
                "name": item.name,
                "state": item.state.rawValue,
                "detail": item.statusDetail,
                "frame_count": item.acceptedFrameCount,
                "point_count": item.pointCloudPointCount,
                "has_preview": item.pointCloudPreviewFile != nil,
                "has_room_plan": item.roomPlanFile != nil,
            ] as [String: Any]
        }
        let data = try JSONSerialization.data(withJSONObject: records, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
    }
}

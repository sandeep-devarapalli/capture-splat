import SwiftUI

@main
struct CaptureSplatApp: App {
    @StateObject private var capture = CaptureController()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(capture)
        }
    }
}

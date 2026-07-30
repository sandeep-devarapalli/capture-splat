import SwiftUI
import UIKit

@main
struct CaptureSplatApp: App {
    @StateObject private var capture = CaptureController()
    @StateObject private var livePairing: LivePairingCoordinator

    init() {
        let deviceName = UIDevice.current.name
        let appVersion = Bundle.main.object(
            forInfoDictionaryKey: "CFBundleShortVersionString"
        ) as? String ?? "0.1.0"
        _livePairing = StateObject(
            wrappedValue: LivePairingCoordinator.application(
                deviceName: { deviceName },
                appVersion: { appVersion }
            )
        )
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(capture)
                .environmentObject(livePairing)
        }
    }
}

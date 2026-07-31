import SwiftUI
import UIKit

@main
struct CaptureSplatApp: App {
    @StateObject private var capture: CaptureController
    @StateObject private var livePairing: LivePairingCoordinator
    private let liveSender: LiveCaptureSenderBridge

    init() {
        let deviceName = UIDevice.current.name
        let appVersion = Bundle.main.object(
            forInfoDictionaryKey: "CFBundleShortVersionString"
        ) as? String ?? "0.1.0"
        let services: (
            pairing: LivePairingCoordinator,
            sender: LiveCaptureSenderBridge
        )
        do {
            let secureStore = KeychainLiveSecureValueStore()
            let recoveryStore = LivePairingRecoveryStore(secureStore: secureStore)
            let identityStore = LiveDeviceIdentityStore(secureStore: secureStore)
            let grantStore = LiveGrantStore(secureStore: secureStore)
            let pendingStore = LivePendingPairingStore(secureStore: secureStore)
            let paths = try LiveApplicationSupportPaths.application()
            let counterStore = LiveRequestCounterStore(
                stateURL: paths.requestCountersURL
            )
            let pairingClient = LivePairingClient(
                identityStore: identityStore,
                grantStore: grantStore,
                pendingStore: pendingStore,
                counterStore: counterStore
            )
            let pairing = LivePairingCoordinator(
                profileStore: LivePairingProfileStore(
                    stateURL: paths.pairingProfileURL
                ),
                recoveryStore: recoveryStore,
                grantStore: grantStore,
                pendingStore: pendingStore,
                pairingService: pairingClient,
                resolverFactory: { LiveBonjourResolver() },
                deviceName: { deviceName },
                appVersion: { appVersion },
                hasPendingLiveTransfer: LivePairingCoordinator
                    .pendingLiveTransferCheck(
                        currentSessionURL: paths.currentSessionURL,
                        pendingCaptureURL: paths.pendingCaptureURL
                    )
            )
            guard let documentsRoot = FileManager.default.urls(
                for: .documentDirectory,
                in: .userDomainMask
            ).first else {
                throw LiveAuthContractError.invalid(
                    "Capture Documents directory is unavailable."
                )
            }
            let connector = LiveCaptureSenderConnector(
                recoveryStore: recoveryStore,
                identityStore: identityStore,
                grantStore: grantStore,
                counterStore: counterStore
            )
            services = (
                pairing,
                try LiveCaptureSenderBridge.application(
                    paths: paths,
                    documentsRoot: documentsRoot,
                    connector: connector
                )
            )
        } catch {
            services = (
                LivePairingCoordinator.application(
                    deviceName: { deviceName },
                    appVersion: { appVersion }
                ),
                LiveCaptureSenderBridge.disabled()
            )
        }
        let capture = CaptureController()
        capture.setLiveSenderEventSink(services.sender)
        _capture = StateObject(wrappedValue: capture)
        _livePairing = StateObject(wrappedValue: services.pairing)
        liveSender = services.sender
    }

    var body: some Scene {
        WindowGroup {
            ContentView(liveSender: liveSender)
                .environmentObject(capture)
                .environmentObject(livePairing)
        }
    }
}

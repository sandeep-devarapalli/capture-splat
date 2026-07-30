import SwiftUI
import Vision
import VisionKit

struct LivePairingView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var coordinator: LivePairingCoordinator
    @State private var isScanning = false
    @State private var pastedInvitation = ""
    @State private var scannerError: String?
    @State private var confirmsForget = false
    @State private var confirmsResetAll = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    authorityCard
                    statusCard
                    pairingControls
                    persistenceCard
                }
                .padding()
            }
            .navigationTitle("World Studio Pairing")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
        }
        .onDisappear {
            if [.scanning, .resolving, .awaitingApproval].contains(
                coordinator.snapshot.phase
            ) {
                coordinator.cancel()
            }
        }
        .onChange(of: coordinator.snapshot.phase) { _, phase in
            if phase != .scanning {
                isScanning = false
            }
        }
    }

    private var authorityCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Proposal only", systemImage: "shield.lefthalf.filled")
                .font(.headline)
                .foregroundStyle(.orange)
            Text(
                "Live frames and reconstruction outputs are evidence proposals. "
                    + "They are not measurement, collision, navigation, semantic, or physics authority."
            )
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
        .padding()
        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityIdentifier("live-pairing-authority")
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label(statusTitle, systemImage: statusIcon)
                    .font(.headline)
                    .foregroundStyle(statusColor)
                Spacer()
                Text(coordinator.snapshot.phase.rawValue.replacingOccurrences(
                    of: "_",
                    with: " "
                ))
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            Text(coordinator.snapshot.message)
                .font(.subheadline)

            if let name = coordinator.snapshot.desktopName {
                LabeledContent("Mac", value: name)
            }
            if let identifier = coordinator.snapshot.desktopID {
                LabeledContent("Desktop identity") {
                    Text(identifier)
                        .font(.caption2.monospaced())
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
            if let expiresAt = coordinator.snapshot.grantExpiresAt {
                LabeledContent("Grant expires") {
                    Text(expiresAt)
                        .font(.caption.monospacedDigit())
                }
            }
        }
        .padding()
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .accessibilityIdentifier("live-pairing-status")
    }

    @ViewBuilder
    private var pairingControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Pair a specific Mac")
                .font(.headline)
            Text(
                "In World Studio, choose a network interface and show a fresh QR. "
                    + "Capture Splat discovers only that advertised service and pins its TLS certificate."
            )
            .font(.subheadline)
            .foregroundStyle(.secondary)

            if isScanning {
                if DataScannerViewController.isSupported,
                   DataScannerViewController.isAvailable {
                    LivePairingQRScanner(
                        onInvitation: acceptInvitation,
                        onFailure: { message in
                            scannerError = message
                            isScanning = false
                            coordinator.stopScanning()
                        }
                    )
                    .frame(height: 300)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay {
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(.white.opacity(0.65), lineWidth: 1)
                    }
                    .accessibilityIdentifier("live-pairing-qr-scanner")
                } else {
                    Text("QR scanning is unavailable on this device. Paste the invitation below.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }

                Button("Cancel QR Scan", role: .cancel) {
                    isScanning = false
                    coordinator.stopScanning()
                }
                .buttonStyle(.bordered)
            } else if canOfferNewPairing {
                Button {
                    scannerError = nil
                    coordinator.startScanning()
                    isScanning = true
                } label: {
                    Label("Scan World Studio QR", systemImage: "qrcode.viewfinder")
                }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("live-pairing-scan")
            }

            if let scannerError {
                Text(scannerError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            if canOfferNewPairing {
                Divider()
                Text("Simulator or accessibility fallback")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                SecureField(
                    "Paste capture-splat://pair/ invitation",
                    text: $pastedInvitation
                )
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .privacySensitive()
                .textContentType(.URL)

                Button("Use Pasted Invitation") {
                    let invitation = pastedInvitation
                    pastedInvitation = ""
                    acceptInvitation(invitation)
                }
                .buttonStyle(.bordered)
                .disabled(pastedInvitation.isEmpty)
                .accessibilityIdentifier("live-pairing-paste")
            }

            if isPairingBusy {
                Button("Cancel Pairing", role: .cancel) {
                    coordinator.cancel()
                }
                .buttonStyle(.bordered)
            }

            if coordinator.snapshot.canRetry {
                Button("Retry Pending Approval") {
                    coordinator.retry()
                }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("live-pairing-retry")
            }

            if coordinator.snapshot.desktopID != nil,
               coordinator.snapshot.phase != .cancelling {
                if confirmsForget {
                    Text(
                        "This clears local pending or granted pairing state. Revoke the "
                            + "device in World Studio to invalidate any Mac-side grant immediately."
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                    HStack {
                        Button("Keep Pairing") {
                            confirmsForget = false
                        }
                        Button("Clear Locally", role: .destructive) {
                            confirmsForget = false
                            Task {
                                await coordinator.clearLocalPairing()
                            }
                        }
                    }
                } else {
                    Button("Clear This Mac", role: .destructive) {
                        confirmsForget = true
                    }
                }
            }

            if coordinator.canResetAllCredentials {
                Divider()
                Text(
                    "The local pairing identity cannot be decoded. This removes the "
                        + "entire Capture Splat live Keychain service, rotates the "
                        + "device identity after restart, and resets local pairing state."
                )
                .font(.caption)
                .foregroundStyle(.red)
                if confirmsResetAll {
                    HStack {
                        Button("Keep Credentials") {
                            confirmsResetAll = false
                        }
                        Button("Confirm Reset", role: .destructive) {
                            confirmsResetAll = false
                            Task {
                                await coordinator.resetAllLocalCredentials()
                            }
                        }
                    }
                } else {
                    Button("Reset All Local Live Credentials", role: .destructive) {
                        confirmsResetAll = true
                    }
                    .accessibilityIdentifier("live-pairing-reset-all")
                }
            }
        }
        .padding()
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }

    private var persistenceCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Local-first boundary", systemImage: "internaldrive")
                .font(.headline)
            Text(
                "Device keys, grants, pending requests, and a one-Mac recovery pointer "
                    + "stay in Keychain. A rebuildable non-secret desktop cache and "
                    + "request counters live in Application Support. "
                    + "No capture files are queued or uploaded in this milestone."
            )
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
        .padding()
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }

    private var isPairingBusy: Bool {
        coordinator.snapshot.phase == .resolving
            || coordinator.snapshot.phase == .awaitingApproval
            || coordinator.snapshot.phase == .cancelling
    }

    private var canOfferNewPairing: Bool {
        coordinator.canStartNewPairing
            && !isScanning
            && !coordinator.snapshot.canRetry
    }

    private var statusTitle: String {
        switch coordinator.snapshot.phase {
        case .off:
            return "Live transfer off"
        case .scanning:
            return "Scanning invitation"
        case .resolving:
            return "Resolving World Studio"
        case .awaitingApproval:
            return "Approval required on Mac"
        case .cancelling:
            return "Cancelling pairing"
        case .paired:
            return "Paired"
        case .interrupted:
            return "Pairing interrupted"
        case .failed:
            return "Pairing failed closed"
        }
    }

    private var statusIcon: String {
        switch coordinator.snapshot.phase {
        case .paired:
            return "checkmark.shield"
        case .scanning:
            return "qrcode.viewfinder"
        case .resolving, .awaitingApproval, .cancelling:
            return "antenna.radiowaves.left.and.right"
        case .interrupted:
            return "wifi.exclamationmark"
        case .failed:
            return "xmark.shield"
        case .off:
            return "antenna.radiowaves.left.and.right.slash"
        }
    }

    private var statusColor: Color {
        switch coordinator.snapshot.phase {
        case .paired:
            return .green
        case .scanning, .resolving, .awaitingApproval:
            return .blue
        case .cancelling:
            return .orange
        case .interrupted:
            return .orange
        case .failed:
            return .red
        case .off:
            return .secondary
        }
    }

    private func acceptInvitation(_ value: String) {
        guard !value.isEmpty else { return }
        isScanning = false
        scannerError = nil
        coordinator.beginPairing(invitationURI: value)
    }
}

private struct LivePairingQRScanner: UIViewControllerRepresentable {
    let onInvitation: (String) -> Void
    let onFailure: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(
            onInvitation: onInvitation,
            onFailure: onFailure
        )
    }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true,
            isGuidanceEnabled: true,
            isHighlightingEnabled: true
        )
        scanner.delegate = context.coordinator
        return scanner
    }

    func updateUIViewController(
        _ uiViewController: DataScannerViewController,
        context: Context
    ) {
        guard !context.coordinator.didStart else { return }
        context.coordinator.didStart = true
        do {
            try uiViewController.startScanning()
        } catch {
            context.coordinator.onFailure(
                "The QR scanner could not start. Paste the invitation instead."
            )
        }
    }

    static func dismantleUIViewController(
        _ uiViewController: DataScannerViewController,
        coordinator: Coordinator
    ) {
        uiViewController.stopScanning()
        uiViewController.delegate = nil
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let onInvitation: (String) -> Void
        let onFailure: (String) -> Void
        var didStart = false
        private var emitted = false

        init(
            onInvitation: @escaping (String) -> Void,
            onFailure: @escaping (String) -> Void
        ) {
            self.onInvitation = onInvitation
            self.onFailure = onFailure
        }

        func dataScanner(
            _ dataScanner: DataScannerViewController,
            didAdd addedItems: [RecognizedItem],
            allItems: [RecognizedItem]
        ) {
            guard !emitted else { return }
            for item in addedItems {
                guard case .barcode(let barcode) = item,
                      let value = barcode.payloadStringValue,
                      value.hasPrefix(LiveAuthContract.qrPrefix) else {
                    continue
                }
                emitted = true
                dataScanner.stopScanning()
                onInvitation(value)
                return
            }
        }
    }
}

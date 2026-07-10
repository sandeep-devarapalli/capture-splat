import Foundation
import RoomPlan
import SceneKit
import SwiftUI

private enum WorkspaceTab: String, CaseIterable, Identifiable {
    case capture = "Capture"
    case reconstruct = "Reconstruct"
    case projects = "Projects"

    var id: String { rawValue }
}

private enum ScanMode: String, CaseIterable, Identifiable {
    case objectOrbit = "Object"
    case roomWalk = "Room"
    case video3DGS = "Video 3DGS"
    case outdoor = "Outdoor"
    case flipObject = "Flip"

    var id: String { rawValue }
    static let activeCases: [ScanMode] = [.video3DGS]

    var controllerTargetMode: String {
        switch self {
        case .objectOrbit, .flipObject:
            return "object"
        case .roomWalk:
            return "room"
        case .video3DGS:
            return "video_3dgs"
        case .outdoor:
            return "outdoor"
        }
    }
}

private enum ScanViewMode: String, CaseIterable, Identifiable {
    case scan = "Scan"
    case camera = "Camera"

    var id: String { rawValue }
}

private enum ActiveSheet: String, Identifiable {
    case export
    case camera
    case review
    case roomPlan

    var id: String { rawValue }
}

struct ContentView: View {
    @EnvironmentObject private var capture: CaptureController
    @State private var selectedTab: WorkspaceTab = .capture
    @State private var scanMode: ScanMode = .video3DGS
    @State private var viewMode: ScanViewMode = .scan
    @State private var isCapturePanelExpanded = false
    @State private var activeSheet: ActiveSheet?

    var body: some View {
        ZStack {
            ARCaptureView()
                .ignoresSafeArea()

            if selectedTab == .capture, viewMode == .scan {
                scanGuidanceOverlay
            }

            VStack(spacing: 12) {
                topBar
                Spacer()
                if selectedTab == .reconstruct {
                    reconstructionPanel
                } else if selectedTab == .projects {
                    projectPanel
                }
                if selectedTab == .capture {
                    capturePanel
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 12)
        }
        .task {
            capture.prepareSensors()
            capture.setScanTargetMode(scanMode.controllerTargetMode)
        }
        .onChange(of: scanMode) { _, mode in
            capture.setScanTargetMode(mode.controllerTargetMode)
        }
        .sheet(item: $activeSheet) { sheet in
            switch sheet {
            case .export:
                exportPresetSheet
            case .camera:
                cameraSettingsSheet
            case .review:
                pointCloudReviewSheet
            case .roomPlan:
                roomPlanCaptureSheet
            }
        }
    }

    private var topBar: some View {
        HStack(spacing: 10) {
            Picker("Workspace", selection: $selectedTab) {
                ForEach(WorkspaceTab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .disabled(capture.isRecording || capture.isFinalizing)

            Button {
                activeSheet = .camera
            } label: {
                Image(systemName: "camera.aperture")
            }
            .buttonStyle(.bordered)
            .accessibilityLabel("Camera settings")
        }
        .padding(8)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var capturePanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            capturePanelHeader

            if isCapturePanelExpanded {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        scanModeControls
                        if scanMode != .video3DGS {
                            targetLockCard
                        }
                        if scanMode == .objectOrbit {
                            objectExtentCard
                        }
                        if scanMode == .video3DGS {
                            videoCaptureCard
                        }
                        recordExportControls
                        guidanceReadinessCard
                        if capture.isRecording || capture.captureBlockerStatus != "Clear" {
                            captureBlockerCard
                        }
                        keyframeStatusCard
                        if scanMode == .roomWalk {
                            colmapCoachCard
                            roomQualityCard
                        }
                        roomPlanCard
                        coverageStrip
                        sensorToggles

                        HStack(alignment: .top, spacing: 10) {
                            metricGrid
                            healthGrid
                        }

                        Text(capture.statusText)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                .scrollIndicators(.visible)
                .frame(maxHeight: 420)
            } else {
                compactCapturePanel
            }
        }
        .padding(10)
        .frame(maxWidth: 560)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var capturePanelHeader: some View {
        HStack(spacing: 10) {
            Capsule()
                .fill(.secondary.opacity(0.45))
                .frame(width: 44, height: 4)
                .accessibilityHidden(true)
            Text(isCapturePanelExpanded ? "Scan Details" : capture.targetLockStatus)
                .font(.caption)
                .fontWeight(.semibold)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Spacer()
            Button {
                withAnimation(.spring(response: 0.25, dampingFraction: 0.85)) {
                    isCapturePanelExpanded.toggle()
                }
            } label: {
                Image(systemName: isCapturePanelExpanded ? "chevron.down" : "chevron.up")
            }
            .buttonStyle(.bordered)
            .accessibilityLabel(isCapturePanelExpanded ? "Collapse scan details" : "Expand scan details")
        }
    }

    private var compactCapturePanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Video 3DGS Max", systemImage: "video")
                    .font(.caption)
                    .fontWeight(.semibold)
                Spacer()
                Text(capture.targetLockStatus)
                    .font(.caption2)
                    .foregroundStyle(targetLockColor)
                    .lineLimit(1)
            }

            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    compactIntentAndTargetControls
                    compactRoomPlanButton
                    compactRecordButton
                }
                VStack(spacing: 8) {
                    HStack(spacing: 8) {
                        compactIntentAndTargetControls
                    }
                    HStack(spacing: 8) {
                        compactRoomPlanButton
                        compactRecordButton
                    }
                }
            }

            HStack(spacing: 8) {
                Label(capture.nextAction, systemImage: readinessIcon)
                    .foregroundStyle(readinessColor)
                    .fontWeight(.semibold)
                Spacer()
                Text("\(coveredSectorCount)/12")
                    .monospacedDigit()
                Text(capture.targetLockDistanceText)
                    .foregroundStyle(.secondary)
            }
            .font(.caption)
            .lineLimit(1)
            .minimumScaleFactor(0.75)

            coverageStrip

            if scanMode == .roomWalk {
                HStack(spacing: 8) {
                    Label(capture.colmapCoachAction, systemImage: "camera.metering.matrix")
                        .foregroundStyle(colmapCoachColor)
                    Spacer()
                    Text(capture.colmapFeatureText)
                        .foregroundStyle(.secondary)
                }
                .font(.caption2)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            } else if scanMode == .video3DGS {
                HStack(spacing: 8) {
                    Label(capture.captureProfileText, systemImage: "video")
                        .foregroundStyle(.cyan)
                    Spacer()
                    Text("\(capture.acceptedKeyframes) kept")
                        .foregroundStyle(.secondary)
                }
                .font(.caption2)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            }

            if capture.captureBlockerStatus != "Clear" {
                Label(capture.captureBlockerStatus, systemImage: "hand.raised")
                    .font(.caption2)
                    .fontWeight(.semibold)
                    .foregroundStyle(captureBlockerColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
        }
    }

    private var scanModeControls: some View {
        VStack(spacing: 10) {
            HStack(spacing: 10) {
                Label("Video 3DGS Max", systemImage: "video")
                    .font(.caption)
                    .fontWeight(.semibold)
                Spacer()
                captureIntentPicker
            }
            HStack(spacing: 10) {
                Picker("View", selection: $viewMode) {
                    ForEach(ScanViewMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 160)

                Toggle("Auto", isOn: $capture.isSmartAutoCaptureEnabled)
                    .toggleStyle(.button)
                    .font(.caption)
                    .disabled(capture.isRecording)

                Toggle("Lock", isOn: $capture.isCaptureLockEnabled)
                    .toggleStyle(.button)
                    .font(.caption)
                    .disabled(capture.isRecording)

                Spacer()
                compactRoomPlanButton
            }
        }
    }

    private var captureIntentPicker: some View {
        Menu {
            Picker("Capture Intent", selection: captureIntentBinding) {
                ForEach(CaptureController.captureIntentOptions) { intent in
                    Label(intent.title, systemImage: intent.systemImage).tag(intent.id)
                }
            }
        } label: {
            Label(capture.currentCaptureIntentOption.shortTitle, systemImage: capture.currentCaptureIntentOption.systemImage)
        }
        .buttonStyle(.bordered)
        .disabled(capture.isRecording)
        .accessibilityLabel("Capture intent")
        .accessibilityValue(capture.currentCaptureIntentOption.title)
    }

    private var compactIntentAndTargetControls: some View {
        Group {
            captureIntentPicker
            if capture.requiresSubjectTarget {
                Button {
                    if capture.isObjectTargetLocked {
                        capture.clearTargetLock()
                    } else {
                        capture.lockSubjectTargetIfStable()
                    }
                } label: {
                    Label(
                        capture.isObjectTargetLocked ? "Reset" : "Center",
                        systemImage: capture.isObjectTargetLocked ? "scope" : "viewfinder"
                    )
                }
                .buttonStyle(.bordered)
                .disabled(
                    capture.isRecording || capture.isFinalizing
                        || (!capture.isObjectTargetLocked && !capture.isSubjectTargetReady)
                )
                .accessibilityLabel(capture.isObjectTargetLocked ? "Reset subject target" : "Center subject target")
            }
        }
        .font(.caption)
    }

    private var compactRoomPlanButton: some View {
        Button {
            activeSheet = .roomPlan
        } label: {
            Image(systemName: "map")
        }
        .buttonStyle(.bordered)
        .disabled(capture.isRecording || capture.isFinalizing)
        .accessibilityLabel("Open Room Plan")
    }

    private var compactRecordButton: some View {
        Button {
            capture.isRecording ? capture.stopRecording() : capture.startRecording()
        } label: {
            Label(recordButtonTitle, systemImage: recordButtonIcon)
        }
        .buttonStyle(.borderedProminent)
        .disabled(
            capture.isFinalizing || !capture.isRGBEnabled || !capture.isDepthEnabled
                || (!capture.isRecording && !canRecordCurrentMode)
        )
        .accessibilityHint(capture.requiresSubjectTarget && !capture.isObjectTargetLocked
            ? "Locks the centered subject and starts capture"
            : "Starts or stops the Video 3DGS capture")
    }

    private var recordExportControls: some View {
        HStack(spacing: 10) {
            Button {
                capture.isRecording ? capture.stopRecording() : capture.startRecording()
            } label: {
                Label(recordButtonTitle, systemImage: recordButtonIcon)
            }
            .buttonStyle(.borderedProminent)
            .disabled(capture.isFinalizing || !capture.isRGBEnabled || !capture.isDepthEnabled || (!capture.isRecording && !canRecordCurrentMode))

            Button {
                activeSheet = .export
            } label: {
                Label("Export", systemImage: "square.and.arrow.down")
            }
            .buttonStyle(.bordered)
            .disabled(capture.isRecording || capture.isFinalizing || !capture.isCapturePackageReady)

            if let directory = capture.currentSessionDirectory,
               capture.isCapturePackageReady || capture.hasRecoverablePartialCapture {
                ShareLink(item: directory) {
                    Label(capture.isCapturePackageReady ? "Share" : "Share Partial", systemImage: "square.and.arrow.up")
                }
                .buttonStyle(.bordered)
                .disabled(capture.isRecording || capture.isFinalizing)
            }
        }
    }

    private var guidanceReadinessCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 8) {
                Label(capture.readinessState, systemImage: readinessIcon)
                    .font(.subheadline)
                    .fontWeight(.semibold)
                    .foregroundStyle(readinessColor)
                Spacer()
                Text("\(capture.missingSectorCount) missing")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            Label(capture.nextAction, systemImage: "arrow.triangle.turn.up.right.diamond")
                .font(.caption)
                .fontWeight(.semibold)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            Text(capture.backgroundWarning)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            Label(capture.coverageNavigationText, systemImage: "scope")
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var captureBlockerCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label(capture.captureBlockerStatus, systemImage: "hand.raised")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(captureBlockerColor)
                Spacer()
                Text(capture.lastKeyframeDecision)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }

            Text(capture.captureBlockerDetail)
                .font(.caption)
                .fontWeight(.semibold)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            Text(capture.lastAcceptedViewHint)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var videoCaptureCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(capture.captureProfileText, systemImage: "video")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(.cyan)
                Spacer()
                Text("\(capture.acceptedKeyframes) kept")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            Text(capture.captureProfileDetail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)

            Label("Move like a slow video; haptics mark sharp RGB-D frames for the Mac 3DGS gate.", systemImage: "record.circle")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var targetLockCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(capture.targetLockStatus, systemImage: targetLockIcon)
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(targetLockColor)
                Spacer()
                Text(capture.targetLockDistanceText)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            Text(capture.targetLockDetail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                Button {
                    if scanMode == .roomWalk {
                        capture.lockRoomTarget()
                    } else {
                        capture.lockObjectTarget()
                    }
                } label: {
                    Label(scanMode == .roomWalk ? "Lock Room" : "Lock Object", systemImage: "scope")
                }
                .buttonStyle(.bordered)
                .disabled(capture.isRecording || scanMode == .outdoor)

                Button {
                    capture.clearTargetLock()
                } label: {
                    Image(systemName: "xmark.circle")
                }
                .buttonStyle(.bordered)
                .disabled(capture.isRecording)
                .accessibilityLabel("Clear target lock")
            }
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var objectExtentCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(capture.objectExtentStatus, systemImage: capture.isObjectExtentLocked ? "crop.rotate" : "crop")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(capture.isObjectExtentLocked ? .green : .yellow)
                Spacer()
                Text(capture.objectExtentSizeText)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            Text(capture.objectExtentDetail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                Button {
                    capture.lockObjectExtent()
                } label: {
                    Label("Lock Extent", systemImage: "crop")
                }
                .buttonStyle(.bordered)
                .disabled(capture.isRecording || scanMode == .roomWalk || scanMode == .outdoor || !capture.isObjectTargetLocked)

                Toggle("Mask", isOn: $capture.isObjectMaskEnabled)
                    .toggleStyle(.button)
                    .font(.caption)
                    .disabled(capture.isRecording)
            }
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var keyframeStatusCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label("Smart Keyframes", systemImage: capture.isSmartAutoCaptureEnabled ? "camera.metering.center.weighted" : "timer")
                    .font(.caption)
                    .fontWeight(.semibold)
                Spacer()
                Text("\(capture.acceptedKeyframes) kept | \(capture.skippedKeyframes) skipped")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            HStack {
                Text(capture.lastKeyframeDecision)
                    .font(.caption2)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                Spacer()
                Text(String(format: "%.2f", capture.keyframeScore))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var colmapCoachCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label(capture.colmapCoachStatus, systemImage: "camera.metering.matrix")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(colmapCoachColor)
                Spacer()
                Text(String(format: "%.0f%%", capture.colmapCoachScore * 100))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            Text(capture.colmapCoachAction)
                .font(.caption)
                .fontWeight(.semibold)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                Text(capture.colmapFeatureText)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                Spacer()
                Text(capture.roomOverlapText)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }

            ProgressView(value: capture.colmapCoachScore, total: 1)
                .tint(colmapCoachColor)

            Text(capture.colmapCoachDetail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var roomQualityCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label("Room Quality", systemImage: "figure.walk.motion")
                    .font(.caption)
                    .fontWeight(.semibold)
                Spacer()
                Text("\(capture.hapticAcceptedCount) haptics")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Text(capture.roomQualityText)
                .font(.caption2)
                .fontWeight(.semibold)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            Text(capture.roomLoopText)
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Text(capture.roomOverlapText)
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Text(capture.captureQualityText)
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var roomPlanCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label(capture.roomPlanStatus, systemImage: "map")
                    .font(.caption)
                    .fontWeight(.semibold)
                Spacer()
                Text(capture.roomPlanSummaryText)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.65)
            }

            Text(capture.roomPlanDetail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            Button {
                activeSheet = .roomPlan
            } label: {
                Label("Open Room Plan", systemImage: "map")
            }
            .buttonStyle(.bordered)
            .disabled(capture.isRecording)
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var coverageStrip: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label(viewMode == .scan ? "Angle Coverage" : "Camera View", systemImage: viewMode == .scan ? "circle.hexagongrid" : "camera")
                Spacer()
                Text("\(capture.coverageHintText) | \(scanModeStatus)")
                    .foregroundStyle(.secondary)
            }
            .font(.caption)

            GeometryReader { proxy in
                HStack(spacing: 4) {
                    ForEach(0..<capture.coverageSectors.count, id: \.self) { index in
                        RoundedRectangle(cornerRadius: 3)
                            .fill(coverageColor(score: capture.coverageSectors[index]))
                            .frame(width: max((proxy.size.width - 44) / 12, 8), height: 8)
                    }
                }
            }
            .frame(height: 8)
        }
    }

    private var scanGuidanceOverlay: some View {
        GeometryReader { proxy in
            ZStack(alignment: .topLeading) {
                ForEach(capture.guidancePoints) { point in
                    Circle()
                        .fill(guidancePointColor(point.depthMeters))
                        .frame(width: guidancePointSize(point.depthMeters), height: guidancePointSize(point.depthMeters))
                        .position(
                            x: point.normalizedX * proxy.size.width,
                            y: point.normalizedY * proxy.size.height
                        )
                        .opacity(0.32)
                }

                objectExtentOverlay(in: proxy)

                VStack(alignment: .leading, spacing: 8) {
                    Label(primaryOverlayAction, systemImage: overlayActionIcon)
                        .font(.caption)
                        .fontWeight(.semibold)
                        .foregroundStyle(overlayActionColor)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(.thinMaterial, in: Capsule())

                    CoverageMiniMap(
                        scores: capture.coverageSectors,
                        currentIndex: capture.currentCoverageSector,
                        targetIndex: capture.targetCoverageSector
                    )
                        .frame(width: 156, height: 36)

                    if scanMode == .roomWalk {
                        Text("\(capture.colmapFeatureText) | \(capture.roomOverlapText)")
                            .font(.caption2.monospacedDigit())
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(.thinMaterial, in: Capsule())
                    }
                }
                .padding(.top, 72)
                .padding(.leading, 14)

                VStack(spacing: 6) {
                    targetReticle
                    Text(primaryOverlayAction)
                        .font(.caption2)
                        .fontWeight(.semibold)
                        .lineLimit(2)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(.thinMaterial, in: Capsule())
                    Text(primaryOverlayDetail)
                        .font(.caption2)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(.thinMaterial, in: Capsule())
                }
                .frame(maxWidth: 220)
                .position(x: proxy.size.width * 0.5, y: proxy.size.height * 0.32)
            }
            .allowsHitTesting(false)
        }
    }

    private var targetReticle: some View {
        ZStack {
            Circle()
                .stroke(targetLockColor, lineWidth: 2)
                .frame(width: 88, height: 88)
            Circle()
                .stroke(targetLockColor.opacity(0.55), lineWidth: 1)
                .frame(width: 22, height: 22)
            Image(systemName: targetLockIcon)
                .font(.caption)
                .foregroundStyle(targetLockColor)
        }
        .shadow(radius: 4)
    }

    private func objectExtentOverlay(in proxy: GeometryProxy) -> some View {
        Group {
            if let box = capture.objectExtentOverlay, scanMode == .objectOrbit {
                Rectangle()
                    .stroke(capture.isObjectExtentLocked ? .green : .yellow, style: StrokeStyle(lineWidth: 2, dash: [7, 5]))
                    .frame(
                        width: max(box.normalizedWidth * proxy.size.width, 42),
                        height: max(box.normalizedHeight * proxy.size.height, 42)
                    )
                    .position(
                        x: (box.normalizedX + box.normalizedWidth * 0.5) * proxy.size.width,
                        y: (box.normalizedY + box.normalizedHeight * 0.5) * proxy.size.height
                    )
            }
        }
    }

    private var sensorToggles: some View {
        HStack(spacing: 10) {
            sensorToggle("RGB", isOn: $capture.isRGBEnabled, disabled: true)
            sensorToggle("Depth", isOn: $capture.isDepthEnabled, disabled: true)
            sensorToggle("Conf", isOn: $capture.isConfidenceEnabled)
            sensorToggle("IMU", isOn: $capture.isIMUEnabled)
            sensorToggle("GPS", isOn: $capture.isGPSEnabled)
        }
        .disabled(capture.isRecording)
    }

    private var metricGrid: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
            GridRow {
                statusItem("RGB", capture.rgbFrames, rate: capture.rgbRate)
                statusItem("Depth", capture.depthFrames, rate: capture.depthRate)
            }
            GridRow {
                statusItem("IMU", capture.imuRows, rate: capture.imuRate)
                statusItem("GPS", capture.gpsRows, rate: capture.gpsRate)
            }
        }
        .font(.caption.monospacedDigit())
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var healthGrid: some View {
        Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 6) {
            GridRow {
                healthItem("Drop", "\(capture.droppedFrames)")
                healthItem("Depth", percent(capture.validDepthRatio))
            }
            GridRow {
                healthItem("Thermal", capture.thermalStateText)
                healthItem("Battery", capture.batteryText)
            }
            GridRow {
                healthItem("Storage", capture.storageFreeText)
                healthItem("Track", capture.trackingStatus)
            }
        }
        .font(.caption2.monospacedDigit())
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var reconstructionPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Reconstruction", systemImage: "cube.transparent")
                    .font(.headline)
                Spacer()
                Text(capturePackageStatus)
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(.thinMaterial, in: Capsule())
            }

            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                stageCard("Capture Bundle", state: capture.isCapturePackageReady ? "Ready" : capturePackageStatus, icon: "folder")
                stageCard("Point Cloud + LAS", state: "Mac Gate", icon: "point.3.connected.trianglepath.dotted")
                stageCard("COLMAP Bridge", state: "Converter Ready", icon: "camera.metering.matrix")
                stageCard("Nerfstudio", state: "Parser Gate", icon: "film.stack")
                stageCard("3DGS", state: "Not Started", icon: "sparkles")
                stageCard("3DGS Viewer", state: "Preview", icon: "display")
            }
        }
        .padding(14)
        .frame(maxWidth: 560)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var projectPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Projects", systemImage: "square.stack.3d.up")
                .font(.headline)

            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(capture.currentSessionDirectory?.lastPathComponent ?? "No active capture")
                        .font(.subheadline)
                        .fontWeight(.semibold)
                        .lineLimit(1)
                    Text("\(capture.rgbFrames) RGB | \(capture.depthFrames) depth | \(capture.imuRows) IMU | \(capture.gpsRows) GPS")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Text(capture.isRecording ? "Recording" : projectState)
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(.thinMaterial, in: Capsule())
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Label("Review", systemImage: "point.3.connected.trianglepath.dotted")
                        .font(.caption)
                        .fontWeight(.semibold)
                    Spacer()
                    Text("\(capture.acceptedKeyframes) kept | \(capture.skippedKeyframes) held")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                CoverageMiniMap(
                    scores: capture.coverageSectors,
                    currentIndex: capture.currentCoverageSector,
                    targetIndex: capture.targetCoverageSector
                )
                    .frame(height: 36)
                Text("\(capture.captureBlockerStatus): \(capture.captureBlockerDetail)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    activeSheet = .review
                } label: {
                    Label("Open LiDAR Preview", systemImage: "cube.transparent")
                }
                .buttonStyle(.bordered)
                .disabled(capture.pointCloudPreviewPointCount == 0)
                Text("\(capture.pointCloudPreviewPointCount) preview points")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)

                Divider()

                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(capture.roomPlanStatus)
                            .font(.caption)
                            .fontWeight(.semibold)
                        Text(capture.roomPlanSummaryText)
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                    }
                    Spacer()
                    Button {
                        activeSheet = .roomPlan
                    } label: {
                        Label("Room", systemImage: "map")
                    }
                    .buttonStyle(.bordered)
                    .disabled(capture.isRecording)
                }
            }
            .padding(10)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .padding(14)
        .frame(maxWidth: 560)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var exportPresetSheet: some View {
        NavigationStack {
            List {
                Section("Primary") {
                    Button {
                        capture.finalizeSession()
                        activeSheet = nil
                    } label: {
                        exportRow("Capture Bundle", detail: "Raw RGB-D, IMU, GNSS, metadata", icon: "archivebox", enabled: capture.isCapturePackageReady)
                    }
                    .disabled(!capture.isCapturePackageReady || capture.isRecording || capture.isFinalizing)
                    exportRow("Room Plan", detail: "RoomPlan USDZ and conservative layout report", icon: "map", enabled: capture.roomPlanFile != nil)
                    exportRow("PLY + LAS", detail: "Mac ingest output", icon: "point.3.connected.trianglepath.dotted", enabled: false)
                    exportRow("Nerfstudio", detail: "Images and transforms.json", icon: "film.stack", enabled: false)
                    exportRow("COLMAP", detail: "sparse/0 text model", icon: "camera.metering.matrix", enabled: false)
                    exportRow("Video to 3DGS", detail: "Mac keyframe extraction and OpenSplat/COLMAP gate", icon: "video", enabled: false)
                }
                Section("Inspection") {
                    exportRow("3DGS Viewer", detail: "Viewer manifest", icon: "display", enabled: false)
                    exportRow("Viewer Package", detail: "After trained splat exists", icon: "safari", enabled: false)
                }
            }
            .navigationTitle("Export")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") {
                        activeSheet = nil
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private var pointCloudReviewSheet: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 10) {
                if let url = capture.pointCloudPreviewFile, capture.pointCloudPreviewPointCount > 0 {
                    PointCloudPreviewScene(url: url)
                        .frame(minHeight: 360)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    Text("\(capture.pointCloudPreviewPointCount) sampled LiDAR points")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                } else {
                    VStack(spacing: 10) {
                        Image(systemName: "cube.transparent")
                            .font(.largeTitle)
                        Text("No preview points yet")
                            .font(.headline)
                        Text("Record accepted RGB-D keyframes before opening the LiDAR preview.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity, minHeight: 360)
                }
            }
            .padding()
            .navigationTitle("LiDAR Preview")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") {
                        activeSheet = nil
                    }
                }
            }
        }
        .presentationDetents([.large])
    }

    @ViewBuilder
    private var roomPlanCaptureSheet: some View {
        if #available(iOS 16.0, *) {
            RoomPlanCaptureSheet()
        } else {
            Text("RoomPlan requires iOS 16 or later.")
                .padding()
        }
    }

    private var cameraSettingsSheet: some View {
        NavigationStack {
            Form {
                Section("Capture") {
                    Toggle("Smart Auto Capture", isOn: $capture.isSmartAutoCaptureEnabled)
                    Toggle("Subject Mask", isOn: $capture.isObjectMaskEnabled)
                }
            }
            .navigationTitle("Camera")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") {
                        activeSheet = nil
                    }
                }
            }
        }
        .presentationDetents([.medium])
    }

    private func sensorToggle(_ title: String, isOn: Binding<Bool>, disabled: Bool = false) -> some View {
        Toggle(title, isOn: isOn)
            .toggleStyle(.button)
            .font(.caption)
            .disabled(disabled)
            .accessibilityLabel("\(title) capture")
    }

    private func statusItem(_ title: String, _ value: Int, rate: Double) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
            Text("\(value)")
                .fontWeight(.semibold)
            Text(rateText(rate))
                .foregroundStyle(.secondary)
        }
        .frame(minWidth: 68, alignment: .leading)
    }

    private func healthItem(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .foregroundStyle(.secondary)
            Text(value)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
        .frame(minWidth: 58, alignment: .leading)
    }

    private func stageCard(_ title: String, state: String, icon: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: icon)
                .font(.headline)
            Text(title)
                .font(.caption)
                .fontWeight(.semibold)
            Text(state)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func exportRow(_ title: String, detail: String, icon: String, enabled: Bool) -> some View {
        HStack {
            Label(title, systemImage: icon)
            Spacer()
            Text(enabled ? "Ready" : "Mac Gate")
                .font(.caption)
                .foregroundStyle(enabled ? .primary : .secondary)
        }
        .accessibilityHint(detail)
    }

    private func coverageColor(score: Double) -> Color {
        if score >= 1 {
            return .green.opacity(0.85)
        }
        if score > 0 {
            return .yellow.opacity(0.85)
        }
        return .secondary.opacity(0.35)
    }

    private func guidancePointColor(_ depth: Double) -> Color {
        if depth < 1.25 {
            return .mint
        }
        if depth < 3.0 {
            return .cyan
        }
        return .orange
    }

    private func guidancePointSize(_ depth: Double) -> CGFloat {
        if depth < 1.25 {
            return 7
        }
        if depth < 3.0 {
            return 5
        }
        return 4
    }

    private func rateText(_ value: Double) -> String {
        value > 0 ? String(format: "%.1f Hz", value) : "-- Hz"
    }

    private func percent(_ value: Double) -> String {
        value > 0 ? "\(Int((value * 100).rounded()))%" : "--"
    }

    private var scanModeIcon: String {
        switch scanMode {
        case .objectOrbit:
            return "viewfinder.circle"
        case .roomWalk:
            return "house"
        case .video3DGS:
            return "video"
        case .outdoor:
            return "location"
        case .flipObject:
            return "arrow.triangle.2.circlepath"
        }
    }

    private var scanModeStatus: String {
        switch scanMode {
        case .objectOrbit:
            return capture.isSmartAutoCaptureEnabled ? "Smart orbit" : "Timed orbit"
        case .roomWalk:
            return "Room path"
        case .video3DGS:
            return "Video 3DGS"
        case .outdoor:
            return "GNSS"
        case .flipObject:
            return "Phase A"
        }
    }

    private var projectState: String {
        capturePackageStatus
    }

    private var capturePackageStatus: String {
        switch capture.capturePackageState {
        case .idle: return "No Session"
        case .recording: return "Recording"
        case .finalizing: return "Finalizing"
        case .ready: return "Captured"
        case .partial: return "Incomplete"
        }
    }

    private var primaryOverlayAction: String {
        if capture.captureBlockerStatus != "Clear" {
            return capture.captureBlockerStatus
        }
        if scanMode == .roomWalk {
            return capture.colmapCoachAction
        }
        return capture.nextAction
    }

    private var primaryOverlayDetail: String {
        if capture.captureBlockerStatus != "Clear" {
            return capture.captureBlockerDetail
        }
        return scanMode == .roomWalk ? capture.colmapCoachDetail : capture.coverageNavigationText
    }

    private var overlayActionIcon: String {
        if capture.captureBlockerStatus != "Clear" {
            return "hand.raised"
        }
        return scanMode == .roomWalk ? "camera.metering.matrix" : readinessIcon
    }

    private var overlayActionColor: Color {
        if capture.captureBlockerStatus != "Clear" {
            return captureBlockerColor
        }
        return scanMode == .roomWalk ? colmapCoachColor : readinessColor
    }

    private var canRecordCurrentMode: Bool {
        switch scanMode {
        case .objectOrbit, .flipObject:
            return capture.isObjectTargetLocked
        case .roomWalk:
            return capture.isRoomTargetLocked
        case .video3DGS:
            return !capture.requiresSubjectTarget || capture.isObjectTargetLocked || capture.isSubjectTargetReady
        case .outdoor:
            return true
        }
    }

    private var captureIntentBinding: Binding<String> {
        Binding(
            get: { capture.captureIntent },
            set: { capture.setCaptureIntent($0) }
        )
    }

    private var coveredSectorCount: Int {
        capture.coverageSectors.filter { $0 >= 1 }.count
    }

    private var targetLockColor: Color {
        capture.isObjectTargetLocked || !capture.requiresSubjectTarget ? .green : .yellow
    }

    private var targetLockIcon: String {
        capture.isObjectTargetLocked || !capture.requiresSubjectTarget ? "checkmark.viewfinder" : "viewfinder"
    }

    private var recordButtonTitle: String {
        if capture.isFinalizing { return "Finalizing" }
        if capture.isRecording { return "Stop" }
        if capture.requiresSubjectTarget && !capture.isObjectTargetLocked { return "Lock & Record" }
        return "Record"
    }

    private var recordButtonIcon: String {
        if capture.isFinalizing { return "hourglass" }
        if capture.isRecording { return "stop.fill" }
        return capture.requiresSubjectTarget && !capture.isObjectTargetLocked ? "viewfinder.circle" : "record.circle"
    }

    private var readinessColor: Color {
        switch capture.readinessState {
        case "Ready":
            return .green
        case "Good", "Almost":
            return .yellow
        case "Hold":
            return .orange
        default:
            return .red
        }
    }

    private var captureBlockerColor: Color {
        switch capture.captureBlockerStatus {
        case "Clear":
            return .green
        case "Side-step now", "Needs translation":
            return .yellow
        case "Waiting":
            return .secondary
        default:
            return .orange
        }
    }

    private var colmapCoachColor: Color {
        if capture.colmapCoachScore >= 0.75 {
            return .green
        }
        if capture.colmapCoachScore >= 0.45 {
            return .yellow
        }
        if capture.colmapCoachScore > 0 {
            return .orange
        }
        return .red
    }

    private var readinessIcon: String {
        switch capture.readinessState {
        case "Ready":
            return "checkmark.seal"
        case "Good", "Almost":
            return "arrow.triangle.2.circlepath"
        case "Hold":
            return "hand.raised"
        default:
            return "location.viewfinder"
        }
    }
}

@available(iOS 16.0, *)
private struct RoomPlanCaptureSheet: View {
    @EnvironmentObject private var capture: CaptureController
    @Environment(\.dismiss) private var dismiss
    @State private var isRunning = true

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 10) {
                RoomPlanCaptureRepresentable(capture: capture, isRunning: $isRunning)
                    .frame(minHeight: 420)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(capture.roomPlanStatus)
                            .font(.caption)
                            .fontWeight(.semibold)
                        Text(capture.roomPlanDetail)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Text(capture.roomPlanSummaryText)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                }

                Button {
                    if isRunning {
                        isRunning = false
                    } else {
                        dismiss()
                    }
                } label: {
                    Label(isRunning ? "Stop and Export" : "Done", systemImage: isRunning ? "stop.fill" : "checkmark")
                }
                .buttonStyle(.borderedProminent)
            }
            .padding()
            .navigationTitle("Room Plan")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") {
                        isRunning = false
                        dismiss()
                    }
                }
            }
        }
        .presentationDetents([.large])
    }
}

@available(iOS 16.0, *)
private struct RoomPlanCaptureRepresentable: UIViewRepresentable {
    let capture: CaptureController
    @Binding var isRunning: Bool

    func makeCoordinator() -> Coordinator {
        Coordinator(capture: capture)
    }

    func makeUIView(context: Context) -> RoomCaptureView {
        let view = RoomCaptureView(frame: .zero)
        view.isModelEnabled = true
        view.captureSession.delegate = context.coordinator
        context.coordinator.start(view)
        return view
    }

    func updateUIView(_ uiView: RoomCaptureView, context: Context) {
        if isRunning {
            context.coordinator.start(uiView)
        } else {
            context.coordinator.stop(uiView)
        }
    }

    static func dismantleUIView(_ uiView: RoomCaptureView, coordinator: Coordinator) {
        coordinator.stop(uiView)
    }

    final class Coordinator: NSObject, RoomCaptureSessionDelegate {
        private weak var capture: CaptureController?
        private var didStart = false
        private var didStop = false

        init(capture: CaptureController) {
            self.capture = capture
        }

        func start(_ view: RoomCaptureView) {
            guard !didStart, !didStop else { return }
            guard RoomCaptureSession.isSupported else {
                capture?.noteRoomPlanFailure("RoomPlan is not supported on this device.")
                didStop = true
                return
            }
            didStart = true
            var configuration = RoomCaptureSession.Configuration()
            configuration.isCoachingEnabled = true
            capture?.roomPlanStatus = "RoomPlan scanning"
            capture?.roomPlanDetail = "Sweep walls, corners, openings, and large objects slowly."
            view.captureSession.run(configuration: configuration)
        }

        func stop(_ view: RoomCaptureView) {
            guard didStart, !didStop else { return }
            didStop = true
            capture?.roomPlanStatus = "RoomPlan processing"
            capture?.roomPlanDetail = "Building layout evidence and USDZ export."
            view.captureSession.stop()
        }

        func captureSession(_ session: RoomCaptureSession, didUpdate room: CapturedRoom) {
            DispatchQueue.main.async { [weak self] in
                self?.capture?.updateRoomPlanPreview(room: room)
            }
        }

        func captureSession(_ session: RoomCaptureSession, didProvide instruction: RoomCaptureSession.Instruction) {
            DispatchQueue.main.async { [weak self] in
                self?.capture?.noteRoomPlanInstruction(instruction)
            }
        }

        func captureSession(_ session: RoomCaptureSession, didEndWith data: CapturedRoomData, error: Error?) {
            if let error {
                DispatchQueue.main.async { [weak self] in
                    self?.capture?.noteRoomPlanFailure(error.localizedDescription)
                }
                return
            }
            let capture = capture
            Task {
                do {
                    let builder = RoomBuilder(options: [.beautifyObjects])
                    let room = try await builder.capturedRoom(from: data)
                    await MainActor.run {
                        capture?.exportRoomPlan(room: room)
                    }
                } catch {
                    await MainActor.run {
                        capture?.noteRoomPlanFailure(error.localizedDescription)
                    }
                }
            }
        }
    }
}

private struct CoverageMiniMap: View {
    let scores: [Double]
    let currentIndex: Int
    let targetIndex: Int

    var body: some View {
        Canvas { context, _ in
            let barWidth: CGFloat = 7
            let spacing: CGFloat = 3
            for item in items {
                let x = CGFloat(item.id) * (barWidth + spacing)
                let bar = CGRect(x: x, y: 0, width: barWidth, height: 24)
                context.fill(Path(roundedRect: bar, cornerRadius: 4), with: .color(color(for: item.score)))
                if item.id == currentIndex {
                    let dot = CGRect(x: x + 1.5, y: 29, width: 4, height: 4)
                    context.fill(Path(ellipseIn: dot), with: .color(.primary))
                }
                if item.id == targetIndex {
                    var marker = Path()
                    marker.move(to: CGPoint(x: x + 3.5, y: -5))
                    marker.addLine(to: CGPoint(x: x + 7, y: -1))
                    marker.addLine(to: CGPoint(x: x, y: -1))
                    marker.closeSubpath()
                    context.fill(marker, with: .color(.blue))
                }
            }
        }
        .frame(width: 117, height: 33)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Angle coverage")
        .accessibilityValue("\(scores.filter { $0 >= 1 }.count) of \(scores.count) sectors covered. Current sector \(currentIndex + 1). Target sector \(targetIndex + 1).")
    }

    private var items: [CoverageMiniMapItem] {
        scores.enumerated().map { CoverageMiniMapItem(id: $0.offset, score: $0.element) }
    }

    private func color(for score: Double) -> Color {
        if score >= 1 {
            return .green.opacity(0.85)
        }
        if score > 0 {
            return .yellow.opacity(0.85)
        }
        return .secondary.opacity(0.3)
    }
}

private struct CoverageMiniMapItem: Identifiable {
    let id: Int
    let score: Double
}

private struct PointCloudPreviewScene: UIViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView()
        view.allowsCameraControl = true
        view.autoenablesDefaultLighting = true
        view.backgroundColor = UIColor.black
        view.scene = makeScene(from: url)
        context.coordinator.loadedURL = url
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        guard context.coordinator.loadedURL != url else { return }
        view.scene = makeScene(from: url)
        context.coordinator.loadedURL = url
    }

    final class Coordinator {
        var loadedURL: URL?
    }

    private func makeScene(from url: URL) -> SCNScene {
        let scene = SCNScene()
        let points = loadPreviewPoints(from: url)
        if !points.positions.isEmpty {
            scene.rootNode.addChildNode(makePointNode(points: points))
        }
        let camera = SCNCamera()
        camera.zNear = 0.01
        camera.zFar = 100
        let cameraNode = SCNNode()
        cameraNode.camera = camera
        cameraNode.position = cameraPosition(for: points.bounds)
        let target = SCNNode()
        target.position = points.bounds.center
        scene.rootNode.addChildNode(target)
        cameraNode.constraints = [SCNLookAtConstraint(target: target)]
        scene.rootNode.addChildNode(cameraNode)
        scene.rootNode.addChildNode(makeLightNode())
        scene.background.contents = UIColor.black
        return scene
    }

    private func makePointNode(points: PreviewGeometry) -> SCNNode {
        let vertexData = points.positions.withUnsafeBufferPointer { Data(buffer: $0) }
        let colorData = points.colors.withUnsafeBufferPointer { Data(buffer: $0) }
        let indices = Array(UInt32(0)..<UInt32(points.positions.count / 3))
        let indexData = indices.withUnsafeBufferPointer { Data(buffer: $0) }
        let vertexSource = SCNGeometrySource(
            data: vertexData,
            semantic: .vertex,
            vectorCount: points.positions.count / 3,
            usesFloatComponents: true,
            componentsPerVector: 3,
            bytesPerComponent: MemoryLayout<Float>.size,
            dataOffset: 0,
            dataStride: MemoryLayout<Float>.size * 3
        )
        let colorSource = SCNGeometrySource(
            data: colorData,
            semantic: .color,
            vectorCount: points.colors.count / 4,
            usesFloatComponents: true,
            componentsPerVector: 4,
            bytesPerComponent: MemoryLayout<Float>.size,
            dataOffset: 0,
            dataStride: MemoryLayout<Float>.size * 4
        )
        let element = SCNGeometryElement(
            data: indexData,
            primitiveType: .point,
            primitiveCount: points.positions.count / 3,
            bytesPerIndex: MemoryLayout<UInt32>.size
        )
        element.pointSize = 5
        element.minimumPointScreenSpaceRadius = 2
        element.maximumPointScreenSpaceRadius = 8
        let geometry = SCNGeometry(sources: [vertexSource, colorSource], elements: [element])
        let material = SCNMaterial()
        material.lightingModel = .constant
        material.diffuse.contents = UIColor.white
        geometry.materials = [material]
        return SCNNode(geometry: geometry)
    }

    private func makeLightNode() -> SCNNode {
        let light = SCNLight()
        light.type = .omni
        light.intensity = 500
        let node = SCNNode()
        node.light = light
        node.position = SCNVector3(0, 2, 2)
        return node
    }

    private func cameraPosition(for bounds: PreviewBounds) -> SCNVector3 {
        let radius = max(bounds.radius, 0.5)
        return SCNVector3(
            bounds.center.x,
            bounds.center.y + radius * 0.45,
            bounds.center.z + radius * 2.2
        )
    }

    private func loadPreviewPoints(from url: URL) -> PreviewGeometry {
        guard let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = object["points"] as? [[String: Any]] else {
            return PreviewGeometry.empty
        }
        var positions: [Float] = []
        var colors: [Float] = []
        var bounds = PreviewBounds.empty
        for row in rows.prefix(12000) {
            guard let x = number(row["x"]),
                  let y = number(row["y"]),
                  let z = number(row["z"]) else { continue }
            let r = Float(number(row["r"]) ?? 120) / 255.0
            let g = Float(number(row["g"]) ?? 220) / 255.0
            let b = Float(number(row["b"]) ?? 255) / 255.0
            positions.append(contentsOf: [Float(x), Float(y), Float(z)])
            colors.append(contentsOf: [r, g, b, 1.0])
            bounds.include(SCNVector3(Float(x), Float(y), Float(z)))
        }
        return PreviewGeometry(positions: positions, colors: colors, bounds: bounds)
    }

    private func number(_ value: Any?) -> Double? {
        guard let value = value as? NSNumber else { return nil }
        let number = value.doubleValue
        return number.isFinite ? number : nil
    }
}

private struct PreviewGeometry {
    let positions: [Float]
    let colors: [Float]
    let bounds: PreviewBounds

    static let empty = PreviewGeometry(positions: [], colors: [], bounds: .empty)
}

private struct PreviewBounds {
    var min = SCNVector3(Float.greatestFiniteMagnitude, Float.greatestFiniteMagnitude, Float.greatestFiniteMagnitude)
    var max = SCNVector3(-Float.greatestFiniteMagnitude, -Float.greatestFiniteMagnitude, -Float.greatestFiniteMagnitude)
    var hasPoint = false

    static let empty = PreviewBounds()

    var center: SCNVector3 {
        guard hasPoint else { return SCNVector3(0, 0, 0) }
        return SCNVector3((min.x + max.x) * 0.5, (min.y + max.y) * 0.5, (min.z + max.z) * 0.5)
    }

    var radius: Float {
        guard hasPoint else { return 1 }
        let dx = max.x - min.x
        let dy = max.y - min.y
        let dz = max.z - min.z
        let diagonal = sqrt(dx * dx + dy * dy + dz * dz)
        return Swift.max(diagonal * 0.5, 0.1)
    }

    mutating func include(_ point: SCNVector3) {
        hasPoint = true
        min = SCNVector3(Swift.min(min.x, point.x), Swift.min(min.y, point.y), Swift.min(min.z, point.z))
        max = SCNVector3(Swift.max(max.x, point.x), Swift.max(max.y, point.y), Swift.max(max.z, point.z))
    }
}

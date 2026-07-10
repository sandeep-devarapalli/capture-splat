import ARKit
import RealityKit
import SwiftUI

struct ARCaptureView: UIViewRepresentable {
    @EnvironmentObject private var capture: CaptureController

    func makeUIView(context: Context) -> ARView {
        let view = ARView(frame: .zero)
        capture.attach(session: view.session)

        let configuration = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            configuration.frameSemantics.insert(.sceneDepth)
        }
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.personSegmentationWithDepth) {
            configuration.frameSemantics.insert(.personSegmentationWithDepth)
        }
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification) {
            configuration.sceneReconstruction = .meshWithClassification
        } else if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
            configuration.sceneReconstruction = .mesh
        }
        configuration.planeDetection = [.horizontal, .vertical]
        view.session.run(configuration)
        return view
    }

    func updateUIView(_ uiView: ARView, context: Context) {}
}

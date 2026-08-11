import Foundation

@main
struct CaptureKeyframePolicyProbe {
    static func main() throws {
        let first = CaptureKeyframePolicy.videoNoveltyDecision(
            hasAcceptedFrame: false,
            observedParallaxMeters: 0,
            currentSectorIndex: 2,
            lastAcceptedSectorIndex: nil,
            baseParallaxMeters: 0.05
        )
        let cases = [
            "first": first,
            "new_below": decision(observed: 0.049_999, current: 2, last: 1),
            "new_exact": decision(observed: 0.05, current: 2, last: 1),
            "same_below": decision(observed: 0.069_999, current: 2, last: 2),
            "same_exact": decision(observed: 0.07, current: 2, last: 2),
            "negative": decision(observed: -0.01, current: 2, last: 1),
            "nan": decision(observed: .nan, current: 2, last: 1),
            "infinity": decision(observed: .infinity, current: 2, last: 1),
            "invalid_policy": CaptureKeyframePolicy.videoNoveltyDecision(
                hasAcceptedFrame: true,
                observedParallaxMeters: 0.1,
                currentSectorIndex: 2,
                lastAcceptedSectorIndex: 1,
                baseParallaxMeters: 0
            ),
        ]
        let output: [String: Any] = [
            "video_intervals": [0.2, 0.2, 0.5, 1.0].map {
                CaptureKeyframePolicy.candidateInterval(
                    baseInterval: 0.2,
                    thermalGuidanceInterval: $0,
                    isVideo3DGS: true
                )
            },
            "standard_interval": CaptureKeyframePolicy.candidateInterval(
                baseInterval: 0.5,
                thermalGuidanceInterval: 1,
                isVideo3DGS: false
            ),
            "cases": cases.mapValues(encode),
        ]
        let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
    }

    private static func decision(
        observed: Double,
        current: Int,
        last: Int?
    ) -> CaptureNoveltyDecision {
        CaptureKeyframePolicy.videoNoveltyDecision(
            hasAcceptedFrame: true,
            observedParallaxMeters: observed,
            currentSectorIndex: current,
            lastAcceptedSectorIndex: last,
            baseParallaxMeters: 0.05
        )
    }

    private static func encode(_ decision: CaptureNoveltyDecision) -> [String: Any] {
        [
            "disposition": decision.disposition.rawValue,
            "observed": decision.observedParallaxMeters,
            "required": decision.requiredParallaxMeters,
            "remaining_cm": decision.remainingCentimeters,
        ]
    }
}

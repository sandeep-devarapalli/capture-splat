import Foundation

enum CaptureNoveltyDisposition: String {
    case evaluateQuality
    case waitForMovement
    case invalidPose
    case invalidPolicy
}

struct CaptureNoveltyDecision: Equatable {
    let disposition: CaptureNoveltyDisposition
    let observedParallaxMeters: Double
    let requiredParallaxMeters: Double

    var shouldEvaluateQuality: Bool {
        disposition == .evaluateQuality
    }

    var remainingParallaxMeters: Double {
        max(requiredParallaxMeters - observedParallaxMeters, 0)
    }

    var remainingCentimeters: Int {
        max(Int(ceil(remainingParallaxMeters * 100)), 0)
    }
}

enum CaptureKeyframePolicy {
    static func candidateInterval(
        baseInterval: TimeInterval,
        thermalGuidanceInterval: TimeInterval,
        isVideo3DGS: Bool
    ) -> TimeInterval {
        isVideo3DGS ? max(baseInterval, thermalGuidanceInterval) : baseInterval
    }

    static func videoNoveltyDecision(
        hasAcceptedFrame: Bool,
        observedParallaxMeters: Double,
        currentSectorIndex: Int,
        lastAcceptedSectorIndex: Int?,
        baseParallaxMeters: Double,
        repeatedSectorMultiplier: Double = 1.4
    ) -> CaptureNoveltyDecision {
        guard hasAcceptedFrame else {
            return CaptureNoveltyDecision(
                disposition: .evaluateQuality,
                observedParallaxMeters: 0,
                requiredParallaxMeters: 0
            )
        }
        guard observedParallaxMeters.isFinite else {
            return CaptureNoveltyDecision(
                disposition: .invalidPose,
                observedParallaxMeters: 0,
                requiredParallaxMeters: 0
            )
        }
        guard baseParallaxMeters.isFinite,
              baseParallaxMeters > 0,
              repeatedSectorMultiplier.isFinite,
              repeatedSectorMultiplier >= 1 else {
            return CaptureNoveltyDecision(
                disposition: .invalidPolicy,
                observedParallaxMeters: max(observedParallaxMeters, 0),
                requiredParallaxMeters: 0
            )
        }

        let observed = max(observedParallaxMeters, 0)
        let repeatsSector = lastAcceptedSectorIndex == currentSectorIndex
        let required = baseParallaxMeters * (repeatsSector ? repeatedSectorMultiplier : 1)
        return CaptureNoveltyDecision(
            disposition: observed >= required ? .evaluateQuality : .waitForMovement,
            observedParallaxMeters: observed,
            requiredParallaxMeters: required
        )
    }
}

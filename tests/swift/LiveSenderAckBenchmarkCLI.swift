import Darwin
import Foundation

@main
private struct LiveSenderAckBenchmarkCLI {
    static func main() async {
        do {
            let arguments = try parseArguments(Array(CommandLine.arguments.dropFirst()))
            let configuration = try LiveSenderAckBenchmarkConfiguration(
                acknowledgedFrameCount: arguments.count,
                trialIndex: arguments.trialIndex
            )
            switch arguments.phase {
            case .full:
                try emit(try await LiveSenderAckBenchmarkCore.run(
                    configuration: configuration,
                    workspaceURL: arguments.workspace
                ))
            case .reconcile:
                try emit(try await LiveSenderAckBenchmarkCore.prepareAndReconcile(
                    configuration: configuration,
                    workspaceURL: arguments.workspace
                ))
            case .reopen:
                try emit(try await LiveSenderAckBenchmarkCore.reopenAndProbe(
                    configuration: configuration,
                    workspaceURL: arguments.workspace
                ))
            case .seedComplete:
                try emit(try LiveSenderAckBenchmarkCore.prepareCompleteState(
                    configuration: configuration,
                    workspaceURL: arguments.workspace
                ))
            case .unpacedStream:
                try emit(try await LiveSenderAckBenchmarkCore.runUnpacedStream(
                    finalAcknowledgedFrameCount: arguments.count,
                    workspaceURL: arguments.workspace
                ))
            case .pacedStream:
                guard let initialCount = arguments.initialCount,
                      let rate = arguments.rate,
                      let durationSeconds = arguments.durationSeconds else {
                    throw LiveSenderAckBenchmarkError.invalidConfiguration
                }
                let streamConfiguration =
                    try LiveSenderAckBenchmarkPacedConfiguration(
                        initialAcknowledgedFrameCount: initialCount,
                        finalAcknowledgedFrameCount: arguments.count,
                        acknowledgementsPerSecond: rate,
                        nominalDurationSeconds: durationSeconds
                    )
                try emit(try await LiveSenderAckBenchmarkCore.runPacedStream(
                    configuration: streamConfiguration,
                    workspaceURL: arguments.workspace
                ))
            }
        } catch {
            FileHandle.standardError.write(
                Data("ACK benchmark failed: \(error.localizedDescription)\n".utf8)
            )
            Darwin.exit(1)
        }
    }

    private struct Arguments {
        let count: Int
        let trialIndex: Int
        let workspace: URL
        let phase: Phase
        let initialCount: Int?
        let rate: Int?
        let durationSeconds: Int?
    }

    private enum Phase: String {
        case full
        case reconcile
        case reopen
        case seedComplete = "seed-complete"
        case unpacedStream = "unpaced-stream"
        case pacedStream = "paced-stream"
    }

    private static func parseArguments(_ values: [String]) throws -> Arguments {
        var count: Int?
        var trialIndex: Int?
        var workspace: URL?
        var phase = Phase.full
        var initialCount: Int?
        var rate: Int?
        var durationSeconds: Int?
        var index = 0
        while index < values.count {
            guard index + 1 < values.count else {
                throw LiveSenderAckBenchmarkError.invalidConfiguration
            }
            let value = values[index + 1]
            switch values[index] {
            case "--count":
                count = Int(value)
            case "--trial-index":
                trialIndex = Int(value)
            case "--workspace":
                workspace = URL(fileURLWithPath: value, isDirectory: true)
            case "--phase":
                guard let parsed = Phase(rawValue: value) else {
                    throw LiveSenderAckBenchmarkError.invalidConfiguration
                }
                phase = parsed
            case "--initial-count":
                initialCount = Int(value)
            case "--rate":
                rate = Int(value)
            case "--duration-seconds":
                durationSeconds = Int(value)
            default:
                throw LiveSenderAckBenchmarkError.invalidConfiguration
            }
            index += 2
        }
        guard let count, let trialIndex, let workspace else {
            throw LiveSenderAckBenchmarkError.invalidConfiguration
        }
        return Arguments(
            count: count,
            trialIndex: trialIndex,
            workspace: workspace,
            phase: phase,
            initialCount: initialCount,
            rate: rate,
            durationSeconds: durationSeconds
        )
    }

    private static func emit<T: Encodable>(_ value: T) throws {
        let data = try LiveSenderAckBenchmarkCore.canonicalJSONData(value)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0a]))
    }
}

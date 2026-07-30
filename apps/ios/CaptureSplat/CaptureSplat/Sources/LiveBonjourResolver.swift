import Foundation
import Network

@MainActor
protocol LiveBonjourResolving: AnyObject {
    func resolve(
        discovery: LiveDiscoveryIdentity,
        timeout: TimeInterval
    ) async throws -> LiveResolvedEndpoint
    func cancel()
}

enum LiveBonjourResolverError: Error, Equatable, LocalizedError {
    case alreadyResolving
    case browseFailed(String)
    case resolutionFailed
    case timedOut

    var errorDescription: String? {
        switch self {
        case .alreadyResolving:
            return "A World Studio discovery is already active."
        case .browseFailed(let message):
            return "Bonjour discovery failed: \(message)"
        case .resolutionFailed:
            return "The selected World Studio service could not be resolved."
        case .timedOut:
            return "The World Studio pairing invitation could not be found before it expired."
        }
    }
}

@MainActor
final class LiveBonjourResolver: NSObject, LiveBonjourResolving, NetServiceDelegate {
    private var browser: NWBrowser?
    private var service: NetService?
    private var continuation: CheckedContinuation<LiveResolvedEndpoint, Error>?
    private var expectedDiscovery: LiveDiscoveryIdentity?
    private var timeoutTask: Task<Void, Never>?

    func resolve(
        discovery: LiveDiscoveryIdentity,
        timeout: TimeInterval
    ) async throws -> LiveResolvedEndpoint {
        try discovery.validate()
        guard continuation == nil, browser == nil, service == nil else {
            throw LiveBonjourResolverError.alreadyResolving
        }
        guard timeout > 0, timeout.isFinite else {
            throw LiveBonjourResolverError.timedOut
        }

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                self.continuation = continuation
                expectedDiscovery = discovery

                let parameters = NWParameters.tcp
                parameters.includePeerToPeer = false
                let browser = NWBrowser(
                    for: .bonjour(
                        type: discovery.serviceType,
                        domain: discovery.domain
                    ),
                    using: parameters
                )
                self.browser = browser
                browser.stateUpdateHandler = { [weak self] state in
                    Task { @MainActor in
                        self?.handleBrowserState(state)
                    }
                }
                browser.browseResultsChangedHandler = { [weak self] results, _ in
                    Task { @MainActor in
                        self?.handleResults(results)
                    }
                }
                browser.start(queue: .main)

                timeoutTask = Task { @MainActor [weak self] in
                    let nanoseconds = UInt64(
                        min(timeout, 300) * 1_000_000_000
                    )
                    try? await Task.sleep(nanoseconds: nanoseconds)
                    guard !Task.isCancelled else { return }
                    self?.finish(throwing: LiveBonjourResolverError.timedOut)
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.cancel()
            }
        }
    }

    func cancel() {
        finish(throwing: CancellationError())
    }

    static func matches(
        endpoint: NWEndpoint,
        discovery: LiveDiscoveryIdentity
    ) -> Bool {
        guard case .service(let name, let type, let domain, _) = endpoint else {
            return false
        }
        return name == discovery.serviceName
            && normalizedServiceType(type) == discovery.serviceType
            && normalizedDomain(domain) == discovery.domain
    }

    private func handleBrowserState(_ state: NWBrowser.State) {
        switch state {
        case .failed(let error):
            finish(throwing: LiveBonjourResolverError.browseFailed(
                String(describing: error)
            ))
        case .cancelled:
            if continuation != nil, service == nil, browser != nil {
                finish(throwing: CancellationError())
            }
        case .setup, .waiting, .ready:
            break
        @unknown default:
            finish(throwing: LiveBonjourResolverError.browseFailed(
                "Unexpected browser state."
            ))
        }
    }

    private func handleResults(_ results: Set<NWBrowser.Result>) {
        guard service == nil,
              let discovery = expectedDiscovery,
              let match = results.first(where: {
                  Self.matches(endpoint: $0.endpoint, discovery: discovery)
              }),
              case .service(let name, let type, let domain, _) = match.endpoint else {
            return
        }

        browser?.cancel()
        browser = nil
        let service = NetService(
            domain: Self.normalizedDomain(domain),
            type: Self.netServiceType(type),
            name: name
        )
        self.service = service
        service.delegate = self
        service.schedule(in: .main, forMode: .common)
        service.resolve(withTimeout: 15)
    }

    nonisolated func netServiceDidResolveAddress(_ sender: NetService) {
        Task { @MainActor [weak self] in
            guard let self,
                  sender === service,
                  let discovery = expectedDiscovery,
                  let hostName = sender.hostName,
                  sender.port > 0 else {
                self?.finish(throwing: LiveBonjourResolverError.resolutionFailed)
                return
            }
            let host = hostName.hasSuffix(".")
                ? String(hostName.dropLast())
                : hostName
            let endpoint = LiveResolvedEndpoint(
                host: host,
                port: sender.port,
                discovery: discovery
            )
            finish(returning: endpoint)
        }
    }

    nonisolated func netService(
        _ sender: NetService,
        didNotResolve errorDict: [String: NSNumber]
    ) {
        Task { @MainActor [weak self] in
            guard let self, sender === service else { return }
            finish(throwing: LiveBonjourResolverError.resolutionFailed)
        }
    }

    private func finish(returning endpoint: LiveResolvedEndpoint) {
        guard let continuation else { return }
        cleanup()
        continuation.resume(returning: endpoint)
    }

    private func finish(throwing error: Error) {
        guard let continuation else {
            cleanup()
            return
        }
        cleanup()
        continuation.resume(throwing: error)
    }

    private func cleanup() {
        timeoutTask?.cancel()
        timeoutTask = nil
        browser?.cancel()
        browser = nil
        service?.stop()
        service?.remove(from: .main, forMode: .common)
        service?.delegate = nil
        service = nil
        expectedDiscovery = nil
        continuation = nil
    }

    private static func normalizedServiceType(_ value: String) -> String {
        value.hasSuffix(".") ? String(value.dropLast()) : value
    }

    private static func netServiceType(_ value: String) -> String {
        let normalized = normalizedServiceType(value)
        return "\(normalized)."
    }

    private static func normalizedDomain(_ value: String) -> String {
        value.hasSuffix(".") ? value : "\(value)."
    }
}

import Foundation
import MowerWidgetCore
import WatchConnectivity
import WatchKit
import WidgetKit

/// What the last command did, shown under the buttons.
struct CommandOutcome: Equatable {
    let command: WatchCommand
    let failure: WatchFailure?
    let at: Date
}

/// The watch side of the WatchConnectivity link (ADR 0060). It never calls the MowDeck server and
/// holds no credential: commands go to the iPhone app with `sendMessage` and a reply handler,
/// state arrives as the application context (and as complication user info), is kept in the
/// watch's own App Group for the complication, and is shown as it is.
@MainActor
final class WatchModel: NSObject, ObservableObject {
    static let shared = WatchModel()

    @Published private(set) var state: WatchState?
    @Published private(set) var phoneReachable = false
    /// The command on its way (its button shows progress, the others wait).
    @Published private(set) var sending: WatchCommand?
    @Published private(set) var outcome: CommandOutcome?

    private let store = WatchStore.shared

    override init() {
        super.init()
        state = store.state
    }

    func activate() {
        guard WCSession.isSupported() else { return }
        let session = WCSession.default
        if session.delegate == nil { session.delegate = self }
        if session.activationState != .activated { session.activate() }
    }

    /// Ask the iPhone for the current state (the app came to the foreground). The answer comes
    /// as a new application context.
    func refresh() {
        Task { _ = await deliver(.refresh) }
    }

    /// The complication shows the mower last opened here.
    func select(_ mowerId: String) {
        guard store.primaryMowerId != mowerId else { return }
        store.primaryMowerId = mowerId
        WidgetCenter.shared.reloadAllTimelines()
    }

    /// Send a command through the iPhone and show the outcome. The emergency stop is tried
    /// three times within about two seconds when the iPhone does not answer (stopping twice is
    /// harmless); the other commands are sent once, and never queued for later.
    func send(_ command: WatchCommand, to mowerId: String) async {
        sending = command
        defer { sending = nil }
        let attempts = command.isEmergency ? 3 : 1
        var failure: WatchFailure? = .phoneUnreachable
        for attempt in 0..<attempts {
            if attempt > 0 {
                activate()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
            failure = await deliver(.command(command, mowerId: mowerId))
            if failure != .phoneUnreachable && failure != .noAnswer { break }
        }
        outcome = CommandOutcome(command: command, failure: failure, at: Date())
        WKInterfaceDevice.current().play(failure == nil ? .success : .failure)
    }

    func clearOutcome() {
        outcome = nil
    }

    private func deliver(_ request: WatchRequest) async -> WatchFailure? {
        guard WCSession.isSupported() else { return .phoneUnreachable }
        let session = WCSession.default
        guard session.activationState == .activated, session.isReachable else {
            return .phoneUnreachable
        }
        return await withCheckedContinuation { continuation in
            session.sendMessage(
                request.message,
                replyHandler: { @Sendable reply in
                    continuation.resume(returning: WatchReply(message: reply).failure)
                },
                errorHandler: { @Sendable error in
                    continuation.resume(returning: WatchModel.failure(for: error))
                })
        }
    }

    nonisolated static func failure(for error: Error) -> WatchFailure {
        guard let wc = error as? WCError else { return .phoneUnreachable }
        switch wc.code {
        case .messageReplyTimedOut, .messageReplyFailed: return .noAnswer
        default: return .phoneUnreachable
        }
    }

    /// A newer state from the iPhone: keep it for the complication and show it.
    func apply(context: [String: Any]) {
        guard let next = WatchState(context: context) else { return }
        if let current = state, current.updatedAt > next.updatedAt { return }
        state = next
        store.state = next
        WidgetCenter.shared.reloadAllTimelines()
    }

    fileprivate func setReachable(_ reachable: Bool) {
        let regained = reachable && !phoneReachable
        phoneReachable = reachable
        if regained { refresh() }
    }

    /// For the `.watchConnectivity` background task: wait until the system delivered what it
    /// holds for this app (at most about ten seconds).
    func drainPendingContent() async {
        activate()
        for _ in 0..<20 {
            if WCSession.default.activationState == .activated && !WCSession.default.hasContentPending {
                return
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
    }
}

extension WatchModel: WCSessionDelegate {
    nonisolated func session(
        _ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {
        let reachable = activationState == .activated && session.isReachable
        let context = session.receivedApplicationContext
        Task { @MainActor in
            self.apply(context: context)
            self.setReachable(reachable)
        }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        let reachable = session.isReachable
        Task { @MainActor in self.setReachable(reachable) }
    }

    nonisolated func session(
        _ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]
    ) {
        Task { @MainActor in self.apply(context: applicationContext) }
    }

    /// Complication user info (`transferCurrentComplicationUserInfo` on the iPhone): the same
    /// state, delivered even while the app is not running.
    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        Task { @MainActor in self.apply(context: userInfo) }
    }
}

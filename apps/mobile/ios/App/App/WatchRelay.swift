import Foundation
import MowerWidgetCore
import UIKit
import WatchConnectivity

/// The iPhone half of the Apple Watch app (ADR 0060). The watch holds no credential and never
/// calls the server: it sends its commands here (`sendMessage` with a reply handler, which wakes
/// this app in the background) and this relay runs them with the device credential the widgets
/// already use (ADR 0059, Keychain), then answers with the outcome. State goes to the watch as
/// the application context (latest wins) and, when the watch face would change, as complication
/// user info (about 50 a day).
///
/// The web view is not involved: the app may have been launched in the background just for the
/// message, without a scene and without the Capacitor bridge.
final class WatchRelay: NSObject, WCSessionDelegate, @unchecked Sendable {
    static let shared = WatchRelay()

    enum Reason {
        /// The session became active or the watch app was installed.
        case activation
        /// The server's silent push for a state change (`widget-reload`, ADR 0059).
        case serverPush
        /// After a command from the watch.
        case afterCommand
        /// The watch asked (its app came to the foreground).
        case watchAsked
        /// Widgets were turned on or off on this iPhone.
        case configuration
    }

    private let lock = NSLock()
    /// The state last sent, to spend complication transfers only on visible changes.
    private var lastSent: WatchState?

    private var session: WCSession? { WCSession.isSupported() ? WCSession.default : nil }

    /// Called once at launch (also a background launch for a watch message): the delegate must
    /// be set before the message is delivered.
    func activate() {
        guard let session else { return }
        session.delegate = self
        session.activate()
    }

    /// For the settings panel (`MowDeckShell.getWatchStatus`).
    func status() -> [String: Any] {
        guard let session else { return ["supported": false] }
        let active = session.activationState == .activated
        return [
            "supported": true,
            "paired": active && session.isPaired,
            "installed": active && session.isWatchAppInstalled,
            "reachable": active && session.isReachable,
            "complication": active && session.isComplicationEnabled,
        ]
    }

    // MARK: WCSessionDelegate

    func session(
        _ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {
        guard activationState == .activated else { return }
        Task { await self.pushState(reason: .activation) }
    }

    func sessionDidBecomeInactive(_ session: WCSession) {}

    /// The user switched to another watch: activate again for the new one.
    func sessionDidDeactivate(_ session: WCSession) {
        lock.lock()
        lastSent = nil
        lock.unlock()
        session.activate()
    }

    func sessionWatchStateDidChange(_ session: WCSession) {
        guard session.isPaired, session.isWatchAppInstalled else { return }
        Task { await self.pushState(reason: .activation) }
    }

    func session(
        _ session: WCSession, didReceiveMessage message: [String: Any],
        replyHandler: @escaping ([String: Any]) -> Void
    ) {
        guard let request = WatchRequest(message: message) else {
            replyHandler(WatchReply(failure: .badRequest).message)
            return
        }
        // A background launch for the message gets about 30 seconds; ask for them explicitly so
        // the request and the state that follows are not cut off.
        Task { @MainActor in
            let background = UIApplication.shared.beginBackgroundTask(withName: "mowdeck.watch")
            let reply = await self.handle(request)
            replyHandler(reply.message)
            await self.pushState(reason: request == .refresh ? .watchAsked : .afterCommand)
            if background != .invalid { UIApplication.shared.endBackgroundTask(background) }
        }
    }

    /// Messages without a reply handler are not part of the protocol: commands always want an
    /// answer, and nothing is ever queued to run later (a start an hour late is not wanted).
    func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {}

    // MARK: Commands

    func handle(_ request: WatchRequest) async -> WatchReply {
        guard case .command(let command, let mowerId) = request else { return .success }
        guard let credential = try? WidgetBridge.vault.load() else {
            return WatchReply(failure: .notSetUp)
        }
        let client = MowDeckClient(credential: credential)
        do {
            switch command {
            case .emergencyStop: try await client.emergencyStop(mowerId: mowerId)
            case .start: try await client.start(mowerId: mowerId)
            case .pause: try await client.pause(mowerId: mowerId)
            case .dock: try await client.dock(mowerId: mowerId)
            }
            return .success
        } catch {
            return WatchReply(failure: WatchFailure(error))
        }
    }

    // MARK: State

    /// Read every mower's snapshot and send it to the watch. Does nothing without a paired
    /// watch that has the app.
    func pushState(reason: Reason) async {
        guard let session, session.activationState == .activated, session.isPaired,
              session.isWatchAppInstalled
        else { return }
        let state = await readState()
        send(state, reason: reason, session: session)
    }

    private func readState() async -> WatchState {
        let now = Date()
        guard let credential = try? WidgetBridge.vault.load() else {
            return .notConfigured(at: now, signedOut: SharedState.shared.signedOut)
        }
        let client = MowDeckClient(credential: credential)
        var reachable = true
        var mowers = SharedState.shared.mowers
        do {
            mowers = try await client.mowers()
            SharedState.shared.mowers = mowers
        } catch ClientError.credentialRejected {
            return .notConfigured(at: now)
        } catch {
            reachable = false
        }
        var snapshots: [MowerSnapshot] = []
        for mower in mowers.prefix(WatchLink.maxMowers) {
            if reachable, let fresh = try? await client.snapshot(of: mower), fresh.status != nil {
                SharedState.shared.store(fresh)
                snapshots.append(fresh)
            } else if let cached = SharedState.shared.snapshot(for: mower.id) {
                reachable = false
                snapshots.append(cached)
            } else {
                reachable = false
                snapshots.append(MowerSnapshot(mower: mower, status: nil, schedule: nil, fetchedAt: now))
            }
        }
        return WatchState(
            configured: true, serverReachable: reachable, snapshots: snapshots, updatedAt: now)
    }

    private func send(_ state: WatchState, reason: Reason, session: WCSession) {
        let context = state.context
        guard !context.isEmpty else { return }
        lock.lock()
        let worth = ComplicationPolicy.worthTransfer(previous: lastSent, current: state)
        lastSent = state
        lock.unlock()
        // Complication transfers wake the watch app in the background; they are budgeted, so
        // only state changes the server pushed (or the first state) spend one.
        if worth, reason != .watchAsked, session.isComplicationEnabled,
           session.remainingComplicationUserInfoTransfers > 0 {
            session.transferCurrentComplicationUserInfo(context)
        }
        try? session.updateApplicationContext(context)
    }
}

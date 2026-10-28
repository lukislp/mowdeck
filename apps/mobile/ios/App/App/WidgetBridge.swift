import ActivityKit
import Foundation
import MowerWidgetCore
import WidgetKit

/// The native half of the widgets and the Live Activity (ADR 0059). The web app creates the
/// widget credential on the server (it has the session), hands it to `MowDeckShell.configureWidgets`
/// and this file keeps it in the shared Keychain for the widget extension. ActivityKit tokens
/// are reported back to the web app, which registers them with the server.
enum WidgetBridge {
    /// The Keychain item of the widget credential, shared with the extension through the
    /// keychain access group both entitlements list first.
    static let vault = KeychainSecretStore()

    struct Status {
        let configured: Bool
        let credentialId: String?
        let expiresAt: Date?
        let needsRotation: Bool
    }

    static func configure(_ credential: WidgetCredential, mowers: [MowerRef]) throws {
        try vault.save(credential)
        SharedState.shared.mowers = mowers
        WidgetCenter.shared.reloadAllTimelines()
        Task { await WatchRelay.shared.pushState(reason: .configuration) }
    }

    static func setMowers(_ mowers: [MowerRef]) {
        SharedState.shared.mowers = mowers
        WidgetCenter.shared.reloadAllTimelines()
    }

    static func clear() {
        try? vault.delete()
        SharedState.shared.clear()
        WidgetCenter.shared.reloadAllTimelines()
        // The watch relays through this credential (ADR 0060): it now shows the set-up hint.
        Task { await WatchRelay.shared.pushState(reason: .configuration) }
        if #available(iOS 16.2, *) { LiveActivityCoordinator.shared.endAll() }
    }

    static func status(now: Date = Date()) -> Status {
        guard let credential = try? vault.load() else {
            return Status(configured: false, credentialId: nil, expiresAt: nil, needsRotation: false)
        }
        return Status(
            configured: true, credentialId: credential.credentialId, expiresAt: credential.expiresAt,
            needsRotation: credential.needsRotation(now: now))
    }
}

/// Starts, observes and ends the Live Activities of mowing jobs. Activities the server starts by
/// push-to-start arrive through `activityUpdates` as well, so both kinds report their update
/// token the same way: stored as pending (survives a launch without the web app) and emitted
/// as an event the web app answers by registering it with the server.
@available(iOS 16.2, *)
final class LiveActivityCoordinator: @unchecked Sendable {
    static let shared = LiveActivityCoordinator()

    /// Set by the plugin: `(event name, payload)`.
    var emit: (@Sendable (String, [String: Any]) -> Void)?
    private var observing = false
    private var observed = Set<String>()
    private let lock = NSLock()

    var enabled: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    func startObserving() {
        lock.lock()
        defer { lock.unlock() }
        guard !observing else { return }
        observing = true
        Task { [weak self] in
            for await activity in Activity<MowingActivityAttributes>.activityUpdates {
                self?.observe(activity)
            }
        }
        for activity in Activity<MowingActivityAttributes>.activities { observe(activity) }
        if #available(iOS 17.2, *) {
            Task { [weak self] in
                for await data in Activity<MowingActivityAttributes>.pushToStartTokenUpdates {
                    let token = PushToken.hex(data)
                    SharedState.shared.pushToStartToken = token
                    self?.emit?("pushToStartToken", ["token": token])
                }
            }
        }
    }

    private func observe(_ activity: Activity<MowingActivityAttributes>) {
        lock.lock()
        let isNew = observed.insert(activity.id).inserted
        lock.unlock()
        guard isNew else { return }
        let mowerId = activity.attributes.mowerId
        Task { [weak self] in
            for await data in activity.pushTokenUpdates {
                guard let installation = try? InstallationID.current() else { continue }
                let registration = LiveActivityRegistration(
                    installationId: installation, activityId: activity.id,
                    pushToken: PushToken.hex(data))
                var pending = SharedState.shared.pendingRegistrations
                pending[mowerId] = registration
                SharedState.shared.pendingRegistrations = pending
                self?.emit?("liveActivityToken", self?.payload(mowerId, registration) ?? [:])
            }
        }
        Task { [weak self] in
            for await state in activity.activityStateUpdates where state == .dismissed || state == .ended {
                var pending = SharedState.shared.pendingRegistrations
                pending.removeValue(forKey: mowerId)
                SharedState.shared.pendingRegistrations = pending
                self?.emit?("liveActivityEnded", ["mowerId": mowerId, "activityId": activity.id])
                self?.lock.lock()
                self?.observed.remove(activity.id)
                self?.lock.unlock()
            }
        }
    }

    func payload(_ mowerId: String, _ registration: LiveActivityRegistration) -> [String: Any] {
        [
            "mowerId": mowerId, "installationId": registration.installationId,
            "activityId": registration.activityId, "pushToken": registration.pushToken,
        ]
    }

    /// Start an activity from the app (a job the user started in the app).
    func start(mowerId: String, mowerName: String, state: MowingContentState) throws -> String {
        if let running = Activity<MowingActivityAttributes>.activities.first(where: {
            $0.attributes.mowerId == mowerId
        }) {
            return running.id
        }
        let attributes = MowingActivityAttributes(
            mowerId: mowerId, mowerName: mowerName, startedAt: Int(Date().timeIntervalSince1970))
        let content = ActivityContent(
            state: state, staleDate: Date().addingTimeInterval(15 * 60))
        let activity = try Activity.request(
            attributes: attributes, content: content, pushType: .token)
        observe(activity)
        return activity.id
    }

    func end(mowerId: String) {
        for activity in Activity<MowingActivityAttributes>.activities
        where activity.attributes.mowerId == mowerId {
            Task { await activity.end(nil, dismissalPolicy: .immediate) }
        }
    }

    func endAll() {
        for activity in Activity<MowingActivityAttributes>.activities {
            Task { await activity.end(nil, dismissalPolicy: .immediate) }
        }
    }
}

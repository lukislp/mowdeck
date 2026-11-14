// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import MowerWidgetCore
import UIKit
import WidgetKit

/// The app's area-clear confirmation for the widget's Start button (ADR 0084).
///
/// An interactive widget runs its App Intent inside the widget extension, where no confirmation
/// can be shown, so the widget's Start is a link (`AreaClearLink`,
/// `mowdeck://mowers/<id>?confirm=start`). It opens the app, which asks here with a system alert
/// (VoiceOver and Dynamic Type work as in every alert) and sends the start natively with the
/// widget credential (ADR 0059) only after "Confirm". "Cancel" sends nothing. The link itself
/// never starts anything, whoever opens it.
///
/// The wording and the decision which commands are asked come from MowerWidgetCore
/// (`AreaClearWording`, `MotionCommandPolicy`), shared with Siri, Shortcuts and the watch.
@MainActor
final class AreaClearPrompt {
    static let shared = AreaClearPrompt()

    /// One question at a time: a second link while the alert is up is ignored.
    private weak var presented: UIAlertController?

    /// Shows the confirmation for a confirmation link; false for every other link (it goes on
    /// to Capacitor and the web app unchanged).
    @discardableResult
    func handle(_ url: URL, in window: UIWindow?) -> Bool {
        guard let request = AreaClearLink.parse(url) else { return false }
        guard presented == nil, let host = Self.topViewController(of: window) else { return true }
        let name = SharedState.shared.mowers.first { $0.id == request.mowerId }?.name
        let alert = UIAlertController(
            title: AreaClearWording.title,
            message: AreaClearWording.message(mowerName: name),
            preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: AreaClearWording.cancel, style: .cancel))
        alert.addAction(
            UIAlertAction(title: AreaClearWording.confirm, style: .default) { _ in
                Task { @MainActor in
                    await self.send(request.command, mowerId: request.mowerId, window: window)
                }
            })
        presented = alert
        host.present(alert, animated: true)
        return true
    }

    private func send(_ command: MotionCommand, mowerId: String, window: UIWindow?) async {
        let feedback = UINotificationFeedbackGenerator()
        guard let credential = try? WidgetBridge.vault.load() else {
            showFailure(AreaClearWording.failedNotSetUp, window: window)
            return
        }
        let client = MowDeckClient(credential: credential)
        do {
            switch command {
            case .start: try await client.start(mowerId: mowerId)
            case .resume: try await client.resume(mowerId: mowerId)
            // Never linked (AreaClearLink only carries commands that are asked).
            case .pause, .returnToDock, .emergencyStop: return
            }
            feedback.notificationOccurred(.success)
            UIAccessibility.post(notification: .announcement, argument: AreaClearWording.sent)
            WidgetCenter.shared.reloadAllTimelines()
        } catch ClientError.forbidden {
            showFailure(AreaClearWording.failedNotAllowed, window: window)
        } catch ClientError.credentialRejected, ClientError.notConfigured {
            showFailure(AreaClearWording.failedNotSetUp, window: window)
        } catch {
            showFailure(AreaClearWording.failed, window: window)
        }
    }

    private func showFailure(_ message: String, window: UIWindow?) {
        UINotificationFeedbackGenerator().notificationOccurred(.error)
        guard let host = Self.topViewController(of: window) else { return }
        let alert = UIAlertController(
            title: AreaClearWording.failedTitle, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        host.present(alert, animated: true)
    }

    private static func topViewController(of window: UIWindow?) -> UIViewController? {
        var top = window?.rootViewController
        while let next = top?.presentedViewController { top = next }
        return top
    }
}

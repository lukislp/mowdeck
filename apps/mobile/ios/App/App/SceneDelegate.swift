// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import UIKit
import Capacitor
import MowerWidgetCore

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }

        window = UIWindow(windowScene: windowScene)
        window?.rootViewController = MainViewController()
        window?.makeKeyAndVisible()

        SceneDelegateProxy.shared.scene(scene, willConnectTo: session, options: connectionOptions)
        // A cold start from the widget's Start link: ask once the window is on screen. The web
        // app still gets the launch URL and opens the mower page behind the question.
        if let url = connectionOptions.urlContexts.first?.url, AreaClearLink.parse(url) != nil {
            Task { @MainActor [weak self] in
                // Let the bridge view controller appear first: an alert presented before it is
                // in the window hierarchy is dropped.
                try? await Task.sleep(nanoseconds: 600_000_000)
                AreaClearPrompt.shared.handle(url, in: self?.window)
            }
        }
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        // The widget's Start link (ADR 0084) shows the area-clear confirmation; the link also
        // goes on to the web app, which opens the mower page and ignores the query.
        for context in URLContexts {
            AreaClearPrompt.shared.handle(context.url, in: window)
        }
        SceneDelegateProxy.shared.scene(scene, openURLContexts: URLContexts)
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        SceneDelegateProxy.shared.scene(scene, continue: userActivity)
    }
}

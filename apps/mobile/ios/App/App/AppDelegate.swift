import Capacitor
import UIKit
import WidgetKit

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // The Apple Watch relay (ADR 0060) before anything else: a command from the watch can be
        // the reason for this (background) launch and is delivered once the session is active.
        WatchRelay.shared.activate()
        return true
    }

    func application(_ application: UIApplication,
                     configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default Configuration",
                                          sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }

    // MARK: - APNs (direct, no Firebase): hand the device token to @capacitor/push-notifications,
    // whose "registration" event the web app posts to POST /api/v1/notifications/me/push/apns.

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        NotificationCenter.default.post(name: .capacitorDidRegisterForRemoteNotifications, object: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NotificationCenter.default.post(name: .capacitorDidFailToRegisterForRemoteNotifications, object: error)
    }

    // MARK: - Silent pushes: reload the widgets (ADR 0059)

    /// The Notifications service sends `content-available` pushes with `kind: widget-reload` when
    /// a mower's state changes. iOS decides how often it wakes the app for them (a few per hour);
    /// the widgets also refresh on their own schedule. The same push sends the new state to the
    /// Apple Watch and its complication (ADR 0060). Other background pushes do nothing here.
    func application(_ application: UIApplication,
                     didReceiveRemoteNotification userInfo: [AnyHashable: Any],
                     fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void) {
        if userInfo["kind"] as? String == "widget-reload" {
            WidgetCenter.shared.reloadAllTimelines()
            Task { @MainActor in
                await WatchRelay.shared.pushState(reason: .serverPush)
                completionHandler(.newData)
            }
        } else {
            completionHandler(.noData)
        }
    }

    // MARK: - mowdeck:// deep links (@capacitor/app "appUrlOpen")

    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        return ApplicationDelegateProxy.shared.application(app, open: url, options: options)
    }
}

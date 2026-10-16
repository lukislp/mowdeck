import Capacitor
import UIKit

/// The shell's bridge view controller (ADR 0036).
///
/// Loads the web app from the server the user chose on the bundled first-launch screen: the
/// stored origin becomes Capacitor's `server.url`, so the web app runs on its own origin (the
/// HttpOnly refresh cookie keeps working) and the native bridge is injected into it. Without a
/// stored origin the bundled `public/index.html` (the first-launch screen) is shown.
class MainViewController: CAPBridgeViewController {
    override func instanceDescriptor() -> InstanceDescriptor {
        let descriptor = super.instanceDescriptor()
        if let server = ShellSettings.serverURL, let host = URL(string: server)?.host {
            descriptor.serverURL = server
            descriptor.allowedNavigationHostnames = descriptor.allowedNavigationHostnames + [host]
        }
        return descriptor
    }

    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(MowDeckShellPlugin())
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        // The web view's surroundings (behind the status bar, overscroll) follow the system
        // appearance like the web app's own background (--md-color-bg).
        let background = UIColor { traits in
            traits.userInterfaceStyle == .dark
                ? UIColor(red: 0x0E / 255, green: 0x10 / 255, blue: 0x11 / 255, alpha: 1)
                : UIColor(red: 0xF4 / 255, green: 0xF5 / 255, blue: 0xF2 / 255, alpha: 1)
        }
        view.backgroundColor = background
        webView?.backgroundColor = background
        webView?.scrollView.backgroundColor = background
        // The page itself never rubber-bands (also `overscroll-behavior-y: none` in the web
        // app): the bounce dragged the pinned top bar and the fixed bars along. Scroll boxes
        // inside the page are separate scroll views and keep their bounce.
        webView?.scrollView.bounces = false
    }
}

/// The server origin, stored natively (UserDefaults) so it is known before the web view loads.
enum ShellSettings {
    private static let key = "mowdeck.serverUrl"

    static var serverURL: String? {
        get {
            guard let value = UserDefaults.standard.string(forKey: key), !value.isEmpty else { return nil }
            return value
        }
        set {
            if let value = newValue {
                UserDefaults.standard.set(value, forKey: key)
            } else {
                UserDefaults.standard.removeObject(forKey: key)
            }
        }
    }
}

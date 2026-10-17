import Capacitor
import UIKit
import WebKit

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

    /// The web view looks like Mobile Safari to the web app's libraries (see ``ShellUserAgent``).
    override func webViewConfiguration(for instanceConfiguration: InstanceConfiguration) -> WKWebViewConfiguration {
        let configuration = super.webViewConfiguration(for: instanceConfiguration)
        configuration.applicationNameForUserAgent = ShellUserAgent.applicationName(
            systemVersion: UIDevice.current.systemVersion,
            appToken: instanceConfiguration.appendedUserAgentString
        )
        // Capacitor's defaults, stated here because the live camera depends on them: the muted
        // camera <video> plays inline (not fullscreen) and starts without a tap.
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        return configuration
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

/// The application name WebKit puts at the end of the web view's user agent.
///
/// A plain WKWebView reports `... (KHTML, like Gecko) Mobile/15E148`, without Safari's
/// `Version/x.y` and `Safari/604.1` tokens. The Agora Web SDK of the live camera (ADR 0032)
/// classifies the browser from the user agent (its bundled ua-parser-js) and then sees an unknown
/// "WebKit" browser: a third capability profile that neither Safari nor Chrome on iOS get
/// (no data channel, no encoded transform, no dual-stream encoding), although the engine and its
/// WebRTC stack are exactly Safari's. With Safari's tokens the shell gets Safari's profile, the
/// one the web app is tested with. `MowDeckApp` (Capacitor's `appendUserAgent`) stays at the end,
/// so the server can still tell the app from a browser.
/// `apps/mobile/src/user-agent.test.ts` checks the template below against ua-parser-js.
enum ShellUserAgent {
    /// Safari's version is the system's major.minor (iOS 18.6.2 runs Safari 18.6).
    static func safariVersion(_ systemVersion: String) -> String {
        let parts = systemVersion.split(separator: ".").map(String.init)
        let major = parts.first.flatMap { Int($0) } ?? 18
        let minor = parts.count > 1 ? (Int(parts[1]) ?? 0) : 0
        return "\(major).\(minor)"
    }

    static func applicationName(systemVersion: String, appToken: String?) -> String {
        let safari = "Version/\(safariVersion(systemVersion)) Mobile/15E148 Safari/604.1"
        guard let token = appToken, !token.isEmpty else { return safari }
        return "\(safari) \(token)"
    }
}

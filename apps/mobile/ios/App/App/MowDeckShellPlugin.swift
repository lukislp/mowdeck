import Capacitor
import UIKit

/// `MowDeckShell`: stores the server origin and restarts the bridge with it (see
/// apps/mobile/src/shell.ts for the JavaScript side).
@objc(MowDeckShellPlugin)
public class MowDeckShellPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "MowDeckShellPlugin"
    public let jsName = "MowDeckShell"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "getServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "resetServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "reload", returnType: CAPPluginReturnPromise)
    ]

    @objc func getServerUrl(_ call: CAPPluginCall) {
        call.resolve(["url": ShellSettings.serverURL as Any])
    }

    @objc func setServerUrl(_ call: CAPPluginCall) {
        guard let raw = call.getString("url"), let origin = MowDeckShellPlugin.origin(of: raw) else {
            call.reject("expected an http(s) origin")
            return
        }
        ShellSettings.serverURL = origin
        call.resolve()
        restart()
    }

    @objc func resetServerUrl(_ call: CAPPluginCall) {
        ShellSettings.serverURL = nil
        call.resolve()
        restart()
    }

    @objc func reload(_ call: CAPPluginCall) {
        call.resolve()
        restart()
    }

    /// `scheme://host[:port]` of an http(s) URL without path, query or credentials.
    static func origin(of raw: String) -> String? {
        guard let parts = URLComponents(string: raw),
              let scheme = parts.scheme?.lowercased(), scheme == "https" || scheme == "http",
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil,
              parts.path.isEmpty || parts.path == "/", parts.query == nil, parts.fragment == nil
        else { return nil }
        let port = parts.port.map { ":\($0)" } ?? ""
        return "\(scheme)://\(host.lowercased())\(port)"
    }

    /// A fresh bridge view controller reads the stored origin again (the configuration of a
    /// running bridge is fixed).
    private func restart() {
        DispatchQueue.main.async {
            guard let window = self.bridge?.viewController?.view.window else { return }
            window.rootViewController = MainViewController()
        }
    }
}

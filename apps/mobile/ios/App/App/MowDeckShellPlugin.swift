import Capacitor
import Security
import UIKit

/// `MowDeckShell`: stores the server origin and restarts the bridge with it, and hands out the
/// installation id for the APNs registration (see apps/mobile/src/shell.ts for the JavaScript
/// side).
@objc(MowDeckShellPlugin)
public class MowDeckShellPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "MowDeckShellPlugin"
    public let jsName = "MowDeckShell"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "getServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "resetServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "reload", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getInstallationId", returnType: CAPPluginReturnPromise)
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

    @objc func getInstallationId(_ call: CAPPluginCall) {
        do {
            let id = try InstallationID.current()
            call.resolve(["id": id])
        } catch {
            call.reject("installation id not available", nil, error)
        }
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

/// The id of this app installation on this device, sent with the APNs token so the server keeps
/// one registration per installation (a new token replaces the old one).
///
/// A random UUID, created once and kept in the Keychain as a generic password with
/// `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`: it survives app updates and, because iOS
/// keeps an app's Keychain items when the app is deleted, a reinstall on the same device; it is
/// not synced through iCloud Keychain and not restored onto another device from a backup (a new
/// device gets a new id). Readable once the device was unlocked after a restart, which is always
/// the case while the app is in use.
enum InstallationID {
    private static let service = "app.mowdeck.mobile.installation"
    private static let account = "installation-id"

    struct KeychainError: Error {
        let status: OSStatus
    }

    private static var item: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
    }

    /// The stored id, created on the first call.
    static func current() throws -> String {
        if let stored = try read() {
            return stored
        }
        let id = UUID().uuidString.lowercased()
        var attributes = item
        attributes[kSecValueData as String] = Data(id.utf8)
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(attributes as CFDictionary, nil)
        if status == errSecDuplicateItem, let stored = try read() {
            return stored  // created by a concurrent call
        }
        guard status == errSecSuccess else {
            throw KeychainError(status: status)
        }
        return id
    }

    /// The stored id; nil when there is none (an unreadable value is deleted and replaced).
    private static func read() throws -> String? {
        var query = item
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw KeychainError(status: status)
        }
        if let data = result as? Data, let value = String(data: data, encoding: .utf8),
           UUID(uuidString: value) != nil {
            return value.lowercased()
        }
        SecItemDelete(item as CFDictionary)
        return nil
    }
}

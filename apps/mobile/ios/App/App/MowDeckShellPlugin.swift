import AuthenticationServices
import Capacitor
import Security
import UIKit

/// `MowDeckShell`: stores the server origin and restarts the bridge with it, hands out the
/// installation id for the APNs registration, and runs a sign-in of the server in the system
/// browser (`openAuthSession`, passkeys; ADR 0050). The JavaScript side is
/// apps/web/src/lib/native/ (shell.svelte.ts, handoff.ts).
@objc(MowDeckShellPlugin)
public class MowDeckShellPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "MowDeckShellPlugin"
    public let jsName = "MowDeckShell"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "getServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "resetServerUrl", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "reload", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getInstallationId", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "openAuthSession", returnType: CAPPluginReturnPromise)
    ]

    /// The running sign-in session (kept alive until it completes).
    private var authSession: ASWebAuthenticationSession?

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

    /// Open `url` (a page of the configured server) in an `ASWebAuthenticationSession` and
    /// resolve with the `mowdeck://` callback URL the page navigates to. The system browser has
    /// the user's passkeys for any origin, which the web view has not without Associated
    /// Domains. Ephemeral: no cookies shared with Safari, nothing left behind. Rejects with the
    /// code `cancelled` when the user closes the sheet.
    @objc func openAuthSession(_ call: CAPPluginCall) {
        guard let raw = call.getString("url"),
              let url = URL(string: raw),
              let server = ShellSettings.serverURL,
              MowDeckShellPlugin.sameOrigin(raw, server)
        else {
            call.reject("expected a page of the MowDeck server")
            return
        }
        let scheme = call.getString("callbackScheme") ?? "mowdeck"
        guard scheme == "mowdeck" else {
            call.reject("unsupported callback scheme")
            return
        }
        DispatchQueue.main.async {
            let session = ASWebAuthenticationSession(url: url, callbackURLScheme: scheme) {
                [weak self] callback, error in
                self?.authSession = nil
                if let callback {
                    call.resolve(["url": callback.absoluteString])
                } else if let error = error as? ASWebAuthenticationSessionError,
                          error.code == .canceledLogin {
                    call.reject("the sign-in was cancelled", "cancelled")
                } else {
                    call.reject("the sign-in failed", "failed", error)
                }
            }
            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = true
            self.authSession = session
            if !session.start() {
                self.authSession = nil
                call.reject("the sign-in could not start", "failed")
            }
        }
    }

    /// True when `raw` is an http(s) URL on the origin of `server`.
    static func sameOrigin(_ raw: String, _ server: String) -> Bool {
        guard let parts = URLComponents(string: raw),
              let scheme = parts.scheme?.lowercased(), scheme == "https" || scheme == "http",
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil
        else { return false }
        let port = parts.port.map { ":\($0)" } ?? ""
        return "\(scheme)://\(host.lowercased())\(port)" == server
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

extension MowDeckShellPlugin: ASWebAuthenticationPresentationContextProviding {
    public func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        bridge?.viewController?.view.window ?? ASPresentationAnchor()
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

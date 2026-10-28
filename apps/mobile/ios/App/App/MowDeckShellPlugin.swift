import AuthenticationServices
import Capacitor
import MowerWidgetCore
import Security
import WidgetKit
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
        CAPPluginMethod(name: "openAuthSession", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "configureWidgets", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setWidgetMowers", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clearWidgets", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getWidgetStatus", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "reloadWidgets", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "startLiveActivity", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "endLiveActivity", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getPendingActivityRegistrations", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clearPendingActivityRegistration", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getWatchStatus", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "syncWatch", returnType: CAPPluginReturnPromise)
    ]

    override public func load() {
        if #available(iOS 16.2, *) {
            LiveActivityCoordinator.shared.emit = { [weak self] name, data in
                self?.notifyListeners(name, data: data)
            }
            LiveActivityCoordinator.shared.startObserving()
        }
    }

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

    // MARK: Widgets and the Live Activity (ADR 0059)

    private static func mowers(from call: CAPPluginCall) -> [MowerRef] {
        (call.getArray("mowers") as? [[String: Any]] ?? []).compactMap { item in
            guard let id = item["id"] as? String, let name = item["name"] as? String else { return nil }
            return MowerRef(id: id, name: name)
        }
    }

    /// Store the widget credential the web app created on the server and the mower list.
    @objc func configureWidgets(_ call: CAPPluginCall) {
        guard let raw = call.getString("serverUrl"),
              let origin = MowDeckShellPlugin.origin(of: raw),
              let server = URL(string: origin),
              let secret = call.getString("secret"), secret.hasPrefix("mdk_"),
              let credentialId = call.getString("credentialId"),
              let expires = call.getString("expiresAt"),
              let expiresAt = ISO8601DateFormatter().date(from: expires)
        else {
            call.reject("expected serverUrl, secret, credentialId and expiresAt")
            return
        }
        // The credential is only ever sent to the server the shell is configured for.
        guard let configured = ShellSettings.serverURL, MowDeckShellPlugin.sameOrigin(origin, configured) else {
            call.reject("serverUrl is not the configured server")
            return
        }
        let credential = WidgetCredential(
            serverURL: server, secret: secret, credentialId: credentialId, expiresAt: expiresAt,
            deviceName: call.getString("deviceName") ?? "iPhone")
        do {
            try WidgetBridge.configure(credential, mowers: MowDeckShellPlugin.mowers(from: call))
            call.resolve()
        } catch {
            call.reject("the credential could not be stored", nil, error)
        }
    }

    @objc func setWidgetMowers(_ call: CAPPluginCall) {
        WidgetBridge.setMowers(MowDeckShellPlugin.mowers(from: call))
        call.resolve()
    }

    @objc func clearWidgets(_ call: CAPPluginCall) {
        WidgetBridge.clear()
        call.resolve()
    }

    @objc func reloadWidgets(_ call: CAPPluginCall) {
        WidgetCenter.shared.reloadAllTimelines()
        call.resolve()
    }

    @objc func getWidgetStatus(_ call: CAPPluginCall) {
        let status = WidgetBridge.status()
        var activities = false
        var pushToStart: String?
        if #available(iOS 16.2, *) { activities = LiveActivityCoordinator.shared.enabled }
        pushToStart = SharedState.shared.pushToStartToken
        call.resolve([
            "supported": true,
            "configured": status.configured,
            "credentialId": status.credentialId as Any,
            "expiresAt": status.expiresAt.map { ISO8601DateFormatter().string(from: $0) } as Any,
            "needsRotation": status.needsRotation,
            "activitiesEnabled": activities,
            "pushToStartToken": pushToStart as Any
        ])
    }

    @objc func startLiveActivity(_ call: CAPPluginCall) {
        guard #available(iOS 16.2, *) else {
            call.reject("Live Activities need iOS 16.2")
            return
        }
        guard let mowerId = call.getString("mowerId"), let name = call.getString("mowerName") else {
            call.reject("expected mowerId and mowerName")
            return
        }
        let battery = call.getInt("batteryPercent") ?? 0
        let state = MowingContentState(
            state: call.getString("state") ?? "mowing", batteryPercent: battery,
            progressPercent: call.getInt("progressPercent"), areaName: call.getString("areaName"),
            remainingSeconds: call.getInt("remainingSeconds"),
            endsAt: call.getInt("remainingSeconds").map { Int(Date().timeIntervalSince1970) + $0 },
            updatedAt: Int(Date().timeIntervalSince1970))
        do {
            let id = try LiveActivityCoordinator.shared.start(mowerId: mowerId, mowerName: name, state: state)
            call.resolve(["activityId": id])
        } catch {
            call.reject("the Live Activity could not be started", nil, error)
        }
    }

    @objc func endLiveActivity(_ call: CAPPluginCall) {
        if #available(iOS 16.2, *), let mowerId = call.getString("mowerId") {
            LiveActivityCoordinator.shared.end(mowerId: mowerId)
        }
        call.resolve()
    }

    @objc func getPendingActivityRegistrations(_ call: CAPPluginCall) {
        let items = SharedState.shared.pendingRegistrations.map { mowerId, registration in
            [
                "mowerId": mowerId, "installationId": registration.installationId,
                "activityId": registration.activityId, "pushToken": registration.pushToken
            ]
        }
        call.resolve(["items": items])
    }

    @objc func clearPendingActivityRegistration(_ call: CAPPluginCall) {
        if let mowerId = call.getString("mowerId") {
            var pending = SharedState.shared.pendingRegistrations
            pending.removeValue(forKey: mowerId)
            SharedState.shared.pendingRegistrations = pending
        }
        call.resolve()
    }

    // MARK: Apple Watch (ADR 0060)

    /// Whether a watch is paired, has the app, is reachable and shows the complication.
    @objc func getWatchStatus(_ call: CAPPluginCall) {
        call.resolve(WatchRelay.shared.status())
    }

    /// Send the current state to the watch now (after the mower list changed).
    @objc func syncWatch(_ call: CAPPluginCall) {
        Task { await WatchRelay.shared.pushState(reason: .configuration) }
        call.resolve()
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

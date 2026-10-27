import Foundation

/// The credential the widgets and the Live Activity use instead of the app's session (ADR 0059):
/// an API token the server created for this device (`POST /me/widget-credentials`), limited to
/// seeing and commanding mowers. It lives in the Keychain (shared by the app and the widget
/// extension through a keychain access group, this device only) and is exchanged for 10 minute
/// access tokens. Only the metadata is kept next to it in the App Group.
public struct WidgetCredential: Codable, Equatable, Sendable {
    public var serverURL: URL
    /// The API token secret (`mdk_...`).
    public var secret: String
    /// The server's id of the token, to revoke or replace it.
    public var credentialId: String
    public var expiresAt: Date
    public var deviceName: String

    public init(
        serverURL: URL, secret: String, credentialId: String, expiresAt: Date, deviceName: String
    ) {
        self.serverURL = serverURL
        self.secret = secret
        self.credentialId = credentialId
        self.expiresAt = expiresAt
        self.deviceName = deviceName
    }

    /// The server replaces credentials 90 days after their creation; the app asks for a new one
    /// when fewer than this many days are left.
    public static let renewBeforeExpiry: TimeInterval = 30 * 24 * 3600

    public func isExpired(now: Date) -> Bool { expiresAt <= now }

    public func needsRotation(now: Date) -> Bool {
        expiresAt.timeIntervalSince(now) < Self.renewBeforeExpiry
    }
}

/// Where the credential is kept. The system implementation is the Keychain; tests use memory.
public protocol SecretStore: Sendable {
    func load() throws -> WidgetCredential?
    func save(_ credential: WidgetCredential) throws
    func delete() throws
}

public final class InMemorySecretStore: SecretStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: WidgetCredential?

    public init(_ value: WidgetCredential? = nil) { self.value = value }

    public func load() throws -> WidgetCredential? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    public func save(_ credential: WidgetCredential) throws {
        lock.lock()
        defer { lock.unlock() }
        value = credential
    }

    public func delete() throws {
        lock.lock()
        defer { lock.unlock() }
        value = nil
    }
}

#if canImport(Security)
import Security

public struct KeychainError: Error, Equatable {
    public let status: OSStatus
}

/// The credential as a generic password item, `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`:
/// readable by the widget while the phone is locked (after the first unlock since boot), never
/// synced to iCloud, never restored to another device from a backup.
public struct KeychainSecretStore: SecretStore {
    private let service: String
    private let account: String
    private let accessGroup: String?

    /// `accessGroup` is the full group (`<TeamID>.app.mowdeck.mobile.shared`) or nil to use the
    /// first keychain access group of the process, which is the shared one (the entitlements of
    /// the app and the widget extension list it first).
    public init(
        service: String = "app.mowdeck.mobile.widgets", account: String = "credential",
        accessGroup: String? = nil
    ) {
        self.service = service
        self.account = account
        self.accessGroup = accessGroup
    }

    private var query: [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup { query[kSecAttrAccessGroup as String] = accessGroup }
        return query
    }

    public func load() throws -> WidgetCredential? {
        var request = query
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw KeychainError(status: status)
        }
        return try JSONDecoder.mowdeck.decode(WidgetCredential.self, from: data)
    }

    public func save(_ credential: WidgetCredential) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(credential)
        let status = SecItemUpdate(
            query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else { throw KeychainError(status: status) }
        var add = query
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let added = SecItemAdd(add as CFDictionary, nil)
        guard added == errSecSuccess else { throw KeychainError(status: added) }
    }

    public func delete() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError(status: status)
        }
    }
}
#endif

import Foundation

/// Small, non-secret state the app and the widget extension share through the App Group
/// (`group.app.mowdeck.mobile`): the mower list for the widget's configuration, the last
/// snapshot per mower (shown when the network is out) and the tokens the app has not yet handed
/// to the server. The secret itself is never here, only in the Keychain.
public final class SharedState: @unchecked Sendable {
    public static let appGroup = "group.app.mowdeck.mobile"
    public static let shared = SharedState(
        defaults: UserDefaults(suiteName: SharedState.appGroup) ?? .standard)

    private let defaults: UserDefaults
    private let lock = NSLock()

    public init(defaults: UserDefaults) { self.defaults = defaults }

    private enum Key {
        static let mowers = "widgets.mowers"
        static let snapshotPrefix = "widgets.snapshot."
        static let pending = "widgets.pending-registrations"
        static let pushToStart = "widgets.push-to-start-token"
        static let signedOut = "widgets.signed-out"
    }

    // MARK: Mowers

    public var mowers: [MowerRef] {
        get { read([MowerRef].self, forKey: Key.mowers) ?? [] }
        set { write(newValue, forKey: Key.mowers) }
    }

    // MARK: Last snapshot per mower

    public func snapshot(for mowerId: String) -> MowerSnapshot? {
        read(MowerSnapshot.self, forKey: Key.snapshotPrefix + mowerId)
    }

    public func store(_ snapshot: MowerSnapshot) {
        write(snapshot, forKey: Key.snapshotPrefix + snapshot.mower.id)
    }

    // MARK: Tokens waiting for the web app to hand them to the server

    /// The app's native layer learns Live Activity tokens when it may not have a session; the web
    /// app collects them here the next time it runs.
    public var pendingRegistrations: [String: LiveActivityRegistration] {
        get { read([String: LiveActivityRegistration].self, forKey: Key.pending) ?? [:] }
        set { write(newValue, forKey: Key.pending) }
    }

    public var pushToStartToken: String? {
        get { defaults.string(forKey: Key.pushToStart) }
        set { defaults.set(newValue, forKey: Key.pushToStart) }
    }

    // MARK: Signed out

    /// The user signed out in the app: the credential and the stored states are gone, and the
    /// widgets and the watch say so instead of asking to turn widgets on (the app sets them up
    /// again with the next sign-in).
    public var signedOut: Bool {
        get { defaults.bool(forKey: Key.signedOut) }
        set {
            if newValue { defaults.set(true, forKey: Key.signedOut) }
            else { defaults.removeObject(forKey: Key.signedOut) }
        }
    }

    public func clear() {
        lock.lock()
        defer { lock.unlock() }
        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix("widgets.") {
            defaults.removeObject(forKey: key)
        }
    }

    // MARK: Coding

    private func read<T: Decodable>(_ type: T.Type, forKey key: String) -> T? {
        lock.lock()
        defer { lock.unlock() }
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder.mowdeck.decode(type, from: data)
    }

    private func write<T: Encodable>(_ value: T, forKey key: String) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(value) else { return }
        lock.lock()
        defer { lock.unlock() }
        defaults.set(data, forKey: key)
    }
}

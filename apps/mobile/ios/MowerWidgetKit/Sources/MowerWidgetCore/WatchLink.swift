import Foundation

// The messages between the iPhone app and the Apple Watch app over WatchConnectivity (ADR 0060).
//
// The watch never talks to the MowDeck server and holds no credential. It asks the iPhone
// (`WCSession.sendMessage` with a reply handler, which wakes the iPhone app in the background),
// the iPhone runs the command with the device credential of ADR 0059 and answers with the
// outcome. State goes the other way: the latest snapshot of every mower as the application
// context (latest wins) and, when the watch face would change, as complication user info.
//
// Everything on the wire is a property-list dictionary (String, Int, Bool, Data), which is what
// WCSession accepts. Nothing here imports WatchConnectivity, so it is tested with `swift test`.

public enum WatchLink {
    /// Bumped when a message changes incompatibly; both sides ignore other versions.
    public static let version = 1

    /// At most this many mowers travel to the watch (the context must stay small).
    public static let maxMowers = 8

    enum Key {
        static let version = "v"
        static let op = "op"
        static let command = "command"
        static let mowerId = "mowerId"
        static let ok = "ok"
        static let failure = "failure"
        static let state = "state"
    }

    static let commandOp = "command"
    static let refreshOp = "refresh"
}

/// What the watch can ask the iPhone to send to a mower.
public enum WatchCommand: String, CaseIterable, Sendable {
    /// All areas with the mower's saved mowing defaults (like the widget's Start button).
    case start
    case pause
    /// Back to the station, keeping the job (`return_to_dock` with `end_job: false`).
    case dock
    /// Blades and wheels stop at once; confirmed on the watch before it is sent.
    case emergencyStop = "emergency_stop"

    public var isEmergency: Bool { self == .emergencyStop }
}

/// A message from the watch to the iPhone.
public enum WatchRequest: Equatable, Sendable {
    case command(WatchCommand, mowerId: String)
    /// Send the current state now (the watch app came to the foreground).
    case refresh

    public var message: [String: Any] {
        switch self {
        case .command(let command, let mowerId):
            return [
                WatchLink.Key.version: WatchLink.version,
                WatchLink.Key.op: WatchLink.commandOp,
                WatchLink.Key.command: command.rawValue,
                WatchLink.Key.mowerId: mowerId,
            ]
        case .refresh:
            return [WatchLink.Key.version: WatchLink.version, WatchLink.Key.op: WatchLink.refreshOp]
        }
    }

    /// Nil for anything the iPhone must not act on: another version, an unknown operation or
    /// command, a mower id that is not a UUID (it ends up in a URL path).
    public init?(message: [String: Any]) {
        guard message[WatchLink.Key.version] as? Int == WatchLink.version,
              let op = message[WatchLink.Key.op] as? String
        else { return nil }
        switch op {
        case WatchLink.refreshOp:
            self = .refresh
        case WatchLink.commandOp:
            guard let raw = message[WatchLink.Key.command] as? String,
                  let command = WatchCommand(rawValue: raw),
                  let mowerId = message[WatchLink.Key.mowerId] as? String,
                  UUID(uuidString: mowerId) != nil
            else { return nil }
            self = .command(command, mowerId: mowerId)
        default:
            return nil
        }
    }
}

/// Why a command did not go through.
public enum WatchFailure: String, Equatable, Sendable {
    /// The iPhone has no device credential: widgets are off in the app (ADR 0059).
    case notSetUp = "not_set_up"
    /// The user may see the mower but not command it (403).
    case notAllowed = "not_allowed"
    case notFound = "not_found"
    /// The iPhone could not reach the MowDeck server (not at home and no VPN).
    case serverUnreachable = "server_unreachable"
    /// The watch could not reach the iPhone (set on the watch, never sent).
    case phoneUnreachable = "phone_unreachable"
    /// The iPhone did not answer in time: the command may or may not have been sent (set on the
    /// watch, never sent).
    case noAnswer = "no_answer"
    case badRequest = "bad_request"
    case failed

    /// The key in the watch app's `Localizable.xcstrings`.
    public var localizationKey: String { "failure.\(rawValue)" }

    /// The failure of an API call on the iPhone.
    public init(_ error: Error) {
        if let client = error as? ClientError {
            switch client {
            case .notConfigured, .credentialRejected, .unauthorized: self = .notSetUp
            case .forbidden: self = .notAllowed
            case .notFound: self = .notFound
            case .server, .invalidResponse: self = .failed
            }
        } else if error is URLError {
            self = .serverUnreachable
        } else {
            self = .failed
        }
    }
}

/// The iPhone's answer to a command.
public struct WatchReply: Equatable, Sendable {
    public var failure: WatchFailure?

    public init(failure: WatchFailure?) { self.failure = failure }

    public static let success = WatchReply(failure: nil)

    public var ok: Bool { failure == nil }

    public var message: [String: Any] {
        var message: [String: Any] = [WatchLink.Key.version: WatchLink.version, WatchLink.Key.ok: ok]
        if let failure { message[WatchLink.Key.failure] = failure.rawValue }
        return message
    }

    /// An answer that is not a clear success counts as a failure.
    public init(message: [String: Any]) {
        if message[WatchLink.Key.ok] as? Bool == true {
            self.failure = nil
        } else {
            let raw = message[WatchLink.Key.failure] as? String ?? ""
            self.failure = WatchFailure(rawValue: raw) ?? .failed
        }
    }
}

/// What the watch shows: every mower's last snapshot, as the iPhone read it.
public struct WatchState: Codable, Equatable, Sendable {
    /// The iPhone has the device credential (widgets are on in the app).
    public var configured: Bool
    /// The iPhone reached the server for this state; false: the snapshots are the last known.
    public var serverReachable: Bool
    public var snapshots: [MowerSnapshot]
    public var updatedAt: Date
    /// Not configured because the user signed out in the app (absent from older iPhone builds).
    public var signedOut: Bool?

    /// After this long without a new state the watch says the values may be out of date.
    public static let outdatedAfter: TimeInterval = 15 * 60

    public init(
        configured: Bool, serverReachable: Bool, snapshots: [MowerSnapshot], updatedAt: Date,
        signedOut: Bool? = nil
    ) {
        self.configured = configured
        self.serverReachable = serverReachable
        self.snapshots = Array(snapshots.prefix(WatchLink.maxMowers))
        self.updatedAt = updatedAt
        self.signedOut = signedOut
    }

    public static func notConfigured(at now: Date, signedOut: Bool = false) -> WatchState {
        WatchState(
            configured: false, serverReachable: true, snapshots: [], updatedAt: now,
            signedOut: signedOut ? true : nil)
    }

    public func snapshot(for mowerId: String) -> MowerSnapshot? {
        snapshots.first { $0.mower.id == mowerId }
    }

    public func isOutdated(now: Date) -> Bool {
        now.timeIntervalSince(updatedAt) > Self.outdatedAfter
    }

    /// The application context (or complication user info) that carries this state.
    public var context: [String: Any] {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(self) else { return [:] }
        return [WatchLink.Key.version: WatchLink.version, WatchLink.Key.state: data]
    }

    public init?(context: [String: Any]) {
        guard context[WatchLink.Key.version] as? Int == WatchLink.version,
              let data = context[WatchLink.Key.state] as? Data,
              let decoded = try? JSONDecoder.mowdeck.decode(WatchState.self, from: data)
        else { return nil }
        self = decoded
    }
}

/// watchOS allows an app about 50 complication transfers a day
/// (`transferCurrentComplicationUserInfo`); everything else goes as the application context,
/// which the watch reads when the app runs. A transfer is spent only when the watch face would
/// visibly change: another state, a battery step of 10 %, a progress step of 25 %.
public enum ComplicationPolicy {
    struct Face: Equatable {
        let state: DisplayState
        let batteryStep: Int
        let progressStep: Int?
    }

    static func face(_ snapshot: MowerSnapshot?) -> Face? {
        guard let snapshot else { return nil }
        return Face(
            state: Presentation.displayState(
                status: snapshot.status, verdict: snapshot.schedule?.verdict),
            batteryStep: (snapshot.status?.batteryPercent ?? -10) / 10,
            progressStep: snapshot.status?.job?.progressPercent.map { $0 / 25 })
    }

    public static func worthTransfer(previous: WatchState?, current: WatchState) -> Bool {
        guard let previous else { return true }
        if previous.configured != current.configured { return true }
        if previous.snapshots.map(\.mower) != current.snapshots.map(\.mower) { return true }
        for snapshot in current.snapshots
        where face(previous.snapshot(for: snapshot.mower.id)) != face(snapshot) {
            return true
        }
        return false
    }
}

/// The watch's copy of the state, in the watch's own App Group container (a separate device:
/// the same group id as on the iPhone, a different file system). The watch app writes it, the
/// complication reads it.
public final class WatchStore: @unchecked Sendable {
    public static let shared = WatchStore(
        defaults: UserDefaults(suiteName: SharedState.appGroup) ?? .standard)

    private let defaults: UserDefaults
    private let lock = NSLock()

    private enum Key {
        static let state = "watch.state"
        static let primary = "watch.primary-mower"
    }

    public init(defaults: UserDefaults) { self.defaults = defaults }

    public var state: WatchState? {
        get {
            lock.lock()
            defer { lock.unlock() }
            guard let data = defaults.data(forKey: Key.state) else { return nil }
            return try? JSONDecoder.mowdeck.decode(WatchState.self, from: data)
        }
        set {
            lock.lock()
            defer { lock.unlock() }
            guard let newValue else {
                defaults.removeObject(forKey: Key.state)
                return
            }
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            if let data = try? encoder.encode(newValue) { defaults.set(data, forKey: Key.state) }
        }
    }

    /// The mower last opened on the watch: the one the complication shows.
    public var primaryMowerId: String? {
        get { defaults.string(forKey: Key.primary) }
        set { defaults.set(newValue, forKey: Key.primary) }
    }

    /// The complication's mower: the one last opened, else the first.
    public func primarySnapshot() -> MowerSnapshot? {
        guard let state else { return nil }
        if let id = primaryMowerId, let snapshot = state.snapshot(for: id) { return snapshot }
        return state.snapshots.first
    }
}

import Foundation

/// What a widget calls the mower's condition. The owner's four words are `mowing`, `docked`,
/// `error` and `rainPause`; the others keep the picture honest (paused, driving home, offline).
public enum DisplayState: String, Equatable, Sendable {
    case mowing
    case paused
    case returning
    case docked
    case charging
    case idle
    case error
    case rainPause
    case offline
    case updating
    case unknown

    /// The key in `Localizable.xcstrings`.
    public var localizationKey: String { "state.\(rawValue)" }

    /// An SF Symbol.
    public var symbolName: String {
        switch self {
        case .mowing: return "leaf.fill"
        case .paused: return "pause.circle.fill"
        case .returning: return "arrow.uturn.backward.circle.fill"
        case .docked, .idle: return "house.fill"
        case .charging: return "bolt.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .rainPause: return "cloud.rain.fill"
        case .offline: return "wifi.slash"
        case .updating: return "arrow.down.circle.fill"
        case .unknown: return "questionmark.circle"
        }
    }

    /// A job is on (the Live Activity runs, Pause makes sense).
    public var isJob: Bool { self == .mowing || self == .paused || self == .returning }

    /// Needs the user's attention.
    public var isAlert: Bool { self == .error }
}

public enum Presentation {
    /// The condition to show. At the station (or idle) the rain pause's verdict, or the mower's
    /// own rain sensor, turns "Station" into "Regenpause".
    public static func displayState(status: MowerStatus?, verdict: Verdict?) -> DisplayState {
        guard let status else { return .unknown }
        if status.stale { return .offline }
        switch status.activity {
        case "mowing": return .mowing
        case "paused": return .paused
        case "returning": return .returning
        case "error": return .error
        case "offline": return .offline
        case "updating": return .updating
        case "unknown": return .unknown
        case "docked", "charging", "idle":
            if verdict == .rainPause || status.rainDetected == true { return .rainPause }
            if status.activity == "charging" || status.charging { return .charging }
            return status.activity == "idle" ? .idle : .docked
        default: return .unknown
        }
    }

    /// Battery as 0...1 for a ring.
    public static func batteryFraction(_ status: MowerStatus?) -> Double {
        guard let status else { return 0 }
        return Double(max(0, min(100, status.batteryPercent))) / 100
    }

    /// The progress of the running job as 0...1, nil without a job or progress.
    public static func progressFraction(_ status: MowerStatus?) -> Double? {
        guard let percent = status?.job?.progressPercent else { return nil }
        return Double(max(0, min(100, percent))) / 100
    }

    /// The next run that has not started yet.
    public static func nextRun(_ overview: ScheduleOverview?, now: Date) -> ScheduledRun? {
        overview?.runs.filter { $0.startsAt > now }.min { $0.startsAt < $1.startsAt }
    }

    /// The one line of the inline lock screen widget.
    public enum InlineLine: Equatable, Sendable {
        case rainPause(until: Date?)
        case nextRun(Date)
        case state(DisplayState)
    }

    /// "Regenpause bis 16:00", "Nächster Lauf 14:00", else the state.
    public static func inlineLine(
        status: MowerStatus?, overview: ScheduleOverview?, now: Date
    ) -> InlineLine {
        let state = displayState(status: status, verdict: overview?.verdict)
        if state.isJob || state.isAlert || state == .offline { return .state(state) }
        if state == .rainPause { return .rainPause(until: overview?.rain.pauseUntil) }
        if let run = nextRun(overview, now: now) { return .nextRun(run.startsAt) }
        return .state(state)
    }

    /// How long a widget entry stays useful: a mowing mower changes fast, a docked one slowly.
    public static func refreshInterval(for state: DisplayState) -> TimeInterval {
        switch state {
        case .mowing, .paused, .returning: return 5 * 60
        case .error, .updating: return 10 * 60
        default: return 15 * 60
        }
    }

    /// The words for the mower's error: the lexicon's title (ADR 0067), else the vendor's own
    /// message, else the code; nil while the mower reports no error.
    public static func errorLine(_ status: MowerStatus?) -> String? {
        guard let status, status.activity == "error", !status.stale else { return nil }
        for text in [status.errorTitle, status.errorMessage, status.errorCode] {
            if let text, !text.trimmingCharacters(in: .whitespaces).isEmpty { return text }
        }
        return nil
    }

    /// The error needs the user at once (a lift or tilt, `critical` in the lexicon).
    public static func isCriticalError(_ status: MowerStatus?) -> Bool {
        status?.errorSeverity == "critical" || status?.errorCategory == "lift_tilt"
    }

    /// Remaining job time as a rounded number of minutes, nil when unknown.
    public static func remainingMinutes(_ status: MowerStatus?) -> Int? {
        guard let seconds = status?.job?.remainingSeconds else { return nil }
        return max(1, Int((Double(seconds) / 60).rounded(.up)))
    }
}

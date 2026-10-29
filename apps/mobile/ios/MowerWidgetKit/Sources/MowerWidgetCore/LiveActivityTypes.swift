import Foundation

/// The state shown by the Live Activity. The server sends it as the push payload's
/// `content-state` (services/notifications `domain/live_activity.py`), so the JSON keys must match
/// exactly. Times are whole epoch seconds, not `Date`, because ActivityKit's own date coding is
/// not something the server should depend on.
public struct MowingContentState: Codable, Hashable, Sendable {
    /// `mowing`, `paused`, `returning`, or the final `finished` / `error`.
    public var state: String
    public var batteryPercent: Int
    public var progressPercent: Int?
    public var areaName: String?
    public var remainingSeconds: Int?
    /// When the job is expected to end, epoch seconds (a countdown the device runs on its own).
    public var endsAt: Int?
    public var updatedAt: Int
    /// With the final `error`: the error in plain words (the vendor's error lexicon in the user's
    /// language, ADR 0067). Optional, so older servers and older payloads still decode.
    public var errorTitle: String?

    public init(
        state: String, batteryPercent: Int, progressPercent: Int? = nil, areaName: String? = nil,
        remainingSeconds: Int? = nil, endsAt: Int? = nil, updatedAt: Int, errorTitle: String? = nil
    ) {
        self.state = state
        self.batteryPercent = batteryPercent
        self.progressPercent = progressPercent
        self.areaName = areaName
        self.remainingSeconds = remainingSeconds
        self.endsAt = endsAt
        self.updatedAt = updatedAt
        self.errorTitle = errorTitle
    }

    /// From a status read by the app itself (a Live Activity started from the app).
    public init(status: MowerStatus, now: Date) {
        self.init(
            state: status.activity,
            batteryPercent: status.batteryPercent,
            progressPercent: status.job?.progressPercent,
            areaName: status.job?.areaName,
            remainingSeconds: status.job?.remainingSeconds,
            endsAt: status.job?.remainingSeconds.map { Int(now.timeIntervalSince1970) + $0 },
            updatedAt: Int(now.timeIntervalSince1970),
            errorTitle: status.activity == "error" ? Presentation.errorLine(status) : nil)
    }

    public var progressFraction: Double? {
        progressPercent.map { Double(max(0, min(100, $0))) / 100 }
    }

    public var endsAtDate: Date? { endsAt.map { Date(timeIntervalSince1970: TimeInterval($0)) } }

    public var isFinal: Bool { state == "finished" || state == "error" }
}

/// What the app reports to the server about a running Live Activity.
public struct LiveActivityRegistration: Codable, Equatable, Sendable {
    public var installationId: String
    public var activityId: String
    public var pushToken: String

    public init(installationId: String, activityId: String, pushToken: String) {
        self.installationId = installationId
        self.activityId = activityId
        self.pushToken = pushToken
    }

    enum CodingKeys: String, CodingKey {
        case installationId = "installation_id"
        case activityId = "activity_id"
        case pushToken = "push_token"
    }
}

public enum PushToken {
    /// ActivityKit and APNs tokens as lower-case hex, the form the server stores.
    public static func hex(_ data: Data) -> String {
        data.map { String(format: "%02x", $0) }.joined()
    }
}

// iOS only: the watch targets link this package too (ADR 0060) and have no use for ActivityKit.
#if canImport(ActivityKit) && os(iOS)
import ActivityKit

/// The Live Activity of a mowing job (Lock Screen and Dynamic Island). The type name is part of
/// the push-to-start payload (`attributes-type`).
@available(iOS 16.1, *)
public struct MowingActivityAttributes: ActivityAttributes {
    public typealias ContentState = MowingContentState

    public var mowerId: String
    public var mowerName: String
    /// Epoch seconds.
    public var startedAt: Int

    public init(mowerId: String, mowerName: String, startedAt: Int) {
        self.mowerId = mowerId
        self.mowerName = mowerName
        self.startedAt = startedAt
    }
}
#endif

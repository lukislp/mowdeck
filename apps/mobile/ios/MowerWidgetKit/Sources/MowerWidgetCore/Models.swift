import Foundation

// The JSON the widgets read from the MowDeck API (ADR 0059): Telemetry's compact state,
// the Scheduler's overview of next runs and the Fleet's mower list. Field names follow the
// server's snake_case; unknown fields are ignored so a newer server never breaks an older widget.

/// The running job of a mower.
public struct JobInfo: Codable, Equatable, Sendable {
    public var progressPercent: Int?
    public var areaId: String?
    public var areaName: String?
    public var elapsedSeconds: Int?
    public var remainingSeconds: Int?
    public var mowedSquareMetres: Double?

    public init(
        progressPercent: Int? = nil, areaId: String? = nil, areaName: String? = nil,
        elapsedSeconds: Int? = nil, remainingSeconds: Int? = nil, mowedSquareMetres: Double? = nil
    ) {
        self.progressPercent = progressPercent
        self.areaId = areaId
        self.areaName = areaName
        self.elapsedSeconds = elapsedSeconds
        self.remainingSeconds = remainingSeconds
        self.mowedSquareMetres = mowedSquareMetres
    }

    enum CodingKeys: String, CodingKey {
        case progressPercent = "progress_percent"
        case areaId = "area_id"
        case areaName = "area_name"
        case elapsedSeconds = "elapsed_s"
        case remainingSeconds = "remaining_s"
        case mowedSquareMetres = "mowed_m2"
    }
}

/// `GET /api/v1/telemetry/mowers/{id}/widget`.
public struct MowerStatus: Codable, Equatable, Sendable {
    public var mowerId: String
    public var stale: Bool
    /// `MowerActivity` as the server names it; `offline` while the state is stale.
    public var activity: String
    public var batteryPercent: Int
    public var charging: Bool
    public var rainDetected: Bool?
    public var errorCode: String?
    public var errorMessage: String?
    /// The error in plain words from the vendor's error lexicon, in the language the widget asked
    /// for (ADR 0067). Absent from older servers.
    public var errorTitle: String?
    /// `MowerErrorCategory` (`lift_tilt`, `trapped`, ...).
    public var errorCategory: String?
    /// `info`, `warning`, `error` or `critical`.
    public var errorSeverity: String?
    public var job: JobInfo?

    public init(
        mowerId: String, stale: Bool = false, activity: String, batteryPercent: Int,
        charging: Bool = false, rainDetected: Bool? = nil, errorCode: String? = nil,
        errorMessage: String? = nil, errorTitle: String? = nil, errorCategory: String? = nil,
        errorSeverity: String? = nil, job: JobInfo? = nil
    ) {
        self.mowerId = mowerId
        self.stale = stale
        self.activity = activity
        self.batteryPercent = batteryPercent
        self.charging = charging
        self.rainDetected = rainDetected
        self.errorCode = errorCode
        self.errorMessage = errorMessage
        self.errorTitle = errorTitle
        self.errorCategory = errorCategory
        self.errorSeverity = errorSeverity
        self.job = job
    }

    enum CodingKeys: String, CodingKey {
        case mowerId = "mower_id"
        case stale
        case activity
        case batteryPercent = "battery_percent"
        case charging
        case rainDetected = "rain_detected"
        case errorCode = "error_code"
        case errorMessage = "error_message"
        case errorTitle = "error_title"
        case errorCategory = "error_category"
        case errorSeverity = "error_severity"
        case job
    }
}

/// One upcoming run.
public struct ScheduledRun: Codable, Equatable, Sendable {
    public var scheduleId: String
    public var name: String
    public var startsAt: Date
    public var durationMinutes: Int

    public init(scheduleId: String, name: String, startsAt: Date, durationMinutes: Int) {
        self.scheduleId = scheduleId
        self.name = name
        self.startsAt = startsAt
        self.durationMinutes = durationMinutes
    }

    enum CodingKeys: String, CodingKey {
        case scheduleId = "schedule_id"
        case name
        case startsAt = "starts_at"
        case durationMinutes = "duration_minutes"
    }
}

/// The rain pause's verdict for now.
public enum Verdict: String, Codable, Equatable, Sendable {
    case runs
    case rainPause = "rain_pause"
    case unsure

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Verdict(rawValue: raw) ?? .unsure
    }
}

public struct RainFacts: Codable, Equatable, Sendable {
    public var rainingNow: Bool?
    public var expectedMillimetres: Double?
    public var wet: Bool
    public var wetUntil: Date?
    public var pauseUntil: Date?
    public var recentMillimetres6h: Double?

    public init(
        rainingNow: Bool? = nil, expectedMillimetres: Double? = nil, wet: Bool = false,
        wetUntil: Date? = nil, pauseUntil: Date? = nil, recentMillimetres6h: Double? = nil
    ) {
        self.rainingNow = rainingNow
        self.expectedMillimetres = expectedMillimetres
        self.wet = wet
        self.wetUntil = wetUntil
        self.pauseUntil = pauseUntil
        self.recentMillimetres6h = recentMillimetres6h
    }

    enum CodingKeys: String, CodingKey {
        case rainingNow = "raining_now"
        case expectedMillimetres = "expected_mm_next_hours"
        case wet
        case wetUntil = "wet_until"
        case pauseUntil = "pause_until"
        case recentMillimetres6h = "recent_mm_6h"
    }
}

/// `GET /api/v1/scheduler/mowers/{id}/widget`.
public struct ScheduleOverview: Codable, Equatable, Sendable {
    public var mowerId: String
    public var runs: [ScheduledRun]
    public var verdict: Verdict
    public var rain: RainFacts

    public init(mowerId: String, runs: [ScheduledRun], verdict: Verdict, rain: RainFacts) {
        self.mowerId = mowerId
        self.runs = runs
        self.verdict = verdict
        self.rain = rain
    }

    enum CodingKeys: String, CodingKey {
        case mowerId = "mower_id"
        case runs
        case verdict
        case rain
    }
}

/// A mower of the account (`GET /api/v1/fleet/mowers`), as far as the widgets need it.
public struct MowerRef: Codable, Hashable, Sendable {
    public var id: String
    public var name: String

    public init(id: String, name: String) {
        self.id = id
        self.name = name
    }
}

/// Everything one timeline entry shows. A missing part (offline server, no schedule) is nil, so a
/// widget can still show what it has.
public struct MowerSnapshot: Codable, Equatable, Sendable {
    public var mower: MowerRef
    public var status: MowerStatus?
    public var schedule: ScheduleOverview?
    public var fetchedAt: Date

    public init(
        mower: MowerRef, status: MowerStatus?, schedule: ScheduleOverview?, fetchedAt: Date
    ) {
        self.mower = mower
        self.status = status
        self.schedule = schedule
        self.fetchedAt = fetchedAt
    }
}

/// Untyped JSON, used to pass the mower's saved mowing pattern back to the start command
/// unchanged (the widget must not know the pattern's fields).
public enum JSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }
}

extension JSONDecoder {
    /// Decoder for the API: ISO 8601 dates with or without fractional seconds.
    public static var mowdeck: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let raw = try decoder.singleValueContainer().decode(String.self)
            if let date = DateParsing.parse(raw) { return date }
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "bad date \(raw)"))
        }
        return decoder
    }
}

enum DateParsing {
    static func parse(_ raw: String) -> Date? {
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: raw) { return date }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: raw)
    }
}

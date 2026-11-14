// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import Foundation

// The area-clear confirmation of the native surfaces (ADR 0084): before a widget, Siri,
// Shortcuts or the Apple Watch sends a command that makes the mower move, the user confirms
// that the area is clear of people, children and animals, with the same wording as the web
// app's safety check (apps/web/src/lib/safety, PR #367). Stop, pause and back to the station are
// never asked: stopping is never delayed.
//
// A client safeguard only. The server validates every command as before (ADR 0012); nothing
// here is sent to it.

/// The mower commands a native surface can send (the `kind` of the Fleet command).
public enum MotionCommand: String, CaseIterable, Sendable {
    case start = "start_mow"
    case resume = "resume_mow"
    case pause = "pause_mow"
    case returnToDock = "return_to_dock"
    case emergencyStop = "emergency_stop"
}

/// The one place that decides which native commands need the area-clear confirmation. Every
/// surface asks this, none decides on its own. The switch is exhaustive: a new command does not
/// compile until someone decides whether it makes the mower move.
public enum MotionCommandPolicy {
    public static func needsAreaClearConfirmation(_ command: MotionCommand) -> Bool {
        switch command {
        // Starts blade movement or drives the mower out.
        case .start, .resume:
            return true
        // Stops or parks the mower; never delayed (the emergency stop above all).
        case .pause, .returnToDock, .emergencyStop:
            return false
        }
    }
}

extension WatchCommand {
    /// The Fleet command the iPhone sends for this watch command.
    public var motionCommand: MotionCommand {
        switch self {
        case .start: return .start
        case .pause: return .pause
        case .dock: return .returnToDock
        case .emergencyStop: return .emergencyStop
        }
    }

    public var needsAreaClearConfirmation: Bool {
        MotionCommandPolicy.needsAreaClearConfirmation(motionCommand)
    }
}

/// The wording of the confirmation, the same statement as the web app's safety check
/// (`safety_title`, `safety_statement`, `safety_confirm`, `safety_go` in apps/web/messages),
/// in English and German, from this package's `Localizable.strings`.
public enum AreaClearWording {
    /// The bundle with the translations (this package's resources).
    public static var bundle: Bundle { .module }

    /// "Before the mower moves"
    public static var title: String { text("area_clear.title") }
    /// The area must be clear; the user supervises the mower.
    public static var statement: String { text("area_clear.statement") }
    /// What the user confirms by pressing the confirm button.
    public static var acknowledgement: String { text("area_clear.acknowledgement") }
    /// The confirm button ("Confirm").
    public static var confirm: String { text("area_clear.confirm") }
    public static var cancel: String { text("area_clear.cancel") }
    /// The accessibility hint of a start button that asks first.
    public static var startHint: String { text("area_clear.start_hint") }
    /// The accessibility hint of the widget's Start, which opens the app to ask.
    public static var widgetStartHint: String { text("area_clear.widget_start_hint") }
    /// Said by VoiceOver after the start was sent from the app's confirmation.
    public static var sent: String { text("area_clear.sent") }
    public static var failedTitle: String { text("area_clear.failed_title") }
    public static var failedNotSetUp: String { text("area_clear.failed_not_set_up") }
    public static var failedNotAllowed: String { text("area_clear.failed_not_allowed") }
    public static var failed: String { text("area_clear.failed") }

    /// The body of a confirmation: the mower's name (when known), the statement and what the
    /// confirm button acknowledges.
    public static func message(mowerName: String?) -> String {
        var parts: [String] = []
        if let mowerName, !mowerName.isEmpty { parts.append(mowerName) }
        parts.append(statement)
        parts.append(acknowledgement)
        return parts.joined(separator: "\n\n")
    }

    /// The whole question in one text, for Siri and Shortcuts (spoken or shown).
    public static func dialog(mowerName: String?) -> String {
        "\(title). \(message(mowerName: mowerName))"
    }

    /// Every key, for the completeness test of the translations.
    static let keys = [
        "area_clear.title", "area_clear.statement", "area_clear.acknowledgement",
        "area_clear.confirm", "area_clear.cancel", "area_clear.start_hint",
        "area_clear.widget_start_hint", "area_clear.sent", "area_clear.failed_title",
        "area_clear.failed_not_set_up", "area_clear.failed_not_allowed", "area_clear.failed",
    ]

    private static func text(_ key: String) -> String {
        Bundle.module.localizedString(forKey: key, value: nil, table: nil)
    }
}

/// The link the widget's Start button opens: the app shows the confirmation and sends the start
/// only after it (an interactive widget runs its App Intent in the extension, where no
/// confirmation can be shown). `mowdeck://mowers/<id>?confirm=start`: without the app's
/// interception the web app opens the mower page and ignores the query. The link never starts
/// anything by itself, so another app opening it can only bring up the question.
public enum AreaClearLink {
    static let host = "mowers"
    static let query = "confirm"

    public static func url(mowerId: String, command: MotionCommand = .start) -> URL? {
        guard UUID(uuidString: mowerId) != nil,
              MotionCommandPolicy.needsAreaClearConfirmation(command)
        else { return nil }
        var components = URLComponents()
        components.scheme = "mowdeck"
        components.host = host
        components.path = "/\(mowerId)"
        components.queryItems = [URLQueryItem(name: query, value: name(of: command))]
        return components.url
    }

    /// The mower and command of a confirmation link; nil for every other link (and for a
    /// command that needs no confirmation, a mower id that is not a UUID).
    public static func parse(_ url: URL) -> (mowerId: String, command: MotionCommand)? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme == "mowdeck", components.host == host
        else { return nil }
        let segments = components.path.split(separator: "/")
        guard segments.count == 1, UUID(uuidString: String(segments[0])) != nil,
              let value = components.queryItems?.first(where: { $0.name == query })?.value,
              let command = MotionCommand.allCases.first(where: { name(of: $0) == value }),
              MotionCommandPolicy.needsAreaClearConfirmation(command)
        else { return nil }
        return (String(segments[0]), command)
    }

    private static func name(of command: MotionCommand) -> String {
        switch command {
        case .start: return "start"
        case .resume: return "resume"
        case .pause: return "pause"
        case .returnToDock: return "dock"
        case .emergencyStop: return "emergency_stop"
        }
    }
}

import AppIntents
import MowerWidgetCore
import SwiftUI
import WidgetKit

// The pictures of the widgets. Text keys live in Localizable.xcstrings (English and German).

extension DisplayState {
    /// Semantic colours; the system adapts them to light and dark.
    var tint: Color {
        switch self {
        case .mowing: return .green
        case .paused, .returning, .updating: return .orange
        case .charging: return .mint
        case .error: return .red
        case .rainPause: return .blue
        case .docked, .idle: return .secondary
        case .offline, .unknown: return .gray
        }
    }

    var title: LocalizedStringKey { LocalizedStringKey(localizationKey) }
}

/// The battery as a ring with the percentage (or an icon) inside.
struct BatteryRing: View {
    let fraction: Double
    let charging: Bool
    var lineWidth: CGFloat = 5

    var body: some View {
        ZStack {
            Circle().stroke(Color.secondary.opacity(0.25), lineWidth: lineWidth)
            Circle()
                .trim(from: 0, to: max(0.02, fraction))
                .stroke(
                    fraction < 0.2 ? Color.red : Color.green,
                    style: StrokeStyle(lineWidth: lineWidth, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
            if charging {
                Image(systemName: "bolt.fill").font(.system(size: 9, weight: .bold))
            }
        }
        .accessibilityElement()
        .accessibilityLabel(Text("Battery"))
        .accessibilityValue(Text("\(Int((fraction * 100).rounded())) percent"))
    }
}

struct StateLabel: View {
    let state: DisplayState
    var body: some View {
        Label {
            Text(state.title)
        } icon: {
            Image(systemName: state.symbolName)
        }
        .foregroundStyle(state.tint)
    }
}

/// Shown instead of data when the widget cannot work yet.
struct ProblemView: View {
    let problem: MowerEntry.Problem
    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: problem == .offline ? "wifi.slash" : "gearshape")
                .font(.title2).foregroundStyle(.secondary)
            switch problem {
            case .notSetUp:
                Text("Open MowDeck and turn on widgets in the settings.")
            case .noMower:
                Text("Choose a mower in the widget settings.")
            case .offline:
                Text("Server not reachable.")
            }
        }
        .font(.footnote)
        .multilineTextAlignment(.center)
    }
}

// MARK: Status (small, medium)

struct StatusWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: MowerEntry

    var body: some View {
        Group {
            if let problem = entry.problem, entry.status == nil {
                ProblemView(problem: problem)
            } else if family == .systemMedium {
                medium
            } else {
                small
            }
        }
        .widgetURL(entry.mower.flatMap { URL(string: "mowdeck://mowers/\($0.id)") })
    }

    private var small: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Image(systemName: entry.state.symbolName)
                    .font(.title2).foregroundStyle(entry.state.tint)
                Spacer()
                BatteryRing(
                    fraction: Presentation.batteryFraction(entry.status),
                    charging: entry.status?.charging ?? false
                )
                .frame(width: 34, height: 34)
            }
            Spacer(minLength: 0)
            Text(entry.mower?.name ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Text(entry.state.title).font(.headline).foregroundStyle(entry.state.tint).lineLimit(1)
            if let percent = entry.status?.job?.progressPercent, entry.state.isJob {
                Text("\(percent) %").font(.caption.monospacedDigit())
            }
            if entry.problem == .offline { staleNote }
        }
    }

    private var medium: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Image(systemName: entry.state.symbolName)
                        .font(.title2).foregroundStyle(entry.state.tint)
                    BatteryRing(
                        fraction: Presentation.batteryFraction(entry.status),
                        charging: entry.status?.charging ?? false
                    )
                    .frame(width: 30, height: 30)
                }
                Text(entry.mower?.name ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Text(entry.state.title).font(.headline).foregroundStyle(entry.state.tint)
                if entry.problem == .offline { staleNote }
                Spacer(minLength: 0)
                details
            }
            Spacer(minLength: 0)
            buttons
        }
    }

    @ViewBuilder private var details: some View {
        if entry.state.isJob, let status = entry.status {
            if let fraction = Presentation.progressFraction(status) {
                ProgressView(value: fraction).tint(entry.state.tint)
            }
            HStack(spacing: 6) {
                if let percent = status.job?.progressPercent { Text("\(percent) %") }
                if let area = status.job?.areaName { Text(area).lineLimit(1) }
            }
            .font(.caption)
        } else if let run = Presentation.nextRun(entry.schedule, now: entry.date) {
            Label {
                Text("Next run \(run.startsAt, style: .time)")
            } icon: {
                Image(systemName: "calendar")
            }
            .font(.caption).foregroundStyle(.secondary)
        }
    }

    /// iOS 17 interactive buttons. Start is offered when nothing runs, Pause while mowing.
    @ViewBuilder private var buttons: some View {
        if let mower = entry.mower {
            VStack(spacing: 6) {
                if entry.state == .mowing {
                    Button(intent: PauseMowingIntent(mowerId: mower.id)) {
                        Label("Pause", systemImage: "pause.fill")
                    }
                } else if entry.state.isJob == false, entry.state != .error, entry.state != .offline {
                    Button(intent: StartMowingIntent(mowerId: mower.id)) {
                        Label("Start", systemImage: "play.fill")
                    }
                }
                if entry.state != .docked, entry.state != .charging, entry.state != .rainPause,
                   entry.state != .offline, entry.state != .unknown
                {
                    Button(intent: ReturnToStationIntent(mowerId: mower.id)) {
                        Label("Station", systemImage: "house.fill")
                    }
                }
            }
            .buttonStyle(.bordered)
            .labelStyle(.titleAndIcon)
            .font(.caption.weight(.semibold))
            .tint(entry.state.tint)
        }
    }

    private var staleNote: some View {
        Text("Last known state").font(.caption2).foregroundStyle(.secondary)
    }
}

// MARK: Lock screen (circular, rectangular, inline)

struct LockScreenWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: MowerEntry

    var body: some View {
        switch family {
        case .accessoryCircular: circular
        case .accessoryInline: inline
        default: rectangular
        }
    }

    private var circular: some View {
        Gauge(value: Presentation.batteryFraction(entry.status)) {
            Image(systemName: entry.state.symbolName)
        } currentValueLabel: {
            Text("\(entry.status?.batteryPercent ?? 0)")
        }
        .gaugeStyle(.accessoryCircular)
        .accessibilityLabel(Text(entry.state.title))
    }

    private var rectangular: some View {
        VStack(alignment: .leading, spacing: 2) {
            Label {
                if entry.state.isJob, let percent = entry.status?.job?.progressPercent {
                    Text("\(Text(entry.state.title)) · \(percent) %")
                } else {
                    Text(entry.state.title)
                }
            } icon: {
                Image(systemName: entry.state.symbolName)
            }
            .font(.headline).lineLimit(1)
            if let area = entry.status?.job?.areaName ?? entry.mower?.name {
                Text(area).font(.caption).lineLimit(1)
            }
            if let fraction = Presentation.progressFraction(entry.status), entry.state.isJob {
                ProgressView(value: fraction)
            } else if let problem = entry.problem {
                Text(problem == .notSetUp ? "Set up in the app" : "Offline").font(.caption2)
            }
        }
    }

    @ViewBuilder private var inline: some View {
        switch Presentation.inlineLine(status: entry.status, overview: entry.schedule, now: entry.date) {
        case .rainPause(let until?):
            Label("Rain pause until \(until, style: .time)", systemImage: "cloud.rain")
        case .rainPause(nil):
            Label("Rain pause", systemImage: "cloud.rain")
        case .nextRun(let date):
            Label("Next run \(date, style: .time)", systemImage: "calendar")
        case .state(let state):
            Label {
                Text(state.title)
            } icon: {
                Image(systemName: state.symbolName)
            }
        }
    }
}

// MARK: Map (large)

struct MapWidgetView: View {
    @Environment(\.colorScheme) private var scheme
    let entry: MowerEntry

    var body: some View {
        VStack(spacing: 8) {
            if let problem = entry.problem, entry.status == nil {
                ProblemView(problem: problem).frame(maxHeight: .infinity)
            } else {
                map
                HStack {
                    StateLabel(state: entry.state).font(.headline)
                    Spacer()
                    if let percent = entry.status?.job?.progressPercent, entry.state.isJob {
                        Text("\(percent) %").font(.headline.monospacedDigit())
                    }
                    BatteryRing(
                        fraction: Presentation.batteryFraction(entry.status),
                        charging: entry.status?.charging ?? false, lineWidth: 4
                    )
                    .frame(width: 26, height: 26)
                }
                if let fraction = Presentation.progressFraction(entry.status), entry.state.isJob {
                    ProgressView(value: fraction).tint(entry.state.tint)
                }
                if let area = entry.status?.job?.areaName {
                    Text(area).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .widgetURL(entry.mower.flatMap { URL(string: "mowdeck://mowers/\($0.id)/map") })
    }

    @ViewBuilder private var map: some View {
        let data = scheme == .dark ? entry.mapDark : entry.mapLight
        if let data, let image = UIImage(data: data) {
            Image(uiImage: image)
                .resizable().scaledToFill()
                .frame(maxWidth: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .accessibilityLabel(Text("Map with the mower and the mowed path"))
        } else {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color.secondary.opacity(0.15))
                .overlay(Image(systemName: "map").font(.largeTitle).foregroundStyle(.secondary))
        }
    }
}

// MARK: Schedule and weather (medium)

struct ScheduleWidgetView: View {
    let entry: MowerEntry

    var body: some View {
        if let problem = entry.problem, entry.schedule == nil {
            ProblemView(problem: problem)
        } else {
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(entry.mower?.name ?? "").font(.caption).foregroundStyle(.secondary)
                    if let runs = entry.schedule?.runs, !runs.isEmpty {
                        ForEach(Array(runs.prefix(3)), id: \.scheduleId) { run in
                            HStack {
                                Text(run.startsAt, style: .relative).font(.caption2).foregroundStyle(.secondary)
                                Spacer(minLength: 4)
                                Text(run.startsAt, format: .dateTime.weekday(.abbreviated).hour().minute())
                                    .font(.subheadline.monospacedDigit())
                            }
                        }
                    } else {
                        Text("No upcoming runs").font(.subheadline).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                }
                VStack(alignment: .leading, spacing: 6) {
                    verdict
                    rain
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: 130, alignment: .leading)
            }
        }
    }

    private var verdictInfo: (LocalizedStringKey, String, Color) {
        switch entry.schedule?.verdict ?? .unsure {
        case .runs: return ("Will run", "checkmark.circle.fill", .green)
        case .rainPause: return ("Rain pause", "cloud.rain.fill", .blue)
        case .unsure: return ("Uncertain", "questionmark.circle.fill", .orange)
        }
    }

    private var verdict: some View {
        let info = verdictInfo
        return Label {
            Text(info.0)
        } icon: {
            Image(systemName: info.1)
        }
        .font(.headline).foregroundStyle(info.2)
    }

    @ViewBuilder private var rain: some View {
        if let facts = entry.schedule?.rain {
            if facts.rainingNow == true {
                Text("Raining now").font(.caption)
            } else if let until = facts.pauseUntil {
                Text("Pause until \(until, style: .time)").font(.caption)
            } else if facts.wet, let until = facts.wetUntil {
                Text("Lawn wet until \(until, style: .time)").font(.caption)
            } else if let mm = facts.expectedMillimetres, mm > 0 {
                Text("\(mm, specifier: "%.1f") mm expected").font(.caption)
            } else {
                Text("No rain expected").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

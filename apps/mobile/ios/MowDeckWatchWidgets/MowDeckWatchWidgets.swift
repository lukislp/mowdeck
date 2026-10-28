import MowerWidgetCore
import SwiftUI
import WidgetKit

// The complication of the MowDeck watch app (ADR 0060): battery and state of one mower (the one
// last opened in the watch app, else the first). It makes no network call and holds no
// credential: it reads the state the watch app keeps in the watch's App Group, which the iPhone
// sends over WatchConnectivity. The watch app reloads it on every new state.

struct ComplicationEntry: TimelineEntry {
    let date: Date
    let snapshot: MowerSnapshot?
    let configured: Bool
    /// The state is older than `WatchState.outdatedAfter`.
    let outdated: Bool
}

struct ComplicationProvider: TimelineProvider {
    private static let sample = ComplicationEntry(
        date: Date(),
        snapshot: MowerSnapshot(
            mower: MowerRef(id: "sample", name: "Luba"),
            status: MowerStatus(
                mowerId: "sample", activity: "mowing", batteryPercent: 64,
                job: JobInfo(progressPercent: 42, areaName: "Garten")),
            schedule: nil, fetchedAt: Date()),
        configured: true, outdated: false)

    func placeholder(in context: Context) -> ComplicationEntry { Self.sample }

    func getSnapshot(in context: Context, completion: @escaping (ComplicationEntry) -> Void) {
        completion(context.isPreview ? Self.sample : Self.entry(at: Date()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ComplicationEntry>) -> Void) {
        let now = Date()
        var entries = [Self.entry(at: now)]
        // One more entry for the moment the state turns old, so the face shows it greyed out
        // even when no new state arrives (iPhone away).
        if let updated = WatchStore.shared.state?.updatedAt {
            let outdatedAt = updated.addingTimeInterval(WatchState.outdatedAfter + 1)
            if outdatedAt > now { entries.append(Self.entry(at: outdatedAt)) }
        }
        completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(30 * 60))))
    }

    static func entry(at date: Date) -> ComplicationEntry {
        let store = WatchStore.shared
        let state = store.state
        return ComplicationEntry(
            date: date, snapshot: store.primarySnapshot(), configured: state?.configured ?? false,
            outdated: state?.isOutdated(now: date) ?? true)
    }
}

struct ComplicationView: View {
    @Environment(\.widgetFamily) private var family
    let entry: ComplicationEntry

    private var display: DisplayState {
        entry.outdated && entry.snapshot?.status == nil
            ? .unknown
            : Presentation.displayState(
                status: entry.snapshot?.status, verdict: entry.snapshot?.schedule?.verdict)
    }

    private var battery: Int? { entry.snapshot?.status?.batteryPercent }

    var body: some View {
        content
            .opacity(entry.outdated ? 0.6 : 1)
            .containerBackground(for: .widget) { Color.clear }
    }

    @ViewBuilder
    private var content: some View {
        if !entry.configured || entry.snapshot == nil {
            switch family {
            case .accessoryInline:
                Text(verbatim: "MowDeck")
            case .accessoryRectangular:
                VStack(alignment: .leading) {
                    Text(verbatim: "MowDeck").font(.headline)
                    Text("Set up on the iPhone").font(.caption2)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            default:
                Image(systemName: "leaf")
                    .widgetAccentable()
                    .accessibilityLabel(Text(verbatim: "MowDeck"))
            }
        } else {
            switch family {
            case .accessoryCircular:
                Gauge(value: Double(battery ?? 0) / 100) {
                    Image(systemName: display.symbolName)
                } currentValueLabel: {
                    Image(systemName: display.symbolName)
                        .widgetAccentable()
                }
                .gaugeStyle(.accessoryCircularCapacity)
                .accessibilityLabel(label)
            case .accessoryCorner:
                Image(systemName: display.symbolName)
                    .font(.title3)
                    .widgetAccentable()
                    .widgetLabel {
                        Gauge(value: Double(battery ?? 0) / 100) {
                            Text("Battery")
                        }
                        .gaugeStyle(.accessoryLinearCapacity)
                    }
                    .accessibilityLabel(label)
            case .accessoryInline:
                inlineText
            default:
                rectangular
            }
        }
    }

    private var inlineText: Text {
        let state = Text(LocalizedStringKey(display.localizationKey))
        guard let battery else { return state }
        return state + Text(verbatim: " · ") + Text(Double(battery) / 100, format: .percent)
    }

    private var label: Text {
        Text(verbatim: "\(entry.snapshot?.mower.name ?? "MowDeck"): ") + inlineText
    }

    private var rectangular: some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 4) {
                Image(systemName: display.symbolName)
                    .widgetAccentable()
                Text(entry.snapshot?.mower.name ?? "MowDeck")
                    .font(.headline)
                    .lineLimit(1)
            }
            inlineText
                .font(.caption2)
            if display.isJob, let progress = Presentation.progressFraction(entry.snapshot?.status) {
                ProgressView(value: progress)
                    .tint(.green)
            } else if display == .error, let message = entry.snapshot?.status?.errorMessage {
                Text(message)
                    .font(.caption2)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

struct MowDeckComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "MowDeckWatchComplication", provider: ComplicationProvider()) { entry in
            ComplicationView(entry: entry)
        }
        .configurationDisplayName("Mower")
        .description("Battery and state of the mower last opened on the watch.")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryRectangular, .accessoryInline])
    }
}

@main
struct MowDeckWatchWidgetsBundle: WidgetBundle {
    var body: some Widget {
        MowDeckComplication()
    }
}

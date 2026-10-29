import ActivityKit
import MowerWidgetCore
import SwiftUI
import WidgetKit

/// The Live Activity of a mowing job (Lock Screen and Dynamic Island). The server updates it by
/// APNs (`liveactivity` pushes, ADR 0059); the remaining time counts down on the device.
struct MowingLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: MowingActivityAttributes.self) { context in
            LockScreenActivityView(attributes: context.attributes, state: context.state)
                .activityBackgroundTint(nil)
                .widgetURL(URL(string: "mowdeck://mowers/\(context.attributes.mowerId)"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label {
                        Text(context.attributes.mowerName).lineLimit(1)
                    } icon: {
                        Image(systemName: ActivityPresentation.symbol(context.state.state))
                    }
                    .font(.headline)
                    .foregroundStyle(ActivityPresentation.tint(context.state.state))
                }
                DynamicIslandExpandedRegion(.trailing) {
                    if let fraction = context.state.progressFraction {
                        Text("\(Int(fraction * 100)) %").font(.title2.monospacedDigit())
                    }
                }
                DynamicIslandExpandedRegion(.center) {
                    if let error = ActivityPresentation.errorTitle(context.state) {
                        Text(error).font(.subheadline).lineLimit(1)
                    } else {
                        Text(ActivityPresentation.title(context.state.state))
                            .font(.subheadline)
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 6) {
                        if let fraction = context.state.progressFraction {
                            ProgressView(value: fraction)
                                .tint(ActivityPresentation.tint(context.state.state))
                        }
                        HStack {
                            if let area = context.state.areaName { Text(area).lineLimit(1) }
                            Spacer()
                            RemainingTime(state: context.state)
                        }
                        .font(.caption)
                    }
                }
            } compactLeading: {
                Image(systemName: ActivityPresentation.symbol(context.state.state))
                    .foregroundStyle(ActivityPresentation.tint(context.state.state))
            } compactTrailing: {
                if let fraction = context.state.progressFraction {
                    Text("\(Int(fraction * 100))%").font(.caption.monospacedDigit())
                } else {
                    Text("\(context.state.batteryPercent)%").font(.caption.monospacedDigit())
                }
            } minimal: {
                Image(systemName: ActivityPresentation.symbol(context.state.state))
                    .foregroundStyle(ActivityPresentation.tint(context.state.state))
            }
            .widgetURL(URL(string: "mowdeck://mowers/\(context.attributes.mowerId)"))
        }
    }
}

enum ActivityPresentation {
    static func display(_ state: String) -> DisplayState {
        switch state {
        case "mowing": return .mowing
        case "paused": return .paused
        case "returning": return .returning
        case "error": return .error
        case "finished": return .docked
        default: return .unknown
        }
    }

    static func symbol(_ state: String) -> String {
        state == "finished" ? "checkmark.circle.fill" : display(state).symbolName
    }

    static func tint(_ state: String) -> Color {
        state == "finished" ? .green : display(state).tint
    }

    static func title(_ state: String) -> LocalizedStringKey {
        state == "finished" ? LocalizedStringKey("state.finished") : display(state).title
    }

    /// The final error in plain words (the server's `errorTitle`, ADR 0067), nil otherwise.
    static func errorTitle(_ state: MowingContentState) -> String? {
        guard state.state == "error", let title = state.errorTitle, !title.isEmpty else { return nil }
        return title
    }
}

/// "12 min left" as a countdown the device runs by itself.
struct RemainingTime: View {
    let state: MowingContentState

    var body: some View {
        if !state.isFinal, let end = state.endsAtDate, end > Date() {
            Text(timerInterval: Date()...end, countsDown: true, showsHours: false)
                .monospacedDigit().multilineTextAlignment(.trailing)
        } else if state.state == "finished" {
            Text("Done")
        }
    }
}

struct LockScreenActivityView: View {
    let attributes: MowingActivityAttributes
    let state: MowingContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label {
                    Text(attributes.mowerName).lineLimit(1)
                } icon: {
                    Image(systemName: ActivityPresentation.symbol(state.state))
                }
                .font(.headline)
                Spacer()
                Text(ActivityPresentation.title(state.state))
                    .font(.subheadline)
                    .foregroundStyle(ActivityPresentation.tint(state.state))
            }
            if let error = ActivityPresentation.errorTitle(state) {
                Text(error)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(ActivityPresentation.tint(state.state))
                    .lineLimit(2)
            } else if let fraction = state.progressFraction {
                ProgressView(value: fraction).tint(ActivityPresentation.tint(state.state))
            }
            HStack {
                if let percent = state.progressPercent { Text("\(percent) %").monospacedDigit() }
                if let area = state.areaName { Text(area).lineLimit(1) }
                Spacer()
                RemainingTime(state: state)
                Label("\(state.batteryPercent) %", systemImage: "battery.75")
                    .labelStyle(.titleAndIcon)
            }
            .font(.caption)
        }
        .padding(14)
        .accessibilityElement(children: .combine)
    }
}

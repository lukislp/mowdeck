import SwiftUI
import WidgetKit

@main
struct MowDeckWidgetsBundle: WidgetBundle {
    var body: some Widget {
        MowDeckStatusWidget()
        MowDeckLockScreenWidget()
        MowDeckMapWidget()
        MowDeckScheduleWidget()
        MowingLiveActivityWidget()
    }
}

struct MowDeckStatusWidget: Widget {
    let kind = "app.mowdeck.widget.status"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind, intent: SelectMowerIntent.self, provider: MowerProvider()) {
            entry in
            StatusWidgetView(entry: entry).containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Mower status")
        .description("State, battery and progress. The medium size has Start, Pause and Station.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct MowDeckLockScreenWidget: Widget {
    let kind = "app.mowdeck.widget.lock"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind, intent: SelectMowerIntent.self, provider: MowerProvider()) {
            entry in
            LockScreenWidgetView(entry: entry).containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Mower on the lock screen")
        .description("Battery and state, progress, or the next run.")
        .supportedFamilies([.accessoryCircular, .accessoryRectangular, .accessoryInline])
    }
}

struct MowDeckMapWidget: Widget {
    let kind = "app.mowdeck.widget.map"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(
            kind: kind, intent: SelectMowerIntent.self, provider: MowerProvider(withMap: true)
        ) { entry in
            MapWidgetView(entry: entry).containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Map")
        .description("The mower and the mowed path on the map, with progress and state.")
        .supportedFamilies([.systemLarge])
    }
}

struct MowDeckScheduleWidget: Widget {
    let kind = "app.mowdeck.widget.schedule"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind, intent: SelectMowerIntent.self, provider: MowerProvider()) {
            entry in
            ScheduleWidgetView(entry: entry).containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Schedule and weather")
        .description("The next runs and whether the rain pause lets them start.")
        .supportedFamilies([.systemMedium])
    }
}

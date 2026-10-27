// swift-tools-version: 5.9
import PackageDescription

// The logic of the MowDeck widgets and the Live Activity (ADR 0059) as a package, so it can be
// tested on a Mac without a simulator: `swift test --package-path apps/mobile/ios/MowerWidgetKit`.
// The targets of App.xcodeproj (App and MowDeckWidgets) link the library product
// MowerWidgetCore as a local package. Nothing in here imports SwiftUI or WidgetKit (ActivityKit only behind canImport).
let package = Package(
    name: "MowerWidgetKit",
    platforms: [.iOS(.v15), .macOS(.v12)],
    products: [
        .library(name: "MowerWidgetCore", targets: ["MowerWidgetCore"])
    ],
    targets: [
        .target(name: "MowerWidgetCore"),
        .testTarget(name: "MowerWidgetCoreTests", dependencies: ["MowerWidgetCore"]),
    ]
)

// swift-tools-version: 5.9
import PackageDescription

// The decisions of the Notification Service Extension (PushImage.swift) as a package, so they can
// be tested on a Mac without a simulator: `swift test --package-path apps/mobile/ios/NotificationServiceKit`.
// The extension target of App.xcodeproj compiles Sources/PushImage/PushImage.swift directly.
let package = Package(
    name: "NotificationServiceKit",
    platforms: [.iOS(.v15), .macOS(.v12)],
    targets: [
        .target(name: "PushImage"),
        .testTarget(name: "PushImageTests", dependencies: ["PushImage"]),
    ]
)

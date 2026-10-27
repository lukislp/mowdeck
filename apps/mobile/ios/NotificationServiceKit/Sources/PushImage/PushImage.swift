import Foundation

/// Decisions of the notification image download, kept free of UserNotifications and networking
/// so `swift test` can run them (ADR 0053). The extension target compiles this file directly.
enum PushImage {
    /// The extension may run for about 30 s; the photo is allowed this long, so a theft alarm
    /// is shown without it rather than late.
    static let waitSeconds: TimeInterval = 14
    /// Pause between two asks while the server's photo is not ready yet (HTTP 404).
    static let retryDelaySeconds: TimeInterval = 1
    /// Timeout of one request.
    static let requestTimeoutSeconds: TimeInterval = 6
    /// APNs attachments are limited to 10 MB for images; the server sends far less.
    static let maxBytes = 10 * 1024 * 1024
    /// The path of the owner's server that serves push images.
    static let path = "/api/v1/notifications/push-images/"

    enum Decision: Equatable {
        /// Attach the downloaded bytes as a file with this extension.
        case attach(fileExtension: String)
        /// The photo is not ready or the server is busy: ask again.
        case retry
        /// Show the plain text notification.
        case giveUp
    }

    /// The photo URL of a push payload: an `https` URL of the owner's server (never Apple's),
    /// pointing at the push image path. Anything else yields nil and the text-only notification.
    static func url(from userInfo: [AnyHashable: Any]) -> URL? {
        guard let text = userInfo["image_url"] as? String, text.count <= 2048,
            let url = URL(string: text), url.scheme?.lowercased() == "https",
            let host = url.host, !host.isEmpty, url.user == nil, url.password == nil,
            url.path.contains(path)
        else { return nil }
        return url
    }

    /// The file extension for the answer's media type, nil for anything but JPEG and PNG.
    static func fileExtension(forMediaType mediaType: String?) -> String? {
        let base = mediaType?.split(separator: ";").first?
            .trimmingCharacters(in: .whitespaces).lowercased()
        switch base {
        case "image/jpeg": return "jpg"
        case "image/png": return "png"
        default: return nil
        }
    }

    /// What to do with one answer of the server.
    static func decide(statusCode: Int, mediaType: String?, byteCount: Int) -> Decision {
        switch statusCode {
        case 200:
            guard byteCount > 0, byteCount <= maxBytes,
                let ext = fileExtension(forMediaType: mediaType)
            else { return .giveUp }
            return .attach(fileExtension: ext)
        case 404, 429, 500...599:
            return .retry
        default:
            // 410: expired link or no photo was taken; anything else: not for us.
            return .giveUp
        }
    }

    /// True while another ask may start after the pause (the last one then ends within
    /// `waitSeconds + requestTimeoutSeconds`, about 20 s, inside the extension's 30 s).
    static func mayRetry(elapsed: TimeInterval) -> Bool {
        elapsed + retryDelaySeconds < waitSeconds
    }
}

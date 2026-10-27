import Foundation
import UserNotifications

/// Adds the camera photo to a MowDeck error or theft notification (ADR 0053).
///
/// The push payload carries `mutable-content: 1` and `image_url`, a short-lived signed URL of the
/// owner's own server; the photo is never sent through Apple. The server sends the push at once and
/// takes the photo in parallel, so the URL answers 404 until the photo exists: this extension asks
/// again every second for at most `PushImage.waitSeconds`, then shows the plain text notification.
/// Any failure (no URL, offline, expired link, no photo) ends the same way: the text as it is.
final class NotificationService: UNNotificationServiceExtension {
    private let lock = NSLock()
    private var contentHandler: ((UNNotificationContent) -> Void)?
    private var content: UNMutableNotificationContent?
    private var download: Task<Void, Never>?

    override func didReceive(
        _ request: UNNotificationRequest,
        withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
    ) {
        guard let mutable = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content)
            return
        }
        lock.lock()
        self.contentHandler = contentHandler
        self.content = mutable
        lock.unlock()

        guard let url = PushImage.url(from: mutable.userInfo) else {
            finish(with: nil)
            return
        }
        download = Task { [weak self] in
            let attachment = await NotificationService.fetch(url)
            self?.finish(with: attachment)
        }
    }

    /// iOS is about to end the extension: show what there is.
    override func serviceExtensionTimeWillExpire() {
        download?.cancel()
        finish(with: nil)
    }

    private func finish(with attachment: UNNotificationAttachment?) {
        lock.lock()
        let handler = contentHandler
        let result = content
        contentHandler = nil
        lock.unlock()
        guard let handler, let result else { return }
        if let attachment { result.attachments = [attachment] }
        handler(result)
    }

    /// Downloads the photo, asking again while the server says "not ready" (404).
    private static func fetch(_ url: URL) async -> UNNotificationAttachment? {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = PushImage.requestTimeoutSeconds
        let session = URLSession(configuration: configuration)
        defer { session.finishTasksAndInvalidate() }
        let started = Date()
        while !Task.isCancelled {
            var request = URLRequest(url: url, timeoutInterval: PushImage.requestTimeoutSeconds)
            request.httpMethod = "GET"
            do {
                let (data, response) = try await session.data(for: request)
                if let http = response as? HTTPURLResponse {
                    switch PushImage.decide(
                        statusCode: http.statusCode,
                        mediaType: http.value(forHTTPHeaderField: "Content-Type"),
                        byteCount: data.count
                    ) {
                    case .attach(let fileExtension):
                        return attachment(data, fileExtension: fileExtension)
                    case .giveUp:
                        return nil
                    case .retry:
                        break
                    }
                }
            } catch {
                // Offline or timed out: try again while the budget lasts.
            }
            guard PushImage.mayRetry(elapsed: Date().timeIntervalSince(started)) else { return nil }
            try? await Task.sleep(nanoseconds: UInt64(PushImage.retryDelaySeconds * 1_000_000_000))
        }
        return nil
    }

    private static func attachment(_ data: Data, fileExtension: String) -> UNNotificationAttachment? {
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension(fileExtension)
        do {
            try data.write(to: file, options: .atomic)
            return try UNNotificationAttachment(identifier: "photo", url: file, options: nil)
        } catch {
            return nil
        }
    }
}

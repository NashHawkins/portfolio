import Foundation

/// One finished upload, kept so its link can be copied again later.
struct UploadRecord: Codable, Identifiable, Equatable {
    var id: String { key }
    /// Path inside the bucket, e.g. lipsync/2026-11-13/<uuid>.mp4
    let key: String
    /// The public link the Trial Reels publisher takes as --video-url.
    let publicURL: URL
    let bytes: Int64
    let date: Date
    /// True once the public link answered with the same size and type `video/mp4`.
    let verified: Bool
}

enum UploadPhase: Equatable {
    case idle
    case preparing
    case uploading(Double)
    case verifying
    case done(UploadRecord)
    case failed(String)

    var isBusy: Bool {
        switch self {
        case .preparing, .uploading, .verifying: true
        case .idle, .done, .failed: false
        }
    }
}

enum UploadError: LocalizedError {
    case notConfigured
    case worker(String)
    case unexpected(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured: "Set the Worker address and upload token first (the gear button)."
        case .worker(let message): message
        case .unexpected(let message): message
        }
    }
}

/// Uploads finished videos to R2, one at a time.
///
/// 1. Asks the Worker for a presigned PUT link (needs the app to be open).
/// 2. Hands the file to an iOS *background* session, so the upload carries on
///    if you switch apps or lock the phone.
/// 3. Checks the public link serves the same number of bytes as `video/mp4`,
///    which is what the Trial Reels publisher checks before giving it to Meta.
///
/// @unchecked Sendable: @Published properties are only set on the main thread;
/// `responseBodies` and `finishing` are only touched on the session's serial delegate queue.
final class R2Uploader: NSObject, ObservableObject, @unchecked Sendable {
    static let shared = R2Uploader()

    private static let sessionID = "LipSync.R2Upload"
    private static let recentKey = "R2Uploader.recent"
    private static let maxRecent = 30

    @Published private(set) var phase: UploadPhase = .idle
    /// Newest first.
    @Published private(set) var recent: [UploadRecord] = []
    /// Which review screen started the current upload, so another take doesn't show its progress.
    @Published private(set) var ownerID: UUID?

    /// Given by iOS when it relaunches the app to finish an upload; called once the events are handled.
    var backgroundCompletionHandler: (() -> Void)?

    private var session: URLSession!
    private let delegateQueue: OperationQueue = {
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        return queue
    }()
    private var responseBodies: [Int: Data] = [:]
    /// The link check for a just-finished upload; awaited before telling iOS the background work is done.
    private var finishing: Task<Void, Never>?

    private override init() {
        super.init()
        recent = Self.loadRecent()
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        config.isDiscretionary = false
        config.sessionSendsLaunchEvents = true
        session = URLSession(configuration: config, delegate: self, delegateQueue: delegateQueue)

        // An upload may still be running from before the app was closed.
        session.getAllTasks { tasks in
            let running = tasks.first { $0.state == .running }
            DispatchQueue.main.async {
                if running != nil {
                    self.phase = .uploading(running.map(Self.progress(of:)) ?? 0)
                } else {
                    Self.removeOrphanedFiles()
                }
            }
        }
    }

    /// Starts uploading a copy of `source`. The caller may delete `source` straight after.
    @MainActor
    func upload(fileAt source: URL, owner: UUID) async {
        guard !phase.isBusy else { return }
        ownerID = owner
        phase = .preparing
        do {
            guard let settings = UploadSettings.current(), settings.isComplete, let endpoint = settings.endpoint
            else { throw UploadError.notConfigured }

            let bytes = try Self.fileSize(of: source)
            let ticket = try await requestTicket(from: endpoint, token: settings.trimmedToken, bytes: bytes)

            // The background session reads the file after this screen may be gone, so it gets its own copy
            // (an instant clone on APFS).
            let pendingFile = try Self.pendingDirectory().appendingPathComponent("\(UUID().uuidString).mp4")
            try FileManager.default.copyItem(at: source, to: pendingFile)

            var request = URLRequest(url: ticket.uploadURL)
            request.httpMethod = "PUT"
            request.setValue(ticket.contentType, forHTTPHeaderField: "Content-Type") // part of the signature
            let task = session.uploadTask(with: request, fromFile: pendingFile)
            let pending = Pending(key: ticket.key, publicURL: ticket.publicURL,
                                  fileName: pendingFile.lastPathComponent, bytes: bytes)
            task.taskDescription = String(decoding: try JSONEncoder().encode(pending), as: UTF8.self)
            phase = .uploading(0)
            task.resume()
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    /// Forgets a finished or failed upload so the next take starts clean.
    @MainActor
    func clearFinished() {
        guard !phase.isBusy else { return }
        phase = .idle
        ownerID = nil
    }

    @MainActor
    func removeRecord(_ record: UploadRecord) {
        recent.removeAll { $0.key == record.key }
        saveRecent()
    }

    // MARK: - Worker

    private struct Ticket: Decodable {
        let uploadURL: URL
        let key: String
        let publicURL: URL
        let contentType: String
    }

    private struct WorkerError: Decodable {
        let error: String
    }

    private func requestTicket(from endpoint: URL, token: String, bytes: Int64) async throws -> Ticket {
        var request = URLRequest(url: endpoint, timeoutInterval: 30)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["bytes": bytes])

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw UploadError.worker("Couldn't reach the upload Worker: \(error.localizedDescription)")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else {
            let reason = (try? JSONDecoder().decode(WorkerError.self, from: data))?.error ?? "HTTP \(status)"
            throw UploadError.worker("The upload Worker said no: \(reason)")
        }
        do {
            return try JSONDecoder().decode(Ticket.self, from: data)
        } catch {
            throw UploadError.worker("The upload Worker sent an answer the app doesn't understand.")
        }
    }

    // MARK: - Finishing

    private struct Pending: Codable {
        let key: String
        let publicURL: URL
        let fileName: String
        let bytes: Int64
    }

    /// Runs after R2 accepted the file: confirms the public link, then records it.
    private func finish(_ pending: Pending) async {
        await MainActor.run { phase = .verifying }
        let problem = await Self.checkPublicLink(pending.publicURL, expectedBytes: pending.bytes)
        let record = UploadRecord(key: pending.key, publicURL: pending.publicURL, bytes: pending.bytes,
                                  date: Date(), verified: problem == nil)
        await MainActor.run {
            // Keep the link either way, so a video that did arrive is never lost.
            recent.insert(record, at: 0)
            if recent.count > Self.maxRecent { recent.removeLast(recent.count - Self.maxRecent) }
            saveRecent()
            if let problem {
                phase = .failed("The video reached R2, but its public link didn't check out: \(problem) "
                                + "The link is still listed under Recent uploads.")
            } else {
                phase = .done(record)
            }
        }
    }

    /// Nil if the link serves `expectedBytes` bytes as video/mp4, otherwise what's wrong.
    private static func checkPublicLink(_ url: URL, expectedBytes: Int64) async -> String? {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 30)
        request.httpMethod = "HEAD"
        let session = URLSession(configuration: .ephemeral)
        defer { session.finishTasksAndInvalidate() }
        guard let (_, response) = try? await session.data(for: request),
              let http = response as? HTTPURLResponse
        else { return "it couldn't be reached." }
        guard http.statusCode == 200 else {
            return "it answered HTTP \(http.statusCode). Is public access (r2.dev) on for this bucket, "
                + "and is PUBLIC_BASE_URL in the Worker that bucket's address?"
        }
        let type = (http.value(forHTTPHeaderField: "Content-Type") ?? "")
            .split(separator: ";").first.map { $0.trimmingCharacters(in: .whitespaces).lowercased() } ?? ""
        guard type == "video/mp4" else { return "it is served as \(type.isEmpty ? "no type" : type), not video/mp4." }
        let length = Int64(http.value(forHTTPHeaderField: "Content-Length") ?? "") ?? -1
        guard length == expectedBytes else { return "it is \(length) bytes, but \(expectedBytes) were sent." }
        return nil
    }

    // MARK: - Files

    private static func pendingDirectory() throws -> URL {
        var dir = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                              appropriateFor: nil, create: true)
            .appendingPathComponent("PendingUploads", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? dir.setResourceValues(values)
        return dir
    }

    /// Deletes copies left behind if the app was closed mid-upload. Only call when no upload is running.
    private static func removeOrphanedFiles() {
        guard let dir = try? pendingDirectory(),
              let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
        else { return }
        files.forEach { try? FileManager.default.removeItem(at: $0) }
    }

    private static func fileSize(of url: URL) throws -> Int64 {
        let size = try FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber
        guard let bytes = size?.int64Value, bytes > 0 else { throw UploadError.unexpected("The exported video is empty.") }
        return bytes
    }

    private static func progress(of task: URLSessionTask) -> Double {
        let total = task.countOfBytesExpectedToSend
        return total > 0 ? Double(task.countOfBytesSent) / Double(total) : 0
    }

    /// Pulls <Message> out of an R2 (S3-style) XML error body.
    private static func r2Message(_ body: Data?) -> String? {
        guard let body, let text = String(data: body, encoding: .utf8),
              let start = text.range(of: "<Message>"),
              let end = text.range(of: "</Message>", range: start.upperBound..<text.endIndex)
        else { return nil }
        return String(text[start.upperBound..<end.lowerBound])
    }

    // MARK: - Recent uploads

    private static func loadRecent() -> [UploadRecord] {
        guard let data = UserDefaults.standard.data(forKey: recentKey) else { return [] }
        return (try? JSONDecoder().decode([UploadRecord].self, from: data)) ?? []
    }

    @MainActor
    private func saveRecent() {
        if let data = try? JSONEncoder().encode(recent) {
            UserDefaults.standard.set(data, forKey: Self.recentKey)
        }
    }
}

// MARK: - URLSession delegate (runs on the serial delegate queue)

extension R2Uploader: URLSessionDataDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        let fraction = Self.progress(of: task)
        DispatchQueue.main.async { self.phase = .uploading(fraction) }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        responseBodies[dataTask.taskIdentifier, default: Data()].append(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let body = responseBodies.removeValue(forKey: task.taskIdentifier)
        guard let description = task.taskDescription,
              let pending = try? JSONDecoder().decode(Pending.self, from: Data(description.utf8))
        else { return }

        if let dir = try? Self.pendingDirectory() {
            try? FileManager.default.removeItem(at: dir.appendingPathComponent(pending.fileName))
        }

        let status = (task.response as? HTTPURLResponse)?.statusCode
        if let error {
            let message = "The upload didn't finish: \(error.localizedDescription)"
            DispatchQueue.main.async { self.phase = .failed(message) }
        } else if status != 200 {
            let detail = Self.r2Message(body).map { " (\($0))" } ?? ""
            let message = "R2 refused the upload: HTTP \(status ?? 0)\(detail)."
            DispatchQueue.main.async { self.phase = .failed(message) }
        } else {
            finishing = Task { await self.finish(pending) }
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        let finishing = finishing
        Task {
            await finishing?.value // let the link get recorded before iOS suspends the app again
            await MainActor.run {
                self.backgroundCompletionHandler?()
                self.backgroundCompletionHandler = nil
            }
        }
    }
}

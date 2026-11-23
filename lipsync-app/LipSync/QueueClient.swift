import Foundation

/// One uploaded take waiting in R2 for the next morning's Trial Reels batch.
struct QueuedVideo: Decodable, Identifiable, Hashable {
    var id: String { key }
    /// lipsync/2026-11-13/<uuid>.mp4
    let key: String
    let size: Int64
    let uploaded: Date
    let publicURL: URL
}

struct DayListing: Decodable {
    /// The Denver calendar day the Worker resolved, e.g. 2026-11-13.
    let date: String
    /// Oldest upload first, the order the batch posts them in.
    let videos: [QueuedVideo]
}

enum QueueDay: String, CaseIterable, Identifiable {
    case today, yesterday
    var id: String { rawValue }
    var title: String { self == .today ? "Today" : "Yesterday" }
}

/// Lists and deletes uploads through the Worker, with the same address and token as uploading.
/// The Worker works out what "today" means (Denver), so the app and the batch always agree.
enum QueueClient {
    private struct WorkerError: Decodable { let error: String }

    static func list(_ day: QueueDay) async throws -> DayListing {
        let request = try makeRequest(path: "/list", query: [URLQueryItem(name: "date", value: day.rawValue)],
                                      method: "GET")
        let data = try await send(request, doing: "list the uploads")
        do {
            return try decoder.decode(DayListing.self, from: data)
        } catch {
            throw UploadError.worker("The upload Worker sent a list the app doesn't understand.")
        }
    }

    static func delete(_ video: QueuedVideo) async throws {
        let request = try makeRequest(path: "/video", query: [URLQueryItem(name: "key", value: video.key)],
                                      method: "DELETE")
        _ = try await send(request, doing: "delete the video")
    }

    // MARK: - Plumbing

    private static func makeRequest(path: String, query: [URLQueryItem], method: String) throws -> URLRequest {
        guard let settings = UploadSettings.current(), settings.isComplete,
              let endpoint = settings.endpoint,
              var parts = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)
        else { throw UploadError.notConfigured }
        parts.path = path
        parts.queryItems = query
        guard let url = parts.url else { throw UploadError.notConfigured }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 30)
        request.httpMethod = method
        request.setValue("Bearer \(settings.trimmedToken)", forHTTPHeaderField: "Authorization")
        return request
    }

    private static func send(_ request: URLRequest, doing what: String) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw UploadError.worker("Couldn't reach the upload Worker to \(what): \(error.localizedDescription)")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else {
            let reason = (try? JSONDecoder().decode(WorkerError.self, from: data))?.error ?? "HTTP \(status)"
            throw UploadError.worker("Couldn't \(what): \(reason)")
        }
        return data
    }

    /// R2 dates look like 2026-11-13T20:11:42.288Z (fractional seconds, which .iso8601 rejects).
    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        decoder.dateDecodingStrategy = .custom { decoder in
            let text = try decoder.singleValueContainer().decode(String.self)
            if let date = withFraction.date(from: text) ?? plain.date(from: text) { return date }
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath,
                                                    debugDescription: "Bad date \(text)"))
        }
        return decoder
    }()
}

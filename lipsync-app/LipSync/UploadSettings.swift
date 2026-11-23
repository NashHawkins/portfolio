import Foundation
import Security

/// Where to ask for R2 upload links, and the password for it.
///
/// Kept in the Keychain rather than in the code, so the token never lands in
/// git and can be changed on the phone without rebuilding the app.
struct UploadSettings: Codable, Equatable {
    /// The Worker's address, e.g. https://lipsync-upload.example.workers.dev
    var workerURL: String
    /// Must match the Worker's UPLOAD_TOKEN secret.
    var token: String

    /// The Worker's /upload-url endpoint, or nil if `workerURL` isn't a usable https address.
    var endpoint: URL? {
        let trimmed = workerURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: trimmed),
              parts.scheme?.lowercased() == "https",
              let host = parts.host, !host.isEmpty
        else { return nil }
        parts.path = "/upload-url"
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }

    var trimmedToken: String { token.trimmingCharacters(in: .whitespacesAndNewlines) }

    var isComplete: Bool { endpoint != nil && !trimmedToken.isEmpty }

    // MARK: - Where the settings come from

    /// What uploads use: anything typed into the gear screen wins, otherwise
    /// the values built into the app from UploadConfig.json.
    static func current() -> UploadSettings? {
        if let saved = load(), saved.isComplete { return saved }
        return builtIn()
    }

    /// LipSync/UploadConfig.json, copied into the app at build time. It is git-ignored,
    /// so the token never reaches GitHub; a fresh clone without it simply has no built-in settings.
    static func builtIn() -> UploadSettings? {
        guard let url = Bundle.main.url(forResource: "UploadConfig", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let settings = try? JSONDecoder().decode(UploadSettings.self, from: data),
              settings.isComplete
        else { return nil }
        return settings
    }

    /// Forgets anything typed into the gear screen, going back to the built-in values.
    static func clearSaved() {
        SecItemDelete(baseQuery as CFDictionary)
    }

    // MARK: - Keychain

    private static let service = "LipSync.R2Upload"
    private static let account = "settings"

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    static func load() -> UploadSettings? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data
        else { return nil }
        return try? JSONDecoder().decode(UploadSettings.self, from: data)
    }

    func save() throws {
        let data = try JSONEncoder().encode(self)
        SecItemDelete(Self.baseQuery as CFDictionary)
        var item = Self.baseQuery
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status),
                          userInfo: [NSLocalizedDescriptionKey: "Couldn't save the upload settings (Keychain error \(status))."])
        }
    }
}

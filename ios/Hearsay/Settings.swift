import Foundation
import Security

/// Where uploads go: the capture receiver's URL (install/nas.sh prints it)
/// and its bearer token. Entered in the app, never built in, so the app isn't
/// tied to one Hearsay install. The token lives in the Keychain, readable
/// while the phone is locked (uploads run in the background) and never
/// included in backups.
enum Settings {
    private static let urlKey = "captureURL"
    private static let tokenQuery: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "hearsay",
        kSecAttrAccount as String: "capture-token",
    ]

    static var url: URL? {
        UserDefaults.standard.string(forKey: urlKey).flatMap(URL.init(string:))
    }

    static var urlText: String {
        UserDefaults.standard.string(forKey: urlKey) ?? ""
    }

    static var token: String? {
        var query = tokenQuery
        query[kSecReturnData as String] = true
        var found: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &found) == errSecSuccess,
              let data = found as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func save(url: String, token: String) {
        UserDefaults.standard.set(url.trimmingCharacters(in: .whitespacesAndNewlines), forKey: urlKey)
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        // An empty field keeps the saved token, so the URL can change alone.
        guard !token.isEmpty else { return }
        SecItemDelete(tokenQuery as CFDictionary)
        var item = tokenQuery
        item[kSecValueData as String] = Data(token.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        if status != errSecSuccess {
            log.error("saving the token failed: \(status)")
        }
    }
}

import Security
import SwiftUI

enum OutputLanguage {
    static func name(_ code: String) -> String {
        Locale.current.localizedString(forLanguageCode: code) ?? code
    }
    static func selected(_ preferred: String, supported: [String]) -> String {
        supported.contains(preferred) ? preferred : supported.first ?? "en"
    }
    private static func key(_ name: String) -> [String: Any] {
        var key = Connection.vault
        key[kSecAttrAccount as String] = name
        return key
    }
    static func read() -> String { read("output-language") ?? "en" }
    static func readQuality() -> String { read("voice-quality") ?? "best" }
    private static func read(_ name: String) -> String? {
        var query = key(name)
        query[kSecReturnData as String] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return nil }
        return String(decoding: data, as: UTF8.self)
    }
    static func save(_ code: String) { save(code, as: "output-language") }
    static func saveQuality(_ code: String) { save(code, as: "voice-quality") }
    private static func save(_ code: String, as name: String) {
        let key = key(name)
        let data = Data(code.utf8)
        let update = [kSecValueData as String: data] as CFDictionary
        if SecItemUpdate(key as CFDictionary, update) == errSecItemNotFound {
            var new = key
            new[kSecValueData as String] = data
            new[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            SecItemAdd(new as CFDictionary, nil)
        }
    }
}

struct OutputLanguagePicker: View {
    let supported: [String]
    @Binding var preference: String
    var body: some View {
        Picker("Dub language", selection: Binding(get: {
            OutputLanguage.selected(preference, supported: supported)
        }, set: { code in
            preference = code
            OutputLanguage.save(code)
        })) {
            ForEach(supported, id: \.self) { code in
                Text(OutputLanguage.name(code)).tag(code)
            }
        }.pickerStyle(.menu).labelsHidden().accessibilityIdentifier("dub-language")
    }
}

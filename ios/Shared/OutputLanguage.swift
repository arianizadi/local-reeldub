import Security
import SwiftUI

enum OutputLanguage {
    static func name(_ code: String) -> String {
        Locale.current.localizedString(forLanguageCode: code) ?? code
    }
    static func selected(_ preferred: String, supported: [String]) -> String {
        supported.contains(preferred) ? preferred : supported.first ?? "en"
    }
    private static var key: [String: Any] {
        var key = Connection.vault
        key[kSecAttrAccount as String] = "output-language"
        return key
    }
    static func read() -> String {
        var query = key
        query[kSecReturnData as String] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return "en" }
        return String(decoding: data, as: UTF8.self)
    }
    static func save(_ code: String) {
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
                Text(OutputLanguage.name(code) + (code == "hi" ? " · preset voice" : "")).tag(code)
            }
        }.pickerStyle(.menu).accessibilityIdentifier("dub-language")
    }
}

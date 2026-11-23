import SwiftUI

enum InterfaceLanguage: String, CaseIterable {
    case system
    case chinese = "zh"
    case english = "en"

    var resolved: Self {
        guard self == .system else { return self }
        for identifier in Locale.preferredLanguages {
            switch Locale.Language(identifier: identifier).languageCode?.identifier {
            case "zh": return .chinese
            case "en": return .english
            default: continue
            }
        }
        return .english
    }

    var locale: Locale { Locale(identifier: self == .english ? "en" : "zh-Hans") }

    func text(_ chinese: String, _ english: String) -> String {
        self == .english ? english : chinese
    }
}

private struct InterfaceLanguageKey: EnvironmentKey {
    static let defaultValue = InterfaceLanguage.chinese
}

extension EnvironmentValues {
    var interfaceLanguage: InterfaceLanguage {
        get { self[InterfaceLanguageKey.self] }
        set { self[InterfaceLanguageKey.self] = newValue }
    }
}

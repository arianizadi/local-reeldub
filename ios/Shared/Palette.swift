import SwiftUI

enum Palette {
    static let ink = Color(red: 0.094, green: 0.078, blue: 0.074)
    static let paper = Color(red: 0.976, green: 0.968, blue: 0.950)
    static let leaf = Color(red: 0.75, green: 0.87, blue: 0.64)
    static let action = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 0.75, green: 0.87, blue: 0.64, alpha: 1)
            : UIColor(red: 0.14, green: 0.35, blue: 0.19, alpha: 1)
    })
    static let onAction = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? .black : .white })
    static let canvas = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(white: 0.075, alpha: 1) : UIColor(red: 0.976, green: 0.968, blue: 0.950, alpha: 1) })
    static let surface = Color(uiColor: .secondarySystemGroupedBackground)
}

struct DubValueRow: View {
    let title: String
    let value: String
    @Environment(\.dynamicTypeSize) private var typeSize
    init(_ title: String, value: String) { self.title = title; self.value = value }

    var body: some View {
        let layout = typeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: 3))
            : AnyLayout(HStackLayout(alignment: .firstTextBaseline, spacing: 12))
        layout {
            Text(title).frame(maxWidth: .infinity, alignment: .leading)
            Text(value).foregroundStyle(.secondary)
        }
        .font(.body)
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
    }
}

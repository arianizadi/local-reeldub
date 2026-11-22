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

/// A pair of audio tracks and a playhead for work whose duration is not known.
struct DubLoadingIndicator: View {
    let title: String
    var compact: Bool = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase

    init(_ title: String, compact: Bool = false) {
        self.title = title
        self.compact = compact
    }

    var body: some View {
        let layout = compact
            ? AnyLayout(HStackLayout(alignment: .center, spacing: 12))
            : AnyLayout(VStackLayout(alignment: .center, spacing: 12))
        layout {
            tracks.frame(width: compact ? 66 : 160, height: compact ? 30 : 72)
                .accessibilityHidden(true)
            Text(title).font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }

    private var tracks: some View {
        Canvas { context, size in
            let scale = size.width / 160
            context.scaleBy(x: scale, y: scale)
            let frame = CGRect(x: 1, y: 1, width: 158, height: 70)
            let plate = Path(roundedRect: frame, cornerRadius: 13)
            context.fill(plate, with: .color(.primary.opacity(0.045)))
            context.stroke(plate, with: .color(.primary.opacity(0.18)), lineWidth: 1)
            let heights: [CGFloat] = [3, 5, 9, 15, 20, 11, 6, 4, 7, 13, 19, 23, 15, 8, 4, 6, 12, 16, 9, 4, 7, 12, 6, 3]
            for (index, height) in heights.enumerated() {
                let x = 15 + CGFloat(index) * 5.5
                let bar = CGRect(x: x, y: 27 - height / 2, width: 2.6, height: height)
                context.fill(Path(roundedRect: bar, cornerRadius: 1.3), with: .color(Palette.action))
            }
            // Segments of the second track echo the caption cues beneath an audio waveform.
            for (start, width) in [(15, 22), (41, 35), (83, 13), (100, 26), (130, 14)] {
                let cue = CGRect(x: start, y: 47, width: width, height: 5)
                context.fill(Path(roundedRect: cue, cornerRadius: 2.5), with: .color(.primary.opacity(0.38)))
            }
        }
        .overlay {
            GeometryReader { geometry in
                let scale = geometry.size.width / 160
                let cursor = Capsule().fill(.primary)
                    .frame(width: max(1, 1.5 * scale), height: 46 * scale)
                    .overlay(alignment: .top) {
                        Circle().fill(.primary).frame(width: 5 * scale, height: 5 * scale)
                    }
                if reduceMotion || scenePhase != .active {
                    cursor.position(x: 80 * scale, y: 35 * scale)
                } else {
                    cursor.phaseAnimator([false, true]) { view, forward in
                        view.position(x: (forward ? 143 : 16) * scale, y: 35 * scale)
                    } animation: { _ in .easeInOut(duration: 2.2) }
                }
            }
        }
    }
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

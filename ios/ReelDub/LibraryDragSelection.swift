import SwiftUI
import UIKit

/// The list keeps its native scrolling gesture unless a vertical drag starts on a selectable row.
@MainActor
final class LibraryDragSelection: NSObject, UIGestureRecognizerDelegate {
    typealias Update = (String) -> Void
    private let rows = NSHashTable<Row>.weakObjects()
    private weak var list: UIScrollView?
    private var pan: RangePan?
    private var displayLink: CADisplayLink?
    private var update: Update?
    private var startID: String?
    private var lastID: String?

    private final class RangePan: UIPanGestureRecognizer {
        var origin: CGPoint = .zero
        override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
            if let touch = touches.first { origin = touch.location(in: view) }
            super.touchesBegan(touches, with: event)
        }
    }

    final class Row: UIView {
        weak var selection: LibraryDragSelection?
        var id = ""
        var begin: ((String) -> Update?)?

        override func didMoveToWindow() {
            super.didMoveToWindow()
            if window != nil { selection?.attach(self) }
        }
    }

    func attach(_ row: Row) {
        rows.add(row)
        var ancestor = row.superview
        while let view = ancestor, !(view is UIScrollView) { ancestor = view.superview }
        guard let scroll = ancestor as? UIScrollView, scroll !== list else { return }
        detach()
        list = scroll
        let recognizer = RangePan(target: self, action: #selector(handle(_:)))
        recognizer.minimumNumberOfTouches = 1
        recognizer.maximumNumberOfTouches = 1
        recognizer.delegate = self
        scroll.addGestureRecognizer(recognizer)
        pan = recognizer
    }

    func detach() {
        finish()
        if let pan { list?.removeGestureRecognizer(pan) }
        pan = nil
        list = nil
    }

    private func finish() {
        displayLink?.invalidate()
        displayLink = nil
        update = nil
        lastID = nil
        startID = nil
    }

    private func row(at y: CGFloat, x: CGFloat? = nil) -> Row? {
        guard let list else { return nil }
        return rows.allObjects.first { row in
            guard row.window != nil, row.isDescendant(of: list) else { return false }
            let frame = row.convert(row.bounds, to: list)
            return frame.minY <= y && y <= frame.maxY && (x.map { frame.minX <= $0 && $0 <= frame.maxX } ?? true)
        }
    }

    func gestureRecognizerShouldBegin(_ gesture: UIGestureRecognizer) -> Bool {
        guard let pan = gesture as? RangePan, let list else { return false }
        let location = pan.location(in: list)
        guard abs(location.y - pan.origin.y) > abs(location.x - pan.origin.x),
              let first = row(at: pan.origin.y, x: pan.origin.x) else { return false }
        startID = first.id
        update = first.begin?(first.id)
        return update != nil
    }

    func gestureRecognizer(_ gesture: UIGestureRecognizer, shouldBeRequiredToFailBy other: UIGestureRecognizer) -> Bool {
        other === list?.panGestureRecognizer
    }

    @objc private func handle(_ pan: UIPanGestureRecognizer) {
        switch pan.state {
        case .began:
            if let startID { update?(startID); lastID = startID }
            track()
        case .changed: track()
        case .ended: track(); finish()
        default: finish()
        }
    }

    private func track() {
        guard let list, let pan, update != nil else { return }
        let y = min(max(pan.location(in: list).y, list.bounds.minY + 1), list.bounds.maxY - 1)
        if let id = row(at: y)?.id, id != lastID {
            update?(id)
            lastID = id
        }
        if edgeSpeed(at: y, in: list) == 0 {
            displayLink?.invalidate()
            displayLink = nil
        } else if displayLink == nil {
            let link = CADisplayLink(target: self, selector: #selector(scrollEdge(_:)))
            link.add(to: .main, forMode: .common)
            displayLink = link
        }
    }

    private func edgeSpeed(at y: CGFloat, in list: UIScrollView) -> CGFloat {
        let band: CGFloat = min(64, list.bounds.height / 4)
        guard band > 0 else { return 0 }
        let top = max(0, 1 - (y - list.bounds.minY) / band)
        let bottom = max(0, 1 - (list.bounds.maxY - y) / band)
        return 540 * (min(1, bottom) - min(1, top))
    }

    @objc private func scrollEdge(_ link: CADisplayLink) {
        guard let list, let pan else { finish(); return }
        let low = -list.adjustedContentInset.top
        let high = max(low, list.contentSize.height - list.bounds.height + list.adjustedContentInset.bottom)
        let speed = edgeSpeed(at: pan.location(in: list).y, in: list)
        let y = min(high, max(low, list.contentOffset.y + speed * (link.targetTimestamp - link.timestamp)))
        guard y != list.contentOffset.y else {
            displayLink?.invalidate()
            displayLink = nil
            return
        }
        list.setContentOffset(CGPoint(x: list.contentOffset.x, y: y), animated: false)
        list.layoutIfNeeded()
        track()
    }
}

struct LibrarySelectionRow: UIViewRepresentable {
    let id: String
    let selection: LibraryDragSelection
    let begin: (String) -> LibraryDragSelection.Update?

    func makeUIView(context: Context) -> LibraryDragSelection.Row {
        let row = LibraryDragSelection.Row()
        row.isUserInteractionEnabled = false
        row.isAccessibilityElement = false
        return row
    }
    func updateUIView(_ row: LibraryDragSelection.Row, context: Context) {
        row.id = id
        row.selection = selection
        row.begin = begin
        if row.window != nil { selection.attach(row) }
    }
}

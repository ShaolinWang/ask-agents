import AppKit
import SwiftUI

extension View {
    func compactEnclosingScrollIndicators() -> some View {
        background {
            ScrollIndicatorStyle()
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }
}

private struct ScrollIndicatorStyle: NSViewRepresentable {
    func makeNSView(context: Context) -> CompactScrollIndicatorView {
        CompactScrollIndicatorView()
    }

    func updateNSView(_ view: CompactScrollIndicatorView, context: Context) {}
}

final class CompactScrollIndicatorView: NSView {
    init() {
        super.init(frame: .zero)
        NotificationCenter.default.addObserver(self, selector: #selector(preferredScrollerStyleDidChange(_:)),
                                               name: NSScroller.preferredScrollerStyleDidChangeNotification, object: nil)
    }

    required init?(coder: NSCoder) {
        return nil
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyStyle()
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    @objc private func preferredScrollerStyleDidChange(_ notification: Notification) {
        // Apply the app style after AppKit reapplies the system preference.
        DispatchQueue.main.async { [weak self] in
            self?.applyStyle()
        }
    }

    private func applyStyle() {
        guard window != nil, let scroll = enclosingScrollView else { return }
        scroll.scrollerStyle = .overlay
        scroll.verticalScroller?.controlSize = .small
        scroll.horizontalScroller?.controlSize = .small
    }
}

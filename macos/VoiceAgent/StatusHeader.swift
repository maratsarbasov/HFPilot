import Cocoa

// A single noninteractive NSMenuItem.view. Commands below it retain AppKit's
// standard drawing, keyboard handling and menu tracking.
final class StatusDot: NSView {
    var color: NSColor = .secondaryLabelColor { didSet { needsDisplay = true } }
    override var intrinsicContentSize: NSSize { NSSize(width: 6, height: 6) }
    override func draw(_ dirtyRect: NSRect) {
        color.setFill()
        NSBezierPath(ovalIn: bounds.insetBy(dx: 0.5, dy: 0.5)).fill()
    }
}

final class StatusHeader: NSView {
    private let title = NSTextField(labelWithString: "Checking status…")
    private let subtitle = NSTextField(labelWithString: "")
    private let dot = StatusDot()

    init() {
        super.init(frame: .zero)
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.textColor = .labelColor
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        let inset: CGFloat = 8, gap: CGFloat = 3
        setFrameSize(NSSize(width: 294, height: inset * 2 + title.intrinsicContentSize.height + gap + subtitle.intrinsicContentSize.height))
        for field in [title, subtitle] { field.lineBreakMode = .byTruncatingTail }
        for view in [dot, title, subtitle] {
            view.translatesAutoresizingMaskIntoConstraints = false
            addSubview(view)
        }
        NSLayoutConstraint.activate([
            dot.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 18),
            dot.centerYAnchor.constraint(equalTo: title.centerYAnchor),
            dot.widthAnchor.constraint(equalToConstant: 6), dot.heightAnchor.constraint(equalToConstant: 6),
            title.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 32),
            title.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -16),
            title.topAnchor.constraint(equalTo: topAnchor, constant: inset),
            subtitle.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            subtitle.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            subtitle.topAnchor.constraint(equalTo: title.bottomAnchor, constant: gap),
            subtitle.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -inset)
        ])
        setAccessibilityElement(true)
        setAccessibilityRole(.staticText)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    func update(title: String, subtitle: String, color: NSColor) {
        self.title.stringValue = title
        self.subtitle.stringValue = subtitle
        self.subtitle.toolTip = subtitle
        dot.color = color
        setAccessibilityLabel(title + ". " + subtitle)
    }
}

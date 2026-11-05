import Cocoa

// Presentation only: keep the progressive transcript and saved summary untouched.
enum ConversationText {
    private static let turn = try! NSRegularExpression(pattern: #"^(?:\[([0-9.]+)[–-]([0-9.]+)\]\s*)?(caller|assistant):\s*"#, options: .caseInsensitive)

    static func document(_ source: String, transcript: Bool) -> NSAttributedString {
        let result = NSMutableAttributedString(string: "")
        func paragraph(_ text: String, heading: Bool, after spacing: CGFloat) {
            let style = NSMutableParagraphStyle()
            style.lineSpacing = 3
            style.paragraphSpacing = spacing
            let font: NSFont = heading ? .systemFont(ofSize: 13, weight: .semibold) : .systemFont(ofSize: 14)
            result.append(NSAttributedString(string: text + "\n", attributes: [.font: font, .foregroundColor: NSColor.labelColor, .paragraphStyle: style]))
        }
        func clock(_ seconds: Double) -> String {
            guard seconds.isFinite, seconds >= 0, seconds < 1_000_000 else { return "—" }
            let whole = Int(seconds)
            return String(format: "%d:%02d", whole / 60, whole % 60)
        }
        for line in source.components(separatedBy: .newlines) where !line.isEmpty {
            let ns = line as NSString
            if transcript, let match = turn.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)) {
                var heading = ns.substring(with: match.range(at: 3)).capitalized
                if match.range(at: 1).location != NSNotFound, let from = Double(ns.substring(with: match.range(at: 1))), let to = Double(ns.substring(with: match.range(at: 2))) {
                    heading += "   ·   " + clock(from) + "–" + clock(to)
                }
                paragraph(heading, heading: true, after: 3)
                paragraph(ns.substring(from: NSMaxRange(match.range)), heading: false, after: 14)
            } else {
                let heading = !transcript && ["Result", "Next steps", "Unresolved"].contains(line)
                paragraph(line, heading: heading, after: heading ? 4 : 9)
            }
        }
        if result.length > 0 { result.deleteCharacters(in: NSRange(location: result.length - 1, length: 1)) }
        return result
    }
}

final class InstructionEditor: NSTextView {
    weak var surround: NSBox?
    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted { surround?.borderColor = .keyboardFocusIndicatorColor; surround?.borderWidth = 2 }
        return accepted
    }
    override func resignFirstResponder() -> Bool {
        let accepted = super.resignFirstResponder()
        if accepted { surround?.borderColor = .separatorColor; surround?.borderWidth = 1 }
        return accepted
    }
}

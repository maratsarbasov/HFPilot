import Cocoa
import CoreBluetooth
import Darwin

// Handle only the explicitly persistent rows before NSMenu's tracking loop
// consumes their activation event. Standard drawing and all other events remain
// AppKit's responsibility; this is local to this application and this open menu.
@objc(VoiceAgentApplication) final class MenuApplication: NSApplication {
    weak var trackedMenu: NSMenu?
    var persistentItems = Set<NSMenuItem>()

    override func nextEvent(matching mask: NSEvent.EventTypeMask, until expiration: Date?,
                            inMode mode: RunLoop.Mode, dequeue: Bool) -> NSEvent? {
        while let event = super.nextEvent(matching: mask, until: expiration, inMode: mode, dequeue: dequeue) {
            if dequeue && consumePersistentAction(event) { continue }
            return event
        }
        return nil
    }

    private func consumePersistentAction(_ event: NSEvent) -> Bool {
        guard let menu = trackedMenu else { return false }
        let item: NSMenuItem?
        switch event.type {
        case .leftMouseDown:
            return persistentItems.contains { $0.accessibilityFrame().contains(NSEvent.mouseLocation) }
        case .leftMouseUp:
            item = persistentItems.first { $0.accessibilityFrame().contains(NSEvent.mouseLocation) }
        case .keyDown:
            let flags = event.modifierFlags.intersection([.command, .option, .control, .shift])
            if flags == .command {
                item = persistentItems.first { !$0.keyEquivalent.isEmpty && $0.keyEquivalent == event.charactersIgnoringModifiers }
            } else if flags.isEmpty && [UInt16(36), 49, 76].contains(event.keyCode) {
                item = menu.highlightedItem
            } else { return false }
            if event.isARepeat { return item.map { persistentItems.contains($0) } ?? false }
        default:
            return false
        }
        guard let item = item, persistentItems.contains(item) else { return false }
        if item.isEnabled, let action = item.action {
            sendAction(action, to: item.target, from: item)
        }
        return true
    }
}

if CommandLine.arguments.contains("--test-audio") {
    LocalTestAudio.run()
    exit(0)
}

let application = MenuApplication.shared
let delegate = AppDelegate()
application.setActivationPolicy(.accessory)
application.delegate = delegate
application.run()

import Cocoa
import AVFoundation
import SwiftUI

final class LibraryWindow: NSWindowController, NSTableViewDataSource, NSTableViewDelegate, NSTextViewDelegate, NSTextFieldDelegate, NSWindowDelegate, NSToolbarDelegate, AVAudioPlayerDelegate {
    private let bridge: DesktopBridge
    private let tunnel: TunnelController
    private let control: (String) -> Void
    private let setupPhone: () -> Void
    private let openBluetooth: () -> Void
    private let pages = NSView()
    private let sidebar = NSTableView()
    private static let pageNames = ["Conversations", "Instructions", "Settings", "Test Call"]
    private lazy var testCall = TestCallPane(bridge: bridge, incomingInstructions: { [weak self] in
        self?.editor.string ?? ""
    }, showConversations: { [weak self] id in
        self?.selectedID = id
        self?.present(page: 0)
    }, showSettings: { [weak self] in
        self?.present(page: 2)
    })
    private static let pageTitleID = NSToolbarItem.Identifier("HFPilot.pageTitle")
    private let pageTitle = NSTextField(labelWithString: "")
    private var pageViews: [NSView] = []
    private let preferences = SettingsPresentation()
    private var statusText = "Checking status…"
    private var answering = false
    private var statusKnown = false
    private var renderedDetail: (String, Bool, String)?
    private let table = NSTableView()
    private var calls: [[String: Any]] = []
    private let callsContent = NSView()
    private let emptyCalls = NSView()
    private let emptyCallsTitle = NSTextField(labelWithString: "Loading conversations…")
    private let emptyCallsNote = NSTextField(wrappingLabelWithString: "")
    private let emptyCallsAction = NSButton(title: "Try a Test Call", target: nil, action: nil)
    private let callTitle = NSTextField(labelWithString: "Conversations")
    private let callSubtitle = NSTextField(wrappingLabelWithString: "Select a conversation to read its result.")
    private let detail = NSTextView()
    private let detailMode = NSSegmentedControl(labels: ["Summary", "Transcript"], trackingMode: .selectOne, target: nil, action: nil)
    private let playButton = NSButton(title: "Play Recording", target: nil, action: nil)
    private let audioLabel = NSTextField(labelWithString: "")
    private var selectedID: String?
    private var details: [String: Any] = [:]
    private var player: AVAudioPlayer?
    private let editor = InstructionEditor()
    private let saveButton = NSButton(title: "Save", target: nil, action: nil)
    private let reloadButton = NSButton(title: "Load Latest", target: nil, action: nil)
    private let editorNote = NSTextField(wrappingLabelWithString: "Applies to future calls. Shared with connected assistants.")
    private var revision = ""
    private var savedInstructions = ""
    private var dirty = false
    private var savingInstructions = false
    var hasUnsavedInstructions: Bool { dirty }
    private var loadingEditor = false
    private var overviewLoading = false
    private let phoneStatus = NSTextField(wrappingLabelWithString: "Set up your iPhone to start answering calls.")
    private let reconnectButton = NSButton(title: "Turn On and Connect", target: nil, action: nil)
    private let pairButton = NSButton(title: "Set Up Phone…", target: nil, action: nil)
    private let keyLabel = NSTextField(labelWithString: "API key")
    private let keyField = NSSecureTextField()
    private var savingKey = false
    private let keySaveButton = NSButton(title: "Save API Key", target: nil, action: nil)
    private var settingsSheet: NSWindow?
    private var sheetContent: NSStackView?
    private let sheetNote = NSTextField(wrappingLabelWithString: "")
    private var configured = false
    private var savedPhone = false
    private var focusAfterRestore = false
    private var phoneName = "iPhone"
    private var runtime: [String: Any] = [:]
    private var operation: String?
    private var settingsMessage: String?
    private var recordingSettings: [String: Any] = [:]
    private var savingRecording = false
    private var voiceSettings = VoiceSettings()

    init(bridge: DesktopBridge, tunnel: TunnelController, control: @escaping (String) -> Void, setupPhone: @escaping () -> Void,
         openBluetooth: @escaping () -> Void) {
        self.bridge = bridge
        self.tunnel = tunnel
        self.control = control
        self.setupPhone = setupPhone
        self.openBluetooth = openBluetooth
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 610),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = "HFPilot"
        window.collectionBehavior.insert(.moveToActiveSpace)
        window.minSize = NSSize(width: 760, height: 520)
        window.toolbarStyle = .unifiedCompact
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
        window.center()
        build()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    private func label(_ text: String, heading: Bool = false) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        if heading { field.font = .boldSystemFont(ofSize: 18) }
        return field
    }
    private func button(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        return button
    }
    private func vertical(_ views: [NSView], spacing: CGFloat = 12) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = spacing
        return stack
    }
    private func horizontal(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.spacing = 9
        stack.alignment = .centerY
        return stack
    }
    private func scroll(_ text: NSTextView, editable: Bool) -> NSView {
        text.isEditable = editable
        text.isRichText = false
        text.isAutomaticQuoteSubstitutionEnabled = false
        text.isAutomaticDashSubstitutionEnabled = false
        text.isVerticallyResizable = true
        text.isHorizontallyResizable = false
        text.font = .systemFont(ofSize: 14)
        text.textColor = .labelColor
        text.drawsBackground = false
        text.allowsUndo = editable
        let style = NSMutableParagraphStyle()
        style.lineSpacing = 4
        text.defaultParagraphStyle = style
        text.textContainerInset = editable ? NSSize(width: 16, height: 14) : NSSize(width: 0, height: 5)
        text.textContainer?.lineFragmentPadding = 0
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        let view = NSScrollView()
        view.hasVerticalScroller = true
        view.autohidesScrollers = true
        view.drawsBackground = false
        view.borderType = .noBorder
        view.documentView = text
        guard editable else { return view }
        let box = NSBox()
        box.boxType = .custom
        box.titlePosition = .noTitle
        box.cornerRadius = 9
        box.fillColor = .textBackgroundColor
        box.borderColor = .separatorColor
        box.borderWidth = 1
        box.contentViewMargins = NSSize(width: 2, height: 2)
        box.contentView = view
        editor.surround = box
        return box
    }
    private func build() {
        sidebar.addTableColumn(NSTableColumn(identifier: NSUserInterfaceItemIdentifier("navigation")))
        sidebar.headerView = nil
        sidebar.rowHeight = 32
        sidebar.style = .sourceList
        sidebar.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        sidebar.backgroundColor = .clear
        sidebar.allowsEmptySelection = false
        sidebar.dataSource = self
        sidebar.delegate = self
        sidebar.setAccessibilityLabel("HFPilot navigation")
        let sidebarScroll = NSScrollView()
        sidebarScroll.drawsBackground = false
        sidebarScroll.documentView = sidebar
        let sidebarBackground = NSView()
        sidebarScroll.automaticallyAdjustsContentInsets = false
        sidebarScroll.translatesAutoresizingMaskIntoConstraints = false
        sidebarBackground.addSubview(sidebarScroll)
        NSLayoutConstraint.activate([
            sidebarScroll.leadingAnchor.constraint(equalTo: sidebarBackground.leadingAnchor),
            sidebarScroll.topAnchor.constraint(equalTo: sidebarBackground.safeAreaLayoutGuide.topAnchor, constant: 8),
            sidebarScroll.trailingAnchor.constraint(equalTo: sidebarBackground.trailingAnchor),
            sidebarScroll.bottomAnchor.constraint(equalTo: sidebarBackground.bottomAnchor)
        ])
        pageViews = [buildCalls(), buildInstructions(), buildSettings(), testCall]
        for view in pageViews { view.translatesAutoresizingMaskIntoConstraints = false; pages.addSubview(view) }
        // A real sidebar pane lets AppKit carry its material behind the window
        // controls and position the title above the detail column.
        let navigation = NSViewController()
        navigation.view = sidebarBackground
        let rail = NSSplitViewItem(sidebarWithViewController: navigation)
        rail.minimumThickness = 178
        rail.maximumThickness = 240
        rail.preferredThicknessFraction = 0.2
        rail.canCollapse = false
        let detail = NSViewController()
        detail.view = pages
        let contentItem = NSSplitViewItem(viewController: detail)
        contentItem.titlebarSeparatorStyle = .none
        let split = NSSplitViewController()
        split.splitView.isVertical = true
        split.addSplitViewItem(rail)
        split.addSplitViewItem(contentItem)
        let window = self.window!
        let frame = window.frame
        window.contentViewController = split
        pageTitle.font = .systemFont(ofSize: 16, weight: .semibold)
        let toolbar = NSToolbar(identifier: "HFPilot.library")
        toolbar.delegate = self
        window.toolbar = toolbar
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.setFrame(frame, display: false)
        for view in pageViews {
            NSLayoutConstraint.activate([view.leadingAnchor.constraint(equalTo: pages.leadingAnchor),
                view.trailingAnchor.constraint(equalTo: pages.trailingAnchor),
                view.topAnchor.constraint(equalTo: pages.safeAreaLayoutGuide.topAnchor), view.bottomAnchor.constraint(equalTo: pages.bottomAnchor)])
        }
        selectPage(0)
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.sidebarTrackingSeparator, Self.pageTitleID, .flexibleSpace]
    }
    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.sidebarTrackingSeparator, Self.pageTitleID, .flexibleSpace]
    }
    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier identifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        guard identifier == Self.pageTitleID else { return nil }
        let item = NSToolbarItem(itemIdentifier: identifier)
        item.view = pageTitle
        return item
    }

    private func buildCalls() -> NSView {
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("conversation"))
        table.addTableColumn(column)
        table.headerView = nil
        table.rowHeight = 58
        table.style = .inset
        table.dataSource = self
        table.delegate = self
        table.setAccessibilityLabel("Conversations")
        let list = NSScrollView()
        list.hasVerticalScroller = true
        list.autohidesScrollers = true
        list.drawsBackground = false
        list.documentView = table
        list.widthAnchor.constraint(equalToConstant: 235).isActive = true
        callTitle.font = .systemFont(ofSize: 21, weight: .semibold)
        callSubtitle.font = .systemFont(ofSize: 12)
        detail.setAccessibilityLabel("Conversation text")
        callTitle.lineBreakMode = .byTruncatingTail
        callSubtitle.textColor = .secondaryLabelColor
        detailMode.selectedSegment = 0
        detailMode.target = self
        detailMode.action = #selector(showDetail)
        let text = scroll(detail, editable: false)
        playButton.target = self
        playButton.action = #selector(play)
        playButton.bezelStyle = .rounded
        playButton.isHidden = true
        audioLabel.textColor = .secondaryLabelColor
        audioLabel.font = .systemFont(ofSize: 11)
        let audio = horizontal([playButton, audioLabel])
        let right = vertical([callTitle, callSubtitle, detailMode, text, audio])
        let container = callsContent
        right.translatesAutoresizingMaskIntoConstraints = false
        list.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(list)
        container.addSubview(right)
        NSLayoutConstraint.activate([
            list.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            list.topAnchor.constraint(equalTo: container.topAnchor),
            list.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            right.leadingAnchor.constraint(equalTo: list.trailingAnchor, constant: 20),
            right.topAnchor.constraint(equalTo: container.topAnchor, constant: 20),
            right.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -20),
            right.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -18),
            text.widthAnchor.constraint(equalTo: right.widthAnchor),
            callSubtitle.widthAnchor.constraint(equalTo: right.widthAnchor)
        ])
        let emptyIcon = NSImageView(image: NSImage(systemSymbolName: "bubble.left.and.bubble.right", accessibilityDescription: nil)!)
        emptyIcon.contentTintColor = .secondaryLabelColor
        emptyIcon.imageScaling = .scaleProportionallyUpOrDown
        emptyIcon.widthAnchor.constraint(equalToConstant: 42).isActive = true
        emptyIcon.heightAnchor.constraint(equalToConstant: 42).isActive = true
        emptyCallsTitle.font = .systemFont(ofSize: 21, weight: .semibold)
        emptyCallsTitle.alignment = .center
        emptyCallsNote.font = .systemFont(ofSize: 13)
        emptyCallsNote.textColor = .secondaryLabelColor
        emptyCallsNote.alignment = .center
        emptyCallsAction.target = self
        emptyCallsAction.action = #selector(openTestCall)
        emptyCallsAction.bezelStyle = .rounded
        emptyCallsAction.isHidden = true
        let welcome = vertical([emptyIcon, emptyCallsTitle, emptyCallsNote, emptyCallsAction], spacing: 12)
        welcome.alignment = .centerX
        welcome.translatesAutoresizingMaskIntoConstraints = false
        emptyCalls.addSubview(welcome)
        NSLayoutConstraint.activate([
            welcome.centerXAnchor.constraint(equalTo: emptyCalls.centerXAnchor),
            welcome.centerYAnchor.constraint(equalTo: emptyCalls.centerYAnchor, constant: -24),
            welcome.widthAnchor.constraint(lessThanOrEqualToConstant: 400),
            welcome.widthAnchor.constraint(lessThanOrEqualTo: emptyCalls.widthAnchor, constant: -48),
            emptyCallsNote.widthAnchor.constraint(equalTo: welcome.widthAnchor)
        ])
        let page = NSView()
        for view in [callsContent, emptyCalls] {
            view.translatesAutoresizingMaskIntoConstraints = false
            page.addSubview(view)
            NSLayoutConstraint.activate([
                view.leadingAnchor.constraint(equalTo: page.leadingAnchor),
                view.trailingAnchor.constraint(equalTo: page.trailingAnchor),
                view.topAnchor.constraint(equalTo: page.topAnchor),
                view.bottomAnchor.constraint(equalTo: page.bottomAnchor)
            ])
        }
        callsContent.isHidden = true
        return page
    }

    @objc private func openTestCall() { present(page: 3) }

    private func buildInstructions() -> NSView {
        editor.delegate = self
        let text = scroll(editor, editable: true)
        editor.setAccessibilityLabel("Incoming call instructions")
        saveButton.target = self
        saveButton.action = #selector(saveInstructions)
        saveButton.bezelStyle = .rounded
        saveButton.keyEquivalent = "s"
        reloadButton.target = self
        reloadButton.action = #selector(reloadInstructions)
        reloadButton.bezelStyle = .rounded
        reloadButton.isHidden = true
        editorNote.textColor = .secondaryLabelColor
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let actions = horizontal([spacer, reloadButton, saveButton])
        let stack = vertical([label("Incoming call instructions", heading: true), editorNote, text, actions])
        actions.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let container = NSView()
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 22),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -22),
            text.widthAnchor.constraint(equalTo: stack.widthAnchor),
            editorNote.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        return container
    }

    private func buildSettings() -> NSView {
        reconnectButton.target = self
        reconnectButton.action = #selector(reconnect)
        pairButton.target = self
        pairButton.action = #selector(pair)
        keySaveButton.target = self
        keySaveButton.action = #selector(saveKey)
        for item in [reconnectButton, pairButton, keySaveButton] { item.bezelStyle = .rounded }
        keyField.delegate = self
        keyField.placeholderString = "New API key"
        keyField.setAccessibilityLabel("OpenAI API key")
        sheetNote.textColor = .secondaryLabelColor
        return NSHostingView(rootView: SettingsPane(presentation: preferences, tunnel: tunnel, bridge: bridge,
            didUpdateConnections: { [weak self] info in
                self?.runtime["connections"] = info
                self?.renderPreferences()
            }, didUpdateVoice: { [weak self] info in
                self?.voiceSettings = VoiceSettings(info)
                self?.renderPreferences()
            }) { [weak self] action in
            guard let self = self else { return }
            switch action {
            case .enable(let value): self.control(value ? "enable" : "disable")
            case .recording(let value): self.saveRecording(value)
            case .phone: if self.savedPhone { self.managePhone() } else { self.pair() }
            case .key: self.manageKey()
            case .log: self.openLog()
            case .configuration: self.openConfig()
            }
        })
    }

    private func separator() -> NSBox {
        let line = NSBox()
        line.boxType = .separator
        return line
    }
    private func presentSettingsSheet(_ title: String, views: [NSView], primary: NSButton? = nil) {
        guard let window = window, settingsSheet == nil else { return }
        let sheet = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 450, height: 300), styleMask: [.titled], backing: .buffered, defer: false)
        sheet.title = title
        let done = button(primary == nil ? "Done" : "Cancel", action: #selector(closeSettingsSheet))
        done.keyEquivalent = primary == nil ? "\r" : "\u{1b}"
        primary?.keyEquivalent = "\r"
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        sheetNote.removeFromSuperview()
        sheetNote.stringValue = ""
        sheetNote.isHidden = true
        for view in views { view.removeFromSuperview() }
        let footer = horizontal([spacer, done] + [primary].compactMap { $0 })
        let stack = vertical([label(title, heading: true)] + views + [sheetNote, footer], spacing: 14)
        let content = sheet.contentView!
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20),
            footer.widthAnchor.constraint(equalTo: stack.widthAnchor),
            sheetNote.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        for view in views where view is NSTextField { view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true }
        settingsSheet = sheet
        sheetContent = stack
        resizeSettingsSheet()
        window.beginSheet(sheet)
    }
    private func resizeSettingsSheet() {
        guard let sheet = settingsSheet, let stack = sheetContent else { return }
        let visible = stack.arrangedSubviews.filter { !$0.isHidden }
        let height = visible.reduce(CGFloat(44 + max(0, visible.count - 1) * 14)) { total, view in
            if let field = view as? NSTextField, let cell = field.cell {
                return total + ceil(cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: 402, height: 10000)).height)
            }
            return total + (view is NSBox ? 1 : max(22, view.fittingSize.height))
        }
        if abs(sheet.contentLayoutRect.height - height) > 1 { sheet.setContentSize(NSSize(width: 450, height: height)) }
    }
    @objc private func closeSettingsSheet() {
        guard let sheet = settingsSheet else { return }
        window?.endSheet(sheet)
        settingsSheet = nil
        sheetContent = nil
        keyField.stringValue = ""
    }
    @objc private func managePhone() {
        presentSettingsSheet(phoneName, views: [phoneStatus, reconnectButton,
            separator(), label("Forgot HFPilot on your iPhone, or using another phone? Set it up again."), pairButton,
            button("Open Bluetooth Settings…", action: #selector(bluetoothSettings))])
    }
    @objc private func manageKey() {
        updateKeyButton()
        presentSettingsSheet("OpenAI API key", views: [keyLabel, keyField], primary: keySaveButton)
        settingsSheet?.makeFirstResponder(keyField)
    }
    func present(page: Int) {
        selectPage(page)
        bringToFront()
        refreshData()
    }
    func bringToFront() {
        guard let window = window else { return }
        if NSApp.isHidden { NSApp.unhide(nil) }
        if window.isMiniaturized {
            focusAfterRestore = true
            window.deminiaturize(nil)
            return
        }
        window.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
        (window.attachedSheet ?? window).makeKeyAndOrderFront(nil)
    }
    func windowDidDeminiaturize(_ notification: Notification) {
        guard focusAfterRestore else { return }
        focusAfterRestore = false
        RunLoop.main.perform(inModes: [.default]) { [weak self] in self?.bringToFront() }
    }
    private func selectPage(_ index: Int) {
        if index != 0 { player?.stop(); player = nil }
        pageTitle.stringValue = Self.pageNames[index]
        window?.title = pageTitle.stringValue
        for (position, view) in pageViews.enumerated() { view.isHidden = position != index }
        if sidebar.selectedRow != index { sidebar.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false) }
    }

    func refreshData() {
        guard !overviewLoading && operation == nil else { return }
        overviewLoading = true
        bridge.data("overview") { [weak self] result in
            guard let self = self else { return }
            self.overviewLoading = false
            guard result.code == 0 else {
                if self.calls.isEmpty {
                    self.emptyCallsTitle.stringValue = "Conversations unavailable"
                    self.emptyCallsNote.stringValue = result.error
                }
                self.showMessage(result.error)
                return
            }
            let data = result.object
            self.recordingSettings = data["recording"] as? [String: Any] ?? [:]
            self.voiceSettings = VoiceSettings(data["voice"] as? [String: Any] ?? [:])
            self.configured = data["api_key_configured"] as? Bool == true
            self.keyLabel.stringValue = self.configured ? "API key configured. Updates apply to future calls." : "Add an API key to enable the assistant."
            let phone = data["phone"] as? [String: Any]
            self.savedPhone = phone != nil
            self.phoneName = phone?["name"] as? String ?? "iPhone"
            self.pairButton.title = self.savedPhone ? "Set Up / Replace Phone…" : "Set Up Phone…"
            if let context = data["context"] as? [String: Any], let revision = context["revision"] as? String {
                if !self.dirty {
                    self.loadingEditor = true
                    let latest = context["text"] as? String ?? ""
                    self.savedInstructions = latest
                    if self.editor.string != latest { self.editor.string = latest }
                    self.loadingEditor = false
                    self.revision = revision
                    self.editorNote.stringValue = "Applies to future calls. Shared with connected assistants."
                    self.reloadButton.isHidden = true
                } else if self.revision != revision {
                    self.editorNote.stringValue = "Instructions changed elsewhere. Your draft is kept. Load the latest version to merge it."
                    self.reloadButton.isHidden = false
                }
            }
            if let error = data["configuration_error"] as? String { self.showMessage(error) }
            self.calls = data["calls"] as? [[String: Any]] ?? []
            self.callsContent.isHidden = self.calls.isEmpty
            self.emptyCalls.isHidden = !self.calls.isEmpty
            self.emptyCallsTitle.stringValue = "No conversations yet"
            self.emptyCallsNote.stringValue = "Try a call with your assistant. Transcripts, summaries and recordings will appear here."
            self.emptyCallsAction.isHidden = false
            self.table.reloadData()
            if let id = self.selectedID, let row = self.calls.firstIndex(where: { $0["id"] as? String == id }) {
                self.table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
                self.loadSelectedCall()
            } else if !self.calls.isEmpty {
                self.table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
                self.tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification))
            } else {
                self.selectedID = nil
                self.details = [:]
                self.player?.stop()
                self.player = nil
            }
            self.saveButton.isEnabled = self.dirty && !self.revision.isEmpty
            self.renderPreferences()
            self.testCall.update(configured: self.configured, runtime: self.runtime, operation: self.operation,
                                 recording: self.recordingSettings)
        }
    }

    func update(_ state: [String: Any], operation: String?, status: String, enabled: Bool, known: Bool) {
        runtime = state
        self.operation = operation
        statusText = status
        answering = enabled
        statusKnown = known
        renderPreferences()
        testCall.update(configured: configured, runtime: runtime, operation: operation, recording: recordingSettings)
    }
    private func renderPreferences() {
        let state = runtime
        let active = state["active_call_id"] as? String != nil
        let enabled = answering
        let listening = (state["listening"] as? [String: Any])?["status"] as? String ?? "paused"
        let connected = state["bluetooth_connected"] as? Bool == true
        reconnectButton.title = state["cleanup_failed"] as? Bool == true ? "Restart Assistant" : enabled ? "Reconnect" : "Turn On and Connect"
        reconnectButton.isHidden = !savedPhone
        reconnectButton.isEnabled = operation == nil && !active && !connected
        pairButton.isEnabled = operation == nil && !active && (state["pending_summaries"] as? Int ?? 0) == 0
        phoneStatus.stringValue = connected ? "Connected via Bluetooth" : listening == "pairing" ? "Follow the phone setup window." : enabled ? "Keep the phone nearby with Bluetooth enabled. Reconnecting automatically." : "When paused, calls ring on your iPhone."
        let phoneSummary = !savedPhone ? "Set up an iPhone to get started" : connected ? "Connected · Bluetooth" : enabled ? "Waiting for phone · Reconnecting automatically" : "Saved · Assistant paused"
        if let error = state["connection_error"] as? String { phoneStatus.stringValue += "\n" + error }
        let connections = state["connections"] as? [String: Any]
        var publicURL = connections?["public_url"] as? String
        if connections == nil, let saved = try? String(contentsOf: bridge.root.appendingPathComponent(".work/mcp/public-url"), encoding: .utf8) {
            publicURL = saved.trimmingCharacters(in: .whitespacesAndNewlines) + "/mcp"
        }
        updateKeyButton()
        let recording = state["recording"] as? [String: Any] ?? recordingSettings
        let recordingOverride = recording["overridden"] as? Bool == true
        let recordingDetail = savingRecording ? "Saving…" : recordingOverride ? "Controlled by a command-line option. Restart from HFPilot to change it." :
            recording["error"] as? String ?? "Saved on this Mac. Applies to future calls and tests."
        let note = settingsMessage ?? ((!savedPhone || !configured) ? "Add your iPhone and API key to get started. While answering calls, HFPilot uses your Mac’s Bluetooth controller; other Bluetooth accessories reconnect after you pause." : nil)
        let display = SettingsDisplay(status: statusText, enabled: enabled,
            canToggle: statusKnown && operation == nil && (enabled || (configured && savedPhone)), activeCall: active,
            recordAudio: recording["enabled"] as? Bool == true,
            canToggleRecording: !recording.isEmpty && !savingRecording && !recordingOverride && operation == nil,
            recordingDetail: recordingDetail,
            phone: phoneName, phoneStatus: phoneSummary, savedPhone: savedPhone, serverRunning: statusKnown ? state["listening"] != nil : nil,
            publicURL: publicURL, suggestedURL: connections?["suggested_url"] as? String, hasKey: configured, note: note,
            voice: voiceSettings)
        if display != preferences.display { preferences.display = display }
        resizeSettingsSheet()
    }

    private func saveRecording(_ enabled: Bool) {
        guard !savingRecording else { return }
        savingRecording = true
        renderPreferences()
        bridge.data("save_recording", values: ["enabled": enabled]) { [weak self] result in
            guard let self = self else { return }
            self.savingRecording = false
            if result.code == 0 {
                self.recordingSettings = result.object
                self.runtime["recording"] = result.object
                self.renderPreferences()
            } else { self.showMessage(result.error) }
        }
    }

    func showMessage(_ text: String) {
        settingsMessage = text.isEmpty ? nil : text
        renderPreferences()
        sheetNote.stringValue = text
        sheetNote.isHidden = text.isEmpty
        resizeSettingsSheet()
    }
    func numberOfRows(in tableView: NSTableView) -> Int { tableView === sidebar ? Self.pageNames.count : calls.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let cell = NSTableCellView()
        if tableView === sidebar {
            let title = NSTextField(labelWithString: Self.pageNames[row])
            title.font = .systemFont(ofSize: 13)
            let icon = NSImageView(image: NSImage(systemSymbolName: ["phone", "text.alignleft", "gearshape", "mic"][row], accessibilityDescription: nil)!)
            icon.contentTintColor = .secondaryLabelColor
            cell.textField = title
            cell.imageView = icon
            for view in [title, icon] { view.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(view) }
            NSLayoutConstraint.activate([
                icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
                icon.widthAnchor.constraint(equalToConstant: 16), icon.heightAnchor.constraint(equalToConstant: 16),
                icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                title.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 9),
                title.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                title.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
            ])
            return cell
        }
        let call = calls[row]
        let title = label(call["title"] as? String ?? "Unknown caller")
        title.font = .systemFont(ofSize: 13, weight: .medium)
        let subtitle = label(dateLabel(call) + " · " + (call["direction"] as? String ?? "incoming").capitalized)
        subtitle.font = .systemFont(ofSize: 11)
        subtitle.textColor = .secondaryLabelColor
        let stack = vertical([title, subtitle], spacing: 3)
        stack.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
            stack.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
            stack.centerYAnchor.constraint(equalTo: cell.centerYAnchor)])
        return cell
    }
    private func dateLabel(_ call: [String: Any]) -> String {
        let value = call["started_at"] as? String ?? ""
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = iso.date(from: value)
        if date == nil { iso.formatOptions = [.withInternetDateTime]; date = iso.date(from: value) }
        if date == nil {
            let parser = DateFormatter()
            parser.locale = Locale(identifier: "en_US_POSIX")
            parser.dateFormat = "yyyyMMdd-HHmmss"
            date = parser.date(from: String((call["id"] as? String ?? "").prefix(15)))
        }
        guard let date = date else { return "Date unavailable" }
        return DateFormatter.localizedString(from: date, dateStyle: .short, timeStyle: .short)
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        if notification.object as? NSTableView === sidebar { selectPage(max(0, sidebar.selectedRow)); return }
        let row = table.selectedRow
        guard calls.indices.contains(row) else { return }
        player?.stop()
        player = nil
        let id = calls[row]["id"] as? String ?? ""
        selectedID = id
        details = [:]
        loadSelectedCall()
    }
    private func loadSelectedCall() {
        guard let id = selectedID else { return }
        showDetail()
        bridge.data("call_details", values: ["id": id]) { [weak self] result in
            guard let self = self, self.selectedID == id else { return }
            self.details = result.code == 0 ? result.object : [:]
            self.showDetail()
        }
    }
    @objc private func showDetail() {
        guard let id = selectedID, let call = calls.first(where: { $0["id"] as? String == id }) else { return }
        callTitle.stringValue = call["title"] as? String ?? "Unknown caller"
        let outcome = call["outcome"] as? String ?? "unknown"
        let outcomes = ["remote_hung_up": "Caller ended call", "caller_hung_up": "Caller ended call", "assistant_hung_up": "Assistant ended call", "starting": "In progress", "failed": "Failed", "stopped": "Ended"]
        var subtitle = dateLabel(call) + " · " + (call["direction"] as? String ?? "incoming").capitalized + " · " + (outcomes[outcome] ?? outcome.replacingOccurrences(of: "_", with: " ").capitalized)
        if call["test_call"] as? Bool == true { subtitle += " · Test call" }
        callSubtitle.stringValue = subtitle
        let content = detailMode.selectedSegment == 1 ? (details["transcript"] as? String ?? "Loading transcript…") : (call["summary"] as? String ?? "No summary is available.")
        let display = (id, detailMode.selectedSegment == 1, content)
        if renderedDetail.map({ $0 != display }) ?? true {
            detail.textStorage?.setAttributedString(ConversationText.document(content, transcript: display.1))
            detail.scrollRangeToVisible(NSRange(location: 0, length: 0))
            renderedDetail = display
        }
        playButton.isHidden = details["recording"] as? String == nil
        playButton.title = player?.isPlaying == true ? "Pause Recording" : "Play Recording"
        audioLabel.stringValue = playButton.isHidden ? "No recording is available." : ""
    }
    @objc private func play() {
        guard let path = details["recording"] as? String else { return }
        do {
            if player == nil { player = try AVAudioPlayer(contentsOf: URL(fileURLWithPath: path)); player?.delegate = self }
            if player?.isPlaying == true { player?.pause() } else { player?.play() }
            showDetail()
        } catch { audioLabel.stringValue = "The recording could not be opened." }
    }
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) { showDetail() }
    func textDidChange(_ notification: Notification) {
        guard !loadingEditor, notification.object as? NSTextView === editor else { return }
        dirty = editor.string != savedInstructions
        saveButton.isEnabled = dirty && !revision.isEmpty && !savingInstructions
        editorNote.stringValue = dirty ? "Unsaved changes. Saving applies them to future calls." : "Applies to future calls. Shared with connected assistants."
    }
    @objc private func saveInstructions() {
        guard !savingInstructions else { return }
        savingInstructions = true
        saveButton.isEnabled = false
        let submitted = editor.string
        bridge.data("save_context", values: ["text": submitted, "revision": revision]) { [weak self] result in
            guard let self = self else { return }
            self.savingInstructions = false
            if result.code == 0 {
                self.revision = result.object["revision"] as? String ?? self.revision
                self.savedInstructions = submitted
                self.dirty = self.editor.string != submitted
                self.editorNote.stringValue = self.dirty ? "Your latest edits are not saved yet." : "Saved for future calls."
                self.saveButton.isEnabled = self.dirty
            } else {
                self.editorNote.stringValue = result.error + " Your draft is kept."
                self.reloadButton.isHidden = false
                self.saveButton.isEnabled = true
            }
        }
    }
    @objc private func reloadInstructions() {
        if dirty {
            let alert = NSAlert()
            alert.messageText = "Load the latest instructions?"
            alert.informativeText = "This replaces your unsaved draft with the latest saved instructions."
            alert.addButton(withTitle: "Keep Editing")
            alert.addButton(withTitle: "Load Latest")
            guard alert.runModal() == .alertSecondButtonReturn else { return }
        }
        dirty = false
        refreshData()
    }
    private func updateKeyButton() {
        keySaveButton.isEnabled = !savingKey && operation == nil && runtime["active_call_id"] as? String == nil && !keyField.stringValue.isEmpty
    }
    func controlTextDidChange(_ obj: Notification) {
        if obj.object as? NSTextField === keyField { updateKeyButton() }
    }
    @objc private func saveKey() {
        guard keySaveButton.isEnabled else { return }
        let submittedSheet = settingsSheet
        savingKey = true
        let value = keyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        keyField.stringValue = ""
        keySaveButton.isEnabled = false
        bridge.data("save_key", values: ["key": value]) { [weak self] result in
            self?.savingKey = false
            if result.code == 0 { self?.tunnel.credentialsChanged() }
            if result.code == 0 && self?.settingsSheet === submittedSheet { self?.closeSettingsSheet() }
            self?.showMessage(result.code == 0 ? "API key saved. It will be used for future calls." : result.error)
            self?.refreshData()
        }
    }
    @objc private func reconnect() { control(runtime["cleanup_failed"] as? Bool == true ? "restart-server" : "reconnect") }
    @objc private func pair() { closeSettingsSheet(); setupPhone() }
    @objc private func bluetoothSettings() { openBluetooth() }
    @objc private func openLog() { NSWorkspace.shared.open(bridge.root.appendingPathComponent(".work/mcp/server.log")) }
    @objc private func openConfig() { NSWorkspace.shared.open(bridge.root.appendingPathComponent("agent.toml")) }
    func windowDidBecomeKey(_ notification: Notification) { refreshData() }
    func windowWillClose(_ notification: Notification) { player?.stop(); player = nil }
}

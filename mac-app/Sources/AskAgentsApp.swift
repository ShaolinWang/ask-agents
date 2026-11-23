import AppKit
import SwiftUI
import UniformTypeIdentifiers

@main
struct AskAgentsApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        Window("Ask Agents", id: "conversation") {
            ConversationView(model: model)
                .frame(minWidth: 740, minHeight: 480)
                .task { model.start() }
                .sheet(isPresented: $model.showingSettings) { SettingsView(model: model) }
                .environment(\.interfaceLanguage, model.language)
                .environment(\.locale, model.language.locale)
        }
        .defaultSize(width: 1180, height: 730)
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button(model.language.text("Agent 设置…", "Agent Settings…"), action: model.openSettings)
                    .keyboardShortcut(",")
                    .disabled(!model.canSwitchConversation)
            }
            CommandGroup(replacing: .newItem) {
                Button(model.language.text("新对话", "New Conversation"), action: model.newConversation)
                    .keyboardShortcut("n")
                    .disabled(!model.canSwitchConversation)
            }
        }
    }
}

private struct ConversationView: View {
    @ObservedObject var model: AppModel
    @Environment(\.interfaceLanguage) private var language
    @Environment(\.scenePhase) private var scenePhase
    @State private var showingHistory = true
    @State private var showingExitedAgents = false

    private var visibleAgents: [Agent] {
        showingExitedAgents ? model.conversationAgents.filter { !model.current.exitingAgents.contains($0.name) }
            : model.participatingAgents
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                if showingHistory {
                    HistorySidebar(model: model)
                        .frame(width: 218)
                    Divider()
                }
                VStack(spacing: 0) {
                    if visibleAgents.isEmpty {
                        VStack(spacing: 12) {
                            Image(systemName: "bubble.left.and.bubble.right")
                                .font(.system(size: 30, weight: .light))
                                .foregroundStyle(.secondary)
                            if model.conversationAgents.isEmpty {
                                Text(language.text("启用 agent 开始对话", "Enable an agent to start a conversation"))
                                    .font(.title3.weight(.semibold))
                                Button(language.text("打开 Agent 设置", "Open Agent Settings"), action: model.openSettings)
                                    .disabled(!model.canSwitchConversation)
                            } else {
                                Text(model.current.exitingAgents.isEmpty
                                     ? language.text("所有 Agent 已退出", "All agents have left")
                                     : language.text("正在退出 Agent…", "Removing agent…"))
                                    .font(.title3.weight(.semibold))
                            }
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        panes.id(model.current.id)
                    }
                    Divider()
                    composer.id(model.current.id)
                }
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onChange(of: scenePhase) { _, phase in
            if phase == .active && showingHistory { model.reloadHistory() }
        }
        .onChange(of: model.current.id) {
            showingExitedAgents = !model.current.exitedAgents.isEmpty
        }
    }

    private var header: some View {
        let historyToggle = showingHistory ? language.text("隐藏历史会话", "Hide History")
            : language.text("显示历史会话", "Show History")
        return HStack(spacing: 11) {
            Button {
                showingHistory.toggle()
                if showingHistory { model.reloadHistory() }
            } label: {
                Label(historyToggle, systemImage: "sidebar.left")
                    .labelStyle(.iconOnly)
            }
            .help(historyToggle)

            Button(action: model.chooseDirectory) {
                Label(model.current.directory == "/" ? language.text("根目录", "Root") : URL(fileURLWithPath: model.current.directory).lastPathComponent,
                      systemImage: "folder")
                    .lineLimit(1)
            }
            .help(language.text("选择项目目录", "Choose Workspace"))
            .disabled(!model.canSwitchConversation)

            Text((model.current.directory as NSString).abbreviatingWithTildeInPath)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .font(.callout)
            Spacer()
            if !model.current.exitedAgents.isEmpty {
                Button {
                    showingExitedAgents.toggle()
                } label: {
                    Label(language.text("已退出 \(model.current.exitedAgents.count)", "\(model.current.exitedAgents.count) left"),
                          systemImage: showingExitedAgents ? "eye.fill" : "eye")
                }
                .help(showingExitedAgents
                      ? language.text("隐藏已退出 Agent 的回复", "Hide replies from agents who left")
                      : language.text("查看已退出 Agent 的回复", "Show replies from agents who left"))
                .accessibilityAddTraits(showingExitedAgents ? .isSelected : [])
            }
            newConversationControl

            Button(action: model.openSettings) {
                Label(language.text("Agent 设置", "Agent Settings"), systemImage: "gearshape")
            }
            .disabled(!model.canSwitchConversation)
        }
        .controlSize(.large)
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    @ViewBuilder
    private var newConversationControl: some View {
        Group {
            if model.agents.isEmpty {
                Button(action: model.newConversation) {
                    Label(language.text("新对话", "New Conversation"), systemImage: "plus.bubble")
                }
            } else {
                Menu {
                    Section(language.text("单 Agent 对话", "Talk to One Agent")) {
                        ForEach(model.agents) { agent in
                            Button {
                                model.newConversation(withAgent: agent.name)
                            } label: {
                                Text(verbatim: agent.name)
                                Text("\(Agent.title(agent.cli)) · \(agent.modelDisplayName(in: language))")
                            }
                        }
                    }
                } label: {
                    Label(language.text("新对话", "New Conversation"), systemImage: "plus.bubble")
                } primaryAction: {
                    model.newConversation()
                }
                .menuOrder(.fixed)
            }
        }
        .disabled(!model.canSwitchConversation)
    }

    private var panes: some View {
        GeometryReader { geometry in
            let agents = visibleAgents
            let width = max(270, (geometry.size.width - 36 - CGFloat(agents.count - 1) * 12) / CGFloat(agents.count))
            ScrollView(.horizontal) {
                HStack(spacing: 12) {
                    ForEach(agents) { agent in
                        AgentPane(agent: agent, exchanges: model.current.exchanges,
                                  exited: model.current.exitedAgents.contains(agent.name),
                                  canExit: model.canExitAgent(agent.name),
                                  canRetry: model.canRetryAgent(agent.name),
                                  onRetry: { model.retryAgent(agent.name) },
                                  onExit: {
                                      showingExitedAgents = false
                                      model.exitAgent(agent.name)
                                  })
                        .frame(width: width)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 14)
                .frame(height: geometry.size.height)
                .compactEnclosingScrollIndicators()
            }
        }
    }

    private var composer: some View {
        @Bindable var session = model.current
        return VStack(alignment: .leading, spacing: 8) {
            if !model.current.error.isEmpty {
                Label(model.current.error, systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
                    .accessibilityIdentifier("conversation-error")
            }
            if !model.current.images.isEmpty {
                ImageAttachmentsRow(images: model.current.images, remove: model.removeImage)
            }
            HStack(alignment: .bottom, spacing: 14) {
                ZStack(alignment: .topLeading) {
                    PromptEditor(text: $session.prompt, focusRequest: model.focusRequest,
                                 send: model.sendPrompt, allowImages: model.canAttachImages, addImages: model.addImages)
                        .frame(height: 63)
                    if model.current.prompt.isEmpty {
                        Text(language.text("输入问题", "Ask a question"))
                            .foregroundStyle(.tertiary)
                            .font(.system(size: 15))
                            .padding(.top, 8)
                            .padding(.leading, 13)
                            .allowsHitTesting(false)
                            .accessibilityHidden(true)
                    }
                }
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                .overlay { RoundedRectangle(cornerRadius: 8).strokeBorder(.quaternary) }

                Button(action: model.chooseImages) {
                    Image(systemName: "paperclip")
                }
                .controlSize(.large)
                .disabled(!model.canAttachImages)
                .help(language.text("添加图片，也可粘贴或拖入输入框", "Add images, or paste or drop them in the prompt"))
                .accessibilityLabel(language.text("添加图片", "Add Images"))

                if model.current.isResponding {
                    Button(action: model.cancel) {
                        Label(model.current.stopping ? language.text("正在停止…", "Stopping…") : language.text("停止", "Stop"),
                              systemImage: "stop.fill")
                    }
                    .disabled(model.current.stopping)
                    .controlSize(.large)
                } else {
                    Button(action: model.sendPrompt) { Label(language.text("发送", "Send"), systemImage: "arrow.up") }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(!model.canSend)
                        .controlSize(.large)
                }
            }
            Text(language.text("Return 发送 · Shift + Return 换行", "Return to send · Shift + Return for a new line"))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }
}

private struct HistorySidebar: View {
    @ObservedObject var model: AppModel
    @Environment(\.interfaceLanguage) private var language

    @State private var selecting = false
    @State private var selection: Set<String> = []
    @State private var deletion: Deletion?

    private struct Deletion: Identifiable {
        let id = UUID()
        let items: [HistoryItem]
    }

    var body: some View {
        let items = model.history
        let ids = Set(items.map(\.id))
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(language.text("历史会话", "History")).font(.headline)
                Spacer()
                if !items.isEmpty {
                    Button(selecting ? language.text("完成", "Done") : language.text("选择", "Select")) {
                        selection.removeAll()
                        selecting.toggle()
                    }
                    .buttonStyle(.link)
                }
            }
            .padding(.horizontal, 15)
            if items.isEmpty {
                Text(language.text("暂无历史会话", "No conversations yet"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 15)
            } else {
                ScrollView {
                    LazyVStack(spacing: 3) {
                        ForEach(items) { item in row(item) }
                    }
                    .padding(.horizontal, 8)
                    .compactEnclosingScrollIndicators()
                }
            }
            Spacer(minLength: 0)
            if selecting {
                Divider()
                HStack {
                    Button(selection == ids ? language.text("取消全选", "Deselect All") : language.text("全选", "Select All")) {
                        selection = selection == ids ? [] : ids
                    }
                    Spacer()
                    Button(role: .destructive) {
                        deletion = Deletion(items: items.filter { selection.contains($0.id) })
                    } label: {
                        Label(language.text("删除 (\(selection.count))", "Delete (\(selection.count))"), systemImage: "trash")
                    }
                    .disabled(selection.isEmpty || !model.canSwitchConversation)
                }
                .controlSize(.small)
                .padding([.horizontal, .bottom], 12)
            }
        }
        .padding(.top, 15)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .controlBackgroundColor))
        .onChange(of: ids) {
            selection.formIntersection(ids)
            if ids.isEmpty { selecting = false }
        }
        .alert(item: $deletion) { target in
            let count = target.items.count
            let title = count == 1
                ? language.text("删除「\(target.items[0].title)」？", "Delete “\(target.items[0].title)”?")
                : language.text("删除选中的 \(count) 个会话？", "Delete \(count) selected conversations?")
            let message = language.text("消息和附件将被永久删除。", "Messages and attachments will be permanently deleted.")
            let stopping = target.items.contains { model.isResponding(in: $0.id) }
                ? language.text("所选会话中正在生成的回复也会停止。", " Active replies in the selected conversations will also stop.") : ""
            return Alert(title: Text(verbatim: title), message: Text(verbatim: message + stopping),
                         primaryButton: .destructive(Text(language.text("删除", "Delete"))) {
                             model.deleteHistory(Set(target.items.map(\.id)))
                         }, secondaryButton: .cancel(Text(language.text("取消", "Cancel"))))
        }
    }

    @ViewBuilder
    private func actions(_ item: HistoryItem) -> some View {
        Button { model.setPinned(!item.pinned, for: item.id) } label: {
            Label(item.pinned ? language.text("取消置顶", "Unpin") : language.text("置顶", "Pin"),
                  systemImage: item.pinned ? "pin.slash" : "pin")
        }
        Divider()
        Button(role: .destructive) { deletion = Deletion(items: [item]) } label: {
            Label(language.text("删除…", "Delete…"), systemImage: "trash")
        }
    }

    private func row(_ item: HistoryItem) -> some View {
        let selected = selecting ? selection.contains(item.id) : model.current.id == item.id
        let folder = item.cwd == "/" ? language.text("根目录", "Root") : URL(fileURLWithPath: item.cwd).lastPathComponent
        return HStack(spacing: 4) {
            Button {
                if selecting {
                    if selection.remove(item.id) == nil { selection.insert(item.id) }
                } else {
                    model.openHistory(item)
                }
            } label: {
                HStack(spacing: 6) {
                    if selecting {
                        Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(selected ? Color.accentColor : .secondary)
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 4) {
                            if item.pinned {
                                Image(systemName: "pin.fill").font(.caption2)
                                    .accessibilityLabel(language.text("已置顶", "Pinned"))
                            }
                            Text(verbatim: item.title)
                                .font(.callout.weight(selected ? .semibold : .regular)).lineLimit(2)
                            if model.isResponding(in: item.id) {
                                ProgressView().controlSize(.small)
                                    .accessibilityLabel(language.text("正在回复", "Responding"))
                            }
                        }
                        HStack(spacing: 4) {
                            Text(verbatim: folder).lineLimit(1)
                            Spacer(minLength: 0)
                            Text(Date(timeIntervalSince1970: item.updatedAt), format: .dateTime.month().day().hour().minute())
                        }
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(selected ? .isSelected : [])
            .help("\(item.title)\n\(item.cwd)")
            if !selecting {
                Menu { actions(item) } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .accessibilityLabel(language.text("会话操作", "Conversation actions"))
            }
        }
        .padding(8)
        .background(selected ? Color.accentColor.opacity(0.16) : Color.clear, in: RoundedRectangle(cornerRadius: 6))
        .contextMenu { if !selecting { actions(item) } }
        .disabled(!model.canSwitchConversation)
    }
}

extension Agent {
    var tint: Color {
        switch cli {
        case "codex": .teal
        case "traex": .purple
        case "claude": .orange
        default: .blue
        }
    }
}

private struct AgentPane: View {
    @Environment(\.interfaceLanguage) private var language
    let agent: Agent
    let exchanges: [Exchange]
    let exited: Bool
    let canExit: Bool
    let canRetry: Bool
    let onRetry: () -> Void
    let onExit: () -> Void

    private var tint: Color { agent.tint }
    private var latest: Pane? { exchanges.last?.panes[agent.name] }

    var body: some View {
        ScrollViewReader { proxy in
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Image(systemName: "bubble.left")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(tint)
                        .frame(width: 32, height: 32)
                        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(agent.name).font(.headline).lineLimit(1)
                        Text("\(Agent.title(agent.cli)) · \(agent.modelDisplayName(in: language))")
                            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: 4)
                    if exited {
                        Text(language.text("已退出", "Left")).font(.caption).foregroundStyle(.secondary)
                    } else if let latest, latest.running {
                        ProgressView().controlSize(.small).accessibilityLabel(language.text("正在运行", "Running"))
                    }
                    if !exchanges.isEmpty {
                        Button { proxy.scrollTo("latest", anchor: .bottom) } label: {
                            Image(systemName: "arrow.down.to.line").accessibilityLabel(language.text("跳到最新", "Jump to Latest"))
                        }
                        .buttonStyle(.borderless)
                        .help(language.text("跳到最新", "Jump to Latest"))
                    }
                    if !exited && latest != nil {
                        Button(action: onRetry) {
                            Image(systemName: "arrow.clockwise")
                        }
                        .buttonStyle(.borderless)
                        .disabled(!canRetry)
                        .help(language.text("重试最近一条回复", "Retry the latest reply"))
                        .accessibilityLabel(language.text("重试 \(agent.name) 的回复", "Retry \(agent.name)’s reply"))
                    }
                    if canExit {
                        Button(action: onExit) {
                            Image(systemName: "xmark")
                                .font(.system(size: 11, weight: .semibold))
                                .padding(5)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.borderless)
                        .help(language.text("退出此对话", "Leave this conversation"))
                        .accessibilityLabel(language.text("退出 \(agent.name)", "Remove \(agent.name)"))
                    }
                }
                .padding(13)
                Divider()

                if exchanges.isEmpty {
                    Spacer()
                    Text(language.text("等待提问", "Waiting for a question")).foregroundStyle(.secondary)
                    Spacer()
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            ForEach(exchanges) { exchange in
                                if let pane = exchange.panes[agent.name] {
                                    TurnView(prompt: exchange.prompt, images: exchange.images, pane: pane,
                                             configuredModel: agent.model, tint: tint)
                                        .id(exchange.id)
                                }
                            }
                            Color.clear.frame(height: 1).id("latest")
                        }
                        .padding(13)
                        .compactEnclosingScrollIndicators()
                    }
                    .defaultScrollAnchor(.bottom)
                    .onChange(of: exchanges.last?.id) { _, id in
                        if let id { proxy.scrollTo(id, anchor: .top) }
                    }
                }
            }
        }
        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
        .overlay { RoundedRectangle(cornerRadius: 10).strokeBorder(.quaternary) }
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

private struct TurnView: View {
    @Environment(\.interfaceLanguage) private var language
    let prompt: String
    let images: [ImageAttachment]
    let pane: Pane
    let configuredModel: String?
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            VStack(alignment: .leading, spacing: 8) {
                if !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Text(verbatim: prompt).textSelection(.enabled)
                }
                if !images.isEmpty { ImageAttachmentsRow(images: images) }
            }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(tint.opacity(0.10), in: RoundedRectangle(cornerRadius: 7))

            ForEach(pane.entries.indices, id: \.self) { index in
                let entry = pane.entries[index]
                if let text = entry.text {
                    MarkdownReply(source: text).equatable()
                } else if let activity = entry.activity {
                    Label {
                        Text(verbatim: language.text(activity, entry.activityEnglish ?? activity)).textSelection(.enabled)
                    } icon: {
                        Image(systemName: "arrow.turn.down.right")
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
            }
            if !pane.error.isEmpty {
                Text(verbatim: pane.error).font(.callout).foregroundStyle(.red).textSelection(.enabled)
            }
            HStack(spacing: 6) {
                Text(pane.caption(in: language))
                if !pane.duration.isEmpty { Text(pane.duration).monospacedDigit() }
                if pane.model != (configuredModel ?? "默认模型") { Text(verbatim: pane.model) }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }
}

private struct PromptEditor: NSViewRepresentable {
    @Environment(\.interfaceLanguage) private var language
    @Binding var text: String
    let focusRequest: Int
    let send: () -> Void
    let allowImages: Bool
    let addImages: ([ImageAttachment.Source]) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = PromptTextView.scrollableTextView()
        scroll.drawsBackground = false
        scroll.contentView.addSubview(CompactScrollIndicatorView())
        if let editor = scroll.documentView as? PromptTextView {
            editor.drawsBackground = false
            editor.font = .systemFont(ofSize: 15)
            editor.isRichText = false
            editor.isAutomaticQuoteSubstitutionEnabled = false
            editor.isAutomaticDashSubstitutionEnabled = false
            editor.textContainerInset = NSSize(width: 8, height: 8)
            editor.delegate = context.coordinator
            editor.registerForDraggedTypes(editor.registeredDraggedTypes + [.fileURL] + PromptTextView.imageTypes)
        }
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let editor = scroll.documentView as? PromptTextView else { return }
        editor.setAccessibilityLabel(language.text("问题", "Prompt"))
        editor.addImages = allowImages ? addImages : nil
        if editor.string != text && !editor.hasMarkedText() { editor.string = text }
        if context.coordinator.focus != focusRequest {
            context.coordinator.focus = focusRequest
            DispatchQueue.main.async { editor.window?.makeFirstResponder(editor) }
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: PromptEditor
        var focus: Int?

        init(_ parent: PromptEditor) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            if let editor = notification.object as? NSTextView { parent.text = editor.string }
        }

        func textView(_ textView: NSTextView, doCommandBy command: Selector) -> Bool {
            let modifiers = NSApp.currentEvent?.modifierFlags ?? []
            guard command == #selector(NSResponder.insertNewline(_:)), !textView.hasMarkedText(),
                  modifiers.intersection([.shift, .option, .control]).isEmpty else { return false }
            parent.send()
            return true
        }
    }
}

private final class PromptTextView: NSTextView {
    static let imageTypes: [NSPasteboard.PasteboardType] = [.png, .tiff, .init(UTType.jpeg.identifier)]
    private static let fileOptions: [NSPasteboard.ReadingOptionKey: Any] = [
        .urlReadingFileURLsOnly: true,
        .urlReadingContentsConformToTypes: [UTType.image.identifier],
    ]

    var addImages: (([ImageAttachment.Source]) -> Void)?

    override func paste(_ sender: Any?) {
        if let addImages, let images = Self.imageSources(from: .general) {
            addImages(images)
        } else {
            super.paste(sender)
        }
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        acceptsImages(from: sender) ? .copy : super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        acceptsImages(from: sender) ? .copy : super.draggingUpdated(sender)
    }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        acceptsImages(from: sender) || super.prepareForDragOperation(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        if let addImages, let images = Self.imageSources(from: sender.draggingPasteboard) {
            addImages(images)
            return true
        }
        return super.performDragOperation(sender)
    }

    private func acceptsImages(from sender: NSDraggingInfo) -> Bool {
        let board = sender.draggingPasteboard
        return addImages != nil && (board.availableType(from: Self.imageTypes) != nil ||
               board.canReadObject(forClasses: [NSURL.self], options: Self.fileOptions))
    }

    private static func imageSources(from board: NSPasteboard) -> [ImageAttachment.Source]? {
        if let urls = board.readObjects(forClasses: [NSURL.self], options: fileOptions) as? [URL], !urls.isEmpty {
            return urls.map(ImageAttachment.Source.file)
        }
        if let type = board.availableType(from: imageTypes), let data = board.data(forType: type) {
            return [.clipboard(data)]
        }
        return nil
    }
}

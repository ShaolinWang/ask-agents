import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct Exchange: Identifiable {
    let id: Int
    let prompt: String
    let images: [ImageAttachment]
    var panes: [String: Pane]
}

@Observable
final class ConversationState {
    let id: String
    let directory: String
    var agents: [Agent]?
    var exchanges: [Exchange] = []
    var activeTurn: Int?
    var retries: [Int: (name: String, exchangeID: Int)] = [:]
    var exitingAgents: Set<String> = []
    var exitedAgents: Set<String> = []
    var stopping = false
    var prompt = ""
    var images: [ImageAttachment] = []
    var error = ""
    var provisionalItem: HistoryItem?

    init(directory: String, id: String = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()) {
        self.directory = directory
        self.id = id
    }

    var isResponding: Bool { activeTurn != nil || !retries.isEmpty }
    var busy: Bool { isResponding || !exitingAgents.isEmpty }

    func setLatestPane(_ pane: Pane, for exchangeID: Int) {
        guard exchanges.last?.id == exchangeID else { return }
        exchanges[exchanges.count - 1].panes[pane.name] = pane
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var agents: [Agent] = []
    @Published private(set) var current: ConversationState
    @Published private(set) var timeout: Double
    @Published private(set) var languagePreference: InterfaceLanguage
    @Published private(set) var configPath = ""
    @Published private(set) var installed: [String] = []
    @Published private(set) var connected = false
    @Published private(set) var focusRequest = 0
    @Published private var savedHistory: [HistoryItem] = []
    @Published private var sessions: [String: ConversationState]
    @Published private(set) var switchingConversation = false
    @Published var showingSettings = false

    private var backend: Backend?
    private var nextID = 0
    private var callbacks: [Int: (Event) -> Void] = [:]
    private var drafts: [String: (prompt: String, images: [ImageAttachment])] = [:]
    private let defaults = UserDefaults.standard

    init() {
        let initial = ConversationState(directory: defaults.string(forKey: "workspace") ?? "/")
        current = initial
        sessions = [initial.id: initial]
        languagePreference = defaults.string(forKey: "interfaceLanguage")
            .flatMap(InterfaceLanguage.init(rawValue:)) ?? .system
        let seconds = defaults.double(forKey: "timeout")
        timeout = seconds > 0 && seconds.isFinite ? seconds : 600
    }

    var language: InterfaceLanguage { languagePreference.resolved }
    var conversationAgents: [Agent] { current.agents ?? agents.filter(\.enabled) }
    var participatingAgents: [Agent] {
        conversationAgents.filter { !current.exitedAgents.contains($0.name) && !current.exitingAgents.contains($0.name) }
    }
    var history: [HistoryItem] {
        let provisional = sessions.values.compactMap(\.provisionalItem)
        let ids = Set(provisional.map(\.id))
        return (provisional + savedHistory.filter { !ids.contains($0.id) }).sorted {
            if $0.pinned != $1.pinned { return $0.pinned }
            if $0.updatedAt != $1.updatedAt { return $0.updatedAt > $1.updatedAt }
            return $0.id < $1.id
        }
    }

    func isResponding(in id: String) -> Bool { sessions[id]?.isResponding == true }
    var canSwitchConversation: Bool { connected && !switchingConversation }
    var canSend: Bool {
        canAttachImages && (!current.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !current.images.isEmpty)
    }
    var canAttachImages: Bool { canSwitchConversation && !current.busy && !participatingAgents.isEmpty }

    func start() {
        guard backend == nil else { return }
        let backend = Backend(onEvent: { [weak self] in self?.receive($0) },
                              onExit: { [weak self] chinese, english in
                                  guard let self else { return }
                                  self.disconnected(self.language.text(chinese, english))
                              })
        self.backend = backend
        do {
            try backend.start()
            connected = true
            request(Request(action: "load")) { [weak self] event in self?.applyConfiguration(event) }
            reloadHistory()
        } catch {
            disconnected(language.text("无法启动本机 Python 3。\(error.localizedDescription)",
                                       "Could not start the local Python 3 process. \(error.localizedDescription)"))
        }
    }

    func setLanguage(_ preference: InterfaceLanguage) {
        guard languagePreference != preference else { return }
        languagePreference = preference
        defaults.set(preference.rawValue, forKey: "interfaceLanguage")
    }

    func chooseDirectory() {
        guard canSwitchConversation else { return }
        let panel = NSOpenPanel()
        panel.title = language.text("选择项目目录", "Choose Workspace")
        panel.prompt = language.text("选择", "Choose")
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = false
        panel.directoryURL = URL(fileURLWithPath: current.directory, isDirectory: true)
        guard panel.runModal() == .OK, let newDirectory = panel.url?.path, newDirectory != current.directory else { return }
        let draftAgent = current.exchanges.isEmpty ? current.agents?.first?.name : nil
        startNewConversation(in: newDirectory, agentName: draftAgent)
    }

    func openSettings() {
        guard canSwitchConversation else { return }
        request(Request(action: "load")) { [weak self] event in
            self?.applyConfiguration(event)
            if event.kind == "result" { self?.showingSettings = true }
        }
    }

    func saveSettings(_ updated: [Agent], timeout: Double, reply: @escaping (String?) -> Void) {
        guard timeout.isFinite, timeout > 0 else { return reply(language.text("每轮超时须为正数", "Turn timeout must be positive")) }
        request(Request(action: "save", agents: updated, original: agents)) { [weak self] event in
            guard let self else { return }
            guard event.kind == "result" else { return reply(event.message ?? language.text("无法保存配置", "Could not save settings")) }
            self.timeout = timeout
            defaults.set(timeout, forKey: "timeout")
            applyConfiguration(event)
            reply(nil)
        }
    }

    func loadModels(for agent: Agent, reply: @escaping (Event) -> Void) {
        request(Request(action: "models", agent: agent, cwd: current.directory), reply: reply)
    }

    func newConversation() {
        startNewConversation(in: current.directory)
    }

    func newConversation(withAgent name: String) {
        startNewConversation(in: current.directory, agentName: name)
    }

    private func startNewConversation(in directory: String, agentName: String? = nil) {
        guard canSwitchConversation else { return }
        switchingConversation = true
        let next = ConversationState(directory: directory)
        request(Request(action: "reset", agentName: agentName, sessionID: next.id)) { [weak self] event in
            guard let self else { return }
            self.switchingConversation = false
            self.applyConfiguration(event)
            guard event.kind == "result" else {
                self.request(Request(action: "closeSession", sessionID: next.id))
                self.current.error = event.message ?? language.text("无法开启新对话", "Could not start a new conversation")
                return
            }
            if self.current.directory != directory { self.defaults.set(directory, forKey: "workspace") }
            if let agentName, var agent = self.agents.first(where: { $0.name == agentName }) {
                agent.enabled = true
                next.agents = [agent]
            }
            self.select(next)
        }
    }

    func reloadHistory() {
        guard connected else { return }
        request(Request(action: "history")) { [weak self] event in
            if let history = event.history { self?.savedHistory = history }
            if let message = event.message { self?.current.error = message }
        }
    }

    func setPinned(_ pinned: Bool, for id: String) {
        guard canSwitchConversation else { return }
        request(Request(action: "setPinned", conversationID: id, pinned: pinned)) { [weak self] event in
            guard let self else { return }
            guard event.kind == "result" else {
                current.error = event.message ?? language.text("无法更新置顶状态", "Could not update the pin")
                return
            }
            sessions[id]?.provisionalItem?.pinned = pinned
            if let index = savedHistory.firstIndex(where: { $0.id == id }) {
                savedHistory[index].pinned = pinned
            }
        }
    }

    func deleteHistory(_ ids: Set<String>) {
        guard canSwitchConversation, !ids.isEmpty else { return }
        switchingConversation = true
        request(Request(action: "deleteHistory", conversationIDs: Array(ids))) { [weak self] event in
            guard let self else { return }
            switchingConversation = false
            guard event.kind == "result" else {
                current.error = event.message ?? language.text("无法删除历史会话", "Could not delete conversations")
                return
            }
            for id in ids {
                sessions.removeValue(forKey: id)
                drafts.removeValue(forKey: id)
            }
            savedHistory.removeAll { ids.contains($0.id) }
            if ids.contains(current.id) {
                let next = ConversationState(directory: current.directory)
                current = next
                sessions[next.id] = next
                focusRequest += 1
            }
        }
    }

    func openHistory(_ item: HistoryItem) {
        guard canSwitchConversation else { return }
        if let live = sessions[item.id], live !== current || live.busy || live.provisionalItem != nil {
            if live !== current { select(live) }
            return
        }
        switchingConversation = true
        request(Request(action: "openHistory", conversationID: item.id, sessionID: item.id)) { [weak self] event in
            guard let self else { return }
            self.switchingConversation = false
            guard let cwd = event.cwd, let agents = event.agents, let exchanges = event.exchanges else {
                if self.sessions[item.id] == nil { self.request(Request(action: "closeSession", sessionID: item.id)) }
                self.current.error = event.message ?? language.text("无法打开历史会话", "Could not open the conversation")
                return
            }
            let next = ConversationState(directory: cwd, id: item.id)
            next.agents = agents
            next.exitedAgents = Set(event.exitedAgents ?? [])
            next.exchanges = exchanges.enumerated().map { index, exchange in
                Exchange(id: -index - 1, prompt: exchange.prompt, images: exchange.images ?? [],
                         panes: Dictionary(uniqueKeysWithValues: exchange.panes.map { ($0.name, $0) }))
            }
            self.select(next)
        }
    }

    func chooseImages() {
        guard canAttachImages else { return }
        let panel = NSOpenPanel()
        panel.title = language.text("添加图片", "Add Images")
        panel.prompt = language.text("添加", "Add")
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = true
        guard panel.runModal() == .OK else { return }
        addImages(panel.urls.map(ImageAttachment.Source.file))
        focusRequest += 1
    }

    func addImages(_ sources: [ImageAttachment.Source]) {
        guard canAttachImages else { return }
        current.error = ""
        var total = current.images.reduce(0) { $0 + $1.data.count }
        for source in sources {
            guard current.images.count < ImageAttachment.maxCount else {
                current.error = language.text("每轮最多支持 \(ImageAttachment.maxCount) 张图片",
                                      "A turn can include up to \(ImageAttachment.maxCount) images")
                break
            }
            do {
                let image = try ImageAttachment.load(source, language: language)
                guard total + image.data.count <= ImageAttachment.maxTotalBytes else {
                    current.error = language.text("每轮图片合计最多支持 10 MiB", "Images can total up to 10 MiB per turn")
                    continue
                }
                current.images.append(image)
                total += image.data.count
            } catch {
                current.error = error.localizedDescription
            }
        }
    }

    func removeImage(_ id: ImageAttachment.ID) {
        current.images.removeAll { $0.id == id }
        current.error = ""
        focusRequest += 1
    }

    func sendPrompt() {
        guard canSend else { return }
        let session = current
        let question = session.prompt
        guard question.unicodeScalars.count <= 200_000 else {
            session.error = language.text("问题最多支持 200,000 个字符", "Prompts can contain up to 200,000 characters")
            return
        }
        let attachments = session.images
        let previousAgents = session.agents
        session.agents = conversationAgents
        let requestID = nextID + 1
        let panes = participatingAgents.map { ($0.name, Pane.starting($0.name, model: $0.model)) }
        if session.exchanges.isEmpty {
            let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
            let title = trimmed.split(whereSeparator: \.isNewline).first.map(String.init)
                ?? attachments.map(\.name).joined(separator: ", ")
            session.provisionalItem = HistoryItem(id: session.id, title: String(title.prefix(80)),
                                                  cwd: session.directory, updatedAt: Date().timeIntervalSince1970)
        }
        session.exchanges.append(Exchange(id: requestID, prompt: question, images: attachments,
                                          panes: Dictionary(uniqueKeysWithValues: panes)))
        session.prompt = ""
        session.images = []
        session.error = ""
        session.activeTurn = requestID
        request(Request(action: "ask", agents: session.agents, cwd: session.directory, prompt: question,
                        images: attachments, timeout: timeout, sessionID: session.id)) { [weak self] event in
            guard let self else { return }
            if let message = event.message {
                if session.prompt.isEmpty { session.prompt = question }
                session.images = attachments
                session.exchanges.removeLast()
                session.agents = previousAgents
                session.error = message
            }
            session.activeTurn = nil
            self.finishResponse(event, in: session)
        }
    }

    func cancel() {
        guard current.isResponding, !current.stopping else { return }
        current.stopping = true
        request(Request(action: "cancel", sessionID: current.id))
    }

    func canRetryAgent(_ name: String) -> Bool {
        guard let agent = participatingAgents.first(where: { $0.name == name }),
              let exchange = current.exchanges.last else { return false }
        return canSwitchConversation && !current.stopping && !current.retries.values.contains(where: { $0.name == name }) &&
            (agent.cli != "doubao" || exchange.images.isEmpty) && exchange.panes[name]?.running == false
    }

    func retryAgent(_ name: String) {
        let session = current
        guard canRetryAgent(name), let exchange = session.exchanges.last, let previous = exchange.panes[name] else { return }
        let requestID = nextID + 1
        session.retries[requestID] = (name, exchange.id)
        session.error = ""
        session.setLatestPane(.starting(name, model: previous.model), for: exchange.id)
        request(Request(action: "retryAgent", timeout: timeout, agentName: name, sessionID: session.id)) { [weak self] event in
            guard let self else { return }
            if let message = event.message {
                session.setLatestPane(previous, for: exchange.id)
                session.error = message
            }
            session.retries.removeValue(forKey: requestID)
            self.finishResponse(event, in: session)
        }
    }

    private func finishResponse(_ event: Event, in session: ConversationState) {
        if let item = event.historyItem {
            session.provisionalItem = nil
            savedHistory.removeAll { $0.id == item.id }
            savedHistory.insert(item, at: 0)
        }
        if let message = event.historyError { session.error = message }
        if !session.isResponding {
            session.stopping = false
            if current === session { focusRequest += 1 }
        }
        if session.error.isEmpty { releaseIfIdle(session) }
    }

    func canExitAgent(_ name: String) -> Bool {
        canSwitchConversation && !current.exchanges.isEmpty && participatingAgents.contains { $0.name == name }
    }

    func exitAgent(_ name: String) {
        guard canExitAgent(name) else { return }
        let session = current
        session.exitingAgents.insert(name)
        request(Request(action: "exitAgent", agentName: name, sessionID: session.id)) { [weak self] event in
            guard let self else { return }
            if event.kind == "result" {
                session.exitedAgents.insert(name)
                if let message = event.historyError { session.error = message }
            } else {
                session.error = event.message ?? language.text("无法退出该 Agent", "Could not remove this agent")
            }
            session.exitingAgents.remove(name)
            if session.error.isEmpty { self.releaseIfIdle(session) }
        }
    }

    private func request(_ value: Request, reply: ((Event) -> Void)? = nil) {
        nextID += 1
        var value = value
        value.id = nextID
        value.language = language.rawValue
        guard connected, let backend else {
            let message = language.text("本机 CLI 连接不可用", "The local CLI connection is unavailable")
            reply?(Event(id: value.id, kind: "error", message: message))
            return
        }
        callbacks[value.id] = reply
        do {
            try backend.send(value)
        } catch {
            let message = error.localizedDescription
            callbacks.removeValue(forKey: value.id)?(Event(id: value.id, kind: "error", message: message))
        }
    }

    private func select(_ next: ConversationState) {
        let previous = current
        current = next
        sessions[next.id] = next
        // An explicit reopen of the selected history item refreshes it from disk.
        if previous.id == next.id {
            next.prompt = previous.prompt
            next.images = previous.images
        } else {
            releaseIfIdle(previous)
            let key = next.exchanges.isEmpty && next.provisionalItem == nil ? "" : next.id
            if let draft = drafts.removeValue(forKey: key) {
                next.prompt = draft.prompt
                next.images = draft.images
            }
        }
        focusRequest += 1
    }

    private func releaseIfIdle(_ session: ConversationState) {
        guard session !== current, !session.busy, session.provisionalItem == nil || session.exchanges.isEmpty else { return }
        let key = session.exchanges.isEmpty ? "" : session.id
        drafts[key] = session.prompt.isEmpty && session.images.isEmpty ? nil : (session.prompt, session.images)
        sessions.removeValue(forKey: session.id)
        request(Request(action: "closeSession", sessionID: session.id))
    }

    private func applyConfiguration(_ event: Event) {
        if let updated = event.agents { agents = updated }
        if let installed = event.installed { self.installed = installed }
        if let path = event.configPath { configPath = path }
        if let message = event.message { current.error = message }
    }

    private func receive(_ event: Event) {
        if event.kind == "panes", let panes = event.panes {
            guard let id = event.sessionID, let session = sessions[id] else { return }
            if let retry = session.retries[event.id], let pane = panes.first(where: { $0.name == retry.name }) {
                session.setLatestPane(pane, for: retry.exchangeID)
            } else if session.exchanges.last?.id == event.id {
                var updated = session.exchanges[session.exchanges.count - 1].panes
                let retrying = Set(session.retries.values.map(\.name))
                for pane in panes where !retrying.contains(pane.name) {
                    updated[pane.name] = pane
                }
                session.exchanges[session.exchanges.count - 1].panes = updated
            }
        } else {
            callbacks.removeValue(forKey: event.id)?(event)
        }
    }

    private func disconnected(_ message: String) {
        connected = false
        let pending = callbacks
        callbacks.removeAll()
        for (id, callback) in pending {
            callback(Event(id: id, kind: "error", message: message))
        }
        current.error = message
    }
}

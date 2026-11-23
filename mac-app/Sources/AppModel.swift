import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct Exchange: Identifiable {
    let id: Int
    let prompt: String
    let images: [ImageAttachment]
    var panes: [String: Pane]
}

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var agents: [Agent] = []
    @Published private(set) var exchanges: [Exchange] = []
    @Published private(set) var directory: String
    @Published private(set) var timeout: Double
    @Published private(set) var languagePreference: InterfaceLanguage
    @Published private(set) var configPath = ""
    @Published private(set) var installed: [String] = []
    @Published private(set) var connected = false
    @Published private(set) var stopping = false
    @Published private(set) var activeTurn: Int?
    @Published private(set) var focusRequest = 0
    @Published private(set) var history: [HistoryItem] = []
    @Published private(set) var selectedHistoryID: String?
    @Published private(set) var switchingConversation = false
    @Published private(set) var conversationViewID = 0
    @Published private(set) var exitingAgents: Set<String> = []
    @Published private(set) var exitedAgents: Set<String> = []
    @Published private var retries: [Int: (name: String, exchangeID: Int)] = [:]
    @Published var prompt = ""
    @Published private(set) var images: [ImageAttachment] = []
    @Published var error = ""
    @Published var showingSettings = false

    private var backend: Backend?
    private var nextID = 0
    private var callbacks: [Int: (Event) -> Void] = [:]
    private var conversationAgentSnapshot: [Agent]?
    private var drafts: [String: (prompt: String, images: [ImageAttachment])] = [:]
    private let defaults = UserDefaults.standard

    init() {
        directory = defaults.string(forKey: "workspace") ?? "/"
        languagePreference = defaults.string(forKey: "interfaceLanguage")
            .flatMap(InterfaceLanguage.init(rawValue:)) ?? .system
        let seconds = defaults.double(forKey: "timeout")
        timeout = seconds > 0 && seconds.isFinite ? seconds : 600
    }

    var language: InterfaceLanguage { languagePreference.resolved }
    var enabledAgents: [Agent] { agents.filter(\.enabled) }
    var conversationAgents: [Agent] { conversationAgentSnapshot ?? enabledAgents }
    var participatingAgents: [Agent] {
        conversationAgents.filter { !exitedAgents.contains($0.name) && !exitingAgents.contains($0.name) }
    }
    var isResponding: Bool { activeTurn != nil || !retries.isEmpty }
    var canSwitchConversation: Bool {
        connected && !isResponding && !switchingConversation && exitingAgents.isEmpty
    }
    var canSend: Bool {
        canAttachImages && (!prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !images.isEmpty)
    }
    var canAttachImages: Bool { canSwitchConversation && !participatingAgents.isEmpty }

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
        panel.directoryURL = URL(fileURLWithPath: directory, isDirectory: true)
        guard panel.runModal() == .OK, let newDirectory = panel.url?.path, newDirectory != directory else { return }
        let draftAgent = exchanges.isEmpty ? conversationAgentSnapshot?.first?.name : nil
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
        request(Request(action: "models", agent: agent, cwd: directory), reply: reply)
    }

    func newConversation() {
        startNewConversation(in: directory)
    }

    func newConversation(withAgent name: String) {
        startNewConversation(in: directory, agentName: name)
    }

    private func startNewConversation(in directory: String, agentName: String? = nil) {
        guard canSwitchConversation else { return }
        switchingConversation = true
        request(Request(action: "reset", agentName: agentName)) { [weak self] event in
            guard let self else { return }
            self.switchingConversation = false
            self.applyConfiguration(event)
            guard event.kind == "result" else {
                self.error = event.message ?? language.text("无法开启新对话", "Could not start a new conversation")
                return
            }
            if self.directory != directory {
                self.directory = directory
                self.defaults.set(directory, forKey: "workspace")
            }
            self.exchanges = []
            self.switchDraft(to: nil)
            if let agentName, var agent = self.agents.first(where: { $0.name == agentName }) {
                agent.enabled = true
                self.conversationAgentSnapshot = [agent]
            } else {
                self.conversationAgentSnapshot = nil
            }
            self.exitedAgents = []
            self.conversationViewID += 1
            self.error = ""
            self.focusRequest += 1
        }
    }

    func reloadHistory() {
        guard connected else { return }
        request(Request(action: "history")) { [weak self] event in
            if let history = event.history { self?.history = history }
            if let message = event.message { self?.error = message }
        }
    }

    func openHistory(_ item: HistoryItem) {
        guard canSwitchConversation else { return }
        switchingConversation = true
        request(Request(action: "openHistory", conversationID: item.id)) { [weak self] event in
            guard let self else { return }
            self.switchingConversation = false
            guard let cwd = event.cwd, let agents = event.agents, let exchanges = event.exchanges else {
                self.error = event.message ?? language.text("无法打开历史会话", "Could not open the conversation")
                return
            }
            self.directory = cwd
            self.conversationAgentSnapshot = agents
            self.exitedAgents = Set(event.exitedAgents ?? [])
            self.exchanges = exchanges.enumerated().map { index, exchange in
                Exchange(id: -index - 1, prompt: exchange.prompt, images: exchange.images ?? [],
                         panes: Dictionary(uniqueKeysWithValues: exchange.panes.map { ($0.name, $0) }))
            }
            self.switchDraft(to: item.id)
            self.conversationViewID += 1
            self.error = ""
            self.focusRequest += 1
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
        error = ""
        var total = images.reduce(0) { $0 + $1.data.count }
        for source in sources {
            guard images.count < ImageAttachment.maxCount else {
                error = language.text("每轮最多支持 \(ImageAttachment.maxCount) 张图片",
                                      "A turn can include up to \(ImageAttachment.maxCount) images")
                break
            }
            do {
                let image = try ImageAttachment.load(source, language: language)
                guard total + image.data.count <= ImageAttachment.maxTotalBytes else {
                    error = language.text("每轮图片合计最多支持 10 MiB", "Images can total up to 10 MiB per turn")
                    continue
                }
                images.append(image)
                total += image.data.count
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    func removeImage(_ id: ImageAttachment.ID) {
        images.removeAll { $0.id == id }
        error = ""
        focusRequest += 1
    }

    func sendPrompt() {
        guard canSend else { return }
        let question = prompt
        guard question.unicodeScalars.count <= 200_000 else {
            error = language.text("问题最多支持 200,000 个字符", "Prompts can contain up to 200,000 characters")
            return
        }
        let attachments = images
        let agents = conversationAgents
        let firstTurn = conversationAgentSnapshot == nil
        conversationAgentSnapshot = agents
        let requestID = nextID + 1
        let panes = participatingAgents.map {
            ($0.name, Pane.starting($0.name, model: $0.model))
        }
        exchanges.append(Exchange(id: requestID, prompt: question, images: attachments,
                                  panes: Dictionary(uniqueKeysWithValues: panes)))
        prompt = ""
        images = []
        error = ""
        activeTurn = requestID
        request(Request(action: "ask", agents: agents, cwd: directory, prompt: question,
                        images: attachments, timeout: timeout)) { [weak self] event in
            guard let self else { return }
            if let message = event.message {
                if self.prompt.isEmpty { self.prompt = question }
                self.images = attachments
                if self.exchanges.last?.id == requestID { self.exchanges.removeLast() }
                if firstTurn { self.conversationAgentSnapshot = nil }
                self.error = message
            }
            self.applyHistoryResult(event)
            activeTurn = nil
            self.finishResponse()
        }
    }

    func cancel() {
        guard isResponding, !stopping else { return }
        stopping = true
        request(Request(action: "cancel"))
    }

    func canRetryAgent(_ name: String) -> Bool {
        guard let agent = participatingAgents.first(where: { $0.name == name }),
              let exchange = exchanges.last else { return false }
        return connected && !switchingConversation && !stopping && !retries.values.contains(where: { $0.name == name }) &&
            (agent.cli != "doubao" || exchange.images.isEmpty) && exchange.panes[name]?.running == false
    }

    func retryAgent(_ name: String) {
        guard canRetryAgent(name), let exchange = exchanges.last, let previous = exchange.panes[name] else { return }
        let requestID = nextID + 1
        retries[requestID] = (name, exchange.id)
        error = ""
        setLatestPane(.starting(name, model: previous.model), for: exchange.id)
        request(Request(action: "retryAgent", timeout: timeout, agentName: name)) { [weak self] event in
            guard let self else { return }
            if let message = event.message {
                self.setLatestPane(previous, for: exchange.id)
                self.error = message
            }
            self.applyHistoryResult(event)
            self.retries.removeValue(forKey: requestID)
            self.finishResponse()
        }
    }

    private func setLatestPane(_ pane: Pane, for exchangeID: Int) {
        guard exchanges.last?.id == exchangeID else { return }
        exchanges[exchanges.count - 1].panes[pane.name] = pane
    }

    private func applyHistoryResult(_ event: Event) {
        if let item = event.historyItem {
            selectedHistoryID = item.id
            history.removeAll { $0.id == item.id }
            history.insert(item, at: 0)
        }
        if let message = event.historyError { error = message }
    }

    private func finishResponse() {
        if !isResponding {
            stopping = false
            focusRequest += 1
        }
    }

    func canExitAgent(_ name: String) -> Bool {
        connected && !switchingConversation && !exchanges.isEmpty &&
            participatingAgents.contains { $0.name == name }
    }

    func exitAgent(_ name: String) {
        guard canExitAgent(name) else { return }
        exitingAgents.insert(name)
        request(Request(action: "exitAgent", agentName: name)) { [weak self] event in
            guard let self else { return }
            if event.kind == "result" {
                self.exitedAgents.insert(name)
                if let message = event.historyError { self.error = message }
            } else {
                self.error = event.message ?? language.text("无法退出该 Agent", "Could not remove this agent")
            }
            self.exitingAgents.remove(name)
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

    private func switchDraft(to id: String?) {
        guard selectedHistoryID != id else { return }
        drafts[selectedHistoryID ?? ""] = prompt.isEmpty && images.isEmpty ? nil : (prompt, images)
        selectedHistoryID = id
        let draft = drafts.removeValue(forKey: id ?? "")
        prompt = draft?.prompt ?? ""
        images = draft?.images ?? []
    }

    private func applyConfiguration(_ event: Event) {
        if let updated = event.agents { agents = updated }
        if let installed = event.installed { self.installed = installed }
        if let path = event.configPath { configPath = path }
        if let message = event.message { error = message }
    }

    private func receive(_ event: Event) {
        if event.kind == "panes", let panes = event.panes {
            if let retry = retries[event.id], let pane = panes.first(where: { $0.name == retry.name }) {
                setLatestPane(pane, for: retry.exchangeID)
            } else if exchanges.last?.id == event.id {
                var updated = exchanges[exchanges.count - 1].panes
                let retrying = Set(retries.values.map(\.name))
                for pane in panes where !retrying.contains(pane.name) {
                    updated[pane.name] = pane
                }
                exchanges[exchanges.count - 1].panes = updated
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
        error = message
    }
}

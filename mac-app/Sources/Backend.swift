import Foundation

struct Agent: Codable, Equatable, Identifiable {
    var id: String { name }
    var name: String
    var cli: String
    var model: String?
    var executable: String?
    var enabled: Bool
    var modelName: String? = nil

    func modelDisplayName(in language: InterfaceLanguage) -> String {
        modelName ?? (model == nil ? language.text("默认模型", "Default model") : language.text("未命名模型", "Unnamed model"))
    }

    enum CodingKeys: String, CodingKey {
        case name, cli, model, executable, enabled
        case modelName = "model_name"
    }

    static let providers = ["codex", "traex", "claude", "doubao"]

    static func title(_ cli: String) -> String {
        switch cli {
        case "codex": "Codex"
        case "traex": "TraeX"
        case "claude": "Claude Code"
        case "doubao": "Doubao"
        default: cli
        }
    }
}

struct ModelChoice: Decodable, Identifiable {
    let id: String
    let label: String?

    func displayName(in language: InterfaceLanguage) -> String {
        label ?? language.text("未命名模型", "Unnamed model")
    }
}

struct HistoryItem: Decodable, Identifiable {
    let id: String
    let title: String
    let cwd: String
    let updatedAt: Double
    var pinned = false

    enum CodingKeys: String, CodingKey {
        case id, title, cwd, pinned
        case updatedAt = "updated_at"
    }
}

struct SavedExchange: Decodable {
    let prompt: String
    let images: [ImageAttachment]?
    let panes: [Pane]
}

struct Pane: Decodable, Equatable {
    struct Entry: Decodable, Equatable {
        let activity: String?
        let text: String?
        let activityEnglish: String?
    }

    let name: String
    var model: String
    var status: String
    var error = ""
    var duration = ""
    var phase = ""
    var statusEnglish: String? = nil
    var phaseEnglish: String? = nil
    var entries: [Entry] = []

    var running: Bool { status == "运行中" || status == "等待启动" }

    static func starting(_ name: String, model: String?) -> Self {
        Pane(name: name, model: model ?? "默认模型", status: "等待启动", statusEnglish: "Starting")
    }

    func caption(in language: InterfaceLanguage) -> String {
        if running && !phase.isEmpty {
            return language.text(phase, phaseEnglish ?? phase)
        }
        return language.text(status, statusEnglish ?? status)
    }
}

struct Request: Encodable {
    var id = 0
    let action: String
    var language: String?
    var agents: [Agent]?
    var original: [Agent]?
    var agent: Agent?
    var cwd: String?
    var prompt: String?
    var images: [ImageAttachment]?
    var timeout: Double?
    var conversationID: String?
    var agentName: String?
    var sessionID: String?
    var conversationIDs: [String]?
    var pinned: Bool?
}

struct Event: Decodable {
    let id: Int
    let kind: String
    var sessionID: String?
    var message: String?
    var agents: [Agent]?
    var configPath: String?
    var installed: [String]?
    var models: [ModelChoice]?
    var panes: [Pane]?
    var history: [HistoryItem]?
    var historyItem: HistoryItem?
    var historyError: String?
    var cwd: String?
    var exchanges: [SavedExchange]?
    var exitedAgents: [String]?
}

final class Backend {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private let writes = DispatchQueue(label: "ask-agents.backend.input")
    private let onEvent: @MainActor (Event) -> Void
    private let onExit: @MainActor (String, String) -> Void

    init(onEvent: @escaping @MainActor (Event) -> Void, onExit: @escaping @MainActor (String, String) -> Void) {
        self.onEvent = onEvent
        self.onExit = onExit
    }

    func start() throws {
        guard let resources = Bundle.main.resourceURL else { throw CocoaError(.fileNoSuchFile) }
        signal(SIGPIPE, SIG_IGN)
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-E", "-B", "-u", "-m", "ask_agents.desktop"]
        process.currentDirectoryURL = resources
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.standardError
        try process.run()

        let reader = output.fileHandleForReading
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let decoder = JSONDecoder()
            var buffer = Data()
            var scanned = 0
            while true {
                let bytes = reader.availableData
                if bytes.isEmpty { break }
                buffer.append(bytes)
                var messages: [Event] = []
                var searchStart = buffer.startIndex + scanned
                while let newline = buffer[searchStart...].firstIndex(of: 10) {
                    if let event = try? decoder.decode(Event.self, from: buffer[..<newline]) {
                        messages.append(event)
                    }
                    buffer.removeSubrange(...newline)
                    searchStart = buffer.startIndex
                }
                scanned = buffer.count
                let events = messages
                if !events.isEmpty {
                    DispatchQueue.main.async { events.forEach(self.onEvent) }
                }
            }
            DispatchQueue.main.async {
                self.onExit("连接本机 CLI 的进程已退出，请重新打开 Ask Agents。",
                            "The local CLI connection closed. Reopen Ask Agents to reconnect.")
            }
        }
    }

    func send(_ request: Request) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = .withoutEscapingSlashes
        var data = try encoder.encode(request)
        data.append(10)
        let bytes = data
        writes.async { [self] in
            do {
                try input.fileHandleForWriting.write(contentsOf: bytes)
            } catch {
                DispatchQueue.main.async {
                    self.onExit("无法连接本机 CLI。\(error.localizedDescription)",
                                "Could not connect to the local CLI. \(error.localizedDescription)")
                }
            }
        }
    }
}

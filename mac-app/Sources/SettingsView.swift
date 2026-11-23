import AppKit
import SwiftUI

private struct AgentDraft: Identifiable {
    let id = UUID()
    var agent: Agent
}

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.interfaceLanguage) private var language
    @State private var drafts: [AgentDraft]
    @State private var selection: UUID?
    @State private var timeoutText: String
    @State private var languagePreference: InterfaceLanguage
    @State private var saving = false
    @State private var error = ""

    init(model: AppModel) {
        self.model = model
        let drafts = model.agents.map(AgentDraft.init(agent:))
        _drafts = State(initialValue: drafts)
        _selection = State(initialValue: drafts.first(where: { $0.agent.enabled })?.id ?? drafts.first?.id)
        let seconds = String(model.timeout)
        _timeoutText = State(initialValue: seconds.hasSuffix(".0") ? String(seconds.dropLast(2)) : seconds)
        _languagePreference = State(initialValue: model.languagePreference)
    }

    private var selectedIndex: Int? { drafts.firstIndex { $0.id == selection } }
    private var enabledCount: Int { drafts.filter(\.agent.enabled).count }
    private var timeout: Double? {
        let separator = Locale.current.decimalSeparator ?? "."
        let text = timeoutText.trimmingCharacters(in: .whitespacesAndNewlines)
        let number = Double(text.replacingOccurrences(of: separator, with: ".")) ??
            (try? FloatingPointFormatStyle<Double>().parseStrategy.parse(text))
        guard let number, number.isFinite, number > 0 else { return nil }
        return number
    }
    private var changed: Bool {
        drafts.map(\.agent) != model.agents || timeout != model.timeout || languagePreference != model.languagePreference
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 18) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(language.text("Agent 设置", "Agent Settings")).font(.title2.weight(.semibold))
                    Text(language.text("保存的配置将用于之后的每段新对话。", "Saved agent settings apply to new conversations."))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Picker(language.text("界面语言", "Language"), selection: $languagePreference) {
                    Text(language.text("跟随系统", "System")).tag(InterfaceLanguage.system)
                    Text(verbatim: "简体中文").tag(InterfaceLanguage.chinese)
                    Text(verbatim: "English").tag(InterfaceLanguage.english)
                }
                .fixedSize()
                .disabled(saving)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 17)

            Divider()
            HStack(spacing: 0) {
                sidebar.frame(width: 236)
                Divider()
                if let index = selectedIndex {
                    ScrollView {
                        AgentEditor(agent: $drafts[index].agent, model: model, duplicate: duplicate, remove: remove)
                            .id(drafts[index].id)
                            .padding(22)
                            .compactEnclosingScrollIndicators()
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color(nsColor: .textBackgroundColor))
                } else {
                    VStack(spacing: 10) {
                        Image(systemName: "terminal").font(.system(size: 28, weight: .light))
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        if drafts.isEmpty {
                            Text(language.text("添加一个 Agent 开始配置", "Add an agent to get started")).font(.headline)
                            addMenu
                        } else {
                            Text(language.text("选择左侧实例查看配置", "Select an agent to view its settings")).font(.headline)
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color(nsColor: .textBackgroundColor))
                }
            }
            .disabled(saving)
            Divider()
            footer
        }
        .frame(width: 780, height: 548)
        .background(Color(nsColor: .windowBackgroundColor))
        .interactiveDismissDisabled(saving)
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack {
                Text(language.text("实例", "Agents")).font(.headline)
                Spacer()
                Text(language.text("已启用 \(enabledCount)", "\(enabledCount) enabled"))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 14)
            .padding(.top, 14)
            .padding(.bottom, 8)

            ScrollViewReader { proxy in
                List(selection: $selection) {
                    ForEach($drafts) { $draft in
                        agentRow($draft).tag(draft.id)
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .onChange(of: drafts.count) {
                    if let selection { proxy.scrollTo(selection) }
                }
            }

            Divider()
            addMenu
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 13)
                .padding(.vertical, 9)
        }
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func agentRow(_ draft: Binding<AgentDraft>) -> some View {
        let agent = draft.wrappedValue.agent
        let name = agent.name.isEmpty ? language.text("未命名", "Unnamed") : agent.name
        return HStack(spacing: 9) {
            AgentIcon(agent: agent, size: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text(verbatim: name)
                    .font(.callout.weight(.semibold))
                    .lineLimit(1)
                Text("\(Agent.title(agent.cli)) · \(agent.modelDisplayName(in: language))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .truncationMode(.middle)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Toggle(language.text("启用 \(name)", "Enable \(name)"), isOn: draft.agent.enabled)
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.mini)
        }
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .compactEnclosingScrollIndicators()
    }

    private var addMenu: some View {
        Menu {
            ForEach(Agent.providers, id: \.self) { cli in
                Button(Agent.title(cli)) {
                    add(Agent(name: cli, cli: cli, enabled: true))
                }
            }
        } label: {
            Label(language.text("添加 Agent", "Add Agent"), systemImage: "plus")
        }
        .fixedSize()
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !error.isEmpty {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
            HStack(alignment: .center) {
                HStack(spacing: 7) {
                    Text(language.text("每轮超时", "Turn timeout")).font(.callout)
                    TextField(language.text("每轮超时", "Turn timeout"), text: $timeoutText, prompt: Text("600"))
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 68)
                    Text(language.text("秒", "sec")).font(.callout).foregroundStyle(.secondary)
                }
                .disabled(saving)

                Spacer()
                Button(language.text("取消", "Cancel")) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                Button(saving ? language.text("保存中…", "Saving…")
                       : changed ? language.text("保存", "Save") : language.text("完成", "Done"), action: save)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving)
            }
            Text(verbatim: (model.configPath as NSString).abbreviatingWithTildeInPath)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
                .help(model.configPath)
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 12)
    }

    private func add(_ value: Agent) {
        let names = Set(drafts.map(\.agent.name))
        let base = value.name.isEmpty ? value.cli : value.name
        var name = base
        var number = 2
        while names.contains(name) {
            let suffix = "-\(number)"
            name = String(base.prefix(32 - suffix.count)) + suffix
            number += 1
        }
        var agent = value
        agent.name = name
        agent.enabled = true
        let draft = AgentDraft(agent: agent)
        drafts.append(draft)
        selection = draft.id
    }

    private func duplicate() {
        if let index = selectedIndex { add(drafts[index].agent) }
    }

    private func remove() {
        guard let index = selectedIndex else { return }
        drafts.remove(at: index)
        selection = drafts.isEmpty ? nil : drafts[min(index, drafts.count - 1)].id
    }

    private func save() {
        guard changed else { return dismiss() }
        guard let timeout else {
            error = language.text("每轮超时须为大于 0 的秒数。", "Enter a timeout greater than zero seconds.")
            return
        }
        error = ""
        let updated = drafts.map(\.agent)
        if updated == model.agents && timeout == model.timeout {
            model.setLanguage(languagePreference)
            dismiss()
            return
        }
        saving = true
        model.saveSettings(updated, timeout: timeout) { message in
            saving = false
            if let message {
                error = message
            } else {
                model.setLanguage(languagePreference)
                dismiss()
            }
        }
    }
}

private struct AgentIcon: View {
    let agent: Agent
    let size: CGFloat

    var body: some View {
        Image(systemName: "terminal")
            .font(.system(size: size * 0.43, weight: .semibold))
            .foregroundStyle(agent.tint)
            .frame(width: size, height: size)
            .background(agent.tint.opacity(0.12), in: RoundedRectangle(cornerRadius: size * 0.25))
            .accessibilityHidden(true)
    }
}

private struct AgentEditor: View {
    @Environment(\.interfaceLanguage) private var language
    @Binding var agent: Agent
    let model: AppModel
    let duplicate: () -> Void
    let remove: () -> Void

    @State private var showingModels = false
    @State private var showingAdvanced = false
    @State private var catalog: ModelCatalog = .idle
    @State private var catalogRequest: UUID?

    private var missingCLI: Bool { agent.executable == nil && !model.installed.contains(agent.cli) }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 11) {
                AgentIcon(agent: agent, size: 42)
                VStack(alignment: .leading, spacing: 3) {
                    Text(verbatim: agent.name.isEmpty ? language.text("未命名", "Unnamed") : agent.name)
                        .font(.title3.weight(.semibold))
                        .lineLimit(1)
                    Text(Agent.title(agent.cli)).font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button(action: duplicate) { Label(language.text("复制", "Duplicate"), systemImage: "plus.square.on.square") }
                    .help(language.text("复制当前实例", "Duplicate this agent"))
                Button(role: .destructive, action: remove) { Label(language.text("移除", "Remove"), systemImage: "trash") }
                    .help(language.text("移除当前实例", "Remove this agent"))
            }

            Divider()
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(language.text("实例名称", "Agent name")).font(.callout.weight(.medium))
                    TextField(language.text("实例名称", "Agent name"), text: $agent.name, prompt: Text(agent.cli))
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("CLI").font(.callout.weight(.medium))
                    Picker("CLI", selection: selectedCLI) {
                        ForEach(Agent.providers, id: \.self) { cli in
                            Text(Agent.title(cli)).tag(cli)
                        }
                    }
                    .labelsHidden()
                }
                .frame(width: 138, alignment: .leading)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text(language.text("模型", "Model")).font(.callout.weight(.medium))
                HStack(spacing: 8) {
                    Text(verbatim: agent.modelDisplayName(in: language))
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, minHeight: 24, alignment: .leading)
                        .padding(.horizontal, 8)
                        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                    Button {
                        showingModels.toggle()
                        if case .idle = catalog { fetchModels() }
                    } label: {
                        Label(language.text("选择模型", "Choose Model"), systemImage: "list.bullet")
                    }
                    .popover(isPresented: $showingModels, arrowEdge: .top) {
                        ModelPicker(catalog: catalog, selected: agent.model, retry: fetchModels,
                                    enterManually: showModelID, choose: selectModel)
                    }
                }
            }

            Divider()
            DisclosureGroup(isExpanded: $showingAdvanced) {
                VStack(alignment: .leading, spacing: 12) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(language.text("模型 ID", "Model ID")).font(.callout.weight(.medium))
                        TextField(language.text("模型 ID", "Model ID"), text: manualModelID,
                                  prompt: Text(language.text("使用 CLI 默认模型", "Use the CLI default model")))
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder)
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        Text(language.text("CLI 可执行文件", "CLI executable")).font(.callout.weight(.medium))
                        HStack(spacing: 8) {
                            TextField(language.text("CLI 可执行文件", "CLI executable"), text: optionalText($agent.executable),
                                      prompt: Text(language.text("自动查找", "Detect automatically")))
                                .labelsHidden()
                                .textFieldStyle(.roundedBorder)
                            Button(language.text("浏览…", "Browse…"), action: chooseExecutable)
                        }
                    }
                }
                .padding(.top, 10)
            } label: {
                HStack {
                    Text(language.text("高级", "Advanced")).font(.callout.weight(.medium))
                    if !showingAdvanced && agent.executable != nil {
                        Text(language.text("自定义 CLI 路径", "Custom CLI path")).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }

            if missingCLI {
                Label(language.text("未找到 \(Agent.title(agent.cli)) CLI，可在高级设置中指定路径。",
                                    "\(Agent.title(agent.cli)) CLI was not found. Set its path under Advanced."),
                      systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .onChange(of: agent) { previous, current in
            if previous.cli != current.cli || previous.executable != current.executable {
                resetCatalog()
            }
        }
    }

    private var selectedCLI: Binding<String> {
        Binding(get: { agent.cli }, set: { cli in
            guard cli != agent.cli else { return }
            agent = Agent(name: agent.name, cli: cli, enabled: agent.enabled)
        })
    }

    private func optionalText(_ value: Binding<String?>) -> Binding<String> {
        Binding(get: { value.wrappedValue ?? "" }, set: { value.wrappedValue = $0.isEmpty ? nil : $0 })
    }

    private var manualModelID: Binding<String> {
        Binding(get: { agent.model ?? "" }, set: { value in
            let id = value.isEmpty ? nil : value
            guard agent.model != id else { return }
            agent.model = id
            agent.modelName = nil
        })
    }

    private func showModelID() {
        showingModels = false
        showingAdvanced = true
    }

    private func selectModel(_ choice: ModelChoice?) {
        agent.model = choice?.id
        agent.modelName = choice?.label
        showingModels = false
    }

    private func chooseExecutable() {
        let panel = NSOpenPanel()
        panel.title = language.text("选择 CLI 可执行文件", "Choose CLI Executable")
        panel.prompt = language.text("选择", "Choose")
        panel.canChooseDirectories = false
        if let executable = agent.executable {
            let url = URL(fileURLWithPath: (executable as NSString).expandingTildeInPath)
            panel.directoryURL = url.deletingLastPathComponent()
        }
        if panel.runModal() == .OK { agent.executable = panel.url?.path }
    }

    private func resetCatalog() {
        catalogRequest = nil
        catalog = .idle
        if showingModels { fetchModels() }
    }

    private func fetchModels() {
        let request = UUID()
        catalogRequest = request
        catalog = .loading
        model.loadModels(for: agent) { event in
            guard catalogRequest == request else { return }
            if let message = event.message {
                catalog = .failed(message)
            } else {
                catalog = .loaded(event.models ?? [])
            }
        }
    }
}

private enum ModelCatalog {
    case idle
    case loading
    case loaded([ModelChoice])
    case failed(String)
}

private struct ModelPicker: View {
    @Environment(\.interfaceLanguage) private var language
    let catalog: ModelCatalog
    let selected: String?
    let retry: () -> Void
    let enterManually: () -> Void
    let choose: (ModelChoice?) -> Void

    @State private var query = ""
    @FocusState private var focused: Bool

    private var search: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func matches(in choices: [ModelChoice]) -> [ModelChoice] {
        choices.filter {
            search.isEmpty || $0.displayName(in: language).localizedCaseInsensitiveContains(search) ||
                $0.id.localizedCaseInsensitiveContains(search)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            searchField
            option(nil)
            Divider()

            switch catalog {
            case .idle, .loading:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(language.text("正在读取模型…", "Loading models…")).foregroundStyle(.secondary)
                }
                .padding(6)
            case .failed(let message):
                notice(message, retry: true)
            case .loaded(let choices):
                if choices.isEmpty {
                    notice(language.text("CLI 未返回可选模型。", "The CLI returned no models."), retry: true)
                } else {
                    let results = matches(in: choices)
                    if results.isEmpty {
                        notice(language.text("没有匹配的模型。", "No matching models."), retry: false)
                    } else {
                        ScrollView {
                            LazyVStack(spacing: 3) {
                                ForEach(results) { choice in
                                    option(choice)
                                }
                            }
                            .compactEnclosingScrollIndicators()
                        }
                        .frame(height: min(CGFloat(results.count) * 35, 245))
                    }
                }
            }
            Button(language.text("手动输入模型 ID…", "Enter Model ID…"), action: enterManually)
                .buttonStyle(.borderless)
        }
        .padding(13)
        .frame(width: 340)
        .onAppear { focused = true }
    }

    private var searchField: some View {
        HStack(spacing: 8) {
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField(language.text("搜索模型", "Search models"), text: $query).textFieldStyle(.plain)
                    .focused($focused)
                    .onSubmit {
                        guard !search.isEmpty, case .loaded(let choices) = catalog else { return }
                        let results = matches(in: choices)
                        if results.count == 1 { choose(results[0]) }
                    }
            }
            .padding(8)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 6))

            if case .loaded(let choices) = catalog, !choices.isEmpty {
                Button(action: retry) {
                    Image(systemName: "arrow.clockwise").accessibilityLabel(language.text("重新读取模型", "Reload models"))
                }
                .help(language.text("重新读取模型", "Reload models"))
            }
        }
    }

    private func option(_ choice: ModelChoice?) -> some View {
        let isSelected = selected == choice?.id
        return Button { choose(choice) } label: {
            HStack(spacing: 8) {
                Text(verbatim: choice?.displayName(in: language) ?? language.text("使用 CLI 默认模型", "Use the CLI default model")).lineLimit(1)
                Spacer()
                if isSelected {
                    Image(systemName: "checkmark").foregroundStyle(Color.accentColor)
                        .accessibilityHidden(true)
                }
            }
            .padding(.horizontal, 8)
            .frame(minHeight: 32)
            .background(isSelected ? Color.accentColor.opacity(0.12) : .clear,
                        in: RoundedRectangle(cornerRadius: 5))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }

    private func notice(_ message: String, retry canRetry: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(verbatim: message)
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(4)
                .textSelection(.enabled)
            if canRetry { Button(language.text("重新读取", "Reload"), action: retry).controlSize(.small) }
        }
        .padding(6)
    }
}

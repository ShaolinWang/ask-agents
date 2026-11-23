# Ask Agents

[English](README.md)

把同一个问题发给多个 Agent CLI，并排比较回复。用同一个输入框追问时，每个 Agent 都延续自己的对话。也可以为同一个 CLI 配置多个实例，比较不同模型的回答。

Ask Agents 提供 macOS 原生应用和终端界面。两者都复用本机已安装 CLI 的登录状态，并共用本地配置和会话历史。

## 开始使用

先安装并登录至少一个[受支持的 CLI](#支持的-cli)。以下命令从仓库根目录运行。 [Codex CLI 安装说明](https://learn.chatgpt.com/docs/codex/cli) · [Claude Code 安装说明](https://code.claude.com/docs/en/setup#install-claude-code)

### macOS 应用

需要 macOS 14 或更新版本、Xcode 或 Command Line Tools，以及位于 `/usr/bin/python3` 的 Python 3.9 或更新版本。

```sh
bash mac-app/build.sh
open 'dist/Ask Agents.app'
```

1. 打开「Agent 设置」，添加并启用要使用的 Agent，选择模型后保存。
2. 点文件夹按钮选择工作目录。应用初始使用 `/`，之后会记住你选择的目录。
3. 输入问题，按 Return 发送。按 Shift + Return 换行。

如果应用找不到 CLI，在「Agent 设置 → 高级」填写可执行文件路径。

### 终端

需要 Python 3.9 或更新版本。交互界面需要带 TTY 的终端。

```sh
./ask-agents
```

首次运行时，用方向键移动、空格启用 Agent、**M** 选择模型。按 Enter 保存并开始提问。之后可用 `./ask-agents config` 修改配置。

如果已安装 Codex 和 Claude Code，下面的命令会把同一个问题发给两者，并在输出中标记每条回复来自哪个 Agent。

```sh
./ask-agents add codex codex
./ask-agents add claude claude
./ask-agents --plain -p 'macOS 应用是怎么启动 Python 后端的？'
```

## 使用方法

### 管理会话

在应用中，从「历史会话」打开已有会话。点「新对话」可以与所有已启用的 Agent 开始对话，也可以从按钮菜单选一个 Agent 单独对话。换工作目录也会开启新对话。

Agent 回复期间，可以新建或打开其他会话并发送问题。「历史会话」会标出仍在回复的会话，点「停止」只会停止当前会话的回复。

点会话旁的「…」可以置顶、取消置顶或删除。置顶的会话始终排在历史列表顶部。批量删除时，点「选择」，勾选会话，再点「删除」并确认。

交互界面在各自的回复区实时显示 Markdown 回复，以及 CLI 报告的工具活动。在应用中，点圆箭头可重试最近一条回复。点 **×** 可让该 Agent 退出当前会话，并停止它正在生成的回复。

在终端按 Enter 发送，按 Ctrl+J 或 Alt+Enter 换行。回复过程中按 Ctrl+C 取消本轮并回到输入框。按 Esc 进入浏览模式后，用 Tab 切换回复区，用方向键或 Page Up 和 Page Down 滚动，按 Enter 回到草稿，按 **q** 退出。

### 添加图片

在应用中，点输入框旁的回形针，按 Command+V 粘贴图片，或把图片拖进输入框。可以单独发图，也可以图文一起发送。

在终端用 `-i` 为首轮附图，重复指定可添加多张。终端支持 PNG、JPEG、GIF 和 WebP。

```sh
./ask-agents --plain -i screenshot.png -p '截图里报了什么错？'
```

每轮最多 4 张，每张最多 5 MiB，合计最多 10 MiB。发图需要支持图片的 CLI 和模型。

### 切换界面语言

在应用的「Agent 设置 → 界面语言」中，可选择简体中文、English 或跟随系统。

终端可用 `--language zh` 或 `--language en` 指定本次运行的界面语言。将环境变量 `ASK_AGENTS_LANGUAGE` 设为 `zh` 或 `en` 可覆盖终端的区域设置。

### 继续历史会话

```sh
./ask-agents history
./ask-agents history CONVERSATION_ID
./ask-agents --resume CONVERSATION_ID
```

上面三个命令依次列出会话、打印指定会话、重新打开指定会话以继续提问。会话 ID 可用唯一匹配的前缀。在最后一条命令末尾加上 `--plain -p '继续上一个问题'` 即可只运行一轮。

重开的会话使用原工作目录和 Agent 配置。Ask Agents 从各 Agent 自己的近期问答恢复上下文。如果 Agent 支持图片，且附件额度足够，也会带上这段上下文中最近一轮的图片。历史数据库保存问题、图片、回复、工具活动和运行状态。

## 命令和本地文件

| 参数或命令 | 用途 |
| --- | --- |
| `-C DIR` | 指定工作目录。终端默认使用当前目录。 |
| `-p TEXT` | 直接提供首轮问题，跳过交互输入框。 |
| `--plain` | 只运行一轮，以纯文本输出并标记 Agent 名称。通过管道输入或重定向输出时也只运行一轮。 |
| `--timeout SECONDS` | 限制每轮单个 Agent 的运行时间，默认 600 秒。 |
| `add NAME CLI --model MODEL` | 添加或更新 Agent。省略 `--model` 时使用 CLI 的默认模型，可用 `--executable FILE` 指定 CLI 路径。 |
| `list`、`remove NAME` | 列出或移除已保存的 Agent。 |

可用 `./ask-agents < prompt.txt` 从文件读取问题。一个 Agent 失败后，其余 Agent 可以继续回答，命令最终返回退出码 1。

| 数据 | 默认路径 |
| --- | --- |
| Agent 配置 | `~/.config/ask-agents/config.json` |
| 历史会话 | `~/.config/ask-agents/history.sqlite3` |

用 `ASK_AGENTS_CONFIG` 或终端的 `--config FILE` 选用其他配置。历史数据库位于配置文件同一目录。应用与终端使用同一配置文件时会共用历史。

## 支持的 CLI

| CLI | 配置值 | 输入 | 工作目录访问 |
| --- | --- | --- | --- |
| Codex CLI | `codex` | 文本和图片 | 使用只读 `app-server`，禁用权限提升。 |
| TraeX CLI | `traex` | 文本和图片 | 使用只读 `app-server`，禁用权限提升。 |
| Claude Code | `claude` | 文本和图片 | 使用受限模式，提供 Read、Glob、Grep 工具。 |
| Doubao CLI | `doubao` | 文本 | 使用 macOS `sandbox-exec`，写入范围限于 CLI 状态、临时文件和会话初始化所需的路径。 |

CLI 版本须支持表中对应的模式。Doubao 选定具体项目目录后，沙盒也会禁止写入该目录。问题和附件会经所选 CLI 发给该 CLI 配置的服务。

## 运行测试

```sh
python3 -B -m unittest discover -s tests -v
```

测试使用本地模拟 CLI，无需登录模型服务。

## 许可证

Ask Agents 采用 [MIT 许可证](LICENSE)。

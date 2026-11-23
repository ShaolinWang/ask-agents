# Ask Agents

[简体中文](README.zh-CN.md)

Send one prompt to several agent CLIs and compare their replies side by side. Ask a follow-up from the same input box and each agent continues its own conversation. You can also run the same CLI more than once to compare models.

Ask Agents has a native macOS app and a terminal interface. Both use your installed CLIs and their existing sign-ins, and share settings and conversation history on your machine.

## Get started

Install and sign in to at least one [supported CLI](#supported-clis). Run the commands below from the repository root. [Codex CLI setup](https://learn.chatgpt.com/docs/codex/cli) · [Claude Code setup](https://code.claude.com/docs/en/setup#install-claude-code)

### macOS app

You need macOS 14 or later, Xcode or its Command Line Tools, and Python 3.9 or later at `/usr/bin/python3`.

```sh
bash mac-app/build.sh
open 'dist/Ask Agents.app'
```

1. Open **Agent Settings**. Add and enable the agents you want, choose their models, then save.
2. Use the folder button to select a workspace. The app starts at `/` and remembers the workspace you select.
3. Enter your question and press Return. Use Shift + Return for a new line.

If the app cannot find a CLI, set its executable path under **Advanced** in **Agent Settings**.

### Terminal

You need Python 3.9 or later. The interactive interface uses a terminal with a TTY.

```sh
./ask-agents
```

On the first run, use the arrow keys to move, Space to enable an agent, and **M** to choose a model. Press Enter to save and start asking questions. Run `./ask-agents config` to edit the list later.

If you have Codex and Claude Code installed, this sends the same question to both and prints each reply with the agent's name.

```sh
./ask-agents add codex codex
./ask-agents add claude claude
./ask-agents --plain -p 'How does the macOS app start its Python backend?'
```

## Use Ask Agents

### Manage a conversation

In the app, select a conversation in **History** to reopen it. Use **New Conversation** to start with all enabled agents, or choose one agent from its menu. Changing the workspace also starts a new conversation.

While agents reply, you can start or open another conversation and send a prompt there. History shows which conversations are still responding. **Stop** affects only the selected conversation.

Use a conversation’s **…** menu to pin, unpin, or delete it. Pinned conversations stay at the top of History. For bulk deletion, click **Select**, choose the conversations, then click **Delete** and confirm.

The interactive views stream each agent's Markdown reply into its own pane and show the tool activity the CLI reports. In the app, use the circular arrow to retry the latest reply, or click **×** to remove that agent and stop its current reply.

In the terminal, press Enter to send and Ctrl+J or Alt+Enter for a new line. Press Ctrl+C during a reply to cancel the turn and return to the input. Press Esc to browse, then Tab to switch panes, the arrow keys or Page Up and Page Down to scroll, Enter to return to your draft, or **q** to quit.

### Attach images

In the app, click the paperclip, paste an image with Command+V, or drop files into the prompt box. Send images on their own or with text.

In the terminal, use `-i` to attach an image to the first turn. Repeat `-i` to attach more. The terminal accepts PNG, JPEG, GIF, and WebP.

```sh
./ask-agents --plain -i screenshot.png -p 'What error does this screenshot show?'
```

Each turn can include up to 4 images, up to 5 MiB each and 10 MiB in total. Image input requires a supported CLI and a model that accepts images.

### Change the interface language

In the app, choose **Language** under **Agent Settings**. Choose English, Simplified Chinese, or the system language.

In the terminal, use `--language en` or `--language zh` for one run. Set `ASK_AGENTS_LANGUAGE` to `en` or `zh` to override the terminal locale.

### Reopen a saved conversation

```sh
./ask-agents history
./ask-agents history CONVERSATION_ID
./ask-agents --resume CONVERSATION_ID
```

The commands list saved conversations, print a transcript, and reopen a conversation for follow-up questions. You can use a unique ID prefix. Add `--plain -p 'Your follow-up question'` to the last command to run one turn.

A reopened conversation uses its original workspace and agent settings. Ask Agents builds context for each agent from its own recent turns. For agents that accept images, it also includes the most recent images in that context when the attachment limits allow. The history database stores prompts, images, replies, tool activity, and turn status.

## Command-line and storage reference

| Option or command | Behavior |
| --- | --- |
| `-C DIR` | Set the workspace. The terminal uses the current directory by default. |
| `-p TEXT` | Send the first prompt without typing it in the interactive composer. |
| `--plain` | Run one turn and print each reply as plain text with the agent's name. Piped input or redirected output also runs one turn. |
| `--timeout SECONDS` | Limit each agent to the given number of seconds per turn. The default is 600. |
| `add NAME CLI --model MODEL` | Add or update a named agent. Omit `--model` to use the CLI default. Use `--executable FILE` to specify a CLI path. |
| `list`, `remove NAME` | List or remove saved agents. |

Read a prompt from a file with `./ask-agents < prompt.txt`. If an agent fails, the others can finish and the command returns exit code 1.

| Data | Default path |
| --- | --- |
| Agent settings | `~/.config/ask-agents/config.json` |
| Conversation history | `~/.config/ask-agents/history.sqlite3` |

Set `ASK_AGENTS_CONFIG` or use `--config FILE` in the terminal to select another settings file. The history database lives beside that file. The app and terminal share history when they use the same settings file.

## Supported CLIs

| CLI | Configuration value | Inputs | Workspace access |
| --- | --- | --- | --- |
| Codex CLI | `codex` | Text and images | Read-only `app-server` with approval escalation disabled. |
| TraeX CLI | `traex` | Text and images | Read-only `app-server` with approval escalation disabled. |
| Claude Code | `claude` | Text and images | Restricted mode with the Read, Glob, and Grep tools. |
| Doubao CLI | `doubao` | Text | macOS `sandbox-exec` with writes limited to CLI state, temporary files, and session setup. |

Use a CLI version that supports the modes shown above. For Doubao, selecting a specific project directory also blocks writes to that directory. Your prompts and attachments go through the selected CLIs to their configured providers.

## Run the tests

```sh
python3 -B -m unittest discover -s tests -v
```

The tests use local CLI fixtures and run without provider credentials.

## License

Ask Agents is distributed under the [MIT License](LICENSE).

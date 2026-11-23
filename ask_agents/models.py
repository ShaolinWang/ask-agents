"""Read the model choices advertised by each locally installed CLI."""

import asyncio
import json
from dataclasses import replace

from .localization import tr
from .runners import AppSession, command, events, find_executable, start_process, stop

CATALOG_TIMEOUT = 15


def choices(entries, id_key, label_key):
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model = entry.get(id_key)
        label = entry.get(label_key)
        if isinstance(model, str) and model.strip() and all(ord(char) >= 32 for char in model):
            label = " ".join(label.split()) if isinstance(label, str) else ""
            result[model] = label if label and all(ord(char) >= 32 for char in label) else None
    return result


async def app_models(process):
    app = AppSession(process)
    await app.initialize()
    models = {}
    cursor = None
    while True:
        response = await app.request("model/list", {"limit": 100, "cursor": cursor, "includeHidden": False})
        models.update(choices(response["data"], "model", "displayName"))
        cursor = response.get("nextCursor")
        if not cursor:
            return models


async def claude_models(process):
    request = {
        "type": "control_request",
        "request_id": "list-models",
        "request": {"subtype": "initialize"},
    }
    process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
    await process.stdin.drain()
    async for event in events(process.stdout):
        if event.get("type") == "control_response":
            reply = event["response"]
            if reply.get("request_id") == "list-models":
                if reply.get("subtype") != "success":
                    raise ValueError(reply.get("error") or tr("Claude Code 初始化失败", "Claude Code initialization failed"))
                models = choices(reply["response"]["models"], "value", "displayName")
                models.pop("default", None)
                return models
        if event.get("type") == "result" and event.get("is_error"):
            raise ValueError(event.get("result") or tr("Claude Code 初始化失败", "Claude Code initialization failed"))
    raise ValueError(tr("Claude Code 未返回模型列表", "Claude Code returned no model list"))


async def doubao_models(process):
    output, error = await process.communicate()
    if process.returncode:
        raise ValueError(error.decode("utf-8", errors="replace").strip() or tr(
            "Doubao CLI 未返回模型列表",
            "Doubao CLI returned no model list",
        ))
    response = json.loads(output)
    return choices(response["result"]["models"], "id", "name")


async def model_choices(agent, cwd):
    if agent.cli == "doubao":
        argv = [find_executable(agent), "model", "list", "--json"]
        stdin = asyncio.subprocess.DEVNULL
        reader = doubao_models
    else:
        argv = command(replace(agent, model=None, model_name=None), cwd)
        stdin = asyncio.subprocess.PIPE
        reader = app_models
        if agent.cli == "claude":
            argv += ["--input-format", "stream-json"]
            reader = claude_models

    process = await start_process(
        *argv, cwd=cwd, stdin=stdin, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if agent.cli == "doubao" else asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(reader(process), CATALOG_TIMEOUT)
    except asyncio.TimeoutError:
        raise ValueError(tr(
            f"读取模型列表超时（{CATALOG_TIMEOUT} 秒）",
            f"Loading models timed out after {CATALOG_TIMEOUT} seconds",
        )) from None
    finally:
        await stop(process)

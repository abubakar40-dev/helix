#!/usr/bin/env python3
"""Gemini Live bridge for the HELIX call service.

Why a WebSocket bridge and not WebRTC: Gemini Live has no direct browser WebRTC
mode (unlike OpenAI Realtime), so a server proxy is mandatory. Doing that proxy
over raw PCM keeps the box free of any WebRTC media stack - no opus codec, no
SFU - which matters on a 954 MB machine. The API key never leaves the box; the
browser only ever sees audio.

Tools exposed to the model map onto the same shared systems HELIX already uses:
  create_task -> queued to the laptop, which creates the kanban task
  recall     -> answered by the caller (the laptop syncs shared memory down)
"""
import json, os, time

from google import genai
from google.genai import types

MODEL = os.environ.get("HELIX_LIVE_MODEL", "gemini-3.8-live")

SYSTEM = """You are HELIX, the CEO and orchestrator of a multi-agent system. You are
answering a phone call, so speak naturally and briefly - this is a conversation,
not a report. You can hear the user and they can hear you.

Your team delegates work to: research, document, pc, dev, vision, comm, sysmon,
career. When the user asks for something to be done, use the create_task tool to
assign it rather than only promising to. Prefer one clear task over many vague ones.

If the user refers to something you discussed before, use recall to check your
shared memory before answering. Never invent a prior conversation.
"""

def _load_key():
    """Accepts either a raw key file or a .env line, so the box can hold the key
    by itself without shipping the whole environment."""
    for path in (os.environ.get("GEMINI_API_KEY_FILE", "/opt/helix-call/gemini.key"),
                 os.path.expanduser("~/.hermes/.env")):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("GEMINI_API_KEY"):
                        line = line.split("=", 1)[1].strip().strip("\"'")
                    if line:
                        return line
        except OSError:
            continue
    raise RuntimeError("no GEMINI_API_KEY on the box")


def build_tools(on_task, on_recall):
    return [{
        "function_declarations": [
            {
                "name": "create_task",
                "description": "Assign a task to one of your specialist agents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short task title"},
                        "body": {"type": "string",
                                 "description": "Full instructions, context and expected output"},
                        "assignee": {"type": "string",
                                     "description": "research|document|pc|dev|vision|comm|sysmon|career"},
                    },
                    "required": ["title", "body", "assignee"],
                },
            },
            {
                "name": "recall",
                "description": "Search shared memory across every past conversation, "
                               "including previous phone calls.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        ]
    }]


async def run_voice(ws, audio_out, on_task, on_recall, memory_prompt=""):
    """Bridge one call: browser PCM <-> Gemini Live."""
    key = _load_key()
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=SYSTEM + memory_prompt,
        tools=build_tools(on_task, on_recall),
    )
    turns = []
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        async def pump_in():
            """browser -> gemini"""
            async for msg in ws:
                import aiohttp
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await session.send_realtime_input(
                        audio=types.Blob(data=msg.data, mime_type="audio/pcm;rate=16000"))
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    # {"endTurn":true} forces a reply even if VAD has not fired,
                    # and lets the UI show "helix is thinking"
                    try:
                        ctl = json.loads(msg.data)
                    except ValueError:
                        continue
                    if ctl.get("endTurn"):
                        await session.send_client_content(
                            turns=[types.Content(role="user", parts=[])], turn_complete=True)

        async def pump_out():
            """gemini -> browser"""
            async for msg in session.receive():
                # the SDK yields a LiveServerMessage wrapper; the parts live inside
                tc = getattr(msg, "tool_call", None)
                if tc:
                    for fc in (getattr(tc, "function_calls", None) or []):
                        if fc.name == "create_task":
                            on_task(dict(fc.args or {}))
                            await session.send_tool_response(
                                function_responses=[types.FunctionResponse(
                                    id=fc.id, name="create_task",
                                    response={"result": "task queued"})])
                        elif fc.name == "recall":
                            await session.send_tool_response(
                                function_responses=[types.FunctionResponse(
                                    id=fc.id, name="recall",
                                    response={"result": on_recall(fc.args.get("query", ""))})])
                    continue

                sc = getattr(msg, "server_content", None)
                if not sc:
                    continue
                mt = getattr(sc, "model_turn", None)
                if not (mt and mt.parts):
                    continue
                for p in mt.parts:
                    blob = getattr(p, "inline_data", None)
                    if blob is not None and getattr(blob, "data", None):
                        await ws.send_bytes(blob.data)          # speech -> browser
                text = "".join(p.text or "" for p in mt.parts if getattr(p, "text", None))
                if text.strip():
                    turns.append({"who": "assistant", "text": text.strip(),
                                  "at": time.time()})
                    await ws.send_str(json.dumps({"transcript": text.strip()}))

        import asyncio
        await asyncio.gather(pump_in(), pump_out())
    return turns
#!/usr/bin/env python3
"""OpenAI-shaped SSE proxy that tunnels OpenCode free-tier models via its CLI.

OpenCode's free tier is locked behind desktop-app identity: bare HTTP gets 403.
The only authenticated transport is the signed CLI, which speaks a custom
per-call JSON protocol. This proxy translates OpenAI <-> that protocol.

Two hard problems this solves, both verified empirically (2026-09-28):

1. WinError 206 ("filename or extension is too long").
   The prompt used to ride in argv, and Windows caps a command line at 32,767
   characters. Any real conversation blows that instantly. The prompt now goes
   in over STDIN, which has no such limit. (`--file` does NOT work on its own:
   the CLI rejects it with "You must provide a message".)

2. Tool calling.
   The shim used to drop the OpenAI `tools` array entirely, so models behind it
   could not drive client tools and would hallucinate results. The full tool
   catalogue is now serialised into the prompt, the model emits a fenced
   tool-call block, and the shim parses it back into real OpenAI `tool_calls`
   frames. The client executes the tools; the shim never does.

   3. OpenCode's own agent harness.
   Every `opencode run` starts by injecting OpenCode's agent system
   prompt and its built-in tool catalogue (shell/glob/read/write/...),
   none of which the client registered and none of which are reachable
   over the OpenAI wire protocol. Measured on 2.0.26 (reproduce with
   test_phantom_tools.py): asked three questions that need no tools at
   all, the default agent fired 8 phantom tool calls - `glob`, `shell`,
   `read`, `question`, and Code Mode's `execute` - because the wording
   leaned toward files ("what is the marker?", "describe the project
   structure"). It went looking for a *file* marker. The same three
   prompts under `--agent oc-shim` fire 0. That delta is the point of
   the custom agent.

   Phantom calls are prompt-dependent, not constant: clean factual
   prompts ("17 + 25", capital of Japan) fire 0 under both agents. The
   confusion appears on exactly the wording a coding agent receives.

   What was tried, and what the measurements actually showed:

   * `--agent <custom>` + `agent/*.md` WORKS, and is the real fix.
     The custom agent's body REPLACES the agent system prompt. Proof:
     an agent whose body says "answer MARKER-4417-VERIFIED" makes the
     model answer exactly that, while the default agent ignores the
     question and goes exploring. Token cost drops ~2-3% versus build.

   * `tools: {}` in the agent frontmatter is SILENTLY IGNORED. Measured
     directly: an agent carrying the key and one without it were forced
     to run the same `shell` command, and both executed it. `opencode
     debug agents` reports no `tools` field on ANY agent, so that
     command cannot settle the question either way - only a forced call
     can. Do not rely on it.

   * `permission: {...: "deny"}` loads and does drop tool_use, but it
     reproducibly breaks auth: every run fails with `provider.auth` 403
     "free tier can only be used from within OpenCode". It is `deny`
     itself, not the wildcard - denying ten tools by name fails the
     same way, while the identical key set set to `allow` authenticates
     fine. The free tier appears to read lockdown as a signal. There is
     no way to strip these tools without losing the free tier entirely.

   * Code Mode is undocumented: `execute` is absent from the built-in
     tool list in the official docs, so there is no documented
     off-switch. It is suppressed along with everything else, not
     removed.

   So the tools cannot be removed in this build; they can only be
   *steered*. SHIM_AGENT_MD therefore does not try to deny tools, it
   writes a system prompt that tells the model it has none and that the
   authoritative tool list is the fence in the user message. Combined
   with the `tool_use` suppression and corrective-nudge retry in
   `collect()`, and the throwaway scratch cwd as blast-radius control,
   that keeps alien calls from ever reaching the client.

   OpenCode also still injects a small base environment block (cwd,
   platform, date) regardless of agent; the model will name the working
   directory unprompted. A few hundred tokens, not worth fighting.
"""

import asyncio, glob, json, os, shutil, subprocess, time, uuid, argparse, re
from aiohttp import web


# Custom agent whose body REPLACES OpenCode's agent system prompt on every shim
# turn. That system-prompt swap is the one lever that verifiably works, so the
# prompt below has to carry the whole instruction: the tool catalogue itself
# cannot be emptied from this side (see the notes in the module docstring).
SHIM_AGENT = "oc-shim"

# NOTE: no `tools:` key on purpose. It is accepted by the frontmatter parser
# and then silently dropped from the loaded definition, which reads like a
# successful disable but changes nothing at runtime (measured: phantom
# tool_use events continue unchanged). The system prompt below is the only
# lever that actually works, so it has to carry the full instruction.
SHIM_AGENT_MD = """---
description: Passthrough agent for the OpenAI SSE shim. No tools, no harness.
mode: primary
temperature: 0
---

You are a text completion engine reached over an OpenAI-compatible HTTP API.

You have no tools of your own. Anything you might expect to be available - a
shell, a filesystem, a browser - does not exist here, and nothing you invent
about your environment is trustworthy. Do not speculate about files, do not
describe what you would run, and do not claim to have inspected anything.

The ONLY tools that exist are the ones named in the user message, and they
are reached exclusively through the fenced tool-call format described there.
That list is authoritative; your own idea of what tools exist is not.

Answer in plain prose unless a listed tool is genuinely required, in which
case emit the fence and nothing else.
"""


def config_dir():
    """OpenCode's global config dir, where agent/*.md definitions live."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return os.path.join(xdg, "opencode")
    return os.path.expandvars(r"%USERPROFILE%\.config\opencode")


def agent_path():
    return os.path.join(config_dir(), "agent", SHIM_AGENT + ".md")


def ensure_agent():
    """Provision the passthrough agent if absent. Never clobber a user edit.

    If this fails the shim still works - it just runs with OpenCode's full
    agent harness and the suppression layer in collect() carries the weight.
    """
    p = agent_path()
    try:
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(SHIM_AGENT_MD)
            print(f"[proxy] provisioned agent -> {p}", flush=True)
        return True
    except Exception as e:
        print(f"[proxy] WARN could not provision agent: {e}", flush=True)
        return False


def resolve_cli():
    """Locate the OpenCode CLI binary.

    Order: $OPENCODE_CLI, then PATH, then the desktop app's bundled CLI. The
    desktop-app path was the original target, but plenty of installs (scoop,
    npm, winget) only put the binary on PATH, so try that before giving up.
    """
    env = os.environ.get("OPENCODE_CLI", "").strip().strip('"')
    if env:
        return env
    on_path = shutil.which("opencode") or shutil.which("opencode-cli")
    if on_path:
        return on_path
    legacy = os.path.expandvars(r"%APPDATA%\ai.opencode.desktop\cli")
    hits = sorted(glob.glob(os.path.join(legacy, "*", "opencode-cli.exe")))
    return hits[-1] if hits else "opencode-cli.exe"


OPENCODE_CLI = resolve_cli()

# Belt-and-braces with `--agent oc-shim`: keep the CLI's cwd in a throwaway
# scratch dir so an alien tool can never touch real user files even if the
# agent stops resolving. Client tool calls use absolute paths anyway.
WORK_DIR = os.path.expandvars(r"%LOCALAPPDATA%\Temp\oc_shim_cwd")
os.makedirs(WORK_DIR, exist_ok=True)
HAVE_AGENT = ensure_agent()
PER_READ_TIMEOUT = 300
# Transient CLI failures worth a retry: the step watchdog aborts (often after a
# permission prompt stalled), read timeouts, connection resets.
TRANSIENT_RE = re.compile(r"interrupt|abort|timeout|econn|reset|timed out", re.I)
DEFAULT_MODEL = "longcat-2.5-preview-free"

# ── model catalogue ──────────────────────────────────────────────────────────
#
# The live list comes from `opencode models`, not from a literal here: OpenCode
# retires and adds models without warning, and a hand-maintained list silently
# rots (it already had one entry that no longer existed and six live models it
# never mentioned). SEED_MODELS is only what we serve if the CLI cannot be
# asked - never clobber the discovered list with it.
SEED_MODELS = [
    "longcat-2.5-preview-free",
    "space-bunny-free",
    "mimo-v2.6-flash-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
    "big-pickle",
]
# Manual tombstones. These win over discovery so a model OpenCode still
# advertises can be pinned off if it is unusable through the CLI.
DEAD_MODELS = {
    "mimo-v2.5-free": "Model retired by OpenCode (provider.no-route).",
    "deepseek-v4-flash-free": "Model retired by OpenCode (provider.no-route).",
    "jev-1.13-free": "System One model - not a chat model, unreachable via CLI.",
    # Advertised under opencode/ but never routable - requests failed before
    # discovery existed and no -free entry in the provider list backs it.
    "ling-3.0-flash-fin-free": "Not routable via the CLI (no such model under opencode/).",
}
ALIASES = {
    "longcat": "longcat-2.5-preview-free", "longcat-2.5": "longcat-2.5-preview-free",
    "bunny": "space-bunny-free", "space-bunny": "space-bunny-free",
    "mimo": "mimo-v2.6-flash-free",
    "nemotron": "nemotron-3-ultra-free", "nemotron-3-ultra": "nemotron-3-ultra-free",
    "pickle": "big-pickle", "big-pickle": "big-pickle",
}

CATALOG_TTL = 1800          # re-poll `opencode models` at most this often
DISCOVERY_TIMEOUT = 30      # seconds to wait on the CLI before giving up
# Provider-level failure meaning "this model is gone", as opposed to a
# transport blip. Anything matching is tombstoned for the process lifetime.
NO_ROUTE_RE = re.compile(r"no-route|no_route|not\s*found|unknown\s*model|"
                         r"model\s*not\s*(?:available|supported)", re.I)

_CATALOG = {"models": list(SEED_MODELS), "source": "seed", "at": 0.0}
# Models that died since the last poll. Cleared whenever discovery succeeds, so
# a model that comes back is picked up again instead of staying dead.
_RUNTIME_DEAD = set()


def _models_sync():
    """Shell out to `opencode models` and keep the opencode/ provider entries.

    Runs in a worker thread so the event loop never blocks on the CLI.
    """
    out = subprocess.run([OPENCODE_CLI, "models"], capture_output=True,
                         text=True, timeout=DISCOVERY_TIMEOUT,
                         encoding="utf-8", errors="replace", env=cli_env())
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "?").strip()[-200:])
    models = set()
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("opencode/"):
            models.add(line.split("/", 1)[1])
    if not models:
        raise RuntimeError("no opencode/ models in output")
    return sorted(models)


async def refresh_catalog():
    """Re-poll the CLI. Returns (models, changed). Never raises."""
    try:
        found = await asyncio.get_running_loop().run_in_executor(None, _models_sync)
    except Exception as e:
        print(f"[proxy] WARN model discovery failed ({e}); keeping "
              f"{len(_CATALOG['models'])} cached", flush=True)
        return _CATALOG["models"], False
    changed = found != _CATALOG["models"] or _CATALOG["source"] != "discovered"
    _CATALOG.update(models=found, source="discovered", at=time.time())
    _RUNTIME_DEAD.clear()
    if changed:
        added = sorted(set(found) - set(SEED_MODELS))
        print(f"[proxy] models: discovered {len(found)} "
              f"({', '.join(added) if added else 'no change'})", flush=True)
    return found, changed


def catalog():
    """Live models: discovered (or seed), minus manual and runtime tombstones."""
    dead = set(DEAD_MODELS) | _RUNTIME_DEAD
    return [m for m in _CATALOG["models"] if m not in dead]


def tombstone(model, reason):
    """Mark a model dead for this process. A later successful poll revives it."""
    if model not in _RUNTIME_DEAD:
        _RUNTIME_DEAD.add(model)
        print(f"[proxy] tombstoned {model}: {reason}", flush=True)


def default_model():
    """DEFAULT_MODEL if it survived, else the first live model, else the seed."""
    live = catalog()
    return DEFAULT_MODEL if DEFAULT_MODEL in live else (live[0] if live else DEFAULT_MODEL)


async def catalog_watchdog(app):
    """Keep the catalogue fresh for the process lifetime."""
    while True:
        await asyncio.sleep(CATALOG_TTL)
        try:
            await refresh_catalog()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

OPEN_FENCE = "<oc_tool_call>"
CLOSE_FENCE = "</oc_tool_call>"

PROTOCOL = """\
# TOOL PROTOCOL (read carefully)

You do NOT have any built-in tools. Anything you think you can do with a
built-in shell, file reader or editor does not exist here, and its output is
discarded. You have exactly the tools listed below, provided by an external
harness that executes them for you and returns the results.

To call one or more tools, output ONLY this fenced block, with no other text:

<oc_tool_call>
{"name": "TOOL_NAME", "arguments": {"KEY": "VALUE"}}
</oc_tool_call>

Rules:
- The block must be the entire response. No prose before or after it.
- `arguments` must be a JSON object matching that tool's `parameters` schema.
- To call several tools at once, put one JSON object per line inside the single
  block. They are executed in parallel.
- Use EXACT tool names as written below.
- When you already have everything you need, reply with plain prose and NO block.
"""


def resolve_model(name):
    name = (name or DEFAULT_MODEL).strip()
    for p in ("opencode-go/", "opencode/"):
        if name.startswith(p):
            name = name[len(p):]
    return ALIASES.get(name.lower(), name)


def cli_env():
    """Subprocess env with a native PWD.

    opencode-cli.exe chdir()s to $PWD. Under Git-Bash/MSYS that is a POSIX path
    ('/c/Users/...') the Windows binary cannot chdir into, and every call dies
    with 'Failed to change directory to /c/Users/...'.
    """
    env = dict(os.environ)
    env["PWD"] = WORK_DIR
    return env


# ── tool catalogue ───────────────────────────────────────────────────────────

def render_tools(tools):
    """Serialise the OpenAI tools array into a compact prompt catalogue."""
    if not tools:
        return ""
    out = ["# AVAILABLE TOOLS"]
    for t in tools:
        fn = t.get("function") or {}
        out.append(f"## {fn.get('name')}")
        if fn.get("description"):
            out.append(fn["description"].strip())
        schema = fn.get("parameters") or {}
        props = schema.get("properties") or {}
        if props:
            out.append("parameters (JSON Schema):")
            out.append(json.dumps(
                {"type": schema.get("type", "object"),
                 "properties": props,
                 "required": schema.get("required", [])},
                ensure_ascii=False, separators=(",", ":")))
        out.append("")
    return "\n".join(out)


def _content_to_text(content):
    if isinstance(content, list):
        bits = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("image_url", "image"):
                bits.append("[image supplied; this transport is text-only]")
            else:
                bits.append(p.get("text") or "")
        return " ".join(b for b in bits if b)
    return content or ""


def flatten_history(msgs):
    """Flatten the whole conversation into one prompt.

    The CLI is a fresh process per call, so context must ride in the prompt.
    Tool calls and their results are labelled so the model can chain them.
    """
    lines = []
    for m in msgs:
        role = m.get("role", "user")
        if role == "tool":
            name = m.get("name") or m.get("tool_call_id") or "tool"
            lines.append(f"[tool result: {name}]\n{_content_to_text(m.get('content'))}")
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            try:
                args = json.dumps(json.loads(args)) if isinstance(args, str) else args
            except Exception:
                pass
            lines.append(
                f"[tool call: {fn.get('name')}]\narguments: {json.dumps(args, ensure_ascii=False)}")
        text = _content_to_text(m.get("content"))
        if not text:
            continue
        if role == "system":
            lines.append(f"[system]\n{text}")
        elif role == "assistant":
            lines.append(f"[assistant]\n{text}")
        else:
            lines.append(f"[user]\n{text}")
    return "\n\n".join(lines).strip()


def build_prompt(body):
    msgs = body.get("messages", [])
    tools = body.get("tools") or []
    parts = []
    if tools:
        parts.append(PROTOCOL)
        parts.append(render_tools(tools))
    convo = flatten_history(msgs)
    parts.append(convo if convo else "Hello")
    return "\n\n".join(p for p in parts if p)


# ── tool-call stream splitting ───────────────────────────────────────────────

def _partial_suffix_len(buf, fence):
    """Longest k < len(fence) such that buf ends with fence[:k]."""
    maxk = min(len(fence) - 1, len(buf))
    for k in range(maxk, 0, -1):
        if buf.endswith(fence[:k]):
            return k
    return 0


class Splitter:
    """Split model text into prose and fenced tool-call blocks, streaming-safe.

    Yields ('text', s) / ('tool', s). Holds back only enough trailing text that
    a fence delimiter cannot straddle a chunk boundary, so prose still streams.
    """

    def __init__(self):
        self.buf = ""
        self.in_tool = False

    def feed(self, new):
        self.buf += new
        while self.buf:
            if self.in_tool:
                i = self.buf.find(CLOSE_FENCE)
                if i >= 0:
                    if i:
                        yield ("tool", self.buf[:i])
                    self.buf = self.buf[i + len(CLOSE_FENCE):]
                    self.in_tool = False
                    continue
                keep = _partial_suffix_len(self.buf, CLOSE_FENCE)
                if len(self.buf) > keep:
                    cut = len(self.buf) - keep
                    yield ("tool", self.buf[:cut])
                    self.buf = self.buf[cut:]
                return
            i = self.buf.find(OPEN_FENCE)
            if i >= 0:
                if i:
                    yield ("text", self.buf[:i])
                self.buf = self.buf[i + len(OPEN_FENCE):]
                self.in_tool = True
                continue
            keep = _partial_suffix_len(self.buf, OPEN_FENCE)
            if len(self.buf) > keep:
                cut = len(self.buf) - keep
                yield ("text", self.buf[:cut])
                self.buf = self.buf[cut:]
            return

    def flush(self):
        """Emit whatever is left when the model stops."""
        if self.buf:
            yield ("tool" if self.in_tool else "text", self.buf)
            self.buf = ""
        self.in_tool = False


def parse_tool_block(raw):
    """Parse the inside of a tool fence into a list of OpenAI tool calls."""
    raw = raw.strip()
    if not raw:
        return []
    obj = None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        objs = []
        for line in raw.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        obj = objs or None
    if obj is None:
        return []
    items = obj if isinstance(obj, list) else [obj]
    calls = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("tool") or it.get("function")
        if isinstance(name, dict):
            name = name.get("name")
        if not name:
            continue
        args = it.get("arguments", it.get("parameters", it.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"input": args}
        if not isinstance(args, dict):
            args = {"value": args}
        calls.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": str(name),
                         "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


# ── CLI transport ────────────────────────────────────────────────────────────

class ShimError(Exception):
    pass


async def run_opencode(model, prompt):
    """Yield events: ('text', delta) | ('tool', raw) | ('alien_tool', name).

    The prompt is written to STDIN, never argv (WinError 206).
    """
    cmd = [OPENCODE_CLI, "run", "--format", "json", "--auto",
           "--model", "opencode/" + model]
    if HAVE_AGENT:
        cmd += ["--agent", SHIM_AGENT]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=WORK_DIR, env=cli_env(),
    )
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass

    sent = ""

    async def pump():
        nonlocal sent
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=PER_READ_TIMEOUT)
            if not line:
                break
            line = line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "error":
                err = obj.get("error", {})
                raise ShimError(f"{err.get('type', 'error')}: {err.get('message', '?')}")
            if t == "tool_use":
                # OpenCode's own tools. Not client tools - suppress.
                yield ("alien_tool", (obj.get("part") or {}).get("tool", "?"))
            elif t == "text":
                part = obj.get("part", {})
                new = part.get("text", obj.get("text", ""))
                if new and len(new) > len(sent):
                    yield ("text", new[len(sent):])
                    sent = new
        rc = await proc.wait()
        if rc != 0:
            err = (await proc.stderr.read()).decode("utf-8", "replace")[-500:]
            raise ShimError(f"opencode exit {rc}: {err}")

    try:
        async for ev in pump():
            yield ev
    except Exception:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise


async def collect(model, prompt, stream_cb=None):
    """Run one turn. Returns (text, tool_calls, alien_tool_names).

    If the model reached for one of the CLI's own tools, the turn is retried
    once with a corrective nudge appended to the prompt. Transient CLI deaths
    (step watchdog abort, timeout, connection reset) are also retried: without
    this the partial text of a killed turn is masked as a complete answer and
    the client sees the turn 'finish' with no tool calls.
    """
    alien = []
    for attempt in range(3):
        sp = Splitter()
        text_parts, tool_raw = [], []
        alien = []
        try:
            async for ev, payload in run_opencode(model, prompt):
                if ev == "text":
                    for kind, seg in sp.feed(payload):
                        if kind == "text":
                            text_parts.append(seg)
                            if stream_cb:
                                await stream_cb(seg)
                        else:
                            tool_raw.append(seg)
                elif ev == "alien_tool":
                    alien.append(payload)
        except ShimError as e:
            if NO_ROUTE_RE.search(str(e)):
                # The provider has retired this model out from under us. Drop it
                # from the catalogue so the next /v1/models is honest, and don't
                # burn retries on a call that cannot succeed.
                tombstone(model, str(e))
            elif attempt < 2 and TRANSIENT_RE.search(str(e)):
                if stream_cb:
                    await stream_cb(f"\n[shim: CLI died ({e}); retrying turn]\n")
                continue
            raise
        for kind, seg in sp.flush():
            if kind == "text":
                text_parts.append(seg)
                if stream_cb:
                    await stream_cb(seg)
            else:
                tool_raw.append(seg)

        calls = parse_tool_block("".join(tool_raw))
        if calls or not alien or attempt == 2:
            return "".join(text_parts), calls, alien
        # The model tried a tool the client never registered. Correct it.
        prompt = (prompt + "\n\n[system] Your last attempt tried to use a built-in "
                  "tool (" + ", ".join(sorted(set(alien))) + "). That tool does not "
                  "exist here and its output is discarded. Use the tool protocol "
                  "above with one of the listed tools, or answer in plain text.")
    return "", [], []


# ── SSE plumbing ─────────────────────────────────────────────────────────────

def chunk(cid, model, delta=None, finish=None):
    d = {}
    if delta is not None:
        d["delta"] = delta
    if finish:
        d["finish_reason"] = finish
    return json.dumps({"id": cid, "object": "chat.completion.chunk",
                       "created": 0, "model": model,
                       "choices": [{"index": 0, **d}]})


def tool_call_chunks(cid, model, calls):
    """OpenAI streaming shape for tool calls: index, id, name, then arguments."""
    out = []
    for i, c in enumerate(calls):
        out.append(json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": i, "id": c["id"], "type": "function",
                "function": {"name": c["function"]["name"], "arguments": ""}}]}}]}))
        out.append(json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": i,
                "function": {"arguments": c["function"]["arguments"]}}]}}]}))
    return out


def final_body(cid, model, text, calls):
    msg = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = calls
    return {"id": cid, "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


# ── handlers ─────────────────────────────────────────────────────────────────

async def handle_chat(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "invalid JSON body"}}, status=400)

    requested = body.get("model") or DEFAULT_MODEL
    model = resolve_model(requested)
    if model in DEAD_MODELS:
        return web.json_response(
            {"error": {"message": DEAD_MODELS[model], "type": "model_not_found",
                       "code": "model_dead", "param": "model"}}, status=503)

    stream = bool(body.get("stream"))
    ntools = len(body.get("tools") or [])
    prompt = build_prompt(body)
    print(f"[proxy] {requested}->{model} {len(prompt)}ch "
          f"{'stream' if stream else 'block'} tools={ntools}", flush=True)
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]

    if not stream:
        try:
            text, calls, alien = await collect(model, prompt)
            return web.json_response(final_body(cid, model, text, calls))
        except Exception as e:
            print(f"[proxy] ERROR: {e}", flush=True)
            return web.json_response({"error": {"message": str(e)}}, status=502)

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
        "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    await resp.prepare(request)

    async def send(payload):
        await resp.write(f"data: {payload}\n\n".encode())

    try:
        await send(chunk(cid, model, delta={"role": "assistant", "content": ""}))

        async def on_text(seg):
            await send(chunk(cid, model, delta={"content": seg}))

        text, calls, alien = await collect(model, prompt, stream_cb=on_text)
        for payload in tool_call_chunks(cid, model, calls):
            await send(payload)
        await send(chunk(cid, model, finish="tool_calls" if calls else "stop"))
        await send("[DONE]")
        await resp.write_eof()
        print(f"[proxy] -> done text={len(text)}ch tools={len(calls)} alien={alien}", flush=True)
    except asyncio.TimeoutError:
        await send(chunk(cid, model, delta="[proxy] timeout waiting for opencode"))
        await send(chunk(cid, model, finish="stop"))
        await send("[DONE]")
        await resp.write_eof()
    except Exception as e:
        print(f"[proxy] STREAM ERROR: {e}", flush=True)
        try:
            await send(chunk(cid, model, delta=f"[proxy error] {e}"))
            await send(chunk(cid, model, finish="stop"))
            await send("[DONE]")
            await resp.write_eof()
        except Exception:
            pass
    return resp


async def handle_models(request):
    live = catalog()
    return web.json_response({"object": "list", "data": [
        {"id": m, "object": "model", "created": 0, "owned_by": "opencode"}
        for m in live] + [
        {"id": a, "object": "model", "created": 0, "owned_by": "opencode-alias"}
        for a in ALIASES if ALIASES[a] in live]})


async def handle_refresh(request):
    """Force a re-poll. Useful right after OpenCode changes its model list."""
    models, _ = await refresh_catalog()
    return web.json_response({"ok": True, "source": _CATALOG["source"],
                              "models": catalog(), "seen": len(models)})


async def health(request):
    return web.json_response({
        "status": "ok", "default_model": default_model(),
        "free_models": catalog(),
        "catalog_source": _CATALOG["source"],
        "catalog_age_s": round(time.time() - _CATALOG["at"], 1) if _CATALOG["at"] else None,
        "dead_models": sorted(DEAD_MODELS),
        "runtime_dead": sorted(_RUNTIME_DEAD),
        "transport": "stdin",
        "tool_bridge": True, "cli": OPENCODE_CLI, "cli_exists": os.path.exists(OPENCODE_CLI),
        "agent": SHIM_AGENT if HAVE_AGENT else None,
        "agent_file": agent_path(), "agent_active": HAVE_AGENT,
    })


async def on_startup(app):
    """Seed the catalogue before serving, then keep it fresh in the background."""
    await refresh_catalog()
    app["watchdog"] = asyncio.create_task(catalog_watchdog(app))


async def on_cleanup(app):
    task = app.get("watchdog")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def main():
    global CATALOG_TTL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18788)
    ap.add_argument("--catalog-ttl", type=int, default=CATALOG_TTL,
                    help="seconds between `opencode models` re-polls")
    args = ap.parse_args()
    CATALOG_TTL = max(60, args.catalog_ttl)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/models/refresh", handle_refresh)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_post("/chat/completions", handle_chat)
    print(f"[proxy] streaming :{args.port} -> {WORK_DIR}", flush=True)
    print(f"[proxy] cli: {OPENCODE_CLI} "
          f"(exists={os.path.exists(OPENCODE_CLI)})", flush=True)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

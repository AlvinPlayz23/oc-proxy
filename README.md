# OpenCode Free-Tier Shim

An **OpenAI-shaped SSE proxy** that tunnels free-tier OpenCode models through
OpenCode's own CLI, so any OpenAI-compatible client can use them.

Runs on your machine, binds to `127.0.0.1` only, needs no API key.

---

## How it works

This is the part worth understanding before you debug anything.

**Why it exists.** OpenCode gates its free tier behind desktop-app identity —
a bare HTTP request gets `403`. The only authenticated transport is their
signed CLI binary, which speaks a custom per-call JSON protocol, not OpenAI's
SSE wire format. The shim translates between the two.

**It does not create an OpenCode instance per client.** Instead, **every single
API request spawns one fresh `opencode run` subprocess.** There is no long-lived
session, no connection reuse, no server the shim holds open.

```
client ──POST /v1/chat/completions──▶ shim ──spawn──▶ opencode run --format json
                                              ◀──NDJSON on stdout──
        ◀─────SSE chunks────────────────
```

Because no session persists, **conversation history has nowhere to live**. So
the shim flattens the entire `messages` array into a single prompt on every turn
(`flatten_history()`). That is why multi-turn context works at all — and why
your prompt gets large fast. You are re-sending the whole conversation to a
process that has never seen any of it before.

**No session flags are used.** The CLI supports `--session` and `--continue`;
the shim passes neither. Every request is fully independent.

**The CLI may delegate to a background service.** The shim does not pass
`--standalone`, so `opencode run` can hand off to OpenCode's shared background
service rather than doing everything in-process. You may therefore observe two
processes per request (the `run` invocation plus the service) rather than one.

**Prompt goes over STDIN, never argv.** Windows caps a command line at 32,767
characters; any real conversation blows past that instantly. (`--file` alone
does not work — the CLI rejects it.)

**The CLI's own tools are suppressed.** `opencode run` injects its own
bash/read/write tools, which cannot be disabled in this build, and `--auto`
approves their permission prompts. Those `tool_use` events are swallowed and
never forwarded. To keep them from touching your files, the shim runs the CLI
with its working directory set to a throwaway scratch dir
(`%LOCALAPPDATA%\Temp\oc_shim_cwd`). Real tool calls use absolute paths anyway.

**Tools work by prompt injection.** The shim serializes your OpenAI `tools`
array into the prompt; the model replies with a fenced block:

```
<oc_tool_call>
{"name": "list_files", "arguments": {"path": "."}}
</oc_tool_call>
```

The shim parses that back into standard OpenAI `tool_calls` frames. **Your
client executes the tools** — the shim never does. It also retries a turn once
if the model reaches for a built-in tool that doesn't exist here.

---

## Requirements

- **Python with `aiohttp`** — `pip install aiohttp` if `import aiohttp` fails
- **OpenCode CLI, already authenticated** — `opencode run --model opencode/space-bunny-free "hi"` must work before the shim is useful

The shim finds the CLI itself, in this order:

1. `$OPENCODE_CLI`, if set
2. `opencode` on `PATH`
3. the desktop app's bundled `%APPDATA%\ai.opencode.desktop\cli\*\opencode-cli.exe`

`/health` reports the resolved path and whether it exists. Override it if needed:

```powershell
$env:OPENCODE_CLI = 'C:\path\to\opencode.exe'
```

---

## Running it

Foreground — streams the log, `Ctrl+C` to stop:

```powershell
python opencode_proxy.py --port 18788
```

Detached and idempotent — starts in the background, waits until healthy, and
no-ops if the port already answers (so you can't start two copies by accident):

```powershell
.\start_proxy.cmd
```

Default port is `18788`; `start_proxy.ps1` takes one if you want a different
one: `.\start_proxy.ps1 18800`.

**Check it's alive:**

```powershell
curl http://127.0.0.1:18788/health
```

**Stop it** — it runs detached with no console, so `Ctrl+C` won't reach it:

```powershell
Get-NetTCPConnection -LocalPort 18788 -State Listen |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

A `TIME_WAIT` line may linger in `netstat` output afterward. That's the old
client socket cooling down, not a live listener — ignore it.

**Logs:** `%TEMP%\oc_proxy.log`, `%TEMP%\oc_proxy.err`, `%TEMP%\oc_starter.log`.

**Autostart:** point a `HKCU\...\CurrentVersion\Run` value at `start_proxy.cmd`.
It locates itself via `%~dp0`, so it works from any checkout path.

---

## API key

**There isn't one. Put anything.**

The shim has no authentication code at all — it never reads your
`Authorization` header, and ignores whatever key you send:

```yaml
base_url: http://127.0.0.1:18788/v1
api_key: none          # ignored; any value works
```

Some clients refuse an empty key, so use `none` or `sk-local` rather than `""`.

The real credentials live upstream and aren't yours to supply. The shim shells
out to `opencode run`, and the CLI authenticates with its own stored session
(`opencode auth list`). That's exactly why this shim exists — the CLI is the
only authenticated transport, and it never hands its identity to the shim.

> **Security note.** The shim binds `127.0.0.1` and has **zero authentication**.
> Any process running as your user can hit it and spend your free-tier quota —
> a browser page making a localhost request, another tool, or malware. Fine on
> a single-user desktop; if you ever bind it wider than loopback, add a token
> check first.

---

## Models

### How they're found right now: they aren't

**The list is hardcoded.** Three literals in `opencode_proxy.py` — nothing
queries the provider, and nothing validates the list:

| Line | Constant | Purpose |
|---|---|---|
| 65 | `DEFAULT_MODEL` | model used when the request omits one |
| 67 | `FREE_MODELS` | 6 strings, returned by `/v1/models` |
| 75 | `DEAD_MODELS` | 3 retired models, hand-tombstoned |
| 80 | `ALIASES` | short names like `bunny`, `mimo` |

`/v1/models` simply echoes `FREE_MODELS + ALIASES`. `resolve_model()` strips an
`opencode/` or `opencode-go/` prefix and applies aliases. When a model is
retired upstream, the shim doesn't notice — requests either fail at runtime
or, if you hand-edit the list, drift.

### The list has drifted

Comparing the hardcoded list against `opencode models` (840 models, 11 under
`opencode/`) as of writing:

**Listed but no longer offered — 1:**

- `ling-3.0-flash-fin-free` — doesn't exist under `opencode/` at all. Any
  request for it should fail right now.

**Live but missing from the list — 6:**

- `big-pickle` *(the flagship free model — it has no `-free` suffix)*
- `cat-jam-free`
- `deepseek-v4.1-flash-free` *(note `-4.1-`; the older
  `deepseek-v4-flash-free` in `DEAD_MODELS` is a different model)*
- `muse-spark-1.2-contributor-free`
- `muse-spark-1.3-contributor-free`
- `step-5-preview-free`

So: 5 of the 6 listed entries work, 1 is broken, and 6 working models are
invisible. To check the current truth yourself:

```powershell
opencode models | Select-String '^opencode/'
```

Auto-discovery from `opencode models` is a planned improvement, not something
this version does.

---

## Tests

```powershell
python test_units.py       # 28 tests, no network
```

Covers stream splitting, tool-block parsing, and the argv-length path.

`test_tools.py` and `test_multistep.py` hit a **live** proxy — start it first.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health`, `/` | status, model list, resolved CLI path |
| `GET` | `/v1/models` | model list |
| `POST` | `/v1/chat/completions` | chat (streaming and non-streaming) |
| `POST` | `/chat/completions` | same, unversioned path |

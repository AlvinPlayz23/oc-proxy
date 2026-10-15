"""Measure phantom OpenCode-tool calls on prompts that need no tools.

The shim agent tells the model it has no tools. If it still reaches for
shell/glob/read on a trivial question, that is confusion, and it shows up as
tool_use events in the CLI event stream.
"""
import json
import os
import subprocess
import sys

CLI = os.environ.get("OPENCODE_CLI") or "opencode"
MODEL = "opencode/space-bunny-free"
WORK = os.path.expandvars(r"%LOCALAPPDATA%\Temp\oc_shim_cwd")

# Prompts that lean toward environment/file operations. These are the ones that
# make the model reach for shell/glob when it has no business doing so. Clean
# factual prompts produce zero phantom calls in both configs, so the wording is
# what matters - "marker" suggests a file marker, "project structure" suggests
# a directory listing.
PROMPTS = [
    "What is the marker?",
    "Describe the project structure briefly.",
    "Where am I working right now?",
]

CONFIGS = [
    ("default agent", []),
    ("shim agent", ["--agent", "oc-shim"]),
]


def run(prompt, extra):
    env = dict(os.environ)
    env["PWD"] = WORK
    p = subprocess.run(
        [CLI, "run", "--format", "json", "--auto", *extra, "--model", MODEL],
        input=prompt, capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", cwd=WORK, env=env,
    )
    tools, text, err = [], "", None
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "tool_use":
            tools.append((o.get("part") or {}).get("tool", "?"))
        elif o.get("type") == "text":
            text += (o.get("part") or {}).get("text", "")
        elif o.get("type") == "error":
            err = (o.get("error") or {}).get("message", "?")
    return tools, text.strip(), err


print(f"{'prompt':<34} {'config':<14} {'tools fired':<28} answer")
print("-" * 100)
totals = {}
for label, extra in CONFIGS:
    t_all = 0
    for prompt in PROMPTS:
        tools, answer, err = run(prompt, extra)
        t_all += len(tools)
        shown = ",".join(tools) if tools else "-none-"
        ans = (err or answer).replace("\n", " ")[:26]
        print(f"{prompt[:32]:<34} {label:<14} {shown:<28} {ans}")
    totals[label] = t_all
    print("-" * 100)

print()
for label, _ in CONFIGS:
    print(f"  {label:<14} total phantom tool calls: {totals[label]} / {len(PROMPTS)} prompts")
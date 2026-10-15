"""Assert the proxy's SHIM_AGENT_MD matches the provisioned agent file on disk."""
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("p", "opencode_proxy.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

disk_path = m.agent_path()
with open(disk_path, encoding="utf-8") as fh:
    disk = fh.read().strip()
code = m.SHIM_AGENT_MD.strip()

if disk == code:
    print(f"IN SYNC  ({len(code)} chars)  {disk_path}")
    sys.exit(0)

print(f"DRIFT  disk={len(disk)}ch  code={len(code)}ch  ({disk_path})")
import difflib
for line in list(difflib.unified_diff(disk.splitlines(), code.splitlines(),
                                      "disk", "code", lineterm=""))[:30]:
    print(line)
sys.exit(1)
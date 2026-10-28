#!/usr/bin/env python3
"""Verify that the conversation hooks start the local server by themselves.

Why this exists: `sync_turn` / `prefetch` / `system_prompt_block` used to resolve
the client with `_ensure_client()`, which never spawns a server. A session that
never called a `viking_*` tool therefore silently dropped every turn — nothing was
recorded, and nothing was queued for a later commit. These checks pin the new
behaviour (hooks start the server) plus the guards that keep it cheap (short wait
budget, cooldown after a failure, tool calls unaffected by that cooldown).

It is a standalone script rather than a pytest module on purpose: the plugin's
pytest suite only runs from inside the hermes-agent repo, where this directory is
`plugins/openviking`. This one only needs a Hermes install on PYTHONPATH.

Run:
    PYTHONPATH=/path/to/hermes-agent \\
    HERMES_AGENT_PATH=/path/to/hermes-agent \\
        /path/to/hermes/venv/bin/python tests/verify_hook_autostart.py

Exit code 0 = all checks pass.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_SRC = Path(__file__).resolve().parents[1]
HERMES_AGENT = Path(
    os.environ.get("HERMES_AGENT_PATH") or Path.home() / ".hermes" / "hermes-agent"
)

tmp = Path(tempfile.mkdtemp(prefix="ov-hooktest-"))
home = tmp / "hermes"
target = home / "plugins" / "openviking"
target.parent.mkdir(parents=True)
shutil.copytree(
    PLUGIN_SRC, target, ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache")
)
(home / "config.yaml").write_text(
    "memory:\n  provider: openviking\n  openviking:\n"
    "    use_ovcli_config: false\n    endpoint: http://127.0.0.1:1933\n",
    encoding="utf-8",
)

os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
for key in list(os.environ):
    if key.startswith("OPENVIKING_"):
        del os.environ[key]
sys.path.insert(0, str(HERMES_AGENT))

import plugins.memory as memory  # noqa: E402
from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # noqa: E402

token = set_hermes_home_override(home)
try:
    provider = memory.load_memory_provider("openviking", register_skills=False)
finally:
    reset_hermes_home_override(token)
assert provider is not None, "provider load failed"
module = sys.modules[type(provider).__module__]

calls = {"start": 0, "wait": [], "build": 0}
health = {"state": "healthy"}


def fake_start(endpoint, *, hermes_home=None):
    calls["start"] += 1
    return module._LOCAL_SERVER_STARTED, "stub-started"


def fake_wait(endpoint, *, timeout_seconds, should_stop=None):
    calls["wait"].append(timeout_seconds)
    return True


def fake_health(client, endpoint):
    seq = health.get("seq")
    if seq:
        return seq.pop(0), "stub-seq"
    return health["state"], "stub"


class StubClient:
    def get(self, *args, **kwargs):
        return {"result": []}


def fake_build(endpoint=None):
    calls["build"] += 1
    return StubClient()


module._start_local_openviking_server = fake_start
module._wait_for_openviking_health = fake_wait
module._classify_runtime_openviking_health = fake_health
provider._build_client = fake_build
provider._endpoint = "http://127.0.0.1:1933"
provider._api_key = ""
provider._account = provider._user = provider._agent = "default"
provider._env_refresh_enabled = False
provider._shutting_down = False

results = {}

# 1) No client at all: the hook path must start the server and attach.
provider._client = None
provider._hook_start_cooldown_until = 0.0
before = calls["start"]
client = provider._ensure_client_for_tool(
    wait_seconds=module._LOCAL_OPENVIKING_HOOK_START_TIMEOUT, probe=True
)
results["hook_starts_when_missing"] = calls["start"] == before + 1 and client is not None
results["hook_uses_short_wait_budget"] = calls["wait"][-1] == module._LOCAL_OPENVIKING_HOOK_START_TIMEOUT

# 2) Cached client but the server stopped answering (idle shutdown/crash): drop + restart.
provider._client = StubClient()
provider._hook_start_cooldown_until = 0.0
health["state"] = "healthy"
health["seq"] = ["unreachable", "healthy"]
before = calls["start"]
client = provider._ensure_client_for_tool(probe=True, wait_seconds=5)
results["stale_client_replaced"] = calls["start"] == before + 1 and client is not None
health.pop("seq", None)

# 3) Healthy server: reuse the client, never respawn.
provider._client = StubClient()
kept = provider._client
before = calls["start"]
client = provider._ensure_client_for_tool(probe=True, wait_seconds=5)
results["healthy_client_reused"] = calls["start"] == before and client is kept

# 4) Inside the cooldown after a failed start: hooks skip instead of waiting again.
health["state"] = "healthy"
provider._client = None
provider._hook_start_cooldown_until = module.time.monotonic() + 60
before = calls["start"]
client = provider._ensure_client_for_tool(probe=True, wait_seconds=5)
results["cooldown_skips_start"] = calls["start"] == before and client is None
provider._hook_start_cooldown_until = 0.0

# 5) Tool calls ignore the cooldown: an explicit viking_* request must always try.
provider._client = None
provider._hook_start_cooldown_until = module.time.monotonic() + 60
before = calls["start"]
client = provider._ensure_client_for_tool()
results["tool_path_ignores_cooldown"] = calls["start"] == before + 1 and client is not None
provider._hook_start_cooldown_until = 0.0

# 6) A failed start arms the cooldown.
provider._client = None
health["state"] = "unhealthy"
module._wait_for_openviking_health = lambda endpoint, *, timeout_seconds, should_stop=None: False
provider._ensure_client_for_tool(probe=True, wait_seconds=1)
results["failure_arms_cooldown"] = provider._hook_start_cooldown_until > module.time.monotonic()

# 7) prefetch goes through the hook path.
seen = {"hook": 0}
original = provider._ensure_client_for_tool


def counting(*args, **kwargs):
    seen["hook"] += 1
    return original(*args, **kwargs)


provider._ensure_client_for_tool = counting
health["state"] = "healthy"
module._wait_for_openviking_health = fake_wait
provider._client = None
provider._session_id = "local-hooktest"
provider._user_id = "default"
before = seen["hook"]
try:
    provider.prefetch("这是一个足够长的查询文本，用来触发召回")
except Exception as exc:  # noqa: BLE001 - stub client is intentionally thin
    print(f"prefetch raised (stub client): {exc}")
results["prefetch_uses_hook"] = seen["hook"] > before

print("\n=== hook autostart checks ===")
all_passed = True
for name, passed in results.items():
    print(f"{'PASS' if passed else 'FAIL'}  {name}")
    all_passed = all_passed and bool(passed)
print("ALL PASS" if all_passed else "SOME FAILED")
sys.exit(0 if all_passed else 1)

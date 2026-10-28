# Fork notes — `examples/hermes-plugin`

This fork carries one local patch on top of `volcengine/OpenViking`. Everything
else tracks upstream `main`.

## Patch: start the local server from the tool boundary

**Symptom.** With a custom local connection (`OPENVIKING_ENDPOINT=http://127.0.0.1:1933`,
no Quick Local deployment) and no server running, every `viking_*` call in a
session fails immediately with `OpenViking server not connected`. The server is
never started, so the session stays broken. Startup only happens inside
`initialize()` → `_handle_runtime_openviking_unreachable`, and only on the first
session of a process; later sessions, or any session that reaches a tool before
that path runs, get the dead end above.

**Cause.** `handle_tool_call()` resolves the client with `_ensure_client()`,
which returns `self._client` untouched when `_env_refresh_enabled` is still
false — no health probe, no auto-start, `None`, tool error.

**Patch.** `_ensure_client_for_tool()` (new) is used by `handle_tool_call()` and
`_tool_remember()`:

1. `_ensure_client()` as before — a live client is still the fast path.
2. If the endpoint is unresolved, resolve it the way `initialize()` does.
3. If it is a local URL and no runtime-start is already in flight, spawn
   `openviking-server` from `PATH` (upstream's `_start_local_openviking_server`).
4. Wait for health up to `_LOCAL_OPENVIKING_AUTOSTART_TIMEOUT` (60 s), then build
   and publish the client directly — `_ensure_client_locked()` would short-circuit
   on the in-flight waiter thread or on the 30 s cooldown the failed probe armed.

Cost: the first tool call of a cold session blocks instead of failing (~5–15 s
with a warm model cache, longer on first model load). No behaviour change when a
server is already up.

## Local prerequisites this patch assumes

- `openviking-server` on `PATH`. On macOS a wrapper in `~/.local/bin` is needed
  anyway: Hermes exports `PYTHONPATH` pointing at its 3.14 site-packages, which
  makes the uv-tool 3.13 interpreter load a mismatched `pydantic_core`.
- The server reads its models from `~/.openviking/ov.conf`; model API keys are
  not inherited by the spawned child, so the wrapper bootstraps them from
  `~/.hermes/.env`.

## Merging an upstream release

```bash
git fetch upstream main          # upstream = https://github.com/volcengine/OpenViking
git rebase upstream/main         # the patch is one commit, so this stays clean
# conflicts, if any, land in _ensure_client_for_tool / handle_tool_call / _tool_remember
git push origin main --force-with-lease
hermes plugins install Riding-A-Colt/OpenViking/examples/hermes-plugin --force --yes-deps
```

If upstream starts auto-starting from the tool boundary itself, drop the patch
commit rather than rebasing it:

```bash
git log --oneline upstream/main -- examples/hermes-plugin | head   # check for the fix
git rebase --skip   # or: git revert <patch-commit>
```

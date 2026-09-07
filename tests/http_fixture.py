"""Real HTTP/UI with temporary state and stub inference, never user cron or Ollama."""
from contextlib import contextmanager, ExitStack, nullcontext
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from unittest import mock

from desk import alerts, catalog, connectors, crontab_sync, http_api, installer, jobs, mcp_host, ollama_ctl, paths, ramgate, runner, state, tools


@contextmanager
def isolated_server():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        old_data = paths.DATA
        data = Path(tmp) / "data"
        for module in (paths, state, jobs, connectors, runner, ollama_ctl, http_api, catalog, tools):
            for name, value in list(vars(module).items()):
                if isinstance(value, Path) and value.is_relative_to(old_data):
                    stack.enter_context(mock.patch.object(module, name, data / value.relative_to(old_data)))
        stack.enter_context(mock.patch.object(connectors, "discover", return_value=[]))
        stack.enter_context(mock.patch.object(crontab_sync, "apply", return_value={"ok": True, "preview": "", "jobs": 0}))
        stack.enter_context(mock.patch.object(crontab_sync, "current_user_crontab", return_value=""))
        stack.enter_context(mock.patch.object(alerts, "notify", return_value={"ok": True, "test_stub": True}))
        stack.enter_context(mock.patch.object(ollama_ctl, "running", return_value=False))
        stack.enter_context(mock.patch.object(ollama_ctl, "model_inventory", return_value=[{"name": "fixture-model:latest"}]))
        stack.enter_context(mock.patch.object(ollama_ctl, "session", side_effect=lambda *a, **k: nullcontext()))
        forbidden = AssertionError("Unexpected external operation in isolated HTTP fixture")
        for module, names in ((ollama_ctl, ("start", "stop", "_post", "_get", "chat_messages")),
                              (installer, ("setup", "remove")), (runner, ("_run_cli",)),
                              (mcp_host, ("open_for_job",)), (tools, ("sandbox_run", "_fetch"))):
            for name in names:
                stack.enter_context(mock.patch.object(module, name, side_effect=forbidden))
        def chat(model, prompt, **kwargs):
            return {"message": {"content": "" if prompt == "EMPTY_FIXTURE" else "격리된 테스트 응답"}}
        stack.enter_context(mock.patch.object(ollama_ctl, "chat", side_effect=chat))
        stack.enter_context(mock.patch.object(catalog, "build", return_value={"models": [], "providers": [], "agents": []}))
        snapshot = {"total_gb": 32, "free_gb": 24, "avail_gb": 24, "pressure_level": 1, "pressure_pct": 0, "swap_used_gb": 0, "swap_total_gb": 0, "swap_warn": False, "loaded": []}
        stack.enter_context(mock.patch.object(ramgate, "snapshot", return_value=snapshot))
        stack.enter_context(mock.patch.object(ramgate, "model_need_gb", return_value=3.0))
        stack.enter_context(mock.patch.object(ramgate, "decide", side_effect=lambda model, *a, **k: {"action": "run", "model": model, "reason": "fixture"}))
        paths.ensure_dirs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_api.Handler)
        stack.enter_context(mock.patch.object(http_api, "PORT", server.server_address[1]))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    with isolated_server() as base:
        print(json.dumps({"base": base}), flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass

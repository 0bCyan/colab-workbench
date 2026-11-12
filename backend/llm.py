"""Independent LLM sessions: external probes and video sessions are never adopted."""
import asyncio
import base64
import contextlib
import json
import secrets
import time
import uuid

import aiohttp

from .colab import clean
from .config import ROOT
from .llm_presets import LLAMA_COMMIT, LLM_ROOT, default_config, preset, validate_config
from .telemetry import Telemetry

BUSY = {"checking", "provisioning", "deploying", "stopping"}


class LLM:
    def __init__(self, store, colab, profiles):
        self.store, self.colab, self.profiles = store, colab, {p["id"]: p for p in profiles}
        self.locks = {key: asyncio.Lock() for key in self.profiles}
        self.tasks, self.stops = {}, {}
        self.stopping = set()
        self.active = {key: 0 for key in self.profiles}
        self.telemetry = Telemetry(self)
        self.http = None
        self.closing = False
        self.maintenance = None
        if not store.get("meta", "llm_api_key"):
            store.put("meta", "llm_api_key", {"value": secrets.token_urlsafe(32)})
        for account in self.profiles:
            if not store.get("llm", account):
                store.put("llm", account, {"account": account, "phase": "stopped", "session": None, "port": None,
                                           "message": "按需启动服务", "config": default_config()})

    async def open(self):
        self.http = aiohttp.ClientSession()
        for account in self.profiles:
            if self.machine(account).get("session"):
                self.machine(account, phase="disconnected", port=None, telemetry_port=None, message="服务已重启，请重新连接")
        self.maintenance = asyncio.create_task(self.maintain())

    def machine(self, account, **changes):
        if account not in self.profiles:
            raise ValueError("未知账号")
        return self.store.patch("llm", account, **changes) if changes else self.store.get("llm", account)

    def public(self, account):
        item = self.machine(account)
        # Stable, account-bound proxy paths never expose the random tunnel port.
        return {k: v for k, v in {**item, "endpoint_path": f"/llm/{account}/v1", "model_name": "workbench-llm",
                                  "active_requests": self.active[account]}.items() if k not in {"port", "telemetry_port"}}

    def key(self):
        return self.store.get("meta", "llm_api_key")["value"]

    def log(self, account, message):
        items = self.store.get("llm_logs", account, [])
        message = clean(str(message)).replace(self.key(), "[redacted]")
        self.store.put("llm_logs", account, (items + [{"time": time.time(), "message": message}])[-150:])

    def owned(self, account, session):
        install = self.store.get("meta", "install")["id"]
        return session.startswith(f"wbllm-{install}-{account.removeprefix('colab')}-")

    def configure(self, account, value):
        item = self.machine(account)
        validate_config(value)
        if value == item["config"]:
            return self.public(account)
        if item.get("session") or item["phase"] in BUSY:
            raise ValueError("请先关闭此服务，再更换部署配置")
        self.machine(account, config=value)
        return self.public(account)

    def request_start(self, account):
        item = self.machine(account)
        profile = self.profiles[account]
        if self.closing or account in self.stopping:
            raise ValueError("服务正在停止")
        video = self.store.get("machines", account, {})
        if video.get("session") or video.get("phase") in BUSY or any(
            job["account"] == account and job["status"] in {"pending", "preparing", "submitting", "queued", "running", "downloading", "recovering"}
            for job in self.store.all("jobs")
        ):
            raise ValueError("此账号已有视频机器或任务，请先结束任务并关闭机器，或选择其他账号")
        if not profile["installed"] or not profile["credential_present"]:
            raise ValueError("请先配置此账号的 CLI 入口与登录")
        validate_config(item["config"])
        if item["phase"] == "ready" and item.get("port"):
            return self.public(account)
        if account not in self.tasks or self.tasks[account].done():
            self.machine(account, phase="checking", message="核对账号与会话")
            self.tasks[account] = asyncio.create_task(self.start(account))
        return self.public(account)

    def bootstrap(self, account):
        recipe = preset(self.machine(account)["config"]["preset_id"])
        config = {**self.machine(account)["config"], "recipe": recipe, "llama_commit": LLAMA_COMMIT}
        files = {"deploy.py": (ROOT / "backend/deploy_llm_remote.py").read_bytes(),
                 "telemetry.py": (ROOT / "backend/telemetry_remote.py").read_bytes(),
                 "config.json": json.dumps(config).encode()}
        code = "import base64,subprocess,sys\nfrom pathlib import Path\n" + f"root=Path({LLM_ROOT!r});root.mkdir(exist_ok=True,parents=True)\n"
        for name, data in files.items():
            code += f"(root/{name!r}).write_bytes(base64.b64decode({base64.b64encode(data).decode()!r}))\n"
        code += ("(root/'status.json').unlink(missing_ok=True)\n"
                 "with (root/'telemetry.log').open('ab') as log:\n"
                 " subprocess.Popen([sys.executable,str(root/'telemetry.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)\n"
                 "with (root/'deploy.log').open('ab') as log:\n"
                 " subprocess.Popen([sys.executable,str(root/'deploy.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)\n"
                 "print('LLM deployment started')\n")
        path = self.store.directory / f"{account}-llm-bootstrap.py"
        path.write_text(code)
        return path

    async def models(self, account):
        port = self.machine(account).get("port")
        if not port:
            raise RuntimeError("服务未连接")
        async with self.http.get(f"http://127.0.0.1:{port}/v1/models", timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status()
            body = await response.json()
        if not any(m.get("id") == "workbench-llm" for m in body.get("data", [])):
            raise RuntimeError("服务模型未就绪")
        return body

    async def start(self, account):
        async with self.locks[account]:
            try:
                item = self.machine(account)
                if item.get("session") and not self.owned(account, item["session"]):
                    raise RuntimeError("会话归属不符，拒绝连接")
                if item.get("port"):
                    try:
                        await self.models(account)
                        self.machine(account, phase="ready", message="服务已就绪")
                        return
                    except (aiohttp.ClientError, TimeoutError, RuntimeError):
                        pass
                sessions = await self.colab.sessions(account)
                session = item.get("session")
                if session and not self.owned(account, session):
                    raise RuntimeError("会话归属不符，拒绝连接")
                exists = any(s["name"] == session for s in sessions)
                if account in self.stopping:
                    return
                if not exists:
                    if sessions:
                        raise RuntimeError("此账号另有运行中的会话；请等待性能测试或其他任务结束，或选择空闲账号")
                    install = self.store.get("meta", "install")["id"]
                    session = f"wbllm-{install}-{account.removeprefix('colab')}-{uuid.uuid4().hex[:6]}"
                    self.machine(account, session=session, phase="provisioning", port=None, created_at=time.time())
                    self.log(account, "申请独立 G4 服务机器")
                    await self.colab.create(account, session)
                if account in self.stopping:
                    return
                self.machine(account, phase="deploying", message="准备模型服务")
                port = await self.colab.tunnel(account, session, self.store.get("settings", "main")["identity"], service_port=8000)
                self.machine(account, port=port, telemetry_port=self.colab.telemetry_ports.get(account))
                # A reconnect must reuse a running process, not overwrite its deployment.
                try:
                    await self.models(account)
                    self.machine(account, phase="ready", message="服务已重新连接", last_activity=time.time())
                    return
                except (aiohttp.ClientError, TimeoutError, RuntimeError):
                    pass
                self.log(account, await self.colab.execute(account, session, self.bootstrap(account)))
                check = self.store.directory / f"{account}-llm-check.py"
                check.write_text("from pathlib import Path\n" + f"root=Path({LLM_ROOT!r})\n"
                                 "p=root/'status.json'\nprint('LLM_STATUS:'+p.read_text() if p.exists() else 'LLM_STATUS:{}')\n"
                                 "for name in ('deploy.log','server.log'):\n"
                                 " p=root/name\n"
                                 " if p.exists():\n"
                                 "  with p.open('rb') as f:\n"
                                 "   f.seek(max(0,p.stat().st_size-1800));print(f.read().decode(errors='replace'))\n")
                deadline, checked = time.monotonic() + 3600, 0
                while time.monotonic() < deadline:
                    if account in self.stopping:
                        return
                    try:
                        await self.models(account)
                        self.machine(account, phase="ready", message="服务已就绪", last_activity=time.time())
                        self.log(account, "本地端点已连接")
                        return
                    except (aiohttp.ClientError, TimeoutError, RuntimeError):
                        pass
                    if time.monotonic() - checked > 12:
                        output = await self.colab.execute(account, session, check)
                        self.log(account, output)
                        for line in output.splitlines():
                            if line.startswith("LLM_STATUS:"):
                                state = json.loads(line.removeprefix("LLM_STATUS:"))
                                if state.get("phase") == "error":
                                    raise RuntimeError(state.get("message", "部署失败"))
                                self.machine(account, message=state.get("message", "准备模型服务"), deployment=state)
                        checked = time.monotonic()
                    await asyncio.sleep(3)
                raise TimeoutError("服务部署超过 60 分钟，请查看日志或关闭机器")
            except Exception as exc:
                self.machine(account, phase="error", message=clean(str(exc)), last_activity=time.time())
                self.log(account, "启动失败：" + str(exc))

    def acquire(self, account):
        item = self.machine(account)
        if item["phase"] != "ready" or not item.get("port") or account in self.stopping or self.closing:
            raise ValueError("服务未就绪，请在工作台启动或重新连接")
        if self.active[account]:
            raise ValueError("此服务已有请求正在生成，请稍后重试")
        self.active[account] += 1
        self.machine(account, last_activity=time.time())
        return item

    def release(self, account):
        self.active[account] = max(0, self.active[account] - 1)
        self.machine(account, last_activity=time.time())

    def request_stop(self, account):
        self.machine(account)
        if self.active[account]:
            raise ValueError("服务正在生成，请等待请求结束后关闭")
        if account not in self.stops or self.stops[account].done():
            self.stopping.add(account)
            self.stops[account] = asyncio.create_task(self.stop(account))
        return self.public(account)

    async def stop(self, account):
        async with self.locks[account]:
            try:
                session = self.machine(account).get("session")
                if session:
                    if not self.owned(account, session):
                        raise RuntimeError("此会话不属于本工作台 LLM 服务，拒绝停止")
                    self.machine(account, phase="stopping", message="释放服务机器")
                    await self.colab.stop(account, session)
                await self.colab.close_tunnel(account)
                self.machine(account, phase="stopped", session=None, port=None, telemetry_port=None, deployment=None, message="服务已关闭，机器已释放")
                self.log(account, "服务机器已释放")
            except Exception as exc:
                self.machine(account, phase="error", message="关机未确认：" + clean(str(exc)))
                self.log(account, str(exc))
            finally:
                self.stopping.discard(account)

    async def idle_sweep(self):
        minutes = self.store.get("settings", "main")["idle_minutes"]
        for account in self.profiles:
            item = self.machine(account)
            if self.active[account] or item["phase"] not in {"ready", "error", "disconnected"} or not item.get("session"):
                continue
            if minutes and time.time() - item.get("last_activity", item.get("created_at", time.time())) > minutes * 60:
                self.request_stop(account)
            elif item["phase"] == "ready":
                try:
                    await self.models(account)
                except (aiohttp.ClientError, TimeoutError, RuntimeError):
                    self.machine(account, phase="disconnected", message="服务连接中断，请重新连接或关闭机器")

    async def maintain(self):
        while True:
            await asyncio.sleep(15)
            await self.idle_sweep()

    async def close(self):
        self.closing = True
        if self.maintenance:
            self.maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.maintenance
        # Wait for in-flight allocations to reconcile before stopping owned VMs.
        for account, task in self.tasks.items():
            if not task.done():
                self.stopping.add(account)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.store.get("settings", "main")["stop_on_exit"]:
            for account in self.profiles:
                if self.machine(account).get("session") and not self.active[account]:
                    self.request_stop(account)
        await asyncio.gather(*self.stops.values(), return_exceptions=True)
        await self.colab.close()
        if self.http:
            await self.http.close()

"""Each LLM instance owns its own VM, tunnel, endpoint and request slot."""
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
from .llm_presets import LLM_ROOT, default_config, preset, validate_config
from .telemetry import Telemetry

BUSY = {"checking", "provisioning", "deploying", "stopping"}
TABLE = "llm_instances"


class LLM:
    def __init__(self, store, colab, profiles):
        self.store, self.colab, self.profiles = store, colab, {p["id"]: p for p in profiles}
        self.tasks, self.stops, self.locks, self.active = {}, {}, {}, {}
        self.stopping = set()
        self.telemetry = Telemetry(self)
        self.http = None
        self.closing = False
        self.maintenance = None
        if not store.get("meta", "llm_api_key"):
            store.put("meta", "llm_api_key", {"value": secrets.token_urlsafe(32)})
        self.migrate()
        for item in self.all():
            self.register(item["id"])

    def register(self, key):
        self.locks[key] = asyncio.Lock()
        self.active[key] = 0

    def migrate(self):
        """Atomically preserve configured or live account-era services and their logs."""
        if self.store.get("meta", "llm_instances_migrated") is not None:
            return
        records = []
        for item in self.store.all("llm"):
            logs = self.store.get("llm_logs", item["account"], [])
            if not item.get("session") and item["phase"] == "stopped" and item["config"] == default_config() and not logs:
                continue
            key = "svc-" + uuid.uuid4().hex[:12]
            migrated = {**item, "id": key, "created_at": item.get("created_at", time.time()),
                        "legacy_path": item["account"], "legacy_session": item.get("session"),
                        "legacy_model": "workbench-llm" if item.get("session") else None}
            records.extend([(TABLE, key, json.dumps(migrated)), ("llm_logs", key, json.dumps(logs))])
        with self.store.db:
            self.store.db.execute("DELETE FROM records WHERE kind IN ('llm','llm_logs')")
            self.store.db.executemany("INSERT OR REPLACE INTO records VALUES(?,?,?)", records)
            self.store.db.execute("INSERT INTO records VALUES(?,?,?)", ("meta", "llm_instances_migrated", '{}'))

    def all(self):
        return sorted(self.store.all(TABLE), key=lambda item: item.get("created_at", 0))

    async def open(self):
        self.http = aiohttp.ClientSession()
        for item in self.all():
            if item.get("session"):
                self.machine(item["id"], phase="disconnected", port=None, telemetry_port=None, message="服务已重启，请重新连接")
        self.maintenance = asyncio.create_task(self.maintain())

    def machine(self, key, **changes):
        if key not in self.locks:
            raise ValueError("服务实例不存在")
        return self.store.patch(TABLE, key, **changes) if changes else self.store.get(TABLE, key)

    def resolve(self, key):
        if key in self.locks:
            return key
        # Legacy paths remain attached to exactly the migrated instance, never an account's newest VM.
        for item in self.all():
            if item.get("legacy_path") == key:
                return item["id"]
        raise ValueError("服务实例不存在")

    def model_name(self, key):
        item = self.machine(key)
        return item.get("legacy_model") or preset(item["config"]["preset_id"])["model_name"]

    def public(self, key):
        item = self.machine(key)
        return {k: v for k, v in {**item, "endpoint_path": f"/llm/{key}/v1", "model_name": self.model_name(key),
                                  "active_requests": self.active[key]}.items()
                if k not in {"port", "telemetry_port", "legacy_session", "legacy_model"}}

    def key(self):
        return self.store.get("meta", "llm_api_key")["value"]

    def log(self, key, message):
        items = self.store.get("llm_logs", key, [])
        message = clean(str(message)).replace(self.key(), "[redacted]")
        self.store.put("llm_logs", key, (items + [{"time": time.time(), "message": message}])[-150:])

    def owned(self, key, session):
        item = self.machine(key)
        install = self.store.get("meta", "install")["id"]
        prefix = f"wbllm-{install}-{item['account'].removeprefix('colab')}-"
        return session.startswith(prefix + key.removeprefix('svc-') + '-') or (
            session == item.get("legacy_session") and session.startswith(prefix))

    def validate(self, account, config):
        if account not in self.profiles:
            raise ValueError("未知账号")
        validate_config(config)

    def create(self, account, config):
        self.validate(account, config)
        key = "svc-" + uuid.uuid4().hex[:12]
        self.store.put(TABLE, key, {"id": key, "account": account, "phase": "stopped", "session": None,
                                  "port": None, "message": "按需启动服务", "config": config, "created_at": time.time()})
        self.register(key)
        return self.public(key)

    def configure(self, key, account, config):
        item = self.machine(key)
        self.validate(account, config)
        if config == item["config"] and account == item["account"]:
            return self.public(key)
        if item.get("session") or item["phase"] in BUSY or key in self.stopping:
            raise ValueError("请先关闭此实例，再更换部署配置或账号")
        self.machine(key, account=account, config=config, deployment=None, legacy_model=None)
        return self.public(key)

    def delete(self, key):
        item = self.machine(key)
        if item.get("session") or item["phase"] in BUSY or self.active[key] or key in self.stopping:
            raise ValueError("请先关闭此实例并确认机器已释放")
        self.store.delete(TABLE, key)
        self.store.delete("llm_logs", key)
        self.telemetry.cache.pop(key, None)
        self.locks.pop(key)
        self.active.pop(key)
        self.tasks.pop(key, None)
        self.stops.pop(key, None)
        return {"deleted": key}

    def request_start(self, key):
        item = self.machine(key)
        profile = self.profiles.get(item["account"])
        if self.closing or key in self.stopping:
            raise ValueError("服务正在停止")
        if not profile or not profile["installed"] or not profile["credential_present"]:
            raise ValueError("请先配置此账号的 CLI 入口与登录")
        validate_config(item["config"])
        if item["phase"] == "ready" and item.get("port"):
            return self.public(key)
        if key not in self.tasks or self.tasks[key].done():
            self.machine(key, phase="checking", message="核对此实例的云端会话")
            self.tasks[key] = asyncio.create_task(self.start(key))
        return self.public(key)

    def bootstrap(self, key):
        recipe = preset(self.machine(key)["config"]["preset_id"])
        config = {**self.machine(key)["config"], "recipe": recipe}
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
        path = self.store.directory / f"{key}-llm-bootstrap.py"
        path.write_text(code)
        return path

    async def models(self, key):
        port = self.machine(key).get("port")
        expected_model = self.model_name(key)
        if not port:
            raise RuntimeError("服务未连接")
        async with self.http.get(f"http://127.0.0.1:{port}/v1/models", timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status()
            body = await response.json()
        if not any(m.get("id") == expected_model for m in body.get("data", [])):
            raise RuntimeError("服务模型未就绪")
        return body

    async def start(self, key):
        async with self.locks[key]:
            try:
                item = self.machine(key)
                account, session = item["account"], item.get("session")
                if session and not self.owned(key, session):
                    raise RuntimeError("会话归属不符，拒绝连接")
                sessions = await self.colab.sessions(account)
                if key in self.stopping:
                    return
                if not any(s["name"] == session for s in sessions):
                    await self.colab.close_tunnel(key)
                    install = self.store.get("meta", "install")["id"]
                    session = f"wbllm-{install}-{account.removeprefix('colab')}-{key.removeprefix('svc-')}-{uuid.uuid4().hex[:6]}"
                    self.machine(key, session=session, phase="provisioning", port=None, telemetry_port=None,
                                 legacy_session=None, legacy_model=None, deployment=None,
                                 message="申请此实例的 G4 机器", last_activity=time.time())
                    self.log(key, "申请独立 G4 服务机器")
                    try:
                        await self.colab.create(account, session)
                    except Exception as exc:
                        # A CLI failure can be ambiguous. Clear ownership only after a successful absence check.
                        try:
                            remaining = await self.colab.sessions(account)
                            if not any(s["name"] == session for s in remaining):
                                self.machine(key, session=None)
                        except Exception:
                            pass
                        raise RuntimeError("申请机器失败，可更换账号重试：" + str(exc)) from exc
                if key in self.stopping:
                    return
                self.machine(key, phase="deploying", message="准备模型服务")
                port = await self.colab.tunnel(account, session, self.store.get("settings", "main")["identity"],
                                               service_port=8000, tunnel_key=key)
                self.machine(key, port=port, telemetry_port=self.colab.telemetry_ports.get(key))
                try:
                    await self.models(key)
                    self.machine(key, phase="ready", message="服务已重新连接", last_activity=time.time())
                    return
                except (aiohttp.ClientError, TimeoutError, RuntimeError):
                    pass
                self.machine(key, legacy_model=None)
                self.log(key, await self.colab.execute(account, session, self.bootstrap(key)))
                check = self.store.directory / f"{key}-llm-check.py"
                check.write_text("from pathlib import Path\n" + f"root=Path({LLM_ROOT!r})\n"
                                 "p=root/'status.json'\nprint('LLM_STATUS:'+p.read_text() if p.exists() else 'LLM_STATUS:{}')\n"
                                 "for name in ('deploy.log','server.log'):\n"
                                 " p=root/name\n"
                                 " if p.exists():\n"
                                 "  with p.open('rb') as f:\n"
                                 "   f.seek(max(0,p.stat().st_size-1800));print(f.read().decode(errors='replace'))\n")
                deadline, checked = time.monotonic() + 3600, 0
                while time.monotonic() < deadline:
                    if key in self.stopping:
                        return
                    try:
                        await self.models(key)
                        self.machine(key, phase="ready", message="服务已就绪", last_activity=time.time())
                        self.log(key, "本地端点已连接")
                        return
                    except (aiohttp.ClientError, TimeoutError, RuntimeError):
                        pass
                    if time.monotonic() - checked > 12:
                        output = await self.colab.execute(account, session, check)
                        self.log(key, output)
                        for line in output.splitlines():
                            if line.startswith("LLM_STATUS:"):
                                state = json.loads(line.removeprefix("LLM_STATUS:"))
                                if state.get("phase") == "error":
                                    raise RuntimeError(state.get("message", "部署失败"))
                                self.machine(key, message=state.get("message", "准备模型服务"), deployment=state)
                        checked = time.monotonic()
                    await asyncio.sleep(3)
                raise TimeoutError("服务部署超过 60 分钟，请查看日志或关闭机器")
            except Exception as exc:
                self.machine(key, phase="error", message=clean(str(exc)), last_activity=time.time())
                self.log(key, "启动失败：" + str(exc))

    def acquire(self, key):
        item = self.machine(key)
        if item["phase"] != "ready" or not item.get("port") or key in self.stopping or self.closing:
            raise ValueError("服务未就绪，请在工作台启动或重新连接")
        if self.active[key]:
            raise ValueError("此实例已有请求正在生成，请稍后重试")
        self.active[key] += 1
        self.machine(key, last_activity=time.time())
        return item

    def release(self, key):
        self.active[key] = max(0, self.active[key] - 1)
        self.machine(key, last_activity=time.time())

    def request_stop(self, key):
        item = self.machine(key)
        if self.active[key]:
            raise ValueError("服务正在生成，请等待请求结束后关闭")
        if key not in self.stops or self.stops[key].done():
            self.stopping.add(key)
            if item.get("session") or item["phase"] in BUSY:
                self.machine(key, phase="stopping", message="等待当前操作结束并释放此实例的机器")
            self.stops[key] = asyncio.create_task(self.stop(key))
        return self.public(key)

    async def stop(self, key):
        async with self.locks[key]:
            try:
                item = self.machine(key)
                if item.get("session"):
                    if not self.owned(key, item["session"]):
                        raise RuntimeError("此会话不属于本实例，拒绝停止")
                    self.machine(key, phase="stopping", message="释放此实例的机器")
                    await self.colab.stop(item["account"], item["session"])
                await self.colab.close_tunnel(key)
                self.machine(key, phase="stopped", session=None, port=None, telemetry_port=None, deployment=None,
                             legacy_session=None, legacy_model=None, message="服务已关闭，机器已释放")
                self.log(key, "此实例的机器已释放")
            except Exception as exc:
                self.machine(key, phase="error", message="关机未确认：" + clean(str(exc)))
                self.log(key, str(exc))
            finally:
                self.stopping.discard(key)

    async def idle_sweep(self):
        minutes = self.store.get("settings", "main")["idle_minutes"]
        for item in self.all():
            key = item["id"]
            if key not in self.locks:
                continue
            if self.active[key] or item["phase"] not in {"ready", "error", "disconnected"} or not item.get("session"):
                continue
            if minutes and time.time() - item.get("last_activity", item.get("created_at", time.time())) > minutes * 60:
                self.request_stop(key)
            elif item["phase"] == "ready":
                try:
                    await self.models(key)
                except (aiohttp.ClientError, TimeoutError, RuntimeError):
                    if key in self.locks and self.machine(key).get("session") == item["session"]:
                        self.machine(key, phase="disconnected", message="服务连接中断，请重新连接或关闭此实例")

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
        for key, task in self.tasks.items():
            if not task.done():
                self.stopping.add(key)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.store.get("settings", "main")["stop_on_exit"]:
            for item in self.all():
                if item.get("session") and not self.active[item["id"]]:
                    self.request_stop(item["id"])
        await asyncio.gather(*self.stops.values(), return_exceptions=True)
        await self.colab.close()
        if self.http:
            await self.http.close()

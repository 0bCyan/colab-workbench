import asyncio
import base64
import contextlib
import json
import time
import uuid
from pathlib import Path

import aiohttp

from .colab import clean
from .config import REMOTE_ROOT, ROOT, default_identity
from .telemetry import Telemetry
from .workflows import validate_graph

ACTIVE = {"pending", "preparing", "submitting", "queued", "running", "downloading", "recovering"}


class Manager:
    def __init__(self, store, colab, profiles):
        self.store, self.colab, self.profiles = store, colab, profiles
        self.locks = {p["id"]: asyncio.Lock() for p in profiles}
        self.start_tasks, self.stop_tasks, self.workers = {}, {}, {}
        self.stopping = set()
        self.job_locks = {}
        self.inflight_jobs = set()
        self.http = None
        self.telemetry = Telemetry(self)
        self.maintenance = None
        self.closing = False
        self.refresh_locks = {p["id"]: asyncio.Lock() for p in profiles}
        if not store.get("settings", "main"):
            store.put("settings", "main", {"identity": default_identity(), "idle_minutes": 15, "stop_on_exit": True})
        if not store.get("meta", "install"):
            store.put("meta", "install", {"id": uuid.uuid4().hex[:8]})
        for p in profiles:
            if not store.get("machines", p["id"]):
                store.put("machines", p["id"], {"account": p["id"], "phase": "stopped", "session": None, "message": "按需启动 GPU", "port": None})

    async def open(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        for machine in self.store.all("machines"):
            if machine.get("session"):
                self.machine(machine["account"], phase="disconnected", port=None, telemetry_port=None, message="本地服务已重启，请重新连接并核对云端会话")
        for job in self.store.all("jobs"):
            if job["status"] in ACTIVE:
                self.job(job["id"], status="recovering", message="等待重新连接；已提交的任务不会自动重复提交")
        self.maintenance = asyncio.create_task(self.maintain())

    def machine(self, account, **changes):
        if account not in self.locks:
            raise ValueError("未知账号")
        return self.store.patch("machines", account, **changes) if changes else self.store.get("machines", account)

    def job(self, key, **changes):
        return self.store.patch("jobs", key, **changes) if changes else self.store.get("jobs", key)

    def log(self, account, message):
        item = {"time": time.time(), "message": clean(str(message))}
        logs = self.store.get("logs", account, [])
        self.store.put("logs", account, (logs + [item])[-150:])

    def active_jobs(self, account):
        return sorted((j for j in self.store.all("jobs") if j["account"] == account and j["status"] in ACTIVE), key=lambda j: j["created_at"])

    async def refresh(self, account):
        async with self.refresh_locks[account]:
            usage, sessions = await asyncio.gather(self.colab.usage(account), self.colab.sessions(account))
            self.store.put("accounts", account, {"usage": usage, "sessions": sessions, "checked_at": time.time(), "error": None})
            machine = self.machine(account)
            # Never mark a pending allocation stopped; it may not yet appear in sessions.
            if machine.get("session") and machine["phase"] not in {"provisioning", "stopping"} and not any(s["name"] == machine["session"] for s in sessions):
                self.machine(account, phase="stopped", session=None, port=None, message="云端会话已释放")
                await self.colab.close_tunnel(account)
            return self.store.get("accounts", account)

    def request_start(self, account):
        self.check_service_slot(account)
        if account in self.stopping:
            raise ValueError("机器正在停止，请稍候")
        if account not in self.start_tasks or self.start_tasks[account].done():
            # Reserve synchronously so a simultaneous LLM start cannot claim this account.
            if self.machine(account)["phase"] != "ready":
                self.machine(account, phase="checking", message="核对账号与云端会话")
            self.start_tasks[account] = asyncio.create_task(self.start(account))
        return self.machine(account)

    def check_service_slot(self, account):
        self.machine(account)
        service = self.store.get("llm", account, {})
        if service.get("session") or service.get("phase") in {"checking", "provisioning", "deploying", "stopping"}:
            raise ValueError("此账号已有 LLM 服务，请先关闭服务或选择其他账号")

    async def start(self, account):
        async with self.locks[account]:
            machine = self.machine(account)
            if machine["phase"] == "ready":
                try:
                    await self.remote(account, "GET", "/system_stats")
                    self.ensure_worker(account)
                    return
                except Exception:
                    pass
            try:
                self.machine(account, phase="checking", message="核对账号与云端会话")
                sessions = await self.colab.sessions(account)
                if account in self.stopping:
                    return
                session = machine.get("session")
                exists = any(s["name"] == session for s in sessions)
                if session and not exists:
                    # Recovering jobs cannot be silently rerun on a replacement VM.
                    for job in self.active_jobs(account):
                        if job.get("prompt_id"):
                            self.job(job["id"], status="failed", message="原云端会话已释放，无法恢复输出。请新建任务。")
                    session = None
                if not session:
                    install = self.store.get("meta", "install")["id"]
                    session = f"wb-{install}-{account.removeprefix('colab')}-{uuid.uuid4().hex[:6]}"
                    self.machine(account, session=session, phase="provisioning", message="申请 G4 / RTX PRO 6000", created_at=time.time())
                    self.log(account, f"申请会话 {session}")
                    await self.colab.create(account, session)
                if account in self.stopping:
                    return
                self.machine(account, phase="deploying", message="准备固定版本 ComfyUI 和模型")
                try:
                    await self.refresh(account)
                except Exception as exc:
                    self.log(account, f"额度刷新暂时失败，部署继续：{exc}")
                deploy = (ROOT / "backend/deploy_remote.py").read_bytes()
                telemetry = (ROOT / "backend/telemetry_remote.py").read_bytes()
                bootstrap = self.store.directory / f"{account}-bootstrap.py"
                bootstrap.write_text(
                    "import base64, subprocess, sys\nfrom pathlib import Path\n"
                    f"root=Path({REMOTE_ROOT!r}); root.mkdir(parents=True,exist_ok=True)\n"
                    f"(root/'deploy_remote.py').write_bytes(base64.b64decode({base64.b64encode(deploy).decode()!r}))\n"
                    f"(root/'telemetry_remote.py').write_bytes(base64.b64decode({base64.b64encode(telemetry).decode()!r}))\n"
                    "with (root/'telemetry.log').open('ab') as log:\n"
                    " subprocess.Popen([sys.executable,str(root/'telemetry_remote.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)\n"
                    "with (root/'deploy.log').open('ab') as log:\n"
                    " p=subprocess.Popen([sys.executable,str(root/'deploy_remote.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)\n"
                    "print('Deployment started',p.pid)\n"
                )
                self.log(account, await self.colab.execute(account, session, bootstrap))
                port = await self.colab.tunnel(account, session, self.store.get("settings", "main")["identity"])
                self.machine(account, port=port, telemetry_port=self.colab.telemetry_ports.get(account))
                check = self.store.directory / f"{account}-check.py"
                check.write_text(
                    "from pathlib import Path\nimport json\n"
                    f"p=Path({REMOTE_ROOT!r})\n"
                    "s=p/'deploy-status.json'\n"
                    "print('WB_STATUS:'+s.read_text() if s.exists() else 'WB_STATUS:{}')\n"
                    "for name in ('deploy.log','server.log'):\n"
                    " f=p/name\n"
                    " if f.exists():\n"
                    "  with f.open('rb') as stream:\n"
                    "   stream.seek(max(0,f.stat().st_size-2500)); print(name+':'+stream.read().decode(errors='replace'))\n"
                )
                deadline, last_check = time.monotonic() + 3600, 0
                while time.monotonic() < deadline:
                    if account in self.stopping:
                        return
                    try:
                        stats = await self.remote(account, "GET", "/system_stats", timeout=5)
                        self.machine(account, phase="ready", message="GPU 已就绪", stats=stats, last_activity=time.time())
                        self.log(account, "ComfyUI 已连接，可提交工作流")
                        self.ensure_worker(account)
                        return
                    except (aiohttp.ClientError, TimeoutError, RuntimeError):
                        pass
                    if time.monotonic() - last_check > 12:
                        output = await self.colab.execute(account, session, check)
                        for line in output.splitlines():
                            if line.startswith("WB_STATUS:"):
                                status = json.loads(line.removeprefix("WB_STATUS:"))
                                if status.get("phase") == "error":
                                    raise RuntimeError(status.get("message", "部署失败") + "\n" + output[-2500:])
                                self.machine(account, message=status.get("message", "等待部署"), deployment=status)
                        self.log(account, output)
                        last_check = time.monotonic()
                    await asyncio.sleep(3)
                raise TimeoutError("部署超过 60 分钟，请查看日志或停止机器")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.machine(account, phase="error", message=clean(str(exc)), last_activity=time.time())
                self.log(account, f"启动失败：{exc}")
                # Preserve the owned session so the user can stop it even after partial failure.
                for job in self.active_jobs(account):
                    if not job.get("prompt_id"):
                        self.job(job["id"], status="failed", message="启动失败：" + clean(str(exc)))

    async def remote(self, account, method, path, payload=None, timeout=30):
        machine = self.machine(account)
        if not machine.get("port"):
            raise RuntimeError("请先连接 GPU")
        async with self.http.request(method, f"http://127.0.0.1:{machine['port']}{path}", json=payload,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status >= 400:
                raise RuntimeError(f"ComfyUI {response.status}: " + (await response.text())[:3500])
            return await response.json()

    def resume(self, key):
        job = self.job(key)
        if not job or job["status"] != "recovering":
            raise ValueError("只有等待恢复的任务可以继续查询")
        self.check_service_slot(job["account"])
        self.ensure_worker(job["account"])
        return job

    def submit(self, account, graph, name, stop_after):
        validate_graph(graph)
        self.check_service_slot(account)
        if account in self.stopping:
            raise ValueError("账号的机器正在停止")
        if len(self.active_jobs(account)) >= 20:
            raise ValueError("每个账号最多排队 20 个任务")
        if any(j.get("stop_after") for j in self.active_jobs(account)):
            raise ValueError("队列里已有完成后关机的任务；请等它结束后再提交")
        key = uuid.uuid4().hex
        graph = json.loads(json.dumps(graph))
        for node in graph.values():
            if node["class_type"] in {"SaveVideo", "SaveImage", "SaveAudio"}:
                node["inputs"]["filename_prefix"] = f"workbench/{key}"
        self.store.put("jobs", key, {
            "id": key, "account": account, "name": name, "graph": graph, "status": "pending",
            "created_at": time.time(), "updated_at": time.time(), "stop_after": stop_after,
            "message": "等待 GPU", "outputs": [], "prompt_id": None, "progress": None,
        })
        self.ensure_worker(account)
        return self.job(key)

    def ensure_worker(self, account):
        if account not in self.workers or self.workers[account].done():
            self.workers[account] = asyncio.create_task(self.worker(account))

    async def worker(self, account):
        while not self.closing and account not in self.stopping:
            jobs = self.active_jobs(account)
            if not jobs:
                return
            job = jobs[0]
            self.inflight_jobs.add(job["id"])
            try:
                if self.machine(account)["phase"] != "ready":
                    self.job(job["id"], status="preparing", message="准备云端计算环境")
                    self.request_start(account)
                    # Cancelling a worker must not cancel an allocation request: its result
                    # is needed to identify and release the newly assigned cloud machine.
                    await asyncio.shield(self.start_tasks[account])
                if self.machine(account)["phase"] != "ready":
                    return
                if self.job(job["id"])["status"] not in ACTIVE:
                    continue
                await self.run_job(self.job(job["id"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Submission/network uncertainty must remain recoverable; no automatic resubmission.
                current = self.job(job["id"])
                recoverable = bool(current.get("prompt_id")) and isinstance(exc, (aiohttp.ClientError, TimeoutError, OSError))
                self.job(job["id"], status="recovering" if recoverable else "failed", message=clean(str(exc)), ended_at=time.time())
                self.log(account, f"任务 {job['id'][:8]}：{exc}")
                if recoverable:
                    self.machine(account, phase="disconnected", message="连接中断；重新连接后查询原任务")
                    return
            finally:
                self.inflight_jobs.discard(job["id"])
                self.machine(account, last_activity=time.time())
            current = self.job(job["id"])
            if current.get("stop_after") and current["status"] == "completed" and not self.active_jobs(account):
                self.request_stop(account)
                return

    async def run_job(self, job):
        account, key = job["account"], job["id"]
        port = self.machine(account)["port"]
        client_id = str(uuid.uuid4())
        prompt_id = job.get("prompt_id")
        ws = None
        try:
            ws = await self.http.ws_connect(f"http://127.0.0.1:{port}/ws?clientId={client_id}", heartbeat=20)
            async with self.job_locks.setdefault(key, asyncio.Lock()):
                if self.job(key)["status"] not in ACTIVE:
                    return
                if not prompt_id:
                    # Persist UUID before POST. Comfy's pinned version accepts caller-generated UUIDs.
                    prompt_id = str(uuid.uuid4())
                    self.job(key, prompt_id=prompt_id, status="submitting", started_at=time.time(), message="提交工作流")
                    response = await self.remote(account, "POST", "/prompt", {"prompt": job["graph"], "client_id": client_id, "prompt_id": prompt_id})
                    if response.get("prompt_id") != prompt_id:
                        raise RuntimeError("ComfyUI 未返回预期任务 ID")
                self.job(key, status="queued", message="云端排队 / 等待进度")
            deadline, last_history = time.monotonic() + 7200, 0
            missing_since = None
            while time.monotonic() < deadline:
                if self.job(key)["status"] == "cancelled":
                    return
                if time.monotonic() - last_history > 3:
                    history = await self.remote(account, "GET", f"/history/{prompt_id}")
                    last_history = time.monotonic()
                    if self.job(key)["status"] == "cancelled":
                        return
                    if prompt_id in history:
                        record = history[prompt_id]
                        status = record.get("status", {})
                        if status.get("status_str") == "error":
                            messages = status.get("messages", [])
                            raise RuntimeError("ComfyUI 执行失败：" + json.dumps(messages, ensure_ascii=False)[-3500:])
                        if status.get("completed"):
                            self.job(key, status="downloading", message="取回成片到本地", history=record)
                            outputs = await self.download_outputs(account, key, record.get("outputs", {}))
                            if not outputs:
                                raise RuntimeError("工作流已结束，但没有可保存的输出；请检查输出节点")
                            self.job(key, status="completed", message="成片已保存到本地", outputs=outputs, ended_at=time.time(), progress=100)
                            return
                    queue = await self.remote(account, "GET", "/queue")
                    present = any(item[1] == prompt_id for group in ("queue_running", "queue_pending") for item in queue.get(group, []))
                    if not present and prompt_id not in history:
                        missing_since = missing_since or time.monotonic()
                        if time.monotonic() - missing_since > 30:
                            raise RuntimeError("云端找不到原任务，可能未成功提交或 ComfyUI 已重启。为避免重复扣费，请检查后手动重新生成。")
                    else:
                        missing_since = None
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=2)
                except TimeoutError:
                    continue
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    # History polling still recovers completion when the original websocket is lost.
                    await asyncio.sleep(2)
                    continue
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                # A progress message already in flight must not reactivate a cancelled job.
                if self.job(key)["status"] == "cancelled":
                    return
                event = json.loads(message.data)
                data, kind = event.get("data", {}), event.get("type")
                if data.get("prompt_id") not in (None, prompt_id):
                    continue
                if kind == "progress":
                    value, total = data.get("value", 0), data.get("max", 1)
                    self.job(key, status="running", progress=round(100 * value / max(1, total)), message=f"采样 {value} / {total}")
                elif kind == "executing" and data.get("node"):
                    self.job(key, status="running", node=data["node"], message=f"执行节点 {data['node']}")
                elif kind == "execution_error":
                    raise RuntimeError(data.get("exception_message", "ComfyUI 执行错误"))
                elif kind == "execution_interrupted":
                    self.job(key, status="cancelled", message="生成已取消", ended_at=time.time())
                    return
            raise TimeoutError("任务超过两小时；请连接后检查云端进度")
        finally:
            if ws:
                await ws.close()

    async def download_outputs(self, account, key, outputs):
        folder = self.store.directory / "outputs" / key
        folder.mkdir(parents=True, exist_ok=True)
        graph = self.job(key)["graph"]
        (folder / "workflow-api.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2))
        result, seen = [], set()
        for node in outputs.values():
            for kind in ("images", "videos", "audio", "gifs"):
                for item in node.get(kind, []):
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    signature = (item["filename"], item.get("subfolder", ""), item.get("type", "output"))
                    if signature in seen:
                        continue
                    seen.add(signature)
                    name = f"{len(result):02d}-{Path(item['filename']).name}"
                    destination = folder / name
                    port = self.machine(account)["port"]
                    async with self.http.get(f"http://127.0.0.1:{port}/view", params=dict(zip(("filename", "subfolder", "type"), signature)), timeout=aiohttp.ClientTimeout(total=600)) as response:
                        response.raise_for_status()
                        temp = destination.with_suffix(destination.suffix + ".part")
                        with temp.open("wb") as stream:
                            async for chunk in response.content.iter_chunked(1024 * 1024):
                                stream.write(chunk)
                        temp.replace(destination)
                    result.append({"filename": name, "bytes": destination.stat().st_size, "url": f"/api/jobs/{key}/files/{name}"})
        return result

    async def cancel(self, key):
        async with self.job_locks.setdefault(key, asyncio.Lock()):
            job = self.job(key)
            if not job:
                raise ValueError("任务不存在")
            if job["status"] not in ACTIVE:
                return job
            if job["status"] == "downloading":
                raise ValueError("云端已生成完成，正在取回成片，请等待保存结束")
            if job.get("prompt_id"):
                result = await self.remote(job["account"], "POST", f"/api/jobs/{job['prompt_id']}/cancel")
                if not result.get("cancelled"):
                    history = await self.remote(job["account"], "GET", f"/history/{job['prompt_id']}")
                    if history.get(job['prompt_id'], {}).get("status", {}).get("completed"):
                        raise ValueError("云端已生成完成，正在取回成片")
            return self.job(key, status="cancelled", message="生成已取消", ended_at=time.time())

    def request_stop(self, account, force=False):
        self.machine(account)
        if self.active_jobs(account) and not force:
            raise ValueError("还有未完成任务，请先取消任务再关闭机器")
        if account not in self.stop_tasks or self.stop_tasks[account].done():
            self.stopping.add(account)
            self.machine(account, message="等待释放云端机器")
            self.stop_tasks[account] = asyncio.create_task(self.stop(account, force))
        return self.machine(account)

    async def stop(self, account, force=False):
        async with self.locks[account]:
            machine = self.machine(account)
            session = machine.get("session")
            try:
                if not session:
                    self.machine(account, phase="stopped", message="机器已关闭", port=None)
                    return
                install = self.store.get("meta", "install")["id"]
                if not session.startswith(f"wb-{install}-{account.removeprefix('colab')}-"):
                    raise RuntimeError("此会话不属于当前工作台，拒绝停止")
                if not force and machine.get("port") and machine["phase"] == "ready":
                    queue = await self.remote(account, "GET", "/queue", timeout=5)
                    if queue.get("queue_running") or queue.get("queue_pending"):
                        self.machine(account, phase="ready", message="ComfyUI 中仍有任务，请在编辑器中取消或等它完成后再关闭")
                        self.log(account, "拒绝关机：完整编辑器中仍有任务")
                        return
                self.machine(account, phase="stopping", message="释放 Colab 会话并核对状态")
                await self.colab.stop(account, session)
                await self.colab.close_tunnel(account)
                for job in self.active_jobs(account):
                    self.job(job["id"], status="cancelled", message="机器已关闭", ended_at=time.time())
                self.machine(account, phase="stopped", session=None, port=None, message="机器已释放，停止计费")
                self.log(account, f"已释放 {session}")
                with contextlib.suppress(Exception):
                    await self.refresh(account)
            except Exception as exc:
                self.machine(account, phase="error", message="关机未确认：" + clean(str(exc)))
                self.log(account, f"关机失败：{exc}")
            finally:
                self.stopping.discard(account)

    async def idle_sweep(self):
        minutes = self.store.get("settings", "main")["idle_minutes"]
        for machine in self.store.all("machines"):
            account = machine["account"]
            if machine["phase"] not in {"ready", "error"} or not machine.get("session") or self.active_jobs(account):
                continue
            if machine["phase"] == "ready":
                try:
                    queue = await self.remote(account, "GET", "/queue", timeout=5)
                    if queue.get("queue_running") or queue.get("queue_pending"):
                        self.machine(account, last_activity=time.time())
                        continue
                except Exception:
                    self.machine(account, phase="disconnected", message="云端连接中断，请重新连接或关闭机器")
                    continue
            if minutes and time.time() - machine.get("last_activity", machine.get("created_at", time.time())) > minutes * 60:
                self.log(account, f"空闲 {minutes} 分钟，自动关闭")
                self.request_stop(account)

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
        for task in self.workers.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
        # Both exit policies wait for allocation to return. This prevents a CLI
        # cancellation between cloud assignment and persisting its session ID.
        for account, task in self.start_tasks.items():
            if not task.done():
                self.stopping.add(account)
        # Let allocation finish so its assigned session is known and can be stopped.
        if self.store.get("settings", "main")["stop_on_exit"]:
            for machine in self.store.all("machines"):
                if machine.get("session"):
                    self.request_stop(machine["account"], force=True)
            await asyncio.gather(*self.stop_tasks.values(), return_exceptions=True)
        else:
            await asyncio.gather(*self.start_tasks.values(), return_exceptions=True)
        for task in [*self.workers.values(), *self.start_tasks.values()]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.workers.values(), *self.start_tasks.values(), return_exceptions=True)
        await self.colab.close()
        if self.http:
            await self.http.close()

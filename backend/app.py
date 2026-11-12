import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .colab import Colab, clean
from .config import COMFY_COMMIT, DATA, MODEL_FILES, MODEL_REVISION, ROOT, profiles
from .library import Library
from .llm import BUSY, LLM
from .llm_presets import PRESETS
from .manager import Manager
from .store import Store
from .workflows import validate_graph


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="H3 · 文生视频", min_length=1, max_length=100)
    graph: dict
    positions: dict = Field(default_factory=dict)


class Submission(Draft):
    account: str
    stop_after: bool = False


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: str = Field(max_length=4096)
    idle_minutes: int = Field(default=15, ge=0, le=240)
    stop_on_exit: bool = True


class ArtworkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["trash", "restore", "delete"]


class ArtworkName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset_id: str
    context_tokens: Literal[32768, 131072, 262144] = 262144
    gpu_memory: Literal[0.8, 0.9, 0.95] = 0.9
    cpu_threads: Literal[8, 16, 24, 48] = 24


class LLMDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str
    config: LLMConfig


def create_app(directory=DATA, driver=None, account_profiles=None, llm_driver=None):
    configured_profiles = account_profiles
    account_profiles = []
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        os.umask(0o077)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = FileLock(directory / "server.lock", timeout=0)
        with lock:
            store = Store(directory)
            # Keep live sessions visible even if their launcher was removed.
            account_profiles[:] = configured_profiles if configured_profiles is not None else profiles(
                machine["account"] for machine in store.all("machines") + store.all("llm") + store.all("llm_instances") if machine.get("session")
            )
            colab = driver or Colab(account_profiles, directory)
            manager = Manager(store, colab, account_profiles)
            llm = LLM(store, llm_driver or driver or Colab(account_profiles, directory), account_profiles)
            app.state.store, app.state.manager = store, manager
            app.state.llm = llm
            await manager.open()
            await llm.open()
            try:
                yield
            finally:
                await llm.close()
                await manager.close()
                store.close()

    app = FastAPI(title="Colab Workbench", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    class LocalOnly:
        # ASGI middleware keeps disconnects attached to the streaming request.
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            request = Request(scope)
            origin = request.headers.get("origin")
            error = None
            is_llm = request.url.path.startswith("/llm/")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                error = JSONResponse({"detail": "只允许本地工作台的同源请求"}, status_code=403)
            elif is_llm:
                scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
                if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.encode(), app.state.llm.key().encode()):
                    error = JSONResponse({"error": {"message": "服务密钥无效", "type": "authentication_error"}}, status_code=401)
            elif request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("x-workbench-token") != token:
                error = JSONResponse({"detail": "工作台会话已变化，请刷新页面"}, status_code=403)

            async def guarded_send(message):
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers["X-Content-Type-Options"] = "nosniff"
                    headers["Referrer-Policy"] = "no-referrer"
                    if request.url.path.startswith("/api/") or is_llm:
                        headers["Cache-Control"] = "no-store"
                await send(message)

            try:
                if error is not None:
                    await error(scope, receive, guarded_send)
                else:
                    await self.app(scope, receive, guarded_send)
            finally:
                finish = scope.get("state", {}).get("llm_finish")
                if finish:
                    finish()

    app.add_middleware(LocalOnly)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def manager():
        return app.state.manager

    def store():
        return app.state.store

    @app.get("/api/state")
    async def state():
        accounts = []
        for profile in account_profiles:
            accounts.append({**profile, "machine": manager().machine(profile["id"]),
                             **store().get("accounts", profile["id"], {})})
        jobs = sorted(store().all("jobs"), key=lambda j: j["created_at"], reverse=True)
        jobs = [{k: v for k, v in j.items() if k not in {"graph", "history"}} for j in jobs]
        return {"token": token, "accounts": accounts, "jobs": jobs,
                "llm": {"presets": PRESETS, "services": [app.state.llm.public(item["id"]) for item in app.state.llm.all()]},
                "settings": store().get("settings", "main"),
                "drafts": [{k: v for k, v in d.items() if k not in {"graph", "positions"}} for d in store().all("drafts")],
                "preset": {"comfy_commit": COMFY_COMMIT, "model_revision": MODEL_REVISION, "models": MODEL_FILES}}

    @app.get("/api/template")
    async def template():
        return {"name": "H3 · 雪林中的狐狸", "graph": json.loads((ROOT / "templates/h3-int8.json").read_text()), "positions": {}}

    @app.post("/api/accounts/{account}/refresh")
    async def refresh(account):
        manager().machine(account)
        try:
            return await manager().refresh(account)
        except Exception as exc:
            old = store().get("accounts", account, {})
            store().put("accounts", account, {**old, "error": clean(str(exc)), "checked_at": time.time()})
            raise HTTPException(502, clean(str(exc))) from exc

    @app.post("/api/accounts/{account}/start", status_code=202)
    async def start(account):
        manager().machine(account)
        return manager().request_start(account)

    @app.post("/api/accounts/{account}/stop", status_code=202)
    async def stop(account):
        return manager().request_stop(account)

    @app.get("/api/accounts/{account}/logs")
    async def logs(account):
        manager().machine(account)
        return store().get("logs", account, [])

    @app.get("/api/accounts/{account}/nodes")
    async def nodes(account):
        return await manager().remote(account, "GET", "/object_info")

    @app.get("/api/accounts/{account}/metrics")
    async def metrics(account):
        return await manager().telemetry.read(account)

    @app.post("/api/llm/instances", status_code=201)
    async def llm_create(body: LLMDeployment):
        return app.state.llm.create(body.account, body.config.model_dump())

    @app.delete("/api/llm/{key}")
    async def llm_delete(key):
        return app.state.llm.delete(key)

    @app.put("/api/llm/{key}/config")
    async def llm_config(key, body: LLMDeployment):
        return app.state.llm.configure(key, body.account, body.config.model_dump())

    @app.post("/api/llm/{key}/start", status_code=202)
    async def llm_start(key):
        return app.state.llm.request_start(key)

    @app.post("/api/llm/{key}/stop", status_code=202)
    async def llm_stop(key):
        return app.state.llm.request_stop(key)

    @app.get("/api/llm/{key}/logs")
    async def llm_logs(key):
        app.state.llm.machine(key)
        return store().get("llm_logs", key, [])

    @app.get("/api/llm/{key}/metrics")
    async def llm_metrics(key):
        return await app.state.llm.telemetry.read(key)

    @app.post("/api/llm/key")
    async def llm_key():
        return {"key": app.state.llm.key()}

    @app.api_route("/llm/{key}/v1/{route:path}", methods=["GET", "POST"])
    async def llm_proxy(key: str, route: str, request: Request):
        llm = app.state.llm
        if (request.method, route) not in {("GET", "models"), ("POST", "chat/completions"), ("POST", "completions")}:
            raise HTTPException(404, "不支持的服务接口")
        try:
            key = llm.resolve(key)
            item = llm.machine(key)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if item["phase"] != "ready" or not item.get("port"):
            return JSONResponse({"error": {"message": "服务未就绪，请在工作台启动或重新连接", "type": "service_unavailable"}}, status_code=503)
        payload = None
        if request.method == "POST":
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8 * 1024**2:
                    raise HTTPException(413, "请求不能超过 8 MiB")
                chunks.append(chunk)
            try:
                payload = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError) as exc:
                raise HTTPException(400, "请求必须是 JSON") from exc
            if not isinstance(payload, dict) or payload.get("model") != llm.model_name(key):
                raise HTTPException(400, "model 应为 " + llm.model_name(key))
        acquired = False
        upstream = None

        def finish():
            nonlocal acquired
            if upstream is not None:
                upstream.close()
            if acquired:
                acquired = False
                llm.release(key)

        request.state.llm_finish = finish
        try:
            if request.method == "POST":
                item = llm.acquire(key)
                acquired = True
            upstream = await llm.http.request(request.method, f"http://127.0.0.1:{item['port']}/v1/{route}",
                                              json=payload, timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=600),
                                              allow_redirects=False)
        except ValueError as exc:
            finish()
            raise HTTPException(409, str(exc)) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            finish()
            raise HTTPException(502, "服务连接失败，请重新连接") from exc
        except BaseException:
            finish()
            raise

        async def stream():
            try:
                async for chunk in upstream.content.iter_any():
                    yield chunk
            finally:
                finish()

        return StreamingResponse(stream(), status_code=upstream.status,
                                 headers={"Content-Type": upstream.headers.get("Content-Type", "application/json"),
                                          "Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                                 background=BackgroundTask(finish))

    @app.post("/api/jobs", status_code=202)
    async def submit(body: Submission):
        return manager().submit(body.account, body.graph, body.name, body.stop_after)

    @app.post("/api/artworks/manage")
    async def manage_artworks(body: ArtworkAction):
        return Library(manager()).manage(body.ids, body.action)

    @app.patch("/api/jobs/{key}")
    async def rename_artwork(key, body: ArtworkName):
        return Library(manager()).rename(key, body.name)

    @app.get("/api/jobs/{key}")
    async def job(key):
        result = manager().job(key)
        if not result:
            raise HTTPException(404, "任务不存在")
        return result

    @app.post("/api/jobs/{key}/cancel")
    async def cancel(key):
        return await manager().cancel(key)

    @app.post("/api/jobs/{key}/resume", status_code=202)
    async def resume(key):
        return manager().resume(key)

    @app.get("/api/jobs/{key}/files/{filename}")
    async def file(key, filename):
        job = manager().job(key)
        if not job:
            raise HTTPException(404, "任务不存在")
        names = {o["filename"] for o in job.get("outputs", [])} | {"workflow-api.json"}
        if filename not in names or Path(filename).name != filename or Path(key).name != key:
            raise HTTPException(404, "文件不存在")
        path = directory / "outputs" / key / filename
        if not path.is_file():
            raise HTTPException(404, "文件尚未取回本地")
        return FileResponse(path)

    @app.put("/api/drafts/{key}")
    async def save_draft(key, body: Draft):
        if len(key) > 100:
            raise ValueError("草稿 ID 过长")
        validate_graph(body.graph)
        draft = {"id": key, **body.model_dump(), "updated_at": time.time()}
        return store().put("drafts", key, draft)

    @app.get("/api/drafts/{key}")
    async def get_draft(key):
        draft = store().get("drafts", key)
        if not draft:
            raise HTTPException(404, "草稿不存在")
        return draft

    @app.put("/api/settings")
    async def settings(body: Settings):
        if any(m["phase"] in BUSY - {"stopping"} for m in store().all("machines") + store().all("llm")):
            raise ValueError("请等待部署结束后再修改设置")
        return store().put("settings", "main", body.model_dump())

    @app.get("/reference/{name}")
    async def reference(name):
        path = ROOT / "templates" / name
        if name not in {"reference.jpg", "reference.mp4"} or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    dist = ROOT / "frontend/dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()

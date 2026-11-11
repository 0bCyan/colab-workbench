import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .colab import Colab, clean
from .config import COMFY_COMMIT, DATA, MODEL_FILES, MODEL_REVISION, ROOT, profiles
from .library import Library
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


def create_app(directory=DATA, driver=None, account_profiles=None):
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
                machine["account"] for machine in store.all("machines") if machine.get("session")
            )
            colab = driver or Colab(account_profiles, directory)
            manager = Manager(store, colab, account_profiles)
            app.state.store, app.state.manager = store, manager
            await manager.open()
            try:
                yield
            finally:
                await manager.close()
                store.close()

    app = FastAPI(title="Colab Workbench", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def local_only(request, call_next):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "只允许本地工作台的同源请求"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("x-workbench-token") != token:
            return JSONResponse({"detail": "工作台会话已变化，请刷新页面"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

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
        if any(m["phase"] in {"checking", "provisioning", "deploying"} for m in store().all("machines")):
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

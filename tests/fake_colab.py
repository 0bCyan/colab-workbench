"""In-process Colab + HTTP/WebSocket Comfy simulator. Never calls a cloud API."""
import asyncio
import json
import time

from aiohttp import web

from backend.config import ROOT

PROFILES = [{"id": f"colab{i}", "label": f"账号 {i}", "launcher": "/test/fake", "installed": True, "credential_present": True} for i in (1, 2, 3)]


class FakeColab:
    def __init__(self, delay=0.2):
        self.delay = delay
        self.calls = []
        self.active = {"colab1": [{"name": "unrelated-important-session", "endpoint": "unrelated", "hardware": "G4"}], "colab2": [], "colab3": []}
        self.servers, self.ports, self.ws, self.histories, self.pending = {}, {}, {}, {}, {}
        self.tasks = []
        self.fail_stop = False
        self.create_gate = None
        self.prompt_posts = []
        self.downloaded = []
        self.native_busy = False
        self.telemetry_ports = {}
        self.metric_offset = 0

    async def sessions(self, account):
        self.calls.append((account, "sessions"))
        return list(self.active[account])

    async def usage(self, account):
        return {"balance": 1000.0, "rate": len(self.active[account]) * 20, "active": len(self.active[account]), "raw": "test"}

    async def create(self, account, session):
        self.calls.append((account, "create", session))
        if self.create_gate:
            await self.create_gate.wait()
        self.active[account].append({"name": session, "hardware": "G4", "endpoint": session})
        return "ready"

    async def stop(self, account, session):
        self.calls.append((account, "stop", session))
        if self.fail_stop:
            raise RuntimeError("test: unassign failed")
        self.active[account] = [s for s in self.active[account] if s["name"] != session]

    async def execute(self, account, session, script):
        self.calls.append((account, "exec", session))
        return 'WB_STATUS:{"phase":"ready","message":"test ready"}'

    async def tunnel(self, account, session, identity):
        if account in self.ports:
            return self.ports[account]
        self.ws[account] = {}
        self.histories[account] = {}
        self.pending[account] = []
        routes = web.RouteTableDef()

        @routes.get('/metrics')
        async def metrics(request):
            return web.json_response({"sample": {
                "sampled_at": time.time() + self.metric_offset,
                "cpu": {"percent": 24, "cores": 24},
                "memory": {"used": 40 * 2**30, "total": 160 * 2**30, "percent": 25},
                "disk": {"used": 50 * 2**30, "total": 200 * 2**30, "percent": 25},
                "gpu": {"name": "测试 GPU（模拟）", "percent": 73, "memory_used": 38 * 2**30,
                        "memory_total": 96 * 2**30, "memory_percent": 39.6, "temperature": 58,
                        "power": 210, "power_limit": 600},
                "network": {"rx_bytes_s": 2048, "tx_bytes_s": 1024},
            }})

        @routes.get('/system_stats')
        async def stats(request):
            return web.json_response({"devices": [{"name": "测试 GPU（模拟）", "vram_total": 96 * 2**30}]})

        @routes.get('/ws')
        async def websocket(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            self.ws[account][request.query['clientId']] = socket
            async for message in socket:
                pass
            return socket

        @routes.post('/prompt')
        async def prompt(request):
            body = await request.json()
            self.prompt_posts.append((account, body))
            prompt_id = body['prompt_id']
            self.pending[account].append(prompt_id)
            async def finish():
                for step in range(1, 5):
                    await asyncio.sleep(self.delay / 4)
                    socket = self.ws[account].get(body['client_id'])
                    if socket is not None and not socket.closed:
                        await socket.send_json({"type": "progress", "data": {"prompt_id": prompt_id, "value": step, "max": 4}})
                if prompt_id not in self.pending[account]:
                    return
                self.histories[account][prompt_id] = {
                    "outputs": {"16": {"images": [{"filename": "test.mp4", "subfolder": "workbench", "type": "output"}]}},
                    "status": {"completed": True, "status_str": "success"},
                }
                self.pending[account].remove(prompt_id)
                socket = self.ws[account].get(body['client_id'])
                if socket is not None and not socket.closed:
                    await socket.send_json({"type": "execution_success", "data": {"prompt_id": prompt_id}})
            self.tasks.append(asyncio.create_task(finish()))
            return web.json_response({"prompt_id": prompt_id})

        @routes.get('/history/{key}')
        async def history(request):
            key = request.match_info['key']
            return web.json_response({key: self.histories[account][key]} if key in self.histories[account] else {})

        @routes.get('/queue')
        async def queue(request):
            native = [[0, "native-task"]] if self.native_busy else []
            return web.json_response({"queue_running": [[0, key] for key in self.pending[account]] + native, "queue_pending": []})

        @routes.get('/view')
        async def view(request):
            self.downloaded.append((account, time.time()))
            return web.FileResponse(ROOT / "tests/fixtures/synthetic.mp4")

        @routes.post('/api/jobs/{key}/cancel')
        async def cancel(request):
            key = request.match_info['key']
            self.calls.append((account, "cancel", key))
            if key in self.pending[account]:
                self.pending[account].remove(key)
            return web.json_response({"cancelled": True})

        app = web.Application()
        app.add_routes(routes)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.servers[account] = runner
        self.ports[account] = site._server.sockets[0].getsockname()[1]
        self.telemetry_ports[account] = self.ports[account]
        return self.ports[account]

    async def close_tunnel(self, account):
        pass

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for sockets in self.ws.values():
            for socket in sockets.values():
                await socket.close()
        for runner in self.servers.values():
            await runner.cleanup()


def template():
    return json.loads((ROOT / "templates/h3-int8.json").read_text())

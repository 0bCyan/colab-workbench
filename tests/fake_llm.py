"""Local inference simulator with real HTTP streaming; never contacts Colab."""
import asyncio
import json
import time

from aiohttp import web

from tests.fake_colab import FakeColab


class FakeLLM(FakeColab):
    def __init__(self, active=None):
        super().__init__()
        if active is not None:
            self.active = active
        self.ready = set()
        self.requests = []
        self.generation_gate = None
        self.generation_started = asyncio.Event()
        self.fail_deploy = False

    async def execute(self, account, session, script):
        self.calls.append((account, "exec", session))
        if self.fail_deploy:
            return 'LLM_STATUS:{"phase":"error","message":"test: insufficient disk"}'
        self.ready.add(account)
        return 'LLM_STATUS:{"phase":"ready","message":"test ready"}'

    async def stop(self, account, session):
        await super().stop(account, session)
        self.ready.discard(account)

    async def tunnel(self, account, session, identity, service_port=8000):
        self.calls.append((account, "tunnel", session, service_port))
        if account in self.ports:
            return self.ports[account]
        routes = web.RouteTableDef()

        @routes.get('/v1/models')
        async def models(request):
            if account not in self.ready:
                raise web.HTTPServiceUnavailable()
            return web.json_response({"object": "list", "data": [{"id": "workbench-llm"}]})

        @routes.post('/v1/chat/completions')
        @routes.post('/v1/completions')
        async def generate(request):
            body = await request.json()
            self.requests.append((account, body, dict(request.headers)))
            self.generation_started.set()
            if body.get('max_tokens') == -1:
                return web.json_response({"error": {"message": "invalid max_tokens"}}, status=400)
            if not body.get('stream'):
                if self.generation_gate:
                    await self.generation_gate.wait()
                return web.json_response({"choices": [{"message": {"role": "assistant", "content": account}}]})
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            try:
                for content in ["你好，", f"来自 {account} 的测试服务。"]:
                    data = json.dumps({"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)
                    await response.write(f'data: {data}\n\n'.encode())
                    if self.generation_gate:
                        await self.generation_gate.wait()
                    await asyncio.sleep(.05)
                await response.write(b'data: [DONE]\n\n')
                await response.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return response

        @routes.get('/metrics')
        async def metrics(request):
            return web.json_response({"sample": {
                "sampled_at": time.time(), "cpu": {"percent": 10, "cores": 24},
                "memory": {"used": 32 * 2**30, "total": 160 * 2**30, "percent": 20},
                "disk": {"used": 60 * 2**30, "total": 200 * 2**30, "percent": 30},
                "gpu": {"name": "测试 GPU（模拟）", "percent": 85, "memory_used": 70 * 2**30,
                        "memory_total": 96 * 2**30, "memory_percent": 73, "temperature": 55,
                        "power": 230, "power_limit": 600},
                "network": {"rx_bytes_s": 1024, "tx_bytes_s": 2048},
            }})

        app = web.Application()
        app.add_routes(routes)
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.servers[account] = runner
        self.ports[account] = site._server.sockets[0].getsockname()[1]
        self.telemetry_ports[account] = self.ports[account]
        return self.ports[account]

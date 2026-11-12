import asyncio
import os
import re
import shlex
import signal
import socket
from pathlib import Path

from .config import ROOT


def clean(text):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"(?i)([\w-]*(?:token|secret|authorization)[\w-]*[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+", r"\1[redacted]", text)
    return text[-14000:]


class Colab:
    def __init__(self, profiles, directory):
        self.profiles = {p["id"]: p for p in profiles}
        self.directory = directory
        self.tunnels = {}
        self.telemetry_ports = {}

    def launcher(self, account):
        if account not in self.profiles:
            raise ValueError("未知账号")
        return self.profiles[account]["launcher"]

    async def command(self, account, *args, timeout=60):
        process = await asyncio.create_subprocess_exec(
            self.launcher(account), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
            raise
        output = clean(stdout.decode(errors="replace"))
        if process.returncode:
            raise RuntimeError(output or f"Colab CLI exited {process.returncode}")
        return output

    async def sessions(self, account):
        output = await self.command(account, "sessions")
        if "No active sessions found on server" in output:
            return []
        rows = []
        for line in output.splitlines():
            match = re.match(r"^\[([^]]+)\] (\S+) \| Hardware: ([^|]+) \| Shape: ([^|]+) \| Variant: (.+)$", line)
            if match:
                rows.append(dict(zip(("name", "endpoint", "hardware", "shape", "variant"), (p.strip() for p in match.groups()))))
        if not rows:
            raise RuntimeError("无法解析 Colab 会话响应，未修改云端状态。请检查 CLI 版本。\n" + output)
        return rows

    async def usage(self, account):
        text = await self.command(account, "usage")
        def number(pattern):
            match = re.search(pattern, text)
            return float(match[1].replace(",", "")) if match else None
        return {"balance": number(r"Current balance:\s*([\d,.]+)"),
                "rate": number(r"Usage rate:\s*([\d,.]+)"),
                "active": number(r"Active assignments:\s*(\d+)"), "raw": text}

    async def create(self, account, session):
        return await self.command(account, "new", "-s", session, "--gpu", "G4", timeout=300)

    async def stop(self, account, session):
        await self.command(account, "stop", "-s", session, timeout=120)
        sessions = await self.sessions(account)
        if any(s["name"] == session for s in sessions):
            raise RuntimeError("Colab 仍报告会话存活，未确认释放，请重试停止")

    async def execute(self, account, session, script):
        return await self.command(account, "exec", "-s", session, "-f", str(script), "--timeout", "90", timeout=120)

    async def tunnel(self, account, session, identity, service_port=8188):
        await self.close_tunnel(account)
        launcher = Path(self.launcher(account))
        first = launcher.read_text().splitlines()[0]
        python = first.removeprefix("#!").strip()
        if not first.startswith("#!/") or not Path(python).is_file():
            raise RuntimeError("需要现有的托管 Python 版 Colab 多账号入口")
        if not identity or not Path(identity).is_file():
            raise RuntimeError("请在设置中填写有效的 SSH 私钥路径")
        with socket.socket() as sock, socket.socket() as monitor:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            monitor.bind(("127.0.0.1", 0))
            telemetry_port = monitor.getsockname()[1]
        proxy = shlex.join([python, str(ROOT / "backend/bridge.py"), str(launcher), session, identity])
        log_path = self.directory / f"{account}-ssh.log"
        with log_path.open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-N", "-T", "-i", identity,
                "-o", f"ProxyCommand={proxy}", "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                "-o", "ConnectTimeout=30", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-L", f"127.0.0.1:{port}:127.0.0.1:{int(service_port)}",
                "-L", f"127.0.0.1:{telemetry_port}:127.0.0.1:8189", "root@colab-runtime",
                stdout=log, stderr=log, start_new_session=True,
            )
        self.tunnels[account] = proc
        self.telemetry_ports[account] = telemetry_port
        await asyncio.sleep(1)
        if proc.returncode is not None:
            raise RuntimeError(clean(log_path.read_text(errors="replace")))
        return port

    async def close_tunnel(self, account):
        self.telemetry_ports.pop(account, None)
        proc = self.tunnels.pop(account, None)
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def close(self):
        for account in list(self.tunnels):
            await self.close_tunnel(account)

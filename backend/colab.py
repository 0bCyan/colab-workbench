import asyncio
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from .config import ROOT


def launcher_python(launcher):
    first = Path(launcher).read_text(encoding="utf-8").splitlines()[0]
    python = first.removeprefix("#!").strip()
    if not first.startswith("#!") or not Path(python).is_absolute() or not Path(python).is_file():
        raise RuntimeError("需要现有的托管 Python 版 Colab 多账号入口")
    return python


async def stop_process(process):
    if process.returncode is None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if await killer.wait() and process.returncode is None:
                raise RuntimeError("无法结束 Colab 子进程，请检查本机进程权限")
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    await process.wait()


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

    def validate_connection(self, identity):
        path = Path(identity).expanduser() if identity else None
        if not path or not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError('SSH 私钥未配置或文件不可读，请在服务器面板的连接设置中填写本机私钥路径')
        if not shutil.which('ssh'):
            raise ValueError('本机未安装 OpenSSH，请安装后再连接')
        return str(path)

    async def command(self, account, *args, timeout=60):
        launcher = self.launcher(account)
        command = [launcher_python(launcher), launcher] if os.name == "nt" else [launcher]
        process = await asyncio.create_subprocess_exec(
            *command, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb", "PYTHONUTF8": "1"},
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException:
            if process.returncode is None:
                await stop_process(process)
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

    async def tunnel(self, account, session, identity, service_port=8188, tunnel_key=None, reverse_port=None):
        identity = self.validate_connection(identity)
        key = tunnel_key or account
        await self.close_tunnel(key)
        launcher = Path(self.launcher(account))
        python = launcher_python(launcher)
        with socket.socket() as sock, socket.socket() as monitor:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            monitor.bind(("127.0.0.1", 0))
            telemetry_port = monitor.getsockname()[1]
        proxy_args = [python, str(ROOT / "backend/bridge.py"), str(launcher), session, identity]
        proxy = subprocess.list2cmdline(proxy_args) if os.name == "nt" else shlex.join(proxy_args)
        log_path = self.directory / f"{key}-ssh.log"
        with log_path.open("ab") as log:
            reverse = ['-R', f'127.0.0.1:8190:127.0.0.1:{int(reverse_port)}'] if reverse_port else []
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-N", "-T", "-i", identity,
                "-o", f"ProxyCommand={proxy}", "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                "-o", "ConnectTimeout=30", "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={os.devnull}", "-o", "LogLevel=ERROR",
                "-L", f"127.0.0.1:{port}:127.0.0.1:{int(service_port)}",
                "-L", f"127.0.0.1:{telemetry_port}:127.0.0.1:8189", *reverse, "root@colab-runtime",
                stdout=log, stderr=log, start_new_session=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                env={**os.environ, "PYTHONUTF8": "1"},
            )
        self.tunnels[key] = proc
        self.telemetry_ports[key] = telemetry_port
        try:
            await self.wait_tunnel(key, port)
        except Exception:
            message = clean(log_path.read_text(errors='replace'))
            await self.close_tunnel(key)
            raise RuntimeError(message or 'SSH 隧道未就绪，请重试连接') from None
        return port

    async def wait_tunnel(self, key, port):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.tunnels[key].returncode is not None:
                raise RuntimeError('SSH exited before forwarding was ready')
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), .5)
                writer.close()
                await writer.wait_closed()
                return
            except (OSError, TimeoutError):
                await asyncio.sleep(.2)
        raise TimeoutError('SSH forwarding readiness timeout')

    async def close_tunnel(self, account):
        self.telemetry_ports.pop(account, None)
        proc = self.tunnels.pop(account, None)
        if proc and proc.returncode is None:
            await stop_process(proc)

    async def close(self):
        for account in list(self.tunnels):
            await self.close_tunnel(account)

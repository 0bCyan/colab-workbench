"""Read-only Colab telemetry sidecar; loopback only, no ComfyUI/custom-node dependency."""
import csv
import json
import math
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def gpu_sample():
    fields = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
    result = subprocess.run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=2, check=True)
    values = next(csv.reader(result.stdout.splitlines()))
    name, utilization, used, total, temperature, power, limit = [v.strip() for v in values]
    used, total = number(used), number(total)
    return {"name": name, "percent": number(utilization),
            "memory_used": used * 2**20 if used is not None else None,
            "memory_total": total * 2**20 if total is not None else None,
            "memory_percent": 100 * used / total if used is not None and total else None,
            "temperature": number(temperature), "power": number(power), "power_limit": number(limit)}


class Sampler:
    def __init__(self, psutil, disk_path="/content"):
        self.psutil, self.disk_path = psutil, disk_path
        self.previous_net = None
        self.previous_time = None
        self.cpu_primed = False

    def sample(self):
        now = time.monotonic()
        p = self.psutil
        cpu = p.cpu_percent(interval=None)
        if not self.cpu_primed:
            cpu = None  # psutil's first nonblocking measurement has no baseline.
            self.cpu_primed = True
        ram, disk = p.virtual_memory(), p.disk_usage(self.disk_path)
        interfaces = p.net_io_counters(pernic=True)
        rx = sum(v.bytes_recv for name, v in interfaces.items() if name != "lo")
        tx = sum(v.bytes_sent for name, v in interfaces.items() if name != "lo")
        elapsed = now - self.previous_time if self.previous_time is not None else 0
        network = {"rx_bytes_s": None, "tx_bytes_s": None}
        if elapsed > 0 and self.previous_net is not None:
            network = {"rx_bytes_s": max(0, rx - self.previous_net[0]) / elapsed,
                       "tx_bytes_s": max(0, tx - self.previous_net[1]) / elapsed}
        self.previous_time, self.previous_net = now, (rx, tx)
        gpu, gpu_error = None, None
        try:
            gpu = gpu_sample()
        except (OSError, subprocess.SubprocessError, ValueError, StopIteration) as exc:
            gpu_error = type(exc).__name__
        return {"sampled_at": time.time(), "cpu": {"percent": cpu, "cores": p.cpu_count()},
                "memory": {"used": ram.total - ram.available, "total": ram.total, "percent": ram.percent},
                "disk": {"used": disk.used, "total": disk.total, "percent": disk.percent},
                "gpu": gpu, "gpu_error": gpu_error, "network": network}


def main():
    import fcntl

    import psutil

    lock = open(os.path.join(os.path.dirname(__file__), "telemetry.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # The existing sidecar owns the port.
    sampler = Sampler(psutil)
    cache = {"sample": None, "error": None}

    def collect():
        while True:
            start = time.monotonic()
            try:
                cache.update(sample=sampler.sample(), error=None)
            except Exception as exc:
                cache.update(sample=None, error=type(exc).__name__)
            time.sleep(max(0.1, 2 - (time.monotonic() - start)))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            data = json.dumps(cache, allow_nan=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    threading.Thread(target=collect, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8189), Handler).serve_forever()


if __name__ == "__main__":
    main()

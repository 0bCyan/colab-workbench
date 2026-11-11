import asyncio
import math
import time

import aiohttp


def validate_sample(sample):
    if not isinstance(sample, dict):
        raise ValueError("Sampler not ready")
    fields = {"cpu": ("percent", "cores"), "memory": ("used", "total", "percent"),
              "disk": ("used", "total", "percent"), "network": ("rx_bytes_s", "tx_bytes_s")}
    if sample.get("gpu") is not None:
        if not isinstance(sample["gpu"], dict) or not isinstance(sample["gpu"].get("name"), str):
            raise ValueError("Invalid GPU sample")
        fields["gpu"] = ("percent", "memory_used", "memory_total", "memory_percent", "temperature", "power", "power_limit")
    for section, keys in fields.items():
        if not isinstance(sample.get(section), dict):
            raise ValueError("Incomplete metrics")
        for key in keys:
            if key not in sample[section]:
                raise ValueError("Incomplete metrics")
            value = sample[section][key]
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("Invalid metric value")


class Telemetry:
    """Read-through, account/session-scoped cache. Reading metrics never allocates a VM."""
    def __init__(self, manager):
        self.manager = manager
        self.cache, self.locks = {}, {}

    async def read(self, account):
        async with self.locks.setdefault(account, asyncio.Lock()):
            machine = self.manager.machine(account)
            session, port = machine.get("session"), machine.get("telemetry_port")
            cached = self.cache.get(account)
            if cached and cached["session"] != session:
                cached = None
                self.cache.pop(account, None)
            blank = {"account": account, "session": session, "status": "offline", "sample": None, "history": []}
            if not session or machine["phase"] == "stopped":
                self.cache.pop(account, None)
                return blank
            if machine["phase"] in {"disconnected", "stopping"}:
                return {**blank, "status": "disconnected"}
            if not port:
                return {**blank, "status": "waiting"}
            if cached and time.monotonic() - cached["fetched"] < 1.8:
                return cached["payload"]
            try:
                async with self.manager.http.get(f"http://127.0.0.1:{port}/metrics",
                                                 timeout=aiohttp.ClientTimeout(total=3)) as response:
                    response.raise_for_status()
                    body = await response.json()
                sample = body.get("sample") if isinstance(body, dict) else None
                validate_sample(sample)
                stamp = sample.get("sampled_at")
                if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not -10 <= time.time() - stamp <= 10:
                    raise ValueError("Stale sample")
                history = list(cached["payload"]["history"]) if cached else []
                if not history or history[-1]["sampled_at"] != stamp:
                    history.append(sample)
                payload = {**blank, "status": "live", "sample": sample,
                           "history": [s for s in history[-60:] if stamp - s["sampled_at"] <= 120]}
            except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
                payload = {**blank, "status": "unavailable"}
            # Recheck after network IO; a stop/replacement must invalidate an in-flight response.
            latest = self.manager.machine(account)
            if latest.get("session") != session or latest["phase"] in {"stopped", "stopping", "disconnected"}:
                self.cache.pop(account, None)
                return {**blank, "session": latest.get("session"), "status": "offline" if not latest.get("session") else "disconnected"}
            self.cache[account] = {"session": session, "fetched": time.monotonic(), "payload": payload}
            return payload

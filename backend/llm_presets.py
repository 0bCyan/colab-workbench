"""Curated, pinned deployment recipes. Benchmark results are maintained separately."""

import json

from .config import ROOT

LLM_ROOT = "/content/colab-workbench-llm"
PRESETS = sorted(
    [json.loads(path.read_text()) for path in (ROOT / "templates/llm").glob("*.json")],
    key=lambda item: item["order"],
)


def preset(key):
    for item in PRESETS:
        if item["id"] == key:
            return item
    raise ValueError("未知模型预设")


def default_config():
    return {"preset_id": "qwen27-fp8", "context_tokens": 262144, "gpu_memory": 0.90, "cpu_threads": 24}


def validate_config(value):
    recipe = preset(value["preset_id"])
    if not recipe["revision"]:
        raise ValueError("该预设尚未固定模型版本")
    if value["context_tokens"] not in (32768, 131072, 262144):
        raise ValueError("请选择 32K、128K 或 256K 总上下文")
    if value["gpu_memory"] not in (0.8, 0.9, 0.95):
        raise ValueError("显存预算无效")
    if value["cpu_threads"] not in (8, 16, 24, 48):
        raise ValueError("CPU 线程数无效")
    return value

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
    return {"preset_id": PRESETS[0]['id'], "context_tokens": 262144, "gpu_memory": 0.90, "cpu_threads": 12}


def validate_config(value):
    recipe = preset(value["preset_id"])
    if not recipe["revision"]:
        raise ValueError("该预设尚未固定模型版本")
    if value["context_tokens"] != recipe['context_tokens']:
        raise ValueError("当前验证版本使用 256K 总上下文")
    if value["gpu_memory"] not in (0.8, 0.9, 0.95):
        raise ValueError("显存预算无效")
    if value["cpu_threads"] != recipe['cpu_threads']:
        raise ValueError("当前验证版本固定使用 12 个 CPU 线程")
    return value

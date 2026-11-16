"""Curated, pinned deployment recipes. Benchmark results are maintained separately."""

import json

from .config import ROOT

LLM_ROOT = "/content/colab-workbench-llm"
LIFECYCLE = {"idle_shutdown": False, "stop_on_exit": False, "heartbeat_seconds": 15,
             "heartbeat_failures": 3, "reconnect_seconds": 60, "assignment_ping_seconds": 60}
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
    return {"preset_id": PRESETS[0]['id'], "context_tokens": 262144, "gpu_memory": 0.90, "cpu_threads": 12,
            "cache_enabled": True, "cache_mib": 4096, "download_workers": 64}


def request_defaults(context_tokens=262144):
    return {"thinking": True, "thinking_mode": "effort", "reasoning_effort": "xhigh", "reasoning_budget_tokens": 0,
            "preserve_thinking": True,
            "max_tokens": min(4096, context_tokens - 1), "tools_enabled": True, "parallel_tool_calls": True}


def validate_defaults(value, context_tokens):
    if not isinstance(value, dict) or set(value) - set(request_defaults()):
        raise ValueError('请求默认参数无效')
    for key in ('thinking', 'preserve_thinking', 'tools_enabled', 'parallel_tool_calls'):
        if type(value.get(key)) is not bool:
            raise ValueError('开关参数无效')
    if value.get('reasoning_effort') not in {'low', 'medium', 'xhigh'}:
        raise ValueError('请选择 low、medium 或 xhigh 思考深度')
    if value.get('thinking_mode') not in {'effort', 'budget'}:
        raise ValueError('请选择思考深度或预算模式')
    if type(value.get('max_tokens')) is not int or not 1 <= value['max_tokens'] < context_tokens:
        raise ValueError('最大输出须为小于总上下文的正整数')
    if type(value.get('reasoning_budget_tokens')) is not int or not 0 <= value['reasoning_budget_tokens'] <= value['max_tokens']:
        raise ValueError('思考预算须为 0 到最大输出之间的整数')
    if value['thinking'] and value['thinking_mode'] == 'budget' and value['reasoning_budget_tokens'] == 0:
        raise ValueError('自定义思考预算须为正整数')
    return value


def validate_config(value):
    recipe = preset(value["preset_id"])
    if not recipe["revision"]:
        raise ValueError("该预设尚未固定模型版本")
    if type(value['context_tokens']) is not int or not 4096 <= value['context_tokens'] <= recipe['context_tokens']:
        raise ValueError("总上下文须为 4096 到 262144 的整数")
    if value["gpu_memory"] not in (0.8, 0.9, 0.95):
        raise ValueError("显存预算无效")
    if type(value['cpu_threads']) is not int or not 1 <= value['cpu_threads'] <= 48:
        raise ValueError("CPU 并行线程须为 1 到 48 的整数")
    if type(value.get('cache_enabled', True)) is not bool:
        raise ValueError('缓存开关无效')
    if type(value.get('cache_mib', 4096)) is not int or not 0 <= value.get('cache_mib', 4096) <= 8192:
        raise ValueError('会话缓存须为 0 到 8192 MiB 的整数')
    if type(value.get('download_workers', 64)) is not int or not 1 <= value.get('download_workers', 64) <= 256:
        raise ValueError('下载并行数须为 1 到 256 的整数')
    return value

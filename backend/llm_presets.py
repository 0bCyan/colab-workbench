"""Curated, pinned deployment recipes. Benchmark results are maintained separately."""

LLM_ROOT = "/content/colab-workbench-llm"
LLAMA_COMMIT = "5d4c8eeb24db049acf74bd6f354bed6eaeffc7dc"
PRESETS = [
    {"id": "qwen27-fp8", "name": "Qwen3.8 · 27B", "engine": "vllm", "quantization": "FP8",
     "model": "Qwen/Qwen3.8-27B-FP8", "revision": "c3a462cc3d38cf351f3b26cbd763469eeb74ef81",
     "size_gib": 28.75, "tier": "极速", "reduced_refusal": False},
    {"id": "qwen27-rana", "name": "Qwen3.8 · 27B RANA", "engine": "vllm", "quantization": "FP8",
     "model": "abliterant/Qwen3.8-27B-RANA-abliterated-FP8", "revision": "734456efab210cec52fbee771a87405572a5d413",
     "size_gib": 28.75, "tier": "极速", "reduced_refusal": True},
    {"id": "qwen27-huihui", "name": "Qwen3.8 · 27B Huihui", "engine": "vllm", "quantization": "FP8",
     "model": "munekazu/Huihui-Qwen3.8-27B-abliterated-FP8", "revision": "badcb1961b646b3bdf1fc9ebdd9727826a55a48f",
     "size_gib": 28.75, "tier": "极速", "reduced_refusal": True},
    {"id": "flash-iq3", "name": "Qwen3.8 · Flash-Next", "engine": "llama.cpp", "quantization": "IQ3_S",
     "model": "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF",
     "revision": "ed59f92082b1e93c0e96d60a8b11aab089b52f09",
     "files": [f"IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-{part:05d}-of-00002.gguf" for part in (1, 2)],
     "size_gib": 77.88, "tier": "均衡", "reduced_refusal": False},
]


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

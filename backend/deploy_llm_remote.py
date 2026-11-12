"""Executed only in an owned LLM VM. All runtime/model files stay in this root."""
import fcntl
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path("/content/colab-workbench-llm")


def status(phase, message):
    path = ROOT / "status.tmp"
    path.write_text(json.dumps({"phase": phase, "message": message, "time": time.time()}))
    path.replace(ROOT / "status.json")
    print(message, flush=True)


def healthy():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=3) as response:
            body = json.load(response)
        return any(item.get("id") == json.loads((ROOT / "config.json").read_text())["recipe"]["model_name"] for item in body.get("data", []))
    except Exception:
        return False


def run(args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def serve():
    config = json.loads((ROOT / "config.json").read_text())
    from huggingface_hub import hf_hub_download, snapshot_download

    recipe = config["recipe"]
    status("downloading", "下载模型权重")
    if recipe["engine"] == "vllm":
        path = snapshot_download(recipe["model"], revision=recipe["revision"],
                                 cache_dir=str(ROOT / "hf-cache"),
                                 allow_patterns=["*.json", "*.safetensors", "*.jinja", "*.txt", "*.model"])
        command = [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", path,
                   "--served-model-name", recipe["model_name"], "--host", "127.0.0.1", "--port", "8000",
                   "--max-model-len", str(config["context_tokens"]), "--max-num-seqs", "1",
                   "--gpu-memory-utilization", str(config["gpu_memory"]), "--kv-cache-dtype", "fp8",
                   "--enable-chunked-prefill", "--max-num-batched-tokens", "4096",
                   "--limit-mm-per-prompt", '{"image":0,"video":0}',
                   "--enable-prefix-caching", "--reasoning-parser", "qwen3",
                   "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
                   "--speculative-config", '{"method":"mtp","num_speculative_tokens":3}']
    else:
        paths = [hf_hub_download(recipe["model"], name, revision=recipe["revision"],
                                 cache_dir=str(ROOT / "hf-cache")) for name in recipe["files"]]
        # HF cache blobs have hashed names. The GGUF loader discovers native shard names.
        model_dir = ROOT / "gguf"
        model_dir.mkdir(exist_ok=True)
        for name, path in zip(recipe["files"], paths, strict=True):
            target = model_dir / Path(name).name
            target.unlink(missing_ok=True)
            target.symlink_to(path)
        command = [str(ROOT / "llama.cpp/build/bin/llama-server"), "--model", str(model_dir / Path(recipe["files"][0]).name),
                   "--alias", recipe["model_name"], "--host", "127.0.0.1", "--port", "8000", "--parallel", "1",
                   "--ctx-size", str(config["context_tokens"]), "--n-gpu-layers", "99", "--flash-attn", "on",
                   "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--threads", str(config["cpu_threads"]),
                   "--jinja", "--lazy-mode", "on", "--batch-size", "512", "--ubatch-size", "128"]
    status("loading", "加载模型，准备服务")
    with (ROOT / "server.log").open("ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (ROOT / "server.pid").write_text(str(process.pid))
    for _ in range(900):
        if process.poll() is not None:
            raise RuntimeError("推理进程已退出，请查看服务日志")
        if healthy():
            status("ready", "服务已就绪")
            return
        time.sleep(2)
    raise TimeoutError("模型加载超时")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    os.environ.update(HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_PROGRESS_BARS="1", UV_CACHE_DIR=str(ROOT / "uv-cache"))
    # The parent retains the deployment lock while its environment-specific child loads.
    if "--serve" in sys.argv:
        os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        return serve()
    with (ROOT / "deploy.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            if healthy():
                return status("ready", "服务已就绪")
            config = json.loads((ROOT / "config.json").read_text())
            status("checking", "检查 GPU 与部署空间")
            import shutil

            import torch
            if not torch.cuda.is_available() or torch.cuda.get_device_capability()[0] < 12:
                raise RuntimeError("当前预设需要 Blackwell GPU，请使用 G4")
            cached = sum(p.stat().st_size for p in (ROOT / "hf-cache").glob("models--*/blobs/*") if p.is_file())
            remaining = max(0, config["recipe"]["size_gib"] * 2**30 - cached)
            if shutil.disk_usage(ROOT).free < remaining + 20 * 2**30:
                raise RuntimeError("磁盘空间不足，请释放本服务机器后重新部署")
            status("installing", "创建独立 Python 3.12 环境")
            tools = ROOT / "tools"
            run([sys.executable, "-m", "pip", "install", "--target", tools, "uv==0.9.5"])
            env = {**os.environ, "PYTHONPATH": str(tools)}
            uv = [sys.executable, "-m", "uv"]
            python = ROOT / "venv/bin/python"
            if not python.exists():
                run([*uv, "venv", "--python", "3.12", ROOT / "venv"], env=env)
            packages = ["huggingface_hub==1.31.0"]
            if config["recipe"]["engine"] == "vllm":
                packages.append("vllm==" + config["recipe"]["engine_version"])
            else:
                packages.extend(["cmake==4.1.2", "ninja==1.13.0"])
            run([*uv, "pip", "install", "--python", python, *packages], env=env)
            if config["recipe"]["engine"] == "llama.cpp":
                status("installing", "编译 llama.cpp 的 Blackwell 内核")
                source = ROOT / "llama.cpp"
                source.mkdir(exist_ok=True)
                if not (source / ".git").exists():
                    run(["git", "-C", source, "init"])
                    run(["git", "-C", source, "remote", "add", "origin", "https://github.com/ggml-org/llama.cpp.git"])
                run(["git", "-C", source, "fetch", "--depth", "1", "origin", config["recipe"]["engine_version"]])
                run(["git", "-C", source, "checkout", "--detach", config["recipe"]["engine_version"]])
                run([python.parent / "cmake", "-S", source, "-B", source / "build", "-DGGML_CUDA=ON", "-DCMAKE_CUDA_ARCHITECTURES=120", "-DLLAMA_CURL=OFF"])
                run([python.parent / "cmake", "--build", source / "build", "--config", "Release", "-j", "8", "--target", "llama-server"])
            run([python, Path(__file__), "--serve"])
        except Exception as exc:
            traceback.print_exc()
            status("error", str(exc))
            raise


if __name__ == "__main__":
    main()

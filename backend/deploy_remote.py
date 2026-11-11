"""Executed on the owned Colab VM. Contains no local credentials."""
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path("/content/colab-workbench")
COMFY = ROOT / "ComfyUI"
COMMIT = "4d64a8bc072b81c9b47321789b4d83fb7348ae2a"
REVISION = "54472221d27c695fca9621b0c3ed52f06c4af707"
FILES = [
    "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "vae/minimax_h3_video_vae_int8_convrot.safetensors",
    "vae/minimax_h3_audio_vae_fp32.safetensors",
]


def status(phase, message, **extra):
    data = {"phase": phase, "message": message, "time": time.time(), **extra}
    temp = ROOT / "deploy-status.tmp"
    temp.write_text(json.dumps(data))
    temp.replace(ROOT / "deploy-status.json")
    print(json.dumps(data), flush=True)


def run(args):
    subprocess.run(args, check=True)


def healthy():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    # A single deployment owns this directory; retry does not race a previous download.
    import fcntl
    with (ROOT / "deploy.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Deployment is already active", flush=True)
            return
        try:
            if healthy():
                status("ready", "ComfyUI 已就绪")
                return
            status("checking", "检查 Blackwell GPU 与 CUDA")
            import torch
            if not torch.cuda.is_available() or torch.cuda.get_device_capability()[0] < 12:
                raise RuntimeError("此预设需要 Blackwell GPU：请申请 G4 / RTX PRO 6000")
            if int(torch.version.cuda.split('.')[0]) < 13:
                raise RuntimeError("INT8 ConvRot 预设需要 CUDA 13 的 PyTorch")
            status("installing", "安装已验证版本的 ComfyUI")
            # Checkout first; downloads must not create models/ before Git initialization.
            COMFY.mkdir(parents=True, exist_ok=True)
            if not (COMFY / ".git").exists():
                run(["git", "-C", str(COMFY), "init"])
                run(["git", "-C", str(COMFY), "remote", "add", "origin", "https://github.com/Comfy-Org/ComfyUI.git"])
            run(["git", "-C", str(COMFY), "fetch", "--depth", "1", "origin", COMMIT])
            run(["git", "-C", str(COMFY), "checkout", "--detach", COMMIT])
            python = ROOT / "venv/bin/python"
            if not python.exists():
                run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(ROOT / "venv")])
            run([str(python), "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "-r", str(COMFY / "requirements.txt"), "huggingface_hub"])
            # Re-exec into the venv for downloads and serving after the install lock is released.
            run([str(python), str(ROOT / "deploy_remote.py"), "--download-and-serve"])
        except Exception as exc:
            traceback.print_exc()
            status("error", str(exc))
            raise


def download_and_serve():
    from huggingface_hub import hf_hub_download
    records = []
    for index, filename in enumerate(FILES):
        status("downloading", f"下载模型 {index + 1}/{len(FILES)} · {Path(filename).name}", file_index=index, total_files=len(FILES))
        path = Path(hf_hub_download("Comfy-Org/MiniMax-H3", filename, revision=REVISION, cache_dir=str(ROOT / "hf-cache")))
        target = COMFY / "models" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.symlink_to(path)
        records.append({"file": filename, "bytes": path.stat().st_size, "revision": REVISION})
    (ROOT / "assets.json").write_text(json.dumps(records))
    status("starting", "启动 ComfyUI，检查模型加载器")
    with (ROOT / "server.log").open("ab") as log:
        proc = subprocess.Popen([
            sys.executable, str(COMFY / "main.py"), "--listen", "127.0.0.1", "--port", "8188",
            "--disable-auto-launch", "--disable-all-custom-nodes", "--use-pytorch-cross-attention",
            "--preview-method", "none",
        ], cwd=COMFY, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (ROOT / "server.pid").write_text(str(proc.pid))
    for _ in range(180):
        if proc.poll() is not None:
            raise RuntimeError("ComfyUI 启动失败：" + (ROOT / "server.log").read_text(errors="replace")[-3500:])
        if healthy():
            status("ready", "ComfyUI 与四个模型已就绪", gpu="RTX PRO 6000", assets=records)
            return
        time.sleep(2)
    raise TimeoutError("ComfyUI 启动超时，请检查 server.log")


if __name__ == "__main__":
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    if "--download-and-serve" in sys.argv:
        download_and_serve()
    else:
        main()

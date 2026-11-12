"""Executed on the owned Colab VM. Contains no local credentials."""
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

if __package__:
    from .bundle_restore_remote import prepare_in_parallel, restore
else:
    from bundle_restore_remote import prepare_in_parallel, restore

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
IMAGE_REVISION = '0bb149f51e3156ed4b90dd45d5620346bfe1df8f'
IMAGE_FILES = [
    'diffusion_models/minimax_h3_image_pruned_int8_convrot.safetensors',
    'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
    'vae/minimax_h3_image_vae_fp16.safetensors',
]


def selected_model():
    config = ROOT / 'media-config.json'
    model = json.loads(config.read_text())['model'] if config.exists() else 'minimax-h3'
    if model not in {'minimax-h3', 'minimax-image'}:
        raise ValueError('未知创作模型')
    return model


def status(phase, message, **extra):
    data = {"phase": phase, "message": message, "time": time.time(), 'model': selected_model(), **extra}
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


def restore_cloud_weights():
    import requests

    cloud = json.loads((ROOT / 'bundle-config.json').read_text())
    key = 'h3-image-g4-v1' if selected_model() == 'minimax-image' else 'h3-video-g4-v1'
    response = requests.get(cloud['base'] + '/' + key + '.json', timeout=30)
    response.raise_for_status()
    index = response.json()
    if not index.get('weights_only') or index.get('preset') != key:
        raise ValueError('需要对应 H3 模型的纯权重部署包')
    weights = ROOT / 'weights'
    if not os.path.ismount(weights):
        available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:'))
        if available < index['total_bytes'] + 30 * 2**30:
            raise RuntimeError('可用内存不足以容纳 H3 视频权重和推理工作区')
        weights.mkdir(exist_ok=True)
        if any(weights.iterdir()):
            raise RuntimeError('权重目录非空，拒绝覆盖')
        run(['mount', '-t', 'tmpfs', '-o', 'size=80G', 'workbench-weights', str(weights)])
    broker = ROOT / 'weight-download-grant.json'
    try:
        broker.write_text(json.dumps({**cloud, 'bundle_id': key}))
        broker.chmod(0o600)
        restore(broker, ROOT, workers=64)
    finally:
        broker.unlink(missing_ok=True)
    return index['assets']


def install_environment():
    COMFY.mkdir(parents=True, exist_ok=True)
    if not (COMFY / '.git').exists():
        run(['git', '-C', str(COMFY), 'init'])
        run(['git', '-C', str(COMFY), 'remote', 'add', 'origin', 'https://github.com/Comfy-Org/ComfyUI.git'])
    run(['git', '-C', str(COMFY), 'fetch', '--depth', '1', 'origin', COMMIT])
    run(['git', '-C', str(COMFY), 'checkout', '--detach', COMMIT])
    python = ROOT / 'venv/bin/python'
    if not python.exists():
        run([sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages', str(ROOT / 'venv')])
    run([str(python), '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check',
         '-r', str(COMFY / 'requirements.txt'), 'huggingface_hub'])


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
                # Add the selected model to this VM without restarting ComfyUI or discarding other weights.
                download_models()
                status("ready", "ComfyUI 已就绪")
                return
            status("checking", "检查 Blackwell GPU 与 CUDA")
            import torch
            if not torch.cuda.is_available() or torch.cuda.get_device_capability()[0] < 12:
                raise RuntimeError("此预设需要 Blackwell GPU：请申请 G4 / RTX PRO 6000")
            if int(torch.version.cuda.split('.')[0]) < 13:
                raise RuntimeError("INT8 ConvRot 预设需要 CUDA 13 的 PyTorch")
            if (ROOT / 'bundle-config.json').exists():
                status('downloading', '权重下载到内存，同时安装 ComfyUI 环境')
                prepare_in_parallel(restore_cloud_weights, install_environment)
            else:
                status('installing', '安装已验证版本的 ComfyUI')
                install_environment()
            # Re-exec into the venv for downloads and serving after the install lock is released.
            run([str(ROOT / 'venv/bin/python'), str(ROOT / "deploy_remote.py"), "--download-and-serve"])
        except Exception as exc:
            traceback.print_exc()
            status("error", str(exc))
            raise


def download_models():
    if (ROOT / 'bundle-config.json').exists():
        records = restore_cloud_weights()
        for item in records:
            target = COMFY / 'models' / item['file']
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            if not target.exists():
                target.symlink_to((ROOT / item['target']).resolve(strict=True))
        (ROOT / 'assets.json').write_text(json.dumps(records))
        return records
    from huggingface_hub import hf_hub_download
    records = []
    files = IMAGE_FILES if selected_model() == 'minimax-image' else FILES
    for index, filename in enumerate(files):
        image_file = filename in IMAGE_FILES and not filename.startswith('text_encoders/')
        repo, revision = ('Comfy-Org/MiniMax-H3-Image', IMAGE_REVISION) if image_file else ('Comfy-Org/MiniMax-H3', REVISION)
        status("downloading", f"下载模型 {index + 1}/{len(files)} · {Path(filename).name}", file_index=index, total_files=len(files))
        path = Path(hf_hub_download(repo, filename, revision=revision, cache_dir=str(ROOT / "hf-cache")))
        target = COMFY / "models" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.symlink_to(path)
        records.append({"file": filename, "bytes": path.stat().st_size, "revision": revision})
    (ROOT / "assets.json").write_text(json.dumps(records))
    return records


def download_and_serve():
    records = download_models()
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
            status("ready", "ComfyUI 与所选模型已就绪", gpu="RTX PRO 6000", assets=records)
            return
        time.sleep(2)
    raise TimeoutError("ComfyUI 启动超时，请检查 server.log")


if __name__ == "__main__":
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    if "--download-and-serve" in sys.argv:
        download_and_serve()
    else:
        main()

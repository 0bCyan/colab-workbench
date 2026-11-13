"""Executed on the owned Colab VM. Contains no local credentials."""
import json
import os
import subprocess
import sys
import threading
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


def selected_model():
    config = ROOT / 'media-config.json'
    model = json.loads(config.read_text())['model'] if config.exists() else 'minimax-h3'
    if model not in {'minimax-h3', 'minimax-image', 'minimax-image-bf16'}:
        raise ValueError('未知创作模型')
    return model


STATUS_LOCK = threading.Lock()


def write_progress(update, reset=False):
    with STATUS_LOCK:
        path = ROOT / 'deploy-status.json'
        try:
            data = json.loads(path.read_text()) if not reset else {}
        except (OSError, ValueError):
            data = {}
        data.setdefault('started_at', time.time())
        data.update(time=time.time(), model=selected_model())
        update(data)
        temp = ROOT / 'deploy-status.tmp'
        temp.write_text(json.dumps(data))
        temp.replace(path)
    return data


def status(phase, message, reset=False, **extra):
    data = write_progress(lambda data: data.update(phase=phase, message=message, **extra), reset)
    print(json.dumps(data), flush=True)


def stage(name, value, **extra):
    def update(data):
        current = data.setdefault('stages', {}).setdefault(name, {})
        if current.get('status') in {'completed', 'error'} and value in {'running', 'waiting'}:
            return
        current.update(status=value, **extra)
    write_progress(update)


def monitor_download(done):
    path = ROOT.parent / 'stream-restore-status.json'
    while not done.wait(.25):
        try:
            summary = json.loads(path.read_text())
            stage('download', 'completed' if summary.get('restored') else 'running',
                  bytes=summary.get('bytes', 0), total_bytes=summary.get('total_bytes', 0),
                  elapsed_seconds=summary.get('elapsed_seconds', 0), rate_bytes_s=summary.get('rate_bytes_s', 0),
                  files_done=summary.get('files_done', 0), files_total=summary.get('files_total', 0),
                  current=summary.get('current'))
        except (OSError, ValueError):
            pass


def run(args):
    subprocess.run(args, check=True)


def healthy():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def restore_cloud_weights():
    """HF is primary; an optional Drive grant is used only if transfer fails."""
    source = json.loads((ROOT / 'media-config.json').read_text()).get('weight_source', 'hf')
    if source not in {'hf', 'drive'}:
        raise ValueError('未知权重来源')
    fallback = ROOT / 'bundle-config.json'
    cloud = json.loads(fallback.read_text()) if fallback.exists() else None
    model = selected_model()
    key = {'minimax-h3': 'h3-video-g4-v1', 'minimax-image': 'h3-image-g4-v1',
           'minimax-image-bf16': 'minimax-image-bf16'}[model]
    if source == 'hf':
        index = json.loads((ROOT / 'public-weights.json').read_text())
    else:
        if not cloud:
            raise RuntimeError('此自有模型需要网盘权重及只读授权')
        import requests
        response = requests.get(cloud['base'] + '/' + key + '.json', timeout=(5, 30))
        response.raise_for_status()
        index = response.json()
    if not index.get('weights_only') or index.get('preset') != key:
        raise ValueError('需要对应 H3 模型的纯权重部署包')
    large = model == 'minimax-image-bf16'
    weights = ROOT / 'weights'
    available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:'))
    missing = sum(item['bytes'] for item in index['files'] if not (ROOT / item['target']).is_file()
                  or (ROOT / item['target']).stat().st_size != item['bytes'])
    if missing and available < missing + 30 * 2**30:
        raise RuntimeError('可用内存不足以容纳所选 H3 权重和推理工作区')
    if not os.path.ismount(weights):
        weights.mkdir(exist_ok=True)
        if any(weights.iterdir()):
            raise RuntimeError('权重目录非空，拒绝覆盖')
        run(['mount', '-t', 'tmpfs', '-o', 'size=128G' if large else 'size=80G', 'workbench-weights', str(weights)])
    elif large:
        run(['mount', '-o', 'remount,size=128G', str(weights)])
    stage('download', 'running', bytes=0, total_bytes=index['total_bytes'], files_done=0, files_total=len(index['files']))
    broker = ROOT / 'weight-download-grant.json'
    broker.unlink(missing_ok=True)
    try:
        if cloud:
            broker.write_text(json.dumps({**cloud, 'bundle_id': key}))
            broker.chmod(0o600)
        result = restore(broker if broker.exists() else None, ROOT, workers=64,
                         public_index=index if source == 'hf' else None)
        stage('download', 'completed', bytes=result['total_bytes'], total_bytes=result['total_bytes'],
              files_done=len(index['files']), files_total=len(index['files']), current=None)
    except BaseException:
        stage('download', 'error')
        raise
    finally:
        broker.unlink(missing_ok=True)
    return index['assets']


def install_environment():
    stage('environment', 'running', message='安装 ComfyUI 依赖')
    try:
        _install_environment()
        stage('environment', 'completed', message='环境已就绪')
    except BaseException:
        stage('environment', 'error', message='环境安装失败')
        raise


def _install_environment():
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
            status('checking', '检查 GPU 与部署环境', reset=True)
            (ROOT.parent / 'stream-restore-status.json').unlink(missing_ok=True)
            if healthy():
                # Add the selected model to this VM without restarting ComfyUI or discarding other weights.
                stage('environment', 'completed', message='复用当前环境')
                done = threading.Event()
                monitor = threading.Thread(target=monitor_download, args=(done,), daemon=True)
                monitor.start()
                try:
                    download_models()
                finally:
                    done.set()
                    monitor.join(timeout=1)
                status("ready", "ComfyUI 已就绪")
                return
            status("checking", "检查 Blackwell GPU 与 CUDA")
            import torch
            if not torch.cuda.is_available() or torch.cuda.get_device_capability()[0] < 12:
                raise RuntimeError("此预设需要 Blackwell GPU：请申请 G4 / RTX PRO 6000")
            if int(torch.version.cuda.split('.')[0]) < 13:
                raise RuntimeError("INT8 ConvRot 预设需要 CUDA 13 的 PyTorch")
            status('downloading', '权重下载到内存，同时安装 ComfyUI 环境')
            done = threading.Event()
            monitor = threading.Thread(target=monitor_download, args=(done,), daemon=True)
            monitor.start()
            try:
                prepare_in_parallel(restore_cloud_weights, install_environment)
            finally:
                done.set()
                monitor.join(timeout=1)
            # Re-exec into the venv for downloads and serving after the install lock is released.
            run([str(ROOT / 'venv/bin/python'), str(ROOT / "deploy_remote.py"), "--download-and-serve"])
        except Exception as exc:
            traceback.print_exc()
            status("error", str(exc))
            raise


def download_models():
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

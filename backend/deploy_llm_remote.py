"""Restore and start the verified Strata package; never receives refresh credentials."""
import fcntl
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

ROOT = Path('/content/colab-workbench-llm')


def status(phase, message, **extra):
    temporary = ROOT / 'status.tmp'
    temporary.write_text(json.dumps({'phase': phase, 'message': message, 'time': time.time(), **extra}))
    temporary.replace(ROOT / 'status.json')
    print(message, flush=True)


def healthy(model):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/v1/models', timeout=3) as response:
            return any(item['id'] == model for item in json.load(response).get('data', []))
    except Exception:
        return False


def main():
    ROOT.mkdir(exist_ok=True, parents=True)
    with (ROOT / 'deploy.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        broker = ROOT / 'download-grant.json'
        try:
            config = json.loads((ROOT / 'config.json').read_text())
            recipe = config['recipe']
            if healthy(recipe['model_name']):
                return status('ready', '服务已就绪')
            status('checking', '检查 G4、CUDA 与可用内存')
            import torch
            if not torch.cuda.is_available() or torch.cuda.get_device_capability()[0] != 12:
                raise RuntimeError('此版本需要 G4 / RTX PRO 6000 Blackwell')
            target = ROOT / 'model'
            if not os.path.ismount(target):
                available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                                 if line.startswith('MemAvailable:'))
                if available < recipe['minimum_available_ram_gib'] * 2**30:
                    raise RuntimeError('此已验证内存直载版本需要至少 155 GiB 可用内存')
                target.mkdir(exist_ok=True)
                if any(target.iterdir()):
                    raise RuntimeError('恢复目录非空，拒绝覆盖；请重新申请此实例')
                subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=145G', 'workbench-llm', str(target)], check=True)
            broker.write_text(json.dumps({**config['cloud'], 'bundle_id': recipe['bundle_id']}))
            broker.chmod(0o600)
            status('downloading', '权重下载到内存，同时配置 Python 环境')
            transfer_done = threading.Event()

            def download():
                try:
                    return restore(broker, target, workers=recipe['restore_workers'])
                finally:
                    transfer_done.set()

            def environment():
                # The existing verified bundle puts its small engine/scripts archive first.
                while not (target / '.runtime-ready').is_file():
                    if transfer_done.wait(.2):
                        if not (target / 'requirements.txt').is_file():
                            raise RuntimeError('模型包缺少引擎运行依赖清单')
                subprocess.run([sys.executable, str(target / 'bootstrap.py'), '--skip-memory'], check=True)

            prepare_in_parallel(download, environment)
            profile = json.loads((target / 'preset.json').read_text())
            if profile['id'] != recipe['bundle_id'] or profile['model_revision'] != recipe['revision']:
                raise RuntimeError('部署包与模型配方不匹配')
            profile['engine_options']['mmap-experts'] = True
            (target / 'preset.json').write_text(json.dumps(profile))
            broker.unlink(missing_ok=True)
            status('loading', '加载 Strata NVFP4 与 MTP，连接服务端点')
            with (ROOT / 'server.log').open('ab') as output:
                process = subprocess.Popen([str(target / '.venv/bin/python'), str(target / 'start.py')],
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            (ROOT / 'server.pid').write_text(str(process.pid))
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError('模型进程退出，请查看实例日志')
                if healthy(recipe['model_name']):
                    return status('ready', '云盘部署包已就绪')
                time.sleep(2)
            raise TimeoutError('模型启动超过 10 分钟')
        except Exception as exc:
            traceback.print_exc()
            status('error', str(exc))
            raise
        finally:
            broker.unlink(missing_ok=True)


if __name__ == '__main__':
    main()

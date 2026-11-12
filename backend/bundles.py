"""Private local bundle catalog and short-lived read-only download grants over SSH."""
import asyncio
import configparser
import datetime
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path, PurePosixPath

from aiohttp import web

LLM_BUNDLE = 'orca-flash-next-nvfp4-strata-mtp-g4-v1'
MEDIA_BUNDLES = {'minimax-h3': 'h3-video-g4-v1'}


def validate_index(data, key):
    if not isinstance(key, str) or key not in {LLM_BUNDLE, *MEDIA_BUNDLES.values()} or not isinstance(data, dict):
        raise ValueError('未知部署包')
    files = data.get('files', [])
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files) or not data.get('complete') or data.get('preset') != key or not 1 <= len(files) <= 1000:
        raise ValueError('云盘部署包未完成校验：' + key)
    if key == LLM_BUNDLE and (type(data.get('deployment_revision')) is not int or data['deployment_revision'] < 3 or data.get('assemblies') or len(files) != 7):
        raise ValueError('LLM 需要已验证的七文件完整 PLE 清单')
    if key != LLM_BUNDLE and (not data.get('weights_only') or any(item.get('archive') for item in files)):
        raise ValueError('媒体部署包仅接受权重文件')
    for item in files:
        target = item.get('target', '')
        if not isinstance(target, str) or not target or PurePosixPath(target).is_absolute() or '..' in PurePosixPath(target).parts or '\\' in target:
            raise ValueError('权重目标路径无效')
        if type(item.get('bytes')) is not int or item['bytes'] <= 0 or not isinstance(item.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
            raise ValueError('权重大小或校验值无效')
        if not isinstance(item.get('file_id'), str) or not re.fullmatch('[-_A-Za-z0-9]+', item['file_id']) or not isinstance(item.get('name'), str):
            raise ValueError('云盘文件标识无效')
        if any(name in item for name in ('access_token', 'refresh_token', 'client_secret', 'upload_url')):
            raise ValueError('权重清单不能包含授权信息')
    if len({item['name'] for item in files}) != len(files):
        raise ValueError('部署文件名称重复')
    if data.get('total_bytes') != sum(item['bytes'] for item in files):
        raise ValueError('部署包总大小不一致')
    for asset in data.get('assets', []):
        destination = asset.get('file', '')
        if not isinstance(destination, str) or not destination or PurePosixPath(destination).is_absolute() or '..' in PurePosixPath(destination).parts or '\\' in destination:
            raise ValueError('素材目标路径无效')
        if asset.get('target') not in {item['target'] for item in files}:
            raise ValueError('素材引用了未声明的权重')
    return data


class Bundles:
    def __init__(self, directory):
        self.directory = Path(directory) / 'cloud'
        self.runner, self.port = None, None
        self.leases = {}
        self.token_lock = asyncio.Lock()

    def index(self, key):
        path = self.directory / (key + '.json')
        if key not in {LLM_BUNDLE, *MEDIA_BUNDLES.values()} or not path.is_file():
            raise ValueError('云盘部署包尚未配置：' + key)
        data = json.loads(path.read_text())
        return validate_index(data, key)

    def require(self, keys):
        for key in set(keys):
            self.index(key)
        if not self.reader_config().is_file() or not self.rclone():
            raise ValueError('请先配置本机云盘只读授权与 rclone')
        config = configparser.ConfigParser()
        config.read(self.reader_config())
        if config.get('reader', 'scope', fallback='') != 'drive.readonly':
            raise ValueError('云盘下载需要独立的 drive.readonly 授权')

    def public(self):
        rows = []
        for key in dict.fromkeys([LLM_BUNDLE, *MEDIA_BUNDLES.values()]):
            try:
                self.require([key])
                data = self.index(key)
                rows.append({'id': key, 'available': True, 'bytes': data.get('total_bytes', 0)})
            except (ValueError, OSError, configparser.Error):
                rows.append({'id': key, 'available': False, 'bytes': 0})
        return rows

    def reader_config(self):
        return Path(os.environ.get('WORKBENCH_DRIVE_READER', Path.home() / '.local/share/llm-workbench/drive-reader.conf'))

    def rclone(self):
        configured = os.environ.get('WORKBENCH_RCLONE') or shutil.which('rclone')
        fallback = Path.home() / '.local/share/llm-workbench/tools' / ('rclone.exe' if os.name == 'nt' else 'rclone')
        return configured or (str(fallback) if fallback.is_file() else '')

    async def reader_token(self):
        async with self.token_lock:
            config = configparser.ConfigParser()
            config.read(self.reader_config())
            try:
                token = json.loads(config['reader']['token'])
                expires = datetime.datetime.fromisoformat(token['expiry'].replace('Z', '+00:00')).timestamp()
            except (KeyError, ValueError) as exc:
                raise RuntimeError('云盘只读授权无效') from exc
            if expires - time.time() < 90:
                process = await asyncio.create_subprocess_exec(
                    self.rclone(), 'about', 'reader:', '--config', str(self.reader_config()), '--json',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    await asyncio.wait_for(process.communicate(), 60)
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    raise
                if process.returncode:
                    raise RuntimeError('云盘只读授权刷新失败')
                config.read(self.reader_config())
                token = json.loads(config['reader']['token'])
                expires = datetime.datetime.fromisoformat(token['expiry'].replace('Z', '+00:00')).timestamp()
            if config['reader'].get('scope') != 'drive.readonly':
                raise RuntimeError('云盘下载必须使用独立的 drive.readonly 授权')
            return {'access_token': token['access_token'], 'expires_at': expires,
                    'scope': 'https://www.googleapis.com/auth/drive.readonly'}

    async def open(self):
        async def read(request):
            lease = self.leases.get(request.match_info['nonce'])
            if not lease or lease['expires'] < time.time():
                raise web.HTTPNotFound()
            name = request.match_info['name']
            if name == 'token':
                try:
                    data = await self.reader_token()
                except Exception:
                    raise web.HTTPServiceUnavailable(text='Drive reader unavailable') from None
            elif name.endswith('.json') and name[:-5] in lease['keys']:
                data = self.index(name[:-5])
            else:
                raise web.HTTPNotFound()
            return web.json_response(data, headers={'Cache-Control': 'no-store'})

        app = web.Application()
        app.router.add_get('/{nonce}/{name}', read)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    def lease(self, owner, keys):
        self.require(keys)
        for nonce, item in list(self.leases.items()):
            if item['owner'] == owner:
                self.leases.pop(nonce)
        nonce = secrets.token_urlsafe(32)
        self.leases[nonce] = {'owner': owner, 'keys': set(keys), 'expires': time.time() + 7200}
        return {'base': f'http://127.0.0.1:8190/{nonce}', 'local_port': self.port}

    def revoke(self, owner):
        self.leases = {nonce: item for nonce, item in self.leases.items() if item['owner'] != owner}

    def renew(self, owner):
        for lease in self.leases.values():
            if lease['owner'] == owner:
                lease['expires'] = time.time() + 7200

    def allow(self, owner, keys):
        self.require(keys)
        for lease in self.leases.values():
            if lease['owner'] == owner:
                lease['keys'].update(keys)
                lease['expires'] = time.time() + 7200
                return
        raise ValueError('下载授权已过期，请重新连接此机器')

    async def close(self):
        self.leases.clear()
        if self.runner:
            await self.runner.cleanup()

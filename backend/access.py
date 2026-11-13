"""Single-user password access; secrets and persistent session records stay in runtime data."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

COOKIE = 'fieldwork_session'
SESSION_SECONDS = 12 * 3600


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${digest}'


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class Access:
    def __init__(self, directory, store, required=False):
        self.path, self.store = directory / 'access.json', store
        self.enabled = required or self.path.is_file()
        self.failures = {}
        self.config = None
        if self.enabled:
            try:
                self.config = json.loads(self.path.read_text())
                encoded = self.config['password_hash']
                scheme, salt, digest = encoded.split('$')
                if (scheme != 'scrypt' or len(bytes.fromhex(salt)) != 16 or len(bytes.fromhex(digest)) != 64
                    or not isinstance(self.config['username'], str) or not 1 <= len(self.config['username']) <= 100):
                    raise ValueError('Invalid password configuration')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError('工作台登录配置缺失或无效') from exc
        self.revision = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    def user(self, request):
        supplied = request.cookies.get(COOKIE, '')
        if not self.enabled or not supplied or len(supplied) > 256:
            return None
        key = hashlib.sha256(supplied.encode()).hexdigest()
        row = self.store.get('access_sessions', key)
        if not row or row.get('expires', 0) <= time.time() or row.get('revision') != self.revision:
            if row:
                self.store.delete('access_sessions', key)
            return None
        return row['username']

    async def login(self, request: Request, body: Login):
        if not self.enabled:
            raise HTTPException(409, '当前工作台未启用账号登录')
        peer = request.client.host if request.client else 'local'
        now = time.monotonic()
        failures = [stamp for stamp in self.failures.get(peer, []) if now - stamp < 60]
        if len(failures) >= 5:
            raise HTTPException(429, '尝试次数过多，请稍后重试', headers={'Retry-After': '60'})
        if len(self.failures) > 1000:
            self.failures = {key: value for key, value in self.failures.items() if value and now - value[-1] < 60}
        self.failures[peer] = failures + [now]
        _, salt, _ = self.config['password_hash'].split('$')
        candidate = await asyncio.to_thread(password_hash, body.password, salt)
        password_ok = hmac.compare_digest(candidate.encode(), self.config['password_hash'].encode())
        username_ok = hmac.compare_digest(body.username.encode(), self.config['username'].encode())
        if not password_ok or not username_ok:
            raise HTTPException(401, '账号或密码不正确')
        self.failures.pop(peer, None)
        old = request.cookies.get(COOKIE)
        if old:
            self.store.delete('access_sessions', hashlib.sha256(old.encode()).hexdigest())
        for row in self.store.all('access_sessions'):
            if row.get('expires', 0) <= time.time() or row.get('revision') != self.revision:
                self.store.delete('access_sessions', row['id'])
        session = secrets.token_urlsafe(32)
        key = hashlib.sha256(session.encode()).hexdigest()
        self.store.put('access_sessions', key, {'id': key, 'username': self.config['username'],
                                              'expires': time.time() + SESSION_SECONDS, 'revision': self.revision})
        response = JSONResponse({'authenticated': True, 'username': self.config['username']})
        response.set_cookie(COOKIE, session, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == 'https', samesite='lax', path='/')
        return response

    def logout(self, request):
        supplied = request.cookies.get(COOKIE, '')
        if supplied:
            self.store.delete('access_sessions', hashlib.sha256(supplied.encode()).hexdigest())
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(COOKIE, path='/', httponly=True, samesite='lax', secure=request.url.scheme == 'https')
        return response


def required_from_environment():
    return os.environ.get('WORKBENCH_AUTH_REQUIRED', '').lower() in {'1', 'true', 'yes'}

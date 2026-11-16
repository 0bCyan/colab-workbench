"""Each LLM instance owns its own VM, tunnel, endpoint and request slot."""
import asyncio
import base64
import contextlib
import json
import re
import secrets
import time
import uuid

import aiohttp

from .colab import clean
from .config import ROOT
from .llm_presets import (
    LIFECYCLE,
    LLM_ROOT,
    default_config,
    preset,
    request_defaults,
    validate_config,
    validate_defaults,
)
from .telemetry import Telemetry

BUSY = {"checking", "provisioning", "deploying", "stopping"}
TABLE = "llm_instances"
STATUS_READ_FAILURES = 5


def transient_status_error(error):
    if isinstance(error, (aiohttp.ClientError, TimeoutError, OSError)):
        return True
    text = str(error).lower()
    return isinstance(error, RuntimeError) and any(word in text for word in (
        'timeout', 'timed out', 'connection', 'ssl', 'eof', '502', '503', 'temporarily', 'network',
    ))


def error_message(error):
    return clean(str(error).strip() or type(error).__name__)


class LLM:
    def __init__(self, store, colab, profiles, bundles=None):
        self.store, self.colab, self.profiles = store, colab, {p["id"]: p for p in profiles}
        self.bundles = bundles
        self.tasks, self.stops, self.locks, self.active = {}, {}, {}, {}
        self.stopping = set()
        self.telemetry = Telemetry(self)
        self.http = None
        self.closing = False
        self.maintenance = None
        self.heartbeat_failures = {}
        self.reconnect_attempts = {}
        self.assignment_ping_task = None
        self.assignment_ping_attempts = {}
        if not store.get("meta", "llm_api_key"):
            store.put("meta", "llm_api_key", {"value": secrets.token_urlsafe(32)})
        self.migrate()
        self.migrate_catalog()
        for item in self.all():
            config = {**default_config(), **item['config']}
            defaults = {**request_defaults(config['context_tokens']), **item.get('request_defaults', {})}
            if defaults['reasoning_effort'] == 'high':
                defaults['reasoning_effort'] = 'xhigh'
            self.store.patch(TABLE, item['id'], config=config, request_defaults=defaults)
            self.register(item["id"])
            if not item.get("name"):
                self.machine(item["id"], name=self.default_name(default_config()['preset_id'], item["id"]), custom_name=False)

    def migrate_catalog(self):
        current = default_config()['preset_id']
        old_models = {'qwen27-fp8': 'Qwen/Qwen3.8-27B-FP8',
                      'flash-iq3': 'ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF',
                      'qwen27-rana': 'abliterant/Qwen3.8-27B-RANA-abliterated-FP8',
                      'qwen27-huihui': 'munekazu/Huihui-Qwen3.8-27B-abliterated-FP8'}
        for item in self.all():
            if item['config']['preset_id'] == current:
                continue
            if item.get('session'):
                self.store.patch(TABLE, item['id'], retired=True,
                                 legacy_model=item.get('legacy_model') or old_models.get(item['config']['preset_id'], 'workbench-llm'))
            else:
                changes = {'config': default_config(), 'retired': False, 'legacy_model': None, 'deployment': None,
                           'message': '已切换到已验证的 Strata NVFP4 云盘预设'}
                if not item.get('custom_name'):
                    changes['name'] = self.default_name(current, item['id'])
                self.store.patch(TABLE, item['id'], **changes)

    def register(self, key):
        self.locks[key] = asyncio.Lock()
        self.active[key] = 0

    def migrate(self):
        """Atomically preserve configured or live account-era services and their logs."""
        if self.store.get("meta", "llm_instances_migrated") is not None:
            return
        records = []
        for item in self.store.all("llm"):
            logs = self.store.get("llm_logs", item["account"], [])
            old_default = {"preset_id": "qwen27-fp8", "context_tokens": 262144, "gpu_memory": .9, "cpu_threads": 24}
            if not item.get("session") and item["phase"] == "stopped" and item["config"] in (default_config(), old_default) and not logs:
                continue
            key = "svc-" + uuid.uuid4().hex[:12]
            migrated = {**item, "id": key, "created_at": item.get("created_at", time.time()),
                        "legacy_path": item["account"], "legacy_session": item.get("session"),
                        "legacy_model": "workbench-llm" if item.get("session") else None}
            records.extend([(TABLE, key, json.dumps(migrated)), ("llm_logs", key, json.dumps(logs))])
        with self.store.db:
            self.store.db.execute("DELETE FROM records WHERE kind IN ('llm','llm_logs')")
            self.store.db.executemany("INSERT OR REPLACE INTO records VALUES(?,?,?)", records)
            self.store.db.execute("INSERT INTO records VALUES(?,?,?)", ("meta", "llm_instances_migrated", '{}'))

    def all(self):
        return sorted(self.store.all(TABLE), key=lambda item: item.get("created_at", 0))

    async def open(self):
        self.http = aiohttp.ClientSession(json_serialize=lambda value: json.dumps(value, ensure_ascii=False))
        for item in self.all():
            if item.get("session"):
                self.machine(item["id"], phase="disconnected", port=None, telemetry_port=None, message="保留原机器，等待恢复服务连接")
        self.maintenance = asyncio.create_task(self.maintain())
        self.assignment_ping_task = asyncio.create_task(self.maintain_assignments())

    def machine(self, key, **changes):
        if key not in self.locks:
            raise ValueError("服务实例不存在")
        return self.store.patch(TABLE, key, **changes) if changes else self.store.get(TABLE, key)

    def resolve(self, key):
        if key in self.locks:
            return key
        # Legacy paths remain attached to exactly the migrated instance, never an account's newest VM.
        for item in self.all():
            if item.get("legacy_path") == key:
                return item["id"]
        raise ValueError("服务实例不存在")

    def model_name(self, key):
        item = self.machine(key)
        return item.get("legacy_model") or preset(item["config"]["preset_id"])["model_name"]

    def public(self, key):
        item = self.machine(key)
        return {k: v for k, v in {**item, "endpoint_path": f"/llm/{key}/v1", "model_name": self.model_name(key),
                                  "active_requests": self.active[key], "lifecycle": dict(LIFECYCLE)}.items()
                if k not in {"port", "telemetry_port", "legacy_session", "legacy_model"}}

    def key(self):
        return self.store.get("meta", "llm_api_key")["value"]

    def log(self, key, message):
        items = self.store.get("llm_logs", key, [])
        message = clean(str(message)).replace(self.key(), "[redacted]")
        self.store.put("llm_logs", key, (items + [{"time": time.time(), "message": message}])[-150:])

    def owned(self, key, session):
        item = self.machine(key)
        install = self.store.get("meta", "install")["id"]
        prefix = f"wbllm-{install}-{item['account'].removeprefix('colab')}-"
        return session.startswith(prefix + key.removeprefix('svc-') + '-') or (
            session == item.get("legacy_session") and session.startswith(prefix))

    def validate(self, account, config):
        if account not in self.profiles:
            raise ValueError("未知账号")
        validate_config(config)

    def default_name(self, preset_id, exclude=None):
        base = preset(preset_id)["name"]
        used = {item.get("name") for item in self.all() if item["id"] != exclude}
        number = 1
        while f"{base} · {number}" in used:
            number += 1
        return f"{base} · {number}"

    def validate_name(self, name):
        name = name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("实例名称须为 1–80 个字符，不能包含换行或控制字符")
        return name

    def create(self, account, config, name=None):
        config = {**default_config(), **config}
        self.validate(account, config)
        label = self.validate_name(name) if name is not None else self.default_name(config["preset_id"])
        key = "svc-" + uuid.uuid4().hex[:12]
        self.store.put(TABLE, key, {"id": key, "account": account, "phase": "stopped", "session": None,
                                  "name": label, "custom_name": name is not None,
                                  "port": None, "message": "按需启动服务", "config": config,
                                  "request_defaults": request_defaults(config['context_tokens']), "created_at": time.time()})
        self.register(key)
        return self.public(key)

    def rename(self, key, name):
        self.machine(key)
        self.machine(key, name=self.validate_name(name), custom_name=True)
        return self.public(key)

    def configure(self, key, account, config):
        item = self.machine(key)
        config = {**default_config(), **config}
        self.validate(account, config)
        if config == item["config"] and account == item["account"]:
            return self.public(key)
        if item.get("session") or item["phase"] in BUSY or key in self.stopping:
            raise ValueError("请先关闭此实例，再更换部署配置或账号")
        changes = {}
        if not item.get("custom_name") and config["preset_id"] != item["config"]["preset_id"]:
            changes["name"] = self.default_name(config["preset_id"], key)
        defaults = {**request_defaults(config['context_tokens']), **item.get('request_defaults', {})}
        defaults['max_tokens'] = min(defaults['max_tokens'], config['context_tokens'] - 1)
        defaults['reasoning_budget_tokens'] = min(defaults['reasoning_budget_tokens'], defaults['max_tokens'])
        self.machine(key, account=account, config=config, request_defaults=defaults, deployment=None, legacy_model=None, **changes)
        return self.public(key)

    def configure_defaults(self, key, changes):
        item = self.machine(key)
        changes = dict(changes)
        aliases = {'high': 'xhigh', 'max': 'xhigh', 'minimal': 'low'}
        if changes.get('reasoning_effort') == 'none':
            changes.update(thinking=False)
            changes.pop('reasoning_effort')
        elif changes.get('reasoning_effort') in aliases:
            changes['reasoning_effort'] = aliases[changes['reasoning_effort']]
        defaults = {**request_defaults(item['config']['context_tokens']), **item.get('request_defaults', {}), **changes}
        validate_defaults(defaults, item['config']['context_tokens'])
        self.machine(key, request_defaults=defaults)
        return self.public(key)

    def apply_defaults(self, key, payload, route):
        """Map official Qwen controls to native Strata without replacing explicit client choices."""
        item = self.machine(key)
        defaults = {**request_defaults(item['config']['context_tokens']), **item.get('request_defaults', {})}
        body = dict(payload)
        if route == 'completions':
            if 'max_tokens' not in body and 'max_completion_tokens' not in body:
                body['max_tokens'] = defaults['max_tokens']
            return body

        template = body.get('chat_template_kwargs')
        template = dict(template) if isinstance(template, dict) else {}
        official_enable = body.pop('enable_thinking', None)
        if 'enable_thinking' in payload:
            if type(official_enable) is not bool:
                raise ValueError('enable_thinking 必须是布尔值')
            template.setdefault('enable_thinking', official_enable)
        official_budget = body.pop('thinking_budget', None)
        if 'thinking_budget' in payload:
            if type(official_budget) is not int or official_budget < 0:
                raise ValueError('thinking_budget 必须是非负整数')
            if 'reasoning_effort' in payload or ('reasoning' in payload and isinstance(payload['reasoning'], dict) and 'effort' in payload['reasoning']):
                raise ValueError('reasoning_effort 与 thinking_budget 不能同时使用')
            body.setdefault('reasoning_budget_tokens', official_budget)
            if not any(name in body for name in ('reasoning_effort', 'reasoning')) and 'reasoning_effort' not in template:
                body['reasoning_effort'] = 'low' if official_budget <= 4096 else 'medium' if official_budget <= 16384 else 'xhigh'
        aliases = {'high': 'xhigh', 'max': 'xhigh', 'minimal': 'low'}
        if body.get('reasoning_effort') in aliases:
            body['reasoning_effort'] = aliases[body['reasoning_effort']]
        if template.get('reasoning_effort') in aliases:
            template['reasoning_effort'] = aliases[template['reasoning_effort']]
        if 'reasoning_effort' in body and body['reasoning_effort'] not in {'none', 'low', 'medium', 'xhigh'}:
            raise ValueError('reasoning_effort 必须是 none、low、medium 或 xhigh')
        if 'reasoning_effort' in template and template['reasoning_effort'] not in {'none', 'low', 'medium', 'xhigh'}:
            raise ValueError('reasoning_effort 必须是 none、low、medium 或 xhigh')
        preserve = body.pop('preserve_thinking', defaults['preserve_thinking'])
        if type(preserve) is not bool:
            raise ValueError('preserve_thinking 必须是布尔值')
        if not preserve:
            self.remove_historical_reasoning(body)

        explicit = any(name in body for name in ('thinking', 'output_config', 'reasoning', 'reasoning_effort',
                                                'reasoning_budget_tokens', 'thinking_budget_tokens')) or \
            any(name in template for name in ('enable_thinking', 'reasoning_effort'))
        if route in ('messages', 'messages/count_tokens'):
            if route == 'messages':
                body.setdefault('max_tokens', defaults['max_tokens'])
            if not explicit:
                if not defaults['thinking']:
                    body['thinking'] = {'type': 'disabled'}
                elif defaults['thinking_mode'] == 'budget':
                    body['thinking'] = {'type': 'enabled', 'budget_tokens': defaults['reasoning_budget_tokens']}
                else:
                    body['thinking'] = {'type': 'adaptive'}
                    body['output_config'] = {'effort': defaults['reasoning_effort']}
            elif official_enable is False and 'thinking' not in body:
                body['thinking'] = {'type': 'disabled'}
            elif official_enable is True and 'thinking' not in body:
                body['thinking'] = {'type': 'enabled'}
            if 'thinking_budget' in payload and 'thinking' not in payload and official_enable is not False:
                body['thinking'] = {'type': 'enabled', 'budget_tokens': official_budget}
            if 'reasoning_effort' in body and 'output_config' not in body:
                effort = body.pop('reasoning_effort')
                if effort == 'none':
                    body.setdefault('thinking', {'type': 'disabled'})
                else:
                    body['output_config'] = {'effort': effort}
            if template and route.startswith('messages'):
                # Anthropic normalization does not read chat_template_kwargs.
                if template.get('enable_thinking') is False:
                    body['thinking'] = {'type': 'disabled'}
                elif 'reasoning_effort' in template and 'output_config' not in body:
                    body['output_config'] = {'effort': template['reasoning_effort']}
            if body.get('tools') and 'tool_choice' not in body:
                body['tool_choice'] = {'type': 'auto' if defaults['tools_enabled'] else 'none',
                                       'disable_parallel_tool_use': not defaults['parallel_tool_calls']}
        else:
            if route == 'responses':
                body.setdefault('max_output_tokens', defaults['max_tokens'])
            elif 'max_tokens' not in body and 'max_completion_tokens' not in body:
                body['max_tokens'] = defaults['max_tokens']
            if not explicit:
                if defaults['thinking'] and defaults['thinking_mode'] == 'budget':
                    budget = defaults['reasoning_budget_tokens']
                    body['reasoning_budget_tokens'] = budget
                    body['reasoning_effort'] = 'low' if budget <= 4096 else 'medium' if budget <= 16384 else 'xhigh'
                elif route == 'responses':
                    body['reasoning'] = {'effort': defaults['reasoning_effort'] if defaults['thinking'] else 'none'}
                else:
                    body['reasoning_effort'] = defaults['reasoning_effort'] if defaults['thinking'] else 'none'
            if template:
                if route == 'responses':
                    if 'reasoning' not in body:
                        if template.get('enable_thinking') is False:
                            body['reasoning'] = {'effort': 'none'}
                        elif 'reasoning_effort' in template:
                            body['reasoning'] = {'effort': template['reasoning_effort']}
                else:
                    body['chat_template_kwargs'] = template
            if route == 'responses' and 'reasoning_effort' in body:
                body.setdefault('reasoning', {'effort': body.pop('reasoning_effort')})
            if body.get('tools'):
                body.setdefault('tool_choice', 'auto' if defaults['tools_enabled'] else 'none')
                body.setdefault('parallel_tool_calls', defaults['parallel_tool_calls'])
        return body

    @staticmethod
    def remove_historical_reasoning(body):
        """Remove assistant thinking, preserving user text and every tool call/result envelope."""
        def clean_message(message):
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                return message
            message = {name: value for name, value in message.items() if name not in ('reasoning_content', 'reasoning')}
            content = message.get('content')
            if isinstance(content, str):
                message['content'] = re.sub(r'^\s*<think>.*?</think>\s*', '', content, count=1, flags=re.DOTALL)
            elif isinstance(content, list):
                message['content'] = [block for block in content if not isinstance(block, dict) or
                                      block.get('type') not in ('thinking', 'redacted_thinking', 'reasoning')]
            return message
        if isinstance(body.get('messages'), list):
            body['messages'] = [clean_message(message) for message in body['messages']]
        if isinstance(body.get('input'), list):
            body['input'] = [clean_message(message) for message in body['input']
                             if not isinstance(message, dict) or message.get('type') != 'reasoning']

    def delete(self, key):
        item = self.machine(key)
        if item.get("session") or item["phase"] in BUSY or self.active[key] or key in self.stopping:
            raise ValueError("请先关闭此实例并确认机器已释放")
        self.store.delete(TABLE, key)
        self.store.delete("llm_logs", key)
        self.telemetry.cache.pop(key, None)
        self.locks.pop(key)
        self.active.pop(key)
        self.heartbeat_failures.pop(key, None)
        self.reconnect_attempts.pop(key, None)
        self.assignment_ping_attempts.pop(key, None)
        self.tasks.pop(key, None)
        self.stops.pop(key, None)
        return {"deleted": key}

    def request_start(self, key):
        item = self.machine(key)
        profile = self.profiles.get(item["account"])
        if self.closing or key in self.stopping:
            raise ValueError("服务正在停止")
        if not profile or not profile["installed"] or not profile["credential_present"]:
            raise ValueError("请先配置此账号的 CLI 入口与登录")
        validate_config(item["config"])
        if item["phase"] == "ready" and item.get("port"):
            return self.public(key)
        if hasattr(self.colab, 'validate_connection'):
            self.colab.validate_connection(self.store.get('settings', 'main')['identity'])
        if self.bundles:
            self.bundles.require([preset(item['config']['preset_id'])['bundle_id']])
        if key not in self.tasks or self.tasks[key].done():
            self.heartbeat_failures.pop(key, None)
            self.reconnect_attempts.pop(key, None)
            self.machine(key, phase="checking", message="核对此实例的云端会话",
                         deployment=item.get('deployment') if item.get('session') else None,
                         loading_started_at=item.get('loading_started_at', time.time()) if item.get('session') else time.time())
            self.tasks[key] = asyncio.create_task(self.start(key))
        return self.public(key)

    def bootstrap(self, key, cloud=None):
        recipe = preset(self.machine(key)["config"]["preset_id"])
        config = {**self.machine(key)["config"], "recipe": recipe, 'cloud': cloud, 'lifecycle': dict(LIFECYCLE)}
        files = {"deploy.py": (ROOT / "backend/deploy_llm_remote.py").read_bytes(),
                 "telemetry.py": (ROOT / "backend/telemetry_remote.py").read_bytes(),
                 "config.json": json.dumps(config).encode(),
                 'llm_serve_remote.py': (ROOT / 'backend/llm_serve_remote.py').read_bytes(),
                 'bundle_restore_remote.py': (ROOT / 'backend/bundle_restore_remote.py').read_bytes()}
        code = "import base64,subprocess,sys\nfrom pathlib import Path\n" + f"root=Path({LLM_ROOT!r});root.mkdir(exist_ok=True,parents=True)\n"
        for name, data in files.items():
            code += f"(root/{name!r}).write_bytes(base64.b64decode({base64.b64encode(data).decode()!r}))\n"
        code += ("(root/'status.json').unlink(missing_ok=True)\n"
                 "with (root/'telemetry.log').open('ab') as log:\n"
                 " subprocess.Popen([sys.executable,str(root/'telemetry.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)\n"
                 "with (root/'deploy.log').open('ab') as log:\n"
                 " subprocess.Popen([sys.executable,str(root/'deploy.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)\n"
                 "print('LLM deployment started')\n")
        path = self.store.directory / f"{key}-llm-bootstrap.py"
        path.write_text(code)
        return path

    async def models(self, key):
        port = self.machine(key).get("port")
        expected_model = self.model_name(key)
        if not port:
            raise RuntimeError("服务未连接")
        async with self.http.get(f"http://127.0.0.1:{port}/v1/models", timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status()
            body = await response.json()
        if not any(m.get("id") == expected_model for m in body.get("data", [])):
            raise RuntimeError("服务模型未就绪")
        return body

    async def start(self, key):
        async with self.locks[key]:
            try:
                item = self.machine(key)
                account, session = item["account"], item.get("session")
                if session and not self.owned(key, session):
                    raise RuntimeError("会话归属不符，拒绝连接")
                sessions = await self.colab.sessions(account)
                if key in self.stopping:
                    return
                if not any(s["name"] == session for s in sessions):
                    await self.colab.close_tunnel(key)
                    install = self.store.get("meta", "install")["id"]
                    session = f"wbllm-{install}-{account.removeprefix('colab')}-{key.removeprefix('svc-')}-{uuid.uuid4().hex[:6]}"
                    self.machine(key, session=session, phase="provisioning", port=None, telemetry_port=None,
                                 legacy_session=None, legacy_model=None, deployment=None,
                                 message="申请此实例的 G4 机器", last_activity=time.time())
                    self.log(key, "申请独立 G4 服务机器")
                    try:
                        await self.colab.create(account, session)
                        self.assignment_ping_attempts.pop(key, None)
                    except Exception as exc:
                        # A CLI failure can be ambiguous. Clear ownership only after a successful absence check.
                        try:
                            remaining = await self.colab.sessions(account)
                            if not any(s["name"] == session for s in remaining):
                                self.machine(key, session=None)
                        except Exception:
                            pass
                        raise RuntimeError("申请机器失败，可更换账号重试：" + str(exc)) from exc
                if key in self.stopping:
                    return
                self.machine(key, phase="deploying", message="准备模型服务")
                cloud = self.bundles.lease(key, [preset(item['config']['preset_id'])['bundle_id']]) if self.bundles else None
                port = await self.colab.tunnel(account, session, self.store.get("settings", "main")["identity"],
                                               service_port=8000, tunnel_key=key, **({'reverse_port': cloud['local_port']} if cloud else {}))
                self.machine(key, port=port, telemetry_port=self.colab.telemetry_ports.get(key))
                try:
                    await self.models(key)
                    self.machine(key, phase="ready", message="服务已重新连接", last_activity=time.time())
                    if self.bundles:
                        self.bundles.revoke(key)
                    return
                except (aiohttp.ClientError, TimeoutError, RuntimeError, OSError):
                    pass
                self.machine(key, legacy_model=None)
                self.log(key, await self.colab.execute(account, session, self.bootstrap(key, cloud)))
                check = self.store.directory / f"{key}-llm-check.py"
                check.write_text("from pathlib import Path\n" + f"root=Path({LLM_ROOT!r})\n"
                                 "p=root/'status.json'\nprint('LLM_STATUS:'+p.read_text() if p.exists() else 'LLM_STATUS:{}')\n"
                                 "for name in ('deploy.log','server.log'):\n"
                                 " p=root/name\n"
                                 " if p.exists():\n"
                                 "  with p.open('rb') as f:\n"
                                 "   f.seek(max(0,p.stat().st_size-1800));print(f.read().decode(errors='replace'))\n")
                deadline, checked, read_failures = time.monotonic() + 3600, 0, 0
                while time.monotonic() < deadline:
                    if key in self.stopping:
                        return
                    try:
                        await self.models(key)
                        self.machine(key, phase="ready", message="服务已就绪", last_activity=time.time())
                        self.log(key, "本地端点已连接")
                        if self.bundles:
                            self.bundles.revoke(key)
                        return
                    except (aiohttp.ClientError, TimeoutError, RuntimeError, OSError):
                        pass
                    deployment = await self.telemetry.deployment(key, 'llm')
                    if deployment is not None:
                        if deployment.get('phase') == 'error':
                            raise RuntimeError(deployment.get('message', '部署失败'))
                        self.machine(key, message=deployment.get('message', '准备服务'), deployment=deployment)
                        read_failures = 0
                    elif time.monotonic() - checked > 3:
                        try:
                            output = await self.colab.execute(account, session, check)
                        except Exception as exc:
                            if not transient_status_error(exc):
                                raise
                            read_failures += 1
                            detail = error_message(exc)
                            self.log(key, f'状态读取暂时中断，继续检查原实例端点（{read_failures}/{STATUS_READ_FAILURES}）：{detail}')
                            if read_failures >= STATUS_READ_FAILURES:
                                # The control command can time out even after the model becomes healthy.
                                try:
                                    await self.models(key)
                                    self.machine(key, phase='ready', message='服务已就绪', last_activity=time.time())
                                    if self.bundles:
                                        self.bundles.revoke(key)
                                    return
                                except (aiohttp.ClientError, TimeoutError, RuntimeError, OSError):
                                    raise RuntimeError(f'连续 {read_failures} 次无法读取部署状态，原机器仍保留；可重新连接：{detail}') from exc
                            self.machine(key, message='状态连接暂时中断，继续检查原实例端点')
                            checked = time.monotonic()
                            await asyncio.sleep(1)
                            continue
                        read_failures = 0
                        self.log(key, output)
                        for line in output.splitlines():
                            if line.startswith("LLM_STATUS:"):
                                state = json.loads(line.removeprefix("LLM_STATUS:"))
                                if state.get("phase") == "error":
                                    raise RuntimeError(state.get("message", "部署失败"))
                                self.machine(key, message=state.get("message", "准备模型服务"), deployment=state)
                        checked = time.monotonic()
                    await asyncio.sleep(1)
                raise TimeoutError("服务部署超过 60 分钟，请查看日志或关闭机器")
            except Exception as exc:
                detail = error_message(exc)
                self.machine(key, phase="error", message=detail, last_activity=time.time())
                self.log(key, "启动失败：" + detail)

    def acquire(self, key):
        item = self.machine(key)
        if item["phase"] != "ready" or not item.get("port") or key in self.stopping or self.closing:
            raise ValueError("服务未就绪，请在工作台启动或重新连接")
        if self.active[key]:
            raise ValueError("此实例已有请求正在生成，请稍后重试")
        self.active[key] += 1
        self.machine(key, last_activity=time.time())
        return item

    def release(self, key):
        self.active[key] = max(0, self.active[key] - 1)
        self.machine(key, last_activity=time.time())

    def request_stop(self, key):
        item = self.machine(key)
        if self.active[key]:
            raise ValueError("服务正在生成，请等待请求结束后关闭")
        if key not in self.stops or self.stops[key].done():
            self.stopping.add(key)
            if item.get("session") or item["phase"] in BUSY:
                self.machine(key, phase="stopping", message="等待当前操作结束并释放此实例的机器")
            self.stops[key] = asyncio.create_task(self.stop(key))
        return self.public(key)

    async def stop(self, key):
        async with self.locks[key]:
            try:
                item = self.machine(key)
                if item.get("session"):
                    if not self.owned(key, item["session"]):
                        raise RuntimeError("此会话不属于本实例，拒绝停止")
                    self.machine(key, phase="stopping", message="释放此实例的机器")
                    await self.colab.stop(item["account"], item["session"])
                await self.colab.close_tunnel(key)
                if self.bundles:
                    self.bundles.revoke(key)
                self.machine(key, phase="stopped", session=None, port=None, telemetry_port=None, deployment=None,
                             legacy_session=None, legacy_model=None, heartbeat=None, assignment_keepalive=None, message="服务已关闭，机器已释放")
                self.heartbeat_failures.pop(key, None)
                self.reconnect_attempts.pop(key, None)
                self.assignment_ping_attempts.pop(key, None)
                self.log(key, "此实例的机器已释放")
                self.migrate_catalog()
            except Exception as exc:
                self.machine(key, phase="error", message="关机未确认：" + clean(str(exc)))
                self.log(key, str(exc))
            finally:
                self.stopping.discard(key)

    async def ping_assignment(self, key, session):
        item = self.machine(key)
        if self.closing or key in self.stopping or item.get('session') != session or not self.owned(key, session):
            return
        try:
            result = await self.colab.keepalive(item['account'], session)
            if self.closing or key in self.stopping or self.machine(key).get('session') != session:
                return
            self.machine(key, assignment_keepalive={'last_attempt_at': time.time(), 'status': result['status'],
                                                  'http_status': result.get('http_status'), 'error': None})
        except Exception as exc:
            if not self.closing and key not in self.stopping and self.machine(key).get('session') == session:
                previous = self.machine(key).get('assignment_keepalive') or {}
                self.machine(key, assignment_keepalive={**previous, 'last_attempt_at': time.time(), 'status': 'error', 'error': error_message(exc)})
                if previous.get('status') != 'error':
                    self.log(key, 'Colab 会话活动保活暂时失败，原实例保留，稍后重试：' + error_message(exc))

    async def assignment_sweep(self):
        if self.closing:
            return
        tasks = []
        for item in self.all():
            key, session = item['id'], item.get('session')
            if not session or key in self.stopping or item['phase'] == 'stopped' or not self.owned(key, session):
                continue
            previous = self.assignment_ping_attempts.get(key)
            if previous is None or previous[0] != session or time.monotonic() - previous[1] >= LIFECYCLE['assignment_ping_seconds']:
                self.assignment_ping_attempts[key] = (session, time.monotonic())
                tasks.append(self.ping_assignment(key, session))
        # A slow auth/HTTP call for one account must not delay sibling pings.
        await asyncio.gather(*tasks)

    async def maintain_assignments(self):
        while True:
            await self.assignment_sweep()
            await asyncio.sleep(5)

    async def idle_sweep(self):
        """LLM is persistent: probe its service without idle release or inference requests."""
        if self.closing:
            return
        for item in self.all():
            key = item["id"]
            if key not in self.locks or key in self.stopping or not item.get('session') or not self.owned(key, item['session']):
                continue
            if self.active[key] or item.get('retired') or item["phase"] not in {"ready", "disconnected"}:
                continue
            if item["phase"] == "disconnected":
                previous = self.reconnect_attempts.get(key)
                if previous is None or time.monotonic() - previous >= LIFECYCLE['reconnect_seconds']:
                    self.reconnect_attempts[key] = time.monotonic()
                    await self.reconnect_existing(key)
            else:
                try:
                    await self.models(key)
                    if self.machine(key).get('session') == item['session'] and self.machine(key)['phase'] == 'ready' and key not in self.stopping:
                        self.heartbeat_failures[key] = 0
                        self.machine(key, heartbeat={'last_success_at': time.time(), 'failures': 0, 'error': None})
                except (aiohttp.ClientError, TimeoutError, RuntimeError, OSError) as exc:
                    if self.machine(key).get('session') != item['session'] or self.machine(key)['phase'] != 'ready' or key in self.stopping:
                        continue
                    failures = self.heartbeat_failures.get(key, 0) + 1
                    self.heartbeat_failures[key] = failures
                    self.machine(key, heartbeat={**(item.get('heartbeat') or {}), 'failures': failures, 'error': error_message(exc)})
                    if failures >= LIFECYCLE['heartbeat_failures']:
                        self.machine(key, phase="disconnected", message="服务连接中断，保留原机器并尝试重连")
                        self.log(key, '连续健康检查失败，保留原会话，等待恢复连接')

    async def reconnect_existing(self, key):
        """Reconnect only an existing owned session; never allocate or redeploy here."""
        async with self.locks[key]:
            item = self.machine(key)
            session = item.get('session')
            if self.closing or key in self.stopping or self.active[key] or item['phase'] != 'disconnected' or not session or not self.owned(key, session):
                return
            try:
                sessions = await self.colab.sessions(item['account'])
                if self.closing or key in self.stopping:
                    return
                if not any(s['name'] == session for s in sessions):
                    await self.colab.close_tunnel(key)
                    self.machine(key, phase='stopped', session=None, port=None, telemetry_port=None, deployment=None,
                                 heartbeat=None, message='云端会话已结束，需要时请手动启动')
                    self.log(key, '已确认原会话不存在；未自动申请新机器')
                    return
                port = await self.colab.tunnel(item['account'], session, self.store.get('settings', 'main')['identity'],
                                               service_port=8000, tunnel_key=key)
                self.machine(key, port=port, telemetry_port=self.colab.telemetry_ports.get(key))
                await self.models(key)
                if self.closing or key in self.stopping:
                    return
                self.heartbeat_failures[key] = 0
                self.machine(key, phase='ready', message='原实例已重新连接',
                             heartbeat={'last_success_at': time.time(), 'failures': 0, 'error': None})
                self.log(key, '已恢复原会话的服务连接，未重新加载模型')
            except (aiohttp.ClientError, TimeoutError, RuntimeError, OSError, ValueError) as exc:
                if not self.closing and key not in self.stopping:
                    self.machine(key, heartbeat={**(item.get('heartbeat') or {}), 'error': error_message(exc)},
                                 message='原机器仍保留，稍后重试连接；也可手动重新连接')
                    self.log(key, '原会话重连暂未成功：' + error_message(exc))

    async def maintain(self):
        while True:
            await asyncio.sleep(LIFECYCLE['heartbeat_seconds'])
            await self.idle_sweep()

    async def close(self):
        self.closing = True
        if self.assignment_ping_task:
            self.assignment_ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.assignment_ping_task
        if self.maintenance:
            self.maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.maintenance
        for key, task in self.tasks.items():
            if not task.done():
                self.stopping.add(key)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await asyncio.gather(*self.stops.values(), return_exceptions=True)
        # Remote deployment/model processes use start_new_session; closing local tunnels does not kill them.
        for item in self.all():
            if item.get('session'):
                self.machine(item['id'], phase='disconnected', port=None, telemetry_port=None,
                             message='本地连接已关闭，云端实例保留')
        await self.colab.close()
        if self.http:
            await self.http.close()

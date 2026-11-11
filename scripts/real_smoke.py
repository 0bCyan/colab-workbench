"""Operator-invoked real integration test. Provisions the chosen account and releases it.

Requires the local workbench server to be running. Never used by pytest.
"""
import argparse
import asyncio
import json
import time

import aiohttp

from backend.config import DATA


async def main(account):
    async with aiohttp.ClientSession() as http:
        base = "http://127.0.0.1:8765/api"
        async with http.get(base + "/state") as response:
            state = await response.json()
        profile = next((p for p in state['accounts'] if p['id'] == account), None)
        if not profile or not profile['credential_present']:
            raise RuntimeError('Choose a configured account from the local workbench')
        if profile['machine'].get('session') or any(
            job['account'] == account and job['status'] not in {'completed', 'failed', 'cancelled'}
            for job in state['jobs']
        ):
            raise RuntimeError('Real smoke tests require an idle account without a workbench machine')
        headers = {"x-workbench-token": state["token"]}
        async def call(path, body=None):
            async with http.request("GET" if body is None else "POST", base + path, json=body, headers=headers) as response:
                data = await response.json()
                if response.status >= 400:
                    raise RuntimeError(str(data))
                return data
        draft = await call('/template')
        job = await call('/jobs', {**draft, 'account': account, 'name': '工作台实机验收 · H3 INT8 20 步', 'stop_after': True})
        started = time.time()
        last = ''
        result = None
        try:
            while time.time() - started < 4500:
                state = await call('/state')
                machine = next(p for p in state['accounts'] if p['id'] == account)['machine']
                job = await call('/jobs/' + job['id'])
                message = f"{machine['phase']}: {machine['message']} | {job['status']}: {job['message']}"
                if message != last:
                    print(message, flush=True)
                    last = message
                if job['status'] in {'failed', 'cancelled', 'recovering'}:
                    raise RuntimeError(message)
                if job['status'] == 'completed' and machine['phase'] == 'stopped':
                    result = {'success': True, 'job': job, 'machine': machine, 'total_seconds': time.time() - started}
                    break
                await asyncio.sleep(5)
            if not result:
                raise TimeoutError('Integration test timeout')
        finally:
            state = await call('/state')
            machine = next(p for p in state['accounts'] if p['id'] == account)['machine']
            if machine.get('session'):
                if job['status'] not in {'completed', 'failed', 'cancelled'}:
                    await call('/jobs/' + job['id'] + '/cancel', {})
                await call(f'/accounts/{account}/stop', {})
                for _ in range(90):
                    state = await call('/state')
                    machine = next(p for p in state['accounts'] if p['id'] == account)['machine']
                    if machine['phase'] == 'stopped':
                        break
                    await asyncio.sleep(2)
            if machine.get('session'):
                raise RuntimeError('IMPORTANT: shutdown not verified; session ' + machine['session'])
        if result:
            (DATA / 'real-smoke-result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
            print('SUCCESS: generated, downloaded, and released the test machine', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run a paid GPU smoke test on an idle local account')
    parser.add_argument('--account', required=True, help='Account launcher ID from this workbench')
    asyncio.run(main(parser.parse_args().account))

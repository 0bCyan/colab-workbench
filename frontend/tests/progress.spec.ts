import { test, expect } from '@playwright/test'

for (const kind of ['media', 'llm'] as const) {
  test(`${kind} loading and performance render the same metrics response despite an older state poll`, async ({ page }) => {
    const original = await (await page.request.get('/api/state')).json()
    const key = kind === 'media' ? 'colab2' : 'svc-shared-progress'
    const started = Date.now() / 1000 - 20
    const session = 'synthetic-shared-progress'
    let tick = 1
    const machine = () => ({ phase: 'deploying', session, message: '并行准备', updated_at: started + tick + 1,
      deployment: { phase: 'downloading', started_at: started, time: started + tick,
        stages: { environment: { status: 'running', message: '安装依赖' }, download: { status: 'running', bytes: tick * 2 ** 30, total_bytes: 8 * 2 ** 30, rate_bytes_s: 256 * 2 ** 20, files_done: tick, files_total: 4 } } } })
    await page.addInitScript(() => localStorage.setItem('fieldwork-account', 'colab2'))
    await page.route('**/api/state', async route => {
      const state = structuredClone(original)
      if (kind === 'media') state.accounts = state.accounts.map((a: any) => a.id === key ? { ...a, machine: { ...machine(), deployment: null, updated_at: started } } : a)
      else state.llm.services = [{ ...machine(), deployment: null, updated_at: started, id: key, name: '共享进度', account: 'colab2', config: { preset_id: state.llm.presets[0].id, context_tokens: 262144, gpu_memory: .9, cpu_threads: 12, cache_enabled: true, cache_mib: 4096, download_workers: 64 }, endpoint_path: `/llm/${key}/v1`, model_name: 'synthetic-model', active_requests: 0 }]
      await route.fulfill({ json: state })
    })
    await page.route('**/api/**/metrics', async route => route.fulfill({ json: { account: key, session, status: 'live', machine: machine(), sample: {
      sampled_at: Date.now() / 1000, cpu: { percent: tick * 10, cores: 48 },
      memory: { used: tick * 2 ** 30, total: 177 * 2 ** 30, percent: tick }, disk: { used: 1, total: 100, percent: 1 },
      gpu: { name: 'Synthetic GPU', percent: tick * 10, memory_used: 10 * 2 ** 30, memory_total: 96 * 2 ** 30, memory_percent: 10, temperature: 45, power: 80, power_limit: 600 }, network: { rx_bytes_s: 256 * 2 ** 20, tx_bytes_s: 0 }
    }, history: [] } }))
    await page.goto('/')
    await expect(page.getByRole('heading', { name: '创作工作台', level: 1 })).toBeVisible()
    if (kind === 'llm') await page.getByRole('button', { name: '模型服务', exact: true }).click()
    const panel = page.getByLabel(kind === 'media' ? '创作加载进度' : '服务加载进度', { exact: true })
    await expect(panel).toContainText('1.0 GiB / 8.0 GiB')
    await expect(page.getByLabel('GPU占用率')).toContainText('10%')
    tick = 4
    await expect(panel).toContainText('4.0 GiB / 8.0 GiB', { timeout: 3000 })
    await expect(page.getByLabel('GPU占用率')).toContainText('40%')
    await expect(panel.getByLabel('权重下载进度')).toHaveAttribute('value', String(4 * 2 ** 30))
    await expect(panel).toContainText('50.0%')
    await page.waitForResponse(r => r.url().endsWith('/api/state'))
    await expect(panel).toContainText('4.0 GiB / 8.0 GiB')
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(720)
    await page.screenshot({ path: `../screenshots/review/${kind}-shared-progress.png` })
  })
}

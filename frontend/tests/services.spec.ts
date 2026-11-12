import { expect, test } from '@playwright/test'

test.afterEach(async ({ page }) => {
  const state = await (await page.request.get('/api/state')).json()
  for (const service of state.llm.services) {
    if (service.session || ['checking', 'provisioning', 'deploying', 'stopping'].includes(service.phase)) {
      await page.request.post(`/api/llm/${service.id}/stop`, { headers: { 'X-Workbench-Token': state.token } })
      await expect.poll(async () => (await (await page.request.get('/api/state')).json()).llm.services.find((s: { id: string }) => s.id === service.id).phase).toBe('stopped')
    }
    await page.request.delete(`/api/llm/${service.id}`, { headers: { 'X-Workbench-Token': state.token } })
  }
})

test('models choose their runtime and multiple instances on one account keep separate endpoints', async ({ page, context }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/')
  await page.getByRole('button', { name: '模型服务', exact: true }).click()
  await expect(page.getByRole('heading', { name: '模型服务', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '视频生成', exact: true })).toHaveCount(0)
  await expect(page.getByLabel('部署引擎')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '复制端点', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: /Flash-Next Uncensored · Strata MTP/ }).click()
  await expect(page.getByRole('heading', { name: 'Flash-Next Uncensored · Strata MTP', exact: true })).toBeVisible()
  await expect(page.getByLabel('CPU 并行线程')).toBeVisible()
  await expect(page.locator('.service-config-footer')).toContainText('Strata')
  await expect(page.locator('.endpoint-details')).toContainText('orca-flash-next-nvfp4')
  await page.getByLabel('服务账号', { exact: true }).selectOption('colab1')
  await expect(page.getByLabel('服务实例', { exact: true }).locator('option:checked')).toHaveText('新建实例…')
  await page.getByLabel('实例名称', { exact: true }).fill('日常助手')
  // This account already has an unrelated VM; creating an instance must remain available.
  await page.getByRole('button', { name: '创建实例', exact: true }).click()
  await expect(page.getByLabel('服务实例', { exact: true })).toHaveValue(/^svc-/)
  const first = await page.getByLabel('服务实例', { exact: true }).inputValue()
  expect(first).toMatch(/^svc-/)
  let state = await (await page.request.get('/api/state')).json()
  expect(state.llm.services).toHaveLength(1)
  expect(state.llm.services[0].session).toBeNull()
  expect(state.llm.services[0].name).toBe('日常助手')
  await page.getByRole('button', { name: /Flash-Next Uncensored · Strata MTP/ }).click()
  await expect(page.getByRole('heading', { name: 'Flash-Next Uncensored · Strata MTP', exact: true })).toBeVisible()
  await expect(page.getByLabel('CPU 并行线程')).toBeVisible()
  await expect(page.locator('.service-config-footer')).toContainText('Strata')
  await expect(page.getByLabel('总上下文')).toHaveValue('262144')
  await expect(page.getByLabel('CPU 并行线程')).toHaveValue('12')
  await page.getByRole('button', { name: '启动服务', exact: true }).click()
  await expect(page.getByText('服务已启动', { exact: true })).toBeVisible({ timeout: 15000 })
  await expect(page.getByLabel('总上下文')).toBeDisabled()
  await expect(page.getByLabel('服务账号', { exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '删除实例', exact: true })).toBeDisabled()
  await expect(page.getByLabel('GPU占用率')).toContainText('85%')
  await page.getByLabel('实例名称', { exact: true }).fill('长文分析')
  await page.getByRole('button', { name: '保存实例名称', exact: true }).click()
  await expect(page.getByLabel('服务实例', { exact: true }).locator('option:checked')).toContainText('长文分析')
  await expect(page.getByRole('heading', { name: 'Flash-Next Uncensored · Strata MTP', exact: true })).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: '模型服务', exact: true }).click()
  await expect(page.getByLabel('实例名称', { exact: true })).toHaveValue('长文分析')
  await page.getByRole('button', { name: '复制端点', exact: true }).click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(`http://127.0.0.1:8776/llm/${first}/v1`)
  const endpoint = await page.evaluate(() => navigator.clipboard.readText())
  expect(endpoint).toBe(`http://127.0.0.1:8776/llm/${first}/v1`)
  await page.getByRole('button', { name: '复制服务密钥', exact: true }).click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toMatch(/^[A-Za-z0-9_-]{43}$/)
  const key = await page.evaluate(() => navigator.clipboard.readText())
  expect(key.length).toBeGreaterThan(30)
  expect(await page.locator('body').innerText()).not.toContain(key)
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(key)
  const outputLimit = page.getByLabel('最大输出', { exact: true })
  await expect(outputLimit).toBeEnabled()
  for (const invalid of ['', '0', '1.5', '262144']) {
    await outputLimit.fill(invalid)
    await expect(outputLimit).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByRole('button', { name: '发送测试', exact: true })).toBeDisabled()
  }
  await outputLimit.fill('8192')
  await expect(outputLimit).toHaveAttribute('aria-invalid', 'false')
  const outgoing = page.waitForRequest(request => request.url() === `${endpoint}/chat/completions` && request.method() === 'POST')
  await page.getByRole('button', { name: '发送测试', exact: true }).click()
  expect((await outgoing).postDataJSON().max_tokens).toBe(8192)
  await expect(page.getByLabel('测试响应')).toContainText(`来自 ${first} 的测试服务。`)
  await expect(page.getByRole('button', { name: '发送测试', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '调用示例', exact: true }).click()
  const firstModel = 'orca-flash-next-nvfp4'
  await expect(page.locator('.service-example')).toContainText(firstModel)
  await expect(page.locator('.service-example')).toContainText('"max_tokens":8192')
  await outputLimit.fill('16384')
  await expect(page.locator('.service-example')).toContainText('"max_tokens":16384')
  await expect(page.locator('.service-example')).toContainText(`${endpoint}/chat/completions`)
  await page.getByRole('button', { name: '新建实例', exact: true }).click()
  await page.getByLabel('服务账号', { exact: true }).selectOption('colab1')
  await page.getByRole('button', { name: /Flash-Next Uncensored · Strata MTP/ }).click()
  await page.getByRole('button', { name: '创建并启动', exact: true }).click()
  await expect(page.getByText('服务已启动', { exact: true })).toBeVisible({ timeout: 15000 })
  const second = await page.getByLabel('服务实例', { exact: true }).inputValue()
  expect(second).not.toBe(first)
  await expect(page.locator('.endpoint-value')).toContainText(`/llm/${second}/v1`)
  await expect(page.getByText('当前账号运行 2 个服务实例', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '刷新服务账号', exact: true }).click()
  state = await (await page.request.get('/api/state')).json()
  expect(state.llm.services).toHaveLength(2)
  expect(state.accounts.find((a: { id: string }) => a.id === 'colab1').sessions).toHaveLength(3)
  const response = await page.request.post(`${endpoint}/chat/completions`, {
    headers: { Authorization: `Bearer ${key}` }, data: { model: firstModel, messages: [] },
  })
  expect(response.status()).toBe(200)
  expect((await response.json()).choices[0].message.content).toBe(first)
  await page.screenshot({ path: '../screenshots/review/service-instances-desktop.png' })
  await page.getByRole('button', { name: '关闭服务并释放机器', exact: true }).click()
  await expect(page.getByText('服务已关闭，机器已释放', { exact: true })).toBeVisible()
  expect((await page.request.get(`${endpoint}/models`, { headers: { Authorization: `Bearer ${key}` } })).status()).toBe(200)
  await page.getByRole('button', { name: '删除实例', exact: true }).click()
  await page.getByRole('dialog', { name: '删除服务实例', exact: true }).getByRole('button', { name: '删除实例', exact: true }).click()
  await expect(page.getByLabel('服务实例', { exact: true }).locator('option')).toHaveCount(2)
  await page.getByLabel('服务实例', { exact: true }).selectOption(first)
  await expect(outputLimit).toHaveValue('16384')
  await expect(page.getByText('服务已启动', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '服务日志', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('本地端点已连接')
  await page.getByLabel('关闭弹窗').click()
  await page.getByRole('button', { name: '创作工作台', exact: true }).click()
  await expect(page.getByRole('button', { name: /^生成视频/ })).toBeEnabled()
  expect(errors).toEqual([])
})

test('instance layout fits desktop and small screens, with zero or many accounts and instances', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }, { width: 1024, height: 600 }, { width: 339, height: 640 }]) {
    await page.setViewportSize(viewport)
    await page.goto('/')
    await page.getByRole('button', { name: '模型服务', exact: true }).click()
    await expect(page.getByRole('button', { name: '新建实例', exact: true })).toBeInViewport()
    if (viewport.width < 1001) {
      await page.getByRole('button', { name: '部署配置', exact: true }).click()
      await page.getByLabel('总上下文').scrollIntoViewIfNeeded()
      await expect(page.getByLabel('总上下文')).toBeInViewport()
      await page.getByRole('button', { name: '算力与性能', exact: true }).click()
    }
    await expect(page.getByLabel('性能看板', { exact: true })).toBeInViewport()
    const scroll = await page.getByLabel('性能看板', { exact: true }).evaluate(el => ({ height: el.clientHeight, scroll: el.scrollHeight }))
    expect(scroll.scroll).toBeLessThanOrEqual(scroll.height + 2)
    if (viewport.width < 1001) await page.getByRole('button', { name: '端点与测试', exact: true }).click()
    await expect(page.getByRole('button', { name: '复制端点', exact: true })).toBeInViewport()
    await expect(page.getByRole('button', { name: '发送测试', exact: true })).toBeInViewport()
    if (viewport.width < 1001) await page.getByRole('button', { name: '部署配置', exact: true }).click()
    await page.getByLabel('最大输出', { exact: true }).scrollIntoViewIfNeeded()
    await expect(page.getByLabel('最大输出', { exact: true })).toBeInViewport()
    expect(await page.evaluate(() => ({ width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }))).toEqual(viewport)
    await page.screenshot({ path: `../screenshots/review/service-instances-${viewport.width}x${viewport.height}.png` })
  }
  let count = 0
  await page.route('**/api/state', async route => {
    const state = await (await route.fetch()).json()
    const sample = state.accounts[0]
    state.accounts = Array.from({ length: count }, (_, i) => ({ ...sample, id: `colab${i + 1}`, sessions: [], machine: { phase: 'stopped', session: null } }))
    state.llm.services = []
    if (count) state.llm.services = Array.from({ length: count }, (_, i) => ({
      id: `svc-demo${i + 1}`, account: count === 1 ? 'colab99' : 'colab12', phase: 'stopped', session: null, message: '按需启动服务',
      config: { preset_id: 'orca-flash-next-nvfp4-strata-mtp-g4-v1', context_tokens: 262144, gpu_memory: .9, cpu_threads: 12 },
      endpoint_path: `/llm/svc-demo${i + 1}/v1`, model_name: state.llm.presets[0].model_name, active_requests: 0,
    }))
    await route.fulfill({ json: state })
  })
  await page.route('**/api/accounts/*/refresh', route => route.fulfill({ json: {} }))
  for (count of [0, 1, 12]) {
    await page.goto('/')
    await page.getByRole('button', { name: '模型服务', exact: true }).click()
    await page.getByRole('button', { name: '算力与性能', exact: true }).click()
    const select = page.getByLabel('服务账号', { exact: true })
    if (!count) {
      await expect(select).toBeDisabled()
      await expect(page.getByRole('button', { name: '创建并启动', exact: true })).toBeDisabled()
    } else {
      await expect(select.locator('option')).toHaveCount(count === 1 ? 2 : count)
      if (count === 1) await expect(select).toHaveValue('colab99')
      await select.selectOption(`colab${count}`)
      await expect(select).toHaveValue(`colab${count}`)
      if (count === 12) {
        await page.getByLabel('服务实例', { exact: true }).selectOption('svc-demo12')
        await page.getByRole('button', { name: '端点与测试', exact: true }).click()
        await expect(page.locator('.endpoint-value')).toContainText('/llm/svc-demo12/v1')
      }
    }
  }
  expect(errors).toEqual([])
})

test('deployment parameters and live request defaults save independently and tool-only streams render', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '模型服务', exact: true }).click()
  await page.getByLabel('实例名称', { exact: true }).fill('Agent 服务')
  await page.getByLabel('总上下文', { exact: true }).fill('98304')
  await page.getByLabel('CPU 并行线程', { exact: true }).fill('24')
  await page.getByLabel('缓存容量', { exact: true }).fill('3072')
  await page.getByLabel('下载并发', { exact: true }).selectOption('96')
  await page.getByLabel('最大输出', { exact: true }).fill('8192')
  await page.getByRole('button', { name: '创建实例', exact: true }).click()
  await expect(page.getByLabel('服务实例', { exact: true })).toHaveValue(/^svc-/)
  const id = await page.getByLabel('服务实例', { exact: true }).inputValue()
  let state = await (await page.request.get('/api/state')).json()
  let service = state.llm.services.find((item: { id: string }) => item.id === id)
  expect(service.config).toMatchObject({ context_tokens: 98304, cpu_threads: 24, cache_enabled: true, cache_mib: 3072, download_workers: 96 })
  expect(service.request_defaults.max_tokens).toBe(8192)
  const hiddenDefaults = { ...service.request_defaults }; delete hiddenDefaults.max_tokens
  await page.getByRole('button', { name: '启动服务', exact: true }).click()
  await expect(page.getByText('服务已启动', { exact: true })).toBeVisible()
  await expect(page.getByLabel('总上下文', { exact: true })).toBeDisabled()
  await expect(page.getByLabel('最大输出', { exact: true })).toBeEnabled()
  const session = (await (await page.request.get('/api/state')).json()).llm.services.find((item: { id: string }) => item.id === id).session
  await page.getByLabel('最大输出', { exact: true }).fill('4096')
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.getByText('设置已保存', { exact: true })).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: '模型服务', exact: true }).click()
  await expect(page.getByLabel('最大输出', { exact: true })).toHaveValue('4096')
  for (const label of ['思考模式', '思考深度', '思考控制方式', '思考预算', '保留历史思考', '工具调用']) await expect(page.getByLabel(label, { exact: true })).toHaveCount(0)
  state = await (await page.request.get('/api/state')).json()
  service = state.llm.services.find((item: { id: string }) => item.id === id)
  expect(service.session).toBe(session)
  expect(service.request_defaults).toEqual({ ...hiddenDefaults, max_tokens: 4096 })
  await expect(page.locator('.service-test .stage-heading').getByLabel('最大输出', { exact: true })).toHaveCount(0)
  await page.route(`**/llm/${id}/v1/chat/completions`, route => route.fulfill({
    contentType: 'text/event-stream',
    body: [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'call-weather', type: 'function', function: { name: 'weather', arguments: '{"city":' } }] } }] },
      { choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: '"上海"}' } }] }, finish_reason: 'tool_calls' }] },
    ].map(value => `data: ${JSON.stringify(value)}\n\n`).join('') + 'data: [DONE]\n\n',
  }))
  const outgoing = page.waitForRequest(request => request.url().endsWith(`/llm/${id}/v1/chat/completions`))
  await page.getByRole('button', { name: '发送测试', exact: true }).click()
  const payload = (await outgoing).postDataJSON()
  expect(payload).toEqual({ model: service.model_name, messages: [{ role: 'user', content: '用一句话介绍自己。' }], max_tokens: 4096, stream: true })
  await expect(page.getByLabel('测试响应')).toContainText('工具调用 · weather')
  await expect(page.getByLabel('测试响应')).toContainText('{"city":"上海"}')
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.screenshot({ path: '../screenshots/review/service-agent-controls.png' })
})

test('startup panel shows measured download and independent environment progress without invented percentages', async ({ page }) => {
  const state = await (await page.request.get('/api/state')).json()
  const response = await page.request.post('/api/llm/instances', {
    headers: { 'X-Workbench-Token': state.token },
    data: { name: '加载进度', account: 'colab2', config: { preset_id: state.llm.presets[0].id, context_tokens: 262144, gpu_memory: .9, cpu_threads: 12 } },
  })
  expect(response.status()).toBe(201)
  const id = (await response.json()).id
  let failed = false
  await page.route('**/api/state', async route => {
    const data = await (await route.fetch()).json()
    data.llm.services = data.llm.services.map((item: { id: string }) => item.id === id ? {
      ...item, phase: failed ? 'error' : 'deploying', message: failed ? '下载连接中断' : '下载与环境并行准备',
      deployment: { phase: failed ? 'error' : 'downloading', message: '并行准备', started_at: Date.now() / 1000 - 30, time: Date.now() / 1000,
        stages: { environment: { status: 'running', message: '安装 Python 依赖' }, download: { status: 'running', bytes: 32 * 2 ** 30, total_bytes: 128 * 2 ** 30, elapsed_seconds: 30, rate_bytes_s: 512 * 2 ** 20, files_done: 2, files_total: 7, current: 'pack/experts.bin' } } },
    } : item)
    await route.fulfill({ json: data })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '模型服务', exact: true }).click()
  const panel = page.getByLabel('服务加载进度', { exact: true })
  await expect(panel).toBeVisible()
  await expect(panel).toContainText('安装 Python 依赖')
  await expect(panel).toContainText('32.0 GiB / 128.0 GiB')
  await expect(panel).toContainText('25.0%')
  await expect(panel).toContainText('512 MiB/s')
  await expect(panel).toContainText('2 / 7 文件')
  await expect(panel).toContainText('experts.bin')
  await expect(panel.locator('progress')).toHaveCount(1)
  await expect(panel.getByLabel('权重下载进度')).toHaveAttribute('value', String(32 * 2 ** 30))
  await page.screenshot({ path: '../screenshots/review/service-loading-stages.png' })
  failed = true
  await page.reload()
  await page.getByRole('button', { name: '模型服务', exact: true }).click()
  await expect(panel).toContainText('加载中断')
  await expect(panel).toContainText('下载连接中断')
  await expect(panel.locator('.spin')).toHaveCount(0)
  await page.unroute('**/api/state')
})

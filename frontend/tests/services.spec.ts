import { expect, test } from '@playwright/test'

test.afterEach(async ({ page }) => {
  const state = await (await page.request.get('/api/state')).json()
  for (const service of state.llm.services) {
    if (service.session) {
      await page.request.post(`/api/llm/${service.account}/stop`, { headers: { 'X-Workbench-Token': state.token } })
      await expect.poll(async () => (await (await page.request.get('/api/state')).json()).llm.services.find((s: { account: string }) => s.account === service.account).phase).toBe('stopped')
    }
  }
})

test('service config, streaming endpoint and account-bound clients work independently', async ({ page, context }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/')
  await page.getByRole('button', { name: '模型服务', exact: true }).click()
  await expect(page.getByRole('heading', { name: '模型服务', exact: true })).toBeVisible()
  await page.getByLabel('服务账号', { exact: true }).selectOption('colab1')
  await expect(page.getByText('此账号另有 1 台外部会话', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '启动服务', exact: true })).toBeDisabled()
  await page.getByLabel('服务账号', { exact: true }).selectOption('colab2')
  const save = page.getByRole('button', { name: '保存配置', exact: true })
  await expect(save).toBeDisabled()
  await page.getByLabel('部署引擎').selectOption('llama.cpp')
  await page.getByRole('button', { name: /Qwen3.8 · Flash-Next/ }).click()
  await expect(page.getByLabel('CPU 线程')).toBeVisible()
  await page.getByLabel('总上下文').selectOption('131072')
  await save.click()
  await expect(save).toBeDisabled()
  await page.getByLabel('部署引擎').selectOption('vllm')
  await page.getByRole('button', { name: /Qwen3.8 · 27B RANA/ }).click()
  await page.getByLabel('显存预算').selectOption('0.8')
  await page.getByRole('button', { name: '启动服务', exact: true }).click()
  await expect(page.getByText('服务已启动', { exact: true })).toBeVisible({ timeout: 15000 })
  await expect(page.getByLabel('总上下文')).toBeDisabled()
  await expect(page.getByLabel('GPU占用率')).toContainText('85%')
  await page.getByRole('button', { name: '创作工作台', exact: true }).click()
  await expect(page.getByRole('button', { name: /^生成视频/ })).toBeDisabled()
  await page.getByRole('button', { name: '管理此账号的 LLM 服务', exact: true }).click()
  await page.getByRole('button', { name: '复制端点', exact: true }).click()
  const endpoint = await page.evaluate(() => navigator.clipboard.readText())
  expect(endpoint).toBe('http://127.0.0.1:8776/llm/colab2/v1')
  await page.getByRole('button', { name: '复制服务密钥', exact: true }).click()
  const key = await page.evaluate(() => navigator.clipboard.readText())
  expect(key.length).toBeGreaterThan(30)
  expect(await page.locator('body').innerText()).not.toContain(key)
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(key)
  await page.getByRole('button', { name: '发送测试', exact: true }).click()
  await expect(page.getByLabel('测试响应')).toContainText('来自 colab2 的测试服务。')
  await expect(page.getByRole('button', { name: '发送测试', exact: true })).toBeEnabled()
  await page.screenshot({ path: '../screenshots/review/services-desktop.png' })
  await page.getByRole('button', { name: '调用示例', exact: true }).click()
  await expect(page.locator('.service-example')).toContainText(`${endpoint}/chat/completions`)
  await page.getByLabel('服务账号', { exact: true }).selectOption('colab3')
  await expect(page.locator('.endpoint-value')).toContainText('/llm/colab3/v1')
  const response = await page.request.post(`${endpoint}/chat/completions`, {
    headers: { Authorization: `Bearer ${key}` }, data: { model: 'workbench-llm', messages: [] },
  })
  expect(response.status()).toBe(200)
  expect((await response.json()).choices[0].message.content).toBe('colab2')
  await page.getByLabel('服务账号', { exact: true }).selectOption('colab2')
  await expect(page.getByText('服务已启动', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '服务日志', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('本地端点已连接')
  await page.getByLabel('关闭弹窗').click()
  await page.getByRole('button', { name: '关闭服务并释放机器', exact: true }).click()
  await expect(page.getByText('服务已关闭，机器已释放', { exact: true })).toBeVisible()
  expect((await page.request.get(`${endpoint}/models`, { headers: { Authorization: `Bearer ${key}` } })).status()).toBe(503)
  expect(errors).toEqual([])
})

test('service layout fits desktop and small screens, zero or many accounts', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }, { width: 1024, height: 600 }, { width: 339, height: 640 }]) {
    await page.setViewportSize(viewport)
    await page.goto('/')
    await page.getByRole('button', { name: '模型服务', exact: true }).click()
    if (viewport.width < 1001) {
      await page.getByRole('button', { name: '部署配置', exact: true }).click()
      await expect(page.getByLabel('总上下文')).toBeInViewport()
      await page.getByRole('button', { name: '算力与性能', exact: true }).click()
    }
    await expect(page.getByLabel('性能看板', { exact: true })).toBeInViewport()
    const scroll = await page.getByLabel('性能看板', { exact: true }).evaluate(el => ({ height: el.clientHeight, scroll: el.scrollHeight }))
    expect(scroll.scroll).toBeLessThanOrEqual(scroll.height + 2)
    if (viewport.width < 1001) await page.getByRole('button', { name: '端点与测试', exact: true }).click()
    await expect(page.getByRole('button', { name: '复制端点', exact: true })).toBeInViewport()
    await expect(page.getByRole('button', { name: '发送测试', exact: true })).toBeInViewport()
    expect(await page.evaluate(() => ({ width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }))).toEqual(viewport)
    await page.screenshot({ path: `../screenshots/review/services-${viewport.width}x${viewport.height}.png` })
  }
  let count = 0
  await page.route('**/api/state', async route => {
    const state = await (await route.fetch()).json()
    const sample = state.accounts[0], service = state.llm.services[0]
    state.accounts = Array.from({ length: count }, (_, i) => ({ ...sample, id: `colab${i + 1}`, sessions: [], machine: { phase: 'stopped', session: null } }))
    state.llm.services = state.accounts.map((a: { id: string }) => ({ ...service, account: a.id, endpoint_path: `/llm/${a.id}/v1`, session: null, phase: 'stopped' }))
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
      await expect(page.getByRole('button', { name: '启动服务', exact: true })).toBeDisabled()
    } else {
      await expect(select.locator('option')).toHaveCount(count)
      await select.selectOption(`colab${count}`)
      await page.getByRole('button', { name: '端点与测试', exact: true }).click()
      await expect(page.locator('.endpoint-value')).toContainText(`/llm/colab${count}/v1`)
    }
  }
  expect(errors).toEqual([])
})

import { expect, test } from '@playwright/test'
import { createServer, loadConfigFromFile } from 'vite'
import { fileURLToPath } from 'node:url'

test('development proxy preserves browser origin for autosave and still rejects cross-origin writes', async ({ page }) => {
  const loaded = await loadConfigFromFile({ command: 'serve', mode: 'test' }, fileURLToPath(new URL('../vite.config.ts', import.meta.url)))
  if (!loaded) throw new Error('Development proxy config missing')
  // Exercise the actual development proxy against the isolated E2E backend, never the local user database.
  const proxy = Object.fromEntries(Object.entries(loaded.config.server!.proxy!).map(([path, value]) => [path,
    typeof value === 'string' ? value.replace('8765', '8776') : { ...value, target: 'http://127.0.0.1:8776' }]))
  const server = await createServer({ ...loaded.config, configFile: false, logLevel: 'error',
    server: { ...loaded.config.server, host: '127.0.0.1', port: 5176, strictPort: false, proxy } })
  try {
    await server.listen()
    const address = server.httpServer!.address()
    if (!address || typeof address === 'string') throw new Error('Development proxy did not bind')
    const url = `http://127.0.0.1:${address.port}`
    await page.goto(url)
    await expect(page.getByRole('heading', { name: '创作工作台', exact: true })).toBeVisible()
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/drafts/main' && response.request().method() === 'PUT')
    await page.getByLabel('项目名称', { exact: true }).fill('开发代理自动保存检查')
    expect((await saved).status()).toBe(200)
    await expect(page.getByText('已保存到本地', { exact: true })).toBeVisible()
    const restored = await (await page.request.get(url + '/api/drafts/main')).json()
    expect(restored.name).toBe('开发代理自动保存检查')
    const state = await (await page.request.get(url + '/api/state')).json()
    const denied = await page.request.put(url + '/api/drafts/main', {
      headers: { Origin: 'https://evil.test', 'X-Workbench-Token': state.token }, data: { name: 'rejected', graph: {}, positions: {} },
    })
    expect(denied.status()).toBe(403)
    const preserved = await (await page.request.get(url + '/api/drafts/main')).json()
    expect(preserved.name).toBe('开发代理自动保存检查')
  } finally {
    await page.goto('about:blank')
    await server.close()
  }
})

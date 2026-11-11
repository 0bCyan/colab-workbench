import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests', timeout: 45_000, workers: 1,
  use: { baseURL: 'http://127.0.0.1:8776', channel: process.env.CI ? undefined : 'chrome', viewport: { width: 1280, height: 720 }, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: { command: 'cd .. && uv run uvicorn tests.e2e_server:app --host 127.0.0.1 --port 8776', url: 'http://127.0.0.1:8776/api/state', reuseExistingServer: false },
})

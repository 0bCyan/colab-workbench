import { test, expect } from '@playwright/test'

test('fixed-account login gates workspace, handles failures and supports logout without registration', async ({ page }) => {
  let authenticated = false
  let attempts = 0
  let stateReads = 0
  await page.route('**/api/auth/session', route => route.fulfill({ json: { required: true, authenticated, username: authenticated ? 'tester' : null, csrf: 'synthetic-csrf' } }))
  await page.route('**/api/auth/login', async route => {
    attempts++
    expect(route.request().headers()['x-workbench-token']).toBe('synthetic-csrf')
    const body = route.request().postDataJSON()
    if (body.password !== 'test-password') return route.fulfill({ status: 401, json: { detail: '账号或密码不正确' } })
    authenticated = true
    await route.fulfill({ json: { authenticated: true, username: 'tester' } })
  })
  await page.route('**/api/auth/logout', async route => { authenticated = false; await route.fulfill({ json: { authenticated: false } }) })
  await page.route('**/api/state', async route => { stateReads++; await route.continue() })
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible()
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeDisabled()
  await expect(page.getByText('注册', { exact: true })).toHaveCount(0)
  expect(stateReads).toBe(0)
  await page.getByLabel('登录账号').fill('tester')
  await page.getByLabel('登录密码').fill('wrong')
  await expect(page.getByLabel('登录密码')).toHaveAttribute('type', 'password')
  await page.getByRole('button', { name: '显示密码' }).click()
  await expect(page.getByLabel('登录密码')).toHaveAttribute('type', 'text')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText('账号或密码不正确')
  await page.screenshot({ path: '../screenshots/review/access-login.png' })
  await page.getByLabel('登录密码').fill('test-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '创作工作台', level: 1 })).toBeVisible()
  expect(attempts).toBe(2)
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible()
  await expect(page.getByLabel('登录密码')).toHaveValue('')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeInViewport()
  expect(await page.evaluate(() => ({ width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }))).toEqual({ width: 390, height: 844 })
  await page.screenshot({ path: '../screenshots/review/access-login-mobile.png' })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  expect(await page.locator('.access-card').evaluate(el => getComputedStyle(el).animationName)).toBe('none')
  expect(await page.getByRole('button', { name: '登录', exact: true }).evaluate(el => getComputedStyle(el).transitionDuration)).toBe('0s')
})

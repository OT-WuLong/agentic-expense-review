import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from '@playwright/test'

const requestId = process.argv[2]
if (!requestId) throw new Error('usage: node frontend/scripts/capture-demo.mjs REQUEST_ID')

const users = JSON.parse(process.env.APP_AUTH_USERS_JSON ?? '[]')
const token = users.find((user) => user.role === 'SYSTEM_ADMIN')?.token
if (!token) throw new Error('SYSTEM_ADMIN token is required in APP_AUTH_USERS_JSON')

const root = fileURLToPath(new URL('../..', import.meta.url))
const output = join(root, 'docs', 'screenshots')
const base = 'http://127.0.0.1:8000'
await mkdir(output, { recursive: true })

const browser = await chromium.launch({ channel: 'msedge', headless: true })
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  await page.goto(`${base}/login`)
  await page.screenshot({ path: join(output, 'login.png'), fullPage: true })

  const login = await context.request.post(`${base}/api/v1/session/login`, { data: { token } })
  if (!login.ok()) throw new Error(`login failed with HTTP ${login.status()}`)
  await page.goto(`${base}/approvals/${requestId}`)
  await page.locator('.title-row').filter({ hasText: requestId }).waitFor()
  await page.addStyleTag({ content: '.identity { visibility: hidden !important }' })
  await page.screenshot({ path: join(output, 'approval-workbench.png'), fullPage: true })
  await context.close()
} finally {
  await browser.close()
}

import { expect, test, type Page } from '@playwright/test'

interface DemoUser {
  role: string
  token: string
}

function roleToken(role: string): string {
  const users = JSON.parse(process.env.APP_AUTH_USERS_JSON ?? '[]') as DemoUser[]
  const token = users.find((user) => user.role === role)?.token
  if (!token) throw new Error(`P14 E2E requires an ${role} token from APP_AUTH_USERS_JSON`)
  return token
}

async function login(page: Page, role: string) {
  await page.goto('/login')
  await page.getByLabel('访问令牌').fill(roleToken(role))
  await page.getByLabel('访问令牌').press('Enter')
  await expect(page).toHaveURL(/\/(approvals|rules)$/)
}

async function launchCase(page: Page, caseId: string): Promise<string> {
  await page.goto('/approvals/new')
  const card = page.locator('.fixture-grid article').filter({ hasText: caseId })
  await expect(card).toBeVisible()
  await card.getByRole('button', { name: '启动此案例' }).click()
  await expect(page).toHaveURL(/\/approvals\/REQ-/)
  return new URL(page.url()).pathname.split('/').pop() ?? ''
}

async function inspectEvidence(page: Page) {
  await expect(page.getByRole('heading', { name: '证据与规则' })).toBeVisible()
  await page.locator('.evidence-list .evidence-item').first().click()
  await expect(page.getByRole('dialog', { name: '证据详情' })).toContainText('Evidence ID')
  await page.getByRole('dialog', { name: '证据详情' }).getByRole('button', { name: 'Close' }).click()
}

async function waitForStatus(page: Page, expected: string, alternatives: string[]) {
  const title = page.locator('.title-row')
  await expect(title).toContainText(new RegExp([expected, ...alternatives].join('|')))
  await expect(title).toContainText(expected, { timeout: 1_000 })
}

test('incorrect token stays on login and shows an error', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('访问令牌').fill('not-a-valid-token')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page).toHaveURL(/\/login$/)
})

test('A rejects commute and keeps cited evidence after refresh', async ({ page }) => {
  await login(page, 'SYSTEM_ADMIN')
  const requestId = await launchCase(page, 'GC-A-TRANSPORT-COMMUTE-REJECT')
  test.info().annotations.push({ type: 'request_id', description: requestId })
  await waitForStatus(page, '业务驳回', ['系统异常', '证据不足', '待人工', '已完成'])
  await inspectEvidence(page)
  await page.reload()
  await expect(page.locator('.title-row')).toContainText(requestId)
  await expect(page.locator('.title-row')).toContainText('业务驳回')
})

test('B passes lodging with policy and city-tier evidence', async ({ page }) => {
  await login(page, 'SYSTEM_ADMIN')
  const requestId = await launchCase(page, 'GC-B-LODGING-MULTIHOP-PASS')
  test.info().annotations.push({ type: 'request_id', description: requestId })
  await waitForStatus(page, '已完成', ['系统异常', '证据不足', '业务驳回', '待人工'])
  await expect(page.locator('.evidence-list')).toContainText('STRUCTURED_RECORD')
  await inspectEvidence(page)
})

test('C interrupts for human review and resumes to a persisted decision', async ({ page }) => {
  await login(page, 'SYSTEM_ADMIN')
  const requestId = await launchCase(page, 'GC-C-DINING-VERSION-CONFLICT-HUMAN')
  test.info().annotations.push({ type: 'request_id', description: requestId })
  await waitForStatus(page, '待人工', ['系统异常', '证据不足', '业务驳回', '已完成'])
  await expect(page.getByText('检测到制度版本冲突')).toBeVisible()
  await page.getByRole('button', { name: '退出登录' }).click()
  await login(page, 'FINANCE_REVIEWER')
  await page.goto(`/approvals/${requestId}`)
  await expect(page.getByRole('heading', { name: '人工复核' })).toBeVisible()
  await page.getByLabel('人工复核理由').fill('P14 合成案例复核：已核对冲突版本，人工驳回。')
  await page.getByText('人工驳回', { exact: true }).click()
  await page.getByRole('button', { name: '提交不可变审计记录' }).click()
  await waitForStatus(page, '已完成', ['系统异常', '证据不足', '业务驳回'])
  await page.reload()
  await expect(page.locator('.title-row')).toContainText('已完成')
  await expect(page.locator('.title-row')).toContainText('建议驳回')
})

import { fileURLToPath } from 'node:url'

import { defineConfig, devices } from '@playwright/test'

const frontend = fileURLToPath(new URL('.', import.meta.url))
const root = fileURLToPath(new URL('..', import.meta.url))

export default defineConfig({
  testDir: './e2e',
  timeout: 300_000,
  expect: { timeout: 180_000 },
  workers: 1,
  retries: 0,
  reporter: [['list'], ['json', { outputFile: 'test-results/results.json' }]],
  use: {
    baseURL: 'http://127.0.0.1:5173',
    ...devices['Desktop Chrome'],
    channel: process.env.PLAYWRIGHT_CHANNEL ?? 'msedge',
    trace: 'off',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: 'uv run --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8000',
      cwd: root,
      url: 'http://127.0.0.1:8000/health',
      reuseExistingServer: true,
      timeout: 90_000,
    },
    {
      command: 'npm run preview -- --host 127.0.0.1 --port 5173',
      cwd: frontend,
      url: 'http://127.0.0.1:5173/login',
      reuseExistingServer: true,
      timeout: 90_000,
    },
  ],
})

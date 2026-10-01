import axios from 'axios'

let csrfToken = ''
let redirectingToLogin = false

export function handleUnauthorized() {
  if (redirectingToLogin || window.location.pathname === '/login') return
  redirectingToLogin = true
  const current = window.location.pathname + window.location.search + window.location.hash
  window.location.replace(`/login?redirect=${encodeURIComponent(current)}`)
}

export function setCsrfToken(value: string) {
  csrfToken = value
}

export const http = axios.create({
  baseURL: '/api/v1',
  timeout: 15_000,
  withCredentials: true,
})

http.interceptors.request.use((config) => {
  if (!['get', 'head', 'options'].includes(config.method?.toLowerCase() ?? 'get') && csrfToken) {
    config.headers['X-CSRF-Token'] = csrfToken
  }
  config.headers['X-Request-ID'] = crypto.randomUUID()
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && error.config?.url !== '/session/login') {
      handleUnauthorized()
    }
    const message = error.response?.data?.error?.message
    if (message) error.message = message
    return Promise.reject(error)
  },
)

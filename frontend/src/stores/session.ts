import { defineStore } from 'pinia'

import { http, setCsrfToken } from '@/api/http'

export type Role = 'APPLICANT' | 'FINANCE_REVIEWER' | 'RULE_ADMIN' | 'SYSTEM_ADMIN'

export interface Identity {
  actor_id: string
  display_name: string
  role: Role
  department_ids: string[]
}

interface SessionInfo {
  identity: Identity
  csrf_token: string | null
}

export const useSessionStore = defineStore('session', {
  state: () => ({
    identity: null as Identity | null,
    ready: false,
  }),
  actions: {
    apply(info: SessionInfo) {
      this.identity = info.identity
      setCsrfToken(info.csrf_token ?? '')
      this.ready = true
    },
    async load() {
      try {
        this.apply((await http.get<SessionInfo>('/session')).data)
      } catch {
        this.identity = null
        setCsrfToken('')
        this.ready = true
      }
    },
    async login(token: string) {
      this.apply((await http.post<SessionInfo>('/session/login', { token })).data)
    },
    async logout() {
      await http.post('/session/logout')
      this.identity = null
      setCsrfToken('')
    },
  },
})

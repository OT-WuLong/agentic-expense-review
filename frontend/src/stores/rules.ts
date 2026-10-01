import { defineStore } from 'pinia'

import {
  fetchRules,
  type CandidateRule,
  type FormalRule,
} from '@/api/rules'

export const useRuleStore = defineStore('rules', {
  state: () => ({
    rules: [] as FormalRule[],
    candidates: [] as CandidateRule[],
    loading: false,
    error: '',
  }),
  actions: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const data = await fetchRules()
        this.rules = data.rules
        this.candidates = data.candidates
      } catch (error) {
        this.error = error instanceof Error ? error.message : '无法加载规则目录'
      } finally {
        this.loading = false
      }
    },
  },
})

import { defineStore } from 'pinia'

import { fetchPolicies, type PolicyItem } from '@/api/policies'

export const usePolicyStore = defineStore('policy', {
  state: () => ({ items: [] as PolicyItem[], loading: false, error: '' }),
  actions: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        this.items = (await fetchPolicies()).items
      } catch (error) {
        this.error = error instanceof Error ? error.message : '无法加载制度库'
      } finally {
        this.loading = false
      }
    },
  },
})

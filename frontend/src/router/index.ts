import { nextTick } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'

import { useSessionStore, type Role } from '@/stores/session'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/approvals' },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
    },
    {
      path: '/approvals',
      name: 'approvals',
      component: () => import('@/views/ApprovalListView.vue'),
      meta: { roles: ['APPLICANT', 'FINANCE_REVIEWER', 'SYSTEM_ADMIN'] },
    },
    {
      path: '/approvals/new',
      name: 'approval-new',
      component: () => import('@/views/ApprovalCreateView.vue'),
      meta: { roles: ['APPLICANT', 'FINANCE_REVIEWER', 'SYSTEM_ADMIN'] },
    },
    {
      path: '/approvals/:id',
      name: 'approval-detail',
      component: () => import('@/views/ApprovalWorkbenchView.vue'),
      meta: { roles: ['APPLICANT', 'FINANCE_REVIEWER', 'SYSTEM_ADMIN'] },
    },
    {
      path: '/policies',
      name: 'policies',
      component: () => import('@/views/PoliciesView.vue'),
    },
    {
      path: '/rules',
      name: 'rules',
      component: () => import('@/views/RulesView.vue'),
      meta: { roles: ['RULE_ADMIN', 'SYSTEM_ADMIN'] },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
    },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const session = useSessionStore()
  if (!session.ready) await session.load()
  const home = session.identity?.role === 'RULE_ADMIN' ? '/rules' : '/approvals'
  if (to.name === 'login') return session.identity ? home : true
  if (!session.identity) return { path: '/login', query: { redirect: to.fullPath } }
  const roles = to.meta.roles as Role[] | undefined
  if (roles && !roles.includes(session.identity.role)) return home
  return true
})

router.afterEach(() => {
  void nextTick(() => document.querySelector<HTMLElement>('#main-content')?.focus())
})

export default router

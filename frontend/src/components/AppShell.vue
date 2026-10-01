<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'

import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const labels = {
  APPLICANT: '申请人',
  FINANCE_REVIEWER: '财务复核员',
  RULE_ADMIN: '规则管理员',
  SYSTEM_ADMIN: '系统管理员',
}

async function logout() {
  await session.logout()
  await router.replace('/login')
}
</script>

<template>
  <slot v-if="route.name === 'login'" />
  <div v-else class="shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <header class="topbar">
      <router-link class="brand" to="/approvals" aria-label="费用预审工作台首页">
        <span class="brand-mark" aria-hidden="true">A</span>
        <span>
          <strong>费用预审工作台</strong>
          <small>Agentic RAG · 单公司 MVP</small>
        </span>
      </router-link>
      <div class="identity">
        <span class="environment">{{ session.identity?.display_name }} · {{ session.identity ? labels[session.identity.role] : '' }}</span>
        <el-button text class="logout-button" @click="logout">退出登录</el-button>
      </div>
    </header>

    <aside class="sidebar" aria-label="主导航">
      <router-link v-if="session.identity?.role !== 'RULE_ADMIN'" to="/approvals" :class="{ active: route.path === '/approvals' }">
        <span aria-hidden="true">01</span>审批队列
      </router-link>
      <router-link v-if="session.identity?.role !== 'RULE_ADMIN'" to="/approvals/new" :class="{ active: route.path === '/approvals/new' }">
        <span aria-hidden="true">02</span>新建审批
      </router-link>
      <router-link to="/policies" :class="{ active: route.path === '/policies' }">
        <span aria-hidden="true">03</span>制度库
      </router-link>
      <router-link v-if="['RULE_ADMIN', 'SYSTEM_ADMIN'].includes(session.identity?.role ?? '')" to="/rules" :class="{ active: route.path === '/rules' }">
        <span aria-hidden="true">04</span>规则治理
      </router-link>
    </aside>

    <main id="main-content" class="main-content" tabindex="-1">
      <slot />
    </main>
  </div>
</template>

<style scoped>
.shell {
  min-height: 100vh;
  padding-top: 64px;
  padding-left: 224px;
}

.skip-link {
  position: fixed;
  z-index: 100;
  top: 8px;
  left: 8px;
  padding: 10px 14px;
  color: white;
  text-decoration: none;
  background: var(--color-action);
  transform: translateY(-160%);
}

.skip-link:focus {
  transform: translateY(0);
}

.topbar {
  position: fixed;
  z-index: 20;
  inset: 0 0 auto;
  display: flex;
  height: 64px;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  color: white;
  background: var(--color-primary);
  border-bottom: 1px solid rgb(255 255 255 / 16%);
}

.brand {
  display: flex;
  min-height: 44px;
  gap: 12px;
  align-items: center;
  color: inherit;
  text-decoration: none;
}

.brand-mark {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  color: var(--color-primary);
  font-weight: 800;
  background: white;
  border-radius: 4px;
}

.brand strong,
.brand small {
  display: block;
}

.brand strong {
  font-size: 15px;
  letter-spacing: 0.03em;
}

.brand small {
  color: #cbd5e1;
  font-size: 11px;
}

.identity {
  display: flex;
  gap: 12px;
  align-items: center;
}

.environment {
  padding: 4px 8px;
  color: #dbeafe;
  font-size: 12px;
  border: 1px solid #60a5fa;
  border-radius: 3px;
}

.logout-button {
  color: white;
}

.sidebar {
  position: fixed;
  z-index: 10;
  inset: 64px auto 0 0;
  width: 224px;
  padding: 20px 12px;
  background: #fff;
  border-right: 1px solid var(--color-border);
}

.sidebar a {
  display: flex;
  min-height: 44px;
  gap: 12px;
  align-items: center;
  padding: 0 14px;
  color: #475569;
  font-weight: 600;
  text-decoration: none;
  border-left: 3px solid transparent;
}

.sidebar a span {
  color: #94a3b8;
  font:
    11px/1 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
}

.sidebar a:hover,
.sidebar a.active {
  color: var(--color-primary);
  background: #eff6ff;
  border-left-color: var(--color-action);
}

.main-content {
  width: 100%;
  max-width: 1600px;
  min-height: calc(100vh - 64px);
  padding: 24px;
  margin: auto;
}

@media (width <= 768px) {
  .shell {
    padding-top: 60px;
    padding-left: 0;
    padding-bottom: 58px;
  }

  .topbar {
    height: 60px;
    padding: 0 12px;
  }

  .brand small,
  .environment {
    display: none;
  }

  .brand strong {
    font-size: 14px;
  }

  .sidebar {
    inset: auto 0 0;
    display: grid;
    width: auto;
    height: 58px;
    grid-auto-columns: 1fr;
    grid-auto-flow: column;
    padding: 0;
    border-top: 1px solid var(--color-border);
    border-right: 0;
  }

  .sidebar a {
    justify-content: center;
    min-width: 0;
    padding: 0 6px;
    font-size: 12px;
    border-top: 3px solid transparent;
    border-left: 0;
  }

  .sidebar a span {
    display: none;
  }

  .sidebar a:hover,
  .sidebar a.active {
    border-top-color: var(--color-action);
    border-left-color: transparent;
  }

  .main-content {
    padding: 16px 12px;
  }
}
</style>

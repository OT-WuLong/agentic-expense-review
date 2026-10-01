<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useSessionStore } from '@/stores/session'

const router = useRouter()
const route = useRoute()
const session = useSessionStore()
const token = ref('')
const busy = ref(false)
const error = ref('')

async function login() {
  if (!token.value.trim() || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await session.login(token.value.trim())
    token.value = ''
    const home = session.identity?.role === 'RULE_ADMIN' ? '/rules' : '/approvals'
    await router.replace(typeof route.query.redirect === 'string' ? route.query.redirect : home)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '登录失败，请检查访问令牌'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-card" aria-labelledby="login-title">
      <span class="login-mark" aria-hidden="true">A</span>
      <p class="eyebrow">Single-company workspace</p>
      <h1 id="login-title">登录费用预审工作台</h1>
      <p class="login-hint">输入管理员发放的访问令牌。身份和可访问部门由服务端确定。</p>
      <form @submit.prevent="login">
        <label for="access-token">访问令牌</label>
        <input
          id="access-token"
          v-model="token"
          type="password"
          autocomplete="current-password"
          required
          aria-describedby="login-help"
        />
        <small id="login-help">可以从密码管理器粘贴；令牌不会保存在浏览器本地存储。</small>
        <p v-if="error" role="alert" class="login-error">{{ error }}</p>
        <button type="submit" :disabled="busy || !token.trim()">
          {{ busy ? '正在登录…' : '登录' }}
        </button>
      </form>
    </section>
  </main>
</template>

<style scoped>
.login-page {
  display: grid;
  min-height: 100vh;
  padding: 24px;
  place-items: center;
  background: var(--color-background);
}
.login-card {
  width: min(100%, 440px);
  padding: clamp(24px, 5vw, 40px);
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  box-shadow: 0 16px 48px rgb(15 23 42 / 8%);
}
.login-mark {
  display: grid;
  width: 44px;
  height: 44px;
  place-items: center;
  color: white;
  font-size: 20px;
  font-weight: 800;
  background: var(--color-primary);
  border-radius: 6px;
}
.login-card h1 { margin: 4px 0 8px; color: var(--color-primary); font-size: 24px; }
.login-hint { margin: 0 0 24px; color: #475569; }
form { display: grid; gap: 12px; }
label { font-weight: 700; }
input { min-height: 44px; padding: 10px 12px; border: 1px solid #64748b; border-radius: 4px; }
small { color: #475569; }
.login-error { margin: 0; color: var(--color-danger); }
button {
  min-height: 44px;
  margin-top: 8px;
  color: white;
  font-weight: 700;
  background: var(--color-action);
  border: 0;
  border-radius: 4px;
  cursor: pointer;
}
button:disabled { cursor: not-allowed; opacity: 0.6; }
</style>

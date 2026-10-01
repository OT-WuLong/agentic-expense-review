<template>
  <div class="not-found">
    <p class="eyebrow">404</p>
    <h1>页面不存在</h1>
    <router-link to="/approvals">返回审批队列</router-link>
  </div>
</template>

<style scoped>
.not-found {
  display: grid;
  min-height: 60vh;
  place-content: center;
  text-align: center;
}

.not-found a {
  min-height: 44px;
  color: var(--color-action);
  line-height: 44px;
}
</style>

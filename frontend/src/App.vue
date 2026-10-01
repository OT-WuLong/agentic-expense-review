<script setup lang="ts">
import AppShell from '@/components/AppShell.vue'
</script>

<template>
  <AppShell>
    <RouterView :key="$route.path" />
  </AppShell>
</template>

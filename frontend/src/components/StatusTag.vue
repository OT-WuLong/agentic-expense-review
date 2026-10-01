<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value?: string | null }>()

const labels: Record<string, string> = {
  CREATED: '已创建',
  RUNNING: '运行中',
  BUSINESS_REJECTED: '业务驳回',
  INSUFFICIENT_EVIDENCE: '证据不足',
  SYSTEM_ERROR: '系统异常',
  HUMAN_PENDING: '待人工',
  COMPLETED: '已完成',
  PASS_RECOMMENDED: '建议通过',
  REJECT_RECOMMENDED: '建议驳回',
  HUMAN_REVIEW: '人工复核',
  LOW: '低风险',
  MEDIUM: '中风险',
  HIGH: '高风险',
  PASS: '通过',
  FAIL: '不通过',
  INDETERMINATE: '无法判定',
}

const tone = computed(() => {
  if (['COMPLETED', 'PASS_RECOMMENDED', 'LOW', 'PASS'].includes(props.value ?? '')) {
    return 'success'
  }
  if (['RUNNING', 'CREATED'].includes(props.value ?? '')) return 'primary'
  if (['HUMAN_PENDING', 'HUMAN_REVIEW', 'MEDIUM', 'INDETERMINATE'].includes(props.value ?? '')) {
    return 'warning'
  }
  if (
    ['BUSINESS_REJECTED', 'REJECT_RECOMMENDED', 'SYSTEM_ERROR', 'HIGH', 'FAIL'].includes(
      props.value ?? '',
    )
  ) {
    return 'danger'
  }
  return 'info'
})
</script>

<template>
  <el-tag :type="tone === 'primary' ? undefined : tone" effect="light" round>
    <span class="status-dot" aria-hidden="true" />{{ labels[value ?? ''] ?? value ?? '—' }}
  </el-tag>
</template>

<style scoped>
.status-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  border-radius: 50%;
  background: currentcolor;
}
</style>

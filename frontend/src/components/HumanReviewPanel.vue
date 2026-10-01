<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import StatusTag from '@/components/StatusTag.vue'
import { useApprovalStore } from '@/stores/approval'
import type { HumanReviewAction, Recommendation, WorkflowState } from '@/types'

const props = withDefaults(
  defineProps<{
    requestId: string
    visible: boolean
    recommendation?: Recommendation | null
    riskLevel?: 'LOW' | 'MEDIUM' | 'HIGH' | null
    rules?: WorkflowState['rule_results']
    allowedActions?: HumanReviewAction[]
    reason?: string
    missingItems?: string[]
  }>(),
  {
    rules: () => [],
    allowedActions: () => [],
  },
)
const emit = defineEmits<{ completed: [] }>()

const store = useApprovalStore()
const submitting = ref(false)
let pendingReview: { signature: string; key: string } | null = null
const form = reactive({
  action: '' as HumanReviewAction | '',
  recommendation: '' as Recommendation | '',
  reason: '',
})
const actionLabels: Record<HumanReviewAction, string> = {
  APPROVE: '人工通过',
  EDIT: '修改结论',
  REJECT: '人工驳回',
}

watch(
  () => props.allowedActions,
  (actions) => {
    if (form.action && !actions.includes(form.action)) form.action = ''
    if (!actions.includes('APPROVE') && form.recommendation === 'PASS_RECOMMENDED')
      form.recommendation = ''
  },
  { immediate: true },
)

watch(
  () => props.requestId,
  () => {
    pendingReview = null
    form.action = ''
    form.recommendation = ''
    form.reason = ''
  },
)

const actionDescription = computed(() => {
  if (!form.action) return '请先选择人工动作；系统不会默认通过或驳回。'
  if (form.action === 'APPROVE') return '由财务复核员将最终结论明确设为通过。'
  if (form.action === 'REJECT') return '人工明确驳回，并记录与 Agent 建议的差异。'
  return '调整为人工指定的通过或驳回建议。'
})
const ruleSummary = computed(() => {
  const count = (outcome: string) => props.rules.filter((item) => item.outcome === outcome).length
  return `通过 ${count('PASS')} · 不通过 ${count('FAIL')} · 待确认 ${count('INDETERMINATE')}`
})

async function submit() {
  if (
    submitting.value ||
    !form.reason.trim() ||
    !form.action ||
    !props.allowedActions.includes(form.action) ||
    (form.action === 'EDIT' && !form.recommendation)
  )
    return
  const signature = JSON.stringify([props.requestId, form.action, form.reason, form.recommendation])
  if (pendingReview?.signature !== signature) {
    pendingReview = {
      signature,
      key: `review-${props.requestId}-${crypto.randomUUID()}`,
    }
  }
  submitting.value = true
  try {
    await store.review(props.requestId, {
      action: form.action,
      reason: form.reason,
      idempotency_key: pendingReview.key,
      ...(form.action === 'EDIT' ? { recommendation: form.recommendation } : {}),
    })
    emit('completed')
    ElMessage.success('人工复核已受理，请等待状态更新')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '人工复核提交失败，请刷新后重试')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section v-if="visible" class="panel review-panel" aria-labelledby="review-heading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">Human checkpoint</p>
        <h2 id="review-heading">人工复核</h2>
      </div>
    </div>

    <el-alert
      :title="reason ?? 'Agent 请求人工做最终判断'"
      type="warning"
      :closable="false"
      show-icon
    />
    <div class="review-context" aria-label="人工复核上下文">
      <div><span>Agent 建议</span><StatusTag :value="recommendation" /></div>
      <div><span>风险等级</span><StatusTag :value="riskLevel" /></div>
      <p><span>规则结果</span>{{ ruleSummary }}</p>
    </div>
    <ul v-if="missingItems?.length" class="missing-list">
      <li v-for="item in missingItems" :key="item">{{ item }}</li>
    </ul>

    <el-alert
      v-if="!allowedActions.length"
      title="当前没有可执行的人工动作，请刷新审批状态"
      type="error"
      :closable="false"
      show-icon
    />
    <el-form v-else label-position="top" class="review-form" @submit.prevent="submit">
      <el-form-item label="复核动作" required>
        <el-radio-group v-model="form.action">
          <el-radio-button v-for="action in allowedActions" :key="action" :value="action">
            {{ actionLabels[action] }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <p class="action-help">{{ actionDescription }}</p>
      <el-form-item v-if="form.action === 'EDIT'" label="修改后的建议" required>
        <el-select v-model="form.recommendation" placeholder="请选择最终建议" class="full-width">
          <el-option
            v-if="allowedActions.includes('APPROVE')"
            label="建议通过"
            value="PASS_RECOMMENDED"
          />
          <el-option label="建议驳回" value="REJECT_RECOMMENDED" />
        </el-select>
      </el-form-item>
      <el-form-item label="复核理由" required>
        <el-input
          v-model="form.reason"
          type="textarea"
          :rows="3"
          maxlength="500"
          show-word-limit
          placeholder="说明判断依据；内容将进入审计日志"
          aria-label="人工复核理由"
        />
      </el-form-item>
      <el-button
        type="primary"
        native-type="submit"
        :loading="submitting"
        :disabled="!form.action || !form.reason.trim() || (form.action === 'EDIT' && !form.recommendation)"
        class="full-width action-button"
      >
        提交不可变审计记录
      </el-button>
    </el-form>
  </section>
</template>

<style scoped>
.review-panel {
  border-top: 3px solid var(--color-warning);
}

.review-form {
  margin-top: 16px;
}

.review-context {
  display: grid;
  gap: 8px;
  padding: 12px;
  margin-top: 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
}

.review-context div,
.review-context p {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 0;
  color: #475569;
  font-size: 12px;
}

.review-context span {
  color: #64748b;
}

.action-help {
  margin: -12px 0 14px;
  color: #64748b;
  font-size: 12px;
}

.missing-list {
  padding-left: 20px;
  color: #92400e;
  font-size: 13px;
}

.full-width {
  width: 100%;
}

.action-button {
  min-height: 44px;
}
</style>

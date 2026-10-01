<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'

import AgentTimeline from '@/components/AgentTimeline.vue'
import ApplicationPanel from '@/components/ApplicationPanel.vue'
import EvidencePanel from '@/components/EvidencePanel.vue'
import HumanReviewPanel from '@/components/HumanReviewPanel.vue'
import StatusTag from '@/components/StatusTag.vue'
import { useApprovalEvents } from '@/composables/useApprovalEvents'
import { useApprovalStore } from '@/stores/approval'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const store = useApprovalStore()
const session = useSessionStore()
const requestId = String(route.params.id)
store.selectRequest(requestId)
const media = window.matchMedia('(max-width: 900px)')
const mobile = ref(media.matches)
const mobileTab = ref('trajectory')
const retrying = ref(false)
const auditError = ref('')

function updateMedia() {
  mobile.value = media.matches
}

async function refreshAudit() {
  try {
    await store.loadAudit(requestId)
    auditError.value = ''
  } catch (error) {
    auditError.value = error instanceof Error ? error.message : '请稍后重试'
  }
}

let refreshTimer = 0
let recoveryRefresh = 0
function scheduleRefresh() {
  window.clearTimeout(refreshTimer)
  refreshTimer = window.setTimeout(async () => {
    await Promise.allSettled([store.loadDetail(requestId), refreshAudit()])
  }, 120)
}

const stream = useApprovalEvents(
  requestId,
  async (event) => {
    if (store.activeRequestId !== requestId) return
    store.mergeEvent(event)
    if (
      [
        'WORKFLOW_NODE_COMPLETED',
        'WORKFLOW_FAILED',
        'DOCUMENT_PARSED',
        'WORKFLOW_RETRY_REQUESTED',
        'HUMAN_REVIEW_RESUME_FAILED',
        'HUMAN_REVIEW_REQUESTED',
        'APPROVAL_FINALIZED',
      ].includes(event.event_type)
    ) {
      scheduleRefresh()
    }
  },
  {
    immediate: false,
    isTerminal: () => {
      const status = store.detail?.approval.status
      return (
        status === 'COMPLETED' ||
        status === 'BUSINESS_REJECTED' ||
        status === 'SYSTEM_ERROR' ||
        (status === 'INSUFFICIENT_EVIDENCE' && store.detail?.workflow?.interrupted === false)
      )
    },
  },
)

const approval = computed(() => store.detail?.approval)
const workflow = computed(() => store.detail?.workflow?.state)
const invoiceProgress = computed(() => {
  if (approval.value?.status !== 'RUNNING' || workflow.value) return null
  if (store.audit.some((event) => event.event_type === 'DOCUMENT_PARSED')) {
    return '发票已解析，正在启动 Agent'
  }
  return store.audit.some(
    (event) => event.event_type === 'DOCUMENT_UPLOADED' && event.payload.ingestion_status === 'PARSING',
  )
    ? '发票解析中，完成后自动启动 Agent'
    : null
})
const steps = computed(() => workflow.value?.agent_trajectory ?? [])
const evidence = computed(() => workflow.value?.evidence ?? [])
const rules = computed(() => workflow.value?.rule_results ?? [])
const conflicts = computed(() => workflow.value?.evidence_review?.conflict_evidence_ids ?? [])
const latency = computed(() =>
  (workflow.value?.tool_observations ?? []).reduce((total, item) => total + item.latency_ms, 0),
)
const degraded = computed(
  () => (workflow.value?.tool_observations ?? []).filter((item) => item.is_degraded).length,
)
const humanVisible = computed(
  () =>
    session.identity?.role === 'FINANCE_REVIEWER' &&
    store.detail?.workflow?.interrupted === true &&
    store.detail?.retry_mode !== 'HUMAN_REVIEW',
)
const workflowFailed = computed(() => approval.value?.status === 'SYSTEM_ERROR')
const failureMessage = computed(() => {
  if (store.detail?.retry_mode === 'UNRECOVERABLE') return '执行已中断，但原始发票或 Checkpoint 不足以安全恢复。'
  if (approval.value?.status === 'RUNNING') return '执行时间超过预期；若任务已中断，可接管后继续处理。'
  if (approval.value?.status === 'HUMAN_PENDING') return '人工复核后的处理尚未完成，可尝试从断点继续。'
  if (workflow.value?.agent_stop_reason === 'TOKEN_BUDGET') {
    return '模型调用累计超出本申请的 token 预算，工作流已停止。'
  }
  if (workflow.value) return '工作流执行失败，详情已写入审计日志。'
  const event = [...store.audit].reverse().find((item) => item.event_type === 'WORKFLOW_FAILED')
  const message = String(event?.payload.message ?? '')
  if (message.startsWith('MinerU upload URL request failed')) return '连接 MinerU 失败，发票解析任务未建立。'
  if (message.startsWith('MinerU result download failed')) return '发票解析结果下载失败。'
  if (message.startsWith('MinerU file upload failed')) return '发票发送至 MinerU 时失败。'
  return '执行失败，详情已写入审计日志。'
})
const retryAvailable = computed(() => store.detail?.retry_mode != null)
const recoveryTitle = computed(() => {
  if (store.detail?.retry_mode === 'UNRECOVERABLE') return '工作流无法续跑'
  if (store.detail?.retry_mode === 'HUMAN_REVIEW') return '人工复核处理中'
  if (approval.value?.status === 'HUMAN_PENDING') return '人工复核需要续跑'
  if (approval.value?.status === 'RUNNING') return '工作流可能已中断'
  return store.detail?.retry_mode === 'INVOICE_PARSE' ? '发票解析失败' : '工作流执行失败'
})
const recoveryHint = computed(() => {
  switch (store.detail?.retry_mode) {
    case 'HUMAN_REVIEW': return '若长时间没有更新，可取回原复核意见重新调度，不需要再次填写。'
    case 'INVOICE_PARSE': return '可使用原申请和已保存的发票重试，无需重新创建。'
    case 'INITIALIZE': return '可从创建时保存的原始申请重新启动。'
    case 'UNRECOVERABLE': return '确认后会把申请标记为失败，再重新提交完整材料。'
    default: return '可从最近的 Checkpoint 安全续跑。'
  }
})
const recoveryButton = computed(() => {
  switch (store.detail?.retry_mode) {
    case 'HUMAN_REVIEW': return '重试原人工复核'
    case 'INVOICE_PARSE': return '重试解析'
    case 'UNRECOVERABLE': return '确认失败'
    case 'CHECKPOINT': return '从断点继续'
    default: return '重新启动'
  }
})

async function retryWorkflow() {
  if (retrying.value) return
  retrying.value = true
  try {
    const retryMode = store.detail?.retry_mode
    await store.retry(requestId)
    if (retryMode !== 'UNRECOVERABLE' && stream.finished.value) void stream.connect()
    if (retryMode === 'UNRECOVERABLE') {
      ElMessage.warning('已标记为系统错误，请重新创建申请并上传发票')
    } else {
      ElMessage.success(
        retryMode === 'INVOICE_PARSE'
          ? '正在原申请重试发票解析'
          : retryMode === 'HUMAN_REVIEW'
            ? '原人工复核已重新调度'
            : '工作流已重新进入运行队列',
      )
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '工作流重试失败')
  } finally {
    retrying.value = false
  }
}

onMounted(async () => {
  recoveryRefresh = window.setInterval(() => {
    if (approval.value?.status === 'RUNNING' || approval.value?.status === 'HUMAN_PENDING') {
      void store.loadDetail(requestId).catch(() => {})
    }
  }, 60_000)
  updateMedia()
  media.addEventListener('change', updateMedia)
  void refreshAudit()
  try {
    await store.loadDetail(requestId)
  } catch {
    return
  }
  void stream.connect()
})

onBeforeUnmount(() => {
  store.leaveRequest(requestId)
  media.removeEventListener('change', updateMedia)
  window.clearTimeout(refreshTimer)
  window.clearInterval(recoveryRefresh)
})
</script>

<template>
  <div v-if="store.detail" class="page workbench-page">
    <div class="workbench-header">
      <div>
        <router-link to="/approvals" class="back-link">← 返回审批队列</router-link>
        <div class="title-row">
          <h1>{{ requestId }}</h1>
          <StatusTag :value="approval?.status" />
          <StatusTag :value="approval?.recommendation" />
        </div>
        <p>
          {{ approval?.expense_type }} · {{ approval?.department_id }} · 创建于
          {{ new Date(approval!.created_at).toLocaleString('zh-CN') }}
        </p>
      </div>
      <div class="connection">
        <span
          :class="{ online: stream.connected.value || stream.finished.value }"
          aria-hidden="true"
        />
        {{
          stream.finished.value ? '事件已归档' : stream.connected.value ? 'SSE 已连接' : '正在重连'
        }}
      </div>
    </div>

    <p class="sr-only" aria-live="polite">
      审批状态 {{ approval?.status }}，已完成 {{ workflow?.agent_step_count ?? 0 }} 个 Agent 步骤。
    </p>

    <div class="runtime-strip" aria-label="运行指标">
      <div>
        <span>Agent 步数</span><strong>{{ workflow?.agent_step_count ?? 0 }}</strong>
      </div>
      <div>
        <span>检索轮次</span><strong>{{ workflow?.retrieval_round_count ?? 0 }}</strong>
      </div>
      <div>
        <span>查询改写</span><strong>{{ workflow?.query_rewrite_count ?? 0 }}</strong>
      </div>
      <div>
        <span>Token</span><strong>{{ workflow?.tokens_used ?? 0 }}</strong>
      </div>
      <div>
        <span>工具耗时</span><strong>{{ latency }}ms</strong>
      </div>
      <div>
        <span>降级调用</span><strong :class="{ warning: degraded }">{{ degraded }}</strong>
      </div>
    </div>

    <el-alert v-if="store.error" :title="store.error" type="error" show-icon :closable="false" />
    <el-alert
      v-if="auditError"
      :title="`审计记录加载失败：${auditError}`"
      type="warning"
      show-icon
      :closable="false"
    >
      <el-button link type="primary" @click="refreshAudit">重试加载审计</el-button>
    </el-alert>
    <el-alert v-if="invoiceProgress" :title="invoiceProgress" type="info" show-icon :closable="false" />
    <el-alert
      v-if="store.detail.consistency.status === 'DIVERGED'"
      :title="`Checkpoint 与业务投影不一致：${store.detail.consistency.mismatched_fields.join('、')}`"
      type="error"
      show-icon
      :closable="false"
      class="consistency-alert"
    />
    <section v-if="workflowFailed || retryAvailable" class="retry-panel" aria-live="polite">
      <div>
        <strong>{{ recoveryTitle }}</strong>
        <span>{{ failureMessage }}</span>
        <span v-if="retryAvailable">{{ recoveryHint }}</span>
        <span v-else-if="store.detail.workflow && store.detail.workflow.next_nodes.length === 0">
          工作流已结束，不能从终点 Checkpoint 续跑；请重新创建申请。
        </span>
        <span v-else>当前没有可重试的发票或 Checkpoint，请重新创建申请。</span>
      </div>
      <el-button
        v-if="retryAvailable"
        type="primary"
        :loading="retrying"
        @click="retryWorkflow"
      >
        {{ recoveryButton }}
      </el-button>
    </section>

    <div v-if="!mobile" class="workbench-grid">
      <div class="left-column">
        <ApplicationPanel :detail="store.detail" @uploaded="scheduleRefresh" />
        <HumanReviewPanel
          :request-id="requestId"
          :visible="humanVisible"
          :recommendation="approval?.recommendation"
          :risk-level="approval?.risk_level"
          :rules="rules"
          :allowed-actions="store.detail.workflow?.allowed_actions ?? []"
          :reason="workflow?.pending_human_action?.reason"
          :missing-items="workflow?.pending_human_action?.missing_items"
          @completed="scheduleRefresh"
        />
      </div>
      <AgentTimeline
        :steps="steps"
        :events="store.audit"
        :running="approval?.status === 'RUNNING'"
      />
      <div class="right-column">
        <EvidencePanel :evidence="evidence" :rules="rules" :conflict-ids="conflicts" />
        <section v-if="workflow?.decision_reasons?.length" class="panel decision-panel">
          <div class="panel-heading">
            <div>
              <p class="eyebrow">Decision</p>
              <h2>决策依据</h2>
            </div>
          </div>
          <article v-for="reason in workflow.decision_reasons" :key="reason.code">
            <code>{{ reason.code }}</code>
            <p>{{ reason.message }}</p>
          </article>
        </section>
      </div>
    </div>

    <el-tabs v-else v-model="mobileTab" class="mobile-tabs" stretch>
      <el-tab-pane label="轨迹" name="trajectory">
        <AgentTimeline
          :steps="steps"
          :events="store.audit"
          :running="approval?.status === 'RUNNING'"
        />
      </el-tab-pane>
      <el-tab-pane label="申请" name="application">
        <ApplicationPanel :detail="store.detail" @uploaded="scheduleRefresh" />
      </el-tab-pane>
      <el-tab-pane label="证据" name="evidence">
        <EvidencePanel :evidence="evidence" :rules="rules" :conflict-ids="conflicts" />
      </el-tab-pane>
      <el-tab-pane v-if="humanVisible" label="人工复核" name="review">
        <HumanReviewPanel
          :request-id="requestId"
          :visible="humanVisible"
          :recommendation="approval?.recommendation"
          :risk-level="approval?.risk_level"
          :rules="rules"
          :allowed-actions="store.detail.workflow?.allowed_actions ?? []"
          :reason="workflow?.pending_human_action?.reason"
          :missing-items="workflow?.pending_human_action?.missing_items"
          @completed="scheduleRefresh"
        />
      </el-tab-pane>
    </el-tabs>
  </div>

  <div v-else v-loading="!store.error" class="loading-page" aria-live="polite">
    {{ store.error ? `${store.error}，请刷新页面重试` : '正在加载审批工作台' }}
  </div>
</template>

<style scoped>
.workbench-page {
  max-width: none;
}

.workbench-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  padding: 2px 0 18px;
}

.back-link {
  display: inline-block;
  min-height: 44px;
  color: #475569;
  font-size: 12px;
  line-height: 44px;
  text-decoration: none;
}

.title-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.title-row h1 {
  margin: 0;
  font:
    700 23px/1.2 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
}

.workbench-header p {
  margin: 8px 0 0;
  color: #64748b;
  font-size: 13px;
}

.connection {
  display: flex;
  gap: 8px;
  align-items: center;
  color: #64748b;
  font-size: 12px;
}

.connection span {
  width: 8px;
  height: 8px;
  background: var(--color-warning);
  border-radius: 50%;
}

.connection span.online {
  background: var(--color-success);
}

.runtime-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  margin-bottom: 14px;
  background: var(--color-primary);
}

.retry-panel {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  margin-bottom: 14px;
  color: #7f1d1d;
  background: #fef2f2;
  border: 1px solid #fecaca;
}

.retry-panel strong,
.retry-panel span {
  display: block;
}

.retry-panel span {
  margin-top: 3px;
  font-size: 12px;
}

.runtime-strip div {
  padding: 11px 14px;
  border-right: 1px solid rgb(255 255 255 / 14%);
}

.runtime-strip span,
.runtime-strip strong {
  display: block;
}

.runtime-strip span {
  color: #bfdbfe;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.runtime-strip strong {
  margin-top: 3px;
  color: white;
  font:
    600 15px/1.2 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
}

.runtime-strip strong.warning {
  color: #fcd34d;
}

.workbench-grid {
  display: grid;
  grid-template-columns: minmax(250px, 0.8fr) minmax(390px, 1.35fr) minmax(280px, 0.95fr);
  gap: 14px;
  align-items: start;
}

.left-column,
.right-column {
  display: grid;
  gap: 14px;
}

.decision-panel article {
  padding: 10px 0;
  border-bottom: 1px solid #e2e8f0;
}

.decision-panel code {
  color: var(--color-action);
  font-size: 11px;
}

.decision-panel p {
  margin: 5px 0 0;
  color: #334155;
  font-size: 13px;
}

.loading-page {
  display: grid;
  min-height: 70vh;
  place-items: center;
  color: #64748b;
}

.mobile-tabs :deep(.el-tabs__header) {
  position: sticky;
  z-index: 5;
  top: 60px;
  padding-top: 8px;
  background: var(--color-background);
}

@media (width <= 1100px) {
  .workbench-grid {
    grid-template-columns: minmax(230px, 0.8fr) minmax(380px, 1.2fr);
  }

  .right-column {
    grid-column: 1 / -1;
    grid-template-columns: 1fr 1fr;
  }
}

@media (width <= 900px) {
  .workbench-header {
    display: grid;
    gap: 12px;
  }

  .runtime-strip {
    grid-template-columns: repeat(3, 1fr);
  }

  .runtime-strip div:nth-child(3) {
    border-right: 0;
  }
}

@media (width <= 600px) {
  .retry-panel {
    display: grid;
  }

  .title-row h1 {
    width: 100%;
    font-size: 18px;
    overflow-wrap: anywhere;
  }
}

@media (width <= 480px) {
  .runtime-strip {
    grid-template-columns: repeat(2, 1fr);
  }

  .runtime-strip div:nth-child(3) {
    border-right: 1px solid rgb(255 255 255 / 14%);
  }

  .runtime-strip div:nth-child(even) {
    border-right: 0;
  }
}
</style>

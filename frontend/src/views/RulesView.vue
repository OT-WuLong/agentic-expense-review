<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  approveCandidate,
  publishCandidate,
  rejectCandidate,
  rollbackCandidate,
  type CandidateRule,
} from '@/api/rules'
import { useSessionStore } from '@/stores/session'
import { useRuleStore } from '@/stores/rules'

const store = useRuleStore()
const session = useSessionStore()
const canManage = session.identity?.role === 'RULE_ADMIN'
const canRead = canManage || session.identity?.role === 'SYSTEM_ADMIN'
const dialog = ref(false)
const active = ref<CandidateRule | null>(null)
const busy = ref<number | null>(null)
const form = reactive({
  source_document_id: '',
  effective_from: '',
  parameters: '{}',
  replay_cases: '[\n  {"amount":"100.00","expected":"PASS"},\n  {"amount":"1000.00","expected":"FAIL"}\n]',
})

function openApproval(candidate: CandidateRule) {
  active.value = candidate
  form.source_document_id =
    store.rules.find((rule) => rule.rule_id === candidate.target_rule_id)?.source_document_id ?? ''
  form.effective_from = ''
  form.parameters = JSON.stringify(candidate.parameters, null, 2)
  const firstKey = Object.keys(candidate.parameters)[0]
  form.replay_cases = firstKey === 'days'
    ? '[\n  {"days":1,"expected":"PASS"},\n  {"days":90,"expected":"FAIL"}\n]'
    : firstKey === 'per_person'
      ? '[\n  {"amount":"100.00","attendees":2,"expected":"PASS"},\n  {"amount":"1000.00","attendees":2,"expected":"FAIL"}\n]'
      : firstKey === 'limit'
        ? '[\n  {"amount":"100.00","expected":"PASS"},\n  {"amount":"1000.00","expected":"FAIL"}\n]'
        : JSON.stringify(
            Object.keys(candidate.parameters).flatMap((tier) => [
              { tier, amount: '100.00', expected: 'PASS' },
              { tier, amount: '1000.00', expected: 'FAIL' },
            ]),
            null,
            2,
          )
  dialog.value = true
}

async function approve() {
  if (!active.value || !form.source_document_id || !form.effective_from) {
    ElMessage.error('请填写来源制度和未来生效日期')
    return
  }
  try {
    const parameters = JSON.parse(form.parameters) as Record<string, string | number>
    const replay_cases = JSON.parse(form.replay_cases) as Array<Record<string, unknown>>
    busy.value = active.value.candidate_id
    await approveCandidate(active.value.candidate_id, {
      source_document_id: form.source_document_id,
      effective_from: form.effective_from,
      parameters,
      replay_cases,
    })
    ElMessage.success('规则回放通过，候选已审核，尚未发布')
    dialog.value = false
    await store.load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '审核失败')
  } finally {
    busy.value = null
  }
}

async function act(candidate: CandidateRule, action: 'reject' | 'publish' | 'rollback') {
  const labels = { reject: '拒绝', publish: '发布', rollback: '回滚' }
  try {
    await ElMessageBox.confirm(`确认${labels[action]}候选 #${candidate.candidate_id}？`, '规则治理')
    busy.value = candidate.candidate_id
    if (action === 'reject') await rejectCandidate(candidate.candidate_id)
    if (action === 'publish') await publishCandidate(candidate.candidate_id)
    if (action === 'rollback') await rollbackCandidate(candidate.candidate_id)
    ElMessage.success(`${labels[action]}完成`)
    await store.load()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error instanceof Error ? error.message : '操作失败')
  } finally {
    busy.value = null
  }
}

onMounted(() => { if (canRead) void store.load() })
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <p class="eyebrow">Rule governance</p>
        <h1>规则治理</h1>
        <p>人工修改先生成候选；审核、回放、发布后才影响未来申请。</p>
      </div>
      <el-button v-if="canRead" @click="store.load()">刷新</el-button>
    </div>

      <el-alert v-if="!canManage" title="当前身份只能查看规则，不能审核或发布" type="info" :closable="false" />
    <template v-if="canRead">
      <el-alert v-if="store.error" :title="store.error" type="error" :closable="false" />
      <section class="panel section-gap">
        <div class="panel-heading">
          <div><p class="eyebrow">Candidates</p><h2>候选规则</h2></div>
          <span>{{ store.candidates.length }} 条</span>
        </div>
        <el-table v-loading="store.loading" :data="store.candidates" stripe empty-text="暂无人工修改产生的候选规则">
          <el-table-column prop="candidate_id" label="编号" width="80" />
          <el-table-column label="建议" min-width="280">
            <template #default="{ row }"><strong>{{ row.target_rule_id }}</strong><p class="muted">{{ row.summary }}</p></template>
          </el-table-column>
          <el-table-column label="范围" min-width="170">
            <template #default="{ row }">{{ row.department_id }} · {{ row.expense_type }}</template>
          </el-table-column>
          <el-table-column label="状态" width="135">
            <template #default="{ row }">{{ row.status }}{{ row.published_at ? ' · 已发布' : '' }}</template>
          </el-table-column>
          <el-table-column v-if="canManage" label="操作" min-width="190">
            <template #default="{ row }">
              <el-button v-if="row.status === 'PENDING'" link type="primary" @click="openApproval(row as CandidateRule)">审核</el-button>
              <el-button v-if="row.status === 'PENDING'" link type="danger" :loading="busy === row.candidate_id" @click="act(row as CandidateRule, 'reject')">拒绝</el-button>
              <el-button v-if="row.status === 'APPROVED' && !row.published_at" link type="primary" :loading="busy === row.candidate_id" @click="act(row as CandidateRule, 'publish')">发布</el-button>
              <el-button v-if="row.status === 'APPROVED' && row.published_at" link type="warning" :loading="busy === row.candidate_id" @click="act(row as CandidateRule, 'rollback')">回滚</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section class="panel">
        <div class="panel-heading"><div><p class="eyebrow">Published catalog</p><h2>正式规则</h2></div></div>
        <el-table v-loading="store.loading" :data="store.rules" stripe>
          <el-table-column prop="rule_id" label="规则 ID" min-width="210" />
          <el-table-column prop="rule_version" label="版本" width="100" />
          <el-table-column prop="expense_type" label="费用" width="90" />
          <el-table-column label="参数" min-width="170"><template #default="{ row }"><code>{{ JSON.stringify(row.parameters) }}</code></template></el-table-column>
          <el-table-column prop="source_document_id" label="来源制度" min-width="210" />
          <el-table-column label="有效期" min-width="190"><template #default="{ row }">{{ row.effective_from }} — {{ row.effective_to ?? '长期' }}</template></el-table-column>
          <el-table-column label="状态" width="90"><template #default="{ row }">{{ row.enabled ? '启用' : '停用' }}</template></el-table-column>
        </el-table>
      </section>
    </template>

    <el-dialog v-model="dialog" title="审核候选规则" width="min(680px, 94vw)">
      <el-alert title="请根据正式制度核对参数和生效日期；回放至少覆盖一个通过和一个不通过示例。" type="info" :closable="false" />
      <el-form label-position="top" class="review-form" @submit.prevent="approve">
        <el-form-item label="来源制度 ID" required><el-input v-model="form.source_document_id" /></el-form-item>
        <el-form-item label="未来生效日期" required><el-date-picker v-model="form.effective_from" type="date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="确认后的规则参数 JSON" required><el-input v-model="form.parameters" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="规则回放样例 JSON" required><el-input v-model="form.replay_cases" type="textarea" :rows="5" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="busy !== null" @click="approve">审核并回放</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.section-gap { margin-bottom: 18px; }
.muted { margin: 4px 0 0; color: #64748b; font-size: 12px; }
.review-form { margin-top: 18px; }
code { overflow-wrap: anywhere; font-size: 11px; }
</style>

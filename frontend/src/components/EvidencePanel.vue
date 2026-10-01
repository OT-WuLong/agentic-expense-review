<script setup lang="ts">
import { computed, ref } from 'vue'

import StatusTag from './StatusTag.vue'
import type { EvidenceItem, WorkflowState } from '@/types'

const props = defineProps<{
  evidence: EvidenceItem[]
  rules: WorkflowState['rule_results']
  conflictIds: string[]
}>()

const selected = ref<EvidenceItem | null>(null)
const conflicts = computed(() =>
  props.evidence.filter((item) => props.conflictIds.includes(item.evidence_id)),
)

function shortId(value: string) {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value
}
</script>

<template>
  <section class="panel evidence-panel" aria-labelledby="evidence-heading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">Grounded evidence</p>
        <h2 id="evidence-heading">证据与规则</h2>
      </div>
      <span class="count">{{ evidence.length }} 条</span>
    </div>

    <el-alert
      v-if="conflicts.length"
      title="检测到证据冲突，请核对具体内容"
      type="warning"
      :closable="false"
      show-icon
      class="conflict-alert"
    />
    <div v-if="conflicts.length" class="conflict-grid">
      <button
        v-for="item in conflicts"
        :key="item.evidence_id"
        type="button"
        @click="selected = item"
      >
        <strong>{{ item.document_id }}</strong>
        <span>v{{ item.version }} · P{{ item.page }}</span>
        <p>{{ item.excerpt }}</p>
      </button>
    </div>

    <div class="evidence-list">
      <button
        v-for="item in evidence"
        :key="item.evidence_id"
        class="evidence-item"
        type="button"
        @click="selected = item"
      >
        <span class="source-type">{{ item.source_type }}</span>
        <strong>{{ item.document_id ?? item.record_key ?? '结构化记录' }}</strong>
        <small
          >{{ item.section ?? item.query_type }} ·
          {{ item.page ? `P${item.page}` : '数据快照' }}</small
        >
        <code>{{ shortId(item.evidence_id) }}</code>
      </button>
      <el-empty v-if="!evidence.length" description="尚未形成有效证据" :image-size="64" />
    </div>

    <div v-if="rules.length" class="rules">
      <h3>确定性规则结果</h3>
      <div v-for="rule in rules" :key="rule.rule_id" class="rule-item">
        <div>
          <strong>{{ rule.rule_id }}</strong>
          <small>v{{ rule.rule_version }} · {{ rule.reason_code ?? '—' }}</small>
        </div>
        <StatusTag :value="rule.outcome" />
      </div>
    </div>

    <el-dialog
      :model-value="Boolean(selected)"
      title="证据详情"
      width="min(680px, 92vw)"
      @close="selected = null"
    >
      <template v-if="selected">
        <dl class="evidence-detail">
          <div>
            <dt>Evidence ID</dt>
            <dd>
              <code>{{ selected.evidence_id }}</code>
            </dd>
          </div>
          <div>
            <dt>来源</dt>
            <dd>{{ selected.source_type }}</dd>
          </div>
          <div>
            <dt>文档</dt>
            <dd>{{ selected.document_id ?? '—' }}</dd>
          </div>
          <div>
            <dt>版本 / 页码</dt>
            <dd>{{ selected.version ?? '—' }} / {{ selected.page ?? '—' }}</dd>
          </div>
          <div>
            <dt>章节</dt>
            <dd>{{ selected.section ?? selected.query_type ?? '—' }}</dd>
          </div>
          <div>
            <dt>有效期</dt>
            <dd>{{ selected.effective_from ?? '—' }} 至 {{ selected.effective_to ?? '长期' }}</dd>
          </div>
        </dl>
        <blockquote>{{ selected.excerpt ?? String(selected.value ?? '无文本片段') }}</blockquote>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.count {
  color: #64748b;
  font-size: 12px;
}

.conflict-alert {
  margin-bottom: 12px;
}

.conflict-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 14px;
}

.conflict-grid button,
.evidence-item {
  min-height: 44px;
  padding: 10px;
  text-align: left;
  cursor: pointer;
  background: white;
  border: 1px solid #dbe2ea;
}

.conflict-grid button {
  border-top: 3px solid var(--color-warning);
}

.conflict-grid strong,
.conflict-grid span,
.conflict-grid p {
  display: block;
}

.conflict-grid span {
  margin-top: 3px;
  color: #64748b;
  font-size: 11px;
}

.conflict-grid p {
  display: -webkit-box;
  margin: 8px 0 0;
  overflow: hidden;
  color: #475569;
  font-size: 12px;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.evidence-list {
  display: grid;
  gap: 7px;
}

.evidence-item {
  position: relative;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 8px;
}

.evidence-item:hover,
.conflict-grid button:hover {
  border-color: var(--color-action);
}

.evidence-item strong {
  min-width: 0;
  overflow: hidden;
  color: #1e293b;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.evidence-item small {
  min-width: 0;
  overflow: hidden;
  color: #64748b;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.evidence-item code {
  grid-row: 2;
  grid-column: 2;
  color: #64748b;
  font-size: 10px;
}

.source-type {
  position: absolute;
  top: -1px;
  right: -1px;
  padding: 2px 5px;
  color: #475569;
  font:
    9px/1.4 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
  background: #f1f5f9;
}

.rules {
  padding-top: 16px;
  margin-top: 18px;
  border-top: 1px solid #e2e8f0;
}

.rules h3 {
  margin: 0 0 8px;
  font-size: 13px;
}

.rule-item {
  display: flex;
  min-height: 50px;
  align-items: center;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid #e2e8f0;
}

.rule-item strong,
.rule-item small {
  display: block;
}

.rule-item strong {
  font:
    12px/1.4 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
}

.rule-item small {
  margin-top: 3px;
  color: #64748b;
}

.evidence-detail div {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid #e2e8f0;
}

.evidence-detail dt {
  color: #64748b;
}

.evidence-detail dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}

blockquote {
  padding: 14px;
  margin: 16px 0 0;
  color: #334155;
  background: #f8fafc;
  border-left: 3px solid var(--color-action);
}

@media (width <= 480px) {
  .conflict-grid {
    grid-template-columns: 1fr;
  }
}
</style>

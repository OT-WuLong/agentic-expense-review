<script setup lang="ts">
import type { AgentStep, AuditEvent } from '@/types'

defineProps<{ steps: AgentStep[]; events: AuditEvent[]; running: boolean }>()

const roleLabel: Record<string, string> = {
  SUPERVISOR: 'Supervisor',
  RETRIEVAL: 'Retrieval Agent',
  EVIDENCE_REVIEWER: 'Evidence Reviewer',
}
</script>

<template>
  <section class="panel timeline-panel" aria-labelledby="timeline-heading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">Agent orchestration</p>
        <h2 id="timeline-heading">执行轨迹</h2>
      </div>
      <span class="live-state" :class="{ active: running }">
        <span aria-hidden="true" />{{ running ? '实时运行' : '轨迹已同步' }}
      </span>
    </div>

    <ol v-if="steps.length" class="timeline">
      <li v-for="step in steps" :key="`${step.step_index}-${step.agent}`">
        <span class="step-index">{{ String(step.step_index).padStart(2, '0') }}</span>
        <div class="step-card">
          <div class="step-meta">
            <strong>{{ roleLabel[step.agent] ?? step.agent }}</strong>
            <span class="action">{{ step.action }}</span>
            <span>{{ step.latency_ms }}ms</span>
            <span>{{ step.tokens_used }} tokens</span>
          </div>
          <p>{{ step.thought_summary }}</p>
          <div v-if="step.tool_calls.length" class="tool-calls">
            <div
              v-for="call in step.tool_calls"
              :key="call.call_id ?? call.query"
              class="tool-call"
            >
              <span>{{ call.tool_name }}</span>
              <p>{{ call.purpose }}</p>
              <code>{{ call.query }}</code>
            </div>
          </div>
          <div v-if="step.new_evidence_ids.length" class="evidence-gain">
            +{{ step.new_evidence_ids.length }} 条新证据
          </div>
        </div>
      </li>
    </ol>

    <el-empty v-else description="工作流启动后，Agent 决策会显示在这里" :image-size="72" />

    <details v-if="events.length" class="event-log">
      <summary>系统事件（{{ events.length }}）</summary>
      <ul>
        <li v-for="event in events" :key="event.event_id">
          <code>#{{ event.event_id }}</code>
          <span>{{ event.event_type }}</span>
          <time>{{ new Date(event.created_at).toLocaleTimeString('zh-CN') }}</time>
        </li>
      </ul>
    </details>
  </section>
</template>

<style scoped>
.timeline-panel {
  min-height: 520px;
}

.timeline {
  padding: 4px 0 0;
  margin: 0;
  list-style: none;
}

.timeline li {
  position: relative;
  display: grid;
  grid-template-columns: 36px 1fr;
  gap: 12px;
  padding-bottom: 20px;
}

.timeline li::before {
  position: absolute;
  top: 28px;
  bottom: -2px;
  left: 17px;
  width: 1px;
  content: '';
  background: #cbd5e1;
}

.timeline li:last-child::before {
  display: none;
}

.step-index {
  position: relative;
  z-index: 1;
  display: grid;
  width: 34px;
  height: 28px;
  place-items: center;
  color: white;
  font:
    11px/1 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
  background: var(--color-primary);
  border-radius: 3px;
}

.step-card {
  min-width: 0;
  padding: 13px 14px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
}

.tool-calls,
.tool-call,
.tool-call p,
.tool-call code {
  min-width: 0;
  max-width: 100%;
}

.step-card > p {
  margin: 8px 0 0;
  color: #334155;
  font-size: 14px;
}

.step-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  align-items: center;
  color: #64748b;
  font-size: 12px;
}

.step-meta strong {
  color: #0f172a;
  font-size: 13px;
}

.action {
  padding: 2px 6px;
  color: #1d4ed8;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  background: #dbeafe;
}

.tool-calls {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.tool-call {
  padding: 10px;
  background: white;
  border-left: 3px solid var(--color-action);
}

.tool-call span {
  color: var(--color-action);
  font:
    600 12px/1 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
}

.tool-call p,
.tool-call code {
  display: block;
  margin: 5px 0 0;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.tool-call code {
  overflow: hidden;
  color: #64748b;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.evidence-gain {
  margin-top: 10px;
  color: var(--color-success);
  font-size: 12px;
  font-weight: 600;
}

.live-state {
  display: flex;
  gap: 6px;
  align-items: center;
  color: #64748b;
  font-size: 12px;
}

.live-state span {
  width: 8px;
  height: 8px;
  background: #94a3b8;
  border-radius: 50%;
}

.live-state.active span {
  background: var(--color-success);
  box-shadow: 0 0 0 4px #dcfce7;
}

.event-log {
  margin-top: 12px;
  color: #475569;
  font-size: 12px;
}

.event-log summary {
  min-height: 44px;
  cursor: pointer;
  line-height: 44px;
}

.event-log ul {
  padding: 0;
  margin: 0;
  list-style: none;
}

.event-log li {
  display: grid;
  grid-template-columns: 52px 1fr auto;
  gap: 8px;
  padding: 6px 0;
  border-top: 1px solid #e2e8f0;
}
</style>

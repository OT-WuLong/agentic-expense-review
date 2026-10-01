<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import StatusTag from '@/components/StatusTag.vue'
import { useApprovalStore } from '@/stores/approval'
import type { ApprovalListItem } from '@/types'

const store = useApprovalStore()
const router = useRouter()
const filters = reactive({ status: '', human_pending: false })
const pageSize = 50
const currentPage = ref(1)

const metrics = computed(() => ({
  total: store.total,
  running: store.items.filter((item) => item.status === 'RUNNING').length,
  human: store.items.filter((item) => item.status === 'HUMAN_PENDING').length,
  completed: store.items.filter((item) => item.status === 'COMPLETED').length,
}))

async function load() {
  await store.loadList({
    page: currentPage.value,
    page_size: pageSize,
    ...(filters.status ? { status: filters.status } : {}),
    ...(filters.human_pending ? { human_pending: true } : {}),
  })
}

function applyFilters() {
  currentPage.value = 1
  void load()
}

function changePage(page: number) {
  currentPage.value = page
  void load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <p class="eyebrow">Approval queue</p>
        <h1>费用预审批队列</h1>
        <p>统一查看运行状态、人工待办与最终建议。</p>
      </div>
      <el-button type="primary" class="primary-action" @click="router.push('/approvals/new')">
        新建审批
      </el-button>
    </div>

    <div class="metric-grid" aria-label="审批概览">
      <article>
        <span>全部申请</span><strong>{{ metrics.total }}</strong>
      </article>
      <article>
        <span>本页运行</span><strong>{{ metrics.running }}</strong>
      </article>
      <article>
        <span>本页待人工</span><strong>{{ metrics.human }}</strong>
      </article>
      <article>
        <span>本页完成</span><strong>{{ metrics.completed }}</strong>
      </article>
    </div>

    <section class="panel queue-panel" aria-labelledby="queue-heading">
      <div class="panel-heading queue-heading">
        <div>
          <p class="eyebrow">Live workload</p>
          <h2 id="queue-heading">审批任务</h2>
        </div>
        <div class="filters">
          <el-select
            v-model="filters.status"
            clearable
            placeholder="全部状态"
            aria-label="按状态筛选"
            @change="applyFilters"
          >
            <el-option label="运行中" value="RUNNING" />
            <el-option label="待人工" value="HUMAN_PENDING" />
            <el-option label="已完成" value="COMPLETED" />
            <el-option label="业务驳回" value="BUSINESS_REJECTED" />
            <el-option label="系统异常" value="SYSTEM_ERROR" />
          </el-select>
          <el-checkbox v-model="filters.human_pending" @change="applyFilters">仅人工待办</el-checkbox>
          <el-button @click="load">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="store.error" :title="store.error" type="error" show-icon :closable="false" />
      <el-table
        v-loading="store.loading"
        :data="store.items"
        stripe
        class="approval-table"
        row-key="request_id"
        @row-click="(row: ApprovalListItem) => router.push(`/approvals/${row.request_id}`)"
      >
        <el-table-column label="申请 / 申请人" min-width="220">
          <template #default="{ row }">
            <strong class="table-primary">{{ row.request_id }}</strong>
            <small class="table-secondary">{{ row.employee_id }} · {{ row.department_id }}</small>
          </template>
        </el-table-column>
        <el-table-column prop="expense_type" label="类型" width="90" />
        <el-table-column label="金额" width="130" align="right">
          <template #default="{ row }">{{ row.currency }} {{ row.amount }}</template>
        </el-table-column>
        <el-table-column prop="occurred_on" label="发生日期" width="120" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }"><StatusTag :value="row.status" /></template>
        </el-table-column>
        <el-table-column label="建议" width="130">
          <template #default="{ row }"><StatusTag :value="row.recommendation" /></template>
        </el-table-column>
        <el-table-column label="风险" width="100">
          <template #default="{ row }"><StatusTag :value="row.risk_level" /></template>
        </el-table-column>
        <el-table-column label="更新时间" min-width="160">
          <template #default="{ row }">{{
            new Date(row.updated_at).toLocaleString('zh-CN')
          }}</template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!store.loading && !store.items.length" description="没有符合条件的审批" />
      <el-pagination
        v-if="store.total > pageSize"
        class="queue-pagination"
        :current-page="currentPage"
        :page-size="pageSize"
        :total="store.total"
        layout="prev, pager, next, total"
        background
        aria-label="审批列表分页"
        @current-change="changePage"
      />
    </section>
  </div>
</template>

<style scoped>
.queue-heading {
  align-items: flex-start;
}

.filters {
  display: flex;
  gap: 10px;
  align-items: center;
}

.filters .el-select {
  width: 150px;
}

.approval-table :deep(.el-table__row) {
  cursor: pointer;
}

.queue-pagination {
  justify-content: flex-end;
  margin-top: 16px;
}

.table-primary,
.table-secondary {
  display: block;
}

.table-primary {
  color: var(--color-primary);
  font:
    600 12px/1.5 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
}

.table-secondary {
  margin-top: 2px;
  color: #64748b;
}

@media (width <= 768px) {
  .queue-heading,
  .filters {
    display: grid;
    width: 100%;
  }

  .filters .el-select {
    width: 100%;
  }

  .approval-table {
    min-width: 900px;
  }

  .queue-panel {
    overflow-x: auto;
  }
}
</style>

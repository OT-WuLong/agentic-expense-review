<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { uploadPolicy } from '@/api/policies'
import { useSessionStore } from '@/stores/session'
import { usePolicyStore } from '@/stores/policy'

const store = usePolicyStore()
const dialog = ref(false)
const uploading = ref(false)
const form = reactive({
  document_id: '',
  title: '',
  expense_type: '交通',
  version: '1.0',
  effective_from: '2026-01-01',
  file: null as File | null,
})
const session = useSessionStore()
const canUpload = session.identity?.role === 'RULE_ADMIN'

async function load() {
  await store.load()
}

function chooseFile(uploadFile: { raw?: File }) {
  form.file = uploadFile.raw ?? null
}

async function submit() {
  if (
    !form.file ||
    !form.document_id.trim() ||
    !form.title.trim() ||
    !form.version.trim() ||
    !form.effective_from
  ) {
    ElMessage.error('请完整填写制度元数据并选择 PDF')
    return
  }
  uploading.value = true
  try {
    await uploadPolicy({ ...form, file: form.file })
    ElMessage.success('制度已上传，状态为待解析入库')
    dialog.value = false
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '制度上传失败')
  } finally {
    uploading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <p class="eyebrow">Policy catalog</p>
        <h1>制度资料库</h1>
        <p>新申请可检索通用制度与适用的部门细则，并按部门、费用类型和发生日期过滤；已上传但待索引的文件不参与审批。</p>
      </div>
      <el-button v-if="canUpload" type="primary" class="primary-action" @click="dialog = true">
        上传制度
      </el-button>
    </div>

    <section class="panel">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">Catalog</p>
          <h2>制度清单</h2>
        </div>
        <span class="total">{{ store.items.length }} 份</span>
      </div>
      <el-alert v-if="store.error" :title="store.error" type="error" :closable="false" />
      <el-table v-loading="store.loading" :data="store.items" stripe>
        <el-table-column label="制度" min-width="300">
          <template #default="{ row }">
            <strong class="policy-title">{{ row.title }}</strong>
            <code>{{ row.document_id }}</code>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="120">
          <template #default="{ row }">{{
            row.expense_types?.join('、') ?? row.expense_type
          }}</template>
        </el-table-column>
        <el-table-column prop="version" label="版本" width="90" />
        <el-table-column label="适用部门" min-width="150">
          <template #default="{ row }">{{ row.department_ids?.join('、') ?? '待配置' }}</template>
        </el-table-column>
        <el-table-column label="有效期" min-width="180">
          <template #default="{ row }"
            >{{ row.effective_from }} — {{ row.effective_to ?? '长期' }}</template
          >
        </el-table-column>
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <span class="policy-status" :class="{ pending: row.status }">
              {{ row.published_status ?? row.status }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="dialog" title="上传企业制度" width="min(560px, 92vw)">
      <el-alert
        title="P10 只完成上传登记；解析、切片和重新建索引由后续离线任务执行。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="upload-form" @submit.prevent="submit">
        <el-form-item label="制度编号" required
          ><el-input v-model="form.document_id"
        /></el-form-item>
        <el-form-item label="制度名称" required><el-input v-model="form.title" /></el-form-item>
        <div class="dialog-grid">
          <el-form-item label="费用类型" required>
            <el-select v-model="form.expense_type" class="full-width">
              <el-option label="交通" value="交通" />
              <el-option label="住宿" value="住宿" />
              <el-option label="餐饮" value="餐饮" />
            </el-select>
          </el-form-item>
          <el-form-item label="版本" required><el-input v-model="form.version" /></el-form-item>
        </div>
        <el-form-item label="生效日期" required>
          <el-date-picker
            v-model="form.effective_from"
            type="date"
            value-format="YYYY-MM-DD"
            class="full-width"
          />
        </el-form-item>
        <el-form-item label="PDF 文件" required>
          <el-upload :auto-upload="false" :limit="1" accept=".pdf" :on-change="chooseFile">
            <el-button>选择 PDF</el-button>
          </el-upload>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :disabled="!form.file" :loading="uploading" @click="submit">
          上传并登记
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.total {
  color: #64748b;
  font-size: 12px;
}

.policy-title,
.policy-title + code {
  display: block;
}

.policy-title {
  font-size: 13px;
}

.policy-title + code {
  margin-top: 3px;
  color: #64748b;
  font-size: 10px;
}

.policy-status {
  display: inline-block;
  padding: 3px 7px;
  color: var(--color-success);
  font-size: 11px;
  font-weight: 600;
  background: #dcfce7;
}

.policy-status.pending {
  color: #92400e;
  background: #fef3c7;
}

.upload-form {
  margin-top: 18px;
}

.dialog-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.full-width {
  width: 100%;
}

@media (width <= 540px) {
  .dialog-grid {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>

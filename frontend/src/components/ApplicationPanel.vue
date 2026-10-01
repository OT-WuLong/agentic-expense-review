<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

import { uploadApprovalDocument } from '@/api/approvals'
import type { ApprovalDetail } from '@/types'

const props = defineProps<{ detail: ApprovalDetail }>()
const emit = defineEmits<{ uploaded: [] }>()

const file = ref<File | null>(null)
const uploading = ref(false)
const uploadError = ref('')
const documentType = ref('SUPPORTING_DOCUMENT')

function selectFile(uploadFile: { raw?: File }) {
  file.value = uploadFile.raw ?? null
  uploadError.value = ''
}

async function upload() {
  if (!file.value) return
  uploading.value = true
  uploadError.value = ''
  try {
    const documentId = `DOC-${props.detail.approval.request_id}-${Date.now()}`
    await uploadApprovalDocument(
      props.detail.approval.request_id,
      documentId,
      documentType.value,
      file.value,
    )
    ElMessage.success('附件已上传并写入审计日志')
    file.value = null
    emit('uploaded')
  } catch (error) {
    uploadError.value = error instanceof Error ? error.message : '附件上传失败，请重试'
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <section class="panel" aria-labelledby="application-heading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">Application</p>
        <h2 id="application-heading">申请信息</h2>
      </div>
    </div>
    <dl class="facts">
      <div>
        <dt>申请人</dt>
        <dd>{{ detail.approval.employee_id }}</dd>
      </div>
      <div>
        <dt>部门</dt>
        <dd>{{ detail.approval.department_id }}</dd>
      </div>
      <div>
        <dt>费用类型</dt>
        <dd>{{ detail.approval.expense_type }}</dd>
      </div>
      <div class="amount">
        <dt>报销金额</dt>
        <dd>{{ detail.approval.currency }} {{ detail.approval.amount }}</dd>
      </div>
      <div>
        <dt>发生日期</dt>
        <dd>{{ detail.approval.occurred_on }}</dd>
      </div>
      <div v-if="detail.approval.expense_type === '住宿'">
        <dt>住宿城市</dt>
        <dd>{{ detail.approval.application.city || '未填写' }}</dd>
      </div>
      <div v-if="detail.approval.expense_type === '住宿'">
        <dt>入住—退房</dt>
        <dd>
          {{ detail.approval.application.check_in || '未填写' }} 至
          {{ detail.approval.application.check_out || '未填写' }}
        </dd>
      </div>
      <div v-if="detail.approval.expense_type === '住宿'">
        <dt>房间数</dt>
        <dd>{{ detail.approval.application.room_count || '未填写' }}</dd>
      </div>
      <div v-if="detail.approval.expense_type === '餐饮'">
        <dt>申请参与人数</dt>
        <dd>{{ detail.approval.application.attendee_count ?? '未填写' }}</dd>
      </div>
      <div>
        <dt>提交日期</dt>
        <dd>{{ detail.approval.submitted_on }}</dd>
      </div>
      <div class="wide">
        <dt>事由</dt>
        <dd>{{ detail.approval.application.description }}</dd>
      </div>
    </dl>

    <div class="documents">
      <h3>附件（{{ detail.approval.documents.length }}）</h3>
      <div v-for="doc in detail.approval.documents" :key="doc.document_id" class="document-row">
        <span>{{ doc.document_type }}</span>
        <code>{{ doc.document_id }}</code>
        <small>{{ doc.synthetic ? '合成样例' : doc.media_type }}</small>
      </div>
      <el-empty v-if="!detail.approval.documents.length" description="暂无附件" :image-size="48" />
    </div>

    <details class="upload-box">
      <summary>事后补充附件（不重跑预审）</summary>
      <el-alert
        title="当前版本只保存附件和审计记录，不会自动解析或重新运行本次预审。"
        type="info"
        :closable="false"
      />
      <el-select v-model="documentType" aria-label="附件类型" class="full-width">
        <el-option label="补充说明" value="SUPPORTING_DOCUMENT" />
        <el-option label="发票" value="INVOICE" />
        <el-option label="行程单" value="ITINERARY" />
      </el-select>
      <el-upload
        :auto-upload="false"
        :limit="1"
        accept=".pdf,.png,.jpg,.jpeg"
        :on-change="selectFile"
      >
        <el-button>选择 PDF 或图片</el-button>
      </el-upload>
      <small>支持 PDF、PNG、JPEG，单个文件不超过 10 MB。</small>
      <el-button
        type="primary"
        :disabled="!file"
        :loading="uploading"
        class="full-width"
        @click="upload"
      >
        上传附件
      </el-button>
      <el-alert v-if="uploadError" :title="uploadError" type="error" show-icon :closable="false" />
    </details>
  </section>
</template>

<style scoped>
.facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 0;
}

.facts div {
  min-width: 0;
}

.facts dt {
  color: #64748b;
  font-size: 11px;
}

.facts dd {
  margin: 3px 0 0;
  overflow-wrap: anywhere;
  color: #1e293b;
  font-size: 13px;
  font-weight: 600;
}

.facts .amount dd {
  color: var(--color-primary);
  font-size: 17px;
}

.facts .wide {
  grid-column: 1 / -1;
}

.documents {
  padding-top: 16px;
  margin-top: 18px;
  border-top: 1px solid #e2e8f0;
}

.documents h3 {
  margin: 0 0 8px;
  font-size: 13px;
}

.document-row {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px 8px;
  padding: 8px 0;
  border-bottom: 1px solid #e2e8f0;
}

.document-row span {
  font-size: 12px;
  font-weight: 600;
}

.document-row code {
  overflow: hidden;
  color: #475569;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.document-row small {
  grid-column: 1 / -1;
  color: #64748b;
}

.upload-box {
  margin-top: 14px;
}

.upload-box summary {
  min-height: 44px;
  cursor: pointer;
  color: var(--color-action);
  font-weight: 600;
  line-height: 44px;
}

.upload-box > * + * {
  margin-top: 10px;
}

.full-width {
  width: 100%;
  min-height: 44px;
}
</style>

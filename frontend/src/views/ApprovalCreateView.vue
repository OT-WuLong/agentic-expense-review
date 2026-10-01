<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules, UploadFile, UploadInstance } from 'element-plus'
import { useRouter } from 'vue-router'

import { createFixture, createManualWithInvoice, fetchApproval, fetchFixtures } from '@/api/approvals'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()
const isApplicant = session.identity?.role === 'APPLICANT'
const tab = ref(isApplicant ? 'manual' : 'demo')
const fixtures = ref<
  Array<{
    case_id: string
    title: string
    expected_recommendation: string
  }>
>([])
const submitting = ref(false)
const formRef = ref<FormInstance>()
const invoiceUpload = ref<UploadInstance>()
const invoice = ref<File | null>(null)
const form = reactive({
  employee_id: isApplicant ? (session.identity?.actor_id ?? '') : 'EMP-DEMO-001',
  department_id: isApplicant ? (session.identity?.department_ids[0] ?? '') : 'DEPT-SYN-RD',
  display_name: isApplicant ? (session.identity?.display_name ?? '') : '演示员工',
  expense_type: '交通',
  amount: '68.00',
  occurred_on: '',
  submitted_on: '',
  description: '工作相关费用报销',
  city: '',
  stay_dates: [] as string[],
  room_count: 1,
  attendee_count: 1,
})
const initialForm = JSON.stringify(form)
const rules: FormRules<typeof form> = {
  employee_id: [{ required: true, message: '请输入员工编号', trigger: 'blur' }],
  department_id: [{ required: true, message: '请输入部门编号', trigger: 'blur' }],
  display_name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  expense_type: [{ required: true, message: '请选择费用类型', trigger: 'change' }],
  amount: [
    { required: true, message: '请输入金额', trigger: 'blur' },
    { pattern: /^\d+(\.\d{1,2})?$/, message: '金额最多保留两位小数', trigger: 'blur' },
  ],
  occurred_on: [{ required: true, message: '请选择发生日期', trigger: 'change' }],
  submitted_on: [{ required: true, message: '请选择提交日期', trigger: 'change' }],
  description: [{ required: true, message: '请输入费用事由', trigger: 'blur' }],
  city: [
    {
      required: true,
      validator: (_rule, value, callback) => {
        if (form.expense_type === '住宿' && !String(value ?? '').trim()) callback(new Error('请输入住宿城市'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
  stay_dates: [
    {
      required: true,
      validator: (_rule, value, callback) => {
        if (form.expense_type !== '住宿') return callback()
        if (!Array.isArray(value) || value.length !== 2) callback(new Error('请选择入住和退房日期'))
        else if (value[1] <= value[0]) callback(new Error('退房日期须晚于入住日期'))
        else callback()
      },
      trigger: 'change',
    },
  ],
  room_count: [{ required: true, type: 'number', min: 1, message: '房间数至少为 1', trigger: 'change' }],
}

function pendingKey(kind: string) {
  return `approval-pending:${session.identity?.actor_id ?? 'unknown'}:${kind}`
}

async function manualSignature() {
  if (!crypto.subtle) throw new Error('请通过 localhost 或 HTTPS 提交发票')
  const formBytes = new TextEncoder().encode(JSON.stringify(form))
  const invoiceBytes = new Uint8Array(await invoice.value!.arrayBuffer())
  const bytes = new Uint8Array(formBytes.length + invoiceBytes.length)
  bytes.set(formBytes)
  bytes.set(invoiceBytes, formBytes.length)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function pendingManualEntries() {
  const prefix = `${pendingKey('manual')}:`
  const entries: Array<{ key: string; requestId: string }> = []
  for (let index = 0; index < window.localStorage.length; index++) {
    const key = window.localStorage.key(index)
    if (!key?.startsWith(prefix)) continue
    const requestId = window.localStorage.getItem(key)
    if (requestId) entries.push({ key, requestId })
  }
  return entries
}

async function openExistingRequest(requestId: string, key: string): Promise<boolean> {
  try {
    await fetchApproval(requestId)
    await router.push(`/approvals/${requestId}`)
    window.localStorage.removeItem(key)
    ElMessage.info('上次申请已创建，已打开原申请')
    return true
  } catch (error) {
    if ((error as { response?: { status?: number } })?.response?.status === 404) return false
    ElMessage.error(`暂时无法确认申请 ${requestId} 是否已创建，请稍后重试，避免重复提交`)
    return true
  }
}

async function startFixture(caseId: string) {
  if (submitting.value) return
  submitting.value = true
  try {
    const key = pendingKey(`fixture:${caseId}`)
    const pending = window.localStorage.getItem(key)
    if (pending && await openExistingRequest(pending, key)) return
    const requestId = pending ?? `REQ-UI-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`
    window.localStorage.setItem(key, requestId)
    const created = await createFixture(caseId, requestId)
    await router.push(`/approvals/${created.request_id}`)
    window.localStorage.removeItem(key)
    ElMessage.success('审批已创建，Agent 正在后台运行')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '创建审批失败')
  } finally {
    submitting.value = false
  }
}

function selectInvoice(uploadFile: UploadFile) {
  const selected = uploadFile.raw
  if (!selected) return
  if (!/\.(pdf|png|jpe?g)$/i.test(selected.name) || selected.size > 10 * 1024 * 1024) {
    ElMessage.error('发票仅支持 PDF、PNG、JPG，且不能超过 10 MB')
    invoice.value = null
    invoiceUpload.value?.clearFiles()
    return
  }
  invoice.value = selected
}

function clearInvoice() {
  invoice.value = null
}

async function submitManual() {
  if (submitting.value) return
  submitting.value = true
  try {
    const pending = pendingManualEntries()
    if (
      pending.length === 1 && !invoice.value && JSON.stringify(form) === initialForm &&
      await openExistingRequest(pending[0]!.requestId, pending[0]!.key)
    ) return
    const valid = await formRef.value?.validate().catch(() => false)
    if (!valid) return
    if (!invoice.value) {
      ElMessage.error('请先选择发票')
      return
    }
    const signature = await manualSignature()
    const key = `${pendingKey('manual')}:${signature}`
    const matching = window.localStorage.getItem(key)
    if (matching && await openExistingRequest(matching, key)) return
    const requestId = matching ?? `REQ-UI-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`
    window.localStorage.setItem(key, requestId)
    const application: Record<string, unknown> = {
      expense_type: form.expense_type,
      currency: 'CNY',
      amount: form.amount,
      occurred_on: form.expense_type === '住宿' ? form.stay_dates[0] : form.occurred_on,
      submitted_on: form.submitted_on,
      description: form.description,
    }
    if (form.expense_type === '住宿') {
      application.city = form.city.trim()
      application.check_in = form.stay_dates[0]
      application.check_out = form.stay_dates[1]
      application.room_count = form.room_count
    }
    if (form.expense_type === '餐饮') application.attendee_count = form.attendee_count
    if (form.expense_type === '交通') {
      application.transport_purpose = 'BUSINESS'
      application.origin_type = 'BUSINESS_LOCATION'
      application.destination_type = 'BUSINESS_LOCATION'
    }
    const created = await createManualWithInvoice({
      request_id: requestId,
      applicant: {
        employee_id: form.employee_id,
        department_id: form.department_id,
        display_name: form.display_name,
      },
      application,
      documents: [],
    }, invoice.value)
    await router.push(`/approvals/${created.request_id}`)
    window.localStorage.removeItem(key)
    ElMessage.success('发票已提交，解析完成后将启动 Agent')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '创建审批失败')
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  if (!isApplicant) fixtures.value = await fetchFixtures()
})
</script>

<template>
  <div class="page narrow-page">
    <div class="page-header">
      <div>
        <p class="eyebrow">New approval</p>
        <h1>发起费用预审</h1>
        <p>使用冻结案例演示完整链路，或录入一笔真实结构的申请。</p>
      </div>
    </div>

    <section class="panel create-panel">
      <el-tabs v-model="tab">
        <el-tab-pane v-if="!isApplicant" label="黄金案例演示" name="demo">
          <div class="fixture-note">
            <strong>推荐用于项目演示</strong>
            <span>案例包含可定位附件、制度与结构化数据，可稳定展示多跳检索和人工中断。</span>
          </div>
          <div class="fixture-grid">
            <article v-for="(item, index) in fixtures" :key="item.case_id">
              <span class="fixture-index">0{{ index + 1 }}</span>
              <h2>{{ item.title }}</h2>
              <code>{{ item.case_id }}</code>
              <p>
                预期：<strong>{{ item.expected_recommendation }}</strong>
              </p>
              <el-button
                type="primary"
                :loading="submitting"
                class="full-width"
                @click="startFixture(item.case_id)"
              >
                启动此案例
              </el-button>
            </article>
          </div>
        </el-tab-pane>

        <el-tab-pane label="手工录入" name="manual">
          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            label-position="top"
            class="manual-form"
            @submit.prevent="submitManual"
          >
            <div class="form-grid">
              <el-form-item label="员工编号" prop="employee_id">
                <el-input v-model="form.employee_id" :disabled="isApplicant" />
              </el-form-item>
              <el-form-item label="姓名" prop="display_name">
                <el-input v-model="form.display_name" :disabled="isApplicant" />
              </el-form-item>
              <el-form-item label="部门编号" prop="department_id">
                <el-input v-model="form.department_id" :disabled="isApplicant" />
              </el-form-item>
              <el-form-item label="费用类型" prop="expense_type">
                <el-select v-model="form.expense_type" class="full-width">
                  <el-option label="交通" value="交通" />
                  <el-option label="住宿" value="住宿" />
                  <el-option label="餐饮" value="餐饮" />
                </el-select>
              </el-form-item>
              <el-form-item label="金额（CNY）" prop="amount">
                <el-input v-model="form.amount" inputmode="decimal" />
              </el-form-item>
              <el-form-item v-if="form.expense_type === '住宿'" label="城市" prop="city">
                <el-input v-model="form.city" />
              </el-form-item>
              <el-form-item v-if="form.expense_type === '住宿'" label="房间数" prop="room_count">
                <el-input-number v-model="form.room_count" :min="1" :precision="0" class="full-width" />
              </el-form-item>
              <el-form-item v-if="form.expense_type === '餐饮'" label="参与人数" required>
                <el-input-number v-model="form.attendee_count" :min="1" class="full-width" />
              </el-form-item>
              <el-form-item v-if="form.expense_type === '住宿'" label="入住—退房日期" prop="stay_dates" class="wide">
                <el-date-picker
                  v-model="form.stay_dates"
                  type="daterange"
                  value-format="YYYY-MM-DD"
                  range-separator="至"
                  start-placeholder="入住日期"
                  end-placeholder="退房日期"
                  class="full-width"
                />
              </el-form-item>
              <el-form-item v-else label="发生日期" prop="occurred_on">
                <el-date-picker
                  v-model="form.occurred_on"
                  type="date"
                  value-format="YYYY-MM-DD"
                  class="full-width"
                />
              </el-form-item>
              <el-form-item label="提交日期" prop="submitted_on">
                <el-date-picker
                  v-model="form.submitted_on"
                  type="date"
                  value-format="YYYY-MM-DD"
                  class="full-width"
                />
                <small v-if="form.expense_type === '住宿'" class="form-hint">
                  退房后超过 30 天提交会转人工，需补充延迟原因和有权限人员的处理记录。
                </small>
              </el-form-item>
              <el-form-item label="费用事由" prop="description" class="wide">
                <el-input v-model="form.description" type="textarea" :rows="3" />
              </el-form-item>
              <el-form-item label="发票" required class="wide">
                <div class="invoice-upload">
                  <el-upload
                    ref="invoiceUpload"
                    :auto-upload="false"
                    :limit="1"
                    accept=".pdf,.png,.jpg,.jpeg"
                    :on-change="selectInvoice"
                    :on-remove="clearInvoice"
                  >
                    <el-button>选择发票（PDF 或图片）</el-button>
                  </el-upload>
                  <p>最多 10 MB。文件会发送至 MinerU 云端解析；请只使用合成或获授权票据。交通类首版按出租车票据核验。</p>
                </div>
              </el-form-item>
            </div>
            <div class="form-actions">
              <span>先解析发票，再启动 Agent；事后补传附件不会自动重跑。</span>
              <el-button type="primary" native-type="submit" :loading="submitting">
                提交发票并启动预审
              </el-button>
            </div>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </section>
  </div>
</template>

<style scoped>
.narrow-page {
  max-width: 1120px;
  margin: auto;
}

.create-panel {
  padding: 22px;
}

.fixture-note {
  display: flex;
  gap: 12px;
  padding: 12px 14px;
  margin: 8px 0 18px;
  color: #1e3a5f;
  font-size: 13px;
  background: #eff6ff;
  border-left: 3px solid var(--color-action);
}

.fixture-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

.fixture-grid article {
  position: relative;
  min-width: 0;
  padding: 18px;
  background: #fff;
  border: 1px solid #cbd5e1;
  border-top: 3px solid var(--color-primary);
}

.fixture-grid h2,
.fixture-grid code,
.fixture-grid p {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
}

.fixture-index {
  color: #94a3b8;
  font:
    12px/1 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
}

.fixture-grid h2 {
  min-height: 52px;
  margin: 12px 0 6px;
  font-size: 17px;
  line-height: 1.5;
}

.fixture-grid code {
  display: block;
  overflow: hidden;
  color: #64748b;
  font-size: 10px;
  text-overflow: ellipsis;
}

.fixture-grid p {
  min-height: 42px;
  color: #64748b;
  font-size: 12px;
}

.fixture-grid p strong {
  color: #334155;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 18px;
  padding-top: 14px;
}

.form-grid .wide {
  grid-column: 1 / -1;
}

.full-width {
  width: 100%;
  min-height: 44px;
}

.invoice-upload p {
  margin: 8px 0 0;
  color: #64748b;
  font-size: 12px;
  line-height: 1.5;
}

.form-hint {
  display: block;
  margin-top: 6px;
  color: #64748b;
  font-size: 12px;
  line-height: 1.5;
}

.form-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 16px;
  border-top: 1px solid #e2e8f0;
}

.form-actions span {
  color: #64748b;
  font-size: 12px;
}

.form-actions .el-button {
  min-height: 44px;
}

@media (width <= 900px) {
  .fixture-grid {
    grid-template-columns: 1fr;
  }

  .fixture-grid h2,
  .fixture-grid p {
    min-height: 0;
  }
}

@media (width <= 600px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .form-grid .wide {
    grid-column: auto;
  }

  .fixture-note,
  .form-actions {
    display: grid;
    gap: 10px;
  }
}
</style>

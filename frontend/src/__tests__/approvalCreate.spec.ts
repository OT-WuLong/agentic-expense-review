import { webcrypto } from 'node:crypto'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ApprovalCreateView from '@/views/ApprovalCreateView.vue'

const mocks = vi.hoisted(() => ({
  push: vi.fn<(path: string) => Promise<void>>(),
  createFixture: vi.fn<(caseId: string, requestId?: string) => Promise<{ request_id: string; status: string }>>(),
  fetchApproval: vi.fn<(requestId: string) => Promise<unknown>>(),
  fetchFixtures: vi.fn<() => Promise<Array<{ case_id: string; title: string; expected_recommendation: string }>>>(),
  createManualWithInvoice: vi.fn<
    (request: Record<string, unknown>, invoice: File) => Promise<{ request_id: string; status: string }>
  >(),
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('@/api/approvals', () => ({
  createFixture: mocks.createFixture,
  fetchApproval: mocks.fetchApproval,
  createManualWithInvoice: mocks.createManualWithInvoice,
  fetchFixtures: mocks.fetchFixtures,
}))

describe('approval create form', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    window.sessionStorage.clear()
    Object.defineProperty(window.crypto, 'subtle', { configurable: true, value: webcrypto.subtle })
    if (!File.prototype.arrayBuffer) {
      Object.defineProperty(File.prototype, 'arrayBuffer', {
        configurable: true,
        value(this: File) {
          return new Promise<ArrayBuffer>((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => resolve(reader.result as ArrayBuffer)
            reader.onerror = () => reject(reader.error)
            reader.readAsArrayBuffer(this)
          })
        },
      })
    }
    mocks.fetchApproval.mockReset().mockRejectedValue({ response: { status: 404 } })
    mocks.fetchFixtures.mockResolvedValue([])
    mocks.createManualWithInvoice.mockResolvedValue({ request_id: 'REQ-FORM-1', status: 'RUNNING' })
  })

  it('submits a structured request and navigates to its workbench', async () => {
    const wrapper = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await wrapper.findAll('.el-tabs__item')[1]!.trigger('click')
    const state = (
      wrapper.vm.$ as unknown as {
        setupState: {
          form: { occurred_on: string; submitted_on: string }
          invoice: File | null
        }
      }
    ).setupState
    state.form.occurred_on = '2026-05-01'
    state.form.submitted_on = '2026-05-02'
    state.invoice = new File(['synthetic invoice'], 'invoice.pdf', { type: 'application/pdf' })
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.createManualWithInvoice).toHaveBeenCalledTimes(1)
    expect(mocks.createManualWithInvoice.mock.calls[0]![0]).toMatchObject({
      applicant: { employee_id: 'EMP-DEMO-001', department_id: 'DEPT-SYN-RD' },
      application: {
        expense_type: '交通',
        amount: '68.00',
        occurred_on: '2026-05-01',
        submitted_on: '2026-05-02',
      },
      documents: [],
    })
    expect(mocks.createManualWithInvoice.mock.calls[0]![1].name).toBe('invoice.pdf')
    expect(mocks.push).toHaveBeenCalledWith('/approvals/REQ-FORM-1')
  })

  it('reuses the demo request ID after a lost response', async () => {
    mocks.fetchFixtures.mockResolvedValue([
      { case_id: 'GC-A-TRANSPORT-COMMUTE-REJECT', title: '通勤', expected_recommendation: 'REJECT_RECOMMENDED' },
    ])
    mocks.createFixture.mockRejectedValue(new Error('timeout'))
    const wrapper = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()

    await wrapper.get('.fixture-grid button').trigger('click')
    await flushPromises()
    await wrapper.get('.fixture-grid button').trigger('click')
    await flushPromises()

    expect(mocks.createFixture.mock.calls[0]![1]).toMatch(/^REQ-UI-/)
    expect(mocks.createFixture.mock.calls[1]![1]).toBe(mocks.createFixture.mock.calls[0]![1])
  })

  it('reuses the manual request ID after a lost response', async () => {
    mocks.createManualWithInvoice.mockRejectedValue(new Error('timeout'))
    const wrapper = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await wrapper.findAll('.el-tabs__item')[1]!.trigger('click')
    const state = (
      wrapper.vm.$ as unknown as {
        setupState: {
          form: { occurred_on: string; submitted_on: string }
          invoice: File | null
          submitting: boolean
        }
      }
    ).setupState
    state.form.occurred_on = '2026-05-01'
    state.form.submitted_on = '2026-05-02'
    state.invoice = new File(['synthetic invoice'], 'invoice.pdf', { type: 'application/pdf' })

    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await vi.waitFor(() => expect(mocks.createManualWithInvoice).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(state.submitting).toBe(false))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await vi.waitFor(() => expect(mocks.createManualWithInvoice).toHaveBeenCalledTimes(2))

    expect(mocks.createManualWithInvoice.mock.calls[0]![0].request_id).toMatch(/^REQ-UI-/)
    expect(mocks.createManualWithInvoice.mock.calls[1]![0].request_id).toBe(
      mocks.createManualWithInvoice.mock.calls[0]![0].request_id,
    )
  })

  it('checks a demo request after reopening the form instead of creating another', async () => {
    mocks.fetchFixtures.mockResolvedValue([
      { case_id: 'GC-A-TRANSPORT-COMMUTE-REJECT', title: '通勤', expected_recommendation: 'REJECT_RECOMMENDED' },
    ])
    mocks.createFixture.mockRejectedValue(new Error('timeout'))
    const first = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await first.get('.fixture-grid button').trigger('click')
    await flushPromises()
    const requestId = mocks.createFixture.mock.calls[0]![1]
    first.unmount()
    mocks.fetchApproval.mockResolvedValue({ approval: { request_id: requestId } })

    const reopened = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await reopened.get('.fixture-grid button').trigger('click')
    await flushPromises()

    expect(mocks.fetchApproval).toHaveBeenCalledWith(requestId)
    expect(mocks.createFixture).toHaveBeenCalledTimes(1)
    expect(mocks.push).toHaveBeenCalledWith(`/approvals/${requestId}`)
  })

  it('checks a manual request after reopening even before reselecting the invoice', async () => {
    mocks.createManualWithInvoice.mockRejectedValue(new Error('timeout'))
    const first = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await first.findAll('.el-tabs__item')[1]!.trigger('click')
    const state = (
      first.vm.$ as unknown as {
        setupState: {
          form: { occurred_on: string; submitted_on: string }
          invoice: File | null
        }
      }
    ).setupState
    state.form.occurred_on = '2026-05-01'
    state.form.submitted_on = '2026-05-02'
    state.invoice = new File(['synthetic invoice'], 'invoice.pdf', { type: 'application/pdf' })
    await first.get('form').trigger('submit')
    await flushPromises()
    const requestId = String(mocks.createManualWithInvoice.mock.calls[0]![0].request_id)
    first.unmount()
    window.sessionStorage.clear() // A closed tab loses session storage.
    mocks.fetchApproval.mockResolvedValue({ approval: { request_id: requestId } })

    const reopened = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await reopened.findAll('.el-tabs__item')[1]!.trigger('click')
    await reopened.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.fetchApproval).toHaveBeenCalledWith(requestId)
    expect(mocks.createManualWithInvoice).toHaveBeenCalledTimes(1)
    expect(mocks.push).toHaveBeenCalledWith(`/approvals/${requestId}`)
  })

  it('keeps different manual drafts in separate tabs from sharing a request ID', async () => {
    mocks.createManualWithInvoice.mockRejectedValue(new Error('timeout'))
    const first = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await first.findAll('.el-tabs__item')[1]!.trigger('click')
    const firstState = (
      first.vm.$ as unknown as {
        setupState: {
          form: { occurred_on: string; submitted_on: string }
          invoice: File | null
        }
      }
    ).setupState
    firstState.form.occurred_on = '2026-05-01'
    firstState.form.submitted_on = '2026-05-02'
    firstState.invoice = new File(['invoice A'], 'invoice.pdf', {
      type: 'application/pdf', lastModified: 123456,
    })
    await first.get('form').trigger('submit')
    await flushPromises()
    const firstId = mocks.createManualWithInvoice.mock.calls[0]![0].request_id
    first.unmount()

    // A duplicated tab can inherit the first tab's session storage snapshot.
    const second = mount(ApprovalCreateView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    await second.findAll('.el-tabs__item')[1]!.trigger('click')
    const secondState = (
      second.vm.$ as unknown as {
        setupState: {
          form: { occurred_on: string; submitted_on: string }
          invoice: File | null
        }
      }
    ).setupState
    secondState.form.occurred_on = '2026-05-01'
    secondState.form.submitted_on = '2026-05-02'
    secondState.invoice = new File(['invoice B'], 'invoice.pdf', {
      type: 'application/pdf', lastModified: 123456,
    })
    await second.get('form').trigger('submit')
    await flushPromises()
    await vi.waitFor(() => expect(mocks.createManualWithInvoice).toHaveBeenCalledTimes(2))

    expect(mocks.createManualWithInvoice.mock.calls[1]![0].request_id).not.toBe(firstId)
  })
})

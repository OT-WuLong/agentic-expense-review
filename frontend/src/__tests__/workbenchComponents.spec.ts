import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import EvidencePanel from '@/components/EvidencePanel.vue'
import HumanReviewPanel from '@/components/HumanReviewPanel.vue'
import { submitHumanReview } from '@/api/approvals'

vi.mock('@/api/approvals', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/approvals')>()
  return { ...original, submitHumanReview: vi.fn<typeof original.submitHumanReview>() }
})

describe('workbench components', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('opens a traceable evidence detail from an Evidence ID', async () => {
    const wrapper = mount(EvidencePanel, {
      props: {
        evidence: [
          {
            evidence_id: 'EVID-001',
            source_type: 'POLICY_DOCUMENT',
            document_id: 'POL-001',
            version: '2.0',
            page: 4,
            section: '第六章',
            excerpt: '每人每日报销上限为 150 元。',
            effective_from: '2026-06-01',
          },
        ],
        rules: [],
        conflictIds: [],
      },
      attachTo: document.body,
    })

    await wrapper.get('.evidence-item').trigger('click')
    await flushPromises()

    expect(document.body.textContent).toContain('Evidence ID')
    expect(document.body.textContent).toContain('POL-001')
    expect(document.body.textContent).toContain('每人每日报销上限为 150 元。')
    wrapper.unmount()
  })

  it('disables duplicate human-review submission while the first request is pending', async () => {
    let resolveRequest: (value: object) => void = () => undefined
    vi.mocked(submitHumanReview).mockReturnValue(
      new Promise((resolve) => {
        resolveRequest = resolve
      }),
    )
    const wrapper = mount(HumanReviewPanel, {
      props: {
        requestId: 'REQ-1',
        visible: true,
        recommendation: 'HUMAN_REVIEW',
        riskLevel: 'HIGH',
        rules: [],
        allowedActions: ['APPROVE', 'EDIT', 'REJECT'],
      },
    })
    await wrapper.get('textarea').setValue('已人工核实制度冲突，按审慎原则处理。')
    const reviewState = (
      wrapper.vm.$ as unknown as { setupState: { form: { action: string } } }
    ).setupState
    reviewState.form.action = 'APPROVE'
    const form = wrapper.get('form')

    await form.trigger('submit')
    await form.trigger('submit')

    expect(submitHumanReview).toHaveBeenCalledTimes(1)
    resolveRequest({})
    await flushPromises()
  })

  it('reuses the review key when retrying an uncertain submission', async () => {
    vi.mocked(submitHumanReview).mockRejectedValue(new Error('timeout'))
    const wrapper = mount(HumanReviewPanel, {
      props: {
        requestId: 'REQ-REVIEW-RETRY',
        visible: true,
        allowedActions: ['APPROVE'],
      },
    })
    const reviewState = (
      wrapper.vm.$ as unknown as { setupState: { form: { action: string } } }
    ).setupState
    reviewState.form.action = 'APPROVE'
    await wrapper.get('textarea').setValue('已经核对申请材料。')

    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const firstKey = vi.mocked(submitHumanReview).mock.calls[0]![1].idempotency_key
    expect(vi.mocked(submitHumanReview).mock.calls[1]![1].idempotency_key).toBe(firstKey)
  })

  it('only offers server-allowed actions and states that approve means pass', async () => {
    const wrapper = mount(HumanReviewPanel, {
      props: {
        requestId: 'REQ-MISSING-DOCS',
        visible: true,
        recommendation: 'HUMAN_REVIEW',
        riskLevel: 'MEDIUM',
        rules: [
          {
            rule_id: 'RULE-DOCUMENT',
            rule_version: '1',
            outcome: 'INDETERMINATE',
            evidence_ids: [],
          },
        ],
        allowedActions: ['EDIT', 'REJECT'],
      },
    })

    expect(wrapper.text()).not.toContain('人工通过')
    expect(wrapper.text()).not.toContain('建议通过')
    expect(wrapper.text()).toContain('修改结论')
    expect(wrapper.text()).toContain('人工驳回')
    expect(wrapper.text()).toContain('待确认 1')
  })
})

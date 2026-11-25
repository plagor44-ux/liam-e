// i18n-exempt: synthetic fixtures.
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { ChatMessageOut, RegisteredToolOut, SkillOut } from '../api/generated/types.gen'
import { de } from '../i18n/de'
import { ProposalCard } from './ProposalCard'
import { ChatSessionProvider, useChatSession } from './session'

const t = de.skillProposal
const draft = {
  slug: 'mail-check', name: 'Mail-Check', description: 'Prüft neue Mails.', instructions: 'Suche ungelesene Mails.',
  allowed_tools: ['search_messages', 'propose_action'],
  workflow_steps: [{ id: 'find', title: 'Suchen', description: 'Ungelesene Mails finden.', tool_names: ['search_messages'] }],
}
const tools: RegisteredToolOut[] = [
  { name: 'search_messages', kind: 'read', description: 'Search.', skill_only: false, authorable: true },
  { name: 'propose_action', kind: 'propose', description: 'Propose.', skill_only: false, authorable: true },
]
const saved: SkillOut = {
  id: 'sk-new', slug: 'mail-pruefung', origin: 'custom', status: 'active', name: 'Mail-Prüfung', description: 'Prüft neue Mails.',
  version: 1, revision_id: 'skrev-new-1', instructions: 'Suche ungelesene Mails.', workflow_steps: [], allowed_tools: ['search_messages'],
  content_hash: 'h-new', updated_at: '2026-11-25T08:00:00Z',
}

function card(status = 'pending', extra: Partial<ChatMessageOut['proposal'] & object> = {}): ChatMessageOut {
  return { id: 'p', conversation_id: 'c', role: 'proposal', content: '', tool_name: 'propose_skill', created_at: '', extras: {}, stopped: false, proposal_status: status,
    proposal: { kind: 'skill', reason: 'Server text.', items: [], skill: draft, ...extra } } as ChatMessageOut
}

function SelectedProbe() {
  const { selectedSkills } = useChatSession()
  return <div data-testid="selected" data-skill={selectedSkills.c?.skill.id ?? ''} data-persistence={selectedSkills.c?.persistence ?? ''} />
}

function setup(initial: ChatMessageOut, skills: SkillOut[] = []) {
  spyOn(sdk, 'skillToolsList').mockResolvedValue({ data: tools } as never)
  const list = spyOn(sdk, 'skillsList').mockResolvedValue({ data: skills } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['conversations'], [{ id: 'c', title: 'Test', account_scope_json: JSON.stringify({ account_ids: ['a'], selection: null }) }])
  client.setQueryData(['history', 'c'], [initial])
  function Page() {
    const { data } = useQuery({ queryKey: ['history', 'c'], queryFn: async () => client.getQueryData<ChatMessageOut[]>(['history', 'c']) ?? [], staleTime: Infinity })
    return <><ProposalCard message={data![0]!} /><SelectedProbe /></>
  }
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><ChatSessionProvider><Page /></ChatSessionProvider></QueryClientProvider> })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/chat/c'] }) })
  render(<RouterProvider router={router} />)
  return { client, list, router }
}
afterEach(() => { cleanup(); mock.restore() })

test('a skill card shows the draft with localized tools and confirms only the edited fields', async () => {
  const done = card('done', { skill: { ...draft, slug: 'mail-pruefung', name: 'Mail-Prüfung' }, result: { done: 1, skill_id: saved.id } })
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: done } as never)
  const { client, list } = setup(card())
  const invalidate = spyOn(client, 'invalidateQueries')
  expect(await screen.findByRole('heading', { name: t.title })).toBeTruthy()
  expect(screen.getByText(t.reason)).toBeTruthy()
  expect(screen.queryByText('Server text.')).toBeNull()
  expect(screen.getByText(t.toolNames.search_messages)).toBeTruthy()
  expect(screen.getByText(t.toolNames.propose_action)).toBeTruthy()
  expect(await screen.findByText(t.kinds.read)).toBeTruthy()
  expect(screen.getByText(t.kinds.propose)).toBeTruthy()
  expect(screen.getByText('Suchen')).toBeTruthy()
  expect(screen.getByText(t.safety)).toBeTruthy()

  fireEvent.change(screen.getByLabelText(t.name), { target: { value: '  Mail-Prüfung ' } })
  fireEvent.change(screen.getByLabelText(t.command), { target: { value: 'mail-pruefung' } })
  expect(screen.getByText(t.commandHint('mail-pruefung'))).toBeTruthy()
  list.mockResolvedValue({ data: [saved] } as never)
  const save = screen.getByRole('button', { name: t.save })
  fireEvent.click(save); fireEvent.click(save)

  expect(await screen.findByText(t.saved(saved.name))).toBeTruthy()
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm.mock.calls[0]?.[0]?.body).toEqual({ selected_ids: [], skill_edit: { slug: 'mail-pruefung', name: 'Mail-Prüfung', description: draft.description, instructions: draft.instructions } })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['skills'] })

  // "Use now" attaches the new skill to this conversation for one message and sends nothing.
  fireEvent.click(await screen.findByRole('button', { name: t.useNow }))
  expect(await screen.findByText(t.attached(saved.name))).toBeTruthy()
  expect(screen.getByTestId('selected').dataset.skill).toBe(saved.id)
  expect(screen.getByTestId('selected').dataset.persistence).toBe('turn')
  expect(screen.queryByRole('button', { name: t.useNow })).toBeNull()
  expect(confirm).toHaveBeenCalledTimes(1)
})

test('invalid fields block saving and a name conflict marks the name field until it is edited', async () => {
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockRejectedValueOnce({ detail: { code: 'skill_name_conflict' } } as never)
  setup(card())
  const save = await screen.findByRole('button', { name: t.save })
  const command = screen.getByLabelText(t.command)
  for (const value of ['Mail Check', '-mail', 'tools', 'skill_creator', '']) {
    fireEvent.change(command, { target: { value } })
    expect(screen.getByText(t.commandInvalid)).toBeTruthy()
    expect((save as HTMLButtonElement).disabled).toBe(true)
  }
  fireEvent.change(command, { target: { value: 'mail-check' } })
  fireEvent.click(screen.getByText(t.instructions, { selector: 'summary' }))
  const instructions = screen.getByLabelText(t.instructions)
  fireEvent.change(instructions, { target: { value: '   ' } })
  expect(screen.getByText(t.required)).toBeTruthy()
  expect((save as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(instructions, { target: { value: 'Suche ungelesene Mails.' } })
  expect((save as HTMLButtonElement).disabled).toBe(false)

  fireEvent.click(save)
  expect(await screen.findByText(t.nameTaken)).toBeTruthy()
  expect(screen.getByLabelText(t.name).getAttribute('aria-invalid')).toBe('true')
  expect((save as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText(t.name), { target: { value: 'Mail-Check 2' } })
  expect(screen.queryByText(t.nameTaken)).toBeNull()
  expect((save as HTMLButtonElement).disabled).toBe(false)
  expect(confirm).toHaveBeenCalledTimes(1)
})

test.each([
  ['skill_conflict', t.commandTaken],
  ['skill_quota', t.quota],
  ['skill_tool_not_authorable', t.toolUnavailable],
  ['proposal_invalid', t.invalid],
])('confirmation refusal %s keeps the card and explains it', async (code, text) => {
  spyOn(sdk, 'chatProposalConfirm').mockRejectedValue({ detail: { code } } as never)
  setup(card())
  fireEvent.click(await screen.findByRole('button', { name: t.save }))
  expect(await screen.findByText(text)).toBeTruthy()
  expect(screen.getByRole('heading', { name: t.title })).toBeTruthy()
})

test('a card from a revoked creator context offers a new conversation and can still be cancelled', async () => {
  spyOn(sdk, 'chatProposalConfirm').mockRejectedValue({ detail: { code: 'proposal_context_revoked' } } as never)
  const cancel = spyOn(sdk, 'chatProposalCancel').mockResolvedValue({ data: card('cancelled') } as never)
  const { router } = setup(card())
  fireEvent.click(await screen.findByRole('button', { name: t.save }))
  expect(await screen.findByText(de.proposalErrors.proposal_context_revoked)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: de.controls.cancel }))
  expect(await screen.findByText(de.proposal.cancelled)).toBeTruthy()
  expect(cancel).toHaveBeenCalledTimes(1)
  expect(router.state.location.pathname).toBe('/chat/c')
})

test('a saved skill that is no longer active offers no shortcut', async () => {
  setup(card('done', { result: { done: 1, skill_id: saved.id } }), [{ ...saved, status: 'disabled' }])
  expect(await screen.findByText(t.saved(draft.name))).toBeTruthy()
  await waitFor(() => expect(screen.queryByRole('button', { name: t.useNow })).toBeNull())
})

test('an unknown proposal kind shows a notice instead of breaking the conversation', async () => {
  setup({ ...card(), proposal: { kind: 'future', reason: '', items: [] } } as unknown as ChatMessageOut)
  expect(await screen.findByText(de.proposal.unsupported)).toBeTruthy()
})

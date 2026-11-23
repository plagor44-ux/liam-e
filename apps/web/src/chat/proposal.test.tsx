import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterContextProvider, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState, type ReactNode } from 'react'
import * as sdk from '../api/generated/sdk.gen'
import type { CandidateOut, ChatMessageOut, ProposalResult } from '../api/generated/types.gen'
import { ProposalCard } from './ProposalCard'
import { ChatSessionProvider, useChatSession } from './session'

const cardRoot = createRootRoute()
function TestRouter({ children }: { children: ReactNode }) {
  const [router] = useState(() => createRouter({ routeTree: cardRoot, history: createMemoryHistory({ initialEntries: ['/'] }) }))
  return <RouterContextProvider router={router}><ChatSessionProvider>{children}</ChatSessionProvider></RouterContextProvider>
}
// Every card restarts from its conversation, so the conversation can have a saved account focus.
function testClient() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['conversations'], [{ id: 'c', title: 'Test', account_scope_json: JSON.stringify({ account_ids: ['a'], selection: 'a' }) }])
  return client
}

function message(status = 'pending', result?: ProposalResult): ChatMessageOut {
  return { id: 'p', conversation_id: 'c', role: 'proposal', content: '', tool_name: '', created_at: '', extras: {}, stopped: false, proposal_status: status, proposal: { kind: 'unsubscribe', reason: 'Nicht mehr gelesen.', items: [{ id: 'a', kind: 'sender', title: 'Alpha', subtitle: '12 Mails', method: 'post' }, { id: 'b', kind: 'sender', title: 'Beta', subtitle: '2 Mails', method: 'manual' }], result } }
}
function StartProbe() {
  const { composerTexts } = useChatSession()
  return <div data-testid="start" data-draft={composerTexts.new ?? ''} />
}
function TypeDraft() {
  const { setComposerText } = useChatSession()
  return <button onClick={() => setComposerText('c', 'Noch offen')}>type</button>
}
function setup(initial: ChatMessageOut, clock?: { now: () => number; setTimeout: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>; clearTimeout: (id: ReturnType<typeof setTimeout>) => void }, loadHistory?: () => Promise<ChatMessageOut[]>) {
  const client = testClient()
  client.setQueryData(['history', 'c'], [initial])
  function Page() {
    const { data } = useQuery({ queryKey: ['history', 'c'], queryFn: loadHistory ?? (async () => client.getQueryData<ChatMessageOut[]>(['history', 'c']) ?? []), staleTime: Infinity })
    return <><ProposalCard message={data![0]!} clock={clock} /><TypeDraft /><StartProbe /></>
  }
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><ChatSessionProvider><Page /></ChatSessionProvider></QueryClientProvider> })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(<RouterProvider router={router} />)
  return client
}
afterEach(() => { cleanup(); mock.restore() })

async function setupConfirmed(done: ChatMessageOut, clock?: Parameters<typeof setup>[1]) {
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: done })) as never)
  const client = setup(message(), clock)
  fireEvent.click(await screen.findByRole('button', { name: '2 abbestellen und archivieren' }))
  await screen.findByRole('button', { name: 'Rückgängig' })
  return client
}

function preferenceCard() {
  const card = message()
  card.proposal = { kind: 'preference', reason: '', items: [], preference: { candidate_id: 'candidate' } }
  return card
}
function preferenceCandidate(): CandidateOut {
  return { id: 'candidate', conversation_id: 'c', source_message_id: 'user', source_text: 'Merke dir: Antworte kurz.', key: 'answer_length', scope: '*', account_name: null, value: 'Kurz', status: 'pending', proposal_id: 'p', expected_version: 1, expected_revision_id: 'revision-1', previous_value: 'Ausführlich', current_value: 'Ausführlich', current_version: 1, current_revision_id: 'revision-1', use_preferences: false }
}

test('preference replacement uses a typed decision once, preserves opt-out and focuses the result', async () => {
  const initial = preferenceCard()
  spyOn(sdk, 'preferenceCandidatesList').mockResolvedValue({ data: [preferenceCandidate()] } as never)
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: { ...initial, proposal_status: 'done', proposal: { ...initial.proposal, result: { done: 1, preference_outcome: 'saved', preferences_enabled: false } } } } as never)
  const settings = spyOn(sdk, 'settingsUpdate')
  const client = setup(initial), invalidate = spyOn(client, 'invalidateQueries')
  expect(await screen.findByText('Bisher')).toBeTruthy()
  expect(screen.getByText('Vorschlag')).toBeTruthy()
  expect(screen.getByText('Merke dir: Antworte kurz.')).toBeTruthy()
  const button = screen.getByRole('button', { name: 'Ersetzen' })
  button.focus(); fireEvent.click(button); fireEvent.click(button)
  const result = await screen.findByText('Gespeichert. Verwendung ist ausgeschaltet.')
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm.mock.calls[0]?.[0]?.body).toEqual({ selected_ids: [], preference_decision: { choice: 'replace', expected_version: 1, expected_revision_id: 'revision-1' } })
  expect(settings).not.toHaveBeenCalled()
  expect(result.closest('.le-proposal-outcome')).toBe(document.activeElement)
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['preferences'] })
})

test('preference conflict requires explicit review, displays current values and never retries confirm', async () => {
  const row = preferenceCandidate()
  spyOn(sdk, 'preferenceCandidatesList').mockImplementation((async () => ({ data: [row] })) as never)
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => {
    row.current_value = 'Mittellang'; row.current_version = 2; row.current_revision_id = 'revision-2'
    throw { detail: { code: 'preference_candidate_conflict', current: row } }
  }) as never)
  const refresh = spyOn(sdk, 'preferenceCandidatePropose').mockImplementation((async () => ({ data: { ...row, expected_version: 2, expected_revision_id: 'revision-2', previous_value: 'Mittellang' } })) as never)
  setup(preferenceCard())
  fireEvent.click(await screen.findByRole('button', { name: 'Ersetzen' }))
  expect(await screen.findByText('Mittellang')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Ersetzen' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Aktuellen Stand prüfen' }))
  await waitFor(() => expect((screen.getByRole('button', { name: 'Ersetzen' }) as HTMLButtonElement).disabled).toBe(false))
  expect(refresh.mock.calls[0]?.[0]?.body).toEqual({ refresh: true })
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Präferenz speichern?' }))
})

for (const choice of ['keep', 'cancel'] as const) test(`preference ${choice} uses only the existing route`, async () => {
  const initial = preferenceCard()
  spyOn(sdk, 'preferenceCandidatesList').mockResolvedValue({ data: [preferenceCandidate()] } as never)
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: { ...initial, proposal_status: 'done', proposal: { ...initial.proposal, result: { done: 0, preference_outcome: 'kept' } } } } as never)
  const cancel = spyOn(sdk, 'chatProposalCancel').mockResolvedValue({ data: { ...initial, proposal_status: 'cancelled' } } as never)
  setup(initial)
  fireEvent.click(await screen.findByRole('button', { name: choice === 'keep' ? 'Bisherige behalten' : 'Abbrechen' }))
  if (choice === 'keep') {
    await screen.findByText('Bisherige Präferenz behalten.')
    expect(confirm.mock.calls[0]?.[0]?.body?.preference_decision?.choice).toBe('keep')
    expect(cancel).not.toHaveBeenCalled()
  } else {
    await screen.findByText('Abgebrochen, es wurde nichts geändert.')
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(confirm).not.toHaveBeenCalled()
  }
})

test('identical preference displays a deterministic no-op', async () => {
  const initial = preferenceCard()
  spyOn(sdk, 'preferenceCandidatesList').mockResolvedValue({ data: [{ ...preferenceCandidate(), previous_value: 'Kurz' }] } as never)
  spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: { ...initial, proposal_status: 'done', proposal: { ...initial.proposal, result: { done: 0, preference_outcome: 'unchanged' } } } } as never)
  setup(initial)
  await screen.findByText('Dieser Wert ist bereits gespeichert. Bestätigen ändert ihn nicht.')
  fireEvent.click(screen.getByRole('button', { name: 'Präferenz speichern' }))
  await screen.findByText('Dieser Wert ist bereits gespeichert. Nichts geändert.')
})

test('a removed preference account shows no values or confirmation button', async () => {
  spyOn(sdk, 'preferenceCandidatesList').mockResolvedValue({ data: [{ ...preferenceCandidate(), status: 'expired', value: null, source_text: null, previous_value: null }] } as never)
  setup(preferenceCard())
  await screen.findByText(/Dieser Vorschlag ist nicht mehr verfügbar/)
  expect(screen.queryByRole('button', { name: 'Ersetzen' })).toBeNull()
  expect(screen.queryByText('Kurz')).toBeNull()
})

test('task revision collisions explain the conflicting completed occurrence', async () => {
  const initial = message()
  initial.proposal = { kind: 'task', reason: 'Example', items: [], task: { action: 'patch', series_id: 'task', expected_version: 1, occurrence_id: 'occurrence', expected_occurrence_version: 1, scope: 'this_and_following', start: '2027-01-01', timezone: 'UTC' } }
  spyOn(sdk, 'tasksPreview').mockResolvedValue({ data: { occurrences: ['2027-01-01'], exceptions: [], through: '2028-01-01', complete: true } } as never)
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => { throw { detail: { code: 'task_revision_conflict' } } }) as never)
  setup(initial)
  await waitFor(() => expect((screen.getByRole('button', { name: 'Bestätigen' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Bestätigen' }))
  expect(await screen.findByText('Die neue Wiederholung überschneidet sich mit einer erledigten Fälligkeit. Wähle einen anderen Zeitplan.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Abbrechen' })).toBeTruthy()
})

test('task creation edits use a fresh preview and confirmation invalidates tasks with outcome focus', async () => {
  const initial = message()
  initial.proposal = { kind: 'task', reason: 'Example', items: [], task: { action: 'create', title: 'Example', start: '2027-01-01', timezone: 'UTC' } }
  const preview = spyOn(sdk, 'tasksPreview').mockImplementation((async () => ({ data: { occurrences: ['2027-01-01'], exceptions: [], through: '2028-01-01', complete: true } })) as never)
  const done = { ...initial, proposal_status: 'done', proposal: { ...initial.proposal, result: { done: 1, task_series_id: 'task' } } }
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: done })) as never)
  const client = setup(initial)
  const invalidate = spyOn(client, 'invalidateQueries')
  await screen.findByLabelText('Titel')
  await waitFor(() => expect((screen.getByRole('button', { name: 'Bestätigen' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.change(screen.getByLabelText('Titel'), { target: { value: 'Edited example' } })
  fireEvent.change(screen.getByLabelText('Erstes Datum'), { target: { value: '2027-02-01' } })
  expect((screen.getByRole('button', { name: 'Bestätigen' }) as HTMLButtonElement).disabled).toBe(true)
  await waitFor(() => expect((screen.getByRole('button', { name: 'Bestätigen' }) as HTMLButtonElement).disabled).toBe(false))
  expect(preview.mock.calls.at(-1)?.[0]?.body?.start).toBe('2027-02-01')
  expect(confirm).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Bestätigen' }))
  await waitFor(() => expect(confirm.mock.calls[0]?.[0]?.body?.task_edit?.title).toBe('Edited example'))
  expect(confirm.mock.calls[0]?.[0]?.body?.task_edit?.start).toBe('2027-02-01')
  const outcome = await screen.findByText('Aufgabenänderung gespeichert.')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['tasks'] })
  expect(outcome.closest('.le-proposal-outcome')).toBe(document.activeElement)
})

test('task card edits survive the replacement by the history row and are confirmed', async () => {
  const initial = message()
  initial.proposal = { kind: 'task', reason: 'Example', items: [], task: { action: 'create', title: 'Example', start: '2027-01-01', timezone: 'UTC' } }
  spyOn(sdk, 'tasksPreview').mockImplementation((async () => ({ data: { occurrences: ['2027-01-01'], exceptions: [], through: '2028-01-01', complete: true } })) as never)
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: { ...initial, proposal_status: 'done' } })) as never)
  const client = testClient()
  // The live card and the history row sit in different places, so React mounts a new card.
  const card = () => <QueryClientProvider client={client}><TestRouter><ProposalCard message={initial} /></TestRouter></QueryClientProvider>
  const view = render(card())
  fireEvent.change(await screen.findByLabelText('Titel'), { target: { value: 'Edited example' } })
  fireEvent.change(screen.getByLabelText('Erstes Datum'), { target: { value: '2027-02-01' } })
  fireEvent.change(screen.getByLabelText('Uhrzeit (optional)'), { target: { value: '09:30' } })
  view.unmount()
  render(card())
  expect((screen.getByLabelText('Titel') as HTMLInputElement).value).toBe('Edited example')
  expect((screen.getByLabelText('Erstes Datum') as HTMLInputElement).value).toBe('2027-02-01')
  expect((screen.getByLabelText('Uhrzeit (optional)') as HTMLInputElement).value).toBe('09:30')
  await waitFor(() => expect((screen.getByRole('button', { name: 'Bestätigen' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Bestätigen' }))
  await waitFor(() => expect(confirm.mock.calls[0]?.[0]?.body?.task_edit).toMatchObject({ title: 'Edited example', start: '2027-02-01T09:30' }))
})

test('task cards fetch their mail link only with the current grant and turn', async () => {
  const card = message()
  card.proposal = { kind: 'task', reason: 'Example', items: [], task: { action: 'archive', series_id: 'task', expected_version: 1, title: 'Task', source_message_id: 'mail' } }
  card.context = { id: 'turn', mail_scope_version: 4, app_prompt_version: '10', prompt_revision_id: null, prompt_hash: null, preference_revision_id: null, account_scope_json: '{}', revoked: false }
  const inbox = spyOn(sdk, 'mailMessageGet')
  const scoped = spyOn(sdk, 'chatMailReferenceGet').mockResolvedValue({ data: { id: 'mail' } } as never)
  const client = testClient()
  const view = (version?: number) => <QueryClientProvider client={client}><TestRouter><ProposalCard message={card} grantVersion={version} /></TestRouter></QueryClientProvider>
  const mounted = render(view())
  expect(scoped).not.toHaveBeenCalled()
  mounted.rerender(view(5))
  expect(scoped).not.toHaveBeenCalled()
  mounted.rerender(view(4))
  await waitFor(() => expect(scoped).toHaveBeenCalledTimes(1))
  expect(scoped.mock.calls[0]?.[0]).toMatchObject({ path: { message_id: 'mail' }, query: { expected_version: 4, context_id: 'turn' } })
  expect(inbox).not.toHaveBeenCalled()
})

test('cancelling a task card never confirms or invalidates tasks', async () => {
  const initial = message()
  initial.proposal = { kind: 'task', reason: 'Example', items: [], task: { action: 'archive', series_id: 'task', expected_version: 1, title: 'Example' } }
  const confirm = spyOn(sdk, 'chatProposalConfirm')
  spyOn(sdk, 'chatProposalCancel').mockImplementation((async () => ({ data: { ...initial, proposal_status: 'cancelled' } })) as never)
  const client = setup(initial), invalidate = spyOn(client, 'invalidateQueries')
  fireEvent.click(await screen.findByRole('button', { name: 'Abbrechen' }))
  await screen.findByText('Abgebrochen, es wurde nichts geändert.')
  expect(confirm).not.toHaveBeenCalled()
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['tasks'] })
})

test('confirmation submits selected ids and replaces the card in history', async () => {
  const done = message('done', { done: 1, undo_action_ids: ['u'] })
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: done })) as never)
  const client = setup(message())
  const invalidate = spyOn(client, 'invalidateQueries')
  expect(await screen.findByText('2 Newsletter abbestellen?')).toBeTruthy()
  expect(screen.getByText('2 Mails · nur über den Link im Newsletter')).toBeTruthy()
  fireEvent.click(screen.getByRole('checkbox', { name: /Beta/ }))
  fireEvent.click(screen.getByRole('button', { name: '1 abbestellen und archivieren' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, body: { selected_ids: ['a'], body: undefined }, throwOnError: true }))
  expect(await screen.findByText('1 Newsletter abbestellt, 1 Mail archiviert.')).toBeTruthy()
  expect(screen.queryByRole('checkbox')).toBeNull()
  expect(client.getQueryData<ChatMessageOut[]>(['history', 'c'])?.[0]?.proposal_status).toBe('done')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['messages'] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['message'] })
})

test('selection survives a card remount and confirmation', async () => {
  const initial = message()
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: message('done', { done: 1 }) })) as never)
  const client = testClient()
  const card = () => <QueryClientProvider client={client}><TestRouter><ProposalCard message={initial} /></TestRouter></QueryClientProvider>
  const view = render(card())
  fireEvent.click(screen.getByRole('checkbox', { name: /Beta/ }))
  view.unmount()
  render(card())
  expect((screen.getByRole('checkbox', { name: /Beta/ }) as HTMLInputElement).checked).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '1 abbestellen und archivieren' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, body: { selected_ids: ['a'], body: undefined }, throwOnError: true }))
})

test('edited draft survives a card remount and confirmation', async () => {
  const initial = message()
  initial.proposal = { kind: 'send', reason: 'Antwort.', items: [], draft: { account_id: 'a', body: 'Erster Text', cc: [], mode: 'new', quote: '', reply_to_message_id: null, subject: 'Hallo', to: ['anna@example.org'] } }
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: { ...initial, proposal_status: 'done' } })) as never)
  const client = testClient()
  const card = () => <QueryClientProvider client={client}><TestRouter><ProposalCard message={initial} /></TestRouter></QueryClientProvider>
  const view = render(card())
  fireEvent.change(screen.getByLabelText('Entwurf'), { target: { value: 'Neuer Text' } })
  view.unmount()
  render(card())
  expect((screen.getByLabelText('Entwurf') as HTMLTextAreaElement).value).toBe('Neuer Text')
  fireEvent.click(screen.getByRole('button', { name: 'Senden' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, body: { selected_ids: [], body: 'Neuer Text' }, throwOnError: true }))
})

test('running proposal refreshes history at most twenty times and clears its timer', () => {
  let next = 0
  const timers = new Map<number, () => void>()
  const clock = { now: () => 0, setTimeout: (fn: () => void, ms: number) => { expect(ms).toBe(3000); const id = ++next; timers.set(id, fn); return id as unknown as ReturnType<typeof setTimeout> }, clearTimeout: (id: ReturnType<typeof setTimeout>) => { timers.delete(id as unknown as number) } }
  const client = testClient()
  const invalidate = spyOn(client, 'invalidateQueries')
  const card = (row: ChatMessageOut) => <QueryClientProvider client={client}><TestRouter><ProposalCard message={row} clock={clock} /></TestRouter></QueryClientProvider>
  const view = render(card(message('running')))
  expect(screen.getByText('Wird ausgeführt …').className).toBe('le-hint')
  for (let i = 0; i < 20; i++) {
    expect(timers.size).toBe(1)
    const [id, callback] = [...timers.entries()][0]!
    timers.delete(id)
    act(callback)
  }
  expect(invalidate).toHaveBeenCalledTimes(20)
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['history', 'c'] })
  expect(timers.size).toBe(0)
  view.unmount()
  const second = render(card(message('running')))
  expect(timers.size).toBe(1)
  second.unmount()
  expect(timers.size).toBe(0)
})

test('running confirmation yields to the completed history row and stops polling', async () => {
  let next = 0
  const timers = new Map<number, number>()
  const clock = { now: () => 0, setTimeout: (_fn: () => void, ms: number) => { const id = ++next; timers.set(id, ms); return id as unknown as ReturnType<typeof setTimeout> }, clearTimeout: (id: ReturnType<typeof setTimeout>) => { timers.delete(id as unknown as number) } }
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: message('running') })) as never)
  const client = testClient()
  const card = (row: ChatMessageOut) => <QueryClientProvider client={client}><TestRouter><ProposalCard message={row} clock={clock} /></TestRouter></QueryClientProvider>
  const view = render(card(message()))
  fireEvent.click(screen.getByRole('button', { name: '2 abbestellen und archivieren' }))
  expect(await screen.findByText('Wird ausgeführt …')).toBeTruthy()
  await waitFor(() => expect([...timers.values()]).toContain(3000))
  view.rerender(card(message('done', { done: 1 })))
  expect(screen.getByText('1 Newsletter abbestellt.')).toBeTruthy()
  expect([...timers.values()]).not.toContain(3000)
})

test('cancelled proposal is quiet', async () => {
  setup(message('cancelled'))
  expect(await screen.findByText('Abgebrochen, es wurde nichts geändert.')).toBeTruthy()
})

test('cancelling uses the SDK and invalidates history and summary', async () => {
  const cancel = spyOn(sdk, 'chatProposalCancel').mockImplementation((async () => ({ data: message('cancelled') })) as never)
  const client = setup(message())
  const invalidate = spyOn(client, 'invalidateQueries')
  fireEvent.click(await screen.findByRole('button', { name: 'Abbrechen' }))
  expect(await screen.findByText('Abgebrochen, es wurde nichts geändert.')).toBeTruthy()
  expect(cancel).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, throwOnError: true })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['history', 'c'] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['summary'] })
})

test('undo sends all action ids and the notice changes', async () => {
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['u', 'v'], skipped: [], expired: [], failed: [] } })) as never)
  const client = await setupConfirmed(message('done', { done: 2, undo_action_ids: ['u', 'v'] }))
  const invalidate = spyOn(client, 'invalidateQueries')
  fireEvent.click(await screen.findByRole('button', { name: 'Rückgängig' }))
  await waitFor(() => expect(undo).toHaveBeenCalledWith({ body: { action_ids: ['u', 'v'] }, throwOnError: true }))
  expect(await screen.findByText('Rückgängig gemacht.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['messages'] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['message'] })
})

test('failed undo also invalidates inbox queries', async () => {
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => { throw new Error('network') }) as never)
  const client = await setupConfirmed(message('done', { done: 1, undo_action_ids: ['u'] }))
  const invalidate = spyOn(client, 'invalidateQueries')
  fireEvent.click(await screen.findByRole('button', { name: 'Rückgängig' }))
  await screen.findByText('Das hat nicht geklappt. Versuch es gleich noch mal.')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['messages'] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['message'] })
})

test('undo expires after ten seconds without waiting in real time', async () => {
  let callback = () => {}, delay = 0, now = 0
  const clock = { now: () => now, setTimeout: (fn: () => void, ms: number) => { callback = fn; delay = ms; return 1 as unknown as ReturnType<typeof setTimeout> }, clearTimeout: () => {} }
  await setupConfirmed(message('done', { done: 1, undo_action_ids: ['u'] }), clock)
  expect(await screen.findByRole('button', { name: 'Rückgängig' })).toBeTruthy()
  expect(delay).toBe(10000)
  act(() => { now = 10000; callback() })
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test('a delayed timer cannot permit undo after the deadline', async () => {
  let now = 0
  const clock = { now: () => now, setTimeout: () => 1 as unknown as ReturnType<typeof setTimeout>, clearTimeout: () => {} }
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: {} })) as never)
  await setupConfirmed(message('done', { done: 1, undo_action_ids: ['u'] }), clock)
  const button = await screen.findByRole('button', { name: 'Rückgängig' })
  now = 10001
  fireEvent.click(button)
  expect(undo).not.toHaveBeenCalled()
})

test.each(['done', 'partial'])('a %s history card has no undo without a local confirmation', async status => {
  setup(message(status, { done: 1, undo_action_ids: ['u'] }))
  expect(await screen.findByText('1 Newsletter abbestellt, 1 Mail archiviert.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test('remounting a confirmed result does not extend the undo deadline', async () => {
  let now = 0, delay = 0
  const clock = { now: () => now, setTimeout: (_fn: () => void, ms: number) => { delay = ms; return 1 as unknown as ReturnType<typeof setTimeout> }, clearTimeout: () => {} }
  const done = message('done', { done: 1, undo_action_ids: ['u'] })
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: done })) as never)
  const client = testClient()
  const card = (row: ChatMessageOut) => <QueryClientProvider client={client}><TestRouter><ProposalCard message={row} clock={clock} /></TestRouter></QueryClientProvider>
  const view = render(card(message()))
  fireEvent.click(screen.getByRole('button', { name: '2 abbestellen und archivieren' }))
  await screen.findByRole('button', { name: 'Rückgängig' })
  // The button renders before the passive effect that arms the timer.
  await waitFor(() => expect(delay).toBe(10000))
  view.unmount()
  now = 5000
  render(card(done))
  await screen.findByRole('button', { name: 'Rückgängig' })
  await waitFor(() => expect(delay).toBe(5000))
})

test.each([
  ['item_gone', 'Diesen Absender gibt es in deinen Mails nicht mehr.'],
  ['item_changed', 'Der Absender hat seinen Abmeldeweg geändert. Lass dir einen neuen Vorschlag machen.'],
  ['action_unsupported', 'Das geht bei dieser Mail nicht.'],
  ['credentials_invalid', 'Die Mail wurde nicht gesendet, weil die Anmeldung für dieses Konto nicht mehr gilt. Melde dich unter Konten erneut an.'],
  ['send_failed', 'Die Mail wurde nicht gesendet. Der Anbieter hat sie nicht angenommen. Versuch es gleich noch mal.'],
  ['provider_permission_missing', 'Für diese Aktion fehlt dem Connector die Berechtigung.'],
  ['send_unconfirmed', 'Der Anbieter hat den Versand nicht bestätigt. Schau unter Gesendet nach, bevor du die Mail erneut sendest.'],
  ['account_gone', 'Die Mail wurde nicht gesendet, weil das Konto nicht mehr verbunden ist.'],
  ...['unsubscribe_rejected', 'unsubscribe_blocked', 'unsubscribe_unreachable', 'unsubscribe_invalid', 'unsubscribe_send_failed'].map(code => [code, 'Der Newsletter ließ sich nicht abbestellen. Öffne die Abmeldeseite im Newsletter.']),
  ['proposal_not_pending', 'Dieser Vorschlag ist schon erledigt oder abgebrochen.'],
  ['selection_empty', 'Wähle mindestens einen Eintrag aus.'],
  ['action_failed', 'Das hat nicht geklappt. Versuch es gleich noch mal.'],
  ['host_not_allowed', 'liam-e darf diesen Server nicht ansprechen. Prüfe die Serveradresse oder trag den Server in Mistral Studio unter MAIL_IMAP_ALLOWED_HOSTS ein.'],
  ['unknown', 'Das hat nicht geklappt. Versuch es gleich noch mal.'],
])('failure %s shows its mapped cause', async (code, text) => {
  setup(message('failed', { failed: [{ id: 'a', code }] }))
  expect(await screen.findByText(text)).toBeTruthy()
})

test('a failed mail item names a missing mail', async () => {
  const initial = message('failed', { failed: [{ id: 'a', code: 'item_gone' }] })
  initial.proposal = { ...initial.proposal!, kind: 'archive' }
  setup(initial)
  expect(await screen.findByText('Die Mail ist nicht mehr da.')).toBeTruthy()
})

test('revoked proposal offers a new conversation', async () => {
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => { throw { detail: { code: 'proposal_context_revoked' } } }) as never)
  setup(message())
  fireEvent.click(await screen.findByRole('button', { name: '2 abbestellen und archivieren' }))
  expect(await screen.findByText('Dieser Vorschlag stammt aus einem widerrufenen Kontext. Starte eine neue Unterhaltung.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Neue Unterhaltung' })).toBeTruthy()
})

test('restart from a revoked proposal carries the unsent text', async () => {
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => { throw { detail: { code: 'proposal_context_revoked' } } }) as never)
  setup(message())
  fireEvent.click(await screen.findByRole('button', { name: 'type' }))
  fireEvent.click(await screen.findByRole('button', { name: '2 abbestellen und archivieren' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Neue Unterhaltung' }))
  await screen.findByTestId('start')
  expect(screen.getByTestId('start').dataset.draft).toBe('Noch offen')
})

test.each(['proposal_not_pending', 'selection_empty'])('confirmation refusal %s shows its cause', async code => {
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => { throw { detail: { code } } }) as never)
  setup(message())
  fireEvent.click(await screen.findByRole('button', { name: '2 abbestellen und archivieren' }))
  expect(await screen.findByText(code === 'selection_empty' ? 'Wähle mindestens einen Eintrag aus.' : 'Dieser Vorschlag ist schon erledigt oder abgebrochen.')).toBeTruthy()
})

test.each(['confirm', 'cancel'] as const)('%s conflict reloads the actual proposal state', async operation => {
  const request = operation === 'confirm' ? 'chatProposalConfirm' : 'chatProposalCancel'
  spyOn(sdk, request).mockImplementation((async () => { throw { detail: { code: 'proposal_not_pending' } } }) as never)
  let resolveHistory!: (rows: ChatMessageOut[]) => void
  const loadHistory = mock(() => new Promise<ChatMessageOut[]>(resolve => { resolveHistory = resolve }))
  const client = setup(message(), undefined, loadHistory)
  const invalidate = spyOn(client, 'invalidateQueries')
  fireEvent.click(await screen.findByRole('button', { name: operation === 'confirm' ? '2 abbestellen und archivieren' : 'Abbrechen' }))
  expect(await screen.findByText('Dieser Vorschlag ist schon erledigt oder abgebrochen.')).toBeTruthy()
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['history', 'c'] })
  expect(loadHistory).toHaveBeenCalledTimes(1)
  await act(async () => { resolveHistory([message('done', { done: 1 })]) })
  expect(await screen.findByText('1 Newsletter abbestellt.')).toBeTruthy()
  expect(screen.queryByRole('checkbox')).toBeNull()
})

test.each([
  ['archive', '1 Mail archiviert. 1 ließ sich nicht archivieren.'],
  ['trash', '1 Mail in den Papierkorb gelegt. 1 ließ sich nicht in den Papierkorb legen.'],
  ['mark_read', '1 Mail als gelesen markiert. 1 ließ sich nicht als gelesen markieren.'],
  ['send', 'Mail gesendet. 1 ließ sich nicht ändern.'],
] as const)('partial %s names the action', async (kind, text) => {
  const initial = message('partial', { done: 1, failed: [{ id: 'a', code: 'action_failed' }] })
  initial.proposal = { ...initial.proposal!, kind }
  setup(initial)
  expect(await screen.findByText(text)).toBeTruthy()
})

test('partial archive names a missing mail', async () => {
  const initial = message('partial', { done: 2, failed: [{ id: 'a', code: 'item_gone' }] })
  initial.proposal = { ...initial.proposal!, kind: 'archive' }
  setup(initial)
  expect(await screen.findByText('2 Mails archiviert. 1 Mail ist nicht mehr da.')).toBeTruthy()
})

test('partial unsubscribe separates gone, changed and other failures', async () => {
  setup(message('partial', { done: 1, failed: [{ id: 'a', code: 'item_gone' }, { id: 'b', code: 'item_changed' }, { id: 'c', code: 'action_failed' }] }))
  expect(await screen.findByText('1 Newsletter abbestellt. 1 Absender ist nicht mehr da. 1 Absender hat seinen Abmeldeweg geändert. 1 ließ sich nicht abbestellen.')).toBeTruthy()
})

test('a send draft is editable and the edited body is confirmed', async () => {
  const initial = message()
  initial.proposal = { kind: 'send', reason: 'Antwort.', irreversible: true, items: [], draft: { account_id: 'a', body: 'Erster Text', cc: [], mode: 'new', quote: '', reply_to_message_id: null, subject: 'Hallo', to: ['anna@example.org'] } }
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: { ...initial, proposal_status: 'done', proposal: { ...initial.proposal!, result: { done: 1 } } } })) as never)
  setup(initial)
  expect(await screen.findByText('Gesendete Mails lassen sich nicht zurückholen.')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Entwurf'), { target: { value: 'Neuer Text' } })
  fireEvent.click(screen.getByRole('button', { name: 'Senden' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, body: { selected_ids: [], body: 'Neuer Text' }, throwOnError: true }))
  expect(await screen.findByText('Mail gesendet.')).toBeTruthy()
})

test('a reply-all draft shows every recipient and the quoted mail before sending', async () => {
  const initial = message()
  initial.proposal = { kind: 'send', reason: 'Antwort an alle: anna@example.org.', irreversible: true, items: [], draft: { account_id: 'a', body: 'Danke', cc: ['ben@example.org', 'cara@example.org'], mode: 'reply_all', quote: 'Am 3.11. um 09:00 schrieb Anna:\n> Termin am Montag?', reply_to_message_id: 'm', subject: 'Re: Termin', to: ['anna@example.org'] } }
  setup(initial)
  expect(await screen.findByText('An: anna@example.org')).toBeTruthy()
  expect(screen.getByText('Kopie: ben@example.org, cara@example.org')).toBeTruthy()
  const quote = screen.getByRole('blockquote', { name: 'Zitierte Mail – wird mitgesendet' })
  expect(quote.textContent).toBe('Am 3.11. um 09:00 schrieb Anna:\n> Termin am Montag?')
  expect((screen.getByRole('button', { name: 'Senden' }) as HTMLButtonElement).disabled).toBe(false)
})

test('a new draft without copy or quote shows neither', async () => {
  const initial = message()
  initial.proposal = { kind: 'send', reason: 'Neue Mail an anna@example.org.', irreversible: true, items: [], draft: { account_id: 'a', body: 'Hallo', cc: [], mode: 'new', quote: '', reply_to_message_id: null, subject: 'Hallo', to: ['anna@example.org'] } }
  setup(initial)
  expect(await screen.findByText('An: anna@example.org')).toBeTruthy()
  expect(screen.queryByText(/^Kopie:/u)).toBeNull()
  expect(screen.queryByRole('blockquote')).toBeNull()
  expect(screen.queryByRole('list')).toBeNull()
})

test.each([1, 2])('partial result uses the correct form for %i failures', async (count) => {
  setup(message('partial', { done: 1, failed: Array.from({ length: count }, (_, i) => ({ id: String(i), code: 'action_failed' })) }))
  const ending = count === 1 ? '1 ließ sich nicht abbestellen.' : '2 ließen sich nicht abbestellen.'
  expect(await screen.findByText(`1 Newsletter abbestellt. ${ending}`)).toBeTruthy()
})

test('manual unsubscribe links have a large touch target', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const rule = css.match(/@media \(pointer: coarse\) \{ \.le-proposal-manual a \{ ([^{}]+) \} \}/u)
  expect(rule).toBeTruthy()
  for (const property of ['display: inline-flex', 'align-items: center', 'min-height: var(--control-height-lg)']) expect(rule?.[1]).toContain(property)
})

test('unsubscribe without archived mail and unsafe manual link', async () => {
  setup(message('partial', { done: 1, failed: [], manual: [{ id: 'a', link: 'javascript:alert(1)' }, { id: 'b', link: 'https://example.org/unsubscribe' }], undo_action_ids: [] }))
  expect(await screen.findByText('1 Newsletter abbestellt.')).toBeTruthy()
  const links = screen.getAllByRole('link', { name: 'Abmeldeseite öffnen' })
  expect(links).toHaveLength(1)
  expect(links[0]?.getAttribute('href')).toBe('https://example.org/unsubscribe')
  expect(links[0]?.getAttribute('rel')).toBe('noopener noreferrer')
  expect(links[0]?.parentElement?.classList.contains('le-proposal-manual')).toBe(true)
  expect(screen.getByText('Alpha: Kein Abmeldelink bekannt. Öffne eine Mail dieses Newsletters und bestell ihn dort ab.').classList.contains('le-proposal-manual')).toBe(true)
})

test('a manual unsubscribe without a link names the next step', async () => {
  setup(message('done', { done: 0, failed: [], manual: [{ id: 'b', link: null }], undo_action_ids: ['u1', 'u2'] }))
  expect(await screen.findByText('Kein Newsletter abbestellt, 2 Mails archiviert.')).toBeTruthy()
  expect(screen.getByText('Beta: Kein Abmeldelink bekannt. Öffne eine Mail dieses Newsletters und bestell ihn dort ab.')).toBeTruthy()
  expect(screen.queryByRole('link')).toBeNull()
})

test('after confirming, the focus moves from the gone button to the result', async () => {
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: message('done', { done: 2, undo_action_ids: ['u'] }) })) as never)
  setup(message())
  const button = await screen.findByRole('button', { name: '2 abbestellen und archivieren' })
  button.focus(); fireEvent.click(button)
  await screen.findByRole('button', { name: 'Rückgängig' })
  await waitFor(() => expect(document.activeElement?.classList.contains('le-proposal-outcome')).toBe(true))
  expect(document.activeElement?.textContent).toContain('2 Newsletter abbestellt')
})

test('after cancelling, the focus moves to the cancelled hint', async () => {
  spyOn(sdk, 'chatProposalCancel').mockImplementation((async () => ({ data: message('cancelled') })) as never)
  setup(message())
  const button = await screen.findByRole('button', { name: 'Abbrechen' })
  button.focus(); fireEvent.click(button)
  await screen.findByText('Abgebrochen, es wurde nichts geändert.')
  await waitFor(() => expect(document.activeElement?.classList.contains('le-proposal-outcome')).toBe(true))
  expect(document.activeElement?.textContent).toBe('Abgebrochen, es wurde nichts geändert.')
})

test('after undoing, the focus stays on the result', async () => {
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['u'], expired: [], failed: [], skipped: [] } })) as never)
  await setupConfirmed(message('done', { done: 1, undo_action_ids: ['u'] }))
  const undo = screen.getByRole('button', { name: 'Rückgängig' })
  undo.focus(); fireEvent.click(undo)
  await screen.findByText('Rückgängig gemacht.')
  expect(document.activeElement?.classList.contains('le-proposal-outcome')).toBe(true)
})

function liveThenHistory() {
  const done = message('done', { done: 2, undo_action_ids: ['u'] })
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: done })) as never)
  const client = testClient()
  // The live card and the history row sit in different places, so React mounts a new card.
  const view = (inHistory: boolean) => <QueryClientProvider client={client}><TestRouter>{inHistory ? <section key="history"><ProposalCard message={done} /></section> : <div key="live"><ProposalCard message={message()} /><input aria-label="Frage" /></div>}</TestRouter></QueryClientProvider>
  return { view, ...render(view(false)) }
}

test('the focus follows a settled card when its history row replaces the live card', async () => {
  const { view, rerender } = liveThenHistory()
  const button = screen.getByRole('button', { name: '2 abbestellen und archivieren' })
  button.focus(); fireEvent.click(button)
  await screen.findByRole('button', { name: 'Rückgängig' })
  await waitFor(() => expect(document.activeElement?.classList.contains('le-proposal-outcome')).toBe(true))
  rerender(view(true))
  await waitFor(() => expect(document.querySelector('section .le-proposal-outcome')).toBe(document.activeElement))
})

test('a replaced card hands the focus on only when it holds it', async () => {
  const { view, rerender } = liveThenHistory()
  fireEvent.click(screen.getByRole('button', { name: '2 abbestellen und archivieren' }))
  await screen.findByRole('button', { name: 'Rückgängig' })
  const question = screen.getByLabelText('Frage')
  act(() => { question.focus() })
  rerender(view(true))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(document.activeElement).toBe(document.body)
  cleanup()
  render(<QueryClientProvider client={testClient()}><TestRouter><ProposalCard message={message('done', { done: 2 })} /></TestRouter></QueryClientProvider>)
  expect(document.activeElement).toBe(document.body)
})

test('a confirmation that finishes after the card is gone does not take the focus later', async () => {
  let resolve: (value: { data: ChatMessageOut }) => void = () => {}
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((() => new Promise(done => { resolve = done })) as never)
  const client = testClient()
  const view = render(<QueryClientProvider client={client}><TestRouter><ProposalCard message={message()} /></TestRouter></QueryClientProvider>)
  const button = screen.getByRole('button', { name: '2 abbestellen und archivieren' })
  button.focus(); fireEvent.click(button)
  view.unmount()
  await act(async () => { resolve({ data: message('done', { done: 2 }) }); await new Promise(done => setTimeout(done, 0)) })
  render(<QueryClientProvider client={client}><TestRouter><ProposalCard message={message('done', { done: 2 })} /></TestRouter></QueryClientProvider>)
  await screen.findByText(/2 Newsletter abbestellt/)
  expect(document.activeElement).toBe(document.body)
})

test('a confirmation that finishes after the card shows another proposal does not take the focus later', async () => {
  let resolve: (value: { data: ChatMessageOut }) => void = () => {}
  spyOn(sdk, 'chatProposalConfirm').mockImplementation((() => new Promise(done => { resolve = done })) as never)
  const client = testClient()
  const card = (row: ChatMessageOut) => <QueryClientProvider client={client}><TestRouter><ProposalCard message={row} /></TestRouter></QueryClientProvider>
  const view = render(card(message()))
  const button = screen.getByRole('button', { name: '2 abbestellen und archivieren' })
  button.focus(); fireEvent.click(button)
  view.rerender(card({ ...message(), id: 'q' }))
  await act(async () => { resolve({ data: message('done', { done: 2 }) }); await new Promise(done => setTimeout(done, 0)) })
  view.unmount()
  render(card(message('done', { done: 2 })))
  await screen.findByText(/2 Newsletter abbestellt/)
  expect(document.activeElement).toBe(document.body)
})

test('a settled card loaded from the history does not take the focus', async () => {
  setup(message('done', { done: 1 }))
  await screen.findByText('1 Newsletter abbestellt.')
  expect(document.activeElement).toBe(document.body)
})

test('app filing card names its destination and changes nothing until confirmed', async () => {
  const initial = message()
  initial.proposal = { kind: 'filing', reason: 'Einordnen', items: [{ id: 'm', kind: 'mail', title: 'Beispiel', subtitle: 'Anna' }], filing: { folder_id: 'f', set_folder: true, add_labels: [], remove_labels: [] } }
  spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'f', kind: 'folder', name: 'Projects' }] } as never)
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: { ...initial, proposal_status: 'done', proposal: { ...initial.proposal!, result: { done: 1 } } } })) as never)
  setup(initial)
  expect(await screen.findByText(/Ordner: Projects/)).toBeTruthy()
  expect(confirm).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Zuordnen' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, body: { selected_ids: ['m'] }, throwOnError: true }))
  expect(await screen.findByText('Mails zugeordnet.')).toBeTruthy()
})

test('rule card shows scope and actions before confirmation', async () => {
  const initial = message()
  initial.proposal = { kind: 'rule', reason: 'Automatisch', items: [], rule: { name: 'Projects', account_id: 'a', sender_domain: 'example.test', folder_id: 'f', mark_read: true } }
  spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'f', kind: 'folder', name: 'Projects folder' }] } as never)
  spyOn(sdk, 'mailAccountsList').mockResolvedValue({ data: [{ id: 'a', email_address: 'a@example.test', display_name: 'Work' }] } as never)
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: { ...initial, proposal_status: 'done', proposal: { ...initial.proposal!, result: { done: 1 } } } })) as never)
  setup(initial)
  expect(await screen.findByText('Ordner: Projects folder')).toBeTruthy()
  expect(screen.getByText('Konto: Work')).toBeTruthy()
  expect(screen.getByText('Als gelesen markieren')).toBeTruthy()
  expect(confirm).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Regel anlegen' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, body: { selected_ids: [] }, throwOnError: true }))
  expect(await screen.findByText('Regel erstellt.')).toBeTruthy()
})

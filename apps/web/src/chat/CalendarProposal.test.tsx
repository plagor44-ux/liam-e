import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import * as sdk from '../api/generated/sdk.gen'
import type { ChatMessageOut } from '../api/generated/types.gen'
import { CalendarProposal } from './CalendarProposal'
import { de } from '../i18n/de'
const message = { created_at: '2026-11-20T09:00:00Z', extras: {}, stopped: false, tool_name: '', id: 'proposal', conversation_id: 'conversation', role: 'assistant', content: '', proposal_status: 'pending', proposal: { kind: 'calendar', reason: 'Zeit anpassen', items: [], calendar: { action: 'update', source_id: 's', timezone: 'Europe/Berlin', before: { source_id: 's', title: 'Vorheriger Termin', start_date: '2026-11-20', end_date: '2026-11-21' }, after: { title: 'Neuer Titel', start_date: '2026-11-21', end_date: '2026-11-22', location: 'Raum' } } } } as ChatMessageOut
afterEach(() => { cleanup(); mock.restore() })
function mount(value = message) {
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [{ id: 's', name: 'Planung', is_active: true, chat_access: 'writable', capabilities: ['read', 'write'] }] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><CalendarProposal message={value} /></QueryClientProvider>)
  return client
}
test('update shows before and after, calendar and timezone', async () => {
  mount()
  await screen.findByText('Planung')
  expect(screen.getByText('Bisher')).toBeTruthy(); expect(screen.getByText('Vorschlag')).toBeTruthy()
  expect(screen.getByText('Vorheriger Termin')).toBeTruthy(); expect(screen.getByText('Neuer Titel')).toBeTruthy()
  expect(screen.getByText(/Europe\/Berlin/)).toBeTruthy()
})
test('delete warns about undo', async () => {
  mount({ ...message, proposal: { ...message.proposal!, calendar: { ...message.proposal!.calendar!, action: 'delete', after: null } } })
  await screen.findByText('Planung')
  expect(screen.getByText(/nicht garantiert rückgängig/)).toBeTruthy()
})
test('unknown has check but no confirm and updates to done', async () => {
  const check = spyOn(sdk, 'chatProposalCheck').mockResolvedValue({ data: { ...message, proposal_status: 'done' }, response: { status: 200 } } as never)
  mount({ ...message, proposal_status: 'unknown' })
  fireEvent.click(await screen.findByRole('button', { name: 'Prüfen' }))
  expect(screen.queryByRole('button', { name: 'Bestätigen' })).toBeNull()
  await screen.findByText('Termin geändert.')
  expect(check).toHaveBeenCalledTimes(1)
})
test('confirmation invalidates calendar and cannot be repeated', async () => {
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: { ...message, proposal_status: 'done' }, response: { status: 200 } } as never)
  const client = mount(), invalidate = spyOn(client, 'invalidateQueries')
  await screen.findByText('Planung')
  fireEvent.click(await screen.findByRole('button', { name: 'Bestätigen' }))
  await screen.findByText('Termin geändert.')
  expect(screen.queryByRole('button', { name: 'Bestätigen' })).toBeNull()
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['calendar'] }))
  expect(confirm.mock.calls[0]?.[0]?.body).toEqual({ selected_ids: [] })
})
test('a card left running after an interrupted confirm can be checked', async () => {
  const check = spyOn(sdk, 'chatProposalCheck').mockResolvedValue({ data: { ...message, proposal_status: 'done' }, response: { status: 200 } } as never)
  mount({ ...message, proposal_status: 'running' })
  fireEvent.click(await screen.findByRole('button', { name: 'Prüfen' }))
  await screen.findByText('Termin geändert.')
  expect(check).toHaveBeenCalledTimes(1)
})
test('a definite refusal outside the conversation scope is not called unclear', async () => {
  mount({ ...message, proposal_status: 'failed', proposal: { ...message.proposal!, result: { done: 0, failed: [{ id: 'proposal', code: 'calendar_out_of_scope' }] } } } as ChatMessageOut)
  await screen.findByText(/nicht zur Kalenderauswahl/)
  expect(screen.queryByText(/noch unklar/)).toBeNull()
})
test('a confirm that never reached the server returns to the open card after a check', async () => {
  spyOn(sdk, 'chatProposalConfirm').mockRejectedValue(new TypeError('test-only'))
  const check = spyOn(sdk, 'chatProposalCheck').mockResolvedValue({ error: { detail: { error_code: 'proposal_not_pending' } }, response: { status: 409 } } as never)
  mount()
  await screen.findByText('Planung')
  fireEvent.click(screen.getByRole('button', { name: 'Bestätigen' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Prüfen' }))
  // The server still has the card pending: confirm and cancel come back, with no stale error.
  await screen.findByRole('button', { name: 'Bestätigen' })
  expect(screen.getByRole('button', { name: 'Abbrechen' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Prüfen' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(check).toHaveBeenCalledTimes(1)
})

const create = { ...message, proposal: { ...message.proposal!, calendar: { action: 'create', source_id: 's', timezone: 'Europe/Berlin', before: null, after: { title: 'Neuer Termin', start_local: '2026-11-21T10:00', end_local: '2026-11-21T11:00' } } } } as ChatMessageOut
const planning = { id: 's', name: 'Planung', is_active: true, chat_access: 'writable', capabilities: ['read', 'write'] }
async function mountCreate(defaultId: string | null, sources: object[] = [planning], value = create) {
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: sources } as never)
  const settings = spyOn(sdk, 'calendarSettingsGet').mockResolvedValue({ data: { version: 1, default_source_id: defaultId } } as never)
  settings.mockClear()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><CalendarProposal message={value} /></QueryClientProvider> })
  const router = createRouter({ routeTree: root.addChildren([createRoute({ getParentRoute: () => root, path: '/settings/calendar' })]), history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(<RouterProvider router={router} />)
  await screen.findByText(value.proposal_status === 'pending' ? 'Planung' : de.calendar.cardDone[value.proposal!.calendar!.action])
  return settings
}
test('a new appointment explains a missing or unusable default calendar and links to its setting', async () => {
  await mountCreate(null)
  expect(await screen.findByText(de.calendarSettings.noDefault, { exact: false })).toBeTruthy()
  expect(screen.getByRole('link', { name: de.calendarSettings.title }).getAttribute('href')).toBe('/settings/calendar')
  cleanup()
  await mountCreate('club', [planning, { id: 'club', name: 'Verein', is_active: true, chat_access: 'read_only', capabilities: ['read'] }])
  expect(await screen.findByText(de.calendarSettings.defaultUnavailable('Verein', de.calendarSettings.reason.read_only), { exact: false })).toBeTruthy()
  cleanup()
  await mountCreate('gone')
  expect(await screen.findByText(de.calendarSettings.missing, { exact: false })).toBeTruthy()
})
test('the default calendar hint stays away when the default is usable, is the target, or the card is not a pending new appointment', async () => {
  const quiet = async () => {
    await waitFor(() => expect(screen.getByRole('button', { name: 'Bestätigen' })).toBeTruthy())
    expect(screen.queryByRole('link', { name: de.calendarSettings.title })).toBeNull()
  }
  await mountCreate('s')
  await quiet()
  cleanup()
  await mountCreate('other', [planning, { id: 'other', name: 'Team', is_active: true, chat_access: 'writable', capabilities: ['read', 'write'] }])
  await quiet()
  cleanup()
  const settings = await mountCreate(null, [planning], message)
  await quiet()
  expect(settings).not.toHaveBeenCalled()
  cleanup()
  await mountCreate(null, [planning], { ...create, proposal_status: 'done' })
  expect(screen.queryByRole('link', { name: de.calendarSettings.title })).toBeNull()
})

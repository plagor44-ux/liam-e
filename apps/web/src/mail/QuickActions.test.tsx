// i18n-exempt: synthetic destinations and messages.
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { QuickActions } from './QuickActions'

const originalMatchMedia = window.matchMedia
afterEach(() => { cleanup(); mock.restore(); window.matchMedia = originalMatchMedia })

function setup(mobile = false) {
  window.matchMedia = ((query: string) => ({ matches: !mobile && query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  const data = [
    { destination: { id: 'f', name: 'Projects', kind: 'folder' }, use_count: 4, last_used_at: '2026-11-23T00:00:00Z', score: 4 },
    { destination: { id: 'l', name: 'Alpha', kind: 'label' }, use_count: 3, last_used_at: '2026-11-23T00:00:00Z', score: 3 },
    { destination: { id: 'm', name: 'Beta', kind: 'label' }, use_count: 3, last_used_at: '2026-11-23T00:00:00Z', score: 3 },
  ]
  spyOn(sdk, 'mailFilingQuickActions').mockResolvedValue({ data } as never)
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  const open = mock(() => {})
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><div onClick={open}><QuickActions messageId="message" showEmpty /></div></QueryClientProvider>)
  return { assign, open }
}

test('quick action files in one click and does not open the mail row', async () => {
  const { assign, open } = setup()
  fireEvent.click(await screen.findByRole('button', { name: 'Label: Alpha' }))
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['message'], add_labels: ['l'] }))
  expect(open).not.toHaveBeenCalled()
})

test('mobile exposes fewer ranked actions and uses alphabetical tie ordering', async () => {
  setup(true)
  expect(await screen.findByRole('button', { name: 'Ordner: Projects' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Label: Alpha' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Label: Beta' })).toBeNull()
})

test('a deleted destination reports failure without opening the mail', async () => {
  const { assign, open } = setup()
  assign.mockRejectedValue(new Error('destination gone'))
  fireEvent.click(await screen.findByRole('button', { name: 'Ordner: Projects' }))
  expect(await screen.findByText('Die Schnellaktion konnte nicht ausgeführt werden. Lade die Auswahl neu.')).toBeTruthy()
  expect(open).not.toHaveBeenCalled()
})

test('empty history leaves the normal filing workflow available', async () => {
  spyOn(sdk, 'mailFilingQuickActions').mockResolvedValue({ data: [] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><QuickActions messageId="message" showEmpty /></QueryClientProvider>)
  expect(await screen.findByText('Noch keine Schnellaktionen. Ordne eine Mail zu, um hier Vorschläge zu sehen.')).toBeTruthy()
})

test('desktop displays at most five quick actions', async () => {
  window.matchMedia = ((query: string) => ({ matches: query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  const data = Array.from({ length: 7 }, (_, n) => ({ destination: { id: `l${n}`, name: `Label ${n}`, kind: 'label' }, use_count: n + 1, last_used_at: '2026-11-23T00:00:00Z', score: n + 1 }))
  spyOn(sdk, 'mailFilingQuickActions').mockResolvedValue({ data } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><QuickActions messageId="message" /></QueryClientProvider>)
  await screen.findByRole('button', { name: 'Label: Label 6' })
  expect(screen.getAllByRole('button')).toHaveLength(5)
  expect(screen.queryByRole('button', { name: 'Label: Label 0' })).toBeNull()
})

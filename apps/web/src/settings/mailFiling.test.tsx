// i18n-exempt: synthetic API responses and test labels.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { client as api } from '../api/generated/client.gen'
import { FolderSettings } from './MailFiling'

const original = globalThis.fetch, config = api.getConfig()
let requests: { method: string; url: string; body?: unknown }[] = []
afterEach(() => { cleanup(); globalThis.fetch = original; api.setConfig(config) })

function inventoryPage(rows: { id: string; name: string; kind: string }[], request: Request) {
  const params = new URL(request.url).searchParams
  const cursor = params.get('cursor'), limit = Number(params.get('limit') ?? '100')
  const ordered = [...rows].sort((a, b) => a.id.localeCompare(b.id)).filter(row => !cursor || row.id > cursor)
  const items = ordered.slice(0, limit)
  return Response.json({ items, next_cursor: ordered.length > limit ? items.at(-1)?.id : null })
}

test('folder deletion asks for confirmation and sends it explicitly', async () => {
  requests = []
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    requests.push({ method: req.method, url: req.url })
    if (req.method === 'DELETE') return Response.json({ ok: true })
    return inventoryPage([{ id: 'folder', name: 'Projects', kind: 'folder' }], req)
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FolderSettings /></QueryClientProvider>)
  await screen.findByText('Projects')
  fireEvent.click(screen.getByRole('button', { name: 'Projects löschen' }))
  expect(requests.filter(request => request.method === 'DELETE')).toHaveLength(0)
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Löschen' }))
  await waitFor(() => expect(requests.some(request => request.method === 'DELETE' && request.url.includes('confirm_delete=true'))).toBe(true))
})

test('folder settings search and filter the whole inventory and reset pagination', async () => {
  api.setConfig({ baseUrl: window.location.origin })
  const rows = Array.from({ length: 122 }, (_, i) => ({ id: `f${String(i).padStart(3, '0')}`, name: `Folder ${String(i).padStart(3, '0')}`, kind: 'folder' }))
  rows.push({ id: 'l1', name: 'Special label', kind: 'label' })
  let pageReads = 0
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    pageReads++
    return inventoryPage(rows, input instanceof Request ? input : new Request(input, init))
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FolderSettings /></QueryClientProvider>)
  expect(await screen.findByText('Folder 000')).toBeTruthy()
  expect(pageReads).toBe(2)
  expect(screen.queryByText('Folder 021')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Nächste Seite' }))
  expect(screen.getByText('Folder 021')).toBeTruthy()
  fireEvent.change(screen.getByRole('searchbox', { name: 'Ordner und Labels suchen' }), { target: { value: 'special' } })
  expect(screen.getByText('Special label')).toBeTruthy()
  expect(screen.queryByText('Folder 021')).toBeNull()
  expect(screen.getByText('Seite 1 von 1')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Suche löschen' }))
  fireEvent.click(screen.getByRole('button', { name: 'Alle Arten' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Nur Labels' }))
  expect(screen.getByText('Special label')).toBeTruthy()
  expect(screen.queryByText('Folder 000')).toBeNull()
  fireEvent.change(screen.getByRole('searchbox', { name: 'Ordner und Labels suchen' }), { target: { value: 'missing' } })
  expect(screen.getByText('Keine passenden Ordner oder Labels.')).toBeTruthy()
})

test('deleting the last item on a later page returns to a valid page', async () => {
  api.setConfig({ baseUrl: window.location.origin })
  let rows = Array.from({ length: 21 }, (_, i) => ({ id: `f${i}`, name: `Folder ${String(i).padStart(2, '0')}`, kind: 'folder' }))
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    if (req.method === 'DELETE') { rows = rows.filter(row => row.id !== 'f20'); return Response.json({ ok: true }) }
    return inventoryPage(rows, req)
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FolderSettings /></QueryClientProvider>)
  await screen.findByText('Folder 00')
  fireEvent.click(screen.getByRole('button', { name: 'Nächste Seite' }))
  fireEvent.click(screen.getByRole('button', { name: 'Folder 20 löschen' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Löschen' }))
  await waitFor(() => expect(screen.queryByText('Folder 20') === null).toBe(true))
  expect(screen.getByText('Seite 1 von 1')).toBeTruthy()
  expect(screen.getByText('Folder 00')).toBeTruthy()
})

test('rules display alphabetically with their stored priority and do not mutate it', async () => {
  const { RuleSettings } = await import('./MailFiling')
  requests = []
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    requests.push({ method: req.method, url: req.url })
    if (path.endsWith('/folders') || path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'z', name: 'Zebra', position: 1, version: 1 }, { id: 'a', name: 'Alpha', position: 9, version: 1 }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  await screen.findByText('Alpha')
  const rows = Array.from(document.querySelectorAll('.le-filing-row'))
  expect(rows.map(row => row.querySelector('strong')?.textContent)).toEqual(['Alpha', 'Zebra'])
  expect(within(rows[0] as HTMLElement).getByText('Reihenfolge: 9')).toBeTruthy()
  expect(within(rows[1] as HTMLElement).getByText('Reihenfolge: 1')).toBeTruthy()
  expect(requests.every(request => request.method === 'GET')).toBe(true)
})

test('applying a rule to existing mail requires confirmation', async () => {
  const { RuleSettings } = await import('./MailFiling')
  requests = []
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    const body = req.method === 'POST' ? await req.clone().json() : undefined
    requests.push({ method: req.method, url: req.url, body })
    if (path.endsWith('/apply')) return Response.json({ matched: 1 })
    if (path.endsWith('/runs')) return Response.json([])
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Projects', version: 1, sender_domain: 'example.test', folder_id: 'folder', mark_read: true, is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  await screen.findByText('Projects')
  expect(screen.getByText('Bedingungen: Absenderdomain: example.test')).toBeTruthy()
  expect(screen.getByText('Aktionen: Ordner: Filed · Als gelesen markieren')).toBeTruthy()
  expect(screen.getByText('Aktiv')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Letzte Ausführungen' }))
  expect(await screen.findByText('Noch keine Ausführungen.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  expect(requests.some(request => request.url.includes('/apply'))).toBe(false)
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  await waitFor(() => expect(requests.some(request => request.url.includes('/apply') && (request.body as { confirm_apply: boolean }).confirm_apply)).toBe(true))
  expect(await screen.findByText('1 Mail zur Verarbeitung vorgemerkt.')).toBeTruthy()
})

test('visible rule outcomes refresh after an initially empty result', async () => {
  const { RuleSettings } = await import('./MailFiling')
  let runReads = 0
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/runs')) return Response.json(++runReads === 1 ? [] : [{ id: 'run', status: 'done', provider_results_json: '{"mark_read":"skipped_unsupported"}' }])
    if (path.endsWith('/folders') || path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Example', version: 1, sender_domain: 'example.test', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Letzte Ausführungen' }))
  expect(await screen.findByText('Noch keine Ausführungen.')).toBeTruthy()
  expect(await screen.findByText('Beim Anbieter übersprungen', {}, { timeout: 6000 })).toBeTruthy()
})

test('an overridden folder is not presented as a completed filing', async () => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/runs')) return Response.json([{ id: 'run', status: 'done', provider_results_json: '{"_folder":"overridden"}' }])
    if (path.endsWith('/folders') || path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Example', version: 1, sender_domain: 'example.test', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Letzte Ausführungen' }))
  expect(await screen.findByText('Ordner nicht zugeordnet: Eine höher priorisierte Regel hat Vorrang.')).toBeTruthy()
  expect(screen.queryByText('App-Zuordnung gespeichert')).toBeNull()
})

test.each([
  ['skipped_imap_rebuild', /Nach dem Neuaufbau dieses IMAP-Postfachs.*Konto: Work/],
  ['skipped_initial_import', /Beim Import dieses Kontos ohne gespeicherten Synchronisationsstand.*Konto: Work/],
])('a %s notice names the account and offers manual application', async (status, notice) => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/runs')) return Response.json([{ id: 'run', account_id: 'account', status, provider_results_json: '{}' }])
    if (path.endsWith('/folders')) return Response.json([])
    if (path.endsWith('/accounts')) return Response.json([{ id: 'account', display_name: 'Work', email_address: 'work@example.test' }])
    return Response.json([{ id: 'rule', name: 'Example', version: 1, sender_domain: 'example.test', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Letzte Ausführungen' }))
  expect(await screen.findByText(notice)).toBeTruthy()
  expect(screen.getByText(/Zusammenfassungen für ungelesene Mails werden wie gewohnt erstellt/)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Auf vorhandene Mails anwenden' })).toBeTruthy()
})

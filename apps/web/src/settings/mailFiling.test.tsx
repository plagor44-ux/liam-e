// i18n-exempt: synthetic API responses and test labels.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Link, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { client as api } from '../api/generated/client.gen'
import * as sdk from '../api/generated/sdk.gen'
import { spyOn, mock } from 'bun:test'
import { FolderSettings } from './MailFiling'
import { I18nProvider } from '../i18n/I18n'

const original = globalThis.fetch, config = api.getConfig()
let requests: { method: string; url: string; body?: unknown }[] = []
afterEach(() => { cleanup(); mock.restore(); globalThis.fetch = original; api.setConfig(config) })

function inventoryPage(rows: { id: string; name: string; kind: string; color?: string }[], request: Request) {
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

test('folder settings save a named palette colour with a destination', async () => {
  requests = []
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    requests.push({ method: req.method, url: req.url, body: req.method === 'POST' ? await req.clone().json() : undefined })
    if (req.method === 'POST') return Response.json({ id: 'new', name: 'Projects', kind: 'folder', color: 'moss' })
    return inventoryPage([], req)
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FolderSettings /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Projects' } })
  fireEvent.click(screen.getByRole('button', { name: 'Farbe: Keine Farbe' }))
  expect(screen.getByRole('menuitemradio', { name: 'Moos' }).querySelector('.le-destination-moss')).toBeTruthy()
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Moos' }))
  expect(screen.getByRole('button', { name: 'Farbe: Moos' }).querySelector('.le-destination-moss')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Anlegen' }))
  await waitFor(() => expect(requests.find(request => request.method === 'POST')?.body).toEqual({ name: 'Projects', kind: 'folder', color: 'moss' }))
})

test('editing a destination changes its colour without changing its kind', async () => {
  requests = []
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    requests.push({ method: req.method, url: req.url, body: req.method === 'PATCH' ? await req.clone().json() : undefined })
    if (req.method === 'PATCH') return Response.json({ id: 'folder', name: 'Projects', kind: 'folder', color: 'moss' })
    return inventoryPage([{ id: 'folder', name: 'Projects', kind: 'folder', color: 'teal' }], req)
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FolderSettings /></QueryClientProvider>)
  await screen.findByText('Projects')
  expect(document.querySelector('.le-filing-row .le-destination-teal')).toBeTruthy()
  const info = document.querySelector('.le-filing-row-info')!
  expect(info.querySelector('.le-filing-row-name .lucide-folder')).toBeTruthy()
  expect(within(info as HTMLElement).getByText('Projects')).toBeTruthy()
  expect(within(info as HTMLElement).getByText('Ordner ·')).toBeTruthy()
  const actions = document.querySelector('.le-filing-row-actions')!
  expect(within(actions as HTMLElement).getByRole('button', { name: 'Umbenennen' })).toBeTruthy()
  expect(within(actions as HTMLElement).getByRole('button', { name: 'Projects löschen' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Umbenennen' }))
  fireEvent.click(screen.getByRole('button', { name: 'Farbe: Petrol' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Moos' }))
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  await waitFor(() => expect(requests.find(request => request.method === 'PATCH')?.body).toEqual({ name: 'Projects', color: 'moss' }))
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

test('a mail offers editable rule conditions and saves only the selected ones', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailMessageGet').mockResolvedValue({ data: { id: 'canonical', account_id: 'account', sender_name: 'Example Sender', sender_address: 'person@example.org', subject: 'Example subject' } } as never)
  const create = spyOn(sdk, 'mailRuleCreateFromMessage').mockResolvedValue({ data: { id: 'rule', name: 'Example' } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Projects', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([{ id: 'account', display_name: 'Work', email_address: 'work@example.org' }])
    return Response.json([])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings sourceMessageId="old-id" /></QueryClientProvider>)
  await waitFor(() => expect((screen.getByLabelText('Absenderadresse') as HTMLInputElement).value).toBe('person@example.org'))
  expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('Example Sender')
  expect(screen.queryByRole('region', { name: 'Ungespeicherte Änderungen' })).toBeNull()
  expect((screen.getByLabelText('Absenderdomain') as HTMLInputElement).value).toBe('example.org')
  expect((screen.getByLabelText('Betreff enthält') as HTMLInputElement).value).toBe('Example subject')
  expect(screen.getByRole('switch', { name: 'Absenderdomain verwenden' }).getAttribute('aria-checked')).toBe('false')
  expect(create).not.toHaveBeenCalled()
  expect((screen.getByRole('button', { name: 'Regel anlegen' }) as HTMLButtonElement).disabled).toBe(true)
  const targetHint = screen.getByText('Wähle einen Ordner oder ein Label, um die Regel anzulegen.')
  expect(screen.getByRole('button', { name: 'Regel anlegen' }).getAttribute('aria-describedby')).toBe(targetHint.id)
  fireEvent.submit(screen.getByRole('button', { name: 'Regel anlegen' }).closest('form')!)
  expect(create).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Konto: Work' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Alle Konten' }))
  fireEvent.click(screen.getByRole('switch', { name: 'Absenderadresse verwenden' }))
  fireEvent.click(screen.getByRole('switch', { name: 'Absenderdomain verwenden' }))
  fireEvent.change(screen.getByLabelText('Betreff enthält'), { target: { value: 'Edited subject' } })
  fireEvent.click(screen.getByRole('switch', { name: 'Betreff enthält verwenden' }))
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'Example' } })
  fireEvent.click(screen.getByRole('button', { name: 'Ordner: Keine Auswahl' }))
  fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Projects' }))
  expect((screen.getByRole('button', { name: 'Regel anlegen' }) as HTMLButtonElement).disabled).toBe(false)
  expect(screen.getByRole('button', { name: 'Regel anlegen' }).hasAttribute('aria-describedby')).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Regel anlegen' }))
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
  expect(create.mock.calls[0]?.[0]?.body).toMatchObject({ source_message_id: 'old-id', source_account_id: 'account', account_id: null, sender_address: '', sender_domain: 'example.org', subject_contains: 'Edited subject', folder_id: 'folder' })
})

test('a removed source blocks rule creation and remains visible to the owner', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailMessageGet').mockResolvedValue({ data: { id: 'mail', account_id: 'account', sender_address: 'person@example.org', subject: 'Example' } } as never)
  const create = spyOn(sdk, 'mailRuleCreateFromMessage').mockRejectedValue({ detail: { code: 'message_not_found' } })
  const ordinaryCreate = spyOn(sdk, 'mailRuleCreate')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Projects', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([{ id: 'account', display_name: 'Work', email_address: 'work@example.org' }])
    return Response.json([])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings sourceMessageId="mail" /></QueryClientProvider>)
  await waitFor(() => expect((screen.getByLabelText('Absenderadresse') as HTMLInputElement).value).toBe('person@example.org'))
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'Example' } })
  fireEvent.click(screen.getByRole('button', { name: 'Ordner: Keine Auswahl' }))
  fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Projects' }))
  fireEvent.click(screen.getByRole('button', { name: 'Regel anlegen' }))
  expect(await screen.findByText('Diese Mail ist nicht mehr verfügbar. Es wurde nichts gespeichert.')).toBeTruthy()
  expect(create).toHaveBeenCalledTimes(1)
  expect(ordinaryCreate).not.toHaveBeenCalled()
  expect((screen.getByRole('button', { name: 'Regel anlegen' }) as HTMLButtonElement).disabled).toBe(true)
})

test('a missing source account is not silently presented as all accounts', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailMessageGet').mockResolvedValue({ data: { id: 'mail', account_id: 'removed', sender_address: 'person@example.org', subject: 'Example' } } as never)
  const create = spyOn(sdk, 'mailRuleCreateFromMessage')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async () => Response.json([])) as unknown as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings sourceMessageId="mail" /></QueryClientProvider>)
  expect(await screen.findByText('Das Konto dieser Mail ist nicht mehr verfügbar. Erstelle bei Bedarf eine neue Regel ohne diesen Bezug.')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'Example' } })
  expect((screen.getByRole('button', { name: 'Regel anlegen' }) as HTMLButtonElement).disabled).toBe(true)
  expect(create).not.toHaveBeenCalled()
})

test('leaving a prefilled rule warns and keeps edits when the owner stays', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailMessageGet').mockResolvedValue({ data: { id: 'mail', account_id: 'account', sender_address: 'person@example.org', subject: 'Example' } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async () => Response.json([])) as unknown as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><Link to="/accounts">Leave</Link><Outlet /></QueryClientProvider> })
  const rules = createRoute({ getParentRoute: () => root, path: '/rules', component: () => <RuleSettings sourceMessageId="mail" /> })
  const other = createRoute({ getParentRoute: () => root, path: '/accounts', component: () => <p>Other</p> })
  const router = createRouter({ routeTree: root.addChildren([rules, other]), history: createMemoryHistory({ initialEntries: ['/rules'] }) })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect((screen.getByLabelText('Absenderadresse') as HTMLInputElement).value).toBe('person@example.org'))
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'Draft' } })
  fireEvent.click(screen.getByRole('link', { name: 'Leave' }))
  const dialog = await screen.findByRole('dialog')
  fireEvent.click(within(dialog).getByRole('button', { name: 'Weiter bearbeiten' }))
  expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('Draft')
  expect(router.state.location.pathname).toBe('/rules')
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
    if (path.endsWith('/apply')) return Response.json({ matched: 1, app_changed: 1, provider_skipped: 1, provider_queued: 0, conflicts: 0 })
    if (path.endsWith('/preview')) return Response.json({ matched: 1, already_filed: 1, message_ids: ['synthetic-message'] })
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
  expect(await within(screen.getByRole('dialog')).findByText(/1 passende Mail/)).toBeTruthy()
  expect(within(screen.getByRole('dialog')).getByText('1 bereits zugeordnet: Der Ordner bleibt erhalten.').tagName).toBe('P')
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  await waitFor(() => expect(requests.some(request => request.url.includes('/apply') && (request.body as { confirm_apply: boolean }).confirm_apply)).toBe(true))
  expect((requests.find(request => request.url.includes('/apply'))?.body as { preview_message_ids: string[] }).preview_message_ids).toEqual(['synthetic-message'])
  expect(await screen.findByText('App-Zuordnungen geändert: 1. Anbieteraktionen bereits behandelt oder unklar: 1; vorgemerkt oder laufend: 0. Konflikte: 0 (Mails seit der Vorschau geändert; nicht überschrieben).')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  expect(screen.queryByText('App-Zuordnungen geändert: 1. Anbieteraktionen bereits behandelt oder unklar: 1; vorgemerkt oder laufend: 0. Konflikte: 0 (Mails seit der Vorschau geändert; nicht überschrieben).')).toBeNull()
})

test('a changed rule requires a fresh preview before applying', async () => {
  const { RuleSettings } = await import('./MailFiling')
  let ruleReads = 0
  const previewVersions: number[] = []
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/apply')) return Response.json({ detail: { code: 'rule_version_conflict' } }, { status: 409 })
    if (path.endsWith('/preview')) { previewVersions.push((await req.clone().json()).expected_version); return Response.json({ matched: 1, already_filed: 0, message_ids: ['synthetic-message'] }) }
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Projects', version: ++ruleReads === 1 ? 1 : 2, sender_domain: 'example.test', folder_id: 'folder', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  const dialog = screen.getByRole('dialog')
  expect(await within(dialog).findByText(/1 passende Mail/)).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  expect(await within(dialog).findByText('Die Vorschau ist nicht mehr aktuell oder nicht verfügbar. Lade sie erneut.')).toBeTruthy()
  expect(within(dialog).queryByText('1 passende Mail')).toBeNull()
  expect((within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(within(dialog).getByRole('button', { name: 'Vorschau erneut laden' }))
  expect(await within(dialog).findByText('1 passende Mail')).toBeTruthy()
  expect(previewVersions.at(-1)).toBe(2)
  expect((within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }) as HTMLButtonElement).disabled).toBe(false)
})

test('starting an editor preview clears an earlier apply result', async () => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/apply')) return Response.json({ matched: 1, app_changed: 1, provider_skipped: 0, provider_queued: 0, conflicts: 0 })
    if (path.endsWith('/preview')) return Response.json({ matched: 1, already_filed: 0, message_ids: ['mail'] })
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Projects', version: 1, sender_domain: 'example.test', folder_id: 'folder', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  const dialog = screen.getByRole('dialog')
  expect(await within(dialog).findByText('1 passende Mail')).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  const result = 'App-Zuordnungen geändert: 1. Anbieteraktionen bereits behandelt oder unklar: 0; vorgemerkt oder laufend: 0. Konflikte: 0 (Mails seit der Vorschau geändert; nicht überschrieben).'
  expect(await screen.findByText(result)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Regel bearbeiten' }))
  fireEvent.click(screen.getByRole('button', { name: 'Vorschau' }))
  expect(screen.queryByText(result)).toBeNull()
  expect(await screen.findByText('1 passende Mail')).toBeTruthy()
})

test('failed preview refresh hides cached counts and disables confirmation until retry', async () => {
  const { RuleSettings } = await import('./MailFiling')
  let calls = 0
  const preview = spyOn(sdk, 'mailRulesPreview').mockImplementation(async () => {
    calls++
    if (calls === 2) throw { detail: { code: 'rule_version_conflict' } }
    return { data: { matched: calls === 1 ? 2 : 1, already_filed: 0, message_ids: ['mail'] } } as never
  })
  let ruleReads = 0
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Projects', version: ++ruleReads === 1 ? 1 : 2, sender_domain: 'example.test', folder_id: 'folder', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  const dialog = screen.getByRole('dialog')
  expect(await within(dialog).findByText('2 passende Mails')).toBeTruthy()
  expect(within(dialog).queryByText(/bereits zugeordnet/u)).toBeNull()
  await act(async () => { await client.invalidateQueries({ queryKey: ['mail-rule-apply-preview'] }) })
  await waitFor(() => expect(within(dialog).queryByText('2 passende Mails')).toBeNull())
  expect(within(dialog).getByText('Die Vorschau ist nicht mehr aktuell oder nicht verfügbar. Lade sie erneut.')).toBeTruthy()
  expect((within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(within(dialog).getByRole('button', { name: 'Vorschau erneut laden' }))
  expect(await within(dialog).findByText('1 passende Mail')).toBeTruthy()
  expect(preview.mock.calls[2]?.[0]?.body.expected_version).toBe(2)
  expect((within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }) as HTMLButtonElement).disabled).toBe(false)
})

test('English apply preview omits zero filed mail and explains conflicts', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailRulesPreview').mockResolvedValue({ data: { matched: 1, already_filed: 0, message_ids: ['mail'] } } as never)
  spyOn(sdk, 'mailRuleApply').mockResolvedValue({ data: { matched: 0, app_changed: 0, provider_skipped: 0, provider_queued: 0, conflicts: 1 } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Projects', version: 1, sender_domain: 'example.test', folder_id: 'folder', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<I18nProvider locale="en"><QueryClientProvider client={client}><RuleSettings /></QueryClientProvider></I18nProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Apply to existing emails' }))
  const dialog = screen.getByRole('dialog')
  expect(await within(dialog).findByText('1 matching email')).toBeTruthy()
  expect(within(dialog).queryByText(/already filed/u)).toBeNull()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Apply to existing emails' }))
  expect(await screen.findByText('App filings changed: 0. Provider actions previously handled or unknown: 0; queued or in progress: 0. Conflicts: 1 (emails changed since the preview; not overwritten).')).toBeTruthy()
})

test('paused rules show why manual application is disabled', async () => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Paused', version: 1, sender_domain: 'example.test', folder_id: 'folder', is_active: false }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  const button = await screen.findByRole('button', { name: 'Auf vorhandene Mails anwenden' }) as HTMLButtonElement
  const reason = screen.getByText('Die Regel ist pausiert. Aktiviere sie, um fortzufahren.')
  expect(button.disabled).toBe(true)
  expect(button.getAttribute('aria-describedby')).toBe(reason.id)
  const row = button.closest('.le-filing-row') as HTMLElement
  expect(reason.parentElement).toBe(row)
  expect(within(row).getByRole('button', { name: 'Letzte Ausführungen' }).compareDocumentPosition(reason) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const css = await Bun.file(new URL('../prompts/prompts.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-rule-inactive-reason\s*\{[^}]*flex-basis:\s*100%/u)
})

test('a paused editor preview explains how to continue without referring to applying', async () => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/preview')) return Response.json({ detail: { code: 'rule_inactive' } }, { status: 409 })
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Paused', version: 1, sender_domain: 'example.test', folder_id: 'folder', is_active: false }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  fireEvent.click(screen.getByRole('button', { name: 'Vorschau' }))
  await waitFor(() => expect(screen.getAllByText('Die Regel ist pausiert. Aktiviere sie, um fortzufahren.')).toHaveLength(2))
  expect(screen.queryByText(/um sie anzuwenden/u)).toBeNull()
})

test('reactivating a paused draft previews its active values without saving', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const preview = spyOn(sdk, 'mailRulesPreview').mockResolvedValue({ data: { matched: 1, already_filed: 0, message_ids: ['mail'] } } as never)
  const save = spyOn(sdk, 'mailRuleUpdate')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Paused', version: 2, sender_domain: 'example.test', folder_id: 'folder', is_active: false }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  fireEvent.click(screen.getByRole('switch', { name: /Regel aktiv/u }))
  fireEvent.click(screen.getByRole('button', { name: 'Vorschau' }))
  expect(await screen.findByText('1 passende Mail')).toBeTruthy()
  expect(preview.mock.calls[0]?.[0]?.body).toMatchObject({ saved_rule_id: 'rule', expected_version: 2, preview_draft: true, is_active: true })
  expect(save).not.toHaveBeenCalled()
})

for (const stage of ['preview', 'apply'] as const) {
  test(`a stale tab shows the paused-rule reason during ${stage}`, async () => {
    const { RuleSettings } = await import('./MailFiling')
    api.setConfig({ baseUrl: window.location.origin })
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input : new Request(input, init)
      const path = new URL(req.url).pathname
      if (path.endsWith(`/${stage}`)) return Response.json({ detail: { code: 'rule_inactive' } }, { status: 409 })
      if (path.endsWith('/preview')) return Response.json({ matched: 1, already_filed: 0, message_ids: ['synthetic-message'] })
      if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Filed', kind: 'folder' }])
      if (path.endsWith('/accounts')) return Response.json([])
      return Response.json([{ id: 'rule', name: 'Projects', version: 1, sender_domain: 'example.test', folder_id: 'folder', is_active: true }])
    }) as typeof fetch
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
    const dialog = screen.getByRole('dialog')
    if (stage === 'apply') {
      expect(await within(dialog).findByText(/1 passende Mail/)).toBeTruthy()
      fireEvent.click(within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
    }
    expect(await screen.findByText('Die Regel ist pausiert. Aktiviere sie, um fortzufahren.')).toBeTruthy()
    if (stage === 'preview') expect((within(dialog).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }) as HTMLButtonElement).disabled).toBe(true)
  })
}

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


test('source initialization blocks edits and later updates preserve the typed draft', async () => {
  const { RuleSettings } = await import('./MailFiling')
  let resolveSource!: (value: unknown) => void
  spyOn(sdk, 'mailMessageGet').mockImplementation((() => new Promise(resolve => { resolveSource = resolve })) as never)
  spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'folder', name: 'Projects', kind: 'folder' }] } as never)
  spyOn(sdk, 'mailAccountsList').mockResolvedValue({ data: [{ id: 'account', display_name: 'Work' }] } as never)
  spyOn(sdk, 'mailRulesList').mockResolvedValue({ data: [{ id: 'rule', name: 'Existing', version: 1 }] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings sourceMessageId="mail" /></QueryClientProvider>)
  const name = screen.getByLabelText('Name der Regel') as HTMLInputElement
  expect(name.matches(':disabled')).toBe(true)
  expect(await screen.findByRole('button', { name: 'Regel bearbeiten' })).toHaveProperty('disabled', true)
  expect(screen.getAllByRole('status').some(node => node.textContent?.includes('Wird geladen'))).toBe(true)
  const source = { id: 'mail', account_id: 'account', sender_name: 'Original', sender_address: 'person@example.org', subject: 'First' }
  await act(async () => { resolveSource({ data: source }); await Promise.resolve() })
  await waitFor(() => expect(name.matches(':disabled')).toBe(false))
  fireEvent.change(name, { target: { value: 'My draft' } })
  act(() => client.setQueryData(['message', 'mail'], { ...source, sender_name: 'Late response', subject: 'Changed' }))
  expect(name.value).toBe('My draft')
  expect((screen.getByLabelText('Betreff enthält') as HTMLInputElement).value).toBe('First')
})

test('late source updates never replace an existing rule draft or its save', async () => {
  const { RuleSettings } = await import('./MailFiling')
  let resolveSource!: (value: unknown) => void
  spyOn(sdk, 'mailMessageGet').mockImplementation((() => new Promise(resolve => { resolveSource = resolve })) as never)
  spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'folder', name: 'Projects', kind: 'folder' }] } as never)
  spyOn(sdk, 'mailAccountsList').mockResolvedValue({ data: [{ id: 'account', display_name: 'Work' }] } as never)
  spyOn(sdk, 'mailRulesList').mockResolvedValue({ data: [{ id: 'rule', name: 'Existing', version: 3, sender_domain: 'example.org', folder_id: 'folder', is_active: true }] } as never)
  const update = spyOn(sdk, 'mailRuleUpdate').mockResolvedValue({ data: { id: 'rule' } } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings sourceMessageId="mail" /></QueryClientProvider>)
  const edit = await screen.findByRole('button', { name: 'Regel bearbeiten' }) as HTMLButtonElement
  expect(edit.disabled).toBe(true)
  const source = { id: 'mail', account_id: 'account', sender_name: 'Source', sender_address: 'person@example.org', subject: 'Original' }
  await act(async () => { resolveSource({ data: source }); await Promise.resolve() })
  await waitFor(() => expect(edit.disabled).toBe(false))
  fireEvent.click(edit)
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'Edited existing' } })
  act(() => client.setQueryData(['message', 'mail'], { ...source, sender_name: 'Late', subject: 'Unrelated' }))
  expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('Edited existing')
  expect((screen.getByLabelText('Absenderdomain') as HTMLInputElement).value).toBe('example.org')
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
  expect(update.mock.calls[0]?.[0]?.body).toMatchObject({ name: 'Edited existing', expected_version: 3, sender_domain: 'example.org', folder_id: 'folder' })
})

test('source load failure unlocks an empty manual rule draft', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailMessageGet').mockRejectedValue(new Error('unavailable'))
  spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'mailAccountsList').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'mailRulesList').mockResolvedValue({ data: [] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings sourceMessageId="mail" /></QueryClientProvider>)
  expect(await screen.findByText('Die Quellmail konnte nicht geladen werden. Du kannst die Regel manuell erstellen.')).toBeTruthy()
  const name = screen.getByLabelText('Name der Regel') as HTMLInputElement
  expect(name.matches(':disabled')).toBe(false)
  expect(name.value).toBe('')
  fireEvent.change(name, { target: { value: 'Manual' } })
  expect(name.value).toBe('Manual')
})


test('cancel during source loading ignores a late response', async () => {
  const { RuleSettings } = await import('./MailFiling')
  let resolveSource!: (value: unknown) => void
  spyOn(sdk, 'mailMessageGet').mockImplementation((() => new Promise(resolve => { resolveSource = resolve })) as never)
  spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'mailAccountsList').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'mailRulesList').mockResolvedValue({ data: [] } as never)
  const sourceUsed = mock(() => {})
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings sourceMessageId="mail" onSourceUsed={sourceUsed} /></QueryClientProvider>)
  const cancel = screen.getByRole('button', { name: 'Abbrechen' }) as HTMLButtonElement
  expect(cancel.matches(':disabled')).toBe(false)
  fireEvent.click(cancel)
  expect(sourceUsed).toHaveBeenCalledTimes(1)
  const name = screen.getByLabelText('Name der Regel') as HTMLInputElement
  expect(name.matches(':disabled')).toBe(false)
  fireEvent.change(name, { target: { value: 'Manual draft' } })
  await act(async () => { resolveSource({ data: { id: 'mail', account_id: 'account', sender_name: 'Late', sender_address: 'late@example.org', subject: 'Late' } }); await Promise.resolve() })
  expect(name.value).toBe('Manual draft')
  expect(name.matches(':disabled')).toBe(false)
  expect(screen.queryByText('Vorschläge aus dieser Mail: Wähle die Bedingungen, die du verwenden möchtest.')).toBeNull()
})

test('rule history describes reconciliation, partial writes and permission independently', async () => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL((input instanceof Request ? input : new Request(input, init)).url).pathname
    if (path.endsWith('/runs')) return Response.json([{
      id: 'run', status: 'done', provider_results_json: '{"mark_read":"reconciled","archive":"partial_permission"}',
    }])
    if (path.endsWith('/folders') || path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Example', version: 1, sender_domain: 'example.test', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Letzte Ausführungen' }))
  expect(await screen.findByText(/Die Mail wurde nicht verschoben/)).toBeTruthy()
  expect(screen.queryByText(/Die Liste zeigt ihren aktuellen Ordner/)).toBeNull()
  expect(screen.getByText(/Der Anbieter hat nur einen Teil der Änderung ausgeführt/)).toBeTruthy()
  expect(screen.getByText(/Für diese Aktion fehlt dem Connector die Berechtigung/)).toBeTruthy()
})

test('rule editor saves multiple labels and lists their names', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const create = spyOn(sdk, 'mailRuleCreate').mockResolvedValue({ data: { id: 'rule', name: 'Example' } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'one', name: 'Important', kind: 'label' }, { id: 'two', name: 'Work', kind: 'label' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'old', name: 'Existing', sender_domain: 'example.org', label_ids: ['one', 'two'], version: 1 }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  await screen.findByRole('checkbox', { name: 'Important' })
  const row = screen.getByText('Existing').closest('.le-filing-row') as HTMLElement
  expect(within(row).getByText(/Important/).textContent).toContain('Work')
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'Example' } })
  fireEvent.change(screen.getByLabelText('Absenderdomain'), { target: { value: 'example.org' } })
  fireEvent.click(screen.getByRole('checkbox', { name: 'Important' }))
  fireEvent.click(screen.getByRole('checkbox', { name: 'Work' }))
  fireEvent.click(screen.getByRole('button', { name: 'Regel anlegen' }))
  await waitFor(() => expect(create).toHaveBeenCalled())
  expect(create.mock.calls[0]?.[0]?.body.label_ids).toEqual(['one', 'two'])
})

test('editing a rule restores all labels and saves the selected set', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const update = spyOn(sdk, 'mailRuleUpdate').mockResolvedValue({ data: { id: 'rule', name: 'Example' } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'one', name: 'Important', kind: 'label' }, { id: 'two', name: 'Work', kind: 'label' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Example', sender_domain: 'example.org', label_ids: ['one', 'two'], version: 3 }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  expect((screen.getByRole('checkbox', { name: 'Important' }) as HTMLInputElement).checked).toBe(true)
  expect((screen.getByRole('checkbox', { name: 'Work' }) as HTMLInputElement).checked).toBe(true)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Important' }))
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  await waitFor(() => expect(update).toHaveBeenCalled())
  expect(update.mock.calls[0]?.[0]?.body.label_ids).toEqual(['two'])
  expect(update.mock.calls[0]?.[0]?.body.expected_version).toBe(3)
})

test('rule preview explains already-filed matches keep their folders', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailRulesPreview').mockResolvedValue({ data: { matched: 3, already_filed: 2 } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (_input: RequestInfo | URL, _init?: RequestInit) => Response.json([])) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'Example' } })
  fireEvent.click(screen.getByRole('button', { name: 'Vorschau' }))
  expect(await screen.findByText('3 passende Mails')).toBeTruthy()
  expect(screen.getByText('2 bereits zugeordnet: Der Ordner bleibt erhalten.')).toBeTruthy()
})

test('previewing an edited saved rule sends its identity and current draft', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const preview = spyOn(sdk, 'mailRulesPreview').mockResolvedValue({ data: { matched: 1, already_filed: 0, message_ids: ['mail'] } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'folder', name: 'Projects', kind: 'folder' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Example', sender_domain: 'example.org', folder_id: 'folder', version: 3, is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  fireEvent.change(screen.getByLabelText('Betreff enthält'), { target: { value: 'Changed subject' } })
  fireEvent.click(screen.getByRole('button', { name: 'Vorschau' }))
  await waitFor(() => expect(preview).toHaveBeenCalled())
  expect(preview.mock.calls[0]?.[0]?.body).toMatchObject({ saved_rule_id: 'rule', expected_version: 3, preview_draft: true, subject_contains: 'Changed subject' })
  expect(await screen.findByText('1 passende Mail')).toBeTruthy()
})

for (const stage of ['preview', 'save'] as const) {
  for (const choice of ['keep', 'discard'] as const) {
    test(`rule ${stage} conflict lets the owner ${choice} a draft against the latest version`, async () => {
      const { RuleSettings } = await import('./MailFiling')
      let current = { id: 'rule', name: 'Original', sender_domain: 'example.org', folder_id: 'folder', version: 1, is_active: true }
      spyOn(sdk, 'mailRulesList').mockImplementation((async () => ({ data: [current] })) as never)
      const preview = spyOn(sdk, 'mailRulesPreview').mockImplementation((async (options: { body: { expected_version: number } }) => {
        if (options.body.expected_version !== current.version) throw { detail: { code: 'rule_version_conflict' } }
        return { data: { matched: 1, already_filed: 0, message_ids: [] } }
      }) as never)
      const update = spyOn(sdk, 'mailRuleUpdate').mockImplementation((async (options: { body: { expected_version: number } }) => {
        if (options.body.expected_version !== current.version) throw { detail: { code: 'rule_version_conflict' } }
        return { data: current }
      }) as never)
      api.setConfig({ baseUrl: window.location.origin })
      globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL((input instanceof Request ? input : new Request(input, init)).url).pathname
        return Response.json(path.endsWith('/folders') ? [{ id: 'folder', name: 'Projects', kind: 'folder' }] : [])
      }) as typeof fetch
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
      fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
      fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'My draft' } })
      current = { ...current, name: 'Changed elsewhere', version: 2 }
      fireEvent.click(screen.getByRole('button', { name: stage === 'preview' ? 'Vorschau' : 'Speichern' }))
      expect(await screen.findByText(/Die Regel wurde an anderer Stelle geändert/u)).toBeTruthy()
      expect((screen.getByRole('button', { name: 'Speichern' }) as HTMLButtonElement).disabled).toBe(true)
      expect((screen.getByRole('button', { name: 'Vorschau' }) as HTMLButtonElement).disabled).toBe(true)
      fireEvent.click(screen.getByRole('button', { name: choice === 'keep' ? 'Entwurf behalten' : 'Gespeicherte Regel laden' }))
      await waitFor(() => expect(screen.queryByText(/Die Regel wurde an anderer Stelle geändert/u)).toBeNull())
      expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe(choice === 'keep' ? 'My draft' : 'Changed elsewhere')
      fireEvent.click(screen.getByRole('button', { name: 'Vorschau' }))
      expect(await screen.findByText('1 passende Mail')).toBeTruthy()
      expect(preview.mock.calls.at(-1)?.[0]?.body).toMatchObject({ expected_version: 2, name: choice === 'keep' ? 'My draft' : 'Changed elsewhere' })
      fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
      await waitFor(() => expect(update.mock.calls.at(-1)?.[0]?.body).toMatchObject({ expected_version: 2, name: choice === 'keep' ? 'My draft' : 'Changed elsewhere' }))
    })
  }
}

test('a failed conflict reload preserves the draft and blocks save', async () => {
  const { RuleSettings } = await import('./MailFiling')
  let missing = false
  spyOn(sdk, 'mailRulesList').mockImplementation((async () => ({ data: missing ? [] : [{ id: 'rule', name: 'Original', sender_domain: 'example.org', folder_id: 'folder', version: 1, is_active: true }] })) as never)
  spyOn(sdk, 'mailRulesPreview').mockRejectedValue({ detail: { code: 'rule_version_conflict' } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL((input instanceof Request ? input : new Request(input, init)).url).pathname
    return Response.json(path.endsWith('/folders') ? [{ id: 'folder', name: 'Projects', kind: 'folder' }] : [])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'My draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Vorschau' }))
  await screen.findByText(/Die Regel wurde an anderer Stelle geändert/u)
  missing = true
  fireEvent.click(screen.getByRole('button', { name: 'Entwurf behalten' }))
  expect(await screen.findByText(/Die Regel ist nicht mehr vorhanden/u)).toBeTruthy()
  expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('My draft')
  expect((screen.getByRole('button', { name: 'Speichern' }) as HTMLButtonElement).disabled).toBe(true)
  // The conflict choices no longer apply; the missing notice offers its own way out.
  expect(screen.queryByText(/Die Regel wurde an anderer Stelle geändert/u)).toBeNull()
  expect(screen.getByRole('button', { name: 'Als neue Regel behalten' })).toBeTruthy()
})

const ruleMissingText = 'Die Regel ist nicht mehr vorhanden. Dein Entwurf bleibt erhalten: Verwirf ihn oder behalte ihn als neue Regel.'
const genericRuleError = 'Die Änderung konnte nicht ausgeführt werden. Versuch es noch einmal.'

function deletedRuleSetup(failure: unknown) {
  const saved = { id: 'rule', name: 'Original', sender_domain: 'example.org', folder_id: 'folder', version: 1, is_active: true }
  const state = { deleted: false }
  spyOn(sdk, 'mailRulesList').mockImplementation((async () => ({ data: state.deleted ? [] : [saved] })) as never)
  const preview = spyOn(sdk, 'mailRulesPreview').mockImplementation((async () => {
    if (state.deleted) throw failure
    return { data: { matched: 1, already_filed: 0, message_ids: [] } }
  }) as never)
  const update = spyOn(sdk, 'mailRuleUpdate').mockImplementation((async () => { throw failure }) as never)
  const create = spyOn(sdk, 'mailRuleCreate').mockImplementation((async (options: { body: { name: string } }) => ({ data: { ...saved, id: 'new', name: options.body.name } })) as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL((input instanceof Request ? input : new Request(input, init)).url).pathname
    return Response.json(path.endsWith('/folders') ? [{ id: 'folder', name: 'Projects', kind: 'folder' }] : [])
  }) as typeof fetch
  return { state, preview, update, create }
}

for (const stage of ['preview', 'save'] as const) {
  for (const choice of ['discard', 'keep'] as const) {
    test(`a rule deleted elsewhere: ${stage} shows the missing notice and lets the owner ${choice} the draft`, async () => {
      const { RuleSettings } = await import('./MailFiling')
      const { state, update, create } = deletedRuleSetup({ detail: { code: 'rule_not_found' } })
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
      fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
      fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'My draft' } })
      state.deleted = true
      fireEvent.click(screen.getByRole('button', { name: stage === 'preview' ? 'Vorschau' : 'Speichern' }))
      expect(await screen.findByText(ruleMissingText)).toBeTruthy()
      expect(screen.queryByText(genericRuleError)).toBeNull()
      expect(screen.queryByText(/Die Regel wurde an anderer Stelle geändert/u)).toBeNull()
      expect((screen.getByRole('button', { name: 'Speichern' }) as HTMLButtonElement).disabled).toBe(true)
      expect((screen.getByRole('button', { name: 'Vorschau' }) as HTMLButtonElement).disabled).toBe(true)
      expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('My draft')
      // The deleted rule leaves the list once it is reloaded.
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Regel bearbeiten' })).toBeNull())
      const updates = update.mock.calls.length
      if (choice === 'discard') {
        fireEvent.click(screen.getByRole('button', { name: 'Entwurf verwerfen' }))
        await waitFor(() => expect(screen.queryByText(ruleMissingText)).toBeNull())
        expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('')
        expect(screen.queryByRole('button', { name: 'Speichern' })).toBeNull()
        expect(screen.getByRole('button', { name: 'Regel anlegen' })).toBeTruthy()
        expect(create).not.toHaveBeenCalled()
      } else {
        fireEvent.click(screen.getByRole('button', { name: 'Als neue Regel behalten' }))
        await waitFor(() => expect(screen.queryByText(ruleMissingText)).toBeNull())
        expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('My draft')
        expect(screen.queryByRole('button', { name: 'Speichern' })).toBeNull()
        // Keeping the draft does not save it; the owner saves the new rule explicitly.
        expect(create).not.toHaveBeenCalled()
        fireEvent.click(screen.getByRole('button', { name: 'Regel anlegen' }))
        await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
        const body = create.mock.calls[0]?.[0]?.body as Record<string, unknown>
        expect(body).toMatchObject({ name: 'My draft', sender_domain: 'example.org', folder_id: 'folder' })
        expect(body).not.toHaveProperty('expected_version')
      }
      expect(update.mock.calls.length).toBe(updates)
    })
  }
}

test('negative case: other save failures keep the generic error and the edited rule', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const { update } = deletedRuleSetup({ detail: { code: 'rule_invalid' } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'My draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  expect(await screen.findByText(genericRuleError)).toBeTruthy()
  expect(update).toHaveBeenCalledTimes(1)
  expect(screen.queryByText(ruleMissingText)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Als neue Regel behalten' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Speichern' }) as HTMLButtonElement).disabled).toBe(false)
})

test('the English conflict notice ties the replacement to keeping the draft', async () => {
  const { RuleSettings } = await import('./MailFiling')
  spyOn(sdk, 'mailRulesList').mockResolvedValue({ data: [{ id: 'rule', name: 'Original', sender_domain: 'example.org', folder_id: 'folder', version: 1, is_active: true }] } as never)
  spyOn(sdk, 'mailRulesPreview').mockRejectedValue({ detail: { code: 'rule_version_conflict' } } as never)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL((input instanceof Request ? input : new Request(input, init)).url).pathname
    return Response.json(path.endsWith('/folders') ? [{ id: 'folder', name: 'Projects', kind: 'folder' }] : [])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<I18nProvider locale="en"><QueryClientProvider client={client}><RuleSettings /></QueryClientProvider></I18nProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Edit rule' }))
  fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
  const notice = await screen.findByText(/The rule was changed elsewhere/u)
  expect(notice.textContent).toContain('If you keep it, the next save replaces the newer version.')
  // Negative case: no unconditional promise about whatever the next save is.
  expect(notice.textContent).not.toContain('The next save will replace')
  expect(screen.getByRole('button', { name: 'Keep draft' })).toBeTruthy()
})

// Records which elements were scrolled into view, and how; jsdom lays nothing out.
function trackScrolls() {
  const scrolls: { element: Element; options: unknown }[] = []
  spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function (this: Element, options?: unknown) { scrolls.push({ element: this, options }) })
  return scrolls
}
const conflictText = /^Die Regel wurde an anderer Stelle geändert/u

for (const stage of ['preview', 'save'] as const) {
  for (const code of ['rule_not_found', 'rule_version_conflict'] as const) {
    test(`${stage} with ${code} brings the editor notice into view and moves the focus to it`, async () => {
      const { RuleSettings } = await import('./MailFiling')
      const { state } = deletedRuleSetup({ detail: { code } })
      const scrolls = trackScrolls()
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
      fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
      fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'My draft' } })
      state.deleted = true
      const button = screen.getByRole('button', { name: stage === 'preview' ? 'Vorschau' : 'Speichern' })
      act(() => button.focus())
      fireEvent.click(button)
      const group = await screen.findByRole('group', { name: code === 'rule_not_found' ? ruleMissingText : conflictText })
      await waitFor(() => expect(document.activeElement).toBe(group))
      expect(group.getAttribute('tabindex')).toBe('-1')
      expect(scrolls.filter(scroll => scroll.element === group).map(scroll => scroll.options)).toEqual([{ block: 'nearest', behavior: 'smooth' }])
      // Announced once through the focus: no live alert role inside the focused notice.
      expect(within(group).queryByRole('alert')).toBeNull()
    })
  }
}

test('the editor notice scrolls without animation when the owner prefers reduced motion', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const { state } = deletedRuleSetup({ detail: { code: 'rule_not_found' } })
  const scrolls = trackScrolls()
  const matchMedia = window.matchMedia
  window.matchMedia = ((query: string) => ({ matches: query.includes('prefers-reduced-motion') || /min-width/u.test(query), media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })) as unknown as typeof window.matchMedia
  try {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
    state.deleted = true
    fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
    const group = await screen.findByRole('group', { name: ruleMissingText })
    await waitFor(() => expect(document.activeElement).toBe(group))
    expect(scrolls.filter(scroll => scroll.element === group).map(scroll => scroll.options)).toEqual([{ block: 'nearest', behavior: 'auto' }])
  } finally { window.matchMedia = matchMedia }
})

test('negative case: a generic save error leaves the focus and scroll position alone', async () => {
  const { RuleSettings } = await import('./MailFiling')
  deletedRuleSetup({ detail: { code: 'rule_invalid' } })
  const scrolls = trackScrolls()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  const save = screen.getByRole('button', { name: 'Speichern' })
  act(() => save.focus())
  fireEvent.click(save)
  expect(await screen.findByText(genericRuleError)).toBeTruthy()
  expect(document.activeElement).toBe(save)
  expect(screen.queryByRole('group', { name: ruleMissingText })).toBeNull()
  expect(scrolls).toEqual([])
})

test('a kept draft in create mode can be dropped with Abbrechen', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const { state, create } = deletedRuleSetup({ detail: { code: 'rule_not_found' } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Regel bearbeiten' }))
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'My draft' } })
  state.deleted = true
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Als neue Regel behalten' }))
  expect(screen.getByRole('button', { name: 'Regel anlegen' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Abbrechen' }))
  expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('')
  expect((screen.getByLabelText('Absenderdomain') as HTMLInputElement).value).toBe('')
  expect(screen.queryByRole('button', { name: 'Abbrechen' })).toBeNull()
  expect(create).not.toHaveBeenCalled()
})

test('negative case: an empty create form offers no Abbrechen until it holds a draft', async () => {
  const { RuleSettings } = await import('./MailFiling')
  deletedRuleSetup({ detail: { code: 'rule_not_found' } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  await screen.findByRole('button', { name: 'Regel bearbeiten' })
  expect(screen.queryByRole('button', { name: 'Abbrechen' })).toBeNull()
  fireEvent.change(screen.getByLabelText('Name der Regel'), { target: { value: 'New rule' } })
  fireEvent.click(screen.getByRole('button', { name: 'Abbrechen' }))
  expect((screen.getByLabelText('Name der Regel') as HTMLInputElement).value).toBe('')
  expect(screen.queryByRole('button', { name: 'Abbrechen' })).toBeNull()
})

test('label group describes its hint and keeps the label prefix with a long name', async () => {
  const { RuleSettings } = await import('./MailFiling')
  const longName = 'SyntheticLongLabel'.repeat(4)
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders')) return Response.json([{ id: 'label', name: longName, kind: 'label' }])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Active', is_active: true, label_ids: ['label'], version: 1 }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  expect(await screen.findByRole('checkbox', { name: longName })).toBeTruthy()
  const group = screen.getByRole('group', { name: 'Labels' })
  const hint = screen.getByText('Wähle bis zu 20 Labels.')
  expect(group.getAttribute('aria-describedby')).toBe(hint.id)
  const row = screen.getByText('Active').closest('.le-filing-row') as HTMLElement
  expect(row.textContent).toContain(`Label:\u00a0${longName}`)
})

test('paused rules omit empty actions and editing explains the save requirement', async () => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders') || path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Paused', is_active: false, label_ids: [], version: 1 }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  const row = (await screen.findByText('Paused')).closest('.le-filing-row') as HTMLElement
  expect(within(row).queryByText(/^Aktionen:/u)).toBeNull()
  fireEvent.click(within(row).getByRole('button', { name: 'Regel bearbeiten' }))
  const hint = screen.getByText('Wähle einen Ordner oder ein Label, um die Regel zu speichern.')
  expect(screen.getByRole('button', { name: 'Speichern' }).getAttribute('aria-describedby')).toBe(hint.id)
})

test('rule label and summary styles allow long words and leave focus-ring space', async () => {
  const css = await Bun.file(new URL('../styles/ds.css', import.meta.url)).text()
  const settingsCss = await Bun.file(new URL('../prompts/prompts.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-rule-labels label\s*\{[^}]*gap:\s*var\(--space-2\)[^}]*min-width:\s*0/u)
  expect(css).toMatch(/\.le-rule-labels label span\s*\{[^}]*overflow-wrap:\s*anywhere/u)
  expect(settingsCss).toMatch(/\.le-filing-summary\s*\{[^}]*min-width:\s*0/u)
})


test('English rule editing uses the save hint and describes the label group', async () => {
  const { RuleSettings } = await import('./MailFiling')
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const path = new URL(req.url).pathname
    if (path.endsWith('/folders') || path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Paused', is_active: false, label_ids: [], version: 1 }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<I18nProvider locale="en"><QueryClientProvider client={client}><RuleSettings /></QueryClientProvider></I18nProvider>)
  const group = screen.getByRole('group', { name: 'Labels' })
  expect(group.getAttribute('aria-describedby')).toBe(screen.getByText('Choose up to 20 labels.').id)
  expect(await screen.findByText('The rule is paused. Activate it to continue.')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Apply to existing emails' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(await screen.findByRole('button', { name: 'Edit rule' }))
  expect(screen.getByText('Choose a folder or label to save the rule.')).toBeTruthy()
  expect(screen.queryByText('Actions:')).toBeNull()
})

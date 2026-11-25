// i18n-exempt: synthetic API fixtures and test labels.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { client as api } from '../api/generated/client.gen'
import type { SkillOut } from '../api/generated/types.gen'
import { de } from '../i18n/de'
import { en } from '../i18n/en'
import { I18nProvider } from '../i18n/I18n'
import { readFileSync } from 'node:fs'
import { SkillSettings } from './SkillSettings'

const original = globalThis.fetch, config = api.getConfig()
const skill = (over: Partial<SkillOut>): SkillOut => ({ id: 'x', slug: 'x', origin: 'custom', status: 'active', name: 'X', description: 'Synthetic.', version: 1, revision_id: 'r', instructions: '', workflow_steps: [], allowed_tools: [], content_hash: 'h', updated_at: null, ...over })
const builtin = skill({ id: 'daily', slug: 'daily_briefing', origin: 'builtin', name: 'Server name', description: 'Server description.' })
const custom = skill({ id: 'invoices', slug: 'offene-rechnungen', name: 'Offene Rechnungen', description: 'Sammelt offene Rechnungen.', version: 2 })
type Request = { method: string; path: string; search: string; body: unknown }
let requests: Request[]

function setup({ skills = [custom, builtin], respond, locale = 'de' }: { skills?: SkillOut[]; respond?: (request: Request) => Response | Promise<Response> | undefined; locale?: 'de' | 'en' } = {}) {
  requests = []
  let rows = skills.map(row => ({ ...row }))
  api.setConfig({ baseUrl: 'https://api.example.test' })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init), url = new URL(req.url)
    const request = { method: req.method, path: url.pathname, search: url.search, body: req.method === 'PATCH' ? await req.clone().json() : undefined }
    requests.push(request)
    const custom = await respond?.(request)
    if (custom) return custom
    const id = url.pathname.split('/').pop()
    if (req.method === 'PATCH') { rows = rows.map(row => row.id === id ? { ...row, ...(request.body as object), version: row.version + 1 } : row); return Response.json(rows.find(row => row.id === id)) }
    if (req.method === 'DELETE') { rows = rows.filter(row => row.id !== id); return new Response(null, { status: 204 }) }
    return Response.json(rows)
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<I18nProvider locale={locale}><QueryClientProvider client={client}><SkillSettings /></QueryClientProvider></I18nProvider>)
  return { client, replace: (next: SkillOut[]) => { rows = next } }
}
const lists = () => requests.filter(request => request.method === 'GET' && request.path.endsWith('/skills')).length
const refused = (status: number, code: string) => Response.json({ detail: { code, message: 'test-only' } }, { status })
afterEach(() => { cleanup(); globalThis.fetch = original; api.setConfig(config) })

test('lists built-in skills first, then custom ones, with command, description, version, origin and status', async () => {
  setup()
  const list = await screen.findByRole('list', { name: de.skillSettings.title })
  const rows = within(list).getAllByRole('listitem')
  expect(rows.map(row => row.querySelector('strong')?.textContent)).toEqual([de.skills.builtin.daily_briefing.name, 'Offene Rechnungen'])
  expect(rows[0]!.textContent).toContain('/daily_briefing')
  expect(rows[0]!.textContent).toContain(de.skills.builtin.daily_briefing.description)
  expect(rows[0]!.textContent).toContain(`${de.skillSettings.version(1)} · ${de.skillSettings.origins.builtin} · ${de.skillSettings.statuses.active}`)
  expect(rows[1]!.textContent).toContain('/offene-rechnungen')
  expect(rows[1]!.textContent).toContain(`${de.skillSettings.version(2)} · ${de.skillSettings.origins.custom} · ${de.skillSettings.statuses.active}`)
  expect(within(rows[1]!).getByRole('button', { name: de.skillSettings.removeName('Offene Rechnungen') })).toBeTruthy()
  // Negative case: a built-in skill offers only the switch, never a delete action.
  expect([...rows[0]!.querySelectorAll('button')].map(button => button.getAttribute('role'))).toEqual(['switch'])
  expect(screen.queryByRole('button', { name: de.skillSettings.removeName(de.skills.builtin.daily_briefing.name) })).toBeNull()
})

test('loading, empty and error states each have their own text', async () => {
  let release = () => {}
  setup({ respond: request => request.method === 'GET' ? new Promise(resolve => { release = () => resolve(Response.json([])) }) : undefined })
  expect((await screen.findByRole('status')).textContent).toBe(de.skillSettings.loading)
  await act(async () => { release() })
  expect(await screen.findByText(de.skillSettings.empty)).toBeTruthy()
  cleanup()
  let fail = true
  setup({ respond: request => request.method === 'GET' && fail ? refused(503, 'skills_unavailable') : undefined })
  expect((await screen.findByRole('alert')).textContent).toContain(de.skillSettings.error)
  fail = false
  fireEvent.click(screen.getByRole('button', { name: de.chat.retry }))
  expect(await screen.findByRole('list', { name: de.skillSettings.title })).toBeTruthy()
})

test('the switch disables a skill with its listed version and refreshes the shared skills query', async () => {
  setup()
  const toggle = await screen.findByRole('switch', { name: de.skillSettings.useName('Offene Rechnungen') })
  expect(toggle.getAttribute('aria-checked')).toBe('true')
  const before = lists()
  fireEvent.click(toggle)
  await waitFor(() => expect(screen.getByRole('switch', { name: de.skillSettings.useName('Offene Rechnungen') }).getAttribute('aria-checked')).toBe('false'))
  expect(requests.find(request => request.method === 'PATCH')).toEqual({ method: 'PATCH', path: '/api/v1/skills/invoices', search: '', body: { expected_version: 2, status: 'disabled' } })
  expect(screen.getByText(de.skillSettings.disabled('Offene Rechnungen'))).toBeTruthy()
  // Favorites and the / menu read the same ['skills'] query, so they refresh with it.
  expect(lists()).toBeGreaterThan(before)
  // A built-in skill can be switched too.
  fireEvent.click(screen.getByRole('switch', { name: de.skillSettings.useName(de.skills.builtin.daily_briefing.name) }))
  await waitFor(() => expect(requests.filter(request => request.method === 'PATCH').at(-1)).toMatchObject({ path: '/api/v1/skills/daily', body: { expected_version: 1, status: 'disabled' } }))
})

test('a quick second toggle sends the version the first one returned, before the list reloads', async () => {
  let version = custom.version, initial = true
  setup({ respond: request => {
    // The list reload after the first toggle never arrives: only the returned row can carry the new version.
    if (request.method === 'GET') { if (initial) { initial = false; return undefined } return new Promise<Response>(() => {}) }
    if (request.method !== 'PATCH') return undefined
    const body = request.body as { expected_version: number; status: SkillOut['status'] }
    // Like the API: a stale version is refused.
    if (body.expected_version !== version) return refused(409, 'version_conflict')
    version += 1
    return Response.json({ ...custom, status: body.status, version })
  } })
  const toggle = () => screen.getByRole('switch', { name: de.skillSettings.useName('Offene Rechnungen') })
  fireEvent.click(await screen.findByRole('switch', { name: de.skillSettings.useName('Offene Rechnungen') }))
  await waitFor(() => expect(toggle().getAttribute('aria-checked')).toBe('false'))
  await waitFor(() => expect((toggle() as HTMLButtonElement).disabled).toBe(false))
  expect(screen.getByText(new RegExp(de.skillSettings.version(3)))).toBeTruthy()
  fireEvent.click(toggle())
  await waitFor(() => expect(toggle().getAttribute('aria-checked')).toBe('true'))
  expect(requests.filter(request => request.method === 'PATCH').map(request => request.body)).toEqual([{ expected_version: 2, status: 'disabled' }, { expected_version: 3, status: 'active' }])
  expect(screen.getByText(de.skillSettings.enabled('Offene Rechnungen'))).toBeTruthy()
  // Negative case: no spurious conflict.
  expect(screen.queryByText(de.skillSettings.conflict('Offene Rechnungen'))).toBeNull()
})

test('a stale version shows the current state and does not overwrite it', async () => {
  const { replace } = setup({ respond: request => request.method === 'PATCH' ? refused(409, 'version_conflict') : undefined })
  const toggle = await screen.findByRole('switch', { name: de.skillSettings.useName('Offene Rechnungen') })
  // Meanwhile another tab disabled it.
  replace([builtin, { ...custom, status: 'disabled', version: 3 }])
  fireEvent.click(toggle)
  expect((await screen.findByRole('alert')).textContent).toContain(de.skillSettings.conflict('Offene Rechnungen'))
  await waitFor(() => expect(screen.getByRole('switch', { name: de.skillSettings.useName('Offene Rechnungen') }).getAttribute('aria-checked')).toBe('false'))
  expect(screen.getByText(new RegExp(de.skillSettings.version(3)))).toBeTruthy()
  // Exactly the one refused request: nothing is retried with the newer version.
  expect(requests.filter(request => request.method === 'PATCH')).toHaveLength(1)
})

test('deleting asks in a dialog that names the consequences and manages the focus', async () => {
  setup()
  const remove = await screen.findByRole('button', { name: de.skillSettings.removeName('Offene Rechnungen') })
  act(() => remove.focus())
  fireEvent.click(remove)
  const dialog = await screen.findByRole('dialog', { name: de.skillSettings.removeTitle })
  expect(dialog.textContent).toContain(de.skillSettings.removeDescription)
  expect(dialog.textContent).toContain('/offene-rechnungen')
  expect(dialog.contains(document.activeElement)).toBe(true)
  // Escape keeps the skill and returns to its Delete button.
  fireEvent.keyDown(document, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(remove)
  expect(requests.some(request => request.method === 'DELETE')).toBe(false)

  fireEvent.click(remove)
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: de.skillSettings.remove }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(requests.find(request => request.method === 'DELETE')).toMatchObject({ path: '/api/v1/skills/invoices', search: '?expected_version=2' })
  await waitFor(() => expect(screen.queryByText('Offene Rechnungen')).toBeNull())
  // The row and its button are gone: the focus lands on the result instead of the page.
  await waitFor(() => expect(document.activeElement?.textContent).toBe(de.skillSettings.deleted('Offene Rechnungen')))
})

test('a stale delete keeps the skill, shows its current state and returns to its button', async () => {
  const { replace } = setup({ respond: request => request.method === 'DELETE' ? refused(409, 'version_conflict') : undefined })
  const remove = await screen.findByRole('button', { name: de.skillSettings.removeName('Offene Rechnungen') })
  replace([builtin, { ...custom, version: 4 }])
  act(() => remove.focus())
  fireEvent.click(remove)
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: de.skillSettings.remove }))
  expect((await screen.findByRole('alert')).textContent).toContain(de.skillSettings.conflict('Offene Rechnungen'))
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect(screen.getByText(new RegExp(de.skillSettings.version(4)))).toBeTruthy())
  expect(screen.getByText('Offene Rechnungen')).toBeTruthy()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: de.skillSettings.removeName('Offene Rechnungen') }))
})

test('a refused built-in change says so instead of a generic error', async () => {
  setup({ respond: request => request.method === 'PATCH' ? refused(403, 'skill_builtin_protected') : undefined })
  fireEvent.click(await screen.findByRole('switch', { name: de.skillSettings.useName(de.skills.builtin.daily_briefing.name) }))
  expect((await screen.findByRole('alert')).textContent).toContain(de.skillSettings.protected)
})

test('English names, states and the delete dialog', async () => {
  setup({ locale: 'en' })
  const list = await screen.findByRole('list', { name: en.skillSettings.title })
  expect(list.textContent).toContain(en.skills.builtin.daily_briefing.name)
  expect(list.textContent).toContain(`${en.skillSettings.version(2)} · ${en.skillSettings.origins.custom} · ${en.skillSettings.statuses.active}`)
  fireEvent.click(screen.getByRole('button', { name: en.skillSettings.removeName('Offene Rechnungen') }))
  expect((await screen.findByRole('dialog', { name: en.skillSettings.removeTitle })).textContent).toContain(en.skillSettings.removeDescription)
})

test('rows wrap on narrow screens and keep 44 px actions on touch screens', () => {
  const css = readFileSync(new URL('./settings.css', import.meta.url), 'utf8')
  expect(css).toMatch(/\.le-skill-row \{[^}]*flex-wrap: wrap/u)
  expect(css).toMatch(/\.le-skill-row-info \{[^}]*min-width: 0/u)
  expect(css).toMatch(/@media \(pointer: coarse\) \{ \.le-skill-row-actions \.le-btn \{ min-height: var\(--control-height-lg\); \} \}/u)
})

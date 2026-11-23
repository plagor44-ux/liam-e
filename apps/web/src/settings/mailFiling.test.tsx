// i18n-exempt: synthetic API responses and test labels.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { client as api } from '../api/generated/client.gen'
import { FolderSettings } from './MailFiling'

const original = globalThis.fetch, config = api.getConfig()
let requests: { method: string; url: string; body?: unknown }[] = []
afterEach(() => { cleanup(); globalThis.fetch = original; api.setConfig(config) })

test('folder deletion asks for confirmation and sends it explicitly', async () => {
  requests = []
  api.setConfig({ baseUrl: window.location.origin })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    requests.push({ method: req.method, url: req.url })
    if (req.method === 'DELETE') return Response.json({ ok: true })
    return Response.json([{ id: 'folder', name: 'Projects', kind: 'folder', created_at: '2026-01-01T00:00:00Z' }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FolderSettings /></QueryClientProvider>)
  await screen.findByText('Projects')
  fireEvent.click(screen.getByRole('button', { name: 'Projects löschen' }))
  expect(requests.filter(request => request.method === 'DELETE')).toHaveLength(0)
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Löschen' }))
  await waitFor(() => expect(requests.some(request => request.method === 'DELETE' && request.url.includes('confirm_delete=true'))).toBe(true))
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
    if (path.endsWith('/folders')) return Response.json([])
    if (path.endsWith('/accounts')) return Response.json([])
    return Response.json([{ id: 'rule', name: 'Projects', version: 1, sender_domain: 'example.test', is_active: true }])
  }) as typeof fetch
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RuleSettings /></QueryClientProvider>)
  await screen.findByText('Projects')
  fireEvent.click(screen.getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  expect(requests.some(request => request.url.includes('/apply'))).toBe(false)
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Auf vorhandene Mails anwenden' }))
  await waitFor(() => expect(requests.some(request => request.url.includes('/apply') && (request.body as { confirm_apply: boolean }).confirm_apply)).toBe(true))
})

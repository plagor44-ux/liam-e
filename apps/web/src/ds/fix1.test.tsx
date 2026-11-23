import { afterEach, expect, mock, test } from 'bun:test'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { MessageListItemOut } from '../api/generated/types.gen'
import { MailRow } from './MailRow'
import { MailReference } from './MailReference'
import { QuickAction } from './QuickAction'
import { providerLabel } from './providerLabel'
import { formatTime } from '../i18n/format'

afterEach(cleanup)
const mail: MessageListItemOut = { id: '1', account_id: '1', color_index: 2, deadline: '2026-11-21', has_attachments: false, internal_date: null, is_flagged: false, is_read: false, needs_reply: false, sender_address: 'a@example.org', sender_name: 'Anna', snippet: '', subject: 'Betreff' }
const actions = { onOpen: () => {}, onArchive: () => {}, onToggleRead: () => {}, onTrash: () => {} }

test('mail row formats deadline and displays account tag', () => {
  render(<MailRow mail={mail} selected={false} coarse accountLabel="Privat" {...actions} />)
  expect(screen.getByText('Frist Sa., 21.11.')).toBeTruthy()
  expect(screen.getByText('Privat').className).toContain('le-tag--on-surface')
  expect(screen.getByText('Privat').querySelector('.le-dot--a3')).toBeTruthy()
})

test('provider labels cover known providers and IMAP domains', () => {
  for (const [provider, email_address, expected] of [['google_mail', 'a@example.org', 'Gmail'], ['outlook', 'a@example.org', 'Outlook'], ['imap', 'a@me.com', 'iCloud'], ['imap', 'a@ymail.com', 'Yahoo'], ['imap', 'a@example.org', 'example.org'], ['imap', 'address', 'address'], ['other', 'a@example.org', 'other']] as const) {
    expect(providerLabel({ provider, email_address })).toBe(expected)
  }
})

test('mobile row places the account and deadline beneath the snippet', async () => {
  const css = await Bun.file(new URL('../styles/ds.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-mailrow-snippet-slot \{ grid-row: 3; height: auto; \}/u)
  expect(css).toMatch(/\.le-mailrow-top \.le-tag \{ grid-row: 4; grid-column: 1; \}/u)
  expect(css).toMatch(/\.le-mailrow-deadline \{ grid-row: 4; grid-column: 2; \}/u)
})

test('mail reference formats deadline and displays account tag', () => {
  render(<MailReference mail={mail} accountLabel="Privat" onOpen={() => {}} />)
  expect(screen.getByText('Frist Sa., 21.11.')).toBeTruthy()
  expect(screen.getByText('Privat').className).toContain('le-tag--on-surface')
  expect(screen.getByText('Privat').querySelector('.le-dot--a3')).toBeTruthy()
})

test('mail reference shows the urgent dot, the time for today and a pencil that asks for a draft', async () => {
  const now = new Date()
  const onDraft = mock(() => {})
  render(<MailReference mail={{ ...mail, is_flagged: true, internal_date: now.toISOString() }} onOpen={() => {}} onDraft={onDraft} />)
  const dot = screen.getByRole('img', { name: 'Dringend' })
  expect(dot.classList.contains('le-mailref-dot')).toBe(true)
  fireEvent.mouseEnter(dot)
  expect(await screen.findByRole('tooltip')).toHaveProperty('textContent', 'Dringend')
  expect(document.querySelector('.le-mailref-dot.le-dot--warm')?.classList.contains('le-mailref-dot--none')).toBe(false)
  expect(screen.getByText(formatTime(now)).tagName).toBe('TIME')
  const pencil = screen.getByRole('button', { name: 'Antwort entwerfen' })
  expect(pencil.querySelector('.lucide-pencil')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Mail öffnen' }).compareDocumentPosition(pencil) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  fireEvent.click(pencil)
  expect(onDraft).toHaveBeenCalledTimes(1)
})

test('mail reference keeps the dot column for plain mails, says Gestern for yesterday and has no pencil without a handler', () => {
  const yesterday = new Date()
  yesterday.setDate(yesterday.getDate() - 1)
  yesterday.setHours(12, 0, 0, 0)
  const { container } = render(<MailReference mail={{ ...mail, is_flagged: false, internal_date: yesterday.toISOString() }} onOpen={() => {}} />)
  expect(screen.queryByRole('img', { name: 'Dringend' })).toBeNull()
  expect(container.querySelector('.le-mailref-dot')?.classList.contains('le-mailref-dot--none')).toBe(true)
  expect(container.firstElementChild?.firstElementChild?.classList.contains('le-mailref-dot')).toBe(true)
  expect(screen.getByText('Gestern').tagName).toBe('TIME')
  expect(screen.queryByRole('button', { name: 'Antwort entwerfen' })).toBeNull()
  cleanup()
  render(<MailReference mail={{ ...mail, is_flagged: true, internal_date: null }} onOpen={() => {}} onDraft={() => {}} draftDisabled />)
  expect((screen.getByRole('button', { name: 'Antwort entwerfen' }) as HTMLButtonElement).disabled).toBe(true)
})

test('mail reference shows one muted line of the opening text below the subject, and none without text', async () => {
  const { container } = render(<MailReference mail={{ ...mail, snippet: 'Einwände bis Fr., 20.11. möglich' }} onOpen={() => {}} />)
  const line = screen.getByText('Einwände bis Fr., 20.11. möglich')
  expect(line.classList.contains('le-mailref-snippet')).toBe(true)
  expect(line.classList.contains('le-muted')).toBe(true)
  expect(screen.getByText('Betreff').compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const css = await Bun.file(new URL('../styles/ds.css', import.meta.url)).text()
  for (const property of ['white-space: nowrap', 'overflow: hidden', 'text-overflow: ellipsis']) expect(css.split('.le-mailref-snippet {')[1]?.split('}')[0]).toContain(property)
  cleanup()
  render(<MailReference mail={{ ...mail, snippet: '' }} onOpen={() => {}} />)
  expect(container.querySelector('.le-mailref-snippet')).toBeNull()
  expect(document.querySelector('.le-mailref-snippet')).toBeNull()
})

test('the mail reference dot keeps its place and aligns with the first line', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-mailref-dot \{ margin-top: 8px; \}/u)
  expect(css).toMatch(/\.le-mailref-dot--none \{ visibility: hidden; \}/u)
})

test('mail row shows time for today', () => {
  const now = new Date()
  render(<MailRow mail={{ ...mail, internal_date: now.toISOString() }} selected={false} coarse {...actions} />)
  expect(screen.getByText(formatTime(now))).toBeTruthy()
})

test('quick action shows its tip on focus', () => {
  render(<QuickAction icon="inbox" title="Posteingang" sub="Mails" tip="Zeigt Mails" onClick={() => {}} />)
  fireEvent.keyDown(document.body, { key: 'Tab' })
  act(() => screen.getByRole('button').focus())
  expect(screen.getByRole('tooltip').textContent).toBe('Zeigt Mails')
})

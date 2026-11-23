// i18n-exempt: synthetic data.
import { afterEach, expect, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { FilingMenu } from './FilingMenu'
import { FilingTags } from './FilingTags'
import { shortcutHint } from './inboxShortcuts'
import type { MessageListItemOut } from '../api/generated/types.gen'
import { I18nProvider } from '../i18n/I18n'

afterEach(() => { cleanup() })

test('filing a selection sends both mail IDs without a provider capability check', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'f', name: 'Projects', kind: 'folder' }] } as never)
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FilingMenu ids={['a', 'b']} /></QueryClientProvider>)
  const trigger = await screen.findByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.click(trigger)
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Ordner: Projects' }))
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['a', 'b'], folder_id: 'f' }))
  folders.mockRestore(); assign.mockRestore()
})

test('filing tags show the assigned folder and labels', () => {
  render(<FilingTags folderId="f" labelIds={['l']} destinations={[
    { id: 'f', name: 'Projects', kind: 'folder' }, { id: 'l', name: 'Work', kind: 'label' },
  ]} />)
  expect(screen.getByText('Ordner: Projects')).toBeTruthy()
  expect(screen.getByText('Label: Work')).toBeTruthy()
})

test('same-named folder and label remain identifiable and deleted assignments stay hidden', () => {
  render(<FilingTags folderId="f" labelIds={['l', 'deleted']} destinations={[
    { id: 'f', name: 'Projects', kind: 'folder' }, { id: 'l', name: 'Projects', kind: 'label' },
  ]} />)
  expect(screen.getByText('Ordner: Projects').parentElement?.querySelector('.lucide-folder')).toBeTruthy()
  expect(screen.getByText('Label: Projects').parentElement?.querySelector('.lucide-tag')).toBeTruthy()
  expect(screen.queryByText(/deleted/u)).toBeNull()
})

test('assigned destinations retain type text and show their individual palette colours', () => {
  render(<FilingTags folderId="f" labelIds={['l', 'n']} destinations={[
    { id: 'f', name: 'Projects', kind: 'folder', color: 'moss' },
    { id: 'l', name: 'Important', kind: 'label', color: 'teal' },
    { id: 'n', name: 'Neutral', kind: 'label', color: 'none' },
  ]} />)
  expect(screen.getByText('Ordner: Projects').parentElement?.classList.contains('le-destination-moss')).toBe(true)
  expect(screen.getByText('Label: Important').parentElement?.classList.contains('le-destination-teal')).toBe(true)
  expect(screen.getByText('Label: Neutral').parentElement?.className).not.toContain('le-destination-')
})

test('filing choices identify same-named destinations by kind', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [
    { id: 'f', name: 'Projects', kind: 'folder' }, { id: 'l', name: 'Projects', kind: 'label' },
  ] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FilingMenu ids={['a']} /></QueryClientProvider>)
  const trigger = await screen.findByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.click(trigger)
  expect(await screen.findByRole('menuitem', { name: 'Ordner: Projects' })).toBeTruthy()
  expect(screen.getByRole('menuitem', { name: 'Label: Projects' })).toBeTruthy()
  folders.mockRestore()
})

test.each(['de', 'en'] as const)('filing choices and tags share the active locale order (%s)', async locale => {
  const data = [
    { id: 'z', name: 'Zebra', kind: 'folder' }, { id: 'a', name: 'Äpfel', kind: 'folder' },
    { id: 'l2', name: 'zeta', kind: 'label' }, { id: 'l1', name: 'Alpha', kind: 'label' },
  ] as const
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<I18nProvider locale={locale}><QueryClientProvider client={client}><FilingMenu ids={['a']} /><FilingTags folderId="a" labelIds={['l2', 'l1']} destinations={[...data]} /></QueryClientProvider></I18nProvider>)
  const trigger = await screen.findByRole('button', { name: locale === 'de' ? 'Zuordnen' : 'File' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.click(trigger)
  const choices = screen.getAllByRole('menuitem').map(row => row.textContent)
  expect(choices.slice(1)).toEqual(locale === 'de' ? ['Ordner: Äpfel', 'Ordner: Zebra', 'Label: Alpha', 'Label: zeta'] : ['Folder: Äpfel', 'Folder: Zebra', 'Label: Alpha', 'Label: zeta'])
  expect(Array.from(document.querySelectorAll('.le-filing-tags .le-tag')).map(row => row.textContent)).toEqual(locale === 'de' ? ['Ordner: Äpfel', 'Label: Alpha', 'Label: zeta'] : ['Folder: Äpfel', 'Label: Alpha', 'Label: zeta'])
  folders.mockRestore()
})

test('filing menu marks common assignments and names a mixed label selection', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [
    { id: 'f', name: 'Projects', kind: 'folder' }, { id: 'l', name: 'Work', kind: 'label' },
  ] } as never)
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  const messages = [{ id: 'a', app_folder_id: 'f', app_label_ids: ['l'] }, { id: 'b', app_folder_id: 'f', app_label_ids: [] }] as MessageListItemOut[]
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FilingMenu ids={['a', 'b']} messages={messages} /></QueryClientProvider>)
  const trigger = await screen.findByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.click(trigger)
  expect((await screen.findByRole('menuitemradio', { name: 'Ordner: Projects' })).getAttribute('aria-checked')).toBe('true')
  const label = screen.getByRole('menuitemcheckbox', { name: 'Label: Work (teilweise zugeordnet)' })
  expect(label.getAttribute('aria-checked')).toBe('mixed')
  fireEvent.click(label)
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['a', 'b'], add_labels: ['l'] }))
  folders.mockRestore(); assign.mockRestore()
})

test('filing menu removes an already assigned label', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'l', name: 'Work', kind: 'label' }] } as never)
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const message = { id: 'a', app_folder_id: null, app_label_ids: ['l'] } as MessageListItemOut
  render(<QueryClientProvider client={client}><FilingMenu ids={['a']} messages={[message]} /></QueryClientProvider>)
  const trigger = await screen.findByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.click(trigger)
  const label = await screen.findByRole('menuitemcheckbox', { name: 'Label: Work' })
  expect(label.getAttribute('aria-checked')).toBe('true')
  expect(label.querySelector('.lucide-tag')).toBeTruthy()
  expect(label.querySelector('.lucide-check')).toBeTruthy()
  fireEvent.click(label)
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['a'], remove_labels: ['l'] }))
  folders.mockRestore(); assign.mockRestore()
})

test('add-label shortcut never removes a label already assigned', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'l', name: 'Work', kind: 'label' }] } as never)
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const message = { id: 'a', app_label_ids: ['l'] } as MessageListItemOut
  const { rerender } = render(<QueryClientProvider client={client}><FilingMenu ids={['a']} messages={[message]} /></QueryClientProvider>)
  await waitFor(() => expect((screen.getByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement).disabled).toBe(false))
  rerender(<QueryClientProvider client={client}><FilingMenu ids={['a']} messages={[message]} request={{ kind: 'label', serial: 1 }} /></QueryClientProvider>)
  const item = await screen.findByRole('menuitemcheckbox', { name: /Label: Work/ })
  expect(item.getAttribute('aria-disabled')).toBe('true')
  expect(item.textContent).toContain('Bereits gesetzt')
  fireEvent.click(item)
  expect(assign).not.toHaveBeenCalled()
  folders.mockRestore(); assign.mockRestore()
})


test('filing menu header exposes registry shortcut hints without changing destination names', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'f', name: 'Projects', kind: 'folder' }, { id: 'l', name: 'Work', kind: 'label' }] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FilingMenu ids={['a']} showShortcuts /></QueryClientProvider>)
  const trigger = await screen.findByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.click(trigger)
  expect(await screen.findByRole('menuitem', { name: 'Ordner: Projects' })).toBeTruthy()
  expect(screen.getByRole('menuitem', { name: 'Label: Work' })).toBeTruthy()
  const header = document.querySelector('.le-filing-shortcuts')!
  expect(header.textContent).toContain(`Ordner zuordnen ${shortcutHint('folder', 'de')}`)
  expect(header.textContent).toContain(`Label zuordnen ${shortcutHint('label', 'de')}`)
  folders.mockRestore()
})


test('label shortcut focuses and roves only through available labels', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [
    { id: 'a', name: 'Alpha', kind: 'label' }, { id: 'b', name: 'Beta', kind: 'label' }, { id: 'z', name: 'Zeta', kind: 'label' },
  ] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const message = { id: 'mail', app_label_ids: ['a'] } as MessageListItemOut
  const { rerender } = render(<QueryClientProvider client={client}><FilingMenu ids={['mail']} messages={[message]} /></QueryClientProvider>)
  await waitFor(() => expect((screen.getByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement).disabled).toBe(false))
  rerender(<QueryClientProvider client={client}><FilingMenu ids={['mail']} messages={[message]} request={{ kind: 'label', serial: 1 }} /></QueryClientProvider>)
  const disabled = await screen.findByRole('menuitemcheckbox', { name: /Label: Alpha/ })
  const beta = screen.getByRole('menuitemcheckbox', { name: 'Label: Beta' })
  const zeta = screen.getByRole('menuitemcheckbox', { name: 'Label: Zeta' })
  expect(disabled.textContent).toContain('Bereits gesetzt')
  await waitFor(() => expect(document.activeElement).toBe(beta))
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' })
  expect(document.activeElement).toBe(zeta)
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' })
  expect(document.activeElement).toBe(beta)
  folders.mockRestore()
})

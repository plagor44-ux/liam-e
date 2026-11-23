// i18n-exempt: synthetic data.
import { afterEach, expect, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { FilingMenu } from './FilingMenu'
import { FilingTags } from './FilingTags'
import type { MessageListItemOut } from '../api/generated/types.gen'

afterEach(() => { cleanup() })

test('filing a selection sends both mail IDs without a provider capability check', async () => {
  const folders = spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'f', name: 'Projects', kind: 'folder' }] } as never)
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FilingMenu ids={['a', 'b']} /></QueryClientProvider>)
  const trigger = await screen.findByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.click(trigger)
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Projects' }))
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
  expect((await screen.findByRole('menuitemradio', { name: 'Projects' })).getAttribute('aria-checked')).toBe('true')
  const label = screen.getByRole('menuitemcheckbox', { name: 'Work (teilweise zugeordnet)' })
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
  const label = await screen.findByRole('menuitemcheckbox', { name: 'Work' })
  expect(label.getAttribute('aria-checked')).toBe('true')
  fireEvent.click(label)
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['a'], remove_labels: ['l'] }))
  folders.mockRestore(); assign.mockRestore()
})

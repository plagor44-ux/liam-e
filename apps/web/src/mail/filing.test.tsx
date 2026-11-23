// i18n-exempt: synthetic data.
import { afterEach, expect, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { FilingMenu } from './FilingMenu'
import { FilingTags } from './FilingTags'

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

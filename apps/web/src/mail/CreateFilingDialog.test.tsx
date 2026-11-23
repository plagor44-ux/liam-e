// i18n-exempt: synthetic names and IDs.
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { CreateFilingDialog } from './CreateFilingDialog'

afterEach(() => { cleanup(); mock.restore() })

test.each(['folder', 'label'] as const)('creating a %s files only after explicit save', async kind => {
  const create = spyOn(sdk, 'mailFolderCreateAndAssign').mockResolvedValue({ data: { id: 'new', name: 'Projects', kind } } as never)
  const close = mock(() => {})
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><CreateFilingDialog messageId="mail" kind={kind} onClose={close} onSuccess={() => {}} /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Projects' } })
  expect(create).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  await waitFor(() => expect(create.mock.calls[0]?.[0]?.body).toEqual({ message_id: 'mail', name: 'Projects', kind }))
  expect(close).toHaveBeenCalledTimes(1)
})

test('cancelling the dialog writes nothing', () => {
  const create = spyOn(sdk, 'mailFolderCreateAndAssign')
  const close = mock(() => {})
  const client = new QueryClient()
  render(<QueryClientProvider client={client}><CreateFilingDialog messageId="mail" kind="folder" onClose={close} onSuccess={() => {}} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'Abbrechen' }))
  expect(close).toHaveBeenCalledTimes(1)
  expect(create).not.toHaveBeenCalled()
})

test('repeated save while a create is pending sends only one operation', async () => {
  const create = spyOn(sdk, 'mailFolderCreateAndAssign').mockImplementation((() => new Promise(() => {})) as never)
  const client = new QueryClient()
  render(<QueryClientProvider client={client}><CreateFilingDialog messageId="mail" kind="folder" onClose={() => {}} onSuccess={() => {}} /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Projects' } })
  const form = screen.getByLabelText('Name').closest('form')!
  fireEvent.submit(form); fireEvent.submit(form)
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
})

test.each([
  ['folder_exists', 'Diesen Namen gibt es bereits. Prüfe den vorhandenen Eintrag, bevor du es erneut versuchst.'],
  ['message_not_found', 'Diese Mail ist nicht mehr verfügbar. Es wurde nichts gespeichert.'],
])('a %s refusal remains visible and is not retried automatically', async (code, text) => {
  const create = spyOn(sdk, 'mailFolderCreateAndAssign').mockRejectedValue({ detail: { code } })
  const close = mock(() => {})
  const client = new QueryClient()
  render(<QueryClientProvider client={client}><CreateFilingDialog messageId="mail" kind="folder" onClose={close} onSuccess={() => {}} /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Projects' } })
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  expect(await screen.findByText(text)).toBeTruthy()
  expect(create).toHaveBeenCalledTimes(1)
  expect(close).not.toHaveBeenCalled()
})

test('an uncertain response asks the owner to check before retrying', async () => {
  const create = spyOn(sdk, 'mailFolderCreateAndAssign').mockRejectedValue(new Error('network unavailable'))
  const client = new QueryClient()
  render(<QueryClientProvider client={client}><CreateFilingDialog messageId="mail" kind="folder" onClose={() => {}} onSuccess={() => {}} /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Projects' } })
  fireEvent.click(screen.getByRole('button', { name: 'Speichern' }))
  expect(await screen.findByText('Die Antwort fehlt. Prüfe den Eintrag und die Mail, bevor du es erneut versuchst.')).toBeTruthy()
  expect(create).toHaveBeenCalledTimes(1)
})

import { afterEach, expect, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { AppLocale } from '../i18n/AppLocale'
import { MailHtml } from './MailHtml'
import { setTheme } from '../shell/theme'

afterEach(() => { cleanup(); setTheme('system') })
function mount(id: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><AppLocale initial={{ page: 'de', browser: 'de' }}><MailHtml key={id} id={id} text="Synthetischer Text" localOnly={false} /></AppLocale></QueryClientProvider>)
}

test('uses text when HTML is absent', async () => {
  spyOn(sdk, 'mailMessageHtmlGet').mockResolvedValue({ data: { html: null } } as never)
  mount('first')
  expect(await screen.findByText('Synthetischer Text')).toBeTruthy()
  expect(screen.queryByTitle('Mailinhalt')).toBeNull()
})

test('isolates HTML and grants images only for this view', async () => {
  spyOn(sdk, 'mailMessageHtmlGet').mockResolvedValue({ data: { html: '<img src="https://example.test/pixel"><p>Hallo</p>' } } as never)
  const view = mount('first')
  const frame = await screen.findByTitle('Mailinhalt')
  expect(frame.getAttribute('sandbox')).not.toContain('allow-scripts')
  expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin')
  expect(frame.getAttribute('srcdoc')).not.toContain('example.test')
  fireEvent.click(screen.getByRole('button', { name: 'Bilder anzeigen' }))
  await waitFor(() => expect(frame.getAttribute('srcdoc')).toContain('example.test'))
  view.unmount()
  mount('second')
  const next = await screen.findByTitle('Mailinhalt')
  expect(next.getAttribute('srcdoc')).not.toContain('example.test')
})

test('uses manually selected dark mode and resets the light-original choice per mail', async () => {
  setTheme('dark')
  spyOn(sdk, 'mailMessageHtmlGet').mockResolvedValue({ data: { html: '<p>Test</p>' } } as never)
  const view = mount('first')
  const frame = await screen.findByTitle('Mailinhalt')
  expect(frame.getAttribute('srcdoc')).toContain('color-scheme:dark')
  fireEvent.click(screen.getByRole('button', { name: 'Original hell anzeigen' }))
  expect(frame.getAttribute('srcdoc')).toContain('color-scheme:light')
  view.unmount()
  mount('second')
  expect((await screen.findByTitle('Mailinhalt')).getAttribute('srcdoc')).toContain('color-scheme:dark')
  act(() => setTheme('light'))
  await waitFor(() => expect(screen.getByTitle('Mailinhalt').getAttribute('srcdoc')).toContain('color-scheme:light'))
  expect(screen.queryByRole('button', { name: 'Original hell anzeigen' })).toBeNull()
})

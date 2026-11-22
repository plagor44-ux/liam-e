import { afterEach, expect, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { UiSettingsOut } from '../api/generated/types.gen'
import { AppLocale } from '../i18n/AppLocale'
import { useI18n } from '../i18n/I18n'
import { LOCALE_COOKIE } from '../i18n/locales'
import { LanguageSettings } from './LanguageSettings'

const browserDefaults: UiSettingsOut = {
  locale: 'browser', swipe_left: 'archive', swipe_right: 'toggle_read', revision: 1, updated_at: '2026-01-01T00:00:00Z',
}

function open() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  render(<QueryClientProvider client={client}>
    <AppLocale initial={{ page: 'de', browser: 'de' }}><LanguageSettings /></AppLocale>
  </QueryClientProvider>)
}

afterEach(() => { document.cookie = LOCALE_COOKIE + '=; Max-Age=0; Path=/' })

test('language changes after the owner setting is saved, with the confirmed revision', async () => {
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: browserDefaults })) as never)
  let resolve!: (value: unknown) => void
  const save = spyOn(sdk, 'settingsUiUpdate').mockImplementation((() => new Promise(done => { resolve = done })) as never)
  open()

  fireEvent.click(await screen.findByRole('button', { name: /Sprache des Browsers verwenden/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Englisch' }))
  fireEvent.click(screen.getByRole('button', { name: 'Sprache speichern' }))
  expect(save.mock.calls[0]?.[0]?.body).toEqual({ locale: 'en', expected_revision: 1 })
  expect(screen.queryByRole('heading', { name: 'App language' })).toBeNull()
  await act(async () => { resolve({ data: { ...browserDefaults, locale: 'en', revision: 2 } }) })

  expect(await screen.findByRole('heading', { name: 'App language' })).toBeTruthy()
  expect(screen.getByText('Language saved.')).toBeTruthy()
  await waitFor(() => expect(document.cookie).toContain(LOCALE_COOKIE + '=en'))
})

test('a second device applies the saved language without relying on a cookie', async () => {
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: { ...browserDefaults, locale: 'en', revision: 2 } })) as never)
  open()
  expect(await screen.findByRole('heading', { name: 'App language' })).toBeTruthy()
  expect(screen.getByRole('button', { name: /App language English/ })).toBeTruthy()
})


function LocaleProbe() {
  const { locale, browserLocale } = useI18n()
  return <output>{locale}/{browserLocale}</output>
}

test('a stale English mirror stays separate from the German browser hint until settings arrive', async () => {
  document.cookie = LOCALE_COOKIE + '=en; Path=/'
  let confirm!: (value: unknown) => void
  spyOn(sdk, 'settingsUiGet').mockImplementation((() => new Promise(done => { confirm = done })) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <AppLocale initial={{ page: 'en', browser: 'de' }}><LocaleProbe /></AppLocale>
  </QueryClientProvider>)
  expect(screen.getByText('en/de')).toBeTruthy()

  await act(async () => { confirm({ data: { ...browserDefaults, revision: 3 } }) })
  expect(await screen.findByText('de/de')).toBeTruthy()
  await waitFor(() => expect(document.cookie).not.toContain(LOCALE_COOKIE + '=en'))
})

test('speech speed saves the selected value with the confirmed revision', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: browserDefaults } as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockResolvedValue({ data: { ...browserDefaults, speech_pace: 'fast', revision: 2 } } as never)
  open()
  fireEvent.click(await screen.findByRole('button', { name: /Sprechtempo Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Schnell' }))
  fireEvent.click(screen.getByRole('button', { name: 'Tempo speichern' }))
  await waitFor(() => expect(save.mock.calls[0]?.[0]?.body).toEqual({ speech_pace: 'fast', expected_revision: 1 }))
  expect(await screen.findByText('Tempo gespeichert.')).toBeTruthy()
})

test('a confirmed settings read reports the browser language for background jobs', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: browserDefaults } as never)
  const confirm = spyOn(sdk, 'settingsUiBrowserLocaleConfirm').mockResolvedValue({ data: { confirmed: true } } as never)
  open()
  await waitFor(() => expect(confirm.mock.calls[0]?.[0]?.body).toEqual({ locale: 'de' }))
})

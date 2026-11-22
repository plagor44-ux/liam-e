import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { UiSettingsOut } from '../api/generated/types.gen'
import { AppLocale } from '../i18n/AppLocale'
import { useI18n } from '../i18n/I18n'
import { LOCALE_COOKIE } from '../i18n/locales'
import { LanguageSettings } from './LanguageSettings'

const browserDefaults: UiSettingsOut = {
  locale: 'browser', speech_pace: 'normal', swipe_left: 'archive', swipe_right: 'toggle_read', revision: 1, updated_at: '2026-01-01T00:00:00Z',
}

beforeEach(() => {
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'audioVoicesList').mockResolvedValue({ data: { verified: true, voices: [{ id: 'voice_123', name: 'Example', languages: ['en'] }, { id: 'voice_481', name: 'Heidi', languages: ['de'] }, { id: 'voice_478', name: 'Sheila', languages: ['en'] }] } } as never)
})

function open(client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })) {
  const view = render(<QueryClientProvider client={client}>
    <AppLocale initial={{ page: 'de', browser: 'de' }}><LanguageSettings /></AppLocale>
  </QueryClientProvider>)
  return { client, view }
}

afterEach(() => {
  cleanup()
  document.cookie = LOCALE_COOKIE + '=; Max-Age=0; Path=/'
})

test('language changes after the owner setting is saved via the global save bar, with the confirmed revision', async () => {
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: browserDefaults })) as never)
  let resolve!: (value: unknown) => void
  const save = spyOn(sdk, 'settingsUiUpdate').mockImplementation((() => new Promise(done => { resolve = done })) as never)
  open()

  expect(screen.queryByRole('region', { name: 'Ungespeicherte Änderungen' })).toBeNull()
  fireEvent.click(await screen.findByRole('button', { name: /Sprache des Browsers verwenden/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Englisch' }))
  expect(screen.getByRole('region', { name: 'Ungespeicherte Änderungen' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  expect(save.mock.calls[0]?.[0]?.body).toEqual({ locale: 'en', expected_revision: 1 })
  expect(screen.queryByRole('heading', { name: 'App language' })).toBeNull()
  await act(async () => { resolve({ data: { ...browserDefaults, locale: 'en', revision: 2 } }) })

  expect(await screen.findByRole('heading', { name: 'App language' })).toBeTruthy()
  expect(screen.getByText('Language saved.')).toBeTruthy()
  expect(screen.queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
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

test('speech speed saves via the global bar and persists when returning from voice mode', async () => {
  let stored: UiSettingsOut = { ...browserDefaults, locale: 'en' }
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: stored })) as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockImplementation((async ({ body }: { body: { expected_revision: number; speech_pace?: 'slow' | 'normal' | 'fast'; locale?: 'browser' | 'de' | 'en' } }) => {
    stored = { ...stored, ...body, revision: body.expected_revision + 1 }
    return { data: stored }
  }) as never)
  const { client, view } = open()
  expect(await screen.findByText(/Experimental: Choose the speed/)).toBeTruthy()
  fireEvent.click(await screen.findByRole('button', { name: /Speech speed Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Fast' }))
  expect(screen.getByRole('region', { name: 'Unsaved changes' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Save choices' }))
  await waitFor(() => expect(save.mock.calls[0]?.[0]?.body).toEqual({ speech_pace: 'fast', expected_revision: 1 }))
  expect(await screen.findByText('Speed saved.')).toBeTruthy()
  expect(screen.queryByRole('region', { name: 'Unsaved changes' })).toBeNull()

  // Simulate navigating away to test voice mode and returning to Settings -> Language.
  view.unmount()
  open(client)
  expect(await screen.findByRole('button', { name: /Speech speed Fast/ })).toBeTruthy()
})

test('changing language and speech speed together saves both atomically with one revision and can be discarded', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: browserDefaults } as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockResolvedValue({ data: { ...browserDefaults, locale: 'en', speech_pace: 'fast', revision: 2 } } as never)
  open()

  fireEvent.click(await screen.findByRole('button', { name: /Sprache des Browsers verwenden/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Englisch' }))
  fireEvent.click(screen.getByRole('button', { name: /Sprechtempo Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Schnell' }))
  expect(screen.getByRole('region', { name: 'Ungespeicherte Änderungen' })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: 'Verwerfen' }))
  expect(screen.queryByRole('region', { name: 'Ungespeicherte Änderungen' })).toBeNull()
  expect(screen.getByRole('button', { name: /Sprache des Browsers verwenden/ })).toBeTruthy()
  expect(screen.getByRole('button', { name: /Sprechtempo Normal/ })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: /Sprache des Browsers verwenden/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Englisch' }))
  fireEvent.click(screen.getByRole('button', { name: /Sprechtempo Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Schnell' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(save.mock.calls[0]?.[0]?.body).toEqual({ expected_revision: 1, locale: 'en', speech_pace: 'fast' }))
  expect(await screen.findByText('Language saved.')).toBeTruthy()
  expect(screen.getByText('Speed saved.')).toBeTruthy()
})

test('a confirmed settings read reports the browser language for background jobs', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: browserDefaults } as never)
  const confirm = spyOn(sdk, 'settingsUiBrowserLocaleConfirm').mockResolvedValue({ data: { confirmed: true } } as never)
  open()
  await waitFor(() => expect(confirm.mock.calls[0]?.[0]?.body).toEqual({ locale: 'de' }))
})

test('a speech-speed conflict against another device language change never marks the untouched language as edited', async () => {
  let stored: UiSettingsOut = { ...browserDefaults }
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: stored })) as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockImplementation((async ({ body }: { body: { expected_revision: number; speech_pace?: 'slow' | 'normal' | 'fast'; locale?: 'browser' | 'de' | 'en' } }) => {
    if (body.expected_revision !== stored.revision) {
      return { error: { detail: { code: 'ui_settings_conflict' } } }
    }
    stored = { ...stored, ...body, revision: stored.revision + 1 }
    return { data: stored }
  }) as never)
  open()

  fireEvent.click(await screen.findByRole('button', { name: /Sprechtempo Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Schnell' }))

  // Another device changes the language to English before this save lands.
  stored = { ...stored, locale: 'en', revision: 2 }
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  expect(await screen.findByText('Settings changed. Reload and review your choice.')).toBeTruthy()
  expect(screen.getByRole('button', { name: /App language English/ })).toBeTruthy()

  // Selecting another speed before resolving the conflict keeps the original baseline revision and never includes locale.
  fireEvent.click(screen.getByRole('button', { name: /Speech speed Fast/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Slow' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save choices' }))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
  expect(save.mock.calls[1]?.[0]?.body).toEqual({ expected_revision: 1, speech_pace: 'slow' })
  expect(await screen.findByText('Settings changed. Reload and review your choice.')).toBeTruthy()

  // Explicit reload adopts revision 2; a subsequent speed edit saves only speech_pace at revision 2.
  fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
  expect(await screen.findByRole('button', { name: /App language English/ })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /Speech speed Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Fast' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save choices' }))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(3))
  expect(save.mock.calls[2]?.[0]?.body).toEqual({ expected_revision: 2, speech_pace: 'fast' })
})

test('voice model saves together with pace through the global bar and discard restores both', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: browserDefaults } as never)
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: [{ id: 'mistral-small-latest', resolved_id: 'mistral-small-2603', label: 'Small 4', availability: 'available', group: 'general', is_default: false }] } as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockResolvedValue({ data: { ...browserDefaults, voice_model: 'mistral-small-latest', speech_pace: 'fast', revision: 2 } } as never)
  open()
  fireEvent.click(await screen.findByRole('button', { name: /Modell für Sprachmodus Wie im Chat/ }))
  fireEvent.click(await screen.findByRole('menuitemradio', { name: /Small 4.*Empfohlen/ }))
  fireEvent.click(screen.getByRole('button', { name: /Sprechtempo Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Schnell' }))
  fireEvent.click(screen.getByRole('button', { name: 'Verwerfen' }))
  expect(screen.getByRole('button', { name: /Modell für Sprachmodus Wie im Chat/ })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /Modell für Sprachmodus Wie im Chat/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Small 4.*Empfohlen/ }))
  fireEvent.click(screen.getByRole('button', { name: /Sprechtempo Normal/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Schnell' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(save.mock.calls[0]?.[0]?.body).toEqual({ expected_revision: 1, voice_model: 'mistral-small-latest', speech_pace: 'fast' }))
})

test('an unavailable saved voice model falls back visibly and only one Small 4 entry is recommended', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: { ...browserDefaults, voice_model: 'removed-model' } } as never)
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: [
    { id: 'mistral-small-latest', resolved_id: 'mistral-small-2603', label: 'Small 4', availability: 'available', group: 'general', is_default: false },
    { id: 'mistral-small-2603', resolved_id: 'mistral-small-2603', label: 'Small 4 fixed', availability: 'available', group: 'general', is_default: false },
  ] } as never)
  open()
  expect(await screen.findByText('Nicht verfügbar. Der Sprachmodus verwendet das Chat-Modell.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /Modell für Sprachmodus removed-model/ }))
  expect(screen.getAllByRole('menuitemradio', { name: /Empfohlen/ })).toHaveLength(1)
})

test('a voice model conflict reloads untouched language without silently overwriting it', async () => {
  let stored: UiSettingsOut = { ...browserDefaults, voice_model: 'chat' }
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: stored })) as never)
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: [{ id: 'mistral-small-latest', resolved_id: 'mistral-small-2603', label: 'Small 4', availability: 'available', group: 'general', is_default: false }] } as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockImplementation((async ({ body }: { body: { expected_revision: number } }) => {
    if (body.expected_revision !== stored.revision) return { error: { detail: { code: 'ui_settings_conflict' } } }
    return { data: { ...stored, ...body, revision: stored.revision + 1 } }
  }) as never)
  open()
  fireEvent.click(await screen.findByRole('button', { name: /Modell für Sprachmodus Wie im Chat/ }))
  fireEvent.click(await screen.findByRole('menuitemradio', { name: /Small 4.*Empfohlen/ }))
  stored = { ...stored, locale: 'en', revision: 2 }
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  expect(await screen.findByText('Settings changed. Reload and review your choice.')).toBeTruthy()
  expect(screen.getByRole('button', { name: /App language English/ })).toBeTruthy()
  expect(save.mock.calls[0]?.[0]?.body).toEqual({ expected_revision: 1, voice_model: 'mistral-small-latest' })
})


test('voice selection saves with the global revision and warns for another language', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: browserDefaults } as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockResolvedValue({ data: { ...browserDefaults, voice_de: 'voice_123', revision: 2 } } as never)
  open()
  const german = await screen.findByRole('button', { name: 'Deutsch' })
  await waitFor(() => expect(german.textContent).toContain('Heidi (Standard)'))
  expect(screen.getByRole('button', { name: 'Englisch' }).textContent).toContain('Sheila (Standard)')
  fireEvent.click(german)
  fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Example' }))
  expect(screen.getByText('Diese Stimme ist nicht für diese Sprache ausgelegt.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(save.mock.calls[0]?.[0]?.body).toEqual({ expected_revision: 1, voice_de: 'voice_123' }))
})


test('the default appears once in its language group and saves null', async () => {
  spyOn(sdk, 'settingsUiGet').mockResolvedValue({ data: { ...browserDefaults, voice_de: 'voice_123' } } as never)
  const save = spyOn(sdk, 'settingsUiUpdate').mockResolvedValue({ data: { ...browserDefaults, revision: 2 } } as never)
  open()
  const german = await screen.findByRole('button', { name: 'Deutsch' })
  await screen.findByText('Example')
  fireEvent.click(german)
  expect(await screen.findByRole('group', { name: 'Deutsch' })).toBeTruthy()
  expect(screen.getAllByRole('menuitemradio', { name: 'Heidi (Standard)' })).toHaveLength(1)
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Heidi (Standard)' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(save.mock.calls[0]?.[0]?.body).toEqual({ expected_revision: 1, voice_de: null }))
})

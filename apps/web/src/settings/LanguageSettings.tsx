// i18n-exempt: CSS classes and preference values.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { settingsUiUpdate } from '../api/generated/sdk.gen'
import type { UiSettingsOut } from '../api/generated/types.gen'
import { cacheUiSettings, uiSettingsQuery } from '../api/queries'
import { errorCode } from '../api/unwrap'
import { Button, Chip, Icon, Menu, Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import type { LocalePreference } from '../i18n/locales'
import './settings.css'

const choices: LocalePreference[] = ['browser', 'de', 'en']

export function LanguageSettings() {
  const { texts } = useI18n()
  const query = useQuery(uiSettingsQuery())
  return <><section className="le-swipe" aria-labelledby="le-language-title">
    <h2 id="le-language-title" className="heading">{texts.language.title}</h2>
    {query.data ? <LanguageForm settings={query.data} /> : query.isPending
      ? <p role="status">{texts.settingsUi.loading}</p>
      : <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void query.refetch() } }}>{texts.settingsUi.error}</Notice>}
  </section>{query.data && <SpeechPaceForm settings={query.data} />}</>
}

function LanguageForm({ settings }: { settings: UiSettingsOut }) {
  const { texts } = useI18n()
  const client = useQueryClient()
  const [edit, setEdit] = useState<{ preference: LocalePreference; revision: number }>()
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<'saved' | 'error' | 'conflict'>()
  const saving = useRef(false)
  const current = settings.locale ?? 'browser'
  const chosen = edit?.preference ?? current

  async function save() {
    if (!edit || saving.current || outcome === 'conflict') return
    saving.current = true
    setBusy(true)
    setOutcome(undefined)
    try {
      const result = await settingsUiUpdate({ body: { locale: edit.preference, expected_revision: edit.revision } })
      const saved = result.data
      if (errorCode(result.error) === 'ui_settings_conflict') {
        await client.refetchQueries({ queryKey: uiSettingsQuery().queryKey, exact: true }, { throwOnError: true })
        setOutcome('conflict')
      } else if (!saved) {
        setOutcome('error')
      } else {
        await cacheUiSettings(client, saved)
        setEdit(undefined)
        setOutcome('saved')
      }
    } catch {
      setOutcome('error')
    } finally {
      saving.current = false
      setBusy(false)
    }
  }

  return <form onSubmit={event => { event.preventDefault(); void save() }}>
    <p className="le-swipe-intro">{texts.language.description}</p>
    <Menu trigger={props => <Chip {...props} quiet disabled={busy} aria-labelledby="le-language-title le-language-choice"><span id="le-language-choice">{texts.language.options[chosen]}</span><Icon name="chevron-down" size="sm" /></Chip>}
      items={choices.map(preference => ({ label: texts.language.options[preference], checked: preference === chosen, onSelect: () => {
        setEdit(preference === current ? undefined : { preference, revision: settings.revision })
        setOutcome(undefined)
      } }))} />
    {outcome === 'saved' && <Notice tone="success">{texts.language.success}</Notice>}
    {outcome === 'error' && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
    {outcome === 'conflict' && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { setEdit(undefined); setOutcome(undefined) } }}>
      {texts.language.conflict(texts.language.options[current])}
    </Notice>}
    <div className="le-swipe-actions"><Button type="submit" variant="primary" disabled={!edit || busy || outcome === 'conflict'}>{texts.language.save}</Button></div>
  </form>
}


type Pace = 'slow' | 'normal' | 'fast'
function SpeechPaceForm({ settings }: { settings: UiSettingsOut }) {
  const { texts } = useI18n()
  const client = useQueryClient()
  const [edit, setEdit] = useState<{ pace: Pace; revision: number }>()
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<'saved' | 'error' | 'conflict'>()
  const saving = useRef(false)
  const current = settings.speech_pace ?? 'normal'
  const chosen = edit?.pace ?? current
  async function save() {
    if (!edit || saving.current || outcome === 'conflict') return
    saving.current = true
    setBusy(true); setOutcome(undefined)
    try {
      const result = await settingsUiUpdate({ body: { speech_pace: edit.pace, expected_revision: edit.revision } })
      if (errorCode(result.error) === 'ui_settings_conflict') {
        await client.refetchQueries({ queryKey: uiSettingsQuery().queryKey, exact: true }, { throwOnError: true })
        setOutcome('conflict')
      } else if (!result.data) setOutcome('error')
      else { await cacheUiSettings(client, result.data); setEdit(undefined); setOutcome('saved') }
    } catch { setOutcome('error') } finally { saving.current = false; setBusy(false) }
  }
  return <section className="le-swipe" aria-labelledby="le-pace-title"><h2 id="le-pace-title" className="heading">{texts.speechPace.title}</h2>
    <form onSubmit={event => { event.preventDefault(); void save() }}>
      <p className="le-swipe-intro">{texts.speechPace.description}</p>
      <Menu trigger={props => <Chip {...props} quiet disabled={busy} aria-labelledby="le-pace-title le-pace-choice"><span id="le-pace-choice">{texts.speechPace.options[chosen]}</span><Icon name="chevron-down" size="sm" /></Chip>}
        items={(['slow', 'normal', 'fast'] as Pace[]).map(pace => ({ label: texts.speechPace.options[pace], checked: pace === chosen, onSelect: () => { setEdit(pace === current ? undefined : { pace, revision: settings.revision }); setOutcome(undefined) } }))} />
      {outcome === 'saved' && <Notice tone="success">{texts.speechPace.success}</Notice>}
      {outcome === 'error' && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
      {outcome === 'conflict' && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { setEdit(undefined); setOutcome(undefined) } }}>{texts.speechPace.conflict}</Notice>}
      <div className="le-swipe-actions"><Button type="submit" variant="primary" disabled={!edit || busy || outcome === 'conflict'}>{texts.speechPace.save}</Button></div>
    </form>
  </section>
}

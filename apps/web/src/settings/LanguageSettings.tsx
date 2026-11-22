// i18n-exempt: CSS classes and preference values.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { settingsUiUpdate } from '../api/generated/sdk.gen'
import type { UiSettingsOut } from '../api/generated/types.gen'
import { cacheUiSettings, uiSettingsQuery } from '../api/queries'
import { errorCode } from '../api/unwrap'
import { Chip, Icon, Menu, Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import type { LocalePreference } from '../i18n/locales'
import { SettingsSaveScope, useSettingsSlice } from './SettingsSave'
import './settings.css'

const choices: LocalePreference[] = ['browser', 'de', 'en']
type Pace = 'slow' | 'normal' | 'fast'
const paces: Pace[] = ['slow', 'normal', 'fast']

export function LanguageSettings() {
  const { texts } = useI18n()
  const client = useQueryClient()
  const query = useQuery(uiSettingsQuery())
  if (!query.data) {
    return <section className="le-swipe" aria-labelledby="le-language-title">
      <h2 id="le-language-title" className="heading">{texts.language.title}</h2>
      {query.isPending
        ? <p role="status">{texts.settingsUi.loading}</p>
        : <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void query.refetch() } }}>{texts.settingsUi.error}</Notice>}
    </section>
  }
  return <SettingsSaveScope>
    <LanguageAndPaceForm
      settings={query.data}
      reload={async () => {
        await client.refetchQueries({ queryKey: uiSettingsQuery().queryKey, exact: true }, { throwOnError: true })
        return client.getQueryData<UiSettingsOut>(uiSettingsQuery().queryKey) ?? query.data
      }}
    />
  </SettingsSaveScope>
}

function LanguageAndPaceForm({ settings, reload }: { settings: UiSettingsOut; reload: () => Promise<UiSettingsOut> }) {
  const { texts } = useI18n()
  const client = useQueryClient()
  const [base, setBase] = useState<UiSettingsOut>(settings)
  const baseLocale: LocalePreference = base.locale ?? 'browser'
  const basePace: Pace = base.speech_pace ?? 'normal'
  const [locale, setLocale] = useState<LocalePreference>(baseLocale)
  const [pace, setPace] = useState<Pace>(basePace)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<'saved' | 'error' | 'conflict'>()
  const [touchedFields, setTouchedFields] = useState<{ locale: boolean; pace: boolean }>({ locale: false, pace: false })
  const saving = useRef(false)

  if (settings.revision > base.revision && locale === baseLocale && pace === basePace && !busy && outcome !== 'conflict') {
    setBase(settings)
    setLocale(settings.locale ?? 'browser')
    setPace(settings.speech_pace ?? 'normal')
    setOutcome(undefined)
  }

  const localeChanged = locale !== baseLocale
  const paceChanged = pace !== basePace
  const hasChange = localeChanged || paceChanged
  const dirty = hasChange || outcome === 'conflict' || outcome === 'error'
  const canSave = hasChange && !busy && outcome !== 'conflict'

  function discard() {
    if (saving.current) return
    const latest = settings.revision >= base.revision ? settings : base
    setBase(latest)
    setLocale(latest.locale ?? 'browser')
    setPace(latest.speech_pace ?? 'normal')
    setOutcome(undefined)
  }

  async function save() {
    if (!hasChange || saving.current || outcome === 'conflict') return
    saving.current = true
    setBusy(true)
    setOutcome(undefined)
    const attempted = { locale: localeChanged, pace: paceChanged }
    setTouchedFields(attempted)
    try {
      const result = await settingsUiUpdate({
        body: {
          expected_revision: base.revision,
          ...(localeChanged ? { locale } : {}),
          ...(paceChanged ? { speech_pace: pace } : {}),
        },
      })
      const saved = result.data
      if (errorCode(result.error) === 'ui_settings_conflict') {
        const current = await reload()
        setBase(prev => ({
          ...prev,
          ...(!attempted.locale ? { locale: current.locale } : {}),
          ...(!attempted.pace ? { speech_pace: current.speech_pace } : {}),
        }))
        if (!attempted.locale) setLocale(current.locale ?? 'browser')
        if (!attempted.pace) setPace(current.speech_pace ?? 'normal')
        setOutcome('conflict')
      } else if (!saved) {
        setOutcome('error')
      } else {
        const current = await cacheUiSettings(client, saved)
        setBase(current)
        setLocale(current.locale ?? 'browser')
        setPace(current.speech_pace ?? 'normal')
        setOutcome('saved')
      }
    } catch {
      setOutcome('error')
    } finally {
      saving.current = false
      setBusy(false)
    }
  }

  useSettingsSlice('ui-language', { dirty, canSave, busy, save, discard })

  return <>
    <section className="le-swipe" aria-labelledby="le-language-title">
      <h2 id="le-language-title" className="heading">{texts.language.title}</h2>
      <p className="le-swipe-intro">{texts.language.description}</p>
      <Menu
        trigger={props => <Chip {...props} quiet disabled={busy} aria-labelledby="le-language-title le-language-choice"><span id="le-language-choice">{texts.language.options[locale]}</span><Icon name="chevron-down" size="sm" /></Chip>}
        items={choices.map(preference => ({
          label: texts.language.options[preference],
          checked: preference === locale,
          onSelect: () => {
            setLocale(preference)
            setOutcome(undefined)
          },
        }))}
      />
      {outcome === 'saved' && touchedFields.locale && <Notice tone="success">{texts.language.success}</Notice>}
      {outcome === 'error' && touchedFields.locale && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
      {outcome === 'conflict' && touchedFields.locale && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: discard }}>
        {texts.language.conflict(texts.language.options[settings.locale ?? 'browser'])}
      </Notice>}
    </section>
    <section className="le-swipe" aria-labelledby="le-pace-title">
      <h2 id="le-pace-title" className="heading">{texts.speechPace.title}</h2>
      <p className="le-swipe-intro">{texts.speechPace.description}</p>
      <Menu
        trigger={props => <Chip {...props} quiet disabled={busy} aria-labelledby="le-pace-title le-pace-choice"><span id="le-pace-choice">{texts.speechPace.options[pace]}</span><Icon name="chevron-down" size="sm" /></Chip>}
        items={paces.map(choice => ({
          label: texts.speechPace.options[choice],
          checked: choice === pace,
          onSelect: () => {
            setPace(choice)
            setOutcome(undefined)
          },
        }))}
      />
      {outcome === 'saved' && touchedFields.pace && <Notice tone="success">{texts.speechPace.success}</Notice>}
      {outcome === 'error' && !touchedFields.locale && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
      {outcome === 'conflict' && touchedFields.pace && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: discard }}>{texts.speechPace.conflict}</Notice>}
    </section>
  </>
}

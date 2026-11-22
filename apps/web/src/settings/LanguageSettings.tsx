// i18n-exempt: CSS classes and preference values.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { settingsUiUpdate } from '../api/generated/sdk.gen'
import type { UiSettingsOut } from '../api/generated/types.gen'
import { cacheUiSettings, uiSettingsQuery, modelsQuery } from '../api/queries'
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
  const catalogue = useQuery(modelsQuery())
  const [base, setBase] = useState<UiSettingsOut>(settings)
  const baseLocale: LocalePreference = base.locale ?? 'browser'
  const basePace: Pace = base.speech_pace ?? 'normal'
  const baseModel = base.voice_model ?? 'chat'
  const [locale, setLocale] = useState<LocalePreference>(baseLocale)
  const [pace, setPace] = useState<Pace>(basePace)
  const [model, setModel] = useState(baseModel)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<'saved' | 'error' | 'conflict'>()
  const [touchedFields, setTouchedFields] = useState<{ locale: boolean; pace: boolean; model: boolean }>({ locale: false, pace: false, model: false })
  const saving = useRef(false)

  if (settings.revision > base.revision && locale === baseLocale && pace === basePace && model === baseModel && !busy && outcome !== 'conflict') {
    setBase(settings)
    setLocale(settings.locale ?? 'browser')
    setPace(settings.speech_pace ?? 'normal')
    setModel(settings.voice_model ?? 'chat')
    setOutcome(undefined)
  }

  const localeChanged = locale !== baseLocale
  const paceChanged = pace !== basePace
  const modelChanged = model !== baseModel
  const hasChange = localeChanged || paceChanged || modelChanged
  const dirty = hasChange || outcome === 'conflict' || outcome === 'error'
  const canSave = hasChange && !busy && outcome !== 'conflict'
  const availableModels = (catalogue.data ?? []).filter(item => item.availability === 'available')
  const recommended = availableModels.find(item => item.id === 'mistral-small-latest' && item.resolved_id === 'mistral-small-2603')
    ?? availableModels.find(item => item.resolved_id === 'mistral-small-2603')

  function discard() {
    if (saving.current) return
    const latest = settings.revision >= base.revision ? settings : base
    setBase(latest)
    setLocale(latest.locale ?? 'browser')
    setPace(latest.speech_pace ?? 'normal')
    setModel(latest.voice_model ?? 'chat')
    setOutcome(undefined)
  }

  async function save() {
    if (!hasChange || saving.current || outcome === 'conflict') return
    saving.current = true
    setBusy(true)
    setOutcome(undefined)
    const attempted = { locale: localeChanged, pace: paceChanged, model: modelChanged }
    setTouchedFields(attempted)
    try {
      const result = await settingsUiUpdate({
        body: {
          expected_revision: base.revision,
          ...(localeChanged ? { locale } : {}),
          ...(paceChanged ? { speech_pace: pace } : {}),
          ...(modelChanged ? { voice_model: model } : {}),
        },
      })
      const saved = result.data
      if (errorCode(result.error) === 'ui_settings_conflict') {
        const current = await reload()
        setBase(prev => ({
          ...prev,
          ...(!attempted.locale ? { locale: current.locale } : {}),
          ...(!attempted.pace ? { speech_pace: current.speech_pace } : {}),
          ...(!attempted.model ? { voice_model: current.voice_model } : {}),
        }))
        if (!attempted.locale) setLocale(current.locale ?? 'browser')
        if (!attempted.pace) setPace(current.speech_pace ?? 'normal')
        if (!attempted.model) setModel(current.voice_model ?? 'chat')
        setOutcome('conflict')
      } else if (!saved) {
        setOutcome('error')
      } else {
        const current = await cacheUiSettings(client, saved)
        setBase(current)
        setLocale(current.locale ?? 'browser')
        setPace(current.speech_pace ?? 'normal')
        setModel(current.voice_model ?? 'chat')
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
    <section className="le-swipe" aria-labelledby="le-voice-model-title">
      <h2 id="le-voice-model-title" className="heading">{texts.voiceModel.title}</h2>
      <p className="le-swipe-intro">{texts.voiceModel.description}</p>
      <Menu
        trigger={props => <Chip {...props} quiet disabled={busy} aria-labelledby="le-voice-model-title le-voice-model-choice"><span id="le-voice-model-choice">{model === 'chat' ? texts.voiceModel.chat : catalogue.data?.find(item => item.id === model || item.resolved_id === model)?.label ?? model}</span><Icon name="chevron-down" size="sm" /></Chip>}
        items={[
          { label: texts.voiceModel.chat, checked: model === 'chat', onSelect: () => { setModel('chat'); setOutcome(undefined) } },
          ...availableModels.map(item => ({
            label: item.id === recommended?.id ? `${item.label} · ${texts.voiceModel.recommended}` : item.label,
            checked: item.id === model, onSelect: () => { setModel(item.id); setOutcome(undefined) },
          })),
        ]}
      />
      {catalogue.isError || (catalogue.data ?? []).some(item => item.availability === 'unverified') ? <Notice tone="info">{texts.voiceModel.unverified}</Notice> : null}
      {model !== 'chat' && catalogue.isSuccess && !(catalogue.data ?? []).some(item => item.availability === 'unverified') && !availableModels.some(item => item.id === model || item.resolved_id === model) && <Notice tone="info">{texts.voiceModel.unavailable}</Notice>}
      {model === 'chat' ? <p className="le-swipe-intro">{texts.voiceModel.chatDetail}</p> : (() => {
        const id = catalogue.data?.find(item => item.id === model || item.resolved_id === model)?.resolved_id
        const detail = id === 'mistral-small-2603' ? texts.voiceModel.small : id === 'mistral-medium-3-5' ? texts.voiceModel.medium : id === 'mistral-large-4' || id === 'mistral-large-4-0' ? texts.voiceModel.large : null
        return detail ? <p className="le-swipe-intro">{detail}</p> : null
      })()}
      {outcome === 'saved' && touchedFields.model && <Notice tone="success">{texts.settingsUi.saved}</Notice>}
      {outcome === 'error' && touchedFields.model && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
      {outcome === 'conflict' && touchedFields.model && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: discard }}>{texts.speechPace.conflict}</Notice>}
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
      {outcome === 'error' && !touchedFields.locale && !touchedFields.model && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
      {outcome === 'conflict' && touchedFields.pace && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: discard }}>{texts.speechPace.conflict}</Notice>}
    </section>
  </>
}

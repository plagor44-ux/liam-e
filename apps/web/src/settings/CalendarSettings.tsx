import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { calendarSettingsUpdate } from '../api/generated/sdk.gen'
import type { CalendarSettingsOut } from '../api/generated/types.gen'
import { calendarSettingsQuery, chatCalendarSourcesQuery } from '../api/queries'
import { errorCode } from '../api/unwrap'
import { calendarTargetLabel } from '../calendar/state'
import { Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import { SettingsSaveScope, useSettingsSlice } from './SettingsSave'
import './settings.css'

export function CalendarSettings() {
  const { texts } = useI18n()
  const t = texts.calendarSettings
  const client = useQueryClient()
  const settings = useQuery(calendarSettingsQuery())
  const sources = useQuery(chatCalendarSourcesQuery())
  const [edit, setEdit] = useState<CalendarSettingsOut>()
  const [status, setStatus] = useState<'saving' | 'saved' | 'conflict' | 'invalid' | 'error'>()
  const saving = useRef(false)
  const shown = edit ?? settings.data
  const choices = sources.data ?? []
  const selected = choices.find(source => source.id === shown?.default_source_id)
  const missing = Boolean(shown?.default_source_id && !selected)
  const hasChange = Boolean(edit && edit.default_source_id !== settings.data?.default_source_id)
  const dirty = hasChange || status === 'conflict' || status === 'invalid' || status === 'error'
  const canSave = hasChange && status !== 'saving' && status !== 'conflict' &&
    (!edit?.default_source_id || Boolean(selected?.is_active && selected.chat_access === 'writable'))

  function discard() {
    if (saving.current) return
    setEdit(undefined)
    setStatus(undefined)
  }

  async function reload() {
    const [config, list] = await Promise.all([settings.refetch(), sources.refetch()])
    if (config.isSuccess && list.isSuccess) {
      setEdit(undefined)
      setStatus(undefined)
    }
  }

  async function save() {
    if (!edit || !canSave || saving.current) return
    saving.current = true
    setStatus('saving')
    try {
      const result = await calendarSettingsUpdate({ body: { expected_version: edit.version, default_source_id: edit.default_source_id } })
      if (!result.data) {
        const code = errorCode(result.error)
        setStatus(code === 'version_conflict' ? 'conflict' : code === 'calendar_settings_invalid' ? 'invalid' : 'error')
        return
      }
      const saved = result.data
      await client.cancelQueries({ queryKey: calendarSettingsQuery().queryKey })
      client.setQueryData(calendarSettingsQuery().queryKey, current => current && current.version > saved.version ? current : saved)
      setEdit(undefined)
      setStatus('saved')
    } catch {
      setStatus('error')
    } finally {
      saving.current = false
    }
  }

  useSettingsSlice('calendar-default', { dirty, canSave, busy: status === 'saving', save, discard })

  const retry = { label: texts.settingsUi.reload, onClick: () => { void reload() } }
  return <SettingsSaveScope><section className="le-swipe" aria-labelledby="le-default-calendar">
    <h2 className="heading" id="le-default-calendar">{t.title}</h2>
    <p className="le-swipe-intro">{t.description}</p>
    {settings.isError || sources.isError ? <Notice tone="danger" action={retry}>{texts.settingsUi.error}</Notice>
      : !shown || sources.isPending ? <p role="status">{texts.settingsUi.loading}</p>
      : <div className="le-swipe-fields">
        <select className="le-input" aria-label={t.title} value={shown.default_source_id ?? ''}
          disabled={status === 'conflict' || status === 'saving'} onChange={event => {
            const nextId = event.target.value || null
            if (settings.data && nextId === settings.data.default_source_id) {
              setEdit(undefined)
            } else {
              setEdit({ version: settings.data?.version ?? shown.version, default_source_id: nextId })
            }
            setStatus(undefined)
          }}>
          <option value="">{t.clear}</option>
          {missing && <option disabled value={shown.default_source_id!}>{t.missing}</option>}
          {choices.map(source => <option value={source.id} key={source.id} disabled={source.chat_access !== 'writable'}>
            {calendarTargetLabel(source, choices)}{source.chat_access !== 'writable' && ' · ' + t.reason[source.chat_access ?? 'unreadable']}
          </option>)}
        </select>
        {selected && selected.chat_access !== 'writable' && <Notice tone="info">{t.defaultUnavailable(selected.name, t.reason[selected.chat_access ?? 'unreadable'])}</Notice>}
        {!choices.some(source => source.chat_access === 'writable') && <Notice tone="info">{t.noWritable}</Notice>}
        {status === 'conflict' && <Notice tone="danger" action={retry}>{t.conflict}</Notice>}
        {status === 'invalid' && <Notice tone="danger" action={retry}>{t.invalid}</Notice>}
        {status === 'error' && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
        {status === 'saved' && <Notice tone="success">{texts.settingsUi.saved}</Notice>}
      </div>}
  </section></SettingsSaveScope>
}

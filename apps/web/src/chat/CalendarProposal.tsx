// i18n-exempt: CSS classes, query keys and protocol literals.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useLayoutEffect, useRef, useState } from 'react'
import { chatProposalCancel, chatProposalCheck, chatProposalConfirm } from '../api/generated/sdk.gen'
import type { CalendarValues, ChatMessageOut, LiveEvent, SourceOut } from '../api/generated/types.gen'
import { calendarSettingsQuery, chatCalendarSourcesQuery } from '../api/queries'
import { errorCode } from '../api/unwrap'
import { Button, Notice } from '../ds'
import { Dot } from '../ds/Chip'
import { formatMinuteOfDay } from '../i18n/format'
import { useI18n } from '../i18n/I18n'
import type { Catalogue } from '../i18n/catalogue'
import { addDays, calendarDate, calendarTargetLabel } from '../calendar/state'
import { writeError } from '../calendar/write-state'
import './calendar-proposal.css'

function Values({ value, zone }: { value: CalendarValues | LiveEvent; zone: string }) {
  const { texts, locale, localeTag } = useI18n()
  const at = (instant?: string | null) => instant ? new Intl.DateTimeFormat(localeTag, { dateStyle: 'medium', timeStyle: 'short', timeZone: zone }).format(new Date(instant)) : ''
  const local = (text?: string | null) => {
    if (!text) return ''
    const [hour = 0, minute = 0] = text.slice(11, 16).split(':').map(Number)
    return `${calendarDate(text.slice(0, 10), undefined, locale)} ${formatMinuteOfDay(hour * 60 + minute, locale)}`
  }
  const start = 'start_at' in value ? at(value.start_at) : local((value as CalendarValues).start_local)
  const end = 'end_at' in value ? at(value.end_at) : local((value as CalendarValues).end_local)
  return <><strong>{value.title}</strong><p>{value.start_date ? `${calendarDate(value.start_date, undefined, locale)} – ${calendarDate(addDays(value.end_date!, -1), undefined, locale)} · ${texts.calendar.allDay}` : `${start} – ${end}`}</p>{value.location && <p>{value.location}</p>}{value.description && <p className="le-calendar-proposal-description">{value.description}</p>}</>
}
/** Why a new appointment did not simply go to the default calendar; nothing when the default is usable or is this card's target. */
function defaultCalendarHint(defaultId: string | null, target: string, sources: SourceOut[], texts: Catalogue): string | undefined {
  if (!defaultId) return sources.some(source => source.chat_access === 'writable') ? texts.calendarSettings.noDefault : undefined
  if (defaultId === target) return undefined
  const source = sources.find(item => item.id === defaultId)
  if (!source) return texts.calendarSettings.missing
  return source.chat_access === 'writable' ? undefined : texts.calendarSettings.defaultUnavailable(source.name, texts.calendarSettings.reason[source.chat_access ?? 'unreadable'])
}

export function CalendarProposal({ message }: { message: ChatMessageOut }) {
  const { texts, locale } = useI18n()
  const client = useQueryClient(), sources = useQuery(chatCalendarSourcesQuery())
  const [updated, setUpdated] = useState<ChatMessageOut>()
  const settled = ['done', 'failed', 'cancelled'].includes(message.proposal_status ?? '')
  const current = updated?.id === message.id && !settled ? updated : message
  const command = current.proposal?.calendar
  const createPending = current.proposal_status === 'pending' && command?.action === 'create'
  const settings = useQuery({ ...calendarSettingsQuery(), enabled: createPending })
  const [busy, setBusy] = useState(false), [error, setError] = useState<string>()
  const pending = useRef(false), focus = useRef(false), region = useRef<HTMLElement>(null)
  useLayoutEffect(() => { if (focus.current) { focus.current = false; region.current?.focus() } })
  const apply = async (action: 'confirm' | 'cancel' | 'check') => {
    if (pending.current) return
    pending.current = true; setBusy(true); setError(undefined)
    try {
      const path = { proposal_id: current.id }
      const result = action === 'confirm' ? await chatProposalConfirm({ path, body: { selected_ids: [] } }) : action === 'cancel' ? await chatProposalCancel({ path }) : await chatProposalCheck({ path })
      if (!result.data || (result.response && result.response.status >= 400)) {
        const code = errorCode(result.error)
        if (action === 'check' && result.response?.status === 409) {
          // The server card is not open for a check, e.g. a confirm that never arrived left it
          // pending. Drop the local guess and show the server's card again.
          setUpdated(undefined); focus.current = true
          return
        }
        if (action === 'confirm' && (!result.response || result.response.status >= 500)) { setUpdated({ ...current, proposal_status: 'unknown' }); focus.current = true }
        setError(code === 'proposal_context_revoked' ? texts.proposalErrors.proposal_context_revoked : code === 'proposal_not_pending' ? texts.proposalErrors.proposal_not_pending : writeError(code, locale))
        return
      }
      setUpdated(result.data); focus.current = true
      client.setQueryData<ChatMessageOut[]>(['history', current.conversation_id], rows => rows?.map(row => row.id === result.data!.id ? result.data! : row))
    } catch {
      setError(texts.calendar.writeErrors.calendar_write_unknown)
      if (action === 'confirm') { setUpdated({ ...current, proposal_status: 'unknown' }); focus.current = true }
    }
    finally {
      void client.invalidateQueries({ queryKey: ['calendar'] })
      void client.invalidateQueries({ queryKey: ['history', current.conversation_id] })
      pending.current = false; setBusy(false)
    }
  }
  if (!command) return null
  const source = sources.data?.find(s => s.id === command.source_id)
  const failed = current.proposal?.result?.failed?.[0]?.code
  const status = current.proposal_status
  const defaultHint = createPending && settings.isSuccess && sources.isSuccess ? defaultCalendarHint(settings.data.default_source_id ?? null, command.source_id, sources.data, texts) : undefined
  return <section className="le-confirm le-calendar-proposal" ref={region} tabIndex={-1} aria-label={texts.calendar.cardActions[command.action]}>
    <h3 className="heading">{texts.calendar.cardActions[command.action]}</h3>
    {status === 'done' ? <Notice tone="success">{texts.calendar.cardDone[command.action]}</Notice> : status === 'cancelled' ? <p>{texts.proposal.cancelled}</p> : <>
      {sources.isPending && <><p role="status">{texts.calendar.loading}</p><div className="le-calendar-proposal-placeholder" aria-hidden="true" /></>}
      {sources.isError && <Notice tone="danger" action={{ label: texts.calendar.retry, onClick: () => { void sources.refetch() } }}>{texts.calendar.error}</Notice>}
      {sources.isSuccess && !source?.is_active && <Notice tone="info">{texts.calendar.writeErrors.calendar_unavailable}</Notice>}
      {sources.isSuccess && source?.is_active && source.chat_access !== 'writable' && <Notice tone="info">{texts.calendar.writeErrors.calendar_not_writable}</Notice>}
      {source?.is_active && !sources.isError && <><p><Dot colorIndex={sources.data!.filter(s => s.is_active).findIndex(s => s.id === source.id)} /> {calendarTargetLabel(source, sources.data!)}</p>
        {defaultHint && <p className="le-hint">{defaultHint} <Link className="le-link" to="/settings/calendar">{texts.calendarSettings.title}</Link></p>}
        <p>{current.proposal?.reason}</p>
        <div className="le-calendar-proposal-values">{command.before && <section><h4>{texts.calendar.cardBefore}</h4><Values value={command.before} zone={command.timezone} /></section>}{command.after && <section><h4>{texts.calendar.cardAfter}</h4><Values value={command.after} zone={command.timezone} /></section>}</div>
        <p>{texts.calendar.eventZone}: {command.timezone}</p>
        {command.action === 'delete' && <p>{texts.calendar.deleteWarning}</p>}<p className="le-hint">{texts.calendar.writeRisk}</p>
      </>}
      {status === 'failed' && <Notice tone="danger">{failed === 'calendar_event_changed' ? texts.calendar.cardChanged : failed === 'calendar_write_not_applied' ? texts.calendar.cardNotApplied : writeError(failed, locale)}</Notice>}
      {(status === 'unknown' || status === 'running') && <p role="status">{texts.calendar.checking}</p>}
      <div className="le-calendar-proposal-actions">
        {status === 'pending' && <><Button variant="primary" disabled={busy || !source?.is_active || source.chat_access !== 'writable' || sources.isError} onClick={() => { void apply('confirm') }}>{texts.calendar.cardConfirm}</Button><Button variant="ghost" disabled={busy} onClick={() => { void apply('cancel') }}>{texts.controls.cancel}</Button></>}
        {/* running: a confirm may have died after claiming the card; the server answers while one is still in flight. */}
        {(status === 'unknown' || status === 'running') && <Button variant="secondary" disabled={busy} onClick={() => { void apply('check') }}>{texts.calendar.check}</Button>}
      </div>
    </>}
    {error && <Notice tone="danger">{error}</Notice>}
  </section>
}

// i18n-exempt: API error codes, query keys and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'
import { apiErrorMessage, errorCode } from '../api/unwrap'
import { Button, Notice, Tooltip } from '../ds'
import { formatListDate } from '../i18n/format'
import { useI18n } from '../i18n/I18n'
import {
  AGENT_ACCESS_POLL_MS,
  AgentAccessError,
  agentAccessQuery,
  disableAgentAccess,
  enableAgentAccess,
  type AgentAccessOut,
} from './agentAccessApi'
import { PreferenceDialog } from './Preferences'
import './settings.css'

type Outcome = { tone: 'success' | 'danger'; text: string; reload?: boolean }

function formatStatus(state: AgentAccessOut, texts: ReturnType<typeof useI18n>['texts'], locale: ReturnType<typeof useI18n>['locale']): string {
  const t = texts.agentAccess
  if (!state.enabled) return t.statusOff
  if (state.changed_at) {
    const changed = new Date(state.changed_at)
    if (!Number.isNaN(changed.getTime())) {
      return t.statusOnSince(formatListDate(changed, new Date(), undefined, locale))
    }
  }
  return t.statusOn
}

function mapAccessError(cause: unknown, texts: ReturnType<typeof useI18n>['texts']): Outcome {
  const t = texts.agentAccess
  const status = cause instanceof AgentAccessError ? cause.status : undefined
  const code = errorCode(cause)
  if (code === 'agent_access_owner_unbound') return { tone: 'danger', text: t.errorUnbound }
  if (code === 'agent_access_conflict') return { tone: 'danger', text: t.errorConflict, reload: true }
  if (code === 'agent_access_unavailable' || status === 503) return { tone: 'danger', text: t.errorUnavailable }
  if (status === 409) return { tone: 'danger', text: t.errorConflict, reload: true }
  if (status === 403 || status === 401 || code === 'agent_access_owner_required') {
    return { tone: 'danger', text: t.errorOwner }
  }
  return { tone: 'danger', text: apiErrorMessage(cause, texts, t.errorUnavailable) }
}

/**
 * Owner-controlled switch for outside assistants. Each action applies at once without the shared
 * settings save bar: turning off needs one click with no confirmation; turning on asks first.
 */
export function AgentAccess({ pollIntervalMs = AGENT_ACCESS_POLL_MS }: { pollIntervalMs?: number | false } = {}) {
  const { texts, locale } = useI18n()
  const t = texts.agentAccess
  const client = useQueryClient()
  const query = useQuery(agentAccessQuery(pollIntervalMs))
  const [busy, setBusy] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [outcome, setOutcome] = useState<Outcome>()
  const lock = useRef(false)
  const unboundId = useId()

  if (!query.data) {
    return <section className="le-access">
      <h2 className="heading">{t.title}</h2>
      <p className="le-access-description">{t.description}</p>
      {query.isPending
        ? <><p role="status">{t.loading}</p><div className="le-access-placeholder" aria-hidden="true" /></>
        : <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void query.refetch() } }}>{t.errorUnavailable}</Notice>}
    </section>
  }

  const state = query.data
  const statusText = formatStatus(state, texts, locale)
  const updateCache = async (next: AgentAccessOut) => {
    const { queryKey } = agentAccessQuery()
    await client.cancelQueries({ queryKey, exact: true })
    client.setQueryData<AgentAccessOut>(queryKey, cached =>
      cached && cached.revision > next.revision ? cached : next,
    )
  }

  const disable = async () => {
    if (lock.current) return
    lock.current = true; setBusy(true); setOutcome(undefined)
    try {
      const next = await disableAgentAccess()
      await updateCache(next)
      setOutcome({ tone: 'success', text: t.turnedOff })
    } catch (cause) {
      setOutcome(mapAccessError(cause, texts))
    } finally {
      lock.current = false; setBusy(false)
    }
  }

  const enable = async () => {
    if (lock.current) return
    lock.current = true; setBusy(true); setOutcome(undefined)
    try {
      const next = await enableAgentAccess(state.revision)
      await updateCache(next)
      setOutcome({ tone: 'success', text: t.turnedOn })
    } catch (cause) {
      setOutcome(mapAccessError(cause, texts))
    } finally {
      setConfirmOpen(false); lock.current = false; setBusy(false)
    }
  }

  return <section className="le-access">
    <h2 className="heading">{t.title}</h2>
    <p className="le-access-description">{t.description}</p>
    <div className="le-access-card">
      <div className="le-access-status">
        <strong className="le-num">{statusText}</strong>
      </div>
      <div className="le-access-actions">
        {state.enabled
          ? <Button variant="primary" disabled={busy} onClick={() => { void disable() }}>{t.turnOff}</Button>
          : <>
              <Tooltip content={t.turnOffTip}><Button variant="secondary" disabled={busy} onClick={() => { void disable() }}>{t.turnOff}</Button></Tooltip>
              <Button variant="primary" disabled={busy || !state.owner_bound} aria-describedby={!state.owner_bound ? unboundId : undefined} onClick={() => setConfirmOpen(true)}>{t.turnOn}</Button>
            </>}
      </div>
    </div>
    {!state.enabled && !state.owner_bound && <Notice tone="info" live={false}><span id={unboundId}>{t.errorUnbound}</span></Notice>}
    {state.enabled && !state.verifier_configured && <Notice tone="info" live={false}>{t.noConnection}</Notice>}
    {outcome && <Notice tone={outcome.tone} action={outcome.reload ? { label: texts.settingsUi.reload, onClick: () => { setOutcome(undefined); void query.refetch() } } : undefined}>{outcome.text}</Notice>}
    <p className="le-access-limits">{t.limits}</p>
    {confirmOpen && <PreferenceDialog title={t.confirmTitle} close={() => { if (!busy) setConfirmOpen(false) }}>
      <p>{t.confirmBody}</p>
      <div className="le-dialog-actions">
        <Button variant="secondary" disabled={busy} onClick={() => setConfirmOpen(false)}>{t.confirmCancel}</Button>
        <Button variant="primary" disabled={busy} onClick={() => { void enable() }}>{t.turnOn}</Button>
      </div>
    </PreferenceDialog>}
  </section>
}

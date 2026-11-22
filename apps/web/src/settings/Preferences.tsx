// i18n-exempt: API keys, route paths and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { preferenceCreate, preferenceUpdate, preferenceDelete, settingsUpdate } from '../api/generated/sdk.gen'
import type { PreferenceOut, PreferenceCreate } from '../api/generated/types.gen'
import { summaryQuery } from '../api/queries'
import { errorCode, unwrap } from '../api/unwrap'
import { Button, Chip, Menu, Notice, TextField } from '../ds'
import { useFocusTrap } from '../ds/useFocusTrap'
import { useI18n } from '../i18n/I18n'
import { preferencesQuery, personalizationQuery } from './preferenceQueries'
import { PreferenceCandidates } from './PreferenceCandidates'
import { SettingsSaveScope, useSettingsSlice } from './SettingsSave'

const codeOf = (cause: unknown) => errorCode(cause instanceof Error ? cause.cause : cause)
function useRefresh() {
  const client = useQueryClient()
  return () => { for (const key of ['preferences', 'personalization', 'preference-context', 'preference-candidates', 'history']) void client.invalidateQueries({ queryKey: [key] }) }
}

export function Personalization({ saved = false }: { saved?: boolean }) {
  const { texts } = useI18n()
  const t = texts.preferences
  const query = useQuery(personalizationQuery()), refresh = useRefresh(), explanationId = useId()
  const [confirm, setConfirm] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const [draft, setDraft] = useState<{ use_preferences: boolean; suggest_preferences: boolean; version: number }>()
  const lock = useRef(false)
  const config = query.data?.personalization

  const shownUse = saved ? (config?.use_preferences ?? false) : (draft?.use_preferences ?? config?.use_preferences ?? false)
  const shownSuggest = saved ? (config?.suggest_preferences ?? false) : (draft?.suggest_preferences ?? config?.suggest_preferences ?? false)

  const stageChange = (nextUse: boolean, nextSuggest: boolean) => {
    if (!config) return
    setError(false)
    if (nextUse === config.use_preferences && nextSuggest === config.suggest_preferences) {
      setDraft(undefined)
    } else {
      setDraft({ use_preferences: nextUse, suggest_preferences: nextSuggest, version: config.version })
    }
  }

  const changeImmediate = async (use: boolean, field: 'use_preferences' | 'suggest_preferences' = 'use_preferences') => {
    if (!config || lock.current) return
    lock.current = true; setBusy(true); setError(false)
    try {
      await unwrap(settingsUpdate({ body: { personalization: { expected_version: config.version, [field]: use } }, throwOnError: true }))
      setConfirm(false); refresh()
    } catch { setError(true) } finally { lock.current = false; setBusy(false) }
  }

  const hasChange = !saved && Boolean(
    draft && config && (draft.use_preferences !== config.use_preferences || draft.suggest_preferences !== config.suggest_preferences),
  )
  const dirty = !saved && (hasChange || error)
  const canSave = !saved && hasChange && !busy && !error

  const saveDraft = async () => {
    if (!draft || !config || !hasChange || lock.current) return
    lock.current = true; setBusy(true); setError(false)
    try {
      const patch: { expected_version: number; use_preferences?: boolean; suggest_preferences?: boolean } = {
        expected_version: draft.version,
      }
      if (draft.use_preferences !== config.use_preferences) patch.use_preferences = draft.use_preferences
      if (draft.suggest_preferences !== config.suggest_preferences) patch.suggest_preferences = draft.suggest_preferences
      await unwrap(settingsUpdate({ body: { personalization: patch }, throwOnError: true }))
      setDraft(undefined)
      refresh()
    } catch {
      setError(true)
    } finally {
      lock.current = false; setBusy(false)
    }
  }

  const discardDraft = () => {
    if (lock.current) return
    setDraft(undefined)
    setError(false)
    setConfirm(false)
  }

  useSettingsSlice(saved ? 'personalization-inline' : 'personalization', {
    dirty,
    canSave,
    busy,
    save: saveDraft,
    discard: discardDraft,
  })

  if (!config) return query.isPending ? <p role="status">{t.loading}</p> : <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void query.refetch() } }}>{t.error}</Notice>
  return <div className="le-prompts">
    <Notice tone="info" action={{ label: shownUse ? t.disable : t.enable, onClick: () => {
      if (busy) return
      if (shownUse) {
        if (saved) void changeImmediate(false)
        else stageChange(false, shownSuggest)
      } else {
        setConfirm(true)
      }
    } }}>{saved ? shownUse ? t.saved : t.savedOff : shownUse ? t.on : t.off}</Notice>
    <Button variant="secondary" role="switch" aria-checked={shownSuggest} aria-describedby={explanationId} disabled={busy} onClick={() => {
      if (saved) void changeImmediate(!shownSuggest, 'suggest_preferences')
      else stageChange(shownUse, !shownSuggest)
    }}>{t.suggestionsSwitch}</Button>
    <p id={explanationId} className="le-hint">{shownSuggest ? t.suggestionsOn : t.suggestionsOff} {t.suggestionsExplanation}</p>
    {error && <Notice tone="danger" action={{ label: t.reload, onClick: () => { discardDraft(); void query.refetch() } }}>{t.conflict}</Notice>}
    {confirm && <PreferenceDialog title={t.enable} close={() => { if (!busy) setConfirm(false) }}><p>{t.transfer}</p><div className="le-dialog-actions"><Button variant="secondary" disabled={busy} onClick={() => setConfirm(false)}>{texts.controls.cancel}</Button><Button variant="primary" disabled={busy} onClick={() => {
      if (saved) void changeImmediate(true)
      else { stageChange(true, shownSuggest); setConfirm(false) }
    }}>{t.enable}</Button></div></PreferenceDialog>}
  </div>
}

// A scope names its account; while the accounts load it waits, and a scope whose account is gone says so.
function useScopeLabel() {
  const { texts } = useI18n()
  const t = texts.preferences
  const summary = useQuery(summaryQuery())
  return (id: string) => {
    if (id === '*') return t.global
    if (!id) return t.chooseAccount
    const account = summary.data?.accounts.find(item => item.id === id)
    return account ? account.display_name || account.email_address : summary.isPending ? texts.taskChat.accountLoading : texts.prompts.scopeUnavailable
  }
}

export function Preferences() {
  const { texts } = useI18n()
  const t = texts.preferences
  const query = useQuery(preferencesQuery())
  const scope = useScopeLabel()
  return <SettingsSaveScope><section className="le-prompts"><div className="le-prompt-actions"><h2 className="heading">{t.title}</h2><Link className="le-btn le-btn--primary" to="/settings/preferences/new">{t.add}</Link></div>
    <Personalization />
    {query.isPending && <><p role="status">{t.loading}</p>{[0, 1, 2].map(i => <div className="le-prompt-placeholder" key={i} aria-hidden="true" />)}</>}
    {query.isError && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void query.refetch() } }}>{t.error}</Notice>}
    {query.data?.length === 0 && <p>{t.empty}</p>}
    {query.data?.map(row => <div className="le-prompt-row" key={row.id}><strong>{t.keys[row.key]}</strong><span className="le-prompt-preview">{row.value}</span><small>{scope(row.scope)} · {row.source === 'confirmed_candidate' ? t.confirmedSource : t.source} · {t.statuses[row.status]}</small><Link className="le-btn le-btn--secondary" to="/settings/preferences/$preferenceId" params={{ preferenceId: row.id }}>{t.edit}</Link></div>)}
    <PreferenceCandidates />
  </section></SettingsSaveScope>
}

export function PreferenceEditor({ id }: { id?: string }) {
  const { texts } = useI18n()
  const t = texts.preferences
  const query = useQuery({ ...preferencesQuery(), enabled: !!id })
  const [generation, setGeneration] = useState(0)
  const row = query.data?.find(p => p.id === id)
  if (id && !row) return <>{query.isPending ? <p role="status">{t.loading}</p> : <Notice tone="danger" action={{ label: t.reload, onClick: () => { void query.refetch() } }}>{query.isError ? t.error : t.unavailable}</Notice>}<Link to="/settings/preferences">{t.back}</Link></>
  return <PreferenceForm key={`${id ?? 'new'}-${generation}`} initial={row} reload={async () => { const result = await query.refetch(); if (result.isError) throw result.error; setGeneration(n => n + 1) }} />
}

function PreferenceForm({ initial, reload }: { initial?: PreferenceOut; reload: () => Promise<void> }) {
  const { texts } = useI18n()
  const t = texts.preferences
  const { data: summary } = useQuery(summaryQuery()), refresh = useRefresh(), scopeLabel = useScopeLabel()
  const [base, setBase] = useState(initial), [key, setKey] = useState<PreferenceCreate['key']>(initial?.key ?? 'answer_length')
  const [scope, setScope] = useState(initial?.scope ?? '*'), [value, setValue] = useState(initial?.value ?? '')
  const [busy, setBusy] = useState(false), [error, setError] = useState<string>(), [conflict, setConflict] = useState(false)
  const [saved, setSaved] = useState(false), [deleting, setDeleting] = useState(false), [removed, setRemoved] = useState(false)
  const lock = useRef(false)
  const accounts = summary?.accounts ?? []
  const execute = async (action: 'save' | 'pause' | 'delete') => {
    if (lock.current) return
    lock.current = true; setBusy(true); setError(undefined); setSaved(false)
    try {
      if (action === 'delete' && base) {
        const result = await preferenceDelete({ path: { id: base.id }, query: { expected_version: base.version } })
        if (!result.response?.ok) throw result.error
        setRemoved(true); setDeleting(false)
      } else {
        const row = base ? await unwrap(preferenceUpdate({ path: { id: base.id }, body: action === 'pause' ? { expected_version: base.version, status: base.status === 'active' ? 'paused' : 'active' } : { expected_version: base.version, value, scope }, throwOnError: true }))
          : await unwrap(preferenceCreate({ body: { key, scope, value }, throwOnError: true }))
        setBase(row); setSaved(true)
      }
      refresh()
    } catch (cause) { const conflict = codeOf(cause) === 'preference_conflict'; setConflict(conflict); setError(conflict ? t.conflict : t.saveError); setDeleting(false) }
    finally { lock.current = false; setBusy(false) }
  }
  if (removed) return <section className="le-prompts"><p role="status">{t.removed}</p><Link to="/settings/preferences">{t.back}</Link><Link to="/">{t.restart}</Link></section>
  return <form className="le-prompts" onSubmit={event => { event.preventDefault(); void execute('save') }}><h2 className="heading">{base ? t.edit : t.add}</h2>
    <div className="le-field"><span className="le-label">{t.field}</span><Menu trigger={props => <Chip {...props} quiet disabled={!!base || busy} aria-label={`${t.field}: ${t.keys[key]}`}>{t.keys[key]}</Chip>} items={(['answer_length', 'salutation', 'signature'] as const).map(k => ({ label: t.keys[k], checked: k === key, onSelect: () => { setKey(k); if (k !== 'answer_length' && scope === '*') setScope('') } }))} /></div>
    <div className="le-field"><span className="le-label">{t.scope}</span><Menu trigger={props => <Chip {...props} quiet disabled={busy} aria-label={`${t.scope}: ${scopeLabel(scope)}`}>{scopeLabel(scope)}</Chip>} items={[...(key === 'answer_length' ? [{ label: t.global, checked: scope === '*', onSelect: () => setScope('*') }] : []), ...accounts.map(a => ({ label: a.display_name || a.email_address, checked: a.id === scope, onSelect: () => setScope(a.id) }))]} /></div>
    <TextField multiline rows={5} label={t.value} value={value} onChange={e => setValue(e.target.value)} maxLength={500} required autoFocus hint={texts.prompts.count(Array.from(value).length, 500)} />
    <p className="le-hint">{base?.source === 'confirmed_candidate' ? t.confirmedSource : t.source}{base && <> · {t.statuses[base.status]}</>}</p><Notice tone="info">{t.safety}</Notice>
    {saved && <Personalization saved />}
    {error && <Notice tone="danger" action={conflict ? { label: t.reload, onClick: () => { void reload().catch(() => setError(t.error)) } } : undefined}>{error}</Notice>}
    <div className="le-prompt-actions"><Button type="submit" variant="primary" disabled={busy || conflict || !scope || !value.trim()}>{t.save}</Button><Link className="le-btn le-btn--secondary" to="/settings/preferences">{t.back}</Link>
      {base && <><Button variant="secondary" disabled={busy || conflict} onClick={() => { void execute('pause') }}>{base.status === 'active' ? t.pause : t.activate}</Button><Button variant="danger" disabled={busy || conflict} onClick={() => setDeleting(true)}>{t.remove}</Button></>}
    </div>
    {deleting && <PreferenceDialog title={t.removeTitle} close={() => { if (!busy) setDeleting(false) }}><p>{t.removeDescription}</p><Link to="/">{t.restart}</Link><div className="le-dialog-actions"><Button variant="secondary" disabled={busy} onClick={() => setDeleting(false)}>{texts.controls.cancel}</Button><Button variant="danger" disabled={busy} onClick={() => { void execute('delete') }}>{t.remove}</Button></div></PreferenceDialog>}
  </form>
}

export function PreferenceDialog({ title, close, children }: { title: string; close: () => void; children: React.ReactNode }) {
  const ref = useFocusTrap(true, close), id = useId()
  return createPortal(<div className="le-scrim"><div className="le-dialog" role="dialog" aria-modal="true" aria-labelledby={id} ref={ref} tabIndex={-1}><h2 id={id} className="heading">{title}</h2>{children}</div></div>, document.body)
}

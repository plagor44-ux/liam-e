import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { chatProposalCancel, chatProposalConfirm, mailActionsUndo } from '../api/generated/sdk.gen'
import type { ChatMessageOut, Proposal } from '../api/generated/types.gen'
import { errorCode, unwrap } from '../api/unwrap'
import { Button, ConfirmCard, Notice } from '../ds'
import { foldersQuery } from '../settings/mailQueries'
import { sortNamed } from '../mail/filingOrder'
import { accountsQuery } from '../api/queries'
import type { Catalogue } from '../i18n/catalogue'
import { useI18n } from '../i18n/I18n'
import { useRestartConversation } from './session'
import { TaskProposal } from './TaskProposal'
import { PreferenceProposal } from './PreferenceProposal'
import { CalendarProposal } from './CalendarProposal'
import type { TaskFormDraft } from '../tasks/TaskForm'

type Clock = { now: () => number; setTimeout: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>; clearTimeout: (id: ReturnType<typeof setTimeout>) => void }
const systemClock: Clock = { now: () => Date.now(), setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id) }
// A live card can be replaced by its history row; preserve edits and the undo deadline.
// After a confirmation or cancellation the clicked button is gone; `focus` moves the focus to the outcome.
// `handoff` passes a held focus to the history row that replaces a settled card in the same commit.
type CardState = { deadline?: number; deselected?: Set<string>; body?: string; task?: TaskFormDraft; focus?: boolean; handoff?: boolean }
const cards = new WeakMap<QueryClient, Map<string, CardState>>()
function cardState(client: QueryClient, id: string): CardState {
  let entries = cards.get(client)
  if (!entries) { entries = new Map(); cards.set(client, entries) }
  let entry = entries.get(id)
  if (!entry) { entry = {}; entries.set(id, entry) }
  return entry
}
function useUndoWindow(client: QueryClient, id: string, clock: Clock) {
  const [deadline] = useState(() => cardState(client, id).deadline ?? 0)
  const [visible, setVisible] = useState(() => clock.now() < deadline)
  useEffect(() => {
    const remaining = deadline - clock.now()
    if (remaining <= 0) { setVisible(false); return }
    const timer = clock.setTimeout(() => setVisible(false), remaining)
    return () => clock.clearTimeout(timer)
  }, [clock, deadline])
  return {
    visible: visible && clock.now() < deadline,
    beforeDeadline: () => clock.now() < deadline,
    expire: () => { cardState(client, id).deadline = 0; setVisible(false) },
  }
}

function safeLink(value: string | null): string | null {
  if (!value || !/^https?:/u.test(value)) return null
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null
  } catch { return null }
}

function failureText(code: string | undefined, kind: Proposal['kind'], texts: Catalogue): string {
  switch (code) {
    case 'task_version_conflict': return texts.tasks.conflict
    case 'task_revision_conflict': return texts.taskChat.revisionConflict
    case 'task_source_gone': return texts.tasks.unavailableMail
    case 'task_scope_invalid': return texts.taskChat.scopeError
    case 'task_not_found': return texts.taskChat.unavailable
    case 'task_invalid': return texts.tasks.invalid
    case 'item_gone': return kind === 'unsubscribe' ? texts.proposalErrors.sender_gone : texts.proposalErrors.item_gone
    case 'item_changed': return texts.proposalErrors.item_changed
    case 'action_unsupported': return texts.proposalErrors.action_unsupported
    case 'provider_action_unsupported': return texts.errors.provider_action_unsupported
    case 'provider_permission_missing': return texts.errors.provider_permission_missing
    case 'credentials_invalid': return texts.proposalErrors.credentials_invalid
    case 'send_failed': return texts.proposalErrors.send_failed
    case 'send_unconfirmed': return texts.proposalErrors.send_unconfirmed
    case 'account_gone': return texts.proposalErrors.account_gone
    case 'unsubscribe_rejected': case 'unsubscribe_blocked': case 'unsubscribe_unreachable': case 'unsubscribe_invalid': case 'unsubscribe_send_failed': return texts.proposalErrors.unsubscribe_failed
    case 'proposal_not_pending': return texts.proposalErrors.proposal_not_pending
    case 'proposal_context_revoked': return texts.proposalErrors.proposal_context_revoked
    case 'selection_empty': return texts.proposalErrors.selection_empty
    case 'host_not_allowed': return texts.errors.host_not_allowed
    default: return texts.proposal.requestError
  }
}

function Result({ message, proposal, clock, onUndone }: { message: ChatMessageOut; proposal: Proposal; clock: Clock; onUndone: () => void }) {
  const { texts } = useI18n()
  const client = useQueryClient()
  const { visible, beforeDeadline, expire } = useUndoWindow(client, message.id, clock)
  const [undone, setUndone] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const pending = useRef(false)
  const result = proposal.result
  const ids = result?.undo_action_ids ?? []
  const undo = async () => {
    if (pending.current || !beforeDeadline()) return
    pending.current = true; setBusy(true); setError(undefined)
    try {
      const outcome = await unwrap(mailActionsUndo({ body: { action_ids: ids }, throwOnError: true }))
      if (outcome.expired.length || outcome.failed.length || outcome.skipped.length) setError(failureText(outcome.error_code ?? (outcome.unsupported?.length ? 'provider_action_unsupported' : undefined), proposal.kind, texts))
      else { expire(); setUndone(true); onUndone() }
    } catch (failure) {
      setError(failureText(errorCode(failure instanceof Error ? failure.cause : failure), proposal.kind, texts))
    } finally {
      void client.invalidateQueries({ queryKey: ['accounts'] })
      void client.invalidateQueries({ queryKey: ['summary'] })
      void client.invalidateQueries({ queryKey: ['messages'] })
      void client.invalidateQueries({ queryKey: ['message'] })
      pending.current = false; setBusy(false)
    }
  }
  if (proposal.kind === 'calendar') return <CalendarProposal message={message} />
  if (proposal.kind === 'task') return <Notice tone="success"><span>{texts.taskChat.done}</span>{result?.task_series_id && <> <Link className="le-link" to="/tasks/$taskId" params={{ taskId: result.task_series_id }}>{texts.tasks.detail}</Link></>}</Notice>
  if (proposal.kind === 'preference') return <Notice tone="success">{result?.preference_outcome === 'kept' ? texts.preferenceCandidates.kept : result?.preference_outcome === 'unchanged' ? texts.preferenceCandidates.unchanged : result?.preferences_enabled ? texts.preferences.saved : texts.preferences.savedOff}</Notice>
  if (proposal.kind === 'filing' || proposal.kind === 'rule') return <Notice tone="success">{proposal.kind === 'filing' ? texts.mailFiling.filingDone : texts.mailFiling.ruleDone}</Notice>
  if (undone) return <Notice tone="info">{texts.proposal.undone}</Notice>
  const failed = result?.failed ?? []
  const entirelyFailed = message.proposal_status === 'failed'
  const otherFailure = entirelyFailed ? failed.find(item => !item.outcome && item.code !== 'provider_permission_missing') : undefined
  const text = entirelyFailed
    ? otherFailure && failureText(otherFailure.code, proposal.kind, texts)
    : proposal.kind === 'unsubscribe' ? texts.proposal.results.unsubscribe(result?.done ?? 0, ids.length)
      : texts.proposal.results[proposal.kind](result?.done ?? 0)
  const gone = failed.filter(item => item.code === 'item_gone').length
  const changed = failed.filter(item => item.code === 'item_changed').length
  const reconciled = failed.filter(item => item.outcome === 'reconciled').length
  const partlyChanged = failed.filter(item => item.outcome === 'partial').length
  const other = failed.length - gone - changed - reconciled - partlyChanged
  return <><Notice tone={entirelyFailed ? 'danger' : message.proposal_status === 'done' ? 'success' : 'info'} action={!entirelyFailed && ids.length && visible && !busy ? { label: texts.proposal.undo, onClick: () => { void undo() } } : undefined}>
    <span>{text}{message.proposal_status === 'partial' && <>{gone > 0 && texts.proposal.partialGone[proposal.kind](gone)}{changed > 0 && texts.proposal.partialChanged(changed)}{other > 0 && texts.proposal.partial[proposal.kind](other)}</>}
      {reconciled > 0 && <> {texts.inbox.bulkProviderReconciled(reconciled)}</>}
      {partlyChanged > 0 && <> {texts.inbox.bulkProviderPartial(partlyChanged)}</>}</span>
  </Notice>{result?.manual?.map(entry => {
    const href = safeLink(entry.link)
    const item = proposal.items.find(candidate => candidate.id === entry.id)
    return <p key={entry.id} className="le-proposal-manual">{item?.title}{item && ': '}{href ? <a href={href} target="_blank" rel="noopener noreferrer">{texts.proposal.manualLink}</a> : texts.proposal.manualNoLink}</p>
  })}{failed.some(item => item.code === 'provider_permission_missing') && <Notice tone="info">{texts.errors.provider_permission_missing}</Notice>}
    {error && <Notice tone="danger">{error}</Notice>}</>
}

export function ProposalCard({ message, clock = systemClock, oldGrant = false, grantVersion, skillRevoked = false }: { message: ChatMessageOut; clock?: Clock; oldGrant?: boolean; grantVersion?: number | null; skillRevoked?: boolean }) {
  const { texts, locale } = useI18n()
  const client = useQueryClient()
  const [updated, setUpdated] = useState<ChatMessageOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [, refresh] = useState(0)
  const pending = useRef(false)
  const outcome = useRef<HTMLDivElement>(null)
  // A response that arrives after the card is gone or shows another proposal must not leave a focus request for a later mount.
  const shown = useRef<string | null>(null)
  useLayoutEffect(() => { shown.current = message.id; return () => { shown.current = null } }, [message.id])
  const current = updated?.id === message.id && !(updated.proposal_status === 'running' && message.proposal_status !== 'pending' && message.proposal_status !== 'running') ? updated : message
  const restart = useRestartConversation(current.conversation_id)
  const proposal = current.proposal
  const destinations = useQuery({ ...foldersQuery(), enabled: proposal?.kind === 'filing' || proposal?.kind === 'rule' })
  const accounts = useQuery({ ...accountsQuery(), enabled: proposal?.kind === 'rule' })
  useEffect(() => {
    if (current.proposal_status !== 'running') return
    let count = 0
    let timer: ReturnType<typeof setTimeout>
    const poll = () => {
      void client.invalidateQueries({ queryKey: ['history', current.conversation_id] })
      count += 1
      if (count < 20) timer = clock.setTimeout(poll, 3_000)
    }
    timer = clock.setTimeout(poll, 3_000)
    return () => clock.clearTimeout(timer)
  }, [client, clock, current.conversation_id, current.id, current.proposal_status])
  const apply = async (operation: () => Promise<ChatMessageOut>, confirm = false) => {
    if (pending.current) return
    pending.current = true; setBusy(true); setError(undefined)
    try {
      const result = await operation()
      if (confirm) cardState(client, result.id).deadline = clock.now() + 10_000
      if (shown.current === result.id) cardState(client, result.id).focus = true
      setUpdated(result)
      client.setQueryData<ChatMessageOut[]>(['history', current.conversation_id], rows => rows?.map(row => row.id === result.id ? { ...result, context: result.context ?? row.context } : row))
      void client.invalidateQueries({ queryKey: ['history', current.conversation_id] })
      void client.invalidateQueries({ queryKey: ['summary'] })
      if (proposal?.kind === 'filing' || proposal?.kind === 'rule') {
        void client.invalidateQueries({ queryKey: ['messages'] }); void client.invalidateQueries({ queryKey: ['mail-rules'] })
        if (proposal.kind === 'filing') void client.invalidateQueries({ queryKey: ['mail-quick-actions'] })
      }
      if (proposal?.kind === 'preference') {
        for (const key of ['preference-candidates', 'preferences', 'personalization']) void client.invalidateQueries({ queryKey: [key] })
      }
      if (confirm) {
        if (result.proposal?.result?.failed?.some(item => item.code === 'provider_permission_missing')) void client.invalidateQueries({ queryKey: ['accounts'] })
        if (proposal?.kind === 'task') void client.invalidateQueries({ queryKey: ['tasks'] })
        void client.invalidateQueries({ queryKey: ['messages'] })
        void client.invalidateQueries({ queryKey: ['message'] })
      }
    } catch (failure) {
      const code = errorCode(failure instanceof Error ? failure.cause : failure) ?? 'action_failed'
      setError(code)
      if (code === 'provider_permission_missing') {
        void client.invalidateQueries({ queryKey: ['accounts'] })
        void client.invalidateQueries({ queryKey: ['summary'] })
      }
      if (proposal?.kind === 'preference') void client.invalidateQueries({ queryKey: ['preference-candidates'] })
      if (code === 'proposal_not_pending') void client.invalidateQueries({ queryKey: ['history', current.conversation_id] })
    } finally { pending.current = false; setBusy(false) }
  }
  // Layout effects: the replaced card's cleanup and the new card's setup run in one commit, before the microtask.
  useLayoutEffect(() => {
    const node = outcome.current
    return () => {
      if (!node?.contains(document.activeElement)) return
      const saved = cardState(client, current.id)
      saved.handoff = true
      queueMicrotask(() => { saved.handoff = false })
    }
  }, [client, current.id, current.proposal_status])
  useLayoutEffect(() => {
    const saved = cardState(client, current.id)
    if (current.proposal_status !== 'pending' && (saved.focus || saved.handoff)) { saved.focus = false; saved.handoff = false; outcome.current?.focus() }
  })
  if (!proposal) return null
  if (oldGrant && current.proposal_status === 'pending') return <>
    <Notice tone="info" action={{ label: texts.controls.cancel, onClick: () => { void apply(() => unwrap(chatProposalCancel({ path: { proposal_id: current.id }, throwOnError: true }))) } }}>{skillRevoked ? texts.skills.cancelOnly : proposal.kind === 'calendar' ? texts.calendarSettings.cancelOnly : texts.chatAccounts.cancelOnly}</Notice>
    {error && <Notice tone="danger">{failureText(error, proposal.kind, texts)}</Notice>}
  </>
  if (proposal.kind === 'calendar') return <CalendarProposal key={current.id} message={current} />
  const settled = (content: ReactNode) => <div ref={outcome} tabIndex={-1} className="le-proposal-outcome">{content}</div>
  if (current.proposal_status === 'running') return settled(<p className="le-hint">{texts.proposal.running}</p>)
  if (current.proposal_status === 'cancelled') return settled(<p className="le-hint">{texts.proposal.cancelled}</p>)
  if (current.proposal_status === 'failed' && !proposal.result?.failed?.some(item => item.outcome)) {
    return settled(<Notice tone="danger">{failureText(proposal.result?.failed?.[0]?.code, proposal.kind, texts)}</Notice>)
  }
  if (current.proposal_status === 'done' || current.proposal_status === 'partial' || current.proposal_status === 'failed') return settled(<Result key={current.id} message={current} proposal={proposal} clock={clock} onUndone={() => outcome.current?.focus()} />)
  if (current.proposal_status !== 'pending') return null
  if (proposal.kind === 'task') return <><TaskProposal key={current.id} grantVersion={grantVersion ?? undefined}
    contextId={!oldGrant && grantVersion != null && current.context?.mail_scope_version === grantVersion && !current.context.revoked ? current.context.id : undefined} proposal={proposal} busy={busy} draft={cardState(client, current.id).task ??= {}}
    confirm={task_edit => { void apply(() => unwrap(chatProposalConfirm({ path: { proposal_id: current.id }, body: { selected_ids: [], task_edit }, throwOnError: true })), true) }}
    cancel={() => { void apply(() => unwrap(chatProposalCancel({ path: { proposal_id: current.id }, throwOnError: true }))) }}
  />{error && <Notice tone="danger" action={error === 'proposal_context_revoked' || error === 'task_scope_invalid' ? { label: texts.shell.newConversation, onClick: restart } : undefined}>{failureText(error, proposal.kind, texts)}</Notice>}</>
  if (proposal.kind === 'preference') return proposal.preference ? <PreferenceProposal key={current.id} id={proposal.preference.candidate_id} busy={busy} error={error} clearError={() => setError(undefined)}
    confirm={preference_decision => { void apply(() => unwrap(chatProposalConfirm({ path: { proposal_id: current.id }, body: { selected_ids: [], preference_decision }, throwOnError: true })), true) }}
    cancel={() => { void apply(() => unwrap(chatProposalCancel({ path: { proposal_id: current.id }, throwOnError: true }))) }}
  /> : null
  if (proposal.kind === 'rule') {
    const rule = proposal.rule
    if (!rule) return null
    const folder = destinations.data?.find(row => row.id === rule.folder_id), label = destinations.data?.find(row => row.id === rule.label_id)
    const account = accounts.data?.find(row => row.id === rule.account_id)
    const loading = destinations.isPending || accounts.isPending
    const loadError = destinations.isError || accounts.isError
    const missingDestination = !loading && !destinations.isError && (Boolean(rule.folder_id && !folder) || Boolean(rule.label_id && !label))
    const missingAccount = !loading && !accounts.isError && Boolean(rule.account_id && !account)
    const unavailable = loading || loadError || missingDestination || missingAccount
    return <section className="le-confirm"><div className="le-confirm-head"><h3 className="le-confirm-title">{texts.mailFiling.ruleConfirm}</h3><span>{proposal.reason}</span></div>
      <p>{rule.name}</p><p>{texts.mailFiling.account}: {account ? account.display_name || account.email_address : rule.account_id ? texts.mailFiling.none : texts.mailFiling.allAccounts}</p>
      {rule.sender_address && <p>{texts.mailFiling.sender}: {rule.sender_address}</p>}{rule.sender_domain && <p>{texts.mailFiling.domain}: {rule.sender_domain}</p>}{rule.subject_contains && <p>{texts.mailFiling.subject}: {rule.subject_contains}</p>}{rule.recipient && <p>{texts.mailFiling.recipient}: {rule.recipient}</p>}
      {folder && <p>{texts.mailFiling.folder}: {folder.name}</p>}{label && <p>{texts.mailFiling.label}: {label.name}</p>}{rule.mark_read && <p>{texts.mailFiling.markRead}</p>}{rule.mark_unread && <p>{texts.mailFiling.markUnread}</p>}{rule.archive && <p>{texts.mailFiling.archive}</p>}
      {loadError && <Notice tone="danger">{texts.mailFiling.error}</Notice>}{missingDestination && <Notice tone="info">{texts.mailFiling.missingDestination}</Notice>}{missingAccount && <Notice tone="info">{texts.mailFiling.missingAccount}</Notice>}{error && <Notice tone="danger">{failureText(error, proposal.kind, texts)}</Notice>}
      <div className="le-confirm-foot"><Button variant="primary" disabled={busy || unavailable} onClick={() => { void apply(() => unwrap(chatProposalConfirm({ path: { proposal_id: current.id }, body: { selected_ids: [] }, throwOnError: true })), true) }}>{texts.mailFiling.addRule}</Button><Button variant="ghost" disabled={busy} onClick={() => { void apply(() => unwrap(chatProposalCancel({ path: { proposal_id: current.id }, throwOnError: true }))) }}>{texts.controls.cancel}</Button></div>
    </section>
  }
  if (proposal.kind === 'filing') {
    const command = proposal.filing
    if (!command) return null
    const saved = cardState(client, current.id), rows = destinations.data ?? []
    const target = command.folder_id ? rows.find(row => row.id === command.folder_id) : undefined
    const added = (command.add_labels ?? []).map(id => rows.find(row => row.id === id)), removed = (command.remove_labels ?? []).map(id => rows.find(row => row.id === id))
    const missingDestination = !destinations.isPending && !destinations.isError && (Boolean(command.folder_id && !target) || added.some(row => !row) || removed.some(row => !row))
    const unavailable = destinations.isPending || destinations.isError || missingDestination
    const details = [texts.mailFiling.filingPending, proposal.reason, command.set_folder ? command.folder_id ? target ? `${texts.mailFiling.folder}: ${target.name}` : '' : texts.mailFiling.clearFolder : '', ...sortNamed(added.filter(row => row !== undefined), locale).map(row => texts.mailFiling.addLabel(row.name)), ...sortNamed(removed.filter(row => row !== undefined), locale).map(row => texts.mailFiling.removeLabel(row.name))].filter(Boolean).join(' · ')
    return <><ConfirmCard title={texts.mailFiling.filingConfirm} reason={details} items={proposal.items} confirmLabel={() => texts.mailFiling.file} deselected={saved.deselected ?? new Set<string>()} onDeselectedChange={value => { saved.deselected = value; refresh(n => n + 1) }} busy={busy} confirmDisabled={unavailable} onConfirm={selected_ids => { void apply(() => unwrap(chatProposalConfirm({ path: { proposal_id: current.id }, body: { selected_ids }, throwOnError: true })), true) }} onCancel={() => { void apply(() => unwrap(chatProposalCancel({ path: { proposal_id: current.id }, throwOnError: true }))) }} />{(error || destinations.isError) && <Notice tone="danger">{texts.mailFiling.error}</Notice>}{missingDestination && <Notice tone="info">{texts.mailFiling.missingDestination}</Notice>}</>
  }
  const saved = cardState(client, current.id)
  const items = proposal.items.map(item => item.kind === 'sender' && !['one_click', 'one-click', 'post', 'mailto'].includes(item.method ?? '') ? { ...item, subtitle: `${item.subtitle} · ${texts.proposal.manualSubtitle}` } : item)
  const irreversibleNote = proposal.irreversible ? proposal.kind === 'unsubscribe' ? texts.proposal.irreversibleUnsubscribe : proposal.kind === 'send' ? texts.proposal.irreversibleSend : undefined : undefined
  return <><ConfirmCard key={current.id} title={texts.proposal.titles[proposal.kind](items.length)} reason={proposal.reason} items={items} irreversibleNote={irreversibleNote} confirmLabel={texts.proposal.confirm[proposal.kind]} draft={proposal.kind === 'send' ? proposal.draft ?? undefined : undefined} deselected={saved.deselected ?? new Set<string>()} onDeselectedChange={value => { saved.deselected = value; refresh(n => n + 1) }} body={saved.body ?? proposal.draft?.body ?? ''} onBodyChange={value => { saved.body = value; refresh(n => n + 1) }} busy={busy} onConfirm={(selected_ids, body) => { void apply(() => unwrap(chatProposalConfirm({ path: { proposal_id: current.id }, body: { selected_ids, body }, throwOnError: true })), true) }} onCancel={() => { void apply(() => unwrap(chatProposalCancel({ path: { proposal_id: current.id }, throwOnError: true }))) }} />{error && <Notice tone="danger" action={error === 'proposal_context_revoked' ? { label: texts.shell.newConversation, onClick: restart } : undefined}>{failureText(error, proposal.kind, texts)}</Notice>}</>
}

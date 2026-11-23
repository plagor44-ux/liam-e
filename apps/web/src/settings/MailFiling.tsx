// i18n-exempt: API keys and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { mailFolderCreate, mailFolderDelete, mailFolderUpdate, mailRulesList, mailRuleCreate, mailRuleUpdate, mailRuleDelete, mailRulesPreview, mailRuleApply, mailRuleRunsList } from '../api/generated/sdk.gen'
import type { MailFolder, MailRule, RuleIn } from '../api/generated/types.gen'
import { unwrap } from '../api/unwrap'
import { accountsQuery } from '../api/queries'
import { foldersQuery } from './mailQueries'
import { Button, Chip, Icon, Menu, Notice, TextField } from '../ds'
import { useI18n } from '../i18n/I18n'
import { PreferenceDialog } from './Preferences'

export function FolderSettings() {
  const { texts } = useI18n(), t = texts.mailFiling
  const client = useQueryClient(), query = useQuery(foldersQuery())
  const [name, setName] = useState(''), [kind, setKind] = useState<'folder' | 'label'>('folder')
  const [editing, setEditing] = useState<MailFolder>(), [deleting, setDeleting] = useState<MailFolder>()
  const [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const refresh = () => { void client.invalidateQueries({ queryKey: ['mail-folders'] }); void client.invalidateQueries({ queryKey: ['messages'] }) }
  const save = async () => {
    if (busy || !name.trim()) return
    setBusy(true); setError(false)
    try {
      if (editing?.id) await unwrap(mailFolderUpdate({ path: { destination_id: editing.id }, body: { name }, throwOnError: true }))
      else await unwrap(mailFolderCreate({ body: { name, kind }, throwOnError: true }))
      setName(''); setEditing(undefined); refresh()
    } catch { setError(true) } finally { setBusy(false) }
  }
  const remove = async () => {
    if (!deleting?.id || busy) return
    setBusy(true); setError(false)
    try { await unwrap(mailFolderDelete({ path: { destination_id: deleting.id }, query: { confirm_delete: true }, throwOnError: true })); setDeleting(undefined); refresh() }
    catch { setError(true) } finally { setBusy(false) }
  }
  return <section className="le-prompts"><h2 className="heading">{t.folders}</h2><p>{t.localOnly}</p>
    <form className="le-filing-form" onSubmit={event => { event.preventDefault(); void save() }}>
      <TextField label={t.name} value={name} maxLength={120} onChange={event => setName(event.target.value)} required />
      {!editing && <div className="le-field"><span className="le-label">{t.kind}</span><Menu trigger={props => <Chip {...props} quiet aria-label={`${t.kind}: ${kind === 'folder' ? t.folder : t.label}`}>{kind === 'folder' ? t.folder : t.label}<Icon name="chevron-down" size="sm" /></Chip>} items={(['folder', 'label'] as const).map(value => ({ label: value === 'folder' ? t.folder : t.label, checked: kind === value, onSelect: () => setKind(value) }))} /></div>}
      <div className="le-prompt-actions"><Button type="submit" variant="primary" disabled={busy || !name.trim()}>{editing ? t.save : t.add}</Button>
      {editing && <Button type="button" variant="secondary" onClick={() => { setEditing(undefined); setName('') }}>{t.cancel}</Button>}</div>
    </form>
    {query.isPending && <p role="status">{t.loading}</p>}
    {(query.isError || error) && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { setError(false); void query.refetch() } }}>{t.error}</Notice>}
    {query.data?.length === 0 && <p>{t.empty}</p>}
    {query.data?.map(row => <div className="le-prompt-row le-filing-row" key={row.id}><strong>{row.name}</strong><small>{row.kind === 'folder' ? t.folder : t.label}</small>
      <Button variant="secondary" onClick={() => { setEditing(row); setName(row.name) }}>{t.rename}</Button>
      <Button variant="secondary" aria-label={t.removeName(row.name)} onClick={() => setDeleting(row)}>{t.remove}</Button></div>)}
    {deleting && <PreferenceDialog title={t.removeTitle} close={() => { if (!busy) setDeleting(undefined) }}><p>{deleting.name}. {t.removeDescription}</p><div className="le-dialog-actions"><Button variant="secondary" disabled={busy} onClick={() => setDeleting(undefined)}>{t.cancel}</Button><Button variant="danger" disabled={busy} onClick={() => { void remove() }}>{t.remove}</Button></div></PreferenceDialog>}
  </section>
}

export function RuleSettings() {
  const { texts } = useI18n(), t = texts.mailFiling
  const client = useQueryClient(), destinations = useQuery(foldersQuery()), accounts = useQuery(accountsQuery())
  const rules = useQuery({ queryKey: ['mail-rules'], queryFn: () => unwrap(mailRulesList({ throwOnError: true })) })
  const [draft, setDraft] = useState<RuleIn>({ name: '', is_active: true })
  const [editing, setEditing] = useState<MailRule>(), [applying, setApplying] = useState<MailRule>(), [deleting, setDeleting] = useState<MailRule>()
  const [showRuns, setShowRuns] = useState<string>(), [matched, setMatched] = useState<number>()
  const [queued, setQueued] = useState<number>()
  const [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const runs = useQuery({ queryKey: ['mail-rule-runs', showRuns], enabled: Boolean(showRuns), refetchInterval: showRuns ? 3000 : false, queryFn: () => unwrap(mailRuleRunsList({ path: { rule_id: showRuns! }, throwOnError: true })) })
  const refresh = () => { void client.invalidateQueries({ queryKey: ['mail-rules'] }); void client.invalidateQueries({ queryKey: ['messages'] }); void client.invalidateQueries({ queryKey: ['mail-rule-runs'] }) }
  const update = (part: Partial<RuleIn>) => { setDraft(old => ({ ...old, ...part })); setMatched(undefined) }
  const execute = async (action: 'save' | 'preview' | 'apply' | 'delete') => {
    if (busy) return
    setBusy(true); setError(false)
    try {
      if (action === 'preview') setMatched((await unwrap(mailRulesPreview({ body: draft, throwOnError: true }))).matched)
      if (action === 'save') {
        if (editing?.id && editing.version) await unwrap(mailRuleUpdate({ path: { rule_id: editing.id }, body: { ...draft, expected_version: editing.version }, throwOnError: true }))
        else await unwrap(mailRuleCreate({ body: draft, throwOnError: true }))
        setDraft({ name: '', is_active: true }); setEditing(undefined); setMatched(undefined); refresh()
      }
      if (action === 'apply' && applying?.id && applying.version) {
        const result = await unwrap(mailRuleApply({ path: { rule_id: applying.id }, body: { expected_version: applying.version, confirm_apply: true }, throwOnError: true }))
        setQueued(result.matched)
        setApplying(undefined); refresh()
      }
      if (action === 'delete' && deleting?.id && deleting.version) {
        await unwrap(mailRuleDelete({ path: { rule_id: deleting.id }, query: { expected_version: deleting.version }, throwOnError: true }))
        setDeleting(undefined); refresh()
      }
    } catch { setError(true) } finally { setBusy(false) }
  }
  const rows = destinations.data ?? []
  const choose = (key: 'account_id' | 'folder_id' | 'label_id', label: string, options: { id?: string; name: string }[]) => {
    const selected = options.find(row => row.id === draft[key])?.name ?? (key === 'account_id' ? t.allAccounts : t.none)
    return <div className="le-field"><span className="le-label">{label}</span><Menu trigger={props => <Chip {...props} quiet aria-label={`${label}: ${selected}`}>{selected}<Icon name="chevron-down" size="sm" /></Chip>} items={[
      { label: key === 'account_id' ? t.allAccounts : t.none, checked: !draft[key], onSelect: () => update({ [key]: null }) },
      ...options.filter(row => row.id).map(row => ({ label: row.name, checked: row.id === draft[key], onSelect: () => update({ [key]: row.id }) })),
    ]} /></div>
  }
  const ruleSummary = (row: MailRule) => {
    const account = (accounts.data ?? []).find(item => item.id === row.account_id)
    const folder = rows.find(item => item.id === row.folder_id), label = rows.find(item => item.id === row.label_id)
    const conditions = [row.account_id && `${t.account}: ${account?.display_name || account?.email_address || t.none}`, row.sender_address && `${t.sender}: ${row.sender_address}`, row.sender_domain && `${t.domain}: ${row.sender_domain}`, row.subject_contains && `${t.subject}: ${row.subject_contains}`, row.recipient && `${t.recipient}: ${row.recipient}`].filter(Boolean)
    const actions = [folder && `${t.folder}: ${folder.name}`, label && `${t.label}: ${label.name}`,
      row.mark_read && t.markRead, row.mark_unread && t.markUnread, row.archive && t.archive].filter(Boolean)
    return <div className="le-filing-summary"><small>{row.is_active ? t.enabled : t.disabled}</small><small>{t.conditions}: {conditions.join(' · ')}</small><small>{t.targets}: {actions.join(' · ')}</small></div>
  }
  const providerOutcomes = (value: string | undefined) => {
    try {
      const parsed: unknown = JSON.parse(value || '{}')
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return []
      return Object.entries(parsed).flatMap(([kind, status]) => {
        const label = kind === 'mark_read' ? t.markRead : kind === 'mark_unread' ? t.markUnread : kind === 'archive' ? t.archive : null
        return label && (status === 'skipped_unsupported' || status === 'unknown') ? [`${label}: ${status === 'unknown' ? t.unknown : t.skipped}`] : []
      })
    } catch { return [] }
  }
  const runAccount = (accountId: string) => {
    const account = (accounts.data ?? []).find(item => item.id === accountId)
    return account?.display_name || account?.email_address || t.none
  }
  return <section className="le-prompts"><h2 className="heading">{t.rules}</h2><p>{t.ruleDelay}</p>
    <form className="le-filing-form" onSubmit={event => { event.preventDefault(); void execute('save') }}>
      <TextField label={t.ruleName} value={draft.name} onChange={event => update({ name: event.target.value })} maxLength={120} required />
      {choose('account_id', t.account, (accounts.data ?? []).map(account => ({ id: account.id, name: account.display_name || account.email_address })))}
      {(['sender_address', 'sender_domain', 'subject_contains', 'recipient'] as const).map((key, index) => <TextField key={key} label={[t.sender, t.domain, t.subject, t.recipient][index]!} value={draft[key] ?? ''} onChange={event => update({ [key]: event.target.value })} />)}
      {choose('folder_id', t.folder, rows.filter(row => row.kind === 'folder'))}{choose('label_id', t.label, rows.filter(row => row.kind === 'label'))}
      <div className="le-prompt-actions">{([['mark_read', t.markRead], ['mark_unread', t.markUnread], ['archive', t.archive], ['is_active', t.active]] as const).map(([key, label]) => {
        const checked = key === 'is_active' ? draft.is_active ?? true : draft[key] ?? false
        return <Button key={key} type="button" variant={checked ? 'primary' : 'secondary'} role="switch" aria-checked={checked} onClick={() => update(key === 'mark_read' ? { mark_read: !checked, mark_unread: false } : key === 'mark_unread' ? { mark_unread: !checked, mark_read: false } : { [key]: !checked })}>{label} · {checked ? t.on : t.off}</Button>
      })}</div>
      <TextField type="number" label={t.priority} value={draft.position ?? 0} onChange={event => update({ position: Number(event.target.value) })} />
      <div className="le-prompt-actions"><Button type="submit" variant="primary" disabled={busy || !draft.name.trim()}>{editing ? t.save : t.addRule}</Button>
      <Button type="button" variant="secondary" disabled={busy || !draft.name.trim()} onClick={() => { void execute('preview') }}>{t.preview}</Button>
      {editing && <Button type="button" variant="secondary" onClick={() => { setEditing(undefined); setDraft({ name: '', is_active: true }) }}>{t.cancel}</Button>}</div>
      {matched !== undefined && <p role="status">{t.matches(matched)}</p>}
    </form>
    {queued !== undefined && <p role="status">{t.queued(queued)}</p>}
    {rules.isPending && <p role="status">{t.loading}</p>}{(error || rules.isError || destinations.isError || accounts.isError) && <Notice tone="danger">{t.error}</Notice>}
    {rules.data?.length === 0 && <p>{t.ruleEmpty}</p>}
    {rules.data?.map(row => <div className="le-prompt-row le-filing-row" key={row.id}><strong>{row.name}</strong>{ruleSummary(row)}
      <Button variant="secondary" onClick={() => { setEditing(row); setDraft({ name: row.name, account_id: row.account_id, sender_address: row.sender_address, sender_domain: row.sender_domain, subject_contains: row.subject_contains, recipient: row.recipient, folder_id: row.folder_id, label_id: row.label_id, mark_read: row.mark_read, mark_unread: row.mark_unread, archive: row.archive, position: row.position, is_active: row.is_active }) }}>{t.editRule}</Button>
      <Button variant="secondary" onClick={() => setApplying(row)}>{t.apply}</Button><Button variant="secondary" onClick={() => setDeleting(row)}>{t.deleteRule}</Button>
      <Button variant="secondary" onClick={() => setShowRuns(showRuns === row.id ? undefined : row.id)}>{t.outcomes}</Button>
      {showRuns === row.id && runs.isPending && <p role="status">{t.loading}</p>}
      {showRuns === row.id && runs.isError && <p role="alert">{t.error}</p>}
      {showRuns === row.id && runs.data?.length === 0 && <p>{t.noRuns}</p>}
      {showRuns === row.id && runs.data?.map(run => <div key={run.id} className="le-filing-summary"><p>{run.status === 'unknown' ? t.unknown : run.status === 'skipped_stale' ? t.stale : run.status === 'skipped_conflict' ? t.conflict : run.status === 'skipped_imap_rebuild' ? `${t.imapRebuild} ${t.account}: ${runAccount(run.account_id)}` : run.status !== 'done' ? t.pending : run.provider_results_json?.includes('overridden') ? t.folderOverridden : run.provider_results_json?.includes('skipped_unsupported') ? t.skipped : t.completed}</p>
        {run.status !== 'done' && run.provider_results_json?.includes('overridden') && <small>{t.folderOverridden}</small>}
        {['done', 'local_done', 'unknown'].includes(run.status ?? '') && (!run.provider_results_json?.includes('overridden') || row.label_id) && <small>{t.localDone}</small>}
        {providerOutcomes(run.provider_results_json).map(detail => <small key={detail}>{detail}</small>)}</div>)}
    </div>)}
    {applying && <PreferenceDialog title={t.applyTitle} close={() => { if (!busy) setApplying(undefined) }}><p>{applying.name}. {t.applyDescription}</p><div className="le-dialog-actions"><Button variant="secondary" onClick={() => setApplying(undefined)}>{t.cancel}</Button><Button variant="primary" disabled={busy} onClick={() => { void execute('apply') }}>{t.apply}</Button></div></PreferenceDialog>}
    {deleting && <PreferenceDialog title={t.deleteRule} close={() => { if (!busy) setDeleting(undefined) }}><p>{deleting.name}</p><div className="le-dialog-actions"><Button variant="secondary" onClick={() => setDeleting(undefined)}>{t.cancel}</Button><Button variant="danger" disabled={busy} onClick={() => { void execute('delete') }}>{t.remove}</Button></div></PreferenceDialog>}
  </section>
}

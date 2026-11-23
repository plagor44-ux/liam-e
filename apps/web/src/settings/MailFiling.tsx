// i18n-exempt: API keys and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { mailFolderCreate, mailFolderDelete, mailFolderUpdate, mailRulesList, mailRuleCreate, mailRuleUpdate, mailRuleDelete, mailRulesPreview, mailRuleApply, mailRuleRunsList } from '../api/generated/sdk.gen'
import type { MailFolder, MailRule, RuleIn } from '../api/generated/types.gen'
import { unwrap } from '../api/unwrap'
import { accountsQuery } from '../api/queries'
import { foldersQuery } from './mailQueries'
import { Button, Notice, TextField } from '../ds'
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
      {!editing && <label className="le-label">{t.kind}<select className="le-input" value={kind} onChange={event => setKind(event.target.value as 'folder' | 'label')}><option value="folder">{t.folder}</option><option value="label">{t.label}</option></select></label>}
      <Button type="submit" variant="primary" disabled={busy || !name.trim()}>{editing ? t.save : t.add}</Button>
      {editing && <Button type="button" variant="secondary" onClick={() => { setEditing(undefined); setName('') }}>{t.cancel}</Button>}
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
  const [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const runs = useQuery({ queryKey: ['mail-rule-runs', showRuns], enabled: Boolean(showRuns), queryFn: () => unwrap(mailRuleRunsList({ path: { rule_id: showRuns! }, throwOnError: true })) })
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
        await unwrap(mailRuleApply({ path: { rule_id: applying.id }, body: { expected_version: applying.version, confirm_apply: true }, throwOnError: true }))
        setApplying(undefined); refresh()
      }
      if (action === 'delete' && deleting?.id && deleting.version) {
        await unwrap(mailRuleDelete({ path: { rule_id: deleting.id }, query: { expected_version: deleting.version }, throwOnError: true }))
        setDeleting(undefined); refresh()
      }
    } catch { setError(true) } finally { setBusy(false) }
  }
  const rows = destinations.data ?? []
  const choose = (key: 'account_id' | 'folder_id' | 'label_id', label: string, options: { id?: string; name: string }[]) => <label className="le-label">{label}<select className="le-input" value={draft[key] ?? ''} onChange={event => update({ [key]: event.target.value || null })}><option value="">{key === 'account_id' ? t.allAccounts : t.none}</option>{options.filter(row => row.id).map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
  return <section className="le-prompts"><h2 className="heading">{t.rules}</h2><p>{t.ruleDelay}</p>
    <form className="le-filing-form" onSubmit={event => { event.preventDefault(); void execute('save') }}>
      <TextField label={t.ruleName} value={draft.name} onChange={event => update({ name: event.target.value })} maxLength={120} required />
      {choose('account_id', t.account, (accounts.data ?? []).map(account => ({ id: account.id, name: account.display_name || account.email_address })))}
      {(['sender_address', 'sender_domain', 'subject_contains', 'recipient'] as const).map((key, index) => <TextField key={key} label={[t.sender, t.domain, t.subject, t.recipient][index]!} value={draft[key] ?? ''} onChange={event => update({ [key]: event.target.value })} />)}
      {choose('folder_id', t.folder, rows.filter(row => row.kind === 'folder'))}{choose('label_id', t.label, rows.filter(row => row.kind === 'label'))}
      <label><input type="checkbox" checked={draft.mark_read ?? false} onChange={event => update({ mark_read: event.target.checked, mark_unread: false })} /> {t.markRead}</label>
      <label><input type="checkbox" checked={draft.mark_unread ?? false} onChange={event => update({ mark_unread: event.target.checked, mark_read: false })} /> {t.markUnread}</label>
      <label><input type="checkbox" checked={draft.archive ?? false} onChange={event => update({ archive: event.target.checked })} /> {t.archive}</label>
      <label><input type="checkbox" checked={draft.is_active ?? true} onChange={event => update({ is_active: event.target.checked })} /> {t.active}</label>
      <TextField type="number" label={t.priority} value={draft.position ?? 0} onChange={event => update({ position: Number(event.target.value) })} />
      <Button type="submit" variant="primary" disabled={busy || !draft.name.trim()}>{editing ? t.save : t.addRule}</Button>
      <Button type="button" variant="secondary" disabled={busy || !draft.name.trim()} onClick={() => { void execute('preview') }}>{t.preview}</Button>
      {editing && <Button type="button" variant="secondary" onClick={() => { setEditing(undefined); setDraft({ name: '', is_active: true }) }}>{t.cancel}</Button>}
      {matched !== undefined && <p role="status">{t.matches(matched)}</p>}
    </form>
    {rules.isPending && <p role="status">{t.loading}</p>}{(error || rules.isError || destinations.isError || accounts.isError) && <Notice tone="danger">{t.error}</Notice>}
    {rules.data?.length === 0 && <p>{t.ruleEmpty}</p>}
    {rules.data?.map(row => <div className="le-prompt-row le-filing-row" key={row.id}><strong>{row.name}</strong>
      <Button variant="secondary" onClick={() => { setEditing(row); setDraft({ name: row.name, account_id: row.account_id, sender_address: row.sender_address, sender_domain: row.sender_domain, subject_contains: row.subject_contains, recipient: row.recipient, folder_id: row.folder_id, label_id: row.label_id, mark_read: row.mark_read, mark_unread: row.mark_unread, archive: row.archive, position: row.position, is_active: row.is_active }) }}>{t.editRule}</Button>
      <Button variant="secondary" onClick={() => setApplying(row)}>{t.apply}</Button><Button variant="secondary" onClick={() => setDeleting(row)}>{t.deleteRule}</Button>
      <Button variant="secondary" onClick={() => setShowRuns(showRuns === row.id ? undefined : row.id)}>{t.outcomes}</Button>
      {showRuns === row.id && runs.data?.map(run => <p key={run.id}>{run.status === 'unknown' ? t.unknown : run.status === 'skipped_stale' ? t.stale : run.status === 'skipped_conflict' ? t.conflict : run.status !== 'done' ? t.pending : run.provider_results_json?.includes('skipped_unsupported') ? t.skipped : t.completed}</p>)}
    </div>)}
    {applying && <PreferenceDialog title={t.applyTitle} close={() => { if (!busy) setApplying(undefined) }}><p>{applying.name}. {t.applyDescription}</p><div className="le-dialog-actions"><Button variant="secondary" onClick={() => setApplying(undefined)}>{t.cancel}</Button><Button variant="primary" disabled={busy} onClick={() => { void execute('apply') }}>{t.apply}</Button></div></PreferenceDialog>}
    {deleting && <PreferenceDialog title={t.deleteRule} close={() => { if (!busy) setDeleting(undefined) }}><p>{deleting.name}</p><div className="le-dialog-actions"><Button variant="secondary" onClick={() => setDeleting(undefined)}>{t.cancel}</Button><Button variant="danger" disabled={busy} onClick={() => { void execute('delete') }}>{t.remove}</Button></div></PreferenceDialog>}
  </section>
}

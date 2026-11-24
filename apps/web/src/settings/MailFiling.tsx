// i18n-exempt: API keys and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { mailFolderCreate, mailFolderDelete, mailFolderUpdate, mailRulesList, mailRuleCreate, mailRuleCreateFromMessage, mailRuleUpdate, mailRuleDelete, mailRulesPreview, mailRuleApply, mailRuleRunsList } from '../api/generated/sdk.gen'
import type { ApplyOut, MailFolder, MailRule, RuleIn } from '../api/generated/types.gen'
import { errorCode, unwrap } from '../api/unwrap'
import { accountsQuery, messageQuery, MessageNotFoundError } from '../api/queries'
import { folderInventoryQuery, foldersQuery } from './mailQueries'
import { Button, Chip, Icon, Menu, Notice, TextField } from '../ds'
import { useI18n } from '../i18n/I18n'
import { PreferenceDialog } from './Preferences'
import { SettingsSaveScope, useSettingsSlice } from './SettingsSave'
import { sortDestinations, sortNamed } from '../mail/filingOrder'

const destinationColors = ['none', 'moss', 'olive', 'teal', 'lake', 'denim', 'indigo', 'plum', 'berry'] as const satisfies readonly NonNullable<MailFolder['color']>[]

export function FolderSettings() {
  const { texts, locale } = useI18n(), t = texts.mailFiling
  const client = useQueryClient(), query = useQuery(folderInventoryQuery())
  const [name, setName] = useState(''), [kind, setKind] = useState<'folder' | 'label'>('folder')
  const [color, setColor] = useState<NonNullable<MailFolder['color']>>('none')
  const [search, setSearch] = useState(''), [filter, setFilter] = useState<'all' | 'folder' | 'label'>('all'), [page, setPage] = useState(0)
  const [editing, setEditing] = useState<MailFolder>(), [deleting, setDeleting] = useState<MailFolder>()
  const [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const refresh = () => { void client.invalidateQueries({ queryKey: ['mail-folders'] }); void client.invalidateQueries({ queryKey: ['mail-rules'] }); void client.invalidateQueries({ queryKey: ['messages'] }); void client.invalidateQueries({ queryKey: ['mail-quick-actions'] }) }
  const save = async () => {
    if (busy || !name.trim()) return
    setBusy(true); setError(false)
    try {
      if (editing?.id) await unwrap(mailFolderUpdate({ path: { destination_id: editing.id }, body: { name, color }, throwOnError: true }))
      else await unwrap(mailFolderCreate({ body: { name, kind, color }, throwOnError: true }))
      setName(''); setColor('none'); setEditing(undefined); refresh()
    } catch { setError(true) } finally { setBusy(false) }
  }
  const remove = async () => {
    if (!deleting?.id || busy) return
    setBusy(true); setError(false)
    try { await unwrap(mailFolderDelete({ path: { destination_id: deleting.id }, query: { confirm_delete: true }, throwOnError: true })); setDeleting(undefined); refresh() }
    catch { setError(true) } finally { setBusy(false) }
  }
  const fold = (value: string) => value.normalize('NFD').replace(/\p{Diacritic}/gu, '').toLocaleLowerCase(locale)
  const matches = sortDestinations(query.data ?? [], locale).filter(row => (filter === 'all' || row.kind === filter) && fold(row.name).includes(fold(search.trim())))
  const totalPages = Math.max(1, Math.ceil(matches.length / 20))
  const currentPage = Math.min(page, totalPages - 1)
  const visible = matches.slice(currentPage * 20, (currentPage + 1) * 20)
  useEffect(() => { setPage(previous => Math.min(previous, totalPages - 1)) }, [totalPages])
  return <section className="le-prompts"><h2 className="heading">{t.folders}</h2><p>{t.localOnly}</p>
    <form className="le-filing-form" onSubmit={event => { event.preventDefault(); void save() }}>
      <TextField label={t.name} value={name} maxLength={120} onChange={event => setName(event.target.value)} required />
      {!editing && <div className="le-field"><span className="le-label">{t.kind}</span><Menu trigger={props => <Chip {...props} quiet aria-label={`${t.kind}: ${kind === 'folder' ? t.folder : t.label}`}>{kind === 'folder' ? t.folder : t.label}<Icon name="chevron-down" size="sm" /></Chip>} items={(['folder', 'label'] as const).map(value => ({ label: value === 'folder' ? t.folder : t.label, checked: kind === value, onSelect: () => setKind(value) }))} /></div>}
      <div className="le-field"><span className="le-label">{t.color}</span><Menu trigger={props => <Chip {...props} quiet aria-label={`${t.color}: ${t.colors[color]}`}><span aria-hidden="true" className={`le-filing-swatch${color === 'none' ? '' : ` le-destination-${color}`}`} />{t.colors[color]}<Icon name="chevron-down" size="sm" /></Chip>} items={destinationColors.map(value => ({ label: t.colors[value], leading: <span aria-hidden="true" className={`le-filing-swatch${value === 'none' ? '' : ` le-destination-${value}`}`} />, checked: color === value, onSelect: () => setColor(value) }))} /></div>
      <div className="le-prompt-actions"><Button type="submit" variant="primary" disabled={busy || !name.trim()}>{editing ? t.save : t.add}</Button>
      {editing && <Button type="button" variant="secondary" onClick={() => { setEditing(undefined); setName(''); setColor('none') }}>{t.cancel}</Button>}</div>
    </form>
    {query.isPending && <p role="status">{t.loading}</p>}
    {(query.isError || error) && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { setError(false); void query.refetch() } }}>{t.error}</Notice>}
    {query.data?.length === 0 && <p>{t.empty}</p>}
    {query.data && query.data.length > 0 && <><div className="le-filing-form"><TextField type="search" label={t.searchDestinations} value={search} onChange={event => { setSearch(event.target.value); setPage(0) }} />
      {search && <Button variant="secondary" onClick={() => { setSearch(''); setPage(0) }}>{t.clearSearch}</Button>}
      <Menu trigger={props => <Chip {...props} quiet>{filter === 'folder' ? t.foldersOnly : filter === 'label' ? t.labelsOnly : t.allKinds}<Icon name="chevron-down" size="sm" /></Chip>} items={([
        ['all', t.allKinds], ['folder', t.foldersOnly], ['label', t.labelsOnly],
      ] as const).map(([value, label]) => ({ label, checked: filter === value, onSelect: () => { setFilter(value); setPage(0) } }))} /></div>
      {!matches.length && <p>{t.noMatches}</p>}</>}
    {visible.map(row => <div className="le-prompt-row le-filing-row" key={row.id}><div className="le-filing-row-info"><span className="le-filing-row-name" title={row.name}><Icon name={row.kind === 'folder' ? 'folder' : 'tag'} size="sm" /><strong>{row.name}</strong></span><small><span>{row.kind === 'folder' ? t.folder : t.label} ·</span><span aria-hidden="true" className={`le-filing-swatch${row.color && row.color !== 'none' ? ` le-destination-${row.color}` : ''}`} /><span>{t.colors[row.color ?? 'none']}</span></small></div>
      <div className="le-filing-row-actions"><Button variant="secondary" onClick={() => { setEditing(row); setName(row.name); setColor(row.color ?? 'none') }}>{t.rename}</Button>
      <Button variant="secondary" aria-label={t.removeName(row.name)} onClick={() => setDeleting(row)}>{t.remove}</Button></div></div>)}
    {matches.length > 0 && <div className="le-prompt-actions"><Button variant="secondary" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>{t.previousPage}</Button><span aria-live="polite">{t.page(currentPage + 1, totalPages)}</span><Button variant="secondary" disabled={currentPage + 1 >= totalPages} onClick={() => setPage(currentPage + 1)}>{t.nextPage}</Button></div>}
    {deleting && <PreferenceDialog title={t.removeTitle} close={() => { if (!busy) setDeleting(undefined) }}><p>{deleting.name}. {deleting.kind === 'label' ? t.labelRemoveDescription : t.removeDescription}</p><div className="le-dialog-actions"><Button variant="secondary" disabled={busy} onClick={() => setDeleting(undefined)}>{t.cancel}</Button><Button variant="danger" disabled={busy} onClick={() => { void remove() }}>{t.remove}</Button></div></PreferenceDialog>}
  </section>
}

type RuleSettingsProps = { sourceMessageId?: string; onSourceUsed?: () => void }
export function RuleSettings(props: RuleSettingsProps) {
  return <SettingsSaveScope><RuleSettingsForm {...props} /></SettingsSaveScope>
}

function RuleSettingsForm({ sourceMessageId, onSourceUsed }: RuleSettingsProps) {
  const { texts, locale } = useI18n(), t = texts.mailFiling
  const client = useQueryClient(), destinations = useQuery(foldersQuery()), accounts = useQuery(accountsQuery())
  const rules = useQuery({ queryKey: ['mail-rules'], queryFn: () => unwrap(mailRulesList({ throwOnError: true })) })
  const [draft, setDraft] = useState<RuleIn>({ name: '', is_active: true })
  const [sourceActive, setSourceActive] = useState(Boolean(sourceMessageId))
  const [sourceMissing, setSourceMissing] = useState(false)
  const [sourceLoadFailed, setSourceLoadFailed] = useState(false)
  const draftTouched = useRef(false)
  const [sourceEnabled, setSourceEnabled] = useState({ sender_address: true, sender_domain: false, subject_contains: false })
  const targetHintId = useId(), labelHintId = useId()
  const sourceLoaded = useRef<string | undefined>(undefined)
  const [sourceReadyId, setSourceReadyId] = useState<string>()
  const formRef = useRef<HTMLFormElement>(null)
  useEffect(() => { if (sourceMessageId) formRef.current?.scrollIntoView?.({ block: 'start' }) }, [sourceMessageId])
  const source = useQuery({ ...messageQuery(sourceMessageId ?? ''), enabled: Boolean(sourceMessageId && sourceActive), retry: false })
  const [editing, setEditing] = useState<MailRule>(), [applying, setApplying] = useState<MailRule>(), [deleting, setDeleting] = useState<MailRule>()
  const [showRuns, setShowRuns] = useState<string>(), [matched, setMatched] = useState<{ matched: number; already_filed: number }>()
  const [queued, setQueued] = useState<ApplyOut>()
  const [applyConflict, setApplyConflict] = useState(false)
  const [inactiveError, setInactiveError] = useState(false)
  const [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const executing = useRef(false)
  useEffect(() => {
    if (!sourceActive || !sourceMessageId || sourceLoaded.current === sourceMessageId) return
    if (source.isError || source.data?.local_reference_only) {
      sourceLoaded.current = sourceMessageId
      setSourceReadyId(sourceMessageId)
      setSourceLoadFailed(true); setSourceActive(false); onSourceUsed?.()
      return
    }
    if (!source.data) return
    sourceLoaded.current = sourceMessageId
    setSourceReadyId(sourceMessageId)
    if (editing || draftTouched.current) return
    const address = source.data.sender_address.trim(), domain = address.includes('@') ? address.slice(address.lastIndexOf('@') + 1) : ''
    setDraft({ name: (source.data.sender_name || address).slice(0, 120), is_active: true, account_id: source.data.account_id, sender_address: address, sender_domain: domain, subject_contains: source.data.subject.slice(0, 200) })
  }, [sourceActive, sourceMessageId, source.data, source.isError, editing, onSourceUsed])
  const sourceInitializing = Boolean(sourceActive && sourceMessageId && !source.isError && sourceReadyId !== sourceMessageId)
  const runs = useQuery({ queryKey: ['mail-rule-runs', showRuns], enabled: Boolean(showRuns), refetchInterval: showRuns ? 3000 : false, queryFn: () => unwrap(mailRuleRunsList({ path: { rule_id: showRuns! }, throwOnError: true })) })
  const applyPreview = useQuery({
    queryKey: ['mail-rule-apply-preview', applying?.id, applying?.version], enabled: Boolean(applying?.id && applying.version), retry: false, staleTime: 0,
    queryFn: () => unwrap(mailRulesPreview({ body: { name: applying!.name, account_id: applying!.account_id,
      sender_address: applying!.sender_address, sender_domain: applying!.sender_domain, subject_contains: applying!.subject_contains,
      recipient: applying!.recipient, folder_id: applying!.folder_id, label_ids: applying!.label_ids,
      mark_read: applying!.mark_read, mark_unread: applying!.mark_unread, archive: applying!.archive,
      position: applying!.position, is_active: applying!.is_active, saved_rule_id: applying!.id, expected_version: applying!.version }, throwOnError: true })),
  })
  const refresh = () => { void client.invalidateQueries({ queryKey: ['mail-rules'] }); void client.invalidateQueries({ queryKey: ['messages'] }); void client.invalidateQueries({ queryKey: ['mail-rule-runs'] }) }
  const update = (part: Partial<RuleIn>) => { draftTouched.current = true; setDraft(old => ({ ...old, ...part })); setMatched(undefined) }
  const prepared = (): RuleIn => sourceActive && !editing ? { ...draft,
    sender_address: sourceEnabled.sender_address ? draft.sender_address : '', sender_domain: sourceEnabled.sender_domain ? draft.sender_domain : '', subject_contains: sourceEnabled.subject_contains ? draft.subject_contains : '',
  } : draft
  const execute = async (action: 'save' | 'preview' | 'apply' | 'delete') => {
    if (executing.current || sourceInitializing) return
    if (action === 'save' && !(draft.folder_id || draft.label_ids?.length)) return
    if (action === 'save' && sourceActive && !editing && (!source.data || source.data.local_reference_only || sourceMissing || !sourceAccountAvailable)) return
    executing.current = true; setBusy(true); setError(false); setInactiveError(false)
    try {
      if (action === 'preview') setMatched(await unwrap(mailRulesPreview({ body: prepared(), throwOnError: true })))
      if (action === 'save') {
        if (editing?.id && editing.version) await unwrap(mailRuleUpdate({ path: { rule_id: editing.id }, body: { ...draft, expected_version: editing.version }, throwOnError: true }))
        else if (sourceActive && sourceMessageId && source.data && !source.data.local_reference_only) await unwrap(mailRuleCreateFromMessage({ body: { ...prepared(), source_message_id: sourceMessageId, source_account_id: source.data.account_id }, throwOnError: true }))
        else if (!sourceActive) await unwrap(mailRuleCreate({ body: prepared(), throwOnError: true }))
        else throw new Error('Source unavailable')
        setDraft({ name: '', is_active: true }); setEditing(undefined); setMatched(undefined); setSourceActive(false); onSourceUsed?.(); refresh()
      }
      if (action === 'apply' && applying?.id && applying.version) {
        const result = await unwrap(mailRuleApply({ path: { rule_id: applying.id }, body: { expected_version: applying.version, confirm_apply: true, preview_message_ids: applyPreview.data?.message_ids }, throwOnError: true }))
        setQueued(result)
        setApplying(undefined); refresh()
      }
      if (action === 'delete' && deleting?.id && deleting.version) {
        await unwrap(mailRuleDelete({ path: { rule_id: deleting.id }, query: { expected_version: deleting.version }, throwOnError: true }))
        setDeleting(undefined); refresh()
      }
    } catch (failure) { if (sourceActive && errorCode(failure) === 'message_not_found') setSourceMissing(true); else if ((action === 'apply' || action === 'preview') && errorCode(failure) === 'rule_inactive') { setApplying(undefined); setInactiveError(true); refresh() } else if (action === 'apply' && errorCode(failure) === 'rule_version_conflict') { setApplying(undefined); setApplyConflict(true); refresh() } else setError(true) } finally { executing.current = false; setBusy(false) }
  }
  const discard = () => { setDraft({ name: '', is_active: true }); setEditing(undefined); setMatched(undefined); setSourceActive(false); setSourceMissing(false); onSourceUsed?.() }
  const dirty = Boolean(editing || draft.name || draft.account_id || draft.sender_address || draft.sender_domain || draft.subject_contains || draft.recipient || draft.folder_id || draft.label_ids?.length || draft.mark_read || draft.mark_unread || draft.archive || draft.position)
  const enabledDraft = prepared()
  const sourceAccountAvailable = Boolean(source.data && accounts.data?.some(account => account.id === source.data?.account_id))
  useSettingsSlice('mail-rule-form', { dirty, hideBar: true, canSave: Boolean(draft.name.trim() && (enabledDraft.account_id || enabledDraft.sender_address || enabledDraft.sender_domain || enabledDraft.subject_contains || enabledDraft.recipient) && (draft.folder_id || draft.label_ids?.length) && (!sourceActive || editing || source.data && !source.data.local_reference_only && !sourceMissing && sourceAccountAvailable)), busy, save: () => execute('save'), discard })
  const rows = sortDestinations(destinations.data ?? [], locale)
  const choose = (key: 'account_id' | 'folder_id', label: string, options: { id?: string; name: string }[]) => {
    const selected = options.find(row => row.id === draft[key])?.name ?? (key === 'account_id' ? t.allAccounts : t.none)
    return <div className="le-field"><span className="le-label">{label}</span><Menu trigger={props => <Chip {...props} quiet aria-label={`${label}: ${selected}`}>{selected}<Icon name="chevron-down" size="sm" /></Chip>} items={[
      { label: key === 'account_id' ? t.allAccounts : t.none, checked: !draft[key], onSelect: () => update({ [key]: null }) },
      ...options.filter(row => row.id).map(row => ({ label: row.name, checked: row.id === draft[key], onSelect: () => update({ [key]: row.id }) })),
    ]} /></div>
  }
  const ruleSummary = (row: MailRule) => {
    const account = (accounts.data ?? []).find(item => item.id === row.account_id)
    const folder = rows.find(item => item.id === row.folder_id), labels = rows.filter(item => row.label_ids?.includes(item.id ?? ''))
    const conditions = [row.account_id && `${t.account}: ${account?.display_name || account?.email_address || t.none}`, row.sender_address && `${t.sender}: ${row.sender_address}`, row.sender_domain && `${t.domain}: ${row.sender_domain}`, row.subject_contains && `${t.subject}: ${row.subject_contains}`, row.recipient && `${t.recipient}: ${row.recipient}`].filter(Boolean)
    const actions = [folder && `${t.folder}: ${folder.name}`, ...labels.map(label => `${t.label}:\u00a0${label.name}`),
      row.mark_read && t.markRead, row.mark_unread && t.markUnread, row.archive && t.archive].filter(Boolean)
    return <div className="le-filing-summary"><small>{row.is_active ? t.enabled : t.disabled}</small><small>{t.priority}: {row.position ?? 0}</small><small>{t.conditions}: {conditions.join(' · ')}</small>{actions.length > 0 && <small>{t.targets}: {actions.join(' · ')}</small>}</div>
  }
  const providerOutcomes = (value: string | undefined) => {
    try {
      const parsed: unknown = JSON.parse(value || '{}')
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return []
      return Object.entries(parsed).flatMap(([kind, status]) => {
        const label = kind === 'mark_read' ? t.markRead : kind === 'mark_unread' ? t.markUnread : kind === 'archive' ? t.archive : null
        if (label && status === 'skipped_permission') return [`${label}: ${texts.errors.provider_permission_missing}`]
        if (label && (status === 'reconciled' || status === 'reconciled_permission' || status === 'partial' || status === 'partial_permission')) {
          const detail = status === 'reconciled' || status === 'reconciled_permission' ? texts.inbox.providerReconciled : texts.errors.provider_partial
          const reason = status.endsWith('_permission') ? ` ${texts.errors.provider_permission_missing}` : ''
          return [`${label}: ${detail}${reason}`]
        }
        return label && (status === 'skipped_unsupported' || status === 'unknown') ? [`${label}: ${status === 'unknown' ? t.unknown : t.skipped}`] : []
      })
    } catch { return [] }
  }
  const runAccount = (accountId: string) => {
    const account = (accounts.data ?? []).find(item => item.id === accountId)
    return account?.display_name || account?.email_address || t.none
  }
  return <section className="le-prompts"><h2 className="heading">{t.rules}</h2><p>{t.ruleDelay}</p>
    <form ref={formRef} className="le-filing-form" onSubmit={event => { event.preventDefault(); void execute('save') }}>
      {sourceActive && !editing && <p>{t.sourceRuleHint}</p>}
      {sourceInitializing && <><p role="status">{t.loading}</p><Button type="button" variant="secondary" onClick={discard}>{t.cancel}</Button></>}
      {sourceLoadFailed && <Notice tone="info">{t.sourceLoadManual}</Notice>}
      {sourceActive && !editing && (sourceMissing || source.error instanceof MessageNotFoundError || source.data?.local_reference_only) && <Notice tone="danger">{t.sourceGone}</Notice>}
      {sourceActive && !editing && source.isError && !(source.error instanceof MessageNotFoundError) && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void source.refetch() } }}>{t.error}</Notice>}
      {sourceActive && !editing && source.data && accounts.isSuccess && !sourceAccountAvailable && <Notice tone="danger">{t.sourceAccountGone}</Notice>}
      <fieldset className="le-filing-fields" disabled={sourceInitializing}>
      <TextField label={t.ruleName} value={draft.name} onChange={event => update({ name: event.target.value })} maxLength={120} required />
      {choose('account_id', t.account, (accounts.data ?? []).map(account => ({ id: account.id, name: account.display_name || account.email_address })))}
      {(['sender_address', 'sender_domain', 'subject_contains', 'recipient'] as const).map((key, index) => <div key={key}><TextField label={[t.sender, t.domain, t.subject, t.recipient][index]!} value={draft[key] ?? ''} onChange={event => update({ [key]: event.target.value })} />{sourceActive && !editing && key !== 'recipient' && <Button type="button" variant={sourceEnabled[key] ? 'primary' : 'secondary'} role="switch" aria-label={t.useCondition([t.sender, t.domain, t.subject][index]!)} aria-checked={sourceEnabled[key]} onClick={() => { setSourceEnabled(old => ({ ...old, [key]: !old[key] })); setMatched(undefined) }}>{sourceEnabled[key] ? t.on : t.off}</Button>}</div>)}
      {choose('folder_id', t.folder, rows.filter(row => row.kind === 'folder'))}
      <fieldset className="le-field le-rule-labels" aria-describedby={labelHintId}><legend className="le-label">{t.ruleLabels}</legend><span id={labelHintId} className="le-hint">{t.ruleLabelsHint}</span>{rows.filter(row => row.kind === 'label' && row.id).map(row => <label key={row.id}><input className="le-check" type="checkbox" checked={draft.label_ids?.includes(row.id!) ?? false} disabled={!draft.label_ids?.includes(row.id!) && (draft.label_ids?.length ?? 0) >= 20} onChange={event => update({ label_ids: event.target.checked ? [...(draft.label_ids ?? []), row.id!] : (draft.label_ids ?? []).filter(id => id !== row.id) })} /><span>{row.name}</span></label>)}</fieldset>
      <div className="le-prompt-actions">{([['mark_read', t.markRead], ['mark_unread', t.markUnread], ['archive', t.archive], ['is_active', t.active]] as const).map(([key, label]) => {
        const checked = key === 'is_active' ? draft.is_active ?? true : draft[key] ?? false
        return <Button key={key} type="button" variant={checked ? 'primary' : 'secondary'} role="switch" aria-checked={checked} onClick={() => update(key === 'mark_read' ? { mark_read: !checked, mark_unread: false } : key === 'mark_unread' ? { mark_unread: !checked, mark_read: false } : { [key]: !checked })}>{label} · {checked ? t.on : t.off}</Button>
      })}</div>
      <TextField type="number" label={t.priority} value={draft.position ?? 0} onChange={event => update({ position: Number(event.target.value) })} />
      <div className="le-prompt-actions"><Button type="submit" variant="primary" aria-describedby={!draft.folder_id && !draft.label_ids?.length ? targetHintId : undefined} disabled={busy || !draft.name.trim() || !(draft.folder_id || draft.label_ids?.length) || Boolean(sourceActive && !editing && (!source.data || source.data.local_reference_only || sourceMissing || !sourceAccountAvailable))}>{editing ? t.save : t.addRule}</Button>
      <Button type="button" variant="secondary" disabled={busy || !draft.name.trim()} onClick={() => { void execute('preview') }}>{t.preview}</Button>
      {(editing || sourceActive) && !sourceInitializing && <Button type="button" variant="secondary" onClick={discard}>{t.cancel}</Button>}</div>
      {!draft.folder_id && !draft.label_ids?.length && <p id={targetHintId} className="le-hint">{editing ? t.ruleTargetRequiredEdit : t.ruleTargetRequired}</p>}
      {matched !== undefined && <div role="status"><p>{t.matches(matched.matched)}</p>{matched.already_filed > 0 && <p>{t.alreadyFiledMatches(matched.already_filed)}</p>}</div>}
      </fieldset>
    </form>
    {queued !== undefined && <p role="status">{t.applyResult(queued.app_changed, queued.provider_skipped, queued.provider_queued, queued.conflicts)}</p>}
    {applyConflict && <Notice tone="danger">{t.applyConflict}</Notice>}
    {inactiveError && <Notice tone="danger">{t.ruleInactive}</Notice>}
    {rules.isPending && <p role="status">{t.loading}</p>}{(error || rules.isError || destinations.isError || accounts.isError) && <Notice tone="danger">{t.error}</Notice>}
    {rules.data?.length === 0 && <p>{t.ruleEmpty}</p>}
    {sortNamed(rules.data ?? [], locale).map(row => <div className="le-prompt-row le-filing-row" key={row.id}><strong>{row.name}</strong>{ruleSummary(row)}
      <Button variant="secondary" disabled={sourceInitializing} onClick={() => { draftTouched.current = true; setEditing(row); setDraft({ name: row.name, account_id: row.account_id, sender_address: row.sender_address, sender_domain: row.sender_domain, subject_contains: row.subject_contains, recipient: row.recipient, folder_id: row.folder_id, label_ids: row.label_ids ?? [], mark_read: row.mark_read, mark_unread: row.mark_unread, archive: row.archive, position: row.position, is_active: row.is_active }) }}>{t.editRule}</Button>
      <Button variant="secondary" disabled={row.is_active === false} aria-describedby={row.is_active === false ? `rule-inactive-${row.id}` : undefined} onClick={() => { setApplyConflict(false); setInactiveError(false); setApplying(row) }}>{t.apply}</Button>{row.is_active === false && <small id={`rule-inactive-${row.id}`}>{t.ruleInactive}</small>}<Button variant="secondary" onClick={() => setDeleting(row)}>{t.deleteRule}</Button>
      <Button variant="secondary" onClick={() => setShowRuns(showRuns === row.id ? undefined : row.id)}>{t.outcomes}</Button>
      {showRuns === row.id && runs.isPending && <p role="status">{t.loading}</p>}
      {showRuns === row.id && runs.isError && <p role="alert">{t.error}</p>}
      {showRuns === row.id && runs.data?.length === 0 && <p>{t.noRuns}</p>}
      {showRuns === row.id && runs.data?.map(run => <div key={run.id} className="le-filing-summary"><p>{run.status === 'unknown' ? t.unknown : run.status === 'skipped_stale' ? t.stale : run.status === 'skipped_conflict' ? t.conflict : run.status === 'skipped_imap_rebuild' ? `${t.imapRebuild} ${t.account}: ${runAccount(run.account_id)}` : run.status === 'skipped_initial_import' ? `${t.initialImport} ${t.account}: ${runAccount(run.account_id)}` : run.status !== 'done' ? t.pending : run.provider_results_json?.includes('overridden') ? t.folderOverridden : (run.provider_results_json?.includes('skipped_unsupported') || run.provider_results_json?.includes('skipped_permission')) ? t.skipped : /"(?:reconciled|partial)(?:_permission)?"/u.test(run.provider_results_json ?? '') ? t.providerIncomplete : t.completed}</p>
        {run.status !== 'done' && run.provider_results_json?.includes('overridden') && <small>{t.folderOverridden}</small>}
        {['done', 'local_done', 'unknown'].includes(run.status ?? '') && (!run.provider_results_json?.includes('overridden') || row.label_ids?.length) && <small>{t.localDone}</small>}
        {providerOutcomes(run.provider_results_json).map(detail => <small key={detail}>{detail}</small>)}</div>)}
    </div>)}
    {applying && <PreferenceDialog title={t.applyTitle} close={() => { if (!busy) setApplying(undefined) }}><p>{applying.name}. {t.applyDescription}</p>{applyPreview.isPending && <p role="status">{t.loading}</p>}{applyPreview.isError && <Notice tone="danger">{errorCode(applyPreview.error) === 'rule_inactive' ? t.ruleInactive : t.applyConflict}</Notice>}{!applyPreview.isError && applyPreview.data && <p>{t.matches(applyPreview.data.matched)} {t.alreadyFiledMatches(applyPreview.data.already_filed)}</p>}<div className="le-dialog-actions"><Button variant="secondary" onClick={() => setApplying(undefined)}>{t.cancel}</Button><Button variant="primary" disabled={busy || applyPreview.isError || !applyPreview.data || applyPreview.isFetching} onClick={() => { void execute('apply') }}>{t.apply}</Button></div></PreferenceDialog>}
    {deleting && <PreferenceDialog title={t.deleteRule} close={() => { if (!busy) setDeleting(undefined) }}><p>{deleting.name}</p><div className="le-dialog-actions"><Button variant="secondary" onClick={() => setDeleting(undefined)}>{t.cancel}</Button><Button variant="danger" disabled={busy} onClick={() => { void execute('delete') }}>{t.remove}</Button></div></PreferenceDialog>}
  </section>
}

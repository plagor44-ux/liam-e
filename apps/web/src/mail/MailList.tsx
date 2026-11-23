import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query'
import { useLocation, useNavigate, useParams, useRouter, useSearch } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'
import { mailSyncAll } from '../api/generated/sdk.gen'
import type { MailActionCapabilities, MessageListItemOut, MessagePageOut, UiSettingsOut } from '../api/generated/types.gen'
import { messagesInfiniteQuery, summaryQuery } from '../api/queries'
import { Button, Chip, Icon, IconButton, MailRow, Menu, Notice, TextField, Tooltip } from '../ds'
import { providerLabel } from '../ds/providerLabel'
import { useI18n } from '../i18n/I18n'
import { formatTime } from '../i18n/format'
import { useBreakpoint, useCoarsePointer } from '../shell/useBreakpoint'
import { useSwipeSettings } from '../settings/useSwipeSettings'
import { hasActiveFilters, toMessageFilters, type InboxSearch } from './filters'
import { groupByDay } from './groups'
import { useInboxKeys } from './useInboxKeys'
import { ActionToast } from './ActionToast'
import { useMailActionsContext } from './useMailActions'

export function MailList({ onOpen }: { onOpen(id: string): void }) {
  const { texts, locale } = useI18n()
  const search = useSearch({ strict: false }) as InboxSearch
  const { messageId } = useParams({ strict: false }) as { messageId?: string }
  const location = useLocation()
  const navigate = useNavigate()
  const router = useRouter()
  const client = useQueryClient()
  const breakpoint = useBreakpoint(), coarse = useCoarsePointer()
  const summary = useQuery(summaryQuery())
  const messages = useInfiniteQuery(messagesInfiniteQuery(toMessageFilters(search)))
  const { run, runBulk, notify, checkCapacity, capacityAvailable, isBusy } = useMailActionsContext()
  const swipeSettings = useSwipeSettings()
  const [localSelected, setLocalSelected] = useState<string | undefined>()
  const [selectionMode, setSelectionMode] = useState(false)
  const [selection, setSelection] = useState<Set<string>>(() => new Set())
  const anchor = useRef<string | undefined>(undefined)
  const [searchOpen, setSearchOpen] = useState(Boolean(search.q))
  const [value, setValue] = useState(search.q ?? '')
  const [syncing, setSyncing] = useState(false)
  const searchRef = useRef<HTMLDivElement>(null), sentinel = useRef<HTMLDivElement>(null)
  const rowRefs = useRef(new Map<string, HTMLLIElement>())
  const scrollRef = useRef<HTMLDivElement>(null)
  // When a removed row held the focus, it moves to the neighbouring row, else to the list.
  const focusAfter = useRef<{ removed: string; target?: string } | null>(null)
  const selected = messageId ?? localSelected
  const items = messages.data?.pages.flatMap(page => page.items) ?? []
  const selectedIndex = items.findIndex(item => item.id === selected)
  const itemIds = items.map(item => item.id).join('\u0000')
  const filterKey = JSON.stringify(toMessageFilters(search))
  const endSelection = () => { setSelectionMode(false); setSelection(new Set()); anchor.current = undefined }
  useEffect(() => { endSelection() }, [filterKey])
  useEffect(() => {
    const present = new Set(itemIds ? itemIds.split('\u0000') : [])
    setSelection(old => { const next = new Set([...old].filter(id => present.has(id))); return next.size === old.size ? old : next })
    if (anchor.current && !present.has(anchor.current)) anchor.current = undefined
  }, [itemIds])
  const toggleSelection = (id: string, shift = false) => {
    setSelectionMode(true)
    const next = new Set(selection)
    const from = items.findIndex(item => item.id === anchor.current), to = items.findIndex(item => item.id === id)
    const ids = shift && from >= 0 && to >= 0 ? items.slice(Math.min(from, to), Math.max(from, to) + 1).map(item => item.id) : [id]
    const add = shift || !next.has(id)
    for (const candidate of ids) {
      if (!add) next.delete(candidate)
      else if (next.size < 100) next.add(candidate)
    }
    if (add && ids.some(candidate => !next.has(candidate))) notify(texts.inbox.selectLimit)
    setSelection(next)
    anchor.current = id
  }
  const accounts = summary.data?.accounts ?? []
  const capabilities = new Map(accounts.map(account => [account.id, account.action_capabilities]))
  type ListAction = 'archive' | 'trash' | 'spam' | 'mark_read' | 'mark_unread'
  const supports = (mail: MessageListItemOut, kind: keyof MailActionCapabilities) => capabilities.get(mail.account_id)?.[kind] !== false
  const allow = (mail: MessageListItemOut, kind: ListAction) => {
    if (supports(mail, kind)) return true
    notify(texts.errors.provider_action_unsupported)
    return false
  }
  const selectedMails = items.filter(item => selection.has(item.id))
  const bulkBlocked = (kind: ListAction) => selectedMails.some(mail => !supports(mail, kind))
  const bulkRead = !selectedMails.length || selectedMails.some(item => !item.is_read) ? 'mark_read' : 'mark_unread'
  const bulk = (kind: ListAction) => {
    if (bulkBlocked(kind)) { notify(texts.inbox.unsupportedSelection); return }
    if (!selectedMails.length || !checkCapacity()) return
    void runBulk(selectedMails.map(item => item.id), kind)
    endSelection()
  }
  const changeSearch = (change: Partial<InboxSearch>) => { void navigate({ to: '.', search: previous => ({ ...previous, ...change }), replace: true }) }

  useEffect(() => { setValue(search.q ?? ''); if (search.q) setSearchOpen(true) }, [search.q])
  useEffect(() => {
    const trimmed = value.trim() || undefined
    if (trimmed === search.q) return
    const id = setTimeout(() => changeSearch({ q: trimmed }), 300)
    return () => clearTimeout(id)
  }, [value, search.q])
  useEffect(() => { if (searchOpen) searchRef.current?.querySelector('input')?.focus() }, [searchOpen])
  useEffect(() => { if (selected) rowRefs.current.get(selected)?.scrollIntoView?.({ block: 'nearest' }) }, [selected])
  useEffect(() => {
    const pending = focusAfter.current
    if (!pending || items.some(item => item.id === pending.removed)) return
    focusAfter.current = null
    const link = pending.target ? rowRefs.current.get(pending.target)?.querySelector<HTMLElement>('.le-mailrow-link') : null
    ;(link ?? scrollRef.current)?.focus()
  }, [items])
  useEffect(() => {
    const node = sentinel.current
    if (!node || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting) && messages.hasNextPage && !messages.isFetchingNextPage) void messages.fetchNextPage()
    }, { rootMargin: '200px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [messages.hasNextPage, messages.isFetchingNextPage, messages.fetchNextPage])

  const remove = (mail: MessageListItemOut, kind: 'archive' | 'trash') => {
    if (!allow(mail, kind) || !checkCapacity()) return
    const active = typeof document === 'undefined' ? null : document.activeElement
    if (active && rowRefs.current.get(mail.id)?.contains(active)) {
      const index = items.findIndex(item => item.id === mail.id)
      focusAfter.current = { removed: mail.id, target: items[index + 1]?.id ?? items[index - 1]?.id }
    }
    const moved = selected === mail.id
    const next = items[selectedIndex + 1]?.id ?? items[selectedIndex - 1]?.id
    if (moved) {
      setLocalSelected(next)
      if (messageId) {
        if (next) onOpen(next)
        else void navigate({ href: `/mail${location.searchStr}` })
      }
    }
    void run(mail.id, kind).then(restored => {
      if (!restored || !moved) return
      if (!messageId) setLocalSelected(current => current === next ? mail.id : current)
      else {
        const destination = next ? `/mail/${encodeURIComponent(next)}` : '/mail'
        if (router.state.location.pathname === destination) void navigate({ href: `/mail/${encodeURIComponent(mail.id)}${router.state.location.searchStr}` })
      }
    })
  }
  const toggleRead = (mail: MessageListItemOut) => {
    const kind = mail.is_read ? 'mark_unread' : 'mark_read'
    if (allow(mail, kind)) void run(mail.id, kind)
  }
  const current = items[selectedIndex]
  const select = (id: string | undefined) => {
    if (messageId && id) onOpen(id)
    else if (!messageId) setLocalSelected(id)
  }
  useInboxKeys({
    onNext: () => select(items[Math.min(selectedIndex + 1, items.length - 1)]?.id),
    onPrev: () => select(items[Math.max(selectedIndex - 1, 0)]?.id),
    onOpen: () => { if (current) { if (selectionMode) toggleSelection(current.id); else onOpen(current.id) } },
    onArchive: () => { if (selectionMode) bulk('archive'); else if (current) remove(current, 'archive') },
    onToggleRead: () => { if (selectionMode) bulk(bulkRead); else if (current) toggleRead(current) },
    onTrash: () => { if (selectionMode) bulk('trash'); else if (current) remove(current, 'trash') },
    onSelect: () => { if (current) toggleSelection(current.id) },
    // Only claim Escape while selecting; otherwise the browser keeps its own Escape behaviour.
    onEscape: selectionMode ? endSelection : undefined,
  })
  const refresh = async () => {
    if (syncing) return
    setSyncing(true)
    try {
      const result = await mailSyncAll({ body: {} })
      if (result.error || !result.response?.ok) throw new Error('sync failed')
      void client.invalidateQueries({ queryKey: ['summary'] }); void client.invalidateQueries({ queryKey: ['messages'] })
    } catch { notify(texts.inbox.refreshError) } finally { setSyncing(false) }
  }
  const controlSize = breakpoint === 'mobile' || coarse ? 'lg' : 'md'
  const selectionBlocked = bulkBlocked('archive') || bulkBlocked('trash') || bulkBlocked('spam') || bulkBlocked(bulkRead)
  const selectionBar = <div className={`le-inbox-selection${selectionBlocked ? ' le-inbox-selection--limited' : ''}`}>
    <IconButton icon="x" label={texts.inbox.endSelection} tip={texts.inbox.endSelectionTip} size={controlSize} onClick={endSelection} />
    <span className="le-inbox-selection-count" role="status">{texts.inbox.selectionCount(selection.size)}</span>
    <Chip style={breakpoint === 'mobile' ? { minHeight: 'var(--control-height-lg)' } : undefined} onClick={() => { setSelection(new Set(items.slice(0, 100).map(item => item.id))); if (items.length > 100) notify(texts.inbox.selectLimit) }}>{texts.inbox.selectAll}</Chip>
    <div className="le-inbox-selection-actions">
      <IconButton icon="archive" label={texts.controls.archive} tip={texts.inbox.bulkTips.archive} size={controlSize} disabled={!selection.size || bulkBlocked('archive')} onClick={() => bulk('archive')} />
      <IconButton icon="mail-open" label={bulkRead === 'mark_read' ? texts.controls.markRead : texts.controls.markUnread} tip={texts.inbox.bulkTips[bulkRead]} size={controlSize} disabled={!selection.size || bulkBlocked(bulkRead)} onClick={() => bulk(bulkRead)} />
      <IconButton icon="trash-2" label={texts.controls.trash} tip={texts.inbox.bulkTips.trash} tone="danger" size={controlSize} disabled={!selection.size || bulkBlocked('trash')} onClick={() => bulk('trash')} />
      <IconButton icon="ban" label={texts.mail.spam} tip={texts.inbox.bulkTips.spam} size={controlSize} disabled={!selection.size || bulkBlocked('spam')} onClick={() => bulk('spam')} />
    </div>
  </div>
  const buttons = <>
    {breakpoint === 'mobile'
      ? <Tooltip content={texts.inbox.selectTip}><Button className="le-inbox-control" variant="secondary" icon="list-checks" onClick={() => setSelectionMode(true)}>{texts.inbox.startSelection}</Button></Tooltip>
      : <IconButton className="le-inbox-control" icon="list-checks" label={texts.inbox.startSelection} tip={texts.inbox.selectTip} size={controlSize} onClick={() => setSelectionMode(true)} />}
    <IconButton className="le-inbox-control" icon="search" label={texts.inbox.search} tip={texts.inbox.searchTip} size={controlSize} onClick={() => { setSearchOpen(true); searchRef.current?.querySelector('input')?.focus() }} />
    <IconButton className="le-inbox-control" icon="refresh-cw" label={texts.inbox.refresh} tip={texts.inbox.refreshTip} size={controlSize} disabled={syncing} onClick={() => { void refresh() }} />
  </>
  const time = summary.data?.last_sync_at ? formatTime(new Date(summary.data.last_sync_at), undefined, locale) : undefined
  const selectedAccount = accounts.find(account => account.id === search.account?.[0])
  const accountLabel = selectedAccount ? selectedAccount.display_name || selectedAccount.email_address : texts.controls.allAccounts
  const labels = new Map(accounts.map(account => [account.id, providerLabel(account)]))
  const groups = groupByDay(items, new Date(), undefined, locale)
  return <>
    {breakpoint !== 'mobile' && (selectionMode ? selectionBar : <div className="le-topbar"><h1 className="le-crumb">{texts.inbox.title}</h1>{time && <small className="le-muted le-num">{texts.inbox.stand(time)}</small>}<span className="le-grow" />{buttons}</div>)}
    <div className="le-inbox-filters"><Menu trigger={props => <Chip {...props} quiet>{accountLabel}<Icon name="chevron-down" size="sm" /></Chip>} items={[{ label: texts.controls.allAccounts, onSelect: () => changeSearch({ account: undefined }) }, ...accounts.map(account => ({ label: account.display_name || account.email_address, onSelect: () => changeSearch({ account: [account.id] }) }))]} />
      {(['unread', 'important', 'attachments'] as const).map(key => <Chip key={key} pressed={Boolean(search[key])} quiet={!search[key]} onClick={() => changeSearch({ [key]: search[key] ? undefined : true })}>{texts.inbox[key]}</Chip>)}
    </div>
    {breakpoint === 'mobile' && (selectionMode ? selectionBar : <div className="le-inbox-mobile-status"><small className="le-muted le-num">{texts.inbox.unreadStand(summary.data?.unread_total ?? 0, time)}</small><span className="le-grow" />{buttons}</div>)}
    {selectionMode && selectionBlocked && <Notice tone="info">{texts.inbox.unsupportedSelection}</Notice>}
    {swipeSettings.status === 'error' && (coarse || breakpoint === 'mobile') && <Notice tone="info" action={{ label: texts.chat.retry, onClick: swipeSettings.retry }}>{texts.inbox.swipeUnavailable}</Notice>}
    {searchOpen && <div className="le-inbox-search" ref={searchRef}><TextField label={texts.inbox.search} type="search" placeholder={texts.inbox.searchPlaceholder} value={value} onChange={event => setValue(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') { setValue(''); setSearchOpen(false); changeSearch({ q: undefined }) } }} /></div>}
    <div className="le-inbox-scroll" ref={scrollRef} tabIndex={-1}>
      {messages.isPending && <><span className="le-sr" role="status">{texts.inbox.loading}</span><div aria-hidden="true">{Array.from({ length: 6 }, (_, i) => <div key={i} className="le-mailrow le-mailrow--placeholder"><span /><span /></div>)}</div></>}
      {messages.isError && !messages.data && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void messages.refetch() } }}>{texts.inbox.loadError}</Notice>}
      {messages.data && !items.length && <p className="le-hint">{hasActiveFilters(search) ? texts.inbox.emptyFiltered : texts.inbox.empty}</p>}
      {groups.map(group => <div key={group.label}><div className="le-side-label">{group.label}</div><ul className="le-maillist" aria-label={group.label}>{group.items.map(mail => <li key={mail.id} ref={node => { if (node) rowRefs.current.set(mail.id, node); else rowRefs.current.delete(mail.id) }}><MailRow mail={mail} actionCapabilities={capabilities.get(mail.account_id)} selected={selected === mail.id} selectionMode={selectionMode} checked={selection.has(mail.id)} onSelect={shift => toggleSelection(mail.id, shift)} coarse={coarse} onAsk={breakpoint === 'mobile' || coarse ? () => { void navigate({ to: '/', search: { about: mail.id } }) } : undefined} accountLabel={labels.get(mail.account_id)} onOpen={() => { setLocalSelected(mail.id); onOpen(mail.id) }} onArchive={() => remove(mail, 'archive')} onToggleRead={() => toggleRead(mail)} onTrash={() => remove(mail, 'trash')} swipe={{ basis: swipeSettings.assignment && swipeSettings.revision !== undefined ? { id: mail.id, isRead: mail.is_read, revision: swipeSettings.revision, ...swipeSettings.assignment } : undefined, enabled: !selectionMode && swipeSettings.status === 'ready' && (swipeSettings.assignment?.left !== 'none' || swipeSettings.assignment?.right !== 'none') && capacityAvailable && !isBusy(mail.id), navigation: location.href, valid: basis => { const settings = client.getQueryData<UiSettingsOut>(['ui-settings']); const data = client.getQueryData<InfiniteData<MessagePageOut>>(messagesInfiniteQuery(toMessageFilters(search)).queryKey); const currentMail = data?.pages.flatMap(page => page.items).find(item => item.id === basis.id); return client.getQueryState(['ui-settings'])?.status === 'success' && settings?.revision === basis.revision && settings.swipe_left === basis.left && settings.swipe_right === basis.right && currentMail?.is_read === basis.isRead && !isBusy(basis.id) && checkCapacity() }, run: action => { if (isBusy(mail.id) || !checkCapacity()) return; if (action === 'archive' || action === 'trash') remove(mail, action); else if (allow(mail, action)) void run(mail.id, action) } }} /></li>)}</ul></div>)}
      <div ref={sentinel} />{messages.isFetchingNextPage && <p className="le-hint">{texts.inbox.loadingMore}</p>}
    </div>
    <ActionToast />
  </>
}

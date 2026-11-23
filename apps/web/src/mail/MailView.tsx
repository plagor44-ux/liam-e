import { useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query'
import { useNavigate, useRouter, useSearch } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'
import type { MessagePageOut } from '../api/generated/types.gen'
import { MessageNotFoundError, messageQuery, messagesInfiniteQuery, summaryQuery } from '../api/queries'
import { useChatSession } from '../chat/session'
import { Avatar, Button, Icon, IconButton, Menu, Notice, Tag, Tooltip } from '../ds'
import { providerLabel } from '../ds/providerLabel'
import { useI18n } from '../i18n/I18n'
import { formatBytes, formatMailTime } from '../i18n/format'
import { useBreakpoint, useCoarsePointer } from '../shell/useBreakpoint'
import { toMessageFilters, type InboxSearch } from './filters'
import { InsightBox } from './InsightBox'
import { OriginalDialog } from './OriginalDialog'
import { ActionToast } from './ActionToast'
import { MailHtml } from './MailHtml'
import { useMailActionsContext } from './useMailActions'
import { FilingMenu } from './FilingMenu'
import { FilingTags } from './FilingTags'
import { foldersQuery } from '../settings/mailQueries'
import { shortcutHint } from './inboxShortcuts'

export function MailView({ id }: { id: string }) {
  const { texts, locale } = useI18n()
  const client = useQueryClient(), router = useRouter(), navigate = useNavigate()
  const search = useSearch({ strict: false }) as InboxSearch
  const breakpoint = useBreakpoint(), coarse = useCoarsePointer()
  const split = breakpoint === 'desktop' || breakpoint === 'wide'
  const message = useQuery({ ...messageQuery(id), retry: (count, error) => !(error instanceof MessageNotFoundError) && count < 2 })
  const summary = useQuery(summaryQuery())
  const destinations = useQuery(foldersQuery())
  const { run, checkCapacity, notify } = useMailActionsContext()
  const { state, send, retry, retryWithoutPreferences } = useChatSession()
  const [started, setStarted] = useState(false)
  const [original, setOriginal] = useState<string | null>(null)
  // The dialog belongs to the mail it was opened for: moving to another mail (browser back) closes it.
  if (original !== null && original !== id) setOriginal(null)
  const opened = useRef(new Set<string>())
  const busy = state.phase === 'working' || state.phase === 'streaming'
  const data = message.data
  const account = summary.data?.accounts.find(item => item.id === data?.account_id)
  const capabilities = account?.action_capabilities
  const readKind = data?.is_read ? 'mark_unread' : 'mark_read'
  const hint = texts.errors.provider_action_unsupported
  useEffect(() => {
    if (!data || !summary.data || data.local_reference_only || opened.current.has(id)) return
    opened.current.add(id)
    if (!data.is_read && capabilities?.mark_read !== false) void run(id, 'mark_read', { silent: true })
  }, [id, data, summary.data, capabilities, run])
  useEffect(() => {
    if (started && state.conversationId) {
      setStarted(false)
      void navigate({ to: '/chat/$conversationId', params: { conversationId: state.conversationId } })
    }
  }, [started, state.conversationId, navigate])
  const back = () => { void navigate({ to: '/mail', search }) }
  const start = (text: string) => {
    if (busy || !data) return
    setStarted(true)
    // Mail reader turns name their task access: a choice left on the start page belongs to that composer.
    void send({ text, context_message_id: id, account_id: data.account_id, task_source: 'none' })
  }
  const remove = (kind: 'archive' | 'trash' | 'spam') => {
    if (capabilities?.[kind] === false) { notify(hint); return }
    if (!checkCapacity()) return
    const list = client.getQueryData<InfiniteData<MessagePageOut>>(messagesInfiniteQuery(toMessageFilters(search)).queryKey)
    const items = list?.pages.flatMap(page => page.items) ?? []
    const index = items.findIndex(item => item.id === id)
    const next = split && index >= 0 ? items[index + 1]?.id ?? items[index - 1]?.id : undefined
    const destination = next ? `/mail/${encodeURIComponent(next)}` : '/mail'
    if (next) void navigate({ to: '/mail/$messageId', params: { messageId: next }, search })
    else back()
    void run(id, kind).then(restored => {
      if (split && restored && router.state.location.pathname === destination) void navigate({ to: '/mail/$messageId', params: { messageId: id }, search: true })
    })
  }
  const ask = () => { void navigate({ to: '/', search: { about: id } }) }
  const proposeTask = () => {
    if (busy || !data) return
    setStarted(true)
    void send({ text: texts.taskChat.prompt, context_message_id: id, account_id: data.account_id,
      task_source: 'account', task_source_account_id: data.account_id })
  }
  const iconSize = !split || coarse ? 'lg' : 'md'
  const actions = data && !data.local_reference_only && <>
    <FilingMenu ids={[id]} messages={[data]} compact={!split} showShortcuts={split} />
    <IconButton label={texts.controls.archive} icon="archive" size={iconSize} shortcut={split ? shortcutHint('archive', locale) : undefined} disabled={capabilities?.archive === false} tip={capabilities?.archive === false ? hint : undefined} onClick={() => remove('archive')} />
    <IconButton label={data.is_read ? texts.controls.markUnread : texts.controls.markRead} icon="mail-open" size={iconSize} shortcut={split ? shortcutHint('read', locale) : undefined} disabled={capabilities?.[readKind] === false} tip={capabilities?.[readKind] === false ? hint : undefined} onClick={() => { void run(id, readKind) }} />
    <IconButton label={texts.controls.trash} icon="trash-2" size={iconSize} tone="danger" shortcut={split ? shortcutHint('trash', locale) : undefined} disabled={capabilities?.trash === false} tip={capabilities?.trash === false ? hint : undefined} onClick={() => remove('trash')} />
    <Menu placement={split ? 'down-start' : 'down-end'} trigger={props => <IconButton {...props} label={texts.mail.more} icon="more-horizontal" size={iconSize} disabled={busy} />} items={[{ label: texts.taskChat.ask, icon: 'check', onSelect: proposeTask }, { label: texts.mail.forward, icon: 'forward', onSelect: () => start(texts.mail.forwardPrompt) }, { label: texts.mail.unsubscribe, icon: 'newspaper', disabled: !data.can_unsubscribe, hint: data.can_unsubscribe ? undefined : texts.mail.unsubscribeUnavailable, onSelect: () => start(texts.mail.unsubscribePrompt) }, { label: texts.mail.spam, icon: 'ban', disabled: capabilities?.spam === false, hint: capabilities?.spam === false ? hint : undefined, onSelect: () => remove('spam') }, { label: texts.mail.original, icon: 'file-code', disabled: !data.can_show_original, hint: data.can_show_original ? undefined : texts.mail.originalUnavailable, onSelect: () => setOriginal(id) }]} />
  </>
  const chatButtons = data && !data.local_reference_only && <>
    <Tooltip content={texts.mail.askTip}><Button variant="secondary" icon="message-circle" className={split ? 'le-btn--sm' : ''} disabled={busy} onClick={ask}>{texts.mail.askInChat}</Button></Tooltip>
    <Tooltip content={texts.mail.draftTip}><Button variant="primary" icon="pencil" className={split ? 'le-btn--sm' : ''} disabled={busy} onClick={() => start(texts.mail.draftPrompt)}>{texts.mail.draftReply}</Button></Tooltip>
  </>
  return <div className="le-mail-view">
    <div className="le-topbar le-mail-topbar">{!split && <IconButton label={texts.mail.back} icon="arrow-left" size="lg" onClick={back} />}{!split && <span className="le-grow" />}{actions}{split && <><span className="le-grow" />{data && chatButtons}</>}</div>
    <div className="le-mail-scroll">
      {data && !data.local_reference_only && capabilities && Object.values(capabilities).every(supported => !supported) && <Notice tone="info">{texts.mail.studioGmailActionsUnavailable}</Notice>}
      {started && state.phase === 'error' && !state.conversationId && <Notice tone="danger" action={state.errorCode === 'preferences_unavailable' ? { label: texts.preferences.continueWithout, onClick: () => { void retryWithoutPreferences() } } : state.errorCode === 'chat_account_not_granted' ? { label: texts.chatAccounts.section, onClick: () => { void navigate({ to: '/settings/chat' }) } } : { label: texts.chat.retry, onClick: () => { void retry() } }}>{texts.errors[state.errorCode ?? 'internal']}</Notice>}
      {message.isPending && <><span className="le-sr" role="status">{texts.mail.loading}</span><div className="le-mail-placeholder" aria-hidden="true"><div className="le-mail-placeholder-head"><span /><div><span /><span /></div></div><span /><span /><span /><span /></div></>}
      {message.isError && <div className="le-mail-error">{message.error instanceof MessageNotFoundError ? <Notice tone="danger" action={{ label: texts.mail.toInbox, onClick: back }}>{texts.proposalErrors.item_gone}</Notice> : <><Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void message.refetch() } }}>{texts.mail.loadError}</Notice><Button variant="secondary" className="le-mail-to-inbox" onClick={back}>{texts.mail.toInbox}</Button></>}</div>}
      {data && <article className="le-mail-article"><h1 className="title">{texts.mail.subjectText(data.subject)}</h1><div className="le-mail-header"><Avatar name={data.sender_name || data.sender_address} address={data.sender_address} colorIndex={data.color_index} size="lg" /><div className="le-mail-sender"><div className="body-strong">{data.sender_name || data.sender_address}</div>{breakpoint !== 'mobile' && <div className="small le-muted le-mail-address">{data.to.length ? texts.mail.addressTo(data.sender_address, data.to.join(', ')) : data.sender_address}</div>}</div>{account && <Tag tone="on-surface" dot={data.color_index}>{providerLabel(account)}</Tag>}{data.internal_date && <time className="small le-muted le-num" dateTime={data.internal_date}>{formatMailTime(new Date(data.internal_date), new Date(), undefined, locale)}</time>}</div>
        <FilingTags folderId={data.app_folder_id} labelIds={data.app_label_ids} destinations={destinations.data ?? []} />
        {data.local_reference_only ? <Notice tone="info">{texts.mail.localReference}</Notice> : <InsightBox id={id} onFollowup={start} />}
        <MailHtml key={id} id={id} text={data.body_text} localOnly={data.local_reference_only ?? false} />
        {data.attachments.length > 0 && <div className="le-mail-attachments">{data.attachments.map((attachment, index) => <span key={index} className="le-mail-attachment"><Icon name="paperclip" size="sm" /><span className="le-mail-attachment-name">{attachment.name}</span><span className="le-mail-attachment-size">{formatBytes(attachment.size, locale)}</span></span>)}</div>}
      </article>}
    </div>
    {original === id && data?.can_show_original && <OriginalDialog id={id} onClose={() => setOriginal(null)} />}
    {!split && <ActionToast inline />}
    {!split && chatButtons && <div className="le-mail-bottom">{chatButtons}</div>}
  </div>
}

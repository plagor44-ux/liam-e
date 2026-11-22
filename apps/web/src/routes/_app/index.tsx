import { oldSource } from '../../calendar/state'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Link, redirect, useNavigate, useSearch } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { calendarSettingsQuery, chatCalendarSourcesQuery, chatMailReferenceQuery, MessageNotFoundError, modelsQuery, skillsQuery, summaryQuery } from '../../api/queries'
import { CalendarReference, readableCalendar } from '../../chat/CalendarContext'
import { ChatComposer } from '../../chat/Conversation'
import { ModelMenu } from '../../chat/ModelMenu'
import { ChatHeader } from '../../chat/ChatHeader'
import { composerDraftKey, useChatSession, useTaskScope } from '../../chat/session'
import { SKILL_REFUSALS } from '../../chat/transport'
import { Button as ActionButton, IconButton, MailReference, Notice, QuickAction } from '../../ds'
import { providerLabel } from '../../ds/providerLabel'
import { useI18n } from '../../i18n/I18n'
import { formatTime } from '../../i18n/format'
export const Route = createFileRoute('/_app/')({
  staticData: { landing: true },
  validateSearch: (search: Record<string, unknown>): { about?: string; calendarAbout?: string; chatAccount?: string } => ({ chatAccount: typeof search.chatAccount === 'string' ? search.chatAccount : undefined, about: typeof search.about === 'string' ? search.about : undefined, calendarAbout: typeof search.calendarAbout === 'string' ? search.calendarAbout : undefined }),
  beforeLoad: ({ context }) => {
    const to = context.landingPath()
    if (to !== undefined && to !== '' && to !== '/') {
      // oxlint-disable-next-line typescript/only-throw-error -- TanStack Router's redirect is thrown, by design
      throw redirect({ href: to })
    }
  },
  component: Greeting,
})
function Greeting() {
  const { texts, locale } = useI18n()
  const client = useQueryClient()
  const { state, selectedSkills, send, retry, retryWithoutPreferences, audioDraftLocked } = useChatSession()
  const navigate = useNavigate()
  const search = useSearch({ strict: false })
  const calendarAbout = typeof search.calendarAbout === 'string' ? search.calendarAbout : undefined
  const about = typeof search.about === 'string' ? search.about : undefined
  const tasks = useTaskScope()
  const summary = useQuery(summaryQuery(true))
  const calendars = useQuery(chatCalendarSourcesQuery())
  const calendarSettings = useQuery({ ...calendarSettingsQuery(), enabled: !about })
  const reference = useQuery(chatMailReferenceQuery(about ?? '', summary.data?.mail_scope_version))
  const [started, setStarted] = useState(false)
  const [voiceActive, setVoiceActive] = useState(false)
  // The model chosen in the chip rides along with the first message and becomes the model of the new conversation.
  const [model, setModel] = useState<string>()
  const [revision, setRevision] = useState<string | null>(null)
  const data = summary.data
  const grantedAccounts = data?.accounts ?? []
  const focusedId = !about ? search.chatAccount : undefined
  const focusedAccount = focusedId ? grantedAccounts.find(item => item.id === focusedId) : undefined
  const invalidFocusedAccount = Boolean(focusedId && summary.isSuccess && !focusedAccount)
  const effectiveAccounts = focusedId ? (focusedAccount ? [focusedAccount] : []) : grantedAccounts
  const mail = reference.isSuccess ? reference.data : undefined
  const activeSkill = selectedSkills[composerDraftKey(undefined, mail?.id)] ?? null
  const account = mail && data?.accounts.find(item => item.id === mail.account_id)
  const mailScope = about ? mail?.account_id : focusedAccount?.id
  const taskScopeValid = (!tasks.scope.account_id || grantedAccounts.some(item => item.id === tasks.scope.account_id)) &&
    (tasks.scope.source !== 'account_and_unlinked' || Boolean(mailScope && mailScope === tasks.scope.account_id))
  const { scope: rawTaskScope, set: setTaskScope } = tasks
  useEffect(() => {
    if (summary.isSuccess && (!about || mail) && !taskScopeValid && rawTaskScope.source !== 'none') {
      setTaskScope({ source: 'none' })
    }
  }, [summary.isSuccess, about, mail, taskScopeValid, rawTaskScope.source, setTaskScope])
  const effectiveTaskScope = summary.isSuccess && (!about || mail) && !taskScopeValid ? { source: 'none' as const } : rawTaskScope
  const busy = state.phase === 'working' || state.phase === 'streaming'
  const quickSend = (text: string) => {
    if (busy || audioDraftLocked) return
    setStarted(true)
    void send({
      text,
      account_id: about ? mail?.account_id : focusedAccount?.id,
      context_message_id: mail?.id,
      context_occurrence_id: calendarAbout,
      model,
      prompt_revision_id: revision,
      ...(activeSkill ? { skill_id: activeSkill.skill.id, skill_revision_id: activeSkill.skill.revision_id } : {}),
      task_source: effectiveTaskScope.source,
      task_source_account_id: effectiveTaskScope.account_id,
    })
  }
  const modelGone = started && state.phase === 'error' && !state.conversationId && state.errorCode === 'model_unknown'
  const skillGone = started && state.phase === 'error' && !state.conversationId && Boolean(state.errorCode && SKILL_REFUSALS.has(state.errorCode))
  useEffect(() => {
    if (modelGone) {
      setModel(undefined)
      void client.invalidateQueries({ queryKey: modelsQuery().queryKey })
    }
    if (skillGone) {
      void client.invalidateQueries({ queryKey: skillsQuery().queryKey })
    }
  }, [modelGone, skillGone, client])
  useEffect(() => {
    if (started && !voiceActive && state.conversationId) {
      setStarted(false)
      void navigate({ to: '/chat/$conversationId', params: { conversationId: state.conversationId } })
    }
  }, [started, voiceActive, state.conversationId, navigate])
  const readable = calendars.data?.filter(readableCalendar) ?? []
  const dueSources = [
    ...(effectiveAccounts.length ? [texts.calendarSettings.deadlines] : []),
    ...(readable.length ? [texts.calendarSettings.events] : []),
    ...(effectiveTaskScope.source !== 'none' ? [texts.calendarSettings.tasks] : []),
  ]
  const accountLabel = (item: typeof effectiveAccounts[number]) => item.display_name || item.email_address || providerLabel(item)
  const unreachableAccounts = effectiveAccounts.filter(item => item.state === 'auth_expired' || item.state === 'error')
  const incompleteAccounts = effectiveAccounts.filter(item => item.state === 'never' || item.state === 'syncing')
  const allAccountsUnreachable = effectiveAccounts.length > 0 && unreachableAccounts.length === effectiveAccounts.length
  const unavailableCalendars = calendars.data?.filter(source => !readableCalendar(source)) ?? []
  const pausedCalendars = unavailableCalendars.filter(source => source.chat_access === 'paused')
  const disconnectedCalendars = unavailableCalendars.filter(source => source.chat_access === 'disconnected')
  const unreadableCalendars = unavailableCalendars.filter(source => source.chat_access === 'unreadable')
  const incompleteCalendars = readable.filter(source => source.stale || source.incomplete || oldSource(source) || Boolean(source.last_error))
  const erroredReadableCalendars = readable.filter(source => Boolean(source.last_error))
  const defaultId = calendarSettings.data?.default_source_id ?? null
  const defaultSource = defaultId ? calendars.data?.find(source => source.id === defaultId) : undefined
  const hasWritableCalendar = calendars.data?.some(source => source.chat_access === 'writable') ?? false
  const hasDueCaveats = unreachableAccounts.length > 0 || incompleteAccounts.length > 0 || unavailableCalendars.length > 0 || incompleteCalendars.length > 0
  const turnError = started && state.phase === 'error' && !state.conversationId
  const resetFocus = () => { void navigate({ to: '/', search: { about, calendarAbout } }) }
  return <div className={about ? 'le-chat-view le-chat-view--mail-question' : 'le-chat-view'}>
    <ChatHeader modelMenu={<ModelMenu disabled={audioDraftLocked} model={model} onChange={setModel} />} />
    {about && <h1 className="le-sr">{texts.mail.askInChat}</h1>}
    {(!about || turnError || summary.isError) && <div className="le-chat-messages le-chat-thread le-chat-start-scroll"><div className="le-chat-start">
      {!about && <h1 className="display le-greeting">{texts.chat.greetingFirst}<br /><span>{texts.chat.greetingSecond}</span></h1>}
      {turnError && <Notice tone="danger" action={state.errorCode === 'preferences_unavailable' ? { label: texts.preferences.continueWithout, onClick: () => { void retryWithoutPreferences() } } : modelGone || skillGone ? undefined : state.errorCode === 'chat_account_not_granted' && search.chatAccount ? { label: texts.shell.newConversation, onClick: resetFocus } : { label: texts.chat.retry, onClick: () => { void retry() } }}>{texts.errors[state.errorCode ?? 'internal']}</Notice>}
      {invalidFocusedAccount && <Notice tone="info" action={{ label: texts.shell.newConversation, onClick: resetFocus }}>{texts.errors.chat_account_not_granted}</Notice>}
      {summary.isPending && <p className="le-hint">{texts.chat.loadingSummary}</p>}
      {summary.isError && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void summary.refetch() } }}>{texts.chat.summaryError}</Notice>}
      {!about && data && (data.accounts.length === 0
        ? <><Notice tone="info">{texts.chatAccounts.empty}</Notice><ActionButton variant="primary" onClick={() => { void navigate({ to: '/settings/chat' }) }}>{texts.chatAccounts.section}</ActionButton></>
        : <p className="le-hint">{texts.chat.stand(data.unread_total, data.accounts.length, data.last_sync_at ? formatTime(new Date(data.last_sync_at), undefined, locale) : undefined)}</p>)}
      {!about && unreachableAccounts.length > 0 && <Notice tone="danger" action={{ label: texts.shell.accounts, onClick: () => { void navigate({ to: '/accounts' }) } }}>{allAccountsUnreachable ? texts.chat.allAccountsUnreachable(unreachableAccounts.map(accountLabel).join(', '), unreachableAccounts.length) : texts.errors.accountUnavailable(unreachableAccounts.map(accountLabel).join(', '))}</Notice>}
      {!about && incompleteAccounts.length > 0 && <p className="le-hint">{texts.chat.mailIncomplete(incompleteAccounts.map(accountLabel).join(', '), incompleteAccounts.length)}</p>}
      {started && busy && <div className="le-working" role="status">{texts.status.thinking}</div>}
      {!about && (data || calendars.isSuccess) && <div className="le-chat-quick-actions">
        {!!effectiveAccounts.length && <>
          <QuickAction icon="mail-open" title={texts.quickActions.unread.title} sub={texts.quickActions.unread.subtitle(data?.unread_total ?? 0)} onClick={() => quickSend(texts.quickActions.unread.prompt)} />
          <QuickAction icon="flag" title={texts.quickActions.priority.title} sub={texts.quickActions.priority.subtitle} onClick={() => quickSend(texts.quickActions.priority.prompt)} />
          <QuickAction icon="newspaper" title={texts.quickActions.newsletters.title} sub={texts.quickActions.newsletters.subtitle(data?.newsletter_senders ?? 0)} onClick={() => quickSend(texts.quickActions.newsletters.prompt)} />
        </>}
        {dueSources.length > 0 && <QuickAction icon="clock" title={texts.quickActions.dueToday.title} sub={dueSources.join(' · ')} tip={texts.quickActions.dueTodayTip(dueSources.join(' · '), hasDueCaveats, effectiveTaskScope.source === 'none')} onClick={() => quickSend(texts.quickActions.dueToday.prompt)} />}
      </div>}
      {!about && calendars.isError && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void calendars.refetch() } }}>{texts.calendar.error}</Notice>}
      {!about && calendars.isSuccess && !calendars.data.length && <p className="le-hint">{texts.calendarSettings.empty}</p>}
      {!about && unavailableCalendars.length > 0 && <p className="le-hint">{texts.calendarSettings.unavailable}</p>}
      {!about && pausedCalendars.length > 0 && <p className="le-hint">{texts.calendarSettings.pausedNames(pausedCalendars.map(source => source.name).join(', '))}</p>}
      {!about && disconnectedCalendars.length > 0 && <Notice tone="danger" action={{ label: texts.calendar.manage, onClick: () => { void navigate({ to: '/accounts' }) } }}>{texts.calendarSettings.unreachable(disconnectedCalendars.map(source => source.name).join(', '))}</Notice>}
      {!about && unreadableCalendars.length > 0 && <p className="le-hint">{texts.calendarSettings.unreadableNames(unreadableCalendars.map(source => source.name).join(', '))}</p>}
      {!about && incompleteCalendars.length > 0 && <p className="le-hint">{texts.calendarSettings.incomplete}</p>}
      {!about && erroredReadableCalendars.length > 0 && <p className="le-hint">{texts.calendarSettings.refreshFailed(erroredReadableCalendars.map(source => source.name).join(', '))}</p>}
      {!about && calendars.isSuccess && calendars.data.length > 0 && calendarSettings.isSuccess && (
        !defaultId
          ? <p className="le-hint">{hasWritableCalendar ? <>{texts.calendarSettings.noDefault} <Link className="le-link" to="/settings/calendar">{texts.calendarSettings.title}</Link></> : texts.calendarSettings.noWritable}</p>
          : !defaultSource
            ? <p className="le-hint">{texts.calendarSettings.missing} <Link className="le-link" to="/settings/calendar">{texts.calendarSettings.title}</Link></p>
            : defaultSource.chat_access !== 'writable'
              ? <p className="le-hint">{texts.calendarSettings.defaultUnavailable(defaultSource.name, texts.calendarSettings.reason[defaultSource.chat_access ?? 'unreadable'])} <Link className="le-link" to="/settings/calendar">{texts.calendarSettings.title}</Link></p>
              : null
      )}
      {!about && (data || calendars.isSuccess) && effectiveTaskScope.source === 'none' && <p className="le-hint">{texts.taskChat.excludedHint}</p>}
    </div></div>}
    {(about || data || calendars.data?.some(source => source.is_active) || calendarAbout) && <div className="le-chat-composer">
      {calendarAbout && <div className="le-chat-reference"><CalendarReference occurrenceId={calendarAbout} /><IconButton icon="x" label={texts.calendar.removeReference} onClick={() => { void navigate({ to: '/', search: { about } }) }} /></div>}
      {about && <div className="le-chat-reference">
        {reference.isPending && <span className="le-hint">{texts.chat.loadingReference}</span>}
        {reference.error instanceof MessageNotFoundError && <Notice tone="info">{texts.errors.chat_account_not_granted}</Notice>}
        {reference.isError && !(reference.error instanceof MessageNotFoundError) && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void reference.refetch() } }}>{texts.chat.referenceError}</Notice>}
        {mail && data && !account && <Notice tone="info">{texts.errors.chat_account_not_granted}</Notice>}
        {mail && account && <MailReference compact mail={mail} accountLabel={account && providerLabel(account)} onOpen={() => { void navigate({ to: '/mail/$messageId', params: { messageId: mail.id } }) }} />}
        <IconButton icon="x" label={texts.chat.removeReference} onClick={() => { void navigate({ to: '/', search: { about: undefined } }) }} />
      </div>}
      {(!about || (mail && account)) && <ChatComposer key={about} autoFocus={Boolean(about)} contextMail={mail} contextOccurrenceId={calendarAbout} model={model} promptRevisionId={revision} onPromptRevisionChange={setRevision} onSend={() => setStarted(true)} onVoiceActiveChange={setVoiceActive} />}
    </div>}
  </div>
}

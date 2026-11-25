import { useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { chatCalendarSourcesQuery, chatMailReferenceQuery, historyQuery, modelsQuery, skillsQuery, summaryQuery } from '../api/queries'
import { errorCode } from '../api/unwrap'
import type { AccountSummaryOut, ChatMessageOut, MailRefOut } from '../api/generated/types.gen'
import { Button, ChatMessage, Chip, Composer, MailReference, Notice, Tag } from '../ds'
import { providerLabel } from '../ds/providerLabel'
import { useI18n } from '../i18n/I18n'
import { ModelMenu } from './ModelMenu'
import { ActiveTemplateBanner, useTemplateSelection } from '../prompts/TemplateMenu'
import { ChatHeader } from './ChatHeader'
import { ProposalCard } from './ProposalCard'
import { useFollowScroll } from './useFollowScroll'
import { SafeMarkdown } from './SafeMarkdown'
import { CalendarReference, calendarScopeAvailable, readableCalendar } from './CalendarContext'
import { composerDraftKey, useChatSession, useConversationScope, useRestartConversation, useTaskScope, type ComposerMail } from './session'
import { TaskScopeSelector } from './TaskContext'
import { PreferenceContext } from './PreferenceContext'
import { AudioRecorder, supportedAudioFormat } from './AudioRecorder'
import { LiveVoice } from './LiveVoice'
import { useAudioCapabilities } from './audio-capabilities'
import { SpeechControls, useSpeechPlayback } from './SpeechPlayback'
import { ActiveSkillBanner } from './ActiveSkillBanner'
import { SkillFavoritesMenu } from './SkillFavoritesMenu'
import { SlashPicker } from './SlashPicker'
import { useFavoriteSkills } from './skill-favorites'
import { buildSlashOptions, detectSlashQuery, isSkillCreator, skillName, stripSlashToken, toActiveSkillSelection, type SlashOption } from './skill-selection'
import { SKILL_REFUSALS } from './transport'

type Accounts = AccountSummaryOut[]
// A retry would send the same request and be refused again.
const NEEDS_NEW_CONVERSATION = new Set(['conversation_not_found', 'conversation_scope_change_requires_new', 'context_unavailable', 'context_revoked', 'chat_account_not_granted', 'chat_settings_changed'])

export function ChatComposer({ conversationId, contextMail, contextOccurrenceId, model, promptRevisionId, onPromptRevisionChange, onSend, autoFocus, onRecordingStart, onVoiceActiveChange }: { conversationId?: string; contextMail?: ComposerMail; contextOccurrenceId?: string; model?: string; promptRevisionId?: string | null; onPromptRevisionChange?: (id: string | null) => void; onSend?: () => void; autoFocus?: boolean; onRecordingStart?: () => void; onVoiceActiveChange?: (active: boolean) => void }) {
  const { texts } = useI18n()
  const navigate = useNavigate()
  const { state, send, stop, composerTexts, setComposerText, selectedSkills, setSelectedSkill, setSkillPersistence, audioDraftLocked, setAudioDraftLocked } = useChatSession()
  const contextMessageId = contextMail?.id
  const { scope, ready, saved } = useConversationScope(conversationId, contextMail)
  const tasks = useTaskScope(conversationId, contextMessageId)
  const stt = useAudioCapabilities()?.stt
  const format = supportedAudioFormat(stt)
  const stopLiveVoice = useRef<(() => void) | null>(null)
  const registerVoiceStop = useCallback((stop: (() => void) | null) => { stopLiveVoice.current = stop }, [])
  const lockDraft = useCallback((locked: boolean) => {
    if (locked) { stopLiveVoice.current?.(); onRecordingStart?.() }
    setAudioDraftLocked(locked)
  }, [onRecordingStart, setAudioDraftLocked])
  const draftKey = composerDraftKey(conversationId, contextMessageId)
  const value = composerTexts[draftKey] ?? ''
  const setValue = (text: string) => setComposerText(draftKey, text)
  const appendTranscript = (text: string) => setComposerText(draftKey, previous =>
    previous ? previous + (previous.endsWith('\n\n') ? '' : previous.endsWith('\n') ? '\n' : '\n\n') + text : text)
  const activeSkillEntry = selectedSkills[draftKey] ?? null
  const { favoriteIds, toggleFavorite } = useFavoriteSkills()
  const skills = useQuery(skillsQuery())
  const templateSelection = useTemplateSelection({ conversationId, revision: promptRevisionId, onChange: onPromptRevisionChange })
  const restartConversation = useRestartConversation(conversationId)

  const containerRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const slashListId = useId()
  const [cursorPos, setCursorPos] = useState(0)
  const [manualSlashOpen, setManualSlashOpen] = useState(false)
  const [dismissedToken, setDismissedToken] = useState<string | null>(null)
  const [activeIndex, setActiveIndex] = useState(0)

  const voiceContext = JSON.stringify([draftKey, contextOccurrenceId, scope, model, promptRevisionId, activeSkillEntry?.skill.revision_id,
    tasks.scope.source, tasks.scope.account_id, format])
  // A turn-scoped skill is consumed when the stream starts; that must not stop the answer.
  const liveVoiceContext = JSON.stringify([draftKey, contextOccurrenceId, scope, conversationId ? saved?.model : model, templateSelection.selected,
    tasks.scope.source, tasks.scope.account_id, format])
  const busy = state.phase === 'working' || state.phase === 'streaming'

  const safeCursor = Math.min(cursorPos, value.length)
  const slashMatch = detectSlashQuery(value, safeCursor)
  const tokenKey = slashMatch ? `${slashMatch.start}:${slashMatch.query}` : null
  const slashOpen = !audioDraftLocked && !busy && (manualSlashOpen || (slashMatch !== null && dismissedToken !== tokenKey))

  useEffect(() => {
    if (tokenKey === null && dismissedToken !== null) setDismissedToken(null)
  }, [tokenKey, dismissedToken])

  useEffect(() => {
    setManualSlashOpen(false)
    setDismissedToken(null)
    setActiveIndex(0)
  }, [draftKey])

  const slashOptions = useMemo(() => buildSlashOptions({
    query: slashMatch?.query ?? '',
    skills: skills.data ?? [],
    prompts: templateSelection.prompts,
    promptDetails: templateSelection.promptDetails,
    favoriteIds,
    selectedSkillId: activeSkillEntry?.skill.id ?? null,
    selectedPromptRevisionId: templateSelection.selected,
    busy: templateSelection.busy || busy,
    texts,
  }), [slashMatch?.query, skills.data, templateSelection.prompts, templateSelection.promptDetails, favoriteIds, activeSkillEntry?.skill.id, templateSelection.selected, templateSelection.busy, busy, texts])

  const clampedActiveIndex = slashOptions.length === 0 ? -1 : Math.min(Math.max(activeIndex, 0), slashOptions.length - 1)
  const activeOption = clampedActiveIndex >= 0 ? slashOptions[clampedActiveIndex] : undefined
  const optionId = (option: SlashOption) => `${slashListId}-${option.key}`

  const closeSlash = useCallback(() => {
    setManualSlashOpen(false)
    if (tokenKey !== null) setDismissedToken(tokenKey)
  }, [tokenKey])

  const restoreFocusAt = (cursor: number) => {
    requestAnimationFrame(() => {
      const el = textareaRef.current
      if (!el) return
      el.focus()
      try { el.setSelectionRange(cursor, cursor) } catch { /* noop */ }
    })
  }

  const handleSelectSlashOption = (option: SlashOption) => {
    if (option.disabled) return
    setManualSlashOpen(false)
    setDismissedToken(null)
    let nextText = value
    if (slashMatch) {
      const stripped = stripSlashToken(value, slashMatch)
      nextText = stripped.nextText
      setValue(stripped.nextText)
      setCursorPos(stripped.nextCursor)
      restoreFocusAt(stripped.nextCursor)
    } else {
      requestAnimationFrame(() => textareaRef.current?.focus())
    }
    if (option.kind === 'skill') {
      stopLiveVoice.current?.()
      setSelectedSkill(
        draftKey,
        toActiveSkillSelection(option.skill),
        activeSkillEntry?.skill.id === option.skill.id ? activeSkillEntry.persistence : 'turn',
      )
    } else if (option.kind === 'template') {
      void templateSelection.choose(option.revisionId)
    } else if (option.kind === 'clear-skill') {
      stopLiveVoice.current?.()
      setSelectedSkill(draftKey, null)
    } else if (option.kind === 'clear-template') {
      void templateSelection.choose(null)
    } else if (option.kind === 'manage-templates') {
      void navigate({ to: '/settings/prompts' })
    } else if (option.kind === 'create-skill') {
      stopLiveVoice.current?.()
      // A skill is drafted in its own conversation, without mail or calendar context; the draft moves along.
      if (draftKey !== 'new' || contextOccurrenceId) {
        restartConversation()
        setComposerText(draftKey, '')
      }
      setComposerText('new', nextText)
      setSelectedSkill('new', toActiveSkillSelection(option.skill), 'until_stopped')
    }
  }

  const onTextareaKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!slashOpen) return
    if (event.key === 'Escape') {
      event.preventDefault()
      closeSlash()
      return
    }
    if (event.key === 'ArrowDown' && slashOptions.length > 0) {
      event.preventDefault()
      setActiveIndex((clampedActiveIndex + 1) % slashOptions.length)
      return
    }
    if (event.key === 'ArrowUp' && slashOptions.length > 0) {
      event.preventDefault()
      setActiveIndex((clampedActiveIndex - 1 + slashOptions.length) % slashOptions.length)
      return
    }
    if ((event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) || event.key === 'Tab') {
      if (slashOptions.length > 0 && activeOption) {
        event.preventDefault()
        if (!activeOption.disabled) handleSelectSlashOption(activeOption)
      } else if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
        event.preventDefault()
      }
    }
  }

  const hasTopContext = Boolean(
    activeSkillEntry
    || templateSelection.selected !== null
    || templateSelection.error,
  )
  const topContext = hasTopContext ? (
    <div className="le-composer-selection">
      {activeSkillEntry && (
        <ActiveSkillBanner
          entry={activeSkillEntry}
          liveSkill={skills.data?.find(item => item.id === activeSkillEntry.skill.id)}
          skillsLoaded={skills.isSuccess}
          disabled={busy || audioDraftLocked}
          onChangePersistence={persistence => { stopLiveVoice.current?.(); setSkillPersistence(draftKey, persistence) }}
          onRemove={() => { stopLiveVoice.current?.(); setSelectedSkill(draftKey, null) }}
        />
      )}
      <ActiveTemplateBanner
        conversationId={conversationId}
        selection={templateSelection}
      />
    </div>
  ) : undefined

  return <>
    <Composer
      value={value}
      locked={audioDraftLocked}
      onChange={nextValue => {
        setValue(nextValue)
        setActiveIndex(0)
      }}
      onSubmit={() => {
        if (busy || audioDraftLocked || !ready || !tasks.ready || !value.trim()) return
        stopLiveVoice.current?.()
        onSend?.()
        void send({
          text: value,
          account_id: scope,
          conversation_id: conversationId,
          context_message_id: contextMessageId,
          context_occurrence_id: contextOccurrenceId,
          task_source: tasks.scope.source,
          task_source_account_id: tasks.scope.account_id,
          model,
          ...(!conversationId ? { prompt_revision_id: promptRevisionId } : {}),
          ...(activeSkillEntry ? { skill_id: activeSkillEntry.skill.id, skill_revision_id: activeSkillEntry.skill.revision_id } : {}),
        })
      }}
      onStop={stop}
      busy={busy}
      placeholder={activeSkillEntry ? isSkillCreator(activeSkillEntry.skill) ? texts.skills.creatorPlaceholder : texts.skills.placeholderWithSkill(skillName(activeSkillEntry.skill, texts)) : contextMessageId ? texts.inbox.mailQuestionPlaceholder : texts.chat.placeholder}
      compactPlaceholder={activeSkillEntry ? isSkillCreator(activeSkillEntry.skill) ? texts.skills.creatorCompactPlaceholder : texts.skills.compactPlaceholder : undefined}
      autoFocus={autoFocus}
      topContext={topContext}
      containerRef={containerRef}
      textareaRef={textareaRef}
      onTextareaKeyDown={onTextareaKeyDown}
      onCursorChange={setCursorPos}
      textareaProps={{
        role: 'combobox',
        'aria-autocomplete': 'list',
        'aria-expanded': slashOpen,
        'aria-controls': slashOpen ? slashListId : undefined,
        'aria-activedescendant': slashOpen && activeOption ? optionId(activeOption) : undefined,
      }}
      controls={<>
        <SkillFavoritesMenu
          disabled={busy || audioDraftLocked}
          favoriteIds={favoriteIds}
          selectedSkillId={activeSkillEntry?.skill.id ?? null}
          onSelectSkill={skill => {
            stopLiveVoice.current?.()
            setSelectedSkill(
              draftKey,
              toActiveSkillSelection(skill),
              activeSkillEntry?.skill.id === skill.id ? activeSkillEntry.persistence : 'turn',
            )
            requestAnimationFrame(() => textareaRef.current?.focus())
          }}
          onToggleFavorite={toggleFavorite}
          onOpenSlash={() => {
            setDismissedToken(null)
            setManualSlashOpen(true)
            setActiveIndex(0)
            requestAnimationFrame(() => textareaRef.current?.focus())
          }}
        />
        <TaskScopeSelector conversationId={conversationId} contextMail={contextMail} disabled={busy || audioDraftLocked} />
      </>}
      audioControls={<><LiveVoice conversationId={conversationId} contextKey={liveVoiceContext} disabled={busy || !ready || !tasks.ready || audioDraftLocked} registerStop={registerVoiceStop} onStart={onRecordingStart} onActiveChange={onVoiceActiveChange}
        stopChat={stop} send={(text, voiceAudio, sttElapsedMs, targetConversationId, onConversationId) => {
          const currentConversationId = targetConversationId ?? conversationId
          const skillEntry = (currentConversationId ? selectedSkills[currentConversationId] : undefined) ?? activeSkillEntry
          if (!currentConversationId) onSend?.()
          return send({
            text,
            voice_response: true,
            voice_stt_elapsed_ms: sttElapsedMs,
            account_id: scope,
            conversation_id: currentConversationId,
            context_message_id: contextMessageId,
            context_occurrence_id: contextOccurrenceId,
            task_source: tasks.scope.source,
            task_source_account_id: tasks.scope.account_id,
            ...(!currentConversationId ? { model, prompt_revision_id: promptRevisionId } : {}),
            ...(skillEntry ? { skill_id: skillEntry.skill.id, skill_revision_id: skillEntry.skill.revision_id } : {}),
          }, { preserveDraft: true, voiceAudio, onConversationCreated: onConversationId })
        }} />
        {stt && format && <AudioRecorder key={voiceContext} format={format}
        limits={stt.app_limits} disabled={busy || !ready || !tasks.ready}
        onLock={lockDraft} onTranscript={appendTranscript} />}</>}
    />
    <SlashPicker
      open={slashOpen}
      id={slashListId}
      anchorRef={containerRef}
      query={slashMatch?.query ?? ''}
      options={slashOptions}
      activeIndex={clampedActiveIndex}
      onActiveIndexChange={setActiveIndex}
      loading={skills.isPending || templateSelection.isPending}
      skillError={skills.isError}
      templateError={templateSelection.isError}
      onRetry={() => {
        if (skills.isError) void skills.refetch()
        if (templateSelection.isError) templateSelection.refetchPrompts()
      }}
      onSelect={handleSelectSlashOption}
      onClose={closeSlash}
      optionId={optionId}
    />
  </>
}

type Draft = { onDraft: (id: string) => void; disabled: boolean }
function Reference({ mail, accounts, draft }: { mail: MailRefOut; accounts: Accounts; draft: Draft }) {
  const navigate = useNavigate()
  return <MailReference mail={mail} accountLabel={accounts.filter(account => account.id === mail.account_id).map(providerLabel)[0]} onOpen={() => { void navigate({ to: '/mail/$messageId', params: { messageId: mail.id } }) }} onDraft={() => draft.onDraft(mail.id)} draftDisabled={draft.disabled || !accounts.some(account => account.id === mail.account_id)} />
}
function HistoryReference({ id, accounts, draft, version, contextId }: { id: string; accounts: Accounts; draft: Draft; version: number; contextId: string }) {
  const { texts } = useI18n()
  const { data, isError } = useQuery(chatMailReferenceQuery(id, version, contextId))
  if (isError) return <p className="le-hint">{texts.chat.referenceError}</p>
  return data && accounts.some(account => account.id === data.account_id) ? <Reference mail={data} accounts={accounts} draft={draft} /> : null
}
// DESIGN.md §9.1: at most three follow-up suggestions, whatever the model sent.
function Suggestions({ items, onSend }: { items: string[]; onSend: (text: string) => void }) {
  return <div className="le-chat-suggestions">{items.slice(0, 3).map(text => <Chip key={text} onClick={() => onSend(text)}>{text}</Chip>)}</div>
}

// The agent saves a proposal while it works and its answer at the end of the turn. Live, the card
// follows the answer; the history shows it there too, so nothing jumps once the history reloads.
export function turnOrder(messages: ChatMessageOut[]): ChatMessageOut[] {
  const ordered: ChatMessageOut[] = []
  let held: ChatMessageOut[] = []
  const flush = () => { ordered.push(...held); held = [] }
  for (const message of messages) {
    if (message.role === 'proposal' || message.role === 'draft') { held.push(message); continue }
    if (message.role === 'user') flush()
    ordered.push(message)
    if (message.role === 'assistant') flush()
  }
  flush()
  return ordered
}

export function Conversation({ conversationId }: { conversationId: string }) {
  const { texts } = useI18n()
  const history = useQuery(historyQuery(conversationId))
  const mailSummary = useQuery(summaryQuery(true))
  const summary = mailSummary.isSuccess ? mailSummary.data : undefined
  const { data: models = [] } = useQuery(modelsQuery())
  const skills = useQuery(skillsQuery())
  const { state, send, retry, retryWithoutPreferences, audioDraftLocked, selectedSkills } = useChatSession()
  const tts = useAudioCapabilities()?.tts
  const { scope, ready } = useConversationScope(conversationId)
  const sources = useQuery(chatCalendarSourcesQuery())
  const restart = useRestartConversation(conversationId)
  const scrollRef = useRef<HTMLDivElement>(null)
  const { showEnd, scrollToEnd } = useFollowScroll(scrollRef, [history.data, state, conversationId])
  const accounts = summary?.accounts ?? []
  const messages = history.data ?? []
  const grantVersion = summary?.mail_scope_version
  const readable = new Set(sources.isSuccess ? sources.data.filter(readableCalendar).map(source => source.id) : [])
  const oldCalendar = messages.some(message => message.context?.calendar_sources_replaced ||
    (sources.isSuccess && !calendarScopeAvailable(message.context?.calendar_scope_json, readable, true)))
  const oldGrant = oldCalendar || grantVersion != null && messages.some(message => message.context?.mail_scope_version !== grantVersion)
  const isSkillRevokedOnly = (context: ChatMessageOut['context'] | null | undefined) => Boolean(
    context?.revoked
    && context.skill_revision_id
    && !context.prompt_revision_id
    && !context.preferences_enabled
    && !context.calendar_sources_replaced
    && (grantVersion == null || context.mail_scope_version === grantVersion)
    && (!sources.isSuccess || calendarScopeAvailable(context.calendar_scope_json, readable)),
  )
  const renderProposal = (message: ChatMessageOut) => {
    const context = message.context
    const revoked = !context || context.revoked || context.calendar_sources_replaced ||
      context.mail_scope_version == null || (grantVersion != null && context.mail_scope_version !== grantVersion) ||
      (sources.isSuccess && !calendarScopeAvailable(context.calendar_scope_json, readable))
    if (!revoked && message.proposal_status === 'pending' && grantVersion == null) {
      return mailSummary.isPending
        ? <p role="status">{texts.chat.loadingSummary}</p>
        : <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void mailSummary.refetch() } }}>{texts.chat.summaryError}</Notice>
    }
    const needsCalendars = !calendarScopeAvailable(context?.calendar_scope_json, new Set<string>())
    if (!revoked && needsCalendars && message.proposal_status === 'pending' && !sources.isSuccess && message.proposal?.kind !== 'calendar') {
      return sources.isError
        ? <Notice tone="danger" action={{ label: texts.calendar.retry, onClick: () => { void sources.refetch() } }}>{texts.calendar.error}</Notice>
        : <p role="status">{texts.calendar.loading}</p>
    }
    return <ProposalCard key={message.id} message={message} oldGrant={revoked} grantVersion={grantVersion} skillRevoked={isSkillRevokedOnly(context)} composer={!oldGrant} />
  }
  const speechEnabled = grantVersion != null && !oldGrant && tts?.status === 'available' && tts.formats.includes('audio/mpeg') &&
    typeof Audio !== 'undefined' && typeof URL.createObjectURL === 'function'
  const speech = useSpeechPlayback(conversationId, speechEnabled, audioDraftLocked)
  const ordered = turnOrder(messages)
  const lastAssistant = messages.findLast(message => message.role === 'assistant')
  // Follow-ups come after the last answer and the cards of its turn.
  let suggestionsAfter = lastAssistant ? ordered.indexOf(lastAssistant) : -1
  while (suggestionsAfter >= 0 && (ordered[suggestionsAfter + 1]?.role === 'proposal' || ordered[suggestionsAfter + 1]?.role === 'draft')) suggestionsAfter += 1
  const live = state.conversationId === conversationId && !(state.messageId && messages.some(message => message.id === state.messageId))
  const userIndex = state.userMessageId ? messages.findIndex(message => message.id === state.userMessageId) : -1
  const savedPartial = (state.phase === 'stopped' || state.phase === 'error') && userIndex >= 0 && messages.slice(userIndex + 1).some(message => message.role === 'assistant')
  const busy = state.phase === 'working' || state.phase === 'streaming'
  const sendFollowUp = (text: string, contextMessageId?: string) => {
    if (audioDraftLocked || !ready) return
    const skill = selectedSkills[conversationId]?.skill
    void send({
      text, account_id: scope, conversation_id: conversationId, context_message_id: contextMessageId,
      ...(skill ? { skill_id: skill.id, skill_revision_id: skill.revision_id } : {}),
    })
  }
  const draft: Draft = { onDraft: id => sendFollowUp(texts.mail.draftPrompt, id), disabled: oldGrant || !ready || busy || audioDraftLocked }
  const modelLabel = (id?: string) => id ? models.find(model => model.id === id)?.label ?? id : undefined
  const skillLabel = (skill?: { id: string; slug: string; version: number } | null) => {
    if (!skill) return undefined
    const found = skills.data?.find(item => item.id === skill.id)
    return texts.skills.usedTag(found ? skillName(found, texts) : skill.slug, skill.version)
  }
  const liveSkillId = state.request?.skill_id
  const liveSkillRevisionId = state.request?.skill_revision_id
  const liveSkillMeta = liveSkillId
    ? (skills.data?.find(item => item.id === liveSkillId && (!liveSkillRevisionId || item.revision_id === liveSkillRevisionId))
      ?? skills.data?.find(item => item.id === liveSkillId))
    : undefined
  // One revoked notice per conversation, at its first revoked answer. When the grant or calendars changed,
  // the notice in place of the composer already offers the new conversation, so this one only names the cause.
  const revokedAnswers = ordered.filter(message => message.role === 'assistant' && message.context?.revoked)
  const revokedKinds = new Set(revokedAnswers.map(message => message.context?.preferences_enabled ? 'preferences' : isSkillRevokedOnly(message.context) ? 'skill' : 'template'))
  const revokedText = revokedKinds.size === 1 && revokedKinds.has('skill') ? texts.skills.revoked
    : revokedKinds.size === 1 && revokedKinds.has('template') ? texts.prompts.removed
    : texts.errors.context_revoked
  const firstRevokedId = revokedAnswers[0]?.id
  const restartAction = { label: texts.shell.newConversation, onClick: restart }
  const renderMessage = (message: ChatMessageOut) => {
    if (message.role === 'user') return <ChatMessage role="user">{message.content}</ChatMessage>
    if (message.role === 'proposal' || message.role === 'draft') return renderProposal(message)
    if (message.role !== 'assistant') return null
    const usedSkillLabel = skillLabel(message.extras.skill)
    return <>{message.context && <PreferenceContext context={message.context} />}{usedSkillLabel && <div><Tag tone="on-surface" icon="sparkle">{usedSkillLabel}</Tag></div>}{message.id === firstRevokedId && <Notice tone="info" action={oldGrant ? undefined : restartAction}>{revokedText}</Notice>}<ChatMessage role="assistant" model={modelLabel(message.extras.model)} stopped={message.stopped}><SafeMarkdown text={message.content} /></ChatMessage>
      {speechEnabled && !message.stopped && !message.context?.revoked && message.content.trim() &&
        <SpeechControls messageId={message.id} player={speech} disabled={audioDraftLocked} />}
      {message.extras.mails?.map(id => !oldGrant && grantVersion != null && message.context && !message.context.revoked ? <HistoryReference key={id} id={id} accounts={accounts} draft={draft} version={grantVersion} contextId={message.context.id} /> : <p className="le-hint" key={id}>{texts.chat.referenceError}</p>)}{!oldGrant && message.extras.calendars?.map(ref => <CalendarReference key={ref.occurrence_id} occurrenceId={ref.occurrence_id} sourceId={ref.source_id} />)}</>
  }
  const status = state.status?.code === 'searching' ? texts.status.searching(state.status.count ?? 0) : state.status ? texts.status[state.status.code] : null
  const historyGone = errorCode(history.error instanceof Error ? history.error.cause : history.error) === 'conversation_not_found'
  const newConversation = restartAction
  const skillFailure = Boolean(state.errorCode && SKILL_REFUSALS.has(state.errorCode))
  return <div className="le-chat-view"><ChatHeader conversationId={conversationId} modelMenu={<ModelMenu disabled={audioDraftLocked} key={`${conversationId}:model`} conversationId={conversationId} />} /><div className="le-chat-messages le-chat-thread" ref={scrollRef}>
    {history.isPending && <p className="le-hint">{texts.chat.loadingHistory}</p>}
    {history.isError && <Notice tone="danger" action={historyGone ? newConversation : { label: texts.chat.retry, onClick: () => { void history.refetch() } }}>{historyGone ? texts.errors.conversation_not_found : texts.chat.historyError}</Notice>}
    {ordered.map((message, index) => <div className="le-chat-entry" id={`message-${message.id}`} key={message.id}>{renderMessage(message)}{index === suggestionsAfter && !live && !oldGrant && <Suggestions items={lastAssistant?.extras.suggestions ?? []} onSend={sendFollowUp} />}</div>)}
    {live && <div className="le-chat-entry">{!savedPartial && <>{!messages.some(message => message.id === state.userMessageId) && <ChatMessage role="user">{state.request?.text}</ChatMessage>}{status && <div className="le-working" role="status">{status}</div>}{liveSkillMeta && <div><Tag tone="on-surface" icon="sparkle">{texts.skills.usedTag(skillName(liveSkillMeta, texts), liveSkillMeta.version)}</Tag></div>}<div aria-hidden={busy || undefined}><ChatMessage role="assistant" model={modelLabel(state.model)} streaming={busy} stopped={state.phase === 'stopped'} live={false}><SafeMarkdown text={state.text} /></ChatMessage></div></>}
      {state.phase === 'stopped' && <div className="le-chat-retry"><Button variant="ghost" onClick={() => { void retry() }}>{texts.chat.askAgain}</Button></div>}
      {state.phase === 'error' && <Notice tone="danger" action={state.errorCode === 'preferences_unavailable' ? { label: texts.preferences.continueWithout, onClick: () => { void retryWithoutPreferences() } } : skillFailure ? undefined : state.errorCode && NEEDS_NEW_CONVERSATION.has(state.errorCode) ? newConversation : { label: texts.chat.retry, onClick: () => { void retry() } }}>{state.errorCode === 'conversation_scope_change_requires_new' ? texts.taskChat.scopeConflict : texts.errors[state.errorCode ?? 'internal']}</Notice>}
      {state.phase === 'done' && <>{!oldGrant && state.calendars?.map(ref => <CalendarReference key={ref.occurrence_id} occurrenceId={ref.occurrence_id} sourceId={ref.source_id} />)}{state.mails.map(mail => <Reference key={mail.id} mail={mail} accounts={accounts} draft={draft} />)}{state.proposal && renderProposal(state.proposal)}{!oldGrant && <Suggestions items={state.suggestions} onSend={sendFollowUp} />}</>}
    </div>}
    <div className="le-sr" aria-live="polite">{state.conversationId === conversationId && state.phase === 'done' ? state.text : ''}</div>
  </div>{showEnd && <div className="le-chat-end"><Button variant="secondary" onClick={scrollToEnd}>{texts.chat.toEnd}</Button></div>}<div className="le-chat-composer">{oldGrant ? <Notice tone="info" action={{ label: texts.prompts.restart, onClick: restart }}>{oldCalendar ? texts.calendarSettings.restart : texts.chatAccounts.restart}</Notice> : <ChatComposer conversationId={conversationId} onRecordingStart={speech.stop} autoFocus={busy && state.conversationId === conversationId} />}</div></div>
}

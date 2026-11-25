// i18n-exempt: route paths, query keys and CSS classes.
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { chatConversationUpdate } from '../api/generated/sdk.gen'
import type { ConversationOut, PromptDetailOut, PromptOut } from '../api/generated/types.gen'
import { conversationsQuery, promptQuery, promptsQuery } from '../api/queries'
import { errorCode, unwrap } from '../api/unwrap'
import { useChatSession, useConversationScope, useRestartConversation } from '../chat/session'
import { IconButton, Notice, Tag } from '../ds'
import { useI18n } from '../i18n/I18n'
import '../chat/composer-skills.css'
import './prompts.css'

export type TemplateSelectionState = {
  prompts: PromptOut[]
  promptDetails: PromptDetailOut[]
  selected: string | null
  current?: PromptOut | PromptDetailOut
  owner?: PromptDetailOut
  resolved: boolean
  isPending: boolean
  isError: boolean
  busy: boolean
  error?: string
  choose: (id: string | null) => Promise<void>
  reload: () => Promise<void>
  refetchPrompts: () => void
  clearError: () => void
}

export function useTemplateSelection({
  conversationId,
  revision = null,
  onChange,
}: {
  conversationId?: string
  revision?: string | null
  onChange?: (id: string | null) => void
}): TemplateSelectionState {
  const { texts } = useI18n()
  const { saved } = useConversationScope(conversationId)
  const { state } = useChatSession()
  const list = useQuery(promptsQuery()), client = useQueryClient()
  const details = useQueries({ queries: (list.data ?? []).map(item => promptQuery(item.id)) })
  const selected = conversationId ? saved?.prompt_revision_id ?? null : revision
  const promptDetails = details.map(item => item.data).filter((item): item is PromptDetailOut => Boolean(item))
  const owner = promptDetails.find(item => item.revisions.some(value => value.id === selected))
  const resolved = list.isSuccess && details.every(item => item.isSuccess)
  const current = list.data?.find(item => item.revision_id === selected) ?? owner
  const [pending, setPending] = useState(false), [error, setError] = useState<string>(), lock = useRef(false)
  const busy = pending || state.phase === 'working' || state.phase === 'streaming'

  useEffect(() => { setError(undefined) }, [conversationId])

  const reload = async () => { await client.fetchQuery({ ...conversationsQuery(), staleTime: 0 }); setError(undefined) }
  const choose = async (id: string | null) => {
    if (busy || lock.current) return
    if (!conversationId) { onChange?.(id); return }
    if (!saved) return
    lock.current = true; setPending(true); setError(undefined)
    try {
      const updated = await unwrap(chatConversationUpdate({ path: { conversation_id: conversationId }, body: { expected_version: saved.version, prompt_revision_id: id }, throwOnError: true }))
      client.setQueryData<ConversationOut[]>(['conversations'], previous => previous?.map(item => item.id === updated.id ? updated : item))
      void client.invalidateQueries({ queryKey: ['conversations'] })
    } catch (cause) {
      const code = errorCode(cause instanceof Error ? cause.cause : cause)
      setError(code === 'version_conflict' ? texts.prompts.selectionConflict : texts.prompts.selectionError)
    } finally { lock.current = false; setPending(false) }
  }

  return {
    prompts: list.data ?? [],
    promptDetails,
    selected,
    current,
    owner,
    resolved,
    isPending: list.isPending,
    isError: list.isError || details.some(item => item.isError),
    busy,
    error,
    choose,
    reload,
    refetchPrompts: () => { void client.invalidateQueries({ queryKey: ['prompts'] }) },
    clearError: () => setError(undefined),
  }
}

export function ActiveTemplateBanner({
  conversationId,
  selection,
  onClear,
}: {
  conversationId?: string
  selection: TemplateSelectionState
  onClear?: () => void
}) {
  const { texts } = useI18n()
  const restart = useRestartConversation(conversationId)
  const { selected, current, owner, resolved, isPending, isError, busy, error, choose, reload, refetchPrompts } = selection
  const showActive = selected !== null || Boolean(error) || (selected !== null && !current && resolved)
  if (!showActive) return null

  const activeRevision = owner?.revisions.find(rev => rev.id === selected)
  const showVersion = Boolean(owner && activeRevision && owner.revision_id !== selected)
  const label = current
    ? `${texts.prompts.chip(current.name)}${showVersion && activeRevision ? ` · ${texts.prompts.version(activeRevision.number)}` : ''}`
    : texts.prompts.chip(resolved ? texts.prompts.removed : texts.prompts.loading)

  return <div className="le-template-control">
    {selected !== null && (current || !resolved) && (
      <div className="le-composer-active-row">
        <Tag tone="on-surface" icon="file-code">{label}</Tag>
        <div className="le-composer-active-actions">
          <span className="le-composer-active-meta">{conversationId ? texts.prompts.durationNext : texts.prompts.durationNew}</span>
        </div>
        {current && (
          <span className="le-composer-active-close">
            <IconButton
              icon="x"
              label={texts.prompts.clearSelection}
              tip={texts.prompts.clearSelectionTip}
              disabled={busy}
              onClick={() => {
                if (onClear) onClear()
                else void choose(null)
              }}
            />
          </span>
        )}
      </div>
    )}
    {selected !== null && isPending && <span role="status" className="le-hint">{texts.prompts.loading}</span>}
    {selected !== null && isError && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: refetchPrompts }}>{texts.prompts.error}</Notice>}
    {owner && owner.revision_id !== selected && <Notice tone="info" action={busy ? undefined : { label: texts.prompts.adopt, onClick: () => { void choose(owner.revision_id) } }}>{texts.prompts.newer}</Notice>}
    {error && <Notice tone="danger" action={{ label: texts.prompts.loadCurrent, onClick: () => { void reload().catch(() => {}) } }}>{error}</Notice>}
    {selected && !current && resolved && <Notice tone="info" action={{ label: conversationId ? texts.prompts.restart : texts.prompts.useStandard, onClick: () => { if (conversationId) restart(); else void choose(null) } }}>{texts.prompts.removed}</Notice>}
  </div>
}

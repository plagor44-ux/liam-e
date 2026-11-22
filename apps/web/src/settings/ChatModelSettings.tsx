// i18n-exempt: route paths and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'
import { chatModelSettingsUpdate } from '../api/generated/sdk.gen'
import type { ChatModelOut, ChatModelSettingsOut } from '../api/generated/types.gen'
import { chatModelSettingsQuery, modelsQuery } from '../api/queries'
import { errorCode, unwrap } from '../api/unwrap'
import { ModelDetails } from '../chat/ModelDetails'
import { modelMenuItems } from '../chat/model-menu-items'
import { Button, Chip, Icon, Menu, Notice, Tooltip } from '../ds'
import { useI18n } from '../i18n/I18n'
import { SettingsSaveScope, useSettingsSlice } from './SettingsSave'
import './settings.css'

const codeOf = (cause: unknown) => errorCode(cause instanceof Error ? cause.cause : cause)

function modelLabel(models: ChatModelOut[], id: string): string {
  return models.find(item => item.id === id)?.label ?? id
}

export function ChatModelSettings() {
  const { texts } = useI18n()
  const settings = useQuery(chatModelSettingsQuery())
  const catalogue = useQuery(modelsQuery())
  const reload = async () => {
    const result = await settings.refetch()
    if (result.isError || !result.data) throw result.error
    return result.data
  }
  return <SettingsSaveScope><section className="le-swipe" aria-labelledby="le-chat-model-title">
    <h2 id="le-chat-model-title" className="heading">{texts.chatModel.title}</h2>
    <p className="le-swipe-intro">{texts.chatModel.description}</p>
    {settings.isError
      ? <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { void settings.refetch(); void catalogue.refetch() } }}>{texts.settingsUi.error}</Notice>
      : !settings.data || catalogue.isPending ? <p role="status">{texts.settingsUi.loading}</p>
      : <ChatModelSettingsForm settings={settings.data} catalogue={catalogue} reload={reload} refreshSettings={() => { void settings.refetch() }} />}
  </section></SettingsSaveScope>
}

function ChatModelSettingsForm({
  settings,
  catalogue,
  reload,
  refreshSettings,
}: {
  settings: ChatModelSettingsOut
  catalogue: ReturnType<typeof useQuery<ChatModelOut[]>>
  reload: () => Promise<ChatModelSettingsOut>
  refreshSettings: () => void
}) {
  const { texts } = useI18n()
  const client = useQueryClient()
  const models = catalogue.data ?? []
  const labelId = useId()
  const [base, setBase] = useState<ChatModelSettingsOut>(settings)
  const [draft, setDraft] = useState<string | null>(settings.default_model)
  const [busy, setBusy] = useState(false)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [outcome, setOutcome] = useState<'saved' | 'conflict' | 'rejected' | 'error'>()
  const [rejectedCode, setRejectedCode] = useState<'model_unknown' | 'model_catalog_unavailable'>()
  const lock = useRef(false)

  const metadataChanged =
    settings.status !== base.status
    || settings.effective_default_model !== base.effective_default_model
    || settings.app_default_model !== base.app_default_model
    || settings.default_model !== base.default_model

  if (!busy && outcome !== 'conflict') {
    if (settings.version > base.version && draft === base.default_model) {
      setBase(settings); setDraft(settings.default_model); setOutcome(undefined)
    } else if (settings.version === base.version && metadataChanged) {
      // Derived availability status changes when the catalog recovers without bumping settings.version.
      setBase(settings)
    }
  }

  const unverified = catalogue.isError || catalogue.isPending || models.some(item => item.availability === 'unverified')
  const refreshCatalogue = () => {
    void catalogue.refetch()
    refreshSettings()
  }
  const refreshAll = async () => {
    try {
      const current = await reload()
      setBase(current); setDraft(current.default_model); setOutcome(undefined); setRejectedCode(undefined)
      refreshCatalogue()
    } catch {
      setOutcome('error')
    }
  }

  const shownId = draft ?? base.app_default_model
  const shownLabel = draft === null ? texts.chatModel.appDefault(modelLabel(models, base.app_default_model)) : modelLabel(models, shownId)
  const { items, details } = modelMenuItems(
    models,
    shownId,
    unverified,
    texts.chat,
    next => { setDraft(next); setOutcome(undefined); setRejectedCode(undefined) },
    refreshCatalogue,
    () => setDetailsOpen(true),
  )

  const selectableDraft = draft !== null && models.some(item => item.id === draft && item.availability === 'available')
  const hasChange = draft !== base.default_model
  const dirty = hasChange || outcome === 'conflict' || outcome === 'rejected' || outcome === 'error'
  const canSave = !busy && outcome !== 'conflict' && hasChange && selectableDraft
  const canReset = !busy && outcome !== 'conflict' && (base.default_model !== null || draft !== null)

  const save = async (next: string | null) => {
    if (lock.current) return
    lock.current = true; setBusy(true); setOutcome(undefined); setRejectedCode(undefined)
    try {
      const saved = await unwrap(chatModelSettingsUpdate({ body: { expected_version: base.version, default_model: next }, throwOnError: true }))
      await client.cancelQueries({ queryKey: chatModelSettingsQuery().queryKey })
      const current = client.setQueryData<ChatModelSettingsOut>(
        chatModelSettingsQuery().queryKey,
        cached => cached && cached.version > saved.version ? cached : saved,
      ) ?? saved
      setBase(current); setDraft(current.default_model); setOutcome('saved')
      await client.invalidateQueries({ queryKey: modelsQuery().queryKey })
    } catch (cause) {
      const code = codeOf(cause)
      if (code === 'version_conflict') {
        setOutcome('conflict')
      } else if (code === 'model_unknown' || code === 'model_catalog_unavailable') {
        setRejectedCode(code); setOutcome('rejected')
        refreshCatalogue()
      } else {
        setOutcome('error')
      }
    } finally {
      lock.current = false; setBusy(false)
    }
  }

  const discard = () => {
    if (lock.current) return
    const latest = settings.version >= base.version ? settings : base
    setBase(latest); setDraft(latest.default_model); setOutcome(undefined); setRejectedCode(undefined)
  }

  useSettingsSlice('chat-model', { dirty, canSave, busy, save: () => save(draft), discard })

  const unchanged = draft === base.default_model
  const storedEntry = base.default_model ? models.find(item => item.id === base.default_model) : undefined
  const storedMissing = Boolean(
    unchanged && base.default_model && (
      base.status === 'unlisted'
      || (!unverified && catalogue.isSuccess && !storedEntry && !models.some(item => item.resolved_id === base.default_model))
    ),
  )
  const storedUnverified = Boolean(unchanged && base.default_model && base.status === 'unverified')
  const showEmptyNotice = !unverified && details.length === 0 && !storedMissing

  return <div className="le-swipe-fields">
    <div className="le-swipe-field">
      <span className="le-swipe-label" id={labelId}>{texts.chatModel.choiceLabel}</span>
      <Menu
        placement="down-start"
        label={texts.chatModel.choiceLabel}
        items={items}
        onOpen={() => { if (catalogue.isStale || unverified || base.status === 'unlisted' || base.status === 'unverified') refreshCatalogue() }}
        trigger={triggerProps => (
          <Chip {...triggerProps} quiet disabled={busy || outcome === 'conflict'} aria-labelledby={labelId}>
            <span className="le-model-label">{shownLabel}</span>
            <Icon name="chevron-down" size="sm" />
          </Chip>
        )}
      />
    </div>
    {storedMissing && (
      <Notice tone="info" action={{ label: texts.chat.modelRefresh, onClick: refreshCatalogue }}>
        {texts.chatModel.unlistedNotice(base.default_model!, modelLabel(models, base.app_default_model))}
      </Notice>
    )}
    {storedUnverified && (
      <Notice tone="info" action={{ label: texts.chat.modelRefresh, onClick: refreshCatalogue }}>
        {texts.chatModel.unverifiedNotice}
      </Notice>
    )}
    {unchanged && base.default_model && !unverified && storedEntry && storedEntry.availability === 'available' && storedEntry.group !== 'general' && (
      <Notice tone="info">{texts.chatModel.outsideMailNotice}</Notice>
    )}
    {unverified && !storedUnverified && (
      <Notice tone="info" action={{ label: texts.chat.modelRefresh, onClick: refreshCatalogue }}>
        {texts.chat.modelUnverified}
      </Notice>
    )}
    {showEmptyNotice && (
      <Notice tone="info" action={{ label: texts.chat.modelRefresh, onClick: refreshCatalogue }}>
        {texts.chat.noMailModels}
      </Notice>
    )}
    {outcome === 'saved' && <Notice tone="success">{texts.settingsUi.saved}</Notice>}
    {outcome === 'conflict' && (
      <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { void refreshAll() } }}>
        {texts.chatModel.conflict}
      </Notice>
    )}
    {outcome === 'rejected' && rejectedCode && (
      <Notice tone="danger" action={{ label: texts.chat.modelRefresh, onClick: refreshCatalogue }}>
        {texts.errors[rejectedCode]}
      </Notice>
    )}
    {outcome === 'error' && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
    <div className="le-swipe-actions">
      <Tooltip content={texts.chatModel.resetTip}>
        <Button variant="secondary" disabled={!canReset} onClick={() => { void save(null) }}>
          {texts.chatModel.reset}
        </Button>
      </Tooltip>
    </div>
    {detailsOpen && <ModelDetails models={details} unavailable={unverified} onRefresh={refreshCatalogue} onClose={() => setDetailsOpen(false)} />}
  </div>
}

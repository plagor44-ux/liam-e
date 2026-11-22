// i18n-exempt: route paths and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { settingsUiUpdate } from '../api/generated/sdk.gen'
import type { UiSettingsOut } from '../api/generated/types.gen'
import { cacheUiSettings, uiSettingsQuery } from '../api/queries'
import { errorCode, unwrap } from '../api/unwrap'
import { Button, Chip, Icon, Menu, Notice, Tooltip, type IconName } from '../ds'
import { useI18n } from '../i18n/I18n'
import { SettingsSaveScope, useSettingsSlice } from './SettingsSave'
import type { SwipeAction } from './useSwipeSettings'
import './settings.css'

const ACTIONS: SwipeAction[] = ['none', 'archive', 'toggle_read', 'trash']
const DEFAULTS = { left: 'archive' as SwipeAction, right: 'toggle_read' as SwipeAction }
const actionIcon = (action: SwipeAction): IconName | undefined =>
  action === 'archive' ? 'archive' : action === 'toggle_read' ? 'mail-open' : action === 'trash' ? 'trash-2' : undefined
const codeOf = (cause: unknown) => errorCode(cause instanceof Error ? cause.cause : cause)

export function SwipeSettings() {
  const { texts } = useI18n()
  const query = useQuery(uiSettingsQuery())
  if (!query.data) return <section className="le-swipe"><h2 className="heading">{texts.settingsUi.title}</h2>
    {query.isPending ? <><p role="status">{texts.settingsUi.loading}</p>{[0, 1].map(index => <div key={index} className="le-swipe-placeholder" aria-hidden="true" />)}</>
      : <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void query.refetch() } }}>{texts.settingsUi.error}</Notice>}
  </section>
  return <SettingsSaveScope><SwipeSettingsForm settings={query.data} reload={async () => {
    const result = await query.refetch()
    if (result.isError || !result.data) throw result.error
    return result.data
  }} /></SettingsSaveScope>
}

/**
 * Edited form values and the confirmed assignment stay separate until the server accepts a save.
 * A save expects the revision of the assignment the form is based on. A background refetch never
 * moves that base under an edit, so a change from another device ends in a conflict instead of
 * being overwritten unseen; an untouched form simply follows the newer assignment.
 */
function SwipeSettingsForm({ settings, reload }: { settings: UiSettingsOut; reload: () => Promise<UiSettingsOut> }) {
  const { texts } = useI18n()
  const client = useQueryClient()
  const [base, setBase] = useState<UiSettingsOut>(settings)
  const [left, setLeft] = useState<SwipeAction>(settings.swipe_left)
  const [right, setRight] = useState<SwipeAction>(settings.swipe_right)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<'saved' | 'error' | 'conflict'>()
  const lock = useRef(false)
  if (settings.revision > base.revision && left === base.swipe_left && right === base.swipe_right && !busy && outcome !== 'conflict') {
    setBase(settings); setLeft(settings.swipe_left); setRight(settings.swipe_right); setOutcome(undefined)
  }
  const select = (setter: (value: SwipeAction) => void, value: SwipeAction) => { setter(value); setOutcome(undefined) }
  const save = async (assignment: { left: SwipeAction; right: SwipeAction }) => {
    if (lock.current) return
    lock.current = true; setBusy(true); setOutcome(undefined)
    try {
      const saved = await unwrap(settingsUiUpdate({ body: { expected_revision: base.revision, swipe_left: assignment.left, swipe_right: assignment.right }, throwOnError: true }))
      const current = await cacheUiSettings(client, saved)
      setBase(current); setLeft(current.swipe_left); setRight(current.swipe_right)
      setOutcome('saved')
    } catch (cause) {
      if (codeOf(cause) === 'ui_settings_conflict') {
        // Show the competing change, but never retry on our own. The shown assignment becomes the
        // base, so saving again consciously replaces exactly what the notice names.
        try { setBase(await reload()); setOutcome('conflict') } catch { setOutcome('error') }
      } else setOutcome('error')
    } finally { lock.current = false; setBusy(false) }
  }
  const discard = () => {
    if (lock.current) return
    const latest = settings.revision >= base.revision ? settings : base
    setBase(latest); setLeft(latest.swipe_left); setRight(latest.swipe_right); setOutcome(undefined)
  }
  const hasChange = left !== base.swipe_left || right !== base.swipe_right
  const dirty = hasChange || outcome === 'conflict' || outcome === 'error'
  useSettingsSlice('ui-swipe', {
    dirty,
    canSave: dirty && !busy,
    busy,
    save: () => save({ left, right }),
    discard,
  })
  return <section className="le-swipe">
    <h2 className="heading">{texts.settingsUi.title}</h2>
    <p className="le-swipe-intro">{texts.settingsUi.intro}</p>
    <div className="le-swipe-fields">
      <SwipeSelect id="le-swipe-left" label={texts.settingsUi.left} action={left} disabled={busy} onSelect={value => select(setLeft, value)} />
      <SwipeSelect id="le-swipe-right" label={texts.settingsUi.right} action={right} disabled={busy} onSelect={value => select(setRight, value)} />
    </div>
    {outcome === 'saved' && <Notice tone="success">{texts.settingsUi.saved}</Notice>}
    {outcome === 'error' && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
    {outcome === 'conflict' && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { setLeft(base.swipe_left); setRight(base.swipe_right); setOutcome(undefined) } }}>
      {texts.settingsUi.conflict(texts.settingsUi.actions[base.swipe_left], texts.settingsUi.actions[base.swipe_right])}</Notice>}
    <div className="le-swipe-actions">
      <Tooltip content={texts.settingsUi.restoreTip}><Button variant="secondary" disabled={busy} onClick={() => { void save(DEFAULTS) }}>{texts.settingsUi.restore}</Button></Tooltip>
    </div>
  </section>
}

function SwipeSelect({ id, label, action, disabled, onSelect }: { id: string; label: string; action: SwipeAction; disabled: boolean; onSelect: (action: SwipeAction) => void }) {
  const { texts } = useI18n()
  return <div className="le-swipe-field">
    <span className="le-swipe-label" id={`${id}-label`}>{label}</span>
    <Menu trigger={props => <Chip {...props} quiet disabled={disabled} aria-labelledby={`${id}-label`}>{texts.settingsUi.actions[action]}<Icon name="chevron-down" size="sm" /></Chip>}
      items={ACTIONS.map(value => ({ label: texts.settingsUi.actions[value], checked: value === action, onSelect: () => onSelect(value) }))} />
    <SwipePreview action={action} />
  </div>
}

function SwipePreview({ action }: { action: SwipeAction }) {
  const { texts } = useI18n()
  const icon = actionIcon(action)
  return <div className="le-swipe-preview">
    <span className="le-swipe-preview-row" aria-hidden="true"><span className="le-swipe-preview-name">{texts.settingsUi.previewSender}</span><span className="le-swipe-preview-subject">{texts.settingsUi.previewSubject}</span></span>
    <span className="le-swipe-preview-effect">{icon && <Icon name={icon} size="sm" />}{texts.settingsUi.effects[action]}</span>
  </div>
}

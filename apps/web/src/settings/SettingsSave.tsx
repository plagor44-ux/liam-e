// i18n-exempt: CSS classes and internal slice identifiers.
import { useBlocker, useRouter } from '@tanstack/react-router'
import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../ds'
import { useFocusTrap } from '../ds/useFocusTrap'
import { useI18n } from '../i18n/I18n'

export interface SettingsSliceState {
  dirty: boolean
  canSave: boolean
  busy: boolean
  hideBar?: boolean
  save: () => Promise<void>
  discard: () => void
}

interface SliceMeta {
  hideBar?: boolean
  dirty: boolean
  canSave: boolean
  busy: boolean
}

interface SettingsSaveContextValue {
  register: (id: string, meta: SliceMeta, handlers: { save: () => Promise<void>; discard: () => void }) => void
  unregister: (id: string) => void
  dirty: boolean
  canSave: boolean
  busy: boolean
  showBar: boolean
  saveAll: () => Promise<void>
  discardAll: () => void
}

const SettingsSaveContext = createContext<SettingsSaveContextValue | null>(null)

export function SettingsSaveProvider({ children }: { children: ReactNode }) {
  const [slices, setSlices] = useState<Record<string, SliceMeta>>({})
  const handlersRef = useRef<Record<string, { save: () => Promise<void>; discard: () => void }>>({})
  const metaRef = useRef<Record<string, SliceMeta>>({})
  metaRef.current = slices

  const register = useCallback((id: string, meta: SliceMeta, handlers: { save: () => Promise<void>; discard: () => void }) => {
    handlersRef.current[id] = handlers
    metaRef.current = { ...metaRef.current, [id]: meta }
    setSlices(prev => {
      const current = prev[id]
      if (current && current.dirty === meta.dirty && current.canSave === meta.canSave && current.busy === meta.busy && current.hideBar === meta.hideBar) {
        return prev
      }
      return { ...prev, [id]: meta }
    })
  }, [])

  const unregister = useCallback((id: string) => {
    delete handlersRef.current[id]
    if (id in metaRef.current) {
      const nextMeta = { ...metaRef.current }
      delete nextMeta[id]
      metaRef.current = nextMeta
    }
    setSlices(prev => {
      if (!(id in prev)) return prev
      const next = { ...prev }
      delete next[id]
      return next
    })
  }, [])

  const entries = Object.entries(slices)
  const dirtyEntries = entries.filter(([, slice]) => slice.dirty)
  const dirty = dirtyEntries.length > 0
  const busy = entries.some(([, slice]) => slice.busy)
  const showBar = entries.some(([, slice]) => !slice.hideBar && (slice.dirty || slice.busy))
  const canSave = dirty && !busy && dirtyEntries.some(([, slice]) => slice.canSave) && dirtyEntries.every(([, slice]) => slice.canSave)

  const saveAll = useCallback(async () => {
    const active = Object.entries(metaRef.current).filter(([, slice]) => slice.dirty && slice.canSave && !slice.busy)
    await Promise.all(active.map(([id]) => handlersRef.current[id]?.save()))
  }, [])

  const discardAll = useCallback(() => {
    for (const id of Object.keys(handlersRef.current)) {
      handlersRef.current[id]?.discard()
    }
  }, [])

  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const router = useRouter({ warn: false })
  const value = useMemo(() => ({ register, unregister, dirty, canSave, busy, showBar, saveAll, discardAll }), [register, unregister, dirty, canSave, busy, showBar, saveAll, discardAll])

  return <SettingsSaveContext.Provider value={value}>
    {children}
    {router && <SettingsRouterBlocker dirty={dirty} onDiscard={discardAll} />}
  </SettingsSaveContext.Provider>
}

function SettingsRouterBlocker({ dirty, onDiscard }: { dirty: boolean; onDiscard: () => void }) {
  const { texts } = useI18n()
  const titleId = useId()
  const blocker = useBlocker({
    shouldBlockFn: ({ current, next }) => dirty && current.pathname !== next.pathname,
    enableBeforeUnload: false,
    disabled: !dirty,
    withResolver: true,
  })
  const blocked = blocker.status === 'blocked'
  const stay = () => { if (blocker.status === 'blocked') blocker.reset() }
  const leave = () => {
    if (blocker.status === 'blocked') {
      onDiscard()
      blocker.proceed()
    }
  }
  const trap = useFocusTrap(blocked, stay)
  if (!blocked) return null
  return createPortal(
    <div className="le-scrim" onMouseDown={event => { if (event.target === event.currentTarget) stay() }}>
      <div className="le-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={trap} tabIndex={-1}>
        <h2 id={titleId} className="heading">{texts.settingsUi.leaveTitle}</h2>
        <p>{texts.settingsUi.leaveDescription}</p>
        <div className="le-dialog-actions">
          <Button variant="secondary" onClick={stay}>{texts.settingsUi.keepEditing}</Button>
          <Button variant="danger" onClick={leave}>{texts.settingsUi.discardAndLeave}</Button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export function SettingsSaveBar() {
  const { texts } = useI18n()
  const context = useContext(SettingsSaveContext)
  if (!context || !context.showBar) return null
  return <div className="le-settings-bar" role="region" aria-label={texts.settingsUi.unsaved}>
    <span className="body-strong" role="status">{texts.settingsUi.unsaved}</span>
    <div className="le-settings-bar-actions">
      <Button variant="secondary" disabled={context.busy} onClick={context.discardAll}>{texts.settingsUi.discard}</Button>
      <Button variant="primary" disabled={!context.canSave || context.busy} onClick={() => { void context.saveAll() }}>{texts.settingsUi.save}</Button>
    </div>
  </div>
}

export function SettingsSaveScope({ children }: { children: ReactNode }) {
  const context = useContext(SettingsSaveContext)
  if (context) return <>{children}</>
  return <SettingsSaveProvider>{children}<SettingsSaveBar /></SettingsSaveProvider>
}

export function useSettingsSlice(id: string, state: SettingsSliceState) {
  const context = useContext(SettingsSaveContext)
  const register = context?.register
  const unregister = context?.unregister
  const saveRef = useRef(state.save)
  const discardRef = useRef(state.discard)
  saveRef.current = state.save
  discardRef.current = state.discard

  useLayoutEffect(() => {
    if (!register) return
    register(
      id,
      { dirty: state.dirty, canSave: state.canSave, busy: state.busy, hideBar: state.hideBar },
      { save: () => saveRef.current(), discard: () => discardRef.current() },
    )
  }, [register, id, state.dirty, state.canSave, state.busy, state.hideBar])

  useLayoutEffect(() => () => { unregister?.(id) }, [unregister, id])
}

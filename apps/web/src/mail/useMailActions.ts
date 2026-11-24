import { useQueryClient, type InfiniteData, type QueryKey } from '@tanstack/react-query'
import { createContext, createElement, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { mailActionsUndo, mailMessageAction, mailMessagesBulkAction } from '../api/generated/sdk.gen'
import type { MessageDetailOut, MessageListItemOut, MessagePageOut } from '../api/generated/types.gen'
import { useI18n } from '../i18n/I18n'
import { errorCode } from '../api/unwrap'
import { useBreakpoint } from '../shell/useBreakpoint'
import { z } from 'zod'

export type Clock = { now(): number; setTimeout(fn: () => void, ms: number): ReturnType<typeof setTimeout>; clearTimeout(id: ReturnType<typeof setTimeout>): void }
const systemClock: Clock = { now: () => Date.now(), setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id) }
type Removal = 'archive' | 'trash' | 'spam'
type Kind = Removal | 'mark_read' | 'mark_unread'
const removes = (kind: Kind | 'remove'): kind is Removal => kind === 'archive' || kind === 'trash' || kind === 'spam'
type MailNotice = { tone: 'success' | 'danger'; text: string; wrap: boolean }
type Change = { key: QueryKey; page: number; index: number; row: MessageListItemOut }
type GroupItem = { actionId: string; messageId: string; changes: Change[]; previousRead?: boolean }
export type UndoEntry = { actionId: string; messageId: string; kind: Kind; changes: Change[]; previousRead?: boolean; group?: GroupItem[]; partial?: boolean; wrap?: boolean; text: string; label: string; deadline: number; pending: boolean }
const LIMIT = 20
const partialFolder = z.object({ detail: z.object({ folder: z.enum(['INBOX', 'TRASH', 'ARCHIVE', 'SPAM']), reconciled: z.boolean().optional() }) })
const inFolder = ({ key }: Change, folder: string) => {
  const filters = key[1]
  const listedFolder = filters && typeof filters === 'object' && 'folder' in filters ? filters.folder : 'INBOX'
  return (listedFolder ?? 'INBOX') === folder
}
// A provider can stop responding; release the queue after fifteen seconds.
const UNDO_TIMEOUT_MS = 15_000

export function useMailActions({ clock = systemClock }: { clock?: Clock } = {}) {
  const { texts } = useI18n()
  const client = useQueryClient()
  const breakpoint = useBreakpoint()
  const [entries, setEntries] = useState<UndoEntry[]>([])
  const entriesRef = useRef<UndoEntry[]>([])
  const [notice, setNotice] = useState<MailNotice | null>(null)
  const [permissionNotice, setPermissionNotice] = useState<string | null>(null)
  const dismissPermissionNotice = () => setPermissionNotice(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const reservations = useRef(0)
  const [reserved, setReserved] = useState(0)
  const pending = useRef(0)
  const queues = useRef(new Map<string, Promise<unknown>>())
  const [, setQueueVersion] = useState(0)
  const reconcile = useRef(false)
  const settle = () => {
    if (--pending.current === 0 && reconcile.current) {
      reconcile.current = false
      void client.invalidateQueries({ queryKey: ['messages'] })
    }
  }
  const schedule = (next: UndoEntry[]) => {
    if (timer.current !== undefined) clock.clearTimeout(timer.current)
    timer.current = undefined
    const deadlines = next.filter(entry => !entry.pending).map(entry => entry.deadline)
    if (deadlines.length) timer.current = clock.setTimeout(() => {
      timer.current = undefined
      publish(entriesRef.current.filter(entry => entry.pending || entry.deadline > clock.now()))
    }, Math.max(0, Math.min(...deadlines) - clock.now()))
  }
  const publish = (next: UndoEntry[]) => {
    entriesRef.current = next
    setEntries(next)
    schedule(next)
  }
  const notify = (text: string, tone: 'danger' | 'success' = 'danger', wrap = false) => {
    setNotice({ text, tone, wrap })
    if (noticeTimer.current !== undefined) clock.clearTimeout(noticeTimer.current)
    noticeTimer.current = clock.setTimeout(() => { noticeTimer.current = undefined; setNotice(null) }, 10_000)
  }
  const notifyPermission = () => setPermissionNotice(texts.errors.provider_permission_missing)
  useEffect(() => () => {
    if (timer.current !== undefined) clock.clearTimeout(timer.current)
    if (noticeTimer.current !== undefined) clock.clearTimeout(noticeTimer.current)
  }, [clock])
  const restore = (changes: Change[], removal: boolean) => {
    for (const { key, page, index, row } of changes) {
      client.setQueryData<InfiniteData<MessagePageOut>>(key, data => {
        if (!data) return data
        if (removal && data.pages.some(p => p.items.some(item => item.id === row.id))) return data
        return { ...data, pages: data.pages.map((p, i) => {
          if (removal && i === page) {
            const items = [...p.items]
            items.splice(Math.min(index, items.length), 0, row)
            return { ...p, items }
          }
          return removal ? p : { ...p, items: p.items.map(item => item.id === row.id ? { ...item, is_read: row.is_read } : item) }
        }) }
      })
    }
  }
  const update = (id: string, kind: Kind | 'remove') => {
    client.setQueriesData<InfiniteData<MessagePageOut>>({ queryKey: ['messages'] }, data => data && ({
      ...data, pages: data.pages.map(page => ({ ...page, items: removes(kind) || kind === 'remove'
        ? page.items.filter(item => item.id !== id)
        : page.items.map(item => item.id === id ? { ...item, is_read: kind === 'mark_read' } : item) })),
    }))
  }
  // Both mutations and undo share the per-message queue. Reserve capacity before enqueueing.
  const enqueue = <T,>(id: string, execute: () => Promise<T>): Promise<T> => {
    const previous = queues.current.get(id)
    const work = previous ? previous.catch(() => {}).then(execute) : execute()
    queues.current.set(id, work)
    setQueueVersion(version => version + 1)
    const finished = () => {
      if (queues.current.get(id) === work) {
        queues.current.delete(id)
        setQueueVersion(version => version + 1)
      }
    }
    void work.then(finished, finished)
    return work
  }
  const checkCapacity = () => {
    const active = entriesRef.current.filter(entry => entry.pending || entry.deadline > clock.now())
    if (active.length !== entriesRef.current.length) publish(active)
    if (active.length + reservations.current < LIMIT) return true
    notify(texts.inbox.undoCapacity)
    return false
  }
  const run = (id: string, kind: Kind, options?: { silent?: boolean }) => {
    // Refused before anything changed: nothing was removed, so nothing came back.
    if (!options?.silent && !checkCapacity()) return Promise.resolve(false)
    if (!options?.silent) { reservations.current++; setReserved(reservations.current) }
    return enqueue(id, () => execute(id, kind, options))
  }
  const execute = async (id: string, kind: Kind, options?: { silent?: boolean }) => {
    ++pending.current
    const changes: Change[] = []
    const removal = removes(kind)
    let previousRead: boolean | undefined
    const rollback = () => {
      restore(changes, removal)
      if (previousRead !== undefined) client.setQueryData<MessageDetailOut>(['message', id], data => data && { ...data, is_read: previousRead! })
      reconcile.current = true
    }
    try {
      await client.cancelQueries({ queryKey: ['messages'] })
      if (!removal) {
        await client.cancelQueries({ queryKey: ['message', id], exact: true })
        previousRead = client.getQueryData<MessageDetailOut>(['message', id])?.is_read
        client.setQueryData<MessageDetailOut>(['message', id], data => data && { ...data, is_read: kind === 'mark_read' })
      }
      for (const [key, data] of client.getQueriesData<InfiniteData<MessagePageOut>>({ queryKey: ['messages'] })) {
        data?.pages.forEach((page, pageIndex) => page.items.forEach((row, index) => {
          if (row.id === id) changes.push({ key, page: pageIndex, index, row })
        }))
      }
      update(id, kind)
      const result = await mailMessageAction({ path: { message_id: id }, body: { kind } })
      const code = errorCode(result.error)
      if (code === 'provider_permission_missing') {
        void client.invalidateQueries({ queryKey: ['accounts'] })
        void client.invalidateQueries({ queryKey: ['summary'] })
      }
      if (result.response?.status === 404) {
        update(id, 'remove')
        if (!options?.silent) notify(texts.proposalErrors.item_gone)
      } else if (result.response?.status === 409 && (code === 'provider_partial' || code === 'provider_permission_missing')) {
        const parsed = partialFolder.safeParse(result.error)
        const folder = parsed.success ? parsed.data.detail.folder : undefined
        const retained = folder && changes.filter(change => inFolder(change, folder))
        if (retained) restore(retained, removal)
        reconcile.current = true
        const reconciled = parsed.success && parsed.data.detail.reconciled === true
        const partial = reconciled ? texts.inbox.providerReconciled : folder ? texts.inbox.providerPartial[folder] : texts.errors.provider_partial
        if (code === 'provider_permission_missing') notifyPermission()
        if (!options?.silent || code === 'provider_permission_missing') {
          notify(partial, 'danger', reconciled || code === 'provider_permission_missing')
        }
        void client.invalidateQueries({ queryKey: ['summary'] })
        void client.invalidateQueries({ queryKey: ['message', id] })
        return Boolean(retained?.length)
      } else if (result.response?.status === 409) {
        rollback()
        if (!options?.silent) notify(errorCode(result.error) === 'provider_action_unsupported' ? texts.errors.provider_action_unsupported : texts.proposalErrors.action_unsupported)
        return changes.length > 0
      } else if (!result.data || result.response?.status !== 200) {
        rollback()
        if (code === 'provider_permission_missing') notifyPermission()
        else if (!options?.silent) notify(result.response?.status === 502 && code === 'provider_failed' ? texts.errors.provider_failed : texts.errors.action_uncertain)
        return changes.length > 0
      } else {
        if (removal) reconcile.current = true
        if (!options?.silent) {
          const text = breakpoint === 'mobile' ? texts.inbox.doneShort[kind] : texts.inbox.done[kind](result.data.message.subject)
          setNotice(null)
          publish([...entriesRef.current, { actionId: result.data.action_id, messageId: id, kind, changes, previousRead, text, label: texts.inbox.done[kind](result.data.message.subject), deadline: clock.now() + 10_000, pending: false }])
        }
        void client.invalidateQueries({ queryKey: ['summary'] })
        void client.invalidateQueries({ queryKey: ['message', id] })
      }
    } catch {
      rollback()
      if (!options?.silent) notify(texts.errors.action_uncertain)
      return changes.length > 0
    } finally {
      if (!options?.silent) { reservations.current--; setReserved(reservations.current) }
      settle()
    }
  }
  const runBulk = (ids: string[], kind: Kind) => {
    if (!ids.length || !checkCapacity()) return Promise.resolve()
    reservations.current++; setReserved(reservations.current)
    const previous = ids.map(id => queues.current.get(id)).filter((p): p is Promise<unknown> => Boolean(p))
    const work = (previous.length ? Promise.all(previous.map(p => p.catch(() => {}))) : Promise.resolve()).then(async () => {
      ++pending.current
      const changes: Change[] = []
      const reads = new Map<string, boolean>()
      const rollback = (selected: string[]) => {
        const wanted = new Set(selected)
        restore(changes.filter(change => wanted.has(change.row.id)).sort((a, b) => a.page - b.page || a.index - b.index), removes(kind))
        for (const id of selected) {
          const read = reads.get(id)
          if (read !== undefined) client.setQueryData<MessageDetailOut>(['message', id], data => data && { ...data, is_read: read })
        }
      }
      try {
        await client.cancelQueries({ queryKey: ['messages'] })
        for (const id of ids) {
          if (!removes(kind)) {
            await client.cancelQueries({ queryKey: ['message', id], exact: true })
            const read = client.getQueryData<MessageDetailOut>(['message', id])?.is_read
            if (read !== undefined) reads.set(id, read)
            client.setQueryData<MessageDetailOut>(['message', id], data => data && { ...data, is_read: kind === 'mark_read' })
          }
        }
        for (const [key, data] of client.getQueriesData<InfiniteData<MessagePageOut>>({ queryKey: ['messages'] })) {
          data?.pages.forEach((page, pageIndex) => page.items.forEach((row, index) => {
            if (ids.includes(row.id)) changes.push({ key, page: pageIndex, index, row })
          }))
        }
        for (const id of ids) update(id, kind)
        const result = await mailMessagesBulkAction({ body: { message_ids: ids, kind } })
        const code = errorCode(result.error)
        if (code === 'provider_action_unsupported' || code === 'provider_permission_missing') {
          rollback(ids)
          reconcile.current = true
          if (code === 'provider_permission_missing') {
            void client.invalidateQueries({ queryKey: ['accounts'] })
            void client.invalidateQueries({ queryKey: ['summary'] })
          }
          if (code === 'provider_permission_missing') notifyPermission()
          else notify(texts.inbox.unsupportedSelection)
          return
        }
        if (!result.data || !result.response?.ok) throw new Error('bulk action failed') // i18n-exempt
        const failed = result.data.results.filter(row => row.status === 'failed' || row.status === 'unsupported').map(row => row.message_id)
        rollback(failed)
        for (const row of result.data.results) if (row.status === 'not_found') { update(row.message_id, 'remove'); void client.invalidateQueries({ queryKey: ['message', row.message_id], exact: true }) }
        const group = result.data.results.filter(row => row.status === 'done' && row.action_id).map(row => ({ actionId: row.action_id!, messageId: row.message_id, changes: changes.filter(change => change.row.id === row.message_id), previousRead: reads.get(row.message_id) }))
        const partial = result.data.results.filter(row => row.status === 'partial')
        const partialFolders = new Map(partial.map(row => [row.message_id, row.folder]))
        const retained = changes.filter(change => {
          const folder = partialFolders.get(change.row.id)
          return folder ? inFolder(change, folder) : false
        })
        restore(retained.sort((a, b) => a.page - b.page || a.index - b.index), removes(kind))
        const base = texts.inbox.bulkResult(kind, group.length, ids.length)
        const permissionMissing = result.data.results.some(row => row.error_code === 'provider_permission_missing')
        if (permissionMissing) void client.invalidateQueries({ queryKey: ['accounts'] })
        const reconciled = partial.filter(row => row.reconciled).length
        const partlyChanged = partial.length - reconciled
        const wrap = permissionMissing || reconciled > 0
        const text = [base, partlyChanged ? texts.inbox.bulkProviderPartial(partlyChanged) : '',
          reconciled ? texts.inbox.bulkProviderReconciled(reconciled) : ''].filter(Boolean).join(' ')
        if (permissionMissing) notifyPermission()
        if (group.length) {
          setNotice(null)
          publish([...entriesRef.current, { ...group[0]!, group, partial: group.length < ids.length, wrap, kind, text, label: text, deadline: clock.now() + 10_000, pending: false }])
        } else notify(text, 'danger', wrap)
        reconcile.current = true
        void client.invalidateQueries({ queryKey: ['summary'] })
      } catch {
        rollback(ids)
        reconcile.current = true
        notify(texts.errors.action_uncertain)
      } finally {
        reservations.current--; setReserved(reservations.current)
        settle()
      }
    })
    for (const id of ids) queues.current.set(id, work)
    setQueueVersion(version => version + 1)
    void work.finally(() => { for (const id of ids) if (queues.current.get(id) === work) queues.current.delete(id); setQueueVersion(version => version + 1) })
    return work
  }
  const undo = (actionId?: string) => {
    const selected = actionId ? entriesRef.current.find(entry => entry.actionId === actionId) : entriesRef.current.at(-1)
    if (!selected || selected.pending) return Promise.resolve()
    if (clock.now() >= selected.deadline) { publish(entriesRef.current.filter(entry => entry !== selected)); return Promise.resolve() }
    publish(entriesRef.current.map(entry => entry === selected ? { ...entry, pending: true } : entry))
    const group = selected.group ?? [{ actionId: selected.actionId, messageId: selected.messageId, changes: selected.changes, previousRead: selected.previousRead }]
    const executeUndo = async () => {
      ++pending.current
      const controller = new AbortController()
      let timedOut = false
      let timeout: ReturnType<typeof setTimeout> | undefined
      const deadline = new Promise<never>((_, reject) => {
        timeout = clock.setTimeout(() => {
          timedOut = true
          controller.abort()
          reject(new Error('undo_timeout')) // i18n-exempt
        }, UNDO_TIMEOUT_MS)
      })
      try {
        const result = await Promise.race([mailActionsUndo({ body: { action_ids: group.map(item => item.actionId) }, signal: controller.signal }), deadline])
        const code = errorCode(result.error) ?? result.data?.error_code
        if (code === 'provider_permission_missing') void client.invalidateQueries({ queryKey: ['accounts'] })
        if (selected.group && result.data) {
          const restored = group.filter(item => result.data!.restored.includes(item.actionId))
          restore(restored.flatMap(item => item.changes).sort((a, b) => a.page - b.page || a.index - b.index), removes(selected.kind))
          for (const item of restored) if (!removes(selected.kind) && item.previousRead !== undefined) client.setQueryData<MessageDetailOut>(['message', item.messageId], data => data && { ...data, is_read: item.previousRead! })
          if (restored.length !== group.length) {
            const unsupported = new Set(result.data.unsupported ?? [])
            const remaining = group.filter(item => !result.data!.restored.includes(item.actionId) && !unsupported.has(item.actionId)).length
            if (unsupported.size) {
              const notice = [remaining ? texts.inbox.bulkUndoFailed(remaining) : '', texts.errors.provider_action_unsupported].filter(Boolean).join(' ')
              if (code === 'provider_permission_missing') notifyPermission()
              notify(notice, 'danger', true)
            } else if (code === 'provider_permission_missing') notifyPermission()
            else notify(code === 'provider_action_unsupported' ? texts.errors.provider_action_unsupported : texts.inbox.bulkUndoFailed(remaining))
          }
        } else if (result.data?.restored.includes(selected.actionId)) {
          restore(selected.changes, removes(selected.kind))
          if (!removes(selected.kind) && selected.previousRead !== undefined) client.setQueryData<MessageDetailOut>(['message', selected.messageId], data => data && { ...data, is_read: selected.previousRead! })
        } else if (result.data?.skipped.includes(selected.actionId)) notify(texts.inbox.undoSkipped)
        else if (result.data?.expired.includes(selected.actionId)) notify(texts.inbox.undoExpired)
        else if (code === 'provider_permission_missing') notifyPermission()
        else if (code === 'provider_action_unsupported') notify(texts.errors.provider_action_unsupported)
        else if (result.data?.failed.includes(selected.actionId)) notify(texts.inbox.undoFailed)
        else notify(texts.inbox.undoUncertain)
      } catch { notify(timedOut ? texts.inbox.undoTimeout : texts.inbox.undoUncertain) }
      finally {
        if (timeout !== undefined) clock.clearTimeout(timeout)
        publish(timedOut
          ? entriesRef.current.map(entry => entry.actionId === selected.actionId ? { ...entry, pending: false, deadline: clock.now() + 10_000 } : entry)
          : entriesRef.current.filter(entry => entry.actionId !== selected.actionId))
        reconcile.current = true
        void client.invalidateQueries({ queryKey: ['summary'] })
        for (const item of group) void client.invalidateQueries({ queryKey: ['message', item.messageId] })
        settle()
      }
    }
    if (!selected.group) return enqueue(selected.messageId, executeUndo)
    const previous = group.map(item => queues.current.get(item.messageId)).filter((p): p is Promise<unknown> => Boolean(p))
    const work = (previous.length ? Promise.all(previous.map(p => p.catch(() => {}))) : Promise.resolve()).then(executeUndo)
    for (const item of group) queues.current.set(item.messageId, work)
    setQueueVersion(version => version + 1)
    void work.finally(() => { for (const item of group) if (queues.current.get(item.messageId) === work) queues.current.delete(item.messageId); setQueueVersion(version => version + 1) })
    return work
  }
  return { run, runBulk, undo, notify, notice, permissionNotice, dismissPermissionNotice, entries, checkCapacity, isBusy: (id: string) => queues.current.has(id), capacityAvailable: entries.filter(entry => entry.pending || entry.deadline > clock.now()).length + reserved < LIMIT }
}

const MailActionsContext = createContext<ReturnType<typeof useMailActions> | null>(null)

export function MailActionsProvider({ children, clock }: { children: ReactNode; clock?: Clock }) {
  const actions = useMailActions({ clock })
  return createElement(MailActionsContext.Provider, { value: actions }, children)
}

export function useMailActionsContext() {
  const actions = useContext(MailActionsContext)
  if (!actions) throw new Error('useMailActionsContext must be used within MailActionsProvider')
  return actions
}

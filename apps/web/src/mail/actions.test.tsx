import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider, useQuery, type InfiniteData } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { MessageListItemOut, MessagePageOut } from '../api/generated/types.gen'
import { MailActionsProvider, useMailActions, useMailActionsContext, type Clock } from './useMailActions'
import { ActionToast } from './ActionToast'
import { de } from '../i18n/de'
import { en } from '../i18n/en'

const originalMatchMedia = window.matchMedia
const rows = ['Erste', 'Zweite'].map((subject, i) => ({ id: String(i + 1), subject, is_read: false }) as MessageListItemOut)
const key = ['messages', {}]
function makeClock() {
  let now = 0, next = 0
  const timers = new Map<number, { fn: () => void; at: number }>()
  const clock: Clock = { now: () => now, setTimeout: (fn, ms) => { const id = ++next; timers.set(id, { fn, at: now + ms }); return id as unknown as ReturnType<typeof setTimeout> }, clearTimeout: id => { timers.delete(id as unknown as number) } }
  return { clock, advance(ms: number) { now += ms; for (const [id, timer] of timers) if (timer.at <= now) { timers.delete(id); timer.fn() } } }
}
function setup(mobile = false) {
  window.matchMedia = ((query: string) => ({ matches: !mobile && query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData<InfiniteData<MessagePageOut>>(key, { pages: [{ items: rows, next_cursor: null }], pageParams: [undefined] })
  const time = makeClock()
  function Harness() {
    const { data } = useQuery<InfiniteData<MessagePageOut>>({ queryKey: key, queryFn: async () => client.getQueryData<InfiniteData<MessagePageOut>>(key)!, enabled: false })
    const { run, runBulk, undo, notify, notice, permissionNotice, entries, capacityAvailable } = useMailActions({ clock: time.clock })
    const latest = entries.at(-1)
    const tone = notice?.tone ?? (permissionNotice || latest?.partial ? 'danger' : 'success')
    return <><div>{data?.pages.flatMap(page => page.items).map(row => <div key={row.id}>{row.subject}:{String(row.is_read)}</div>)}</div>
      <button onClick={() => { void runBulk(['1', '2'], 'archive') }}>bulk archive</button><button onClick={() => { void runBulk(['1', '2'], 'trash') }}>bulk trash</button><button onClick={() => { void runBulk(['1', '2'], 'mark_read') }}>bulk read</button>
      <button onClick={() => { void run('1', 'archive') }}>archive 1</button><button onClick={() => { void run('2', 'archive') }}>archive 2</button><button onClick={() => { void run('2', 'trash') }}>trash 2</button><button onClick={() => { void run('1', 'spam') }}>spam 1</button><button onClick={() => { void run('1', 'mark_read') }}>read 1</button><button onClick={() => { void run('1', 'mark_unread') }}>unread 1</button>
      <button onClick={() => { void run('2', 'mark_read', { silent: true }) }}>silent read 2</button><button onClick={() => { void run('1', 'mark_read', { silent: true }) }}>silent read 1</button><button onClick={() => notify('refresh failed')}>notify</button><button onClick={() => { void undo() }}>force undo</button>
      <div data-testid="capacity">{String(capacityAvailable)}</div><div data-testid="entries">{entries.map(entry => <button key={entry.actionId} onClick={() => { void undo(entry.actionId) }}>{entry.actionId}</button>)}</div>
      {(latest || notice || permissionNotice) && <div role="status" data-tone={tone}>
        {latest && <><span>{latest.text}</span><button onClick={() => { void undo() }}>Rückgängig</button></>}
        {notice && <span>{notice.text}</span>}
        {permissionNotice && <span>{permissionNotice}</span>}
      </div>}</>
  }
  render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  return { client, ...time }
}
function success() {
  return spyOn(sdk, 'mailMessageAction').mockImplementation((async (options: { path: { message_id: string } }) => ({ data: { action_id: `action-${options?.path?.message_id}`, message: rows.find(row => row.id === options?.path?.message_id) }, response: { status: 200 } })) as never)
}
afterEach(() => { cleanup(); mock.restore(); window.matchMedia = originalMatchMedia })

test('a partial trash reconciles the inbox without a success toast or undo', async () => {
  spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({
    error: { detail: { code: 'provider_partial', folder: 'TRASH' } }, response: { status: 409 },
  })) as never)
  setup()
  fireEvent.click(screen.getByText('trash 2'))
  const status = await screen.findByRole('status')
  expect(status.textContent).toBe('Der Anbieter hat nur einen Teil der Änderung ausgeführt. Die Mail ist im Papierkorb.')
  expect(screen.queryByText('Zweite:false')).toBeNull()
  expect(screen.getByText('Erste:false')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test('permission-denied partial trash keeps the reported trash row and explains both results', async () => {
  spyOn(sdk, 'mailMessageAction').mockResolvedValue({
    error: { detail: { code: 'provider_permission_missing', folder: 'TRASH' } }, response: { status: 409 },
  } as never)
  const { client } = setup()
  const trashKey = ['messages', { folder: 'TRASH' }]
  client.setQueryData<InfiniteData<MessagePageOut>>(trashKey, {
    pages: [{ items: [rows[1]!], next_cursor: null }], pageParams: [undefined],
  })
  fireEvent.click(screen.getByText('trash 2'))
  expect(await screen.findByText(de.inbox.providerPartial.TRASH)).toBeTruthy()
  expect(screen.getByText(de.errors.provider_permission_missing)).toBeTruthy()
  expect(screen.queryByText('Zweite:false')).toBeNull()
  expect(screen.getByText('Erste:false')).toBeTruthy()
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(trashKey)?.pages[0]?.items.map(item => item.id)).toEqual(['2'])
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test.each(['provider_partial', 'provider_permission_missing'] as const)('a reconciled folder explains that Gmail did not move the message (%s)', async code => {
  spyOn(sdk, 'mailMessageAction').mockResolvedValue({
    error: { detail: { code, folder: 'TRASH', reconciled: true } }, response: { status: 409 },
  } as never)
  setup()
  fireEvent.click(screen.getByText('trash 2'))
  const status = await screen.findByRole('status')
  expect(status.textContent).toContain('Die Mail wurde durch diese Aktion nicht verschoben.')
  expect(status.textContent).not.toContain('nur einen Teil')
  expect(status.textContent?.includes(de.errors.provider_permission_missing)).toBe(code === 'provider_permission_missing')
  expect(screen.queryByText('Zweite:false')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test('bulk feedback distinguishes a reconciled row from a partially applied change', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockResolvedValue({
    data: { results: [
      { message_id: '1', status: 'partial', folder: 'TRASH', reconciled: true },
      { message_id: '2', status: 'partial', folder: 'TRASH' },
    ] }, response: { ok: true },
  } as never)
  setup()
  fireEvent.click(screen.getByText('bulk trash'))
  const status = await screen.findByRole('status')
  expect(status.textContent).toContain('1 Mail wurde nicht verschoben.')
  expect(status.textContent).toContain(de.inbox.bulkProviderPartial(1))
  expect(status.textContent).not.toContain(de.inbox.bulkProviderPartial(2))
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test('group undo displays the unsupported hint when all tokens fail verification', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockResolvedValue({
    data: { results: ['1', '2'].map(id => ({ message_id: id, status: 'done', action_id: `action-${id}` })) }, response: { ok: true },
  } as never)
  spyOn(sdk, 'mailActionsUndo').mockResolvedValue({
    data: { restored: [], failed: ['action-1', 'action-2'], skipped: [], expired: [], error_code: 'provider_action_unsupported' },
    response: { status: 200 },
  } as never)
  setup()
  fireEvent.click(screen.getByText('bulk archive'))
  fireEvent.click(await screen.findByRole('button', { name: 'Rückgängig' }))
  expect((await screen.findByRole('status')).textContent).toBe(de.errors.provider_action_unsupported)
  expect(screen.queryByText('Erste:false')).toBeNull()
  expect(screen.queryByText('Zweite:false')).toBeNull()
})


test('permission-denied trash rolls back the inbox and only shows the permission reason', async () => {
  spyOn(sdk, 'mailMessageAction').mockResolvedValue({
    error: { detail: { code: 'provider_permission_missing' } }, response: { status: 403 },
  } as never)
  const { client } = setup()
  const invalidate = spyOn(client, 'invalidateQueries')
  fireEvent.click(screen.getByText('trash 2'))
  expect((await screen.findByRole('status')).textContent).toBe(de.errors.provider_permission_missing)
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['accounts'] })
  expect(screen.getByText('Zweite:false')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test('bulk trash keeps a permission-denied row and adds the reason to the result', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockResolvedValue({
    data: { results: [
      { message_id: '1', status: 'done', action_id: 'trash-1' },
      { message_id: '2', status: 'failed', error_code: 'provider_permission_missing' },
    ] }, response: { ok: true },
  } as never)
  const undo = spyOn(sdk, 'mailActionsUndo').mockResolvedValue({ data: { restored: ['trash-1'] } } as never)
  setup()
  fireEvent.click(screen.getByText('bulk trash'))
  expect(await screen.findByText(de.inbox.bulkResult('trash', 1, 2))).toBeTruthy()
  expect(screen.getByText(de.errors.provider_permission_missing)).toBeTruthy()
  expect(screen.queryByText('Erste:false')).toBeNull()
  expect(screen.getByText('Zweite:false')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Rückgängig' }))
  await screen.findByText('Erste:false')
  expect(undo.mock.calls[0]?.[0]?.body).toEqual({ action_ids: ['trash-1'] })
})

test.each(['single', 'group'] as const)('permission-denied %s undo explains the refusal and leaves the failed row out', async scope => {
  const group = scope === 'group'
  success()
  spyOn(sdk, 'mailMessagesBulkAction').mockResolvedValue({
    data: { results: ['1', '2'].map(id => ({ message_id: id, status: 'done', action_id: `action-${id}` })) }, response: { ok: true },
  } as never)
  const undo = spyOn(sdk, 'mailActionsUndo').mockResolvedValue({
    data: { restored: group ? ['action-2'] : [], failed: ['action-1'], skipped: [], expired: [], error_code: 'provider_permission_missing' },
    response: { status: 200 },
  } as never)
  setup()
  fireEvent.click(screen.getByText(group ? 'bulk archive' : 'archive 1'))
  const undoButton = await screen.findByRole('button', { name: 'Rückgängig' })
  expect(screen.queryByText('Erste:false')).toBeNull()
  if (group) expect(screen.queryByText('Zweite:false')).toBeNull()
  fireEvent.click(undoButton)
  await screen.findByText(de.errors.provider_permission_missing)
  expect(screen.queryByText('Erste:false')).toBeNull()
  expect(screen.getByText('Zweite:false')).toBeTruthy()
  expect(undo.mock.calls[0]?.[0]?.body).toEqual({ action_ids: group ? ['action-1', 'action-2'] : ['action-1'] })
})

test('bulk trash identifies partial moves and offers undo only for completed messages', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((async () => ({
    data: { results: [
      { message_id: '1', status: 'partial', folder: 'TRASH' },
      { message_id: '2', status: 'done', action_id: 'trash-2' },
    ] }, response: { ok: true },
  })) as never)
  const undo = spyOn(sdk, 'mailActionsUndo').mockResolvedValue({ data: { restored: ['trash-2'] } } as never)
  setup()
  fireEvent.click(screen.getByText('bulk trash'))
  const status = await screen.findByRole('status')
  expect(status.textContent).toContain('Der Anbieter hat 1 Mail teilweise geändert. Die Liste zeigt den gemeldeten Ordner.')
  expect(screen.queryByText('Erste:false')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Rückgängig' }))
  await screen.findByText('Zweite:false')
  expect(screen.queryByText('Erste:false')).toBeNull()
  expect(undo.mock.calls[0]?.[0]?.body?.action_ids).toEqual(['trash-2'])
})

test('bulk notices use the proper plural and action wording', () => {
  expect(de.inbox.selectionCount(0)).toBe('Keine ausgewählt')
  expect(de.inbox.selectionCount(1)).toBe('1 ausgewählt')
  expect(de.inbox.selectionCount(2)).toBe('2 ausgewählt')
  expect(de.inbox.bulkResult('archive', 1, 1)).toBe('1 Mail archiviert.')
  expect(de.inbox.bulkResult('archive', 0, 2)).toBe('0 von 2 Mails archiviert. 2 ließen sich nicht archivieren.')
  expect(de.inbox.bulkResult('mark_read', 2, 2)).toBe('2 Mails als gelesen markiert.')
  expect(de.inbox.bulkResult('mark_unread', 2, 2)).toBe('2 Mails als ungelesen markiert.')
  expect(de.inbox.bulkResult('trash', 2, 2)).toBe('2 Mails in den Papierkorb gelegt.')
  expect(de.inbox.bulkResult('trash', 2, 3)).toBe('2 von 3 Mails in den Papierkorb gelegt. Für 1 Mail fehlt die Bestätigung.')
  expect(de.inbox.bulkResult('trash', 0, 2)).toBe('0 von 2 Mails in den Papierkorb gelegt. Für 2 Mails fehlt die Bestätigung.')
  expect(en.inbox.bulkResult('trash', 2, 3)).toBe('Moved 2 of 3 emails to trash. The move is unconfirmed for 1 email.')
  expect(de.inbox.bulkResult('spam', 2, 2)).toBe('2 Mails als Spam markiert.')
  expect(en.inbox.bulkProviderPartial(2)).toBe('The provider partly changed 2 emails. The list shows their reported folders.')
  expect(en.inbox.providerPartial.ARCHIVE).toBe('The provider only applied part of the change. The email is in the archive.')
  expect(en.errors.provider_partial).toBe('The provider only applied part of the change. Check the email’s folder.')
})

test('bulk partial failure rolls back only failed rows and keeps one group undo', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((async () => ({ data: { results: [{ message_id: '1', status: 'done', action_id: 'a1', message: rows[0] }, { message_id: '2', status: 'unsupported', action_id: null, message: null }] }, response: { ok: true } })) as never)
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['a1'], skipped: [], expired: [], failed: [] } })) as never)
  setup(); fireEvent.click(screen.getByText('bulk archive'))
  expect(await screen.findByText('1 von 2 Mails archiviert. 1 ließ sich nicht archivieren.')).toBeTruthy()
  expect(screen.getByRole('status').getAttribute('data-tone')).toBe('danger')
  expect(screen.queryByText('Erste:false')).toBeNull()
  expect(screen.getByText('Zweite:false')).toBeTruthy()
  fireEvent.click(screen.getByText('Rückgängig'))
  await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
  expect(undo.mock.calls[0]?.[0]?.body).toEqual({ action_ids: ['a1'] })
})

test('bulk not_found stays removed while a failed mark_read rolls back its row', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((async () => ({ data: { results: [{ message_id: '1', status: 'not_found', action_id: null, message: null }, { message_id: '2', status: 'failed', action_id: null, message: null }] }, response: { ok: true } })) as never)
  setup(); fireEvent.click(screen.getByText('bulk archive'))
  expect(await screen.findByText('0 von 2 Mails archiviert. 2 ließen sich nicht archivieren.')).toBeTruthy()
  expect(screen.queryByText('Erste:false')).toBeNull()
  expect(screen.getByText('Zweite:false')).toBeTruthy()
})

test('bulk mark_read rolls back a failed row and group undo restores the read state of the rest', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((async () => ({ data: { results: [{ message_id: '1', status: 'done', action_id: 'a1', message: { ...rows[0], is_read: true } }, { message_id: '2', status: 'failed', action_id: null, message: null }] }, response: { ok: true } })) as never)
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['a1'], skipped: [], expired: [], failed: [] } })) as never)
  setup(); fireEvent.click(screen.getByText('bulk read'))
  expect(await screen.findByText('1 von 2 Mails als gelesen markiert. 1 ließ sich nicht als gelesen markieren.')).toBeTruthy()
  expect(screen.getByText('Erste:true')).toBeTruthy()
  expect(screen.getByText('Zweite:false')).toBeTruthy()
  fireEvent.click(screen.getByText('Rückgängig'))
  await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
  expect(screen.getByText('Zweite:false')).toBeTruthy()
})

test('bulk request failure restores all rows', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((async () => { throw new Error('network') }) as never)
  setup(); fireEvent.click(screen.getByText('bulk archive'))
  expect(await screen.findByText('Das hat nicht geklappt. Lade den Posteingang neu, um zu sehen, wo die Mail jetzt liegt.')).toBeTruthy()
  expect(screen.getByText('Erste:false')).toBeTruthy()
  expect(screen.getByText('Zweite:false')).toBeTruthy()
})

test('partial group undo restores only reported actions', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((async () => ({ data: { results: [{ message_id: '1', status: 'done', action_id: 'a1' }, { message_id: '2', status: 'done', action_id: 'a2' }] }, response: { ok: true } })) as never)
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['a2'], skipped: ['a1'], expired: [], failed: [] } })) as never)
  setup(); fireEvent.click(screen.getByText('bulk archive'))
  fireEvent.click(await screen.findByText('Rückgängig'))
  expect(await screen.findByText('1 Aktion ließ sich nicht rückgängig machen.')).toBeTruthy()
  expect(screen.queryByText('Erste:false')).toBeNull()
  expect(screen.getByText('Zweite:false')).toBeTruthy()
})

test('archive removes immediately and toast expires after ten seconds', async () => {
  success(); const time = setup()
  fireEvent.click(screen.getByText('archive 1'))
  await waitFor(() => expect(screen.queryByText('Erste:false')).toBeNull())
  expect(await screen.findByText('„Erste“ archiviert.')).toBeTruthy()
  act(() => time.advance(9999)); expect(screen.getByRole('status')).toBeTruthy()
  act(() => time.advance(1)); expect(screen.queryByRole('status')).toBeNull()
})

test.each(['other status', 'other code', 'network'])('uncertain action failure: %s', async failure => {
  spyOn(sdk, 'mailMessageAction').mockImplementation((async () => {
    if (failure === 'network') throw new TypeError('network')
    return { response: { status: failure === 'other status' ? 500 : 502 }, error: { detail: { code: failure === 'other status' ? 'provider_failed' : 'other' } } }
  }) as never)
  setup(); fireEvent.click(screen.getByText('archive 1'))
  expect(await screen.findByText('Das hat nicht geklappt. Lade den Posteingang neu, um zu sehen, wo die Mail jetzt liegt.')).toBeTruthy()
  expect(screen.getByText('Erste:false')).toBeTruthy()
})

test('second action keeps its own deadline without extending the first', async () => {
  success(); const time = setup()
  fireEvent.click(screen.getByText('archive 1')); await screen.findByText('„Erste“ archiviert.')
  act(() => time.advance(5000))
  fireEvent.click(screen.getByText('trash 2')); await screen.findByText('„Zweite“ in den Papierkorb gelegt.')
  expect(screen.getByText('action-1')).toBeTruthy(); expect(screen.getByText('action-2')).toBeTruthy()
  act(() => time.advance(5000)); expect(screen.queryByText('action-1')).toBeNull(); expect(screen.getByText('action-2')).toBeTruthy()
  act(() => time.advance(5000)); expect(screen.queryByRole('status')).toBeNull()
})

test('notification preserves the undo target until its original deadline', async () => {
  success()
  const undo = spyOn(sdk, 'mailActionsUndo')
  const time = setup()
  fireEvent.click(screen.getByText('archive 1')); await screen.findByText('Rückgängig')
  act(() => time.advance(5000))
  fireEvent.click(screen.getByText('notify'))
  expect(screen.getByRole('status').getAttribute('data-tone')).toBe('danger')
  expect(screen.getByText('refresh failed')).toBeTruthy()
  expect(screen.getByText('Rückgängig')).toBeTruthy()
  expect(screen.getByText(de.inbox.done.archive('Erste'))).toBeTruthy()
  expect(screen.getByText('action-1')).toBeTruthy()
  act(() => time.advance(5000)); expect(screen.queryByText('action-1')).toBeNull()
  fireEvent.click(screen.getByText('force undo')); expect(undo).not.toHaveBeenCalled()
  act(() => time.advance(5000)); expect(screen.queryByRole('status')).toBeNull()
})

test('undo times out, aborts and can be retried even if the request never settles', async () => {
  success()
  let signal: AbortSignal | undefined
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation(((options: { signal?: AbortSignal }) => {
    signal = options.signal
    return new Promise(() => {})
  }) as never)
  const time = setup()
  fireEvent.click(screen.getByText('archive 1'))
  fireEvent.click(await screen.findByText('Rückgängig'))
  expect(signal?.aborted).toBe(false)
  await act(async () => { time.advance(15_000) })
  expect(signal?.aborted).toBe(true)
  expect(screen.getByText('Die Rücknahme dauert zu lange. Ob sie geklappt hat, ist unklar. Versuch es noch mal oder lade den Posteingang neu.')).toBeTruthy()
  expect(screen.getByText('action-1')).toBeTruthy()
  fireEvent.click(screen.getByText('action-1'))
  await waitFor(() => expect(undo).toHaveBeenCalledTimes(2))
})

test('undo sends the action id and restores the row', async () => {
  success(); const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['action-1'], expired: [], failed: [], skipped: [] }, response: { status: 200 } })) as never)
  setup(); fireEvent.click(screen.getByText('archive 1')); fireEvent.click(await screen.findByText('Rückgängig'))
  await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
  expect(undo).toHaveBeenCalledWith({ body: { action_ids: ['action-1'] }, signal: expect.any(AbortSignal) })
  expect(screen.queryByRole('status')).toBeNull()
})

for (const replacement of ['new action', 'timeout']) {
  test(`successful pending undo restores its row after ${replacement}`, async () => {
    success()
    let finishUndo!: (value: unknown) => void
    spyOn(sdk, 'mailActionsUndo').mockImplementation((() => new Promise(resolve => { finishUndo = resolve })) as never)
    const { client, advance } = setup()
    fireEvent.click(screen.getByText('archive 1'))
    fireEvent.click(await screen.findByText('Rückgängig'))
    if (replacement === 'new action') {
      fireEvent.click(screen.getByText('archive 2'))
      await screen.findByText('„Zweite“ archiviert.')
    } else {
      act(() => advance(10_000))
      expect(screen.getByRole('status')).toBeTruthy()
    }
    const invalidate = spyOn(client, 'invalidateQueries')
    await act(async () => { finishUndo({ data: { restored: ['action-1'] }, response: { status: 200 } }) })
    await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['messages'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['summary'] })
    if (replacement === 'new action') {
      expect(screen.queryByText('Zweite:false')).toBeNull()
      expect(screen.getByText('„Zweite“ archiviert.')).toBeTruthy()
      expect(screen.getByText('Rückgängig')).toBeTruthy()
    } else expect(screen.queryByRole('status')).toBeNull()
  })
}

for (const [status, text, removed] of [[502, 'Der Anbieter hat die Änderung nicht bestätigt. Prüfe die Mail nach dem nächsten Abruf.', false], [409, 'Das geht bei dieser Mail nicht.', false], [404, 'Die Mail ist nicht mehr da.', true]] as const) {
  test(`${status} shows a danger notice and ${removed ? 'keeps removal' : 'restores the row'}`, async () => {
    spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ error: { detail: { code: status === 409 ? 'action_unsupported' : status === 404 ? 'message_not_found' : 'provider_failed' } }, response: { status } })) as never)
    setup(); fireEvent.click(screen.getByText('archive 1'))
    expect((await screen.findByRole('status')).getAttribute('data-tone')).toBe('danger')
    expect(screen.getByText(text)).toBeTruthy()
    expect(Boolean(screen.queryByText('Erste:false'))).toBe(!removed)
  })
}

for (const first of ['archive 1', 'read 1']) {
  test(`failed ${first} rolls back without reverting a later archive`, async () => {
    let failFirst!: (value: unknown) => void
    spyOn(sdk, 'mailMessageAction').mockImplementation(((options: { path: { message_id: string } }) => options.path.message_id === '1'
      ? new Promise(resolve => { failFirst = resolve })
      : Promise.resolve({ data: { action_id: 'action-2', message: rows[1] }, response: { status: 200 } })) as never)
    const { client } = setup()
    fireEvent.click(screen.getByText(first))
    await waitFor(() => expect(failFirst).toBeDefined())
    fireEvent.click(screen.getByText('archive 2'))
    await screen.findByText('„Zweite“ archiviert.')
    await act(async () => { failFirst({ response: { status: 502 } }) })
    await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
    expect(screen.queryByText('Zweite:false')).toBeNull()
    expect(screen.getByText('Das hat nicht geklappt. Lade den Posteingang neu, um zu sehen, wo die Mail jetzt liegt.')).toBeTruthy()
    expect(screen.getByText('action-2')).toBeTruthy()
    expect(client.getQueryState(key)?.isInvalidated).toBe(true)
  })
}

test('overlapping archives reconcile only after both requests settle', async () => {
  const finish: Record<string, (value: unknown) => void> = {}
  spyOn(sdk, 'mailMessageAction').mockImplementation(((options: { path: { message_id: string } }) =>
    new Promise(resolve => { finish[options.path.message_id] = resolve })) as never)
  const { client } = setup()
  const invalidate = spyOn(client, 'invalidateQueries')
  const messageInvalidations = () => invalidate.mock.calls.filter(([filters]) => JSON.stringify(filters?.queryKey) === JSON.stringify(['messages']))
  fireEvent.click(screen.getByText('archive 1'))
  await waitFor(() => expect(finish['1']).toBeDefined())
  fireEvent.click(screen.getByText('archive 2'))
  await waitFor(() => expect(finish['2']).toBeDefined())
  await act(async () => { finish['1']!({ response: { status: 502 } }) })
  expect(messageInvalidations()).toHaveLength(0)
  await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
  expect(screen.queryByText('Zweite:false')).toBeNull()
  await act(async () => { finish['2']!({ data: { action_id: 'action-2', message: rows[1] }, response: { status: 200 } }) })
  expect(messageInvalidations()).toHaveLength(1)
  await act(async () => { await client.fetchQuery({ queryKey: key, queryFn: async () => ({ pages: [{ items: [rows[0]], next_cursor: null }], pageParams: [undefined] }) }) })
  await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
  expect(screen.queryByText('Zweite:false')).toBeNull()
})

test('a single successful archive reconciles messages once after settling', async () => {
  let finish!: (value: unknown) => void
  spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => { finish = resolve })) as never)
  const { client } = setup()
  const invalidate = spyOn(client, 'invalidateQueries')
  const messageInvalidations = () => invalidate.mock.calls.filter(([filters]) => JSON.stringify(filters?.queryKey) === JSON.stringify(['messages']))
  fireEvent.click(screen.getByText('archive 1'))
  await waitFor(() => expect(finish).toBeDefined())
  expect(messageInvalidations()).toHaveLength(0)
  await act(async () => { finish({ data: { action_id: 'action-1', message: rows[0] }, response: { status: 200 } }) })
  expect(messageInvalidations()).toHaveLength(1)
})

test('spam removes the row, names the mail and can be undone', async () => {
  const action = success(); setup()
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['action-1'], expired: [], skipped: [], failed: [] } })) as never)
  fireEvent.click(screen.getByText('spam 1'))
  await waitFor(() => expect(screen.queryByText('Erste:false')).toBeNull())
  expect(action).toHaveBeenCalledWith({ path: { message_id: '1' }, body: { kind: 'spam' } })
  expect(await screen.findByText('„Erste“ als Spam markiert.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Rückgängig' }))
  expect(await screen.findByText('Erste:false')).toBeTruthy()
  expect(undo).toHaveBeenCalledWith({ body: { action_ids: ['action-1'] }, signal: expect.any(AbortSignal) })
})

test('spam removes the row before the provider answers and puts it back when the provider fails', async () => {
  let answer!: (value: unknown) => void
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(done => { answer = done })) as never)
  setup()
  fireEvent.click(screen.getByText('spam 1'))
  await waitFor(() => expect(screen.queryByText('Erste:false')).toBeNull())
  expect(action).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('status')).toBeNull()
  await act(async () => { answer({ response: { status: 502 }, error: { detail: { code: 'provider_failed' } } }) })
  expect(await screen.findByText('Erste:false')).toBeTruthy()
  expect(screen.getByRole('status').getAttribute('data-tone')).toBe('danger')
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
})

test('spam on mobile uses the short success text', async () => {
  success(); setup(true); fireEvent.click(screen.getByText('spam 1'))
  expect(await screen.findByText('Mail als Spam markiert.')).toBeTruthy()
})

test('a refused spam action puts the row back and says so', async () => {
  spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ response: { status: 409 }, error: { detail: { code: 'action_unsupported' } } })) as never)
  setup(); fireEvent.click(screen.getByText('spam 1'))
  expect(await screen.findByText('Das geht bei dieser Mail nicht.')).toBeTruthy()
  expect(screen.getByText('Erste:false')).toBeTruthy()
})

test('mobile uses the short success text', async () => {
  success(); setup(true); fireEvent.click(screen.getByText('archive 1'))
  expect(await screen.findByText('Mail archiviert.')).toBeTruthy()
})

test('failed undo shows a danger notice without restoring the row', async () => {
  success()
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: [], expired: ['action-1'], failed: [], skipped: [] }, response: { status: 200 } })) as never)
  setup(); fireEvent.click(screen.getByText('archive 1')); fireEvent.click(await screen.findByText('Rückgängig'))
  expect(await screen.findByText('Die Frist für diese Rücknahme ist abgelaufen.')).toBeTruthy()
  expect(screen.getByRole('status').getAttribute('data-tone')).toBe('danger')
  expect(screen.queryByText('Erste:false')).toBeNull()
})

for (const outcome of ['success', 'failure'] as const) test(`runs on the same message are ordered after ${outcome} while other messages run concurrently`, async () => {
  const finish: Array<(value: unknown) => void> = []
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => { finish.push(resolve) })) as never)
  setup()
  fireEvent.click(screen.getByText('read 1'))
  await waitFor(() => expect(action.mock.calls.length).toBe(1))
  fireEvent.click(screen.getByText('archive 1'))
  fireEvent.click(screen.getByText('archive 2'))
  await waitFor(() => expect(action.mock.calls.length).toBe(2))
  expect(action.mock.calls.map(([options]) => options?.path?.message_id)).toEqual(['1', '2'])
  expect(screen.getByText('Erste:true')).toBeTruthy()
  await act(async () => { finish[0]!(outcome === 'success' ? { data: { action_id: 'read-1', message: rows[0] }, response: { status: 200 } } : { response: { status: 502 } }) })
  await waitFor(() => expect(action.mock.calls.length).toBe(3))
  expect(action.mock.calls[2]?.[0]?.body).toEqual({ kind: 'archive' })
  await act(async () => {
    finish[1]!({ data: { action_id: 'action-2', message: rows[1] }, response: { status: 200 } })
    finish[2]!({ data: { action_id: 'action-1', message: rows[0] }, response: { status: 200 } })
  })
})

test('mark_read updates the row in place and offers undo', async () => {
  success(); setup(); fireEvent.click(screen.getByText('read 1'))
  await waitFor(() => expect(screen.getByText('Erste:true')).toBeTruthy())
  expect(await screen.findByText('„Erste“ als gelesen markiert.')).toBeTruthy()
})


test('silent run keeps an existing undo toast and target', async () => {
  success()
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['action-1'] }, response: { status: 200 } })) as never)
  setup(); fireEvent.click(screen.getByText('archive 1')); await screen.findByText('Rückgängig')
  fireEvent.click(screen.getByText('silent read 2'))
  await waitFor(() => expect(screen.getByText('Zweite:true')).toBeTruthy())
  fireEvent.click(screen.getByText('Rückgängig'))
  await waitFor(() => expect(undo).toHaveBeenCalled())
})

test('failed silent run rolls back without a toast', async () => {
  let finish!: (value: unknown) => void
  spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => { finish = resolve })) as never)
  setup(); fireEvent.click(screen.getByText('silent read 1'))
  await waitFor(() => expect(screen.getByText('Erste:true')).toBeTruthy())
  await act(async () => { finish({ response: { status: 502 } }) })
  await waitFor(() => expect(screen.getByText('Erste:false')).toBeTruthy())
  expect(screen.queryByRole('status')).toBeNull()
})

test('read flip updates detail cache and rolls it back on failure', async () => {
  let finish!: (value: unknown) => void
  spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => { finish = resolve })) as never)
  const { client } = setup(); client.setQueryData(['message', '1'], { id: '1', is_read: false })
  fireEvent.click(screen.getByText('read 1'))
  await waitFor(() => expect(client.getQueryData(['message', '1'])).toEqual({ id: '1', is_read: true }))
  await act(async () => { finish({ response: { status: 502 } }) })
  expect(client.getQueryData(['message', '1'])).toEqual({ id: '1', is_read: false })
})

test('read undo restores list and detail without inserting a filtered-out row', async () => {
  success()
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['action-1'], expired: [], failed: [], skipped: [] } })) as never)
  const { client } = setup()
  const filtered = ['messages', { unread: true }]
  client.setQueryData(filtered, { pages: [{ items: [rows[0]], next_cursor: null }], pageParams: [undefined] })
  client.setQueryData(['message', '1'], { id: '1', is_read: false })
  const invalidate = spyOn(client, 'invalidateQueries')
  fireEvent.click(screen.getByText('read 1')); await screen.findByText('Rückgängig')
  client.setQueryData(filtered, { pages: [{ items: [], next_cursor: null }], pageParams: [undefined] })
  fireEvent.click(screen.getByText('Rückgängig'))
  await waitFor(() => expect(client.getQueryData(['message', '1'])).toEqual({ id: '1', is_read: false }))
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(key)?.pages[0]?.items).toHaveLength(2)
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(filtered)?.pages[0]?.items).toHaveLength(0)
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['messages'] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['message', '1'] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['summary'] })
})

test('mark unread can be undone in the list and detail', async () => {
  success()
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['action-1'], expired: [], failed: [], skipped: [] } })) as never)
  const { client } = setup()
  client.setQueryData<InfiniteData<MessagePageOut>>(key, { pages: [{ items: [{ ...rows[0]!, is_read: true }, rows[1]!], next_cursor: null }], pageParams: [undefined] })
  client.setQueryData(['message', '1'], { id: '1', is_read: true })
  fireEvent.click(screen.getByText('unread 1')); await screen.findByText('Rückgängig')
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(key)?.pages[0]?.items[0]?.is_read).toBe(false)
  fireEvent.click(screen.getByText('Rückgängig'))
  await waitFor(() => expect(client.getQueryData(['message', '1'])).toEqual({ id: '1', is_read: true }))
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(key)?.pages[0]?.items[0]?.is_read).toBe(true)
})

test('an in-flight list fetch cannot overwrite an optimistic removal', async () => {
  success(); const { client } = setup()
  let finish!: (value: unknown) => void
  const fetching = client.fetchQuery({ queryKey: key, queryFn: () => new Promise(resolve => { finish = resolve }) }).catch(() => {})
  await waitFor(() => expect(finish).toBeDefined())
  fireEvent.click(screen.getByText('archive 1')); await screen.findByText('Rückgängig')
  await act(async () => { finish({ pages: [{ items: rows, next_cursor: null }], pageParams: [undefined] }); await fetching })
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(key)?.pages[0]?.items.some(row => row.id === '1')).toBe(false)
})

test.each(['skipped', 'expired', 'failed'] as const)('undo result %s never applies an old snapshot', async outcome => {
  success()
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: [], expired: [], failed: [], skipped: [], [outcome]: ['action-1'] } })) as never)
  const { client } = setup(); fireEvent.click(screen.getByText('archive 1')); await screen.findByText('Rückgängig')
  fireEvent.click(screen.getByText('Rückgängig'))
  await waitFor(() => expect(screen.getByRole('status').getAttribute('data-tone')).toBe('danger'))
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(key)?.pages[0]?.items.some(row => row.id === '1')).toBe(false)
  expect(screen.queryByText('action-1')).toBeNull()
})

test('a newer action on the same mail finishes before the older undo and a skip keeps its state', async () => {
  let finish!: (value: unknown) => void
  let calls = 0
  spyOn(sdk, 'mailMessageAction').mockImplementation((() => ++calls === 1
    ? Promise.resolve({ data: { action_id: 'action-1', message: rows[0] }, response: { status: 200 } })
    : new Promise(resolve => { finish = resolve })) as never)
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: [], expired: [], failed: [], skipped: ['action-1'] } })) as never)
  const { client } = setup()
  fireEvent.click(screen.getByText('read 1')); await screen.findByText('action-1')
  fireEvent.click(screen.getByText('archive 1')); await waitFor(() => expect(finish).toBeDefined())
  fireEvent.click(screen.getByText('action-1')); expect(undo).not.toHaveBeenCalled()
  await act(async () => finish({ data: { action_id: 'action-new', message: rows[0] }, response: { status: 200 } }))
  await waitFor(() => expect(undo).toHaveBeenCalledTimes(1))
  expect(screen.getByText('Diese Aktion lässt sich nicht mehr rückgängig machen. Die Mail wurde inzwischen geändert.')).toBeTruthy()
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(key)?.pages[0]?.items.some(row => row.id === '1')).toBe(false)
})

test('undo prevents duplicate requests and does not duplicate a row fetched during undo', async () => {
  success(); let finish!: (value: unknown) => void
  const request = spyOn(sdk, 'mailActionsUndo').mockImplementation((() => new Promise(resolve => { finish = resolve })) as never)
  const { client } = setup(); fireEvent.click(screen.getByText('archive 1')); await screen.findByText('Rückgängig')
  fireEvent.click(screen.getByText('action-1')); fireEvent.click(screen.getByText('action-1'))
  expect(request).toHaveBeenCalledTimes(1)
  client.setQueryData(key, { pages: [{ items: [rows[1]], next_cursor: 'next' }, { items: [rows[0]], next_cursor: null }], pageParams: [undefined, 'next'] })
  await act(async () => finish({ data: { restored: ['action-1'], expired: [], failed: [], skipped: [] } }))
  expect(client.getQueryData<InfiniteData<MessagePageOut>>(key)?.pages.flatMap(page => page.items).filter(row => row.id === '1')).toHaveLength(1)
})

test('ten independent actions retain their deadlines and individual targets', async () => {
  let actions!: ReturnType<typeof useMailActions>
  const time = makeClock(), client = new QueryClient()
  spyOn(sdk, 'mailMessageAction').mockImplementation((async (options: { path: { message_id: string } }) => ({ data: { action_id: `a-${options.path.message_id}`, message: { subject: `Mail ${options.path.message_id}` } }, response: { status: 200 } })) as never)
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async (options: { body: { action_ids: string[] } }) => ({ data: { restored: options.body.action_ids, expired: [], failed: [], skipped: [] } })) as never)
  function Harness() { actions = useMailActions({ clock: time.clock }); return null }
  render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  for (let i = 0; i < 10; i++) { await act(async () => { await actions.run(String(i), 'archive') }); act(() => time.advance(500)) }
  expect(actions.entries).toHaveLength(10)
  await act(async () => { await actions.undo('a-5') })
  expect(undo).toHaveBeenCalledWith({ body: { action_ids: ['a-5'] }, signal: expect.any(AbortSignal) })
  act(() => time.advance(5000)); expect(actions.entries.some(entry => entry.actionId === 'a-0')).toBe(false)
  expect(actions.entries.some(entry => entry.actionId === 'a-1')).toBe(true)
  act(() => time.advance(4500)); expect(actions.entries).toHaveLength(0)
})

test('capacity reserves slots for in-flight actions and becomes available on expiry', async () => {
  let actions!: ReturnType<typeof useMailActions>
  const time = makeClock(), client = new QueryClient()
  const finishes: Array<(value: unknown) => void> = []
  const request = spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => finishes.push(resolve))) as never)
  function Harness() { actions = useMailActions({ clock: time.clock }); return null }
  render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  act(() => { for (let i = 0; i < 20; i++) void actions.run(String(i), 'archive') })
  await waitFor(() => expect(request).toHaveBeenCalledTimes(20))
  expect(actions.capacityAvailable).toBe(false)
  let refused: boolean | undefined
  await act(async () => { refused = await actions.run('extra', 'archive') })
  expect(refused).toBe(false)
  expect(request).toHaveBeenCalledTimes(20)
  await act(async () => { finishes.forEach((finish, i) => finish({ data: { action_id: `a-${i}`, message: { subject: `Mail ${i}` } }, response: { status: 200 } })) })
  expect(actions.entries).toHaveLength(20); expect(actions.capacityAvailable).toBe(false)
  act(() => time.advance(10_000))
  expect(actions.entries).toHaveLength(0); expect(actions.capacityAvailable).toBe(true)
})

test('multiple actions use a portal menu with separate undo items and keyboard focus', async () => {
  window.matchMedia = ((query: string) => ({ matches: query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  success(); const client = new QueryClient()
  function Trigger() { const { run } = useMailActionsContext(); return <><button onClick={() => { void run('1', 'archive') }}>one</button><button onClick={() => { void run('2', 'trash') }}>two</button></> }
  render(<QueryClientProvider client={client}><MailActionsProvider><Trigger /><ActionToast /></MailActionsProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('one')); await screen.findByText('Rückgängig')
  fireEvent.click(screen.getByText('two')); const trigger = await screen.findByText('Letzte Aktionen (2)')
  fireEvent.click(trigger); const menu = screen.getByRole('menu')
  expect(menu.parentElement).toBe(document.body)
  expect(screen.getAllByRole('menuitem')).toHaveLength(2)
  expect(screen.getAllByRole('menuitem')[0]?.textContent).toBe('Rückgängig: „Erste“ archiviert')
  await waitFor(() => expect(document.activeElement).toBe(screen.getAllByRole('menuitem')[0] ?? null))
  fireEvent.keyDown(menu, { key: 'Escape' }); expect(document.activeElement).toBe(trigger)
})

test('a menu item whose undo is running stays focusable and says why', async () => {
  window.matchMedia = ((query: string) => ({ matches: query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  success()
  spyOn(sdk, 'mailActionsUndo').mockImplementation((() => new Promise(() => {})) as never)
  const client = new QueryClient()
  function Trigger() { const { run } = useMailActionsContext(); return <><button onClick={() => { void run('1', 'archive') }}>one</button><button onClick={() => { void run('2', 'trash') }}>two</button></> }
  render(<QueryClientProvider client={client}><MailActionsProvider><Trigger /><ActionToast /></MailActionsProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('one')); await screen.findByText('Rückgängig')
  fireEvent.click(screen.getByText('two')); fireEvent.click(await screen.findByText('Letzte Aktionen (2)'))
  fireEvent.click(screen.getAllByRole('menuitem')[0]!)
  fireEvent.click(await screen.findByText('Letzte Aktionen (2)'))
  const item = screen.getAllByRole('menuitem')[0]!
  expect(item.getAttribute('aria-disabled')).toBe('true')
  expect(item.textContent).toContain('Wird zurückgenommen …')
})

test('focus moves to the remaining undo when a menu item completes', async () => {
  window.matchMedia = ((query: string) => ({ matches: query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  success()
  spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['action-1'], expired: [], failed: [], skipped: [] } })) as never)
  const client = new QueryClient()
  function Trigger() { const { run } = useMailActionsContext(); return <><button onClick={() => { void run('1', 'archive') }}>one</button><button onClick={() => { void run('2', 'trash') }}>two</button></> }
  render(<QueryClientProvider client={client}><MailActionsProvider><Trigger /><ActionToast /></MailActionsProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('one')); await screen.findByText('Rückgängig')
  fireEvent.click(screen.getByText('two')); fireEvent.click(await screen.findByText('Letzte Aktionen (2)'))
  fireEvent.click(screen.getAllByRole('menuitem')[0]!)
  await waitFor(() => expect(document.activeElement).toBe(screen.getByText('Rückgängig')))
})

test('two provider consumers share a single toast', async () => {
  window.matchMedia = ((query: string) => ({ matches: query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  success()
  const client = new QueryClient()
  function Trigger() { const { run } = useMailActionsContext(); return <button onClick={() => { void run('1', 'archive') }}>shared archive</button> }
  function Display() { const { entries } = useMailActionsContext(); return <div>{entries.at(-1)?.text}</div> }
  render(<QueryClientProvider client={client}><MailActionsProvider><Trigger /><Display /></MailActionsProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('shared archive'))
  expect(await screen.findByText('„Erste“ archiviert.')).toBeTruthy()
})

test('permission guidance stays open alongside later success, Undo and failure feedback', async () => {
  window.matchMedia = ((query: string) => ({ matches: query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  const action = success()
  const time = makeClock()
  const client = new QueryClient()
  function Trigger() {
    const { run } = useMailActionsContext()
    return <><button onClick={() => { void run('2', 'trash') }}>trash 2</button>
      <button onClick={() => { void run('1', 'archive') }}>archive 1</button></>
  }
  render(<QueryClientProvider client={client}><MailActionsProvider clock={time.clock}><Trigger /><ActionToast /></MailActionsProvider></QueryClientProvider>)
  action.mockResolvedValueOnce({ error: { detail: { code: 'provider_permission_missing' } }, response: { status: 403 } } as never)
  fireEvent.click(screen.getByText('trash 2'))
  const guidance = await screen.findByText(de.errors.provider_permission_missing)

  fireEvent.click(screen.getByText('archive 1'))
  const archived = await screen.findByText(de.inbox.done.archive('Erste'))
  expect(within(archived.closest('.le-notice') as HTMLElement).getByRole('button', { name: 'Rückgängig' })).toBeTruthy()
  expect(screen.getByText(de.errors.provider_permission_missing)).toBe(guidance)

  action.mockResolvedValueOnce({ error: { detail: { code: 'provider_failed' } }, response: { status: 502 } } as never)
  fireEvent.click(screen.getByText('trash 2'))
  expect(await screen.findByText(de.errors.provider_failed)).toBeTruthy()
  expect(screen.getByText(de.errors.provider_permission_missing)).toBe(guidance)
  expect(within(archived.closest('.le-notice') as HTMLElement).getByRole('button', { name: 'Rückgängig' })).toBeTruthy()

  act(() => time.advance(10_000))
  expect(screen.queryByText(de.errors.provider_failed)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Rückgängig' })).toBeNull()
  expect(screen.getByText(de.errors.provider_permission_missing)).toBe(guidance)
  fireEvent.click(screen.getByRole('button', { name: 'Hinweis schließen' }))
  expect(screen.queryByText(de.errors.provider_permission_missing)).toBeNull()
})

test('bulk undo explains a mixed unsupported and skipped outcome', async () => {
  spyOn(sdk, 'mailMessagesBulkAction').mockResolvedValue({
    data: { results: ['1', '2'].map(id => ({ message_id: id, status: 'done', action_id: `action-${id}` })) }, response: { ok: true },
  } as never)
  spyOn(sdk, 'mailActionsUndo').mockResolvedValue({
    data: { restored: [], failed: ['action-1'], unsupported: ['action-1'], expired: [], skipped: ['action-2'] },
  } as never)
  setup()
  fireEvent.click(screen.getByText('bulk archive'))
  fireEvent.click(await screen.findByRole('button', { name: 'Rückgängig' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain(de.errors.provider_action_unsupported))
  expect(screen.getByRole('status').textContent).toContain(de.inbox.bulkUndoFailed(1))
})

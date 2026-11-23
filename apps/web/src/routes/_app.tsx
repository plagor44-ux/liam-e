import { createFileRoute, Outlet, useLocation, useParams } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { mailSyncAll } from '../api/generated/sdk.gen'
import { summaryQuery } from '../api/queries'
import { AppShell, BrandMark, Drawer, IconButton } from '../ds'
import { useI18n } from '../i18n/I18n'
import { Sidebar, Conversations } from '../shell/Sidebar'
import { MobileHeader, MobileModelSlot } from '../shell/MobileHeader'
import { useBreakpoint } from '../shell/useBreakpoint'
import { ChatSessionProvider } from '../chat/session'
import { foldersQuery } from '../settings/mailQueries'

let started = false
export function resetShellSyncForTest() { started = false }
export function Shell() {
  const { texts } = useI18n()
  const breakpoint = useBreakpoint()
  const [open, setOpen] = useState(false)
  const [modelSlot, setModelSlot] = useState<HTMLSpanElement | null>(null)
  const client = useQueryClient()
  const { data: summary } = useQuery(summaryQuery())
  const previousSummary = useRef<{ syncing: boolean, lastSync: number | null } | null>(null)
  useEffect(() => {
    if (!summary) return
    const syncing = summary.accounts.some(account => account.state === 'syncing')
    const parsed = summary.last_sync_at ? Date.parse(summary.last_sync_at) : NaN
    const lastSync = Number.isNaN(parsed) ? null : parsed
    const previous = previousSummary.current
    if (previous && ((previous.syncing && !syncing) || (lastSync !== null && (previous.lastSync === null || lastSync > previous.lastSync)))) {
      void client.invalidateQueries({ queryKey: ['messages'] })
    }
    previousSummary.current = { syncing, lastSync }
  }, [client, summary])
  const path = useLocation({ select: location => location.pathname })
  const search = useLocation({ select: location => location.search }) as { app_folder?: string; app_label?: string }
  // The phone bar restarts from the open conversation only; every other page keeps the start page as it is.
  // The id comes from the matched route, so any spelling the router accepts (`/CHAT/<id>`) carries state.
  const { conversationId } = useParams({ strict: false }) as { conversationId?: string }
  useEffect(() => {
    if (started) return
    started = true
    void mailSyncAll({ body: { only_if_older_than_minutes: 10 } }).then(result => { if (!result.error) void client.invalidateQueries({ queryKey: summaryQuery().queryKey }) }).catch(() => {})
  }, [client])
  const mobile = breakpoint === 'mobile', tablet = breakpoint === 'tablet'
  const destinations = useQuery({ ...foldersQuery(), enabled: mobile && path === '/mail' && Boolean(search.app_folder || search.app_label) })
  const destination = destinations.data?.find(row => row.id === (search.app_folder ?? search.app_label))
  const title = path.startsWith('/calendar') ? texts.calendar.title : path.startsWith('/settings') ? texts.prompts.settings : path.startsWith('/mail') ? destination ? `${destination.kind === 'folder' ? texts.mailFiling.folder : texts.mailFiling.label}: ${destination.name}` : texts.shell.inbox : path.startsWith('/accounts') ? texts.shell.accounts : path.startsWith('/tasks') ? texts.tasks.title : texts.shell.chat
  return <ChatSessionProvider><MobileModelSlot value={mobile ? modelSlot : null}><AppShell sidebar={mobile ? null : <Sidebar collapsed={tablet} onConversations={() => setOpen(true)} />} header={mobile && !(path.startsWith('/mail/') && path !== '/mail/') ? <MobileHeader title={title} conversationId={conversationId} onMenu={() => setOpen(true)} modelSlotRef={path === '/' || conversationId ? setModelSlot : undefined} /> : undefined}><div key={path === '/' || path.startsWith('/chat/') ? 'chat' : path.startsWith('/mail') ? 'mail' : path.startsWith('/calendar') ? 'calendar' : path} className="le-page-fade"><Outlet /></div></AppShell></MobileModelSlot><Drawer open={open} onClose={() => setOpen(false)} label={mobile ? texts.shell.menu : texts.shell.conversations}><div className="le-brand le-drawer-brand"><BrandMark /><span className="le-grow">{texts.assistant.name}</span><IconButton icon="x" label={texts.shell.closeMenu} onClick={() => setOpen(false)} /></div>{mobile ? <Sidebar hideBrand onNavigate={() => setOpen(false)} /> : <Conversations onNavigate={() => setOpen(false)} />}</Drawer></ChatSessionProvider>
}
export const Route = createFileRoute('/_app')({ component: Shell })

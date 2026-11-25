import { infiniteQueryOptions, queryOptions, type QueryClient } from '@tanstack/react-query'
import { promptsList, promptGet, skillsList, skillGet, skillToolsList, mailSummary, calendarSettingsGet, chatModelSettingsGet, chatSettingsGet, chatMailReferenceGet, chatConversationsList, mailAccountsList, mailAccountOptions, mailConnectorsList, mailMessagesList, mailMessageGet, mailMessageInsight, chatHistory, chatModelsList, settingsUiGet, tasksGet, tasksList, tasksOccurrencesList, calendarSourcesList, calendarOccurrencesList, calendarOccurrenceGet } from './generated/sdk.gen'
import { REFERENCE_LOCALE, type Locale } from '../i18n/locales'
import type { SourceOut, MailMessagesListData, TasksGetData, TasksListData, TasksOccurrencesListData, UiSettingsOut } from './generated/types.gen'
import { calendarIndexCache, readCalendarIndex, refreshChangedIndexes, sourceScope } from '../calendar/cache'
import { dataOr404, unwrap } from './unwrap'

export class MessageNotFoundError extends Error {}

export const promptsQuery = () => queryOptions({ queryKey: ['prompts'], refetchOnWindowFocus: 'always', queryFn: () => unwrap(promptsList({ throwOnError: true })) })
export const promptQuery = (id: string) => queryOptions({ queryKey: ['prompts', id], refetchOnWindowFocus: 'always', queryFn: () => unwrap(promptGet({ path: { id }, throwOnError: true })) })
export const skillsQuery = () => queryOptions({ queryKey: ['skills'], refetchOnWindowFocus: 'always', queryFn: () => unwrap(skillsList({ throwOnError: true })) })
// The registry changes only with a release; a skill card needs it for the kind of each tool.
export const skillToolsQuery = () => queryOptions({ queryKey: ['skill-tools'], staleTime: 10 * 60_000, queryFn: () => unwrap(skillToolsList({ throwOnError: true })) })
export const skillQuery = (id: string) => queryOptions({ queryKey: ['skills', id], refetchOnWindowFocus: 'always', queryFn: () => unwrap(skillGet({ path: { skill_id: id }, throwOnError: true })) })

// Poll fast while an account syncs. While idle, keep a slow poll so syncs the worker schedules on
// its own (every few minutes) still refresh unread counts, the inbox and the sync time. Polling
// pauses while the tab is hidden; a refetch on window focus covers the return.
export const SUMMARY_SYNCING_POLL_MS = 3_000
export const SUMMARY_IDLE_POLL_MS = 60_000
export const summaryQuery = (chatScope = false) => queryOptions({ queryKey: chatScope ? ['summary', 'chat'] : ['summary'], queryFn: ({ signal }) => unwrap(mailSummary({ query: chatScope ? { chat_scope: true } : undefined, signal, throwOnError: true })), refetchInterval: query => !query.state.data ? false : query.state.data.accounts.some(a => a.state === 'syncing') ? SUMMARY_SYNCING_POLL_MS : SUMMARY_IDLE_POLL_MS })
export const chatSettingsQuery = () => queryOptions({ queryKey: ['chat-settings'], refetchOnWindowFocus: 'always', queryFn: ({ signal }) => unwrap(chatSettingsGet({ signal, throwOnError: true })) })
export const chatModelSettingsQuery = () => queryOptions({ queryKey: ['chat-model-settings'], refetchOnWindowFocus: 'always', queryFn: ({ signal }) => unwrap(chatModelSettingsGet({ signal, throwOnError: true })) })
export const conversationsQuery = () => queryOptions({ queryKey: ['conversations'], refetchOnWindowFocus: 'always', queryFn: () => unwrap(chatConversationsList({ throwOnError: true })) })
export const accountsQuery = () => queryOptions({ queryKey: ['accounts'], queryFn: () => unwrap(mailAccountsList({ throwOnError: true })) })
export const accountOptionsQuery = () => queryOptions({ queryKey: ['account-options'], queryFn: () => unwrap(mailAccountOptions({ throwOnError: true })) })
export const connectorsQuery = () => queryOptions({ queryKey: ['connectors'], queryFn: () => unwrap(mailConnectorsList({ throwOnError: true })) })
export const messagesInfiniteQuery = (filters: Omit<NonNullable<MailMessagesListData['query']>, 'cursor'>) => infiniteQueryOptions({ queryKey: ['messages', filters], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => unwrap(mailMessagesList({ query: { ...filters, cursor: pageParam }, throwOnError: true })), getNextPageParam: page => page.next_cursor ?? undefined })
export const messageQuery = (id: string) => queryOptions({ queryKey: ['message', id], queryFn: async () => dataOr404(await mailMessageGet({ path: { message_id: id } }), () => new MessageNotFoundError()) })
export const chatMailReferenceQuery = (id: string, version?: number | null, contextId?: string) => queryOptions({
  queryKey: ['chat-mail-reference', id, version, contextId ?? null],
  enabled: Boolean(id) && version != null,
  retry: (count: number, error: Error) => !(error instanceof MessageNotFoundError) && count < 2,
  queryFn: async ({ signal }) => dataOr404(await chatMailReferenceGet({
    path: { message_id: id }, query: { expected_version: version!, context_id: contextId }, signal,
  }), () => new MessageNotFoundError()),
})
export const insightQuery = (id: string, locale: Locale = REFERENCE_LOCALE) => queryOptions({ queryKey: ['insight', id], queryFn: () => unwrap(mailMessageInsight({ path: { message_id: id }, query: { locale }, throwOnError: true })) })
export const historyQuery = (conversationId: string) => queryOptions({ queryKey: ['history', conversationId], queryFn: () => unwrap(chatHistory({ query: { conversation_id: conversationId }, throwOnError: true })) })
export const modelsQuery = () => queryOptions({ queryKey: ['models'], refetchOnWindowFocus: 'always', queryFn: () => unwrap(chatModelsList({ throwOnError: true })), staleTime: 60_000 })

// The confirmed device-independent UI settings; a second device reads the same row.
export const uiSettingsQuery = () => queryOptions({ queryKey: ['ui-settings'], refetchOnWindowFocus: 'always', queryFn: ({ signal }) => unwrap(settingsUiGet({ signal, throwOnError: true })) })

/** Apply a confirmed PATCH without allowing its delayed reply to roll back a newer language or swipe setting. */
export async function cacheUiSettings(client: QueryClient, saved: UiSettingsOut): Promise<UiSettingsOut> {
  const { queryKey } = uiSettingsQuery()
  await client.cancelQueries({ queryKey, exact: true })
  return client.setQueryData<UiSettingsOut>(queryKey, cached => cached && cached.revision > saved.revision ? cached : saved) ?? saved
}

// Task queries use their own scope; inbox filters never affect this data.
export const taskListQuery = (query: Omit<TasksListData['query'], 'cursor'>) => infiniteQueryOptions({ queryKey: ['tasks', 'list', query], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => unwrap(tasksList({ query: { ...query, cursor: pageParam }, throwOnError: true })), getNextPageParam: page => page.next_cursor ?? undefined })
export const taskOccurrencesQuery = (query: Omit<NonNullable<TasksOccurrencesListData['query']>, 'cursor'>) => infiniteQueryOptions({ queryKey: ['tasks', 'occurrences', query], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => unwrap(tasksOccurrencesList({ query: { ...query, cursor: pageParam }, throwOnError: true })), getNextPageParam: page => page.next_cursor ?? undefined })
export const taskDetailQuery = (id: string, query: Omit<TasksGetData['query'], 'cursor'>) => infiniteQueryOptions({ queryKey: ['tasks', 'detail', id, query], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => unwrap(tasksGet({ path: { series_id: id }, query: { ...query, cursor: pageParam }, throwOnError: true })), getNextPageParam: page => page.occurrences.next_cursor ?? undefined })

// Calendar reads use the index only; refreshes are explicit management operations.
export const calendarSourcesQuery = () => queryOptions({
  queryKey: ['calendar', 'sources'],
  gcTime: calendarIndexCache.gcTime,
  queryFn: async context => {
    const { client, queryKey, signal } = context
    const sources = await readCalendarIndex(context, calendarSourcesList({ signal }))
    const previous = client.getQueryData<SourceOut[]>(queryKey)
    if (previous && !signal.aborted) await refreshChangedIndexes(client, previous, sources)
    return sources
  },
})
// Access may change in another tab while this one still has a fresh index cache.
export const chatCalendarSourcesQuery = () => ({
  ...calendarSourcesQuery(),
  refetchOnWindowFocus: 'always' as const,
})
export const calendarPageQuery = (from: string, to: string, source?: string, cursor?: string) => queryOptions({ queryKey: ['calendar', 'occurrences', from, to, source, cursor], queryFn: () => unwrap(calendarOccurrencesList({ query: { from, to, source_ids: source ? [source] : undefined, cursor, limit: 50 }, throwOnError: true })) })
export const calendarDetailQuery = (id: string, chatScope = false) => queryOptions({
  queryKey: ['calendar', chatScope ? 'chat-occurrence' : 'occurrence', id],
  queryFn: () => unwrap(calendarOccurrenceGet({ path: { occurrence_id: id }, query: chatScope ? { chat_scope: true } : undefined, throwOnError: true })),
})
export const calendarSettingsQuery = () => queryOptions({
  queryKey: ['calendar-settings'], refetchOnWindowFocus: 'always',
  queryFn: ({ signal }) => unwrap(calendarSettingsGet({ signal, throwOnError: true })),
})
import { queryWindows } from '../calendar/state'
export const calendarAgendaQuery = (from: string, to: string, timeZone: string, sources: SourceOut[], source?: string) => {
  const windows = queryWindows(from, to, timeZone)
  const scope = sourceScope(sources)
  return infiniteQueryOptions({
    queryKey: ['calendar', 'agenda', from, to, timeZone, source, scope] as const,
    ...calendarIndexCache,
    meta: { calendarSourceIds: sources.map(s => s.id) },
    initialPageParam: { window: 0, cursor: undefined as string | undefined },
    queryFn: async context => {
      const { pageParam, signal } = context
      const window = windows[pageParam.window]
      if (!window) throw new Error('invalid calendar window')
      const page = await readCalendarIndex(context, calendarOccurrencesList({ signal, query: { ...window, source_ids: source ? [source] : undefined, cursor: pageParam.cursor, limit: 50 } }))
      return { ...page, window: pageParam.window }
    },
    placeholderData: (previous, query) => {
      const key = query?.queryKey
      if (!key || key[2] !== from || key[3] !== to || key[4] !== timeZone || key[5] !== source) return
      const prior = new Map(key[6])
      // While a narrower selection loads, the agenda can still show its permitted rows.
      if (scope.every(([id, revision]) => prior.get(id) === revision)) return previous
    },
    getNextPageParam: page => page.next_cursor ? { window: page.window, cursor: page.next_cursor } : page.window + 1 < windows.length ? { window: page.window + 1, cursor: undefined } : undefined,
  })
}

import { z } from 'zod'
import type { MailMessagesListData } from '../api/generated/types.gen'

export const inboxSearchSchema = z.object({
  account: z.array(z.string()).optional().catch(undefined),
  unread: z.boolean().optional().catch(undefined),
  important: z.boolean().optional().catch(undefined),
  attachments: z.boolean().optional().catch(undefined),
  q: z.string().optional().catch(undefined),
  app_folder: z.string().optional().catch(undefined),
  app_label: z.string().optional().catch(undefined),
})
export type InboxSearch = z.infer<typeof inboxSearchSchema>

export function hasActiveFilters(search: InboxSearch): boolean {
  return Boolean(search.account?.length || search.unread || search.important || search.attachments || search.q?.trim() || search.app_folder || search.app_label)
}

export function toMessageFilters(search: InboxSearch): Omit<NonNullable<MailMessagesListData['query']>, 'cursor'> {
  const q = search.q?.trim()
  return {
    ...(search.account?.length ? { account_id: search.account } : {}),
    ...(search.unread ? { unread: true } : {}),
    ...(search.important ? { important: true } : {}),
    ...(search.attachments ? { attachments: true } : {}),
    ...(q ? { q } : {}),
    ...(search.app_folder ? { app_folder_id: search.app_folder } : {}),
    ...(search.app_label ? { app_label_id: search.app_label } : {}),
  }
}

import type { MailActionCapabilities } from '../api/generated/types.gen'

export function mailCapabilities(supported = true): MailActionCapabilities {
  return { archive: supported, trash: supported, restore: supported, spam: supported,
    mark_read: supported, mark_unread: supported, move: supported, label: supported }
}

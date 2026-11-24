import type { AccountSummaryOut } from '../api/generated/types.gen'
import type { Catalogue } from '../i18n/catalogue'

export function gmailActionHint(account: AccountSummaryOut | undefined, texts: Catalogue) {
  if (account?.connection !== 'connector' || !['gmail', 'google_mail'].includes(account.provider)) return
  return account.provider_actions_blocked ? texts.mail.gmailActionsPaused : texts.mail.gmailActionsPending
}

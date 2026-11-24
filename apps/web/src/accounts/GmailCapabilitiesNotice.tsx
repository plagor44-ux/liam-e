import type { AccountOut } from '../api/generated/types.gen'
import { Notice } from '../ds/Notice'
import { useI18n } from '../i18n/I18n'

export function GmailCapabilitiesNotice({ account, retry }: { account?: AccountOut; retry?: () => void }) {
  const { texts } = useI18n()
  if (account?.provider_actions_blocked) {
    return <Notice tone="info" action={retry ? { label: texts.accounts.gmailRetryActions, onClick: retry } : undefined}>{texts.accounts.gmailReauth}</Notice>
  }
  const verified = account?.action_capabilities?.mark_read
  return <Notice tone="info"><span>{texts.accounts.gmailCapabilitiesAvailable}<br />
    {verified ? texts.accounts.gmailCapabilitiesVerified : texts.accounts.gmailCapabilitiesPending}<br />
    {texts.accounts.gmailCapabilitiesUnavailable}</span></Notice>
}

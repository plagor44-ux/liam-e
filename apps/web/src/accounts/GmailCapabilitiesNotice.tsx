import { Notice } from '../ds/Notice'
import { useI18n } from '../i18n/I18n'

export function GmailCapabilitiesNotice() {
  const { texts } = useI18n()
  return <Notice tone="info"><span>{texts.accounts.gmailCapabilitiesAvailable}<br />{texts.accounts.gmailCapabilitiesUnavailable}</span></Notice>
}

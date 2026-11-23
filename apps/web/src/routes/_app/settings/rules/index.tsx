// i18n-exempt: route path.
import { createFileRoute } from '@tanstack/react-router'
import { RuleSettings } from '../../../../settings/MailFiling'
export const Route = createFileRoute('/_app/settings/rules/')({ component: RuleSettings })

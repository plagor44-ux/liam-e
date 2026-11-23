// i18n-exempt: route path.
import { createFileRoute } from '@tanstack/react-router'
import { RuleSettings } from '../../../../settings/MailFiling'
export const Route = createFileRoute('/_app/settings/rules/')({
  validateSearch: (search: Record<string, unknown>): { source_mail?: string } => ({ source_mail: typeof search.source_mail === 'string' && search.source_mail.length <= 100 ? search.source_mail : undefined }),
  component: RulesPage,
})
function RulesPage() {
  const { source_mail } = Route.useSearch()
  const navigate = Route.useNavigate()
  return <RuleSettings sourceMessageId={source_mail} onSourceUsed={() => { void navigate({ search: { source_mail: undefined }, replace: true }) }} />
}

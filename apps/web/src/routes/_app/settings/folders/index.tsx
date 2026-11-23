// i18n-exempt: route path.
import { createFileRoute } from '@tanstack/react-router'
import { FolderSettings } from '../../../../settings/MailFiling'
export const Route = createFileRoute('/_app/settings/folders/')({ component: FolderSettings })

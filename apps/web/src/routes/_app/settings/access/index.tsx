import { createFileRoute } from '@tanstack/react-router'
import { AgentAccess } from '../../../../settings/AgentAccess'

export const Route = createFileRoute('/_app/settings/access/')({ component: AgentAccess })

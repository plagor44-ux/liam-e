import { createFileRoute } from '@tanstack/react-router'
import { McpAccountSettings } from '../../../../settings/McpAccountSettings'

export const Route = createFileRoute('/_app/settings/mcp/')({ component: McpAccountSettings })

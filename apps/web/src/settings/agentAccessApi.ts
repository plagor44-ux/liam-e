// i18n-exempt: API query keys and error codes.
import { queryOptions } from '@tanstack/react-query'
import { agentAccessDisable, agentAccessEnable, agentAccessGet } from '../api/generated/sdk.gen'
import type { AgentAccessOut } from '../api/generated/types.gen'
import { ApiError } from '../api/unwrap'

export type { AgentAccessOut }

export class AgentAccessError extends ApiError {
  readonly status?: number

  constructor(status: number | undefined, cause: unknown) {
    super(cause)
    this.status = status
  }
}

async function callAccess<T>(
  promise: Promise<{ data?: T; error?: unknown; response?: Response }>,
): Promise<T> {
  const result = await promise
  if (result.response?.ok && result.data !== undefined) return result.data
  throw new AgentAccessError(result.response?.status, result.error)
}

export const agentAccessQuery = () => queryOptions({
  queryKey: ['agent-access'],
  refetchOnWindowFocus: 'always',
  queryFn: ({ signal }) => callAccess(agentAccessGet({ signal })),
})

export const disableAgentAccess = (): Promise<AgentAccessOut> =>
  callAccess(agentAccessDisable())

export const enableAgentAccess = (expectedRevision: number): Promise<AgentAccessOut> =>
  callAccess(agentAccessEnable({ body: { expected_revision: expectedRevision } }))

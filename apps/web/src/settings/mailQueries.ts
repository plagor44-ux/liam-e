// i18n-exempt: query keys.
import { queryOptions } from '@tanstack/react-query'
import { mailFoldersList } from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'

export const foldersQuery = () => queryOptions({ queryKey: ['mail-folders'], queryFn: () => unwrap(mailFoldersList({ throwOnError: true })) })

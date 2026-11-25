import { createFileRoute } from '@tanstack/react-router'
import { SkillSettings } from '../../../../settings/SkillSettings'

export const Route = createFileRoute('/_app/settings/skills/')({ component: SkillSettings })

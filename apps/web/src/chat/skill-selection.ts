// i18n-exempt: internal identifiers and pure matching helpers.
import type { PromptDetailOut, PromptOut, SkillOut, SkillWorkflowStep } from '../api/generated/types.gen'
import type { Catalogue } from '../i18n/catalogue'

export type ActiveSkillSelection = {
  id: string
  revision_id: string
  slug: string
  origin: SkillOut['origin']
  name: string
  version: number
  description: string
  workflow_steps: SkillWorkflowStep[]
}

export function toActiveSkillSelection(skill: SkillOut): ActiveSkillSelection {
  return {
    id: skill.id,
    revision_id: skill.revision_id,
    slug: skill.slug,
    origin: skill.origin,
    name: skill.name,
    version: skill.version,
    description: skill.description,
    workflow_steps: skill.workflow_steps,
  }
}

/** Slug of the built-in skill that drafts new skills; it starts only from its own slash action. */
export const SKILL_CREATOR_SLUG = 'skill_creator'

type SkillIdentity = Pick<SkillOut, 'slug' | 'origin'>

export function isSkillCreator(skill: SkillIdentity): boolean {
  return skill.origin === 'builtin' && skill.slug === SKILL_CREATOR_SLUG
}

function builtinText(skill: SkillIdentity, texts: Catalogue) {
  const known = texts.skills.builtin
  return skill.origin === 'builtin' && Object.hasOwn(known, skill.slug) ? known[skill.slug as keyof typeof known] : undefined
}

/** Built-in skills carry server-side German names; the web catalogue localizes them by slug. */
export function skillName(skill: SkillIdentity & Pick<SkillOut, 'name'>, texts: Catalogue): string {
  return builtinText(skill, texts)?.name ?? skill.name
}

export function skillDescription(skill: SkillIdentity & Pick<SkillOut, 'description'>, texts: Catalogue): string {
  return builtinText(skill, texts)?.description ?? skill.description
}

export type SlashMatch = {
  /** Index of the leading `/` character. */
  start: number
  /** Exclusive index at the end of the `/token` being edited. */
  end: number
  /** Text after `/` up to `end`. */
  query: string
  /** True when the entire input is only this single `/token`. */
  bare: boolean
}

/**
 * Detect an active `/command` token at the textarea cursor.
 * Ordinary slash text (`21/11`, `und/oder`, `https://example.org/a`, `/usr/bin`, `/ `) returns null.
 */
export function detectSlashQuery(text: string, cursor: number): SlashMatch | null {
  const safeCursor = Math.max(0, Math.min(cursor, text.length))
  const before = text.slice(0, safeCursor)
  const after = text.slice(safeCursor)
  if (/^[^\s]*\//u.test(after)) return null
  const match = /(^|\s)(\/([^\s/]*))$/u.exec(before)
  if (!match) return null
  const start = match.index + match[1]!.length
  const afterSuffix = /^[^\s]*/u.exec(after)?.[0] ?? ''
  const end = safeCursor + afterSuffix.length
  const query = text.slice(start + 1, end)
  const bare = text.slice(0, start) === '' && text.slice(end) === ''
  return { start, end, query, bare }
}

/** Remove the matched `/token` from `text` while preserving surrounding content cleanly. */
export function stripSlashToken(text: string, match: SlashMatch): { nextText: string; nextCursor: number } {
  const before = text.slice(0, match.start)
  const after = text.slice(match.end)
  if (!before) {
    const trimmedAfter = after.replace(/^ +/u, '')
    return { nextText: trimmedAfter, nextCursor: 0 }
  }
  if (/[ \t]$/u.test(before) && /^[ \t]+/u.test(after)) {
    const collapsedAfter = after.replace(/^[ \t]+/u, '')
    return { nextText: before + collapsedAfter, nextCursor: before.length }
  }
  return { nextText: before + after, nextCursor: before.length }
}

export type SlashOption =
  | {
      key: string
      group: 'skills' | 'templates' | 'revisions' | 'actions'
      kind: 'skill'
      label: string
      command: string
      hint: string
      badge?: string
      checked: boolean
      disabled: boolean
      skill: SkillOut
    }
  | {
      key: string
      group: 'skills' | 'templates' | 'revisions' | 'actions'
      kind: 'template'
      label: string
      hint: string
      badge: string
      checked: boolean
      disabled: boolean
      revisionId: string
    }
  | {
      key: string
      group: 'skills' | 'templates' | 'revisions' | 'actions'
      kind: 'clear-skill' | 'clear-template' | 'manage-templates'
      label: string
      hint?: string
      checked?: boolean
      disabled: boolean
    }
  | {
      key: string
      group: 'actions'
      kind: 'create-skill'
      label: string
      hint: string
      checked?: undefined
      disabled: boolean
      skill: SkillOut
    }

function matches(query: string, ...fields: (string | undefined)[]): boolean {
  if (!query) return true
  const needle = query.toLowerCase()
  return fields.some(field => field?.toLowerCase().includes(needle))
}

export function buildSlashOptions({
  query,
  skills,
  prompts,
  promptDetails,
  favoriteIds,
  selectedSkillId,
  selectedPromptRevisionId,
  busy,
  texts,
}: {
  query: string
  skills: SkillOut[]
  prompts: PromptOut[]
  promptDetails: PromptDetailOut[]
  favoriteIds: string[]
  selectedSkillId?: string | null
  selectedPromptRevisionId: string | null
  busy: boolean
  texts: Catalogue
}): SlashOption[] {
  const trimmed = query.trim()
  const favorites = new Set(favoriteIds)
  const sortedSkills = skills.filter(skill => !isSkillCreator(skill)).sort((a, b) => {
    const aActive = a.status === 'active' ? 0 : 1
    const bActive = b.status === 'active' ? 0 : 1
    if (aActive !== bActive) return aActive - bActive
    const aFav = favorites.has(a.id) ? 0 : 1
    const bFav = favorites.has(b.id) ? 0 : 1
    if (aFav !== bFav) return aFav - bFav
    return skillName(a, texts).localeCompare(skillName(b, texts))
  })

  const skillOptions: SlashOption[] = sortedSkills
    .filter(skill => (skill.status === 'active' || trimmed.length > 0) && matches(trimmed, skillName(skill, texts), skill.name, skill.slug, skillDescription(skill, texts)))
    .map(skill => ({
      key: `skill:${skill.id}`,
      group: 'skills',
      kind: 'skill',
      label: skillName(skill, texts),
      command: `/${skill.slug}`,
      hint: skill.status === 'active' ? skillDescription(skill, texts) : texts.skills.disabledHint,
      badge: favorites.has(skill.id) ? texts.skills.favoriteBadge : texts.skills.skillTag,
      checked: selectedSkillId === skill.id,
      disabled: skill.status !== 'active',
      skill,
    }))

  const templateOptions: SlashOption[] = prompts
    .filter(item => matches(trimmed, item.name, item.text, texts.skills.templateTag))
    .map(item => ({
      key: `template:${item.revision_id}`,
      group: 'templates',
      kind: 'template',
      label: item.name,
      hint: item.text,
      badge: texts.skills.templateTag,
      checked: selectedPromptRevisionId === item.revision_id,
      disabled: busy,
      revisionId: item.revision_id,
    }))

  const revisionOptions: SlashOption[] = promptDetails.flatMap(detail =>
    detail.revisions
      .filter(rev => rev.id !== detail.revision_id)
      .map(rev => {
        const label = `${detail.name} · ${texts.prompts.version(rev.number)}`
        return { detail, rev, label }
      })
      .filter(({ detail: item, rev, label }) =>
        matches(trimmed, label, item.name, rev.content, texts.skills.templateTag),
      )
      .map(({ rev, label }) => ({
        key: `template-rev:${rev.id}`,
        group: 'revisions' as const,
        kind: 'template' as const,
        label,
        hint: rev.content,
        badge: texts.skills.templateTag,
        checked: selectedPromptRevisionId === rev.id,
        disabled: busy,
        revisionId: rev.id,
      })),
  )

  const actionOptions: SlashOption[] = []
  // The creator never appears as an ordinary skill: its action always starts a fresh conversation.
  const creator = skills.find(skill => isSkillCreator(skill) && skill.status === 'active')
  if (creator && selectedSkillId !== creator.id && matches(trimmed, skillName(creator, texts), creator.slug, texts.skills.createSkillKeywords)) {
    actionOptions.push({
      key: 'action:create-skill',
      group: 'actions',
      kind: 'create-skill',
      label: skillName(creator, texts),
      hint: texts.skills.createSkillHint,
      disabled: false,
      skill: creator,
    })
  }
  if (selectedSkillId && matches(trimmed, texts.skills.clearSkillItem, texts.skills.removeSelection)) {
    actionOptions.push({
      key: 'action:clear-skill',
      group: 'actions',
      kind: 'clear-skill',
      label: texts.skills.clearSkillItem,
      disabled: false,
    })
  }
  if (selectedPromptRevisionId !== null && matches(trimmed, texts.prompts.standard, texts.prompts.useStandard, texts.prompts.clearSelection)) {
    actionOptions.push({
      key: 'action:clear-template',
      group: 'actions',
      kind: 'clear-template',
      label: texts.prompts.standard,
      checked: false,
      disabled: busy,
    })
  }
  if (trimmed === '' || matches(trimmed, texts.prompts.manage)) {
    actionOptions.push({
      key: 'action:manage-templates',
      group: 'actions',
      kind: 'manage-templates',
      label: texts.prompts.manage,
      disabled: false,
    })
  }

  return [...skillOptions, ...templateOptions, ...revisionOptions, ...actionOptions]
}

// i18n-exempt: CSS classes, protocol codes, DOM selectors and field limits.
import { useQuery } from '@tanstack/react-query'
import { useEffect, useId, useState } from 'react'
import type { SkillDraft, SkillEdit } from '../api/generated/types.gen'
import { skillsQuery, skillToolsQuery } from '../api/queries'
import { Button, Notice, Tag, TextField } from '../ds'
import type { Catalogue } from '../i18n/catalogue'
import { useI18n } from '../i18n/I18n'
import { useChatSession, useRestartConversation } from './session'
import { SKILL_CREATOR_SLUG, toActiveSkillSelection } from './skill-selection'
import './skill-proposal.css'

// Mirrors the server rule in api/skills/service.py; the server checks again on confirmation.
const SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/u
const RESERVED_SLUGS = new Set(['tools', SKILL_CREATOR_SLUG])
const LIMITS = { slug: 64, name: 80, description: 400, instructions: 8000 } as const
// Python counts code points, not UTF-16 units.
const length = (text: string) => Array.from(text).length

export function skillEditFrom(draft: SkillDraft): SkillEdit {
  return { slug: draft.slug, name: draft.name, description: draft.description, instructions: draft.instructions }
}

function toolName(name: string, texts: Catalogue): string {
  const names = texts.skillProposal.toolNames
  return Object.hasOwn(names, name) ? names[name as keyof typeof names] : name
}

function errorText(code: string, texts: Catalogue): string {
  const t = texts.skillProposal
  switch (code) {
    case 'skill_quota': return t.quota
    case 'skill_invalid': case 'proposal_invalid': return t.invalid
    case 'skill_tool_unknown': case 'skill_tool_not_authorable': return t.toolUnavailable
    case 'proposal_context_revoked': return texts.proposalErrors.proposal_context_revoked
    case 'proposal_not_pending': return texts.proposalErrors.proposal_not_pending
    default: return texts.proposal.requestError
  }
}

export function SkillProposal({ draft, value, onChange, busy, error, onRestart, confirm, cancel }: {
  draft: SkillDraft; value: SkillEdit; onChange: (value: SkillEdit) => void; busy: boolean; error?: string;
  onRestart: () => void; confirm: (edit: SkillEdit) => void; cancel: () => void;
}) {
  const { texts } = useI18n()
  const t = texts.skillProposal
  const headingId = useId(), nameId = useId(), slugId = useId()
  const tools = useQuery(skillToolsQuery())
  const kinds = new Map(tools.data?.map(tool => [tool.name, tool.kind]))
  const [instructionsOpen, setInstructionsOpen] = useState(false)
  const slug = value.slug.trim()
  const fieldErrors = {
    name: !value.name.trim() ? t.required : error === 'skill_name_conflict' ? t.nameTaken : undefined,
    slug: !SLUG_PATTERN.test(slug) || RESERVED_SLUGS.has(slug) ? t.commandInvalid : error === 'skill_conflict' ? t.commandTaken : undefined,
    description: !value.description.trim() ? t.required : undefined,
    instructions: !value.instructions.trim() ? t.required : undefined,
  }
  const invalid = Object.values(fieldErrors).some(Boolean)
  const notice = error && error !== 'skill_name_conflict' && error !== 'skill_conflict' ? errorText(error, texts) : undefined
  const update = (patch: Partial<SkillEdit>) => onChange({ ...value, ...patch })
  const steps = draft.workflow_steps ?? []
  const allowed = draft.allowed_tools ?? []
  // A taken name or command marks its field while Save stays disabled, and the busy Save button lost
  // the focus. Move the focus to that field and bring it with its error into view: focusing it reads
  // the error through aria-describedby once, so the error itself has no live role.
  useEffect(() => {
    const input = document.getElementById(error === 'skill_name_conflict' ? nameId : error === 'skill_conflict' ? slugId : '')
    if (!input) return
    const reduced = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    input.focus({ preventScroll: true })
    ;(input.closest('.le-field') ?? input).scrollIntoView?.({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' })
  }, [error, nameId, slugId])

  return <section className="le-confirm le-skill-proposal" aria-labelledby={headingId}>
    <div className="le-confirm-head">
      <h3 id={headingId} className="le-confirm-title">{t.title}</h3>
      <span>{t.reason}</span>
    </div>
    <div className="le-skill-fields">
      <TextField id={nameId} label={t.name} value={value.name} maxLength={LIMITS.name} required
        hint={texts.prompts.count(length(value.name), LIMITS.name)} error={fieldErrors.name}
        onChange={event => update({ name: event.target.value })} />
      <TextField id={slugId} label={t.command} value={value.slug} maxLength={LIMITS.slug} required autoCapitalize="none" autoComplete="off" spellCheck={false}
        hint={t.commandHint(slug || '…')} error={fieldErrors.slug}
        onChange={event => update({ slug: event.target.value })} />
      <TextField multiline rows={2} label={t.description} value={value.description} maxLength={LIMITS.description} required
        hint={texts.prompts.count(length(value.description), LIMITS.description)} error={fieldErrors.description}
        onChange={event => update({ description: event.target.value })} />
      <details className="le-skill-instructions" open={instructionsOpen || Boolean(fieldErrors.instructions)}
        onToggle={event => setInstructionsOpen(event.currentTarget.open)}>
        <summary>{t.instructions}</summary>
        <TextField multiline rows={8} label={t.instructions} value={value.instructions} maxLength={LIMITS.instructions} required
          hint={texts.prompts.count(length(value.instructions), LIMITS.instructions)} error={fieldErrors.instructions}
          onChange={event => update({ instructions: event.target.value })} />
      </details>
    </div>
    <div className="le-skill-section">
      <h4>{t.tools}</h4>
      {allowed.length === 0 ? <p>{t.noTools}</p> : <ul className="le-skill-tools">
        {allowed.map(name => {
          const kind = kinds.get(name)
          return <li key={name}><span>{toolName(name, texts)}</span>{kind && <Tag>{t.kinds[kind]}</Tag>}</li>
        })}
      </ul>}
    </div>
    {steps.length > 0 && <div className="le-skill-section">
      <h4>{t.steps}</h4>
      <ol className="le-skill-steps">{steps.map(step => <li key={step.id}><strong>{step.title}</strong>{step.description ? `: ${step.description}` : null}</li>)}</ol>
    </div>}
    <p className="le-skill-section le-hint">{t.safety}</p>
    {notice && <Notice tone="danger" action={error === 'proposal_context_revoked' ? { label: texts.shell.newConversation, onClick: onRestart } : undefined}>{notice}</Notice>}
    <div className="le-confirm-foot">
      <Button variant="primary" disabled={busy || invalid} onClick={() => confirm({ slug, name: value.name.trim(), description: value.description.trim(), instructions: value.instructions.trim() })}>{t.save}</Button>
      <Button variant="ghost" disabled={busy} onClick={cancel}>{texts.controls.cancel}</Button>
    </div>
  </section>
}

/**
 * Result of a confirmed skill card. With a message field (`composer`), the skill can be attached to
 * this conversation without sending. Without one, for example when the conversation shows a restart
 * notice instead, it never claims an attachment and offers a new conversation with the skill attached.
 */
export function SkillSaved({ conversationId, skillId, name, composer = false }: { conversationId: string; skillId?: string | null; name: string; composer?: boolean }) {
  const { texts } = useI18n()
  const t = texts.skillProposal
  const skills = useQuery(skillsQuery())
  const { selectedSkills, setSelectedSkill } = useChatSession()
  const restart = useRestartConversation(conversationId)
  const live = skills.data?.find(skill => skill.id === skillId && skill.status === 'active')
  const label = live?.name ?? name
  const attached = composer && Boolean(skillId) && selectedSkills[conversationId]?.skill.id === skillId
  const use = () => {
    if (!live || !composer) return
    setSelectedSkill(conversationId, toActiveSkillSelection(live), 'turn')
    // The action disappears once used; the owner continues in the message field.
    requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[role="combobox"]')?.focus())
  }
  const useInNew = () => {
    if (!live) return
    // The restart clears the new conversation's skill, so attach it afterwards.
    restart()
    setSelectedSkill('new', toActiveSkillSelection(live), 'turn')
  }
  const action = !live || attached ? undefined : composer ? { label: t.useNow, onClick: use } : { label: t.useInNew, onClick: useInNew }
  return <Notice tone="success" action={action}>
    {attached ? t.attached(label) : live && !composer ? t.savedNoComposer(label) : t.saved(label)}
  </Notice>
}

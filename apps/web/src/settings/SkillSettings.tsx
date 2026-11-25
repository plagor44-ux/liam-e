// i18n-exempt: API codes, query keys and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { skillDelete, skillUpdate } from '../api/generated/sdk.gen'
import type { SkillOut } from '../api/generated/types.gen'
import { skillsQuery } from '../api/queries'
import { errorCode, unwrap } from '../api/unwrap'
import { skillDescription, skillName } from '../chat/skill-selection'
import { Button, Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import { PreferenceDialog } from './Preferences'
import './settings.css'

type Outcome = { tone: 'success' | 'danger'; text: string; focus?: boolean }

/**
 * Built-in and custom skills with their status (#156). Changes go straight to the server with the
 * listed version: a stale version or a vanished skill reloads the list and says so, never retries.
 * Built-in skills can only be switched on or off; the server refuses anything else for them.
 */
export function SkillSettings() {
  const { texts, locale } = useI18n()
  const t = texts.skillSettings
  const client = useQueryClient()
  const query = useQuery(skillsQuery())
  const [busy, setBusy] = useState(false), [deleting, setDeleting] = useState<SkillOut>(), [outcome, setOutcome] = useState<Outcome>()
  const lock = useRef(false), outcomeRef = useRef<HTMLDivElement>(null)
  // A deleted row takes its button with it: continue at the result, which also announces it once.
  useEffect(() => { if (outcome?.focus) outcomeRef.current?.focus() }, [outcome])

  // Favorites, the / menu and open banners read the same query; revoked turns show in their history.
  const refresh = () => { void client.invalidateQueries({ queryKey: ['skills'] }); void client.invalidateQueries({ queryKey: ['history'] }) }
  const failed = async (cause: unknown, name: string) => {
    const code = errorCode(cause)
    const stale = code === 'version_conflict' || code === 'skill_not_found' || code === 'skill_builtin_protected'
    setOutcome({ tone: 'danger', text: code === 'version_conflict' ? t.conflict(name) : code === 'skill_not_found' ? t.gone(name) : code === 'skill_builtin_protected' ? t.protected : t.saveError })
    // Show the server's current state instead of overwriting it.
    if (stale) { await query.refetch(); refresh() }
  }
  const run = async (skill: SkillOut, action: () => Promise<void>) => {
    if (lock.current) return
    lock.current = true; setBusy(true); setOutcome(undefined)
    try { await action() } catch (cause) { await failed(cause, skillName(skill, texts)) } finally { lock.current = false; setBusy(false) }
  }
  const toggle = (skill: SkillOut) => run(skill, async () => {
    const status = skill.status === 'active' ? 'disabled' : 'active'
    await unwrap(skillUpdate({ path: { skill_id: skill.id }, body: { expected_version: skill.version, status }, throwOnError: true }))
    setOutcome({ tone: 'success', text: status === 'active' ? t.enabled(skillName(skill, texts)) : t.disabled(skillName(skill, texts)) })
    refresh()
  })
  const remove = (skill: SkillOut) => run(skill, async () => {
    try {
      const result = await skillDelete({ path: { skill_id: skill.id }, query: { expected_version: skill.version } })
      if (!result.response?.ok) throw result.error
    } finally { setDeleting(undefined) }
    setOutcome({ tone: 'success', text: t.deleted(skillName(skill, texts)), focus: true })
    refresh()
  })

  const skills = [...(query.data ?? [])].sort((a, b) => Number(a.origin !== 'builtin') - Number(b.origin !== 'builtin') || skillName(a, texts).localeCompare(skillName(b, texts), locale, { sensitivity: 'base', numeric: true }) || a.id.localeCompare(b.id))
  return <section className="le-prompts le-skill-settings">
    <h2 className="heading">{t.title}</h2>
    <p>{t.description}</p>
    {outcome && <div ref={outcomeRef} tabIndex={-1} className="le-skill-outcome"><Notice tone={outcome.tone}>{outcome.text}</Notice></div>}
    {query.isPending && <p role="status">{t.loading}</p>}
    {query.isError && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void query.refetch() } }}>{t.error}</Notice>}
    {query.data?.length === 0 && <p>{t.empty}</p>}
    {skills.length > 0 && <ul className="le-skill-list" aria-label={t.title}>
      {skills.map(skill => {
        const name = skillName(skill, texts), active = skill.status === 'active'
        return <li className="le-prompt-row le-skill-row" key={skill.id}>
          <div className="le-skill-row-info">
            <strong>{name}</strong>
            <span className="le-skill-command"><span className="le-sr">{t.command}: </span><code>/{skill.slug}</code></span>
            <span className="le-skill-description">{skillDescription(skill, texts)}</span>
            <small>{t.version(skill.version)} · {t.origins[skill.origin]} · {t.statuses[skill.status]}</small>
          </div>
          <div className="le-skill-row-actions">
            <Button variant={active ? 'primary' : 'secondary'} role="switch" aria-checked={active} aria-label={t.useName(name)} disabled={busy} onClick={() => { void toggle(skill) }}>{t.use}</Button>
            {skill.origin === 'custom' && <Button variant="secondary" aria-label={t.removeName(name)} disabled={busy} onClick={() => setDeleting(skill)}>{t.remove}</Button>}
          </div>
        </li>
      })}
    </ul>}
    {deleting && <PreferenceDialog title={t.removeTitle} close={() => { if (!busy) setDeleting(undefined) }}>
      <p><strong>{skillName(deleting, texts)}</strong> <code>/{deleting.slug}</code></p>
      <p>{t.removeDescription}</p>
      <div className="le-dialog-actions"><Button variant="secondary" disabled={busy} onClick={() => setDeleting(undefined)}>{texts.controls.cancel}</Button><Button variant="danger" disabled={busy} onClick={() => { void remove(deleting) }}>{t.remove}</Button></div>
    </PreferenceDialog>}
  </section>
}

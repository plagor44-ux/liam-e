// i18n-exempt: CSS classes and component props.
import { useId, useState } from 'react'
import type { SkillOut } from '../api/generated/types.gen'
import { Chip, Icon, IconButton, Menu, Notice, Tag, Tooltip } from '../ds'
import { useI18n } from '../i18n/I18n'
import type { SelectedSkillEntry, SkillPersistence } from './session'
import { isSkillCreator, skillName, skillStep } from './skill-selection'
import './composer-skills.css'

export function ActiveSkillBanner({
  entry,
  liveSkill,
  skillsLoaded,
  disabled = false,
  onChangePersistence,
  onRemove,
}: {
  entry: SelectedSkillEntry
  liveSkill?: SkillOut
  skillsLoaded: boolean
  disabled?: boolean
  onChangePersistence: (persistence: SkillPersistence) => void
  onRemove: () => void
}) {
  const { texts } = useI18n()
  const [showSteps, setShowSteps] = useState(false)
  const { skill, persistence } = entry
  const sticky = persistence === 'until_stopped'
  const unavailable = skillsLoaded && (!liveSkill || liveSkill.status !== 'active')
  const steps = skill.workflow_steps
  // The creator always runs until ended; its server-side steps are summarized by a localized note.
  const creator = isSkillCreator(skill)

  const noteId = useId(), stepsId = useId()
  const note = sticky ? texts.skills.activeStickyNote : texts.skills.activeScopeNote
  const choose = (next: SkillPersistence) => { if (next !== persistence) onChangePersistence(next) }

  return (
    <div className="le-composer-active">
      {/* One row: tag, duration, steps and x; on phones the x stays beside the tag and the chips wrap below. */}
      <div className="le-composer-active-row">
        <Tag tone="on-surface" icon="sparkle">{creator ? skillName(skill, texts) : texts.skills.activeSkill(skillName(skill, texts))}</Tag>
        <div className="le-composer-active-actions">
          {creator
            ? <span className="le-composer-active-meta">{texts.skills.creatorNote}</span>
            : <>
              <Menu
                label={texts.skills.durationMenu}
                items={[
                  { label: texts.skills.activeDuration, hint: texts.skills.durationTurnHint, checked: !sticky, onSelect: () => choose('turn') },
                  { label: texts.skills.durationUntilStopped, hint: texts.skills.durationStickyHint, checked: sticky, onSelect: () => choose('until_stopped') },
                ]}
                trigger={props => (
                  <Tooltip content={note} describes={false}>
                    <Chip {...props} quiet disabled={disabled} aria-describedby={noteId}>
                      {sticky ? texts.skills.durationUntilStopped : texts.skills.activeDuration}<Icon name="chevron-down" size="sm" />
                    </Chip>
                  </Tooltip>
                )}
              />
              <span id={noteId} className="le-sr">{note}</span>
              {steps.length > 0 && (
                <Chip quiet aria-expanded={showSteps} aria-controls={showSteps ? stepsId : undefined} onClick={() => setShowSteps(prev => !prev)}>
                  {texts.skills.showSteps(steps.length)}<Icon name="chevron-down" size="sm" />
                </Chip>
              )}
            </>}
        </div>
        <span className="le-composer-active-close">
          <IconButton
            icon="x"
            disabled={disabled}
            label={sticky ? texts.skills.endSkill : texts.skills.removeSelection}
            tip={texts.skills.removeSelectionTip}
            onClick={onRemove}
          />
        </span>
      </div>

      {!creator && showSteps && steps.length > 0 && (
        <div className="le-composer-steps" id={stepsId}>
          <ol className="le-composer-steps-list">
            {steps.map(step => {
              const { title, description } = skillStep(skill, step, texts)
              return (
                <li key={step.id}>
                  <strong>{title}</strong>
                  {description ? `: ${description}` : null}
                </li>
              )
            })}
          </ol>
        </div>
      )}

      {unavailable && (
        <Notice tone="danger" action={{ label: texts.skills.removeSelection, onClick: onRemove }}>
          {texts.skills.unavailableSelection}
        </Notice>
      )}
    </div>
  )
}

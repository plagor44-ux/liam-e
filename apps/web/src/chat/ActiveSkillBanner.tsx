// i18n-exempt: CSS classes and component props.
import { useState } from 'react'
import type { SkillOut } from '../api/generated/types.gen'
import { Chip, IconButton, Notice, Tag } from '../ds'
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

  return (
    <div className="le-composer-active">
      <div className="le-composer-active-row">
        <Tag tone="on-surface" icon="sparkle">{creator ? skillName(skill, texts) : texts.skills.activeSkill(skillName(skill, texts))}</Tag>
        <span className="le-composer-active-meta">
          {sticky ? texts.skills.durationUntilStopped : texts.skills.activeDuration}
        </span>
        <div className="le-composer-active-actions">
          {!creator && (
            <Chip
              quiet
              pressed={sticky}
              disabled={disabled}
              onClick={() => onChangePersistence(sticky ? 'turn' : 'until_stopped')}
            >
              {texts.skills.keepForFollowUps}
            </Chip>
          )}
          {!creator && steps.length > 0 && (
            <Chip
              quiet
              aria-expanded={showSteps}
              onClick={() => setShowSteps(prev => !prev)}
            >
              {showSteps ? texts.skills.hideSteps : texts.skills.showSteps(steps.length)}
            </Chip>
          )}
          <IconButton
            icon="x"
            disabled={disabled}
            label={sticky ? texts.skills.endSkill : texts.skills.removeSelection}
            tip={texts.skills.removeSelectionTip}
            onClick={onRemove}
          />
        </div>
      </div>

      <span className="le-composer-steps-note">
        {creator ? texts.skills.creatorNote : sticky ? texts.skills.activeStickyNote : texts.skills.activeScopeNote}
      </span>

      {!creator && showSteps && steps.length > 0 && (
        <div className="le-composer-steps">
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

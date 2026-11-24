import type { ComponentProps, ReactNode } from 'react'
import type { MailActionCapabilities, MailFolder, MessageListItemOut } from '../api/generated/types.gen'
import { useI18n } from '../i18n/I18n'
import { useSwipeRow } from '../mail/useSwipeRow'
import type { SwipeBasis } from '../mail/swipeGesture'
import { formatListDate, formatDeadline } from '../i18n/format'
import { Avatar } from './Avatar'
import { Tag } from './Chip'
import { Icon } from './Icon'
import { IconButton } from './IconButton'
import { Menu, type MenuItem } from './Menu'
import { Tooltip } from './Tooltip'
import { FilingTags } from '../mail/FilingTags'
import { shortcutHint } from '../mail/inboxShortcuts'
import { QuickActions, useQuickActions } from '../mail/QuickActions'
import { Notice } from './Notice'
// A flagged mail carries the warm urgency dot next to the time; the dot names itself and shows a tooltip on hover.
export function MailRow({ mail, actionCapabilities, providerActionHint, accountLabel, filingDestinations = [], quickActions, quickMenuItems = [], quickError, selected, coarse, shortcutsEnabled = true, onOpen, onAsk, onArchive, onToggleRead, onTrash, swipe, selectionMode = false, checked = false, onSelect }: { mail: MessageListItemOut; actionCapabilities?: MailActionCapabilities; providerActionHint?: string; accountLabel?: string; filingDestinations?: MailFolder[]; quickActions?: ReactNode; quickMenuItems?: MenuItem[]; quickError?: string; selected: boolean; coarse: boolean; shortcutsEnabled?: boolean; onOpen: () => void; onAsk?: () => void; onArchive: () => void; onToggleRead: () => void; onTrash: () => void; selectionMode?: boolean; checked?: boolean; onSelect?: (shift: boolean) => void; swipe?: { basis?: SwipeBasis; enabled: boolean; navigation: string; valid: (basis: SwipeBasis) => boolean; run: (action: 'archive' | 'trash' | 'mark_read' | 'mark_unread') => void } }) {
  const { texts, locale } = useI18n()
  const gesture = useSwipeRow(swipe?.basis, swipe?.enabled ?? false, swipe?.navigation ?? '', basis => swipe?.valid(basis) ?? false, action => swipe?.run(action))
  const readLabel = mail.is_read ? texts.controls.markUnread : texts.controls.markRead
  const hint = providerActionHint ?? texts.errors.provider_action_unsupported
  const archiveDisabled = actionCapabilities?.archive === false
  const readDisabled = actionCapabilities?.[mail.is_read ? 'mark_unread' : 'mark_read'] === false
  const trashDisabled = actionCapabilities?.trash === false
  const items = [
    { label: texts.controls.archive, icon: 'archive' as const, shortcut: shortcutsEnabled ? shortcutHint('archive', locale) : undefined, onSelect: onArchive, disabled: archiveDisabled },
    { label: readLabel, icon: 'mail-open' as const, shortcut: shortcutsEnabled ? shortcutHint('read', locale) : undefined, onSelect: onToggleRead, disabled: readDisabled },
    { label: texts.controls.trash, icon: 'trash-2' as const, shortcut: shortcutsEnabled ? shortcutHint('trash', locale) : undefined, tone: 'danger' as const, onSelect: onTrash, disabled: trashDisabled },
  ]
  const menuGroup = items.some(item => item.disabled) ? [{ label: hint, items }] : undefined
  const preview = gesture.preview
  const blockedPreview = preview && actionCapabilities?.[preview.action] === false
  const label = preview && (blockedPreview ? hint : ({ archive: texts.controls.archive, trash: texts.controls.trash, mark_read: texts.controls.markRead, mark_unread: texts.controls.markUnread })[preview.action])
  const swipeLabel = preview?.armed && !blockedPreview ? texts.inbox.swipeRelease[preview.action] : label
  return <div className="le-mailrow-swipe"><div className={`le-mailrow-preview${!blockedPreview && preview?.action === 'trash' ? ' is-danger' : ''}${preview?.direction === 'left' ? ' is-left' : ''}`} aria-hidden="true" />{preview && <div className={`le-mailrow-preview-label${!blockedPreview && preview.action === 'trash' ? ' is-danger' : ''}${preview.direction === 'left' ? ' is-left' : ''}`} aria-hidden="true"><Icon name={blockedPreview ? 'info' : preview.action === 'trash' ? 'trash-2' : preview.action === 'archive' ? 'archive' : 'mail-open'} /><span>{swipeLabel}</span></div>}<span className="le-sr" role="status">{preview ? swipeLabel : ''}</span><div ref={gesture.row} onPointerDown={gesture.onPointerDown} onPointerMove={gesture.onPointerMove} onPointerUp={gesture.onPointerUp} onPointerCancel={gesture.onPointerCancel} onLostPointerCapture={gesture.onLostPointerCapture} onClickCapture={gesture.onClickCapture} onClick={event => { if (!(event.target as Element).closest('button, a, input, select, textarea, [role="menu"]')) selectionMode ? onSelect?.(event.shiftKey) : onOpen() }} className={`le-mailrow${mail.is_read ? '' : ' is-unread'}${checked ? ' is-checked' : ''}`} aria-current={!selectionMode && selected ? 'true' : undefined}><button type="button" className={`le-mailrow-avatar${checked ? ' is-checked' : ''}${selectionMode ? ' is-selecting' : ''}`} aria-label={texts.inbox.selectMail(mail.subject)} aria-pressed={checked} onClick={event => { event.stopPropagation(); onSelect?.(event.shiftKey) }}><Avatar name={mail.sender_name} address={mail.sender_address} colorIndex={mail.color_index} /><span className="le-mailrow-avatar-check"><Icon name="check" /></span></button><div className="le-mailrow-main"><div className="le-mailrow-top"><span className="le-mailrow-from">{mail.sender_name || mail.sender_address}</span>{accountLabel && <Tag tone="on-surface" dot={mail.color_index}>{accountLabel}</Tag>}</div><button type="button" className="le-mailrow-link le-mailrow-subject" data-swipe-start="" onClick={event => selectionMode ? onSelect?.(event.shiftKey) : onOpen()}>{texts.mail.subjectText(mail.subject)}</button><div className="le-mailrow-snippet-slot"><span className="le-mailrow-snippet">{mail.snippet}</span>{!selectionMode && !coarse && <div className="le-mailrow-actions">
      {quickActions}
      {items.map(item => <IconButton key={item.icon} label={item.label} icon={item.icon} tone={item.tone} shortcut={item.disabled ? undefined : item.shortcut} aria-disabled={item.disabled || undefined} tip={item.disabled ? hint : undefined} onClick={item.disabled ? undefined : item.onSelect} />)}
    </div>}</div><FilingTags focusable={false} folderId={mail.app_folder_id} labelIds={mail.app_label_ids} destinations={filingDestinations} />{mail.deadline && <span className="le-mailrow-deadline"><Tag tone="warm" icon="clock">{texts.controls.deadline(formatDeadline(mail.deadline, undefined, locale))}</Tag></span>}</div><div className="le-mailrow-side">{mail.internal_date && <time className="le-mailrow-time" dateTime={mail.internal_date}>{formatListDate(new Date(mail.internal_date), new Date(), undefined, locale)}</time>}{(mail.is_flagged || mail.has_attachments) && <span className="le-mailrow-marks">{mail.is_flagged && <Tooltip content={texts.controls.urgent} pos="top-end"><span role="img" aria-label={texts.controls.urgent} className="le-dot le-dot--warm" /></Tooltip>}{mail.has_attachments && <span role="img" aria-label={texts.controls.attachment}><Icon name="paperclip" size="sm" /></span>}</span>}{!selectionMode && <div className={coarse ? 'le-mailrow-more le-mailrow-more--coarse' : 'le-mailrow-more'}>
      {onAsk && <IconButton label={texts.inbox.askAboutMail(mail.subject)} tip={texts.mail.askInChat} icon="message-circle" size="lg" onClick={onAsk} />}
      <Menu menuClassName="le-menu--mailrow" placement="down-end" trigger={props => <IconButton {...props} label={texts.controls.moreMail(mail.subject)} tip={texts.mail.more} icon="more-horizontal" size="lg" />} groups={menuGroup} items={menuGroup ? quickMenuItems : [...items, ...quickMenuItems]} />
    </div>}{quickError && <Notice tone="danger">{quickError}</Notice>}</div></div></div>
}


export function MailRowWithQuickActions({ onQuickSuccess, ...props }: ComponentProps<typeof MailRow> & { onQuickSuccess?: () => void }) {
  const quick = useQuickActions([props.mail], onQuickSuccess)
  const quickMenuItems: MenuItem[] = quick.actions.map(({ destination }) => ({
    label: `${destination.kind === 'folder' ? quick.t.folder : quick.t.label}: ${destination.name}`,
    icon: destination.kind === 'folder' ? 'folder' : 'tag', disabled: quick.busy,
    onSelect: () => { void quick.file(destination.id!, destination.kind) },
  }))
  return <MailRow {...props} quickActions={<QuickActions messages={[props.mail]} onSuccess={onQuickSuccess} limit={1} />} quickMenuItems={quickMenuItems} quickError={quick.error ? quick.t.quickError : undefined} />
}

import { Button } from '../ds'
import { useI18n } from '../i18n/I18n'
import { PreferenceDialog } from '../settings/Preferences'
import { inboxShortcuts, shortcutHint, type InboxShortcut } from './inboxShortcuts'

export function ShortcutOverview({ onClose }: { onClose(): void }) {
  const { texts, locale } = useI18n(), t = texts.mailShortcuts
  return <PreferenceDialog title={t.title} close={onClose}>
    <ul className="le-shortcut-list">{(Object.keys(inboxShortcuts) as InboxShortcut[]).map(id => <li key={id} data-testid={`shortcut-${id}`}><kbd>{shortcutHint(id, locale)}</kbd><span>{t[id]} <small>{t[inboxShortcuts[id].context]}</small></span></li>)}</ul>
    <div className="le-dialog-actions"><Button variant="secondary" onClick={onClose}>{t.close}</Button></div>
  </PreferenceDialog>
}

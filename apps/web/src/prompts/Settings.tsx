// i18n-exempt: route paths and CSS classes.
import { Link, Outlet } from '@tanstack/react-router'
import type { Catalogue } from '../i18n/catalogue'
import { useI18n } from '../i18n/I18n'
import { SettingsSaveBar, SettingsSaveProvider } from '../settings/SettingsSave'
import './prompts.css'

// Add implemented preferences and controls sections here; no placeholder destinations.
export const settingsSections = (texts: Catalogue) => [
  { to: '/settings/prompts', label: texts.prompts.title },
  { to: '/settings/chat', label: texts.shell.chat },
  { to: '/settings/calendar', label: texts.calendarSettings.title },
  { to: '/settings/controls', label: texts.settingsUi.section },
  { to: '/settings/preferences', label: texts.preferences.title },
  { to: '/settings/skills', label: texts.skillSettings.section },
  { to: '/settings/folders', label: texts.mailFiling.folders },
  { to: '/settings/rules', label: texts.mailFiling.rules },
  { to: '/settings/language', label: texts.language.section },
] as const
export function Settings() {
  const { texts } = useI18n()
  return <SettingsSaveProvider><section className="le-settings"><h1 className="title">{texts.prompts.settings}</h1><nav aria-label={texts.prompts.settings}>{settingsSections(texts).map(section => <Link key={section.to} to={section.to} className="le-btn le-btn--secondary">{section.label}</Link>)}</nav><Outlet /><SettingsSaveBar /></section></SettingsSaveProvider>
}

import { useQuery } from '@tanstack/react-query'
import { useEffect, type ReactNode } from 'react'
import { uiSettingsQuery } from '../api/queries'
import { settingsUiBrowserLocaleConfirm } from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'
import { I18nProvider } from './I18n'
import type { InitialLocales } from './initial-locale'
import { isLocale, LOCALE_COOKIE } from './locales'

export function AppLocale({ initial, children }: { initial: InitialLocales; children: ReactNode }) {
  const settings = useQuery(uiSettingsQuery())
  const preference = settings.data?.locale
  const locale = isLocale(preference) ? preference : preference === 'browser' ? initial.browser : initial.page

  useEffect(() => {
    if (!settings.isSuccess) return
    const controller = new AbortController()
    void unwrap(settingsUiBrowserLocaleConfirm({ body: { locale: initial.browser }, signal: controller.signal, throwOnError: true })).catch(() => {})
    return () => controller.abort()
  }, [settings.isSuccess, initial.browser])

  useEffect(() => {
    if (preference === undefined) return
    // The server row remains authoritative. This low-risk mirror keeps refreshes consistent
    // during hydration, including in deployments where the web process has no API upstream.
    const value = isLocale(preference) ? preference : ''
    const maxAge = value ? 31_536_000 : 0
    const secure = window.location.protocol === 'https:' ? '; Secure' : ''
    document.cookie = `${LOCALE_COOKIE}=${value}; Max-Age=${maxAge}; Path=/; SameSite=Lax${secure}`
  }, [preference])

  return <I18nProvider locale={locale} browserLocale={initial.browser} ready={settings.isSuccess || settings.isError}>{children}</I18nProvider>
}

// i18n-exempt: Route paths, query keys, API error codes and CSS classes.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useRef, useState } from 'react'
import { mcpSettingsUpdate } from '../api/generated/sdk.gen'
import type { McpSettingsOut } from '../api/generated/types.gen'
import { accountsQuery, mcpSettingsQuery } from '../api/queries'
import { errorCode } from '../api/unwrap'
import { Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import { SettingsSaveScope, useSettingsSlice } from './SettingsSave'
import './settings.css'

function sameAccounts(a: string[], b: string[]) {
  return a.length === b.length && a.every(id => b.includes(id))
}

export function McpAccountSettings() {
  return <SettingsSaveScope><McpAccountSettingsForm /></SettingsSaveScope>
}

function McpAccountSettingsForm() {
  const { texts } = useI18n()
  const t = texts.mcpAccounts
  const client = useQueryClient()
  const settings = useQuery(mcpSettingsQuery())
  const accounts = useQuery(accountsQuery())
  const [edit, setEdit] = useState<McpSettingsOut>()
  const [status, setStatus] = useState<'saving' | 'saved' | 'conflict' | 'invalid' | 'error'>()
  const saving = useRef(false)
  const choices = accounts.data?.filter(account => account.is_active) ?? []
  const shown = edit ?? settings.data
  const hasChange = Boolean(
    edit && (!settings.data || edit.version !== settings.data.version || !sameAccounts(edit.account_ids, settings.data.account_ids)),
  )
  const dirty = hasChange || status === 'conflict' || status === 'invalid' || status === 'error'
  const canSave = hasChange && status !== 'saving' && status !== 'conflict'

  function toggle(id: string, checked: boolean) {
    if (!shown) return
    const ids = checked ? [...shown.account_ids, id] : shown.account_ids.filter(item => item !== id)
    if (settings.data && shown.version === settings.data.version && sameAccounts(ids, settings.data.account_ids)) {
      setEdit(undefined)
    } else {
      setEdit({ version: shown.version, account_ids: ids })
    }
    setStatus(undefined)
  }

  function discard() {
    if (saving.current) return
    const needsReload = status === 'conflict' || status === 'invalid' || status === 'error'
    setEdit(undefined)
    setStatus(undefined)
    if (needsReload) void reload()
  }

  async function reload() {
    const [current, connected] = await Promise.all([settings.refetch(), accounts.refetch()])
    if (current.isSuccess && connected.isSuccess) {
      setEdit(undefined)
      setStatus(undefined)
    }
  }

  async function save() {
    if (!edit || !canSave || saving.current) return
    saving.current = true
    setStatus('saving')
    try {
      const result = await mcpSettingsUpdate({ body: { expected_version: edit.version, account_ids: edit.account_ids } })
      if (!result.data) {
        const code = errorCode(result.error)
        if (code === 'mcp_settings_invalid') setStatus('invalid')
        else setStatus(code === 'version_conflict' ? 'conflict' : 'error')
        return
      }
      await client.cancelQueries({ queryKey: mcpSettingsQuery().queryKey })
      const saved = result.data
      client.setQueryData(mcpSettingsQuery().queryKey, current => current && current.version > saved.version ? current : saved)
      setEdit(undefined)
      setStatus('saved')
    } catch {
      setStatus('error')
    } finally {
      saving.current = false
    }
  }

  useSettingsSlice('mcp-accounts', { dirty, canSave, busy: status === 'saving', save, discard })

  return <section className="le-swipe" aria-labelledby="le-mcp-accounts-title">
    <h2 id="le-mcp-accounts-title" className="heading">{t.title}</h2>
    <p className="le-swipe-intro">{t.description}</p>
    <Notice tone="info" live={false}>{t.privacyNotice}</Notice>
    {settings.isError || accounts.isError
      ? <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { void reload() } }}>{texts.settingsUi.error}</Notice>
      : !shown || accounts.isPending ? <p role="status">{texts.settingsUi.loading}</p>
      : <div>
        {choices.length ? <fieldset className="le-chat-account-list" disabled={status === 'saving' || status === 'conflict'}>
          <legend className="le-sr">{t.title}</legend>
          {choices.map(account => <label key={account.id}>
            <input
              type="checkbox"
              checked={shown.account_ids.includes(account.id)}
              onChange={event => toggle(account.id, event.target.checked)}
            />
            <span>
              {account.display_name || account.email_address}
              {account.display_name && account.display_name !== account.email_address && <small>{account.email_address}</small>}
            </span>
          </label>)}
        </fieldset> : <Link to="/accounts">{texts.accounts.connect}</Link>}
        {status === 'saved' && <Notice tone="success">{texts.settingsUi.saved}</Notice>}
        {status === 'invalid' && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { void reload() } }}>{texts.errors.mcp_settings_invalid}</Notice>}
        {status === 'error' && <Notice tone="danger">{texts.settingsUi.saveError}</Notice>}
        {status === 'conflict' && <Notice tone="danger" action={{ label: texts.settingsUi.reload, onClick: () => { void reload() } }}>{t.conflict}</Notice>}
      </div>}
    <div className="le-mcp-details">
      <h3>{t.toolsHeading}</h3>
      <ul className="le-mcp-tools">
        {t.tools.map(item => <li key={item}>{item}</li>)}
      </ul>
      <h3>{t.revokeTitle}</h3>
      <p>{t.revokeBody}</p>
      <div><Link to="/settings/access" className="le-btn le-btn--secondary">{t.openAccessSwitch}</Link></div>
    </div>
  </section>
}

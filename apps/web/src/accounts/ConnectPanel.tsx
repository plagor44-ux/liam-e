import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState, type Ref } from 'react'
import { mailAccountConnectConnector, mailAccountOauthAuthorize } from '../api/generated/sdk.gen'
import type { AccountOut, ImapPresetOut } from '../api/generated/types.gen'
import { accountOptionsQuery, connectorsQuery } from '../api/queries'
import { apiErrorMessage } from '../api/unwrap'
import { Button, Icon, Notice, Tooltip } from '../ds'
import { GmailCapabilitiesNotice } from './GmailCapabilitiesNotice'
import { useI18n } from '../i18n/I18n'
import { ImapForm } from './ImapForm'
import { redirect } from './redirect'

type Choice = 'gmail' | 'outlook' | 'other'
const noPresets: ImapPresetOut[] = []
export function ConnectPanel({ headingRef, reconnect, onConnected }: { headingRef: Ref<HTMLHeadingElement>; reconnect?: { address: string; sequence: number }; onConnected: (account: AccountOut) => void }) {
  const { texts } = useI18n()
  const options = useQuery(accountOptionsQuery()), connectors = useQuery(connectorsQuery())
  const [choice, setChoice] = useState<Choice>(), [initialAddress, setInitialAddress] = useState(''), [reconnectKey, setReconnectKey] = useState(0)
  const [pending, setPending] = useState(false), [error, setError] = useState<string>()
  const pendingRef = useRef(false), panelRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!reconnect) return
    setChoice('other'); setInitialAddress(reconnect.address); setReconnectKey(reconnect.sequence); setError(undefined)
    panelRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [reconnect])
  const choose = (next: Choice) => { setChoice(next); setInitialAddress(''); setReconnectKey(0); setError(undefined) }
  const cancel = () => { const previous = choice; setChoice(undefined); setError(undefined); if (previous) panelRef.current?.querySelector<HTMLButtonElement>(`[data-choice="${previous}"]`)?.focus() }
  const connector = choice === 'gmail' ? options.data?.gmail_connector : choice === 'outlook' ? options.data?.outlook_connector : false
  const provider = choice === 'outlook' ? texts.accounts.outlook : texts.accounts.gmail
  const company = choice === 'outlook' ? 'Microsoft' : 'Google' // i18n-exempt
  const connect = async () => {
    if (pendingRef.current || !choice || choice === 'other') return
    pendingRef.current = true; setPending(true); setError(undefined)
    try {
      if (connector) {
        const names = choice === 'gmail' ? ['gmail', 'google_mail'] : ['outlook']
        const name = connectors.data?.find(item => names.includes(item.name))?.name ?? names[0]!
        const result = await mailAccountConnectConnector({ path: { name } })
        if (result.error || !result.data) throw result.error
        setChoice(undefined); onConnected(result.data)
      } else {
        const result = await mailAccountOauthAuthorize({ path: { provider: choice === 'gmail' ? 'google' : 'microsoft' } })
        if (result.error || !result.data) throw result.error
        redirect.to(result.data.authorization_url)
      }
    } catch (cause) {
      setError(apiErrorMessage(cause, texts, texts.accounts.actionError))
    } finally { pendingRef.current = false; setPending(false) }
  }
  const rows: { key: Choice; title: string; sub: string }[] = []
  if (options.data?.gmail_connector || options.data?.google_oauth) rows.push({ key: 'gmail', title: texts.accounts.gmail, sub: options.data.gmail_connector ? texts.accounts.gmailConnectorSub : texts.accounts.googleSub })
  if (options.data?.outlook_connector || options.data?.microsoft_oauth) rows.push({ key: 'outlook', title: texts.accounts.outlook, sub: options.data.outlook_connector ? texts.accounts.outlookConnectorSub : texts.accounts.microsoftSub })
  if (!options.isPending || choice === 'other') rows.push({ key: 'other', title: texts.accounts.other, sub: texts.accounts.otherSub })
  const gmailConnector = choice === 'gmail' && connector
  return <section className="le-accounts-connect" ref={panelRef}><h2 className="heading" ref={headingRef} tabIndex={-1}>{texts.accounts.connect}</h2>
    {options.isPending && <><span className="le-sr" role="status">{texts.accounts.providersLoading}</span><div aria-hidden="true">{Array.from({ length: 3 }, (_, i) => <div key={i} className="le-connect-placeholder"><span /></div>)}</div></>}
    {options.isError && !options.data && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { void options.refetch() } }}>{texts.accounts.providersError}</Notice>}
    <div className="le-connect-options" role="group" aria-label={texts.accounts.providersLabel}>{rows.map(row => <Button key={row.key} data-choice={row.key} variant="secondary" className="le-connect-option" aria-pressed={choice === row.key} disabled={pending} onClick={() => choose(row.key)}><span className="le-connect-icon"><Icon name={row.key === 'other' ? 'at-sign' : 'mail-open'} /></span><span className="le-connect-option-text"><strong>{row.title}</strong><small className="le-muted">{row.sub}</small></span></Button>)}</div>
    {choice === 'other' ? <ImapForm key={reconnectKey} presets={options.data?.imap_presets ?? noPresets} initialAddress={initialAddress} focusSecret={Boolean(reconnectKey)} onBusyChange={setPending} onCancel={cancel} onConnected={account => { setChoice(undefined); onConnected(account) }} /> : choice && <div className="le-connect-detail"><p>{gmailConnector ? texts.accounts.gmailConnectorLogin : connector ? texts.accounts.connectorDescription(provider, company) : texts.accounts.loginDescription(company)}</p>{gmailConnector && <GmailCapabilitiesNotice />}{error && <Notice tone="danger">{error}</Notice>}<div className="le-connect-actions"><Tooltip content={connector ? texts.accounts.connectorTip(provider) : texts.accounts.loginTip(company)}><Button variant="primary" disabled={pending} aria-busy={pending} onClick={() => { void connect() }}>{connector ? texts.accounts.connectProvider(provider) : texts.accounts.loginProvider(company)}</Button></Tooltip><Button variant="ghost" disabled={pending} onClick={cancel}>{texts.controls.cancel}</Button></div></div>}
  </section>
}

import { useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { mailFolderCreateAndAssign } from '../api/generated/sdk.gen'
import { errorCode, unwrap } from '../api/unwrap'
import { Button, Notice, TextField } from '../ds'
import { useI18n } from '../i18n/I18n'
import { PreferenceDialog } from '../settings/Preferences'

export function CreateFilingDialog({ messageId, kind, onClose, onSuccess }: { messageId: string; kind: 'folder' | 'label'; onClose(): void; onSuccess(): void }) {
  const { texts } = useI18n(), t = texts.mailFiling
  const client = useQueryClient()
  const [name, setName] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState<string>()
  const pending = useRef(false)
  const save = async () => {
    if (pending.current || !name.trim()) return
    pending.current = true; setBusy(true); setError(undefined)
    try {
      await unwrap(mailFolderCreateAndAssign({ body: { message_id: messageId, name: name.trim(), kind }, throwOnError: true }))
      for (const key of ['mail-folders', 'mail-quick-actions', 'messages', 'message', 'summary']) void client.invalidateQueries({ queryKey: [key] })
      onSuccess(); onClose()
    } catch (failure) { setError(errorCode(failure) ?? 'request_failed') } finally { pending.current = false; setBusy(false) }
  }
  return <PreferenceDialog title={kind === 'folder' ? t.createFolderForMail : t.createLabelForMail} close={() => { if (!busy) onClose() }}>
    <p>{t.createForMailHint}</p>
    <form onSubmit={event => { event.preventDefault(); void save() }}>
      <TextField label={t.name} value={name} maxLength={120} onChange={event => setName(event.target.value)} required />
      {error && <Notice tone="danger">{error === 'folder_exists' ? t.destinationExists : error === 'message_not_found' ? t.sourceGone : error === 'folder_invalid' ? t.error : t.createUnknown}</Notice>}
      <div className="le-dialog-actions"><Button variant="secondary" disabled={busy} onClick={onClose}>{t.cancel}</Button><Button type="submit" variant="primary" disabled={busy || !name.trim()}>{t.save}</Button></div>
    </form>
  </PreferenceDialog>
}

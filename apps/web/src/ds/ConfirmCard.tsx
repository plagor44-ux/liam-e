import { useState } from 'react'
import type { ProposalItem, SendDraft } from '../api/generated/types.gen'
import { useI18n } from '../i18n/I18n'
import { Button } from './Button'
import { TextField } from './TextField'
export function ConfirmCard({ title, reason, items, irreversibleNote, confirmLabel, draft, onConfirm, onCancel, busy, confirmDisabled = false, deselected: controlledDeselected, onDeselectedChange, body: controlledBody, onBodyChange }: { title: string; reason: string; items: ProposalItem[]; irreversibleNote?: string; confirmLabel: (n: number) => string; draft?: SendDraft; onConfirm: (selectedIds: string[], body?: string) => void; onCancel: () => void; busy?: boolean; confirmDisabled?: boolean; deselected?: Set<string>; onDeselectedChange?: (value: Set<string>) => void; body?: string; onBodyChange?: (value: string) => void }) {
  const { texts } = useI18n()
  const [localDeselected, setLocalDeselected] = useState<Set<string>>(() => new Set())
  const [localBody, setLocalBody] = useState(draft?.body ?? '')
  const deselected = controlledDeselected ?? localDeselected
  const body = controlledBody ?? localBody
  const setDeselected = (value: Set<string>) => { if (controlledDeselected === undefined) setLocalDeselected(value); onDeselectedChange?.(value) }
  const setBody = (value: string) => { if (controlledBody === undefined) setLocalBody(value); onBodyChange?.(value) }
  const selected = items.filter(item => !deselected.has(item.id)).map(item => item.id)
  // A send proposal carries only its draft and no items, so it confirms without a selection.
  const canConfirm = items.length ? selected.length > 0 : Boolean(draft)
  return <section className="le-confirm le-rise"><div className="le-confirm-head"><h3 className="le-confirm-title">{title}</h3><span className="le-hint">{reason}</span></div>{items.length > 0 && <ul className="le-confirm-list">{items.map(item => <li key={item.id} className="le-confirm-item"><label><span className="le-confirm-item-name"><input className="le-check" type="checkbox" checked={!deselected.has(item.id)} onChange={event => { const next = new Set(deselected); if (event.target.checked) next.delete(item.id); else next.add(item.id); setDeselected(next) }} /> <strong>{texts.mail.subjectText(item.title)}</strong></span><span className="le-hint">{item.subtitle}</span></label></li>)}</ul>}{draft && <div className="le-confirm-draft"><p>{texts.controls.recipient}: {draft.to.join(', ')}</p>{draft.cc.length > 0 && <p>{texts.controls.copy}: {draft.cc.join(', ')}</p>}<p>{texts.controls.subject}: {texts.mail.subjectText(draft.subject)}</p><TextField label={texts.controls.draftBody} multiline value={body} onChange={event => setBody(event.target.value)} />{draft.quote && <figure className="le-confirm-quote"><figcaption className="le-hint">{draft.mode === 'forward' ? texts.controls.forwarded : texts.controls.quoted}</figcaption><blockquote tabIndex={0} aria-label={draft.mode === 'forward' ? texts.controls.forwarded : texts.controls.quoted}>{draft.quote}</blockquote></figure>}</div>}<div className="le-confirm-foot"><Button variant="primary" disabled={busy || confirmDisabled || !canConfirm} onClick={() => onConfirm(selected, draft ? body : undefined)}>{confirmLabel(selected.length)}</Button><Button variant="ghost" disabled={busy} onClick={onCancel}>{texts.controls.cancel}</Button>{irreversibleNote && <span className="le-hint">{irreversibleNote}</span>}</div></section>
}

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { mailMessageHtmlGet } from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'
import { Button } from '../ds'
import { useI18n } from '../i18n/I18n'
import { prepareMailDocument } from './mail-html'
import { paragraphs } from './paragraphs'

export function MailHtml({ id, text, localOnly }: { id: string; text: string; localOnly: boolean }) {
  const { texts } = useI18n()
  const [images, setImages] = useState(false)
  const query = useQuery({
    queryKey: ['mail', 'html', id], enabled: !localOnly,
    queryFn: ({ signal }) => unwrap(mailMessageHtmlGet({ path: { message_id: id }, signal, throwOnError: true })),
    retry: false, gcTime: 0,
  })
  const html = query.data?.html
  const prepared = useMemo(() => html && typeof window !== 'undefined' ? prepareMailDocument(html, images, texts.mailHtml.imagePlaceholder) : null, [html, images, texts.mailHtml.imagePlaceholder])
  if (!prepared) {
    return text ? <div className="le-mail-text body">{paragraphs(text).map((part, index) => <p key={index}>{part}</p>)}</div> : null
  }
  return <div className="le-mail-html">
    {!images && prepared.hasRemoteImages && <Button variant="secondary" onClick={() => setImages(true)}>{texts.mailHtml.showImages}</Button>}
    <iframe title={texts.mailHtml.title} sandbox="allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" srcDoc={prepared.document} />
  </div>
}

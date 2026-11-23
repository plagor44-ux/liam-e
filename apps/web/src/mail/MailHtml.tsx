import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { mailMessageHtmlGet } from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'
import { Button } from '../ds'
import { useI18n } from '../i18n/I18n'
import { useTheme } from '../shell/theme'
import { prepareMailDocument } from './mail-html'
import { paragraphs } from './paragraphs'

export function MailHtml({ id, text, localOnly }: { id: string; text: string; localOnly: boolean }) {
  const { texts } = useI18n()
  const [images, setImages] = useState(false)
  const [original, setOriginal] = useState(false)
  const theme = useTheme()
  const [systemDark, setSystemDark] = useState(false)
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const update = () => setSystemDark(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  const dark = theme === 'dark' || (theme === 'system' && systemDark)
  const query = useQuery({
    queryKey: ['mail', 'html', id], enabled: !localOnly,
    queryFn: ({ signal }) => unwrap(mailMessageHtmlGet({ path: { message_id: id }, signal, throwOnError: true })),
    retry: false, gcTime: 0,
  })
  const html = query.data?.html
  const prepared = useMemo(() => html && typeof window !== 'undefined' ? prepareMailDocument(html, images, texts.mailHtml.imagePlaceholder, dark && !original) : null, [html, images, texts.mailHtml.imagePlaceholder, dark, original])
  if (!prepared) {
    return text ? <div className="le-mail-text body">{paragraphs(text).map((part, index) => <p key={index}>{part}</p>)}</div> : null
  }
  return <div className="le-mail-html">
    {!images && prepared.hasRemoteImages && <Button variant="secondary" onClick={() => setImages(true)}>{texts.mailHtml.showImages}</Button>}
    {dark && <Button variant="secondary" onClick={() => setOriginal(value => !value)}>{original ? texts.mailHtml.showDark : texts.mailHtml.showOriginal}</Button>}
    <iframe title={texts.mailHtml.title} style={{ colorScheme: dark && !original ? 'dark' : 'light' }} sandbox="allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" srcDoc={prepared.document} />
  </div>
}

import { intlLocale, type Locale } from '../i18n/locales'

// Display order only: rule execution continues to use the stored priority.
export function sortNamed<T extends { name: string; id?: string }>(rows: readonly T[], locale: Locale): T[] {
  const compare = new Intl.Collator(intlLocale[locale], { sensitivity: 'base' }).compare
  return [...rows].sort((a, b) => compare(a.name, b.name) || (a.id ?? '').localeCompare(b.id ?? '', 'en'))
}

export function sortDestinations<T extends { kind: string; name: string; id?: string }>(rows: readonly T[], locale: Locale): T[] {
  return sortNamed(rows, locale).sort((a, b) => Number(a.kind !== 'folder') - Number(b.kind !== 'folder'))
}

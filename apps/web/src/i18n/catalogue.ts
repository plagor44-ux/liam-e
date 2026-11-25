import { de } from './de'
import { en } from './en'
import type { Locale } from './locales'

type CatalogueShape<T> =
  T extends string ? string :
  T extends (...args: infer Args) => infer Text ? (...args: Args) => CatalogueShape<Text> :
  T extends object ? { [Key in keyof T]: CatalogueShape<T[Key]> } : T

export type Catalogue = CatalogueShape<typeof de>

const catalogues = { de, en } satisfies Record<Locale, Catalogue>

export const catalogue = (locale: Locale): Catalogue => catalogues[locale]

/** Every supported catalogue, for input the owner may type in either language. */
export const everyCatalogue: readonly Catalogue[] = Object.values(catalogues)

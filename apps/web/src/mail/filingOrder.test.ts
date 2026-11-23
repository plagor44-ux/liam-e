import { expect, test } from 'bun:test'
import { sortDestinations, sortNamed } from './filingOrder'

test.each(['de', 'en'] as const)('destination order ignores accents and case with deterministic ties (%s)', locale => {
  const rows = [{ id: 'z', name: 'Zebra' }, { id: 'b', name: 'äpfel' }, { id: 'a', name: 'Apfel' }, { id: 'c', name: 'Banane' }]
  expect(sortNamed(rows, locale).map(row => row.id)).toEqual(['a', 'b', 'c', 'z'])
  expect(rows.map(row => row.id)).toEqual(['z', 'b', 'a', 'c'])
})

test('destination groups keep folders before labels while sorting inside each kind', () => {
  const rows = [{ id: 'b', name: 'Beta', kind: 'label' }, { id: 'a', name: 'Alpha', kind: 'label' }, { id: 'f', name: 'Zulu', kind: 'folder' }]
  expect(sortDestinations(rows, 'en').map(row => row.id)).toEqual(['f', 'a', 'b'])
})


test.each(['de', 'en'] as const)('names use numeric collation (%s)', locale => {
  const rows = [{ id: '10', name: 'Archiv 10' }, { id: '2', name: 'Archiv 2' }]
  expect(sortNamed(rows, locale).map(row => row.id)).toEqual(['2', '10'])
})

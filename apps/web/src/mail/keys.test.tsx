import { afterEach, expect, mock, test } from 'bun:test'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { useInboxKeys } from './useInboxKeys'

const originalMatchMedia = window.matchMedia
function setup(desktop: boolean) {
  window.matchMedia = ((query: string) => ({ matches: desktop && query.includes('1024px'), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  const archive = mock(() => {})
  const folder = mock(() => {}), label = mock(() => {}), overview = mock(() => {})
  function Harness() {
    const [selected, setSelected] = useState(0)
    useInboxKeys({ onNext: () => setSelected(n => n + 1), onPrev: () => setSelected(n => n - 1), onOpen: () => {}, onArchive: archive, onToggleRead: () => {}, onTrash: () => {}, onFolder: folder, onLabel: label, onOverview: overview })
    return <><input aria-label="Search" /><div tabIndex={0} aria-label="List">{selected}</div></>
  }
  render(<Harness />)
  return { archive, folder, label, overview, list: screen.getByLabelText('List'), input: screen.getByLabelText('Search') }
}
afterEach(() => { cleanup(); mock.restore(); window.matchMedia = originalMatchMedia })

test('ignores keys typed into fields and handles list keys', () => {
  const { archive, list, input } = setup(true)
  fireEvent.keyDown(input, { key: 'e' })
  expect(archive).toHaveBeenCalledTimes(0)
  fireEvent.keyDown(list, { key: 'e', ctrlKey: true })
  expect(archive).toHaveBeenCalledTimes(0)
  fireEvent.keyDown(list, { key: 'e' })
  expect(archive).toHaveBeenCalledTimes(1)
  fireEvent.keyDown(list, { key: 'j' })
  expect(list.textContent).toBe('1')
})

test('filing and overview shortcuts are suppressed in editing contexts and with modifiers', () => {
  const { folder, label, overview, list, input } = setup(true)
  for (const key of ['v', 'l', '?']) fireEvent.keyDown(input, { key })
  fireEvent.keyDown(list, { key: 'v', ctrlKey: true })
  fireEvent.keyDown(list, { key: 'l', isComposing: true })
  expect(folder).toHaveBeenCalledTimes(0)
  expect(label).toHaveBeenCalledTimes(0)
  expect(overview).toHaveBeenCalledTimes(0)
  fireEvent.keyDown(list, { key: 'v' }); fireEvent.keyDown(list, { key: 'l' }); fireEvent.keyDown(list, { key: '?', shiftKey: true })
  expect(folder).toHaveBeenCalledTimes(1)
  expect(label).toHaveBeenCalledTimes(1)
  expect(overview).toHaveBeenCalledTimes(1)
})

test('shortcuts stay inactive in editable content, selects and while a menu is open', () => {
  const { folder, list } = setup(true)
  const editable = document.createElement('div'), select = document.createElement('select'), menu = document.createElement('div')
  editable.setAttribute('contenteditable', 'true'); menu.setAttribute('role', 'menu')
  document.body.append(editable, select)
  fireEvent.keyDown(editable, { key: 'v' }); fireEvent.keyDown(select, { key: 'v' })
  document.body.append(menu)
  fireEvent.keyDown(list, { key: 'v' })
  expect(folder).toHaveBeenCalledTimes(0)
  editable.remove(); select.remove(); menu.remove()
})

test('does nothing behind an open dialog, wherever the focus is', () => {
  const { archive, list } = setup(true)
  const dialog = document.createElement('div')
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true')
  document.body.append(dialog)
  try {
    for (const target of [document.body, list]) { fireEvent.keyDown(target, { key: 'e' }); fireEvent.keyDown(target, { key: 'j' }) }
    expect(archive).toHaveBeenCalledTimes(0)
    expect(list.textContent).toBe('0')
  } finally { dialog.remove() }
  fireEvent.keyDown(document.body, { key: 'e' })
  expect(archive).toHaveBeenCalledTimes(1)
})

test('does nothing on mobile', () => {
  const { archive, list } = setup(false)
  fireEvent.keyDown(list, { key: 'e' })
  fireEvent.keyDown(list, { key: 'j' })
  expect(archive).toHaveBeenCalledTimes(0)
  expect(list.textContent).toBe('0')
})

import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode, type ButtonHTMLAttributes, type Ref } from 'react'
import { createPortal } from 'react-dom'
import { useFloating, type Align, type Side } from './floating'
import { Icon, type IconName } from './Icon'
type TriggerProps = ButtonHTMLAttributes<HTMLButtonElement> & { ref: Ref<HTMLButtonElement> }
// A disabled item stays focusable (aria-disabled) and says why in its hint line.
// Items with `checked` form a choice (menuitemradio): the chosen one carries a check mark and takes the focus on opening.
// `placement` is the preferred side and edge: below (default) or above the trigger, aligned to its start or end. Like a
// tooltip the menu hangs in <body> with fixed coordinates from floating.ts: it flips, shifts into the viewport and
// scrolls within the room it has, and no scroll area or stacking context around the trigger can cut it off or cover
// it. Tab leaves it for the trigger, so the focus order stays that of the page.
type Placement = 'down-start' | 'down-end' | 'up-start' | 'up-end'
export type MenuItem = { label: string; icon?: IconName; tone?: 'danger'; disabled?: boolean; hint?: string; shortcut?: string; checked?: boolean | 'mixed'; checkable?: 'checkbox' } & (
  { onSelect: () => void; href?: undefined } | { href: string; onSelect?: undefined }
)
type MenuProps = {
  trigger: (props: TriggerProps) => ReactNode
  items?: MenuItem[]
  groups?: { label: string; items: MenuItem[] }[]
  placement?: Placement
  label?: string
  onOpen?: () => void
  openRequest?: number
}
export function Menu({ trigger, items = [], groups, placement = 'down-start', label, onOpen, openRequest }: MenuProps) {
  const [open, setOpen] = useState(false)
  const anchorRef = useRef<HTMLSpanElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const id = useId()
  const [vertical, edge] = placement.split('-') as ['down' | 'up', Align]
  useEffect(() => {
    if (openRequest === undefined || triggerRef.current?.disabled) return
    document.dispatchEvent(new Event('liam-menu-open'))
    setOpen(true)
  }, [openRequest])
  const side: Side = vertical === 'up' ? 'top' : 'bottom'
  // Back to the trigger without scrolling to it: it may just have left the screen.
  const close = () => { setOpen(false); triggerRef.current?.focus({ preventScroll: true }) }
  // A trigger scrolled out of view closes its menu; focus inside the menu goes back to the trigger.
  const spot = useFloating(open, anchorRef, menuRef, side, edge, () => { if (menuRef.current?.contains(document.activeElement)) close(); else setOpen(false) })
  const placed = spot !== null
  const position = { left: spot?.left ?? 0, top: spot?.top ?? 0, visibility: placed ? 'visible' : 'hidden', '--le-menu-room': spot ? `${spot.maxHeight}px` : undefined } as CSSProperties
  // The focus moves in once the menu is placed: an element still hidden cannot take it.
  useEffect(() => {
    if (!open || !placed) return
    if (menuRef.current?.contains(document.activeElement)) return
    const first = menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]') ?? menuRef.current?.querySelector<HTMLElement>('[role^="menuitem"]')
    first?.focus()
  }, [open, placed, groups, items])
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (!menuRef.current?.contains(target) && !triggerRef.current?.contains(target)) setOpen(false)
    }
    const onOtherMenuOpen = () => setOpen(false)
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('liam-menu-open', onOtherMenuOpen)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('liam-menu-open', onOtherMenuOpen)
    }
  }, [open])
  function renderItem(item: MenuItem, index: number) {
    const content = <><span className="le-menu-title">{item.label}{item.shortcut && <> ({item.shortcut})</>}</span>{item.checked === true ? <Icon name="check" /> : item.icon && <Icon name={item.icon} />}{item.hint && <span className="le-menu-sub">{item.hint}</span>}</>
    if (item.href !== undefined) return <a key={index} role="menuitem" className="le-menu-item" href={item.href} target="_blank" rel="noreferrer" onClick={close}>{content}</a>
    return <button key={index} type="button" role={item.checked === undefined ? 'menuitem' : item.checkable === 'checkbox' ? 'menuitemcheckbox' : 'menuitemradio'} aria-checked={item.checked} aria-disabled={item.disabled || undefined} className={`le-menu-item${item.tone === 'danger' ? ' le-menu-item--danger' : ''}`} onClick={() => { if (item.disabled) return; close(); item.onSelect() }}>{content}</button>
  }
  return <span ref={anchorRef} className="le-menu-anchor">{trigger({ ref: triggerRef, 'aria-haspopup': 'menu', 'aria-expanded': open, 'aria-controls': open ? id : undefined, onClick: () => { if (!open) { document.dispatchEvent(new Event('liam-menu-open')); onOpen?.() } setOpen(!open) } })}{open && createPortal(<div ref={menuRef} id={id} role="menu" aria-label={label} className={`le-menu le-rise le-menu--${placement}${groups ? ' le-menu--grouped' : ''}`} style={position} onKeyDown={(event) => {
    if (event.key === 'Escape') { event.preventDefault(); close() }
    // No preventDefault: the browser moves on from the trigger, as if the menu had never been open.
    if (event.key === 'Tab') close()
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const nodes = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]') ?? [])
      const index = nodes.indexOf(document.activeElement as HTMLElement)
      nodes[(index + (event.key === 'ArrowDown' ? 1 : -1) + nodes.length) % nodes.length]?.focus()
    }
  }}>{groups?.map(group => <div key={group.label} role="group" aria-label={group.label}><div className="le-menu-group-label" aria-hidden="true">{group.label}</div>{group.items.map(renderItem)}</div>)}{items.map(renderItem)}</div>, document.body)}</span>
}

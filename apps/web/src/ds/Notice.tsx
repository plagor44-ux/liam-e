import type { ReactNode } from 'react'
import { Icon } from './Icon'
// DESIGN.md §6: the symbol beside the text of a notice is the small 16 px size.
export function Notice({ tone, children, action }: { tone: 'info' | 'success' | 'danger'; children: ReactNode; action?: { label: string; onClick: () => void } }) {
  return <div role={tone === 'danger' ? 'alert' : 'status'} className={`le-notice le-notice--${tone === 'info' ? 'neutral' : tone} le-rise`}>
    <Icon name={tone === 'success' ? 'check' : tone === 'danger' ? 'circle-alert' : 'info'} size="sm" />
    <div className="le-notice-body">
      {children}
      {action && <button type="button" className="le-link" onClick={action.onClick}>{action.label}</button>}
    </div>
  </div>
}

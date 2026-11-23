import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Icon, type IconName } from './Icon'
export function Chip({ pressed, quiet, className = '', type = 'button', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { pressed?: boolean; quiet?: boolean }) {
  return <button {...props} type={type} aria-pressed={pressed} className={`le-chip${quiet ? ' le-chip--quiet' : ''} ${className}`} />
}
export function Dot({ colorIndex, accent }: { colorIndex?: number; accent?: boolean }) {
  const color = colorIndex === undefined ? '' : ` le-dot--a${((colorIndex % 4 + 4) % 4) + 1}`
  return <span aria-hidden="true" className={`le-dot${accent ? ' le-dot--accent' : color}`} />
}
export function Tag({ tone = 'default', dot, icon, children, className = '' }: { tone?: 'default' | 'warm' | 'on-surface'; dot?: number; icon?: IconName; children?: ReactNode; className?: string }) {
  return <span className={`le-tag${tone === 'default' ? '' : ` le-tag--${tone}`} ${className}`}>{dot !== undefined && <Dot colorIndex={dot} />}{icon && <Icon name={icon} size="sm" />}{children}</span>
}
export function Badge({ children }: { children: ReactNode }) {
  return <span className="le-badge le-num">{typeof children === 'number' && children >= 1000 ? '999+' : children}</span>
}

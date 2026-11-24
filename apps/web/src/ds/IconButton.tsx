import type { ButtonHTMLAttributes } from 'react'
import { Icon, type IconName } from './Icon'
import { Tooltip } from './Tooltip'
export function IconButton({ label, icon, shortcut, tip, tipPosition, size = 'md', tone = 'default', className = '', type = 'button', ...props }: Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'size'> & { label: string; icon: IconName; shortcut?: string; tip?: string; tipPosition?: 'top' | 'right' | 'bottom' | 'bottom-end' | 'top-end'; size?: 'md' | 'lg'; tone?: 'default' | 'danger' }) {
  return <Tooltip pos={tipPosition} content={`${tip ?? label}${shortcut ? ` (${shortcut})` : ''}`}><button {...props} type={type} aria-label={label} className={`le-icon-btn${size === 'lg' ? ' le-icon-btn--lg' : ''}${tone === 'danger' ? ' le-icon-btn--danger' : ''} ${className}`}><Icon name={icon} /></button></Tooltip>
}

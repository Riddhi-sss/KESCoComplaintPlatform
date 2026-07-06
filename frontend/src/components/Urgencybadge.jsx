export function UrgencyBadge({ level }) {
  const map = {
    CRITICAL: 'badge-critical',
    HIGH: 'badge-high',
    MEDIUM: 'badge-medium',
    LOW: 'badge-low',
  }
  return (
    <span className={`badge ${map[level] || 'badge-gray'}`}>
      {level}
    </span>
  )
}
import { shell } from 'electron'

const LOCAL_HOSTNAMES = new Set(['localhost', 'localhost.localdomain', '0.0.0.0', '::', '::1'])

function isPrivateIpv4(hostname: string): boolean {
  const parts = hostname.split('.')
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return false
  const octets = parts.map(Number)
  if (octets.some((octet) => octet < 0 || octet > 255)) return false
  const [a, b] = octets
  return (
    a === 0 ||
    a === 10 ||
    a === 127 ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    a >= 224
  )
}

/**
 * Only public HTTP(S) destinations may leave the Electron trust boundary.
 * Governed citations, local files, loopback/private-network URLs and custom
 * protocols must be handled in-app or rejected instead of being delegated to
 * an OS protocol handler.
 */
export function isAllowedExternalUrl(rawUrl: string): boolean {
  if (typeof rawUrl !== 'string' || !rawUrl || rawUrl.length > 8192) return false
  let parsed: URL
  try {
    parsed = new URL(rawUrl)
  } catch {
    return false
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return false
  if (parsed.username || parsed.password) return false

  const hostname = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (!hostname || LOCAL_HOSTNAMES.has(hostname) || hostname.endsWith('.localhost')) return false
  if (isPrivateIpv4(hostname)) return false
  // Reject IPv6 literals other than public addresses. Unique-local, link-local,
  // unspecified and loopback ranges are never valid external destinations.
  if (hostname.includes(':')) {
    const compact = hostname.replace(/^0+(?=[0-9a-f])/g, '')
    if (compact === '1' || compact === '' || /^f[cd]/i.test(hostname) || /^fe[89ab]/i.test(hostname)) {
      return false
    }
  }
  return true
}

export async function openExternalSafely(rawUrl: string): Promise<boolean> {
  if (!isAllowedExternalUrl(rawUrl)) {
    console.warn(`[security] refused external URL: ${String(rawUrl).slice(0, 200)}`)
    return false
  }
  await shell.openExternal(rawUrl)
  return true
}

import type { SignalQualityDto } from '../types'

export function qualityNumber(quality: SignalQualityDto | null | undefined): number | null {
  const value = quality?.quality_score
  return quality?.status === 'ok' && typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Presentation only: unknown scores stay last; ties retain the existing order. */
export function compareQuality(a: SignalQualityDto | null | undefined, b: SignalQualityDto | null | undefined): number {
  const left = qualityNumber(a), right = qualityNumber(b)
  if (left == null) return right == null ? 0 : 1
  if (right == null) return -1
  return right - left
}

export function qualityText(value: number | null | undefined, digits = 1): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

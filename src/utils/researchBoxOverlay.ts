import type { IChartApi, ISeriesApi, ISeriesPrimitive, IPrimitivePaneView, IPrimitivePaneRenderer, Time, UTCTimestamp } from 'lightweight-charts'
import type { CanvasRenderingTarget2D } from 'fancy-canvas'
import { researchChartTime, type ResearchBar, type ResearchBox } from './researchBoxes'

/** Audit-only canvas primitive: no orders, persistent annotations or axis rescaling. */
export function researchBoxOverlay(chart: IChartApi, candles: ISeriesApi<'Candlestick'>, rows: ResearchBar[], boxes: ResearchBox[]): ISeriesPrimitive<Time> {
  const renderer: IPrimitivePaneRenderer = {
    draw(target: CanvasRenderingTarget2D) {
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const x = (i: number) => chart.timeScale().timeToCoordinate(researchChartTime(rows[i].ts) as UTCTimestamp)
        const y = (p: number) => candles.priceToCoordinate(p)
        ctx.save()
        ctx.beginPath(); ctx.rect(0, 0, mediaSize.width, mediaSize.height); ctx.clip()
        ctx.font = '11px "Segoe UI", sans-serif'
        for (const box of boxes) {
          const start = x(box.startIndex); const confirm = x(box.confirmedIndex); const end = x(box.endIndex)
          const upper = y(box.upper); const lower = y(box.lower)
          const upperZone = y(box.upper - .1 * (box.upper - box.lower))
          const lowerZone = y(box.lower + .1 * (box.upper - box.lower))
          if ([start, confirm, end, upper, lower, upperZone, lowerZone].some(v => v == null)) continue
          if (end! < 0 || start! > mediaSize.width) continue
          const right = Math.max(confirm! + 1, end!)
          ctx.fillStyle = 'rgba(139,92,246,.08)'
          ctx.fillRect(start!, upper!, right - start!, upperZone! - upper!)
          ctx.fillStyle = 'rgba(2,132,199,.08)'
          ctx.fillRect(start!, lowerZone!, right - start!, lower! - lowerZone!)
          for (const [priceY, color, name] of [[upper!, '#8b5cf6', '研究上轨'], [lower!, '#0284c7', '研究下轨']] as const) {
            // Dashed retrospective formation segment is explicitly distinguished
            // from the solid post-confirmation, tradable geometry.
            ctx.strokeStyle = color; ctx.globalAlpha = .45; ctx.lineWidth = 1; ctx.setLineDash([4, 4])
            ctx.beginPath(); ctx.moveTo(start!, priceY); ctx.lineTo(confirm!, priceY); ctx.stroke()
            ctx.globalAlpha = 1; ctx.lineWidth = 2; ctx.setLineDash([])
            ctx.beginPath(); ctx.moveTo(confirm!, priceY); ctx.lineTo(right, priceY); ctx.stroke()
            const label = `${name} ${Number((name === '研究上轨' ? box.upper : box.lower).toFixed(3))}`
            const labelWidth = ctx.measureText(label).width + 6
            if (right - Math.max(0, start!) > labelWidth) {
              const labelX = Math.max(2, Math.min(right - labelWidth, mediaSize.width - labelWidth))
              const labelY = name === '研究上轨' ? priceY - 6 : priceY + 14
              ctx.fillStyle = 'rgba(255,255,255,.9)'; ctx.fillRect(labelX - 2, labelY - 11, labelWidth, 15)
              ctx.fillStyle = color; ctx.fillText(label, labelX, labelY)
            }
          }
          ctx.strokeStyle = 'rgba(100,116,139,.7)'; ctx.lineWidth = 1; ctx.setLineDash([3, 3])
          ctx.beginPath(); ctx.moveTo(confirm!, upper!); ctx.lineTo(confirm!, lower!); ctx.stroke(); ctx.setLineDash([])
          const confirmationLabel = `确认 · 回看${box.period}根 · 此后${box.endIndex - box.confirmedIndex}根`
          if (confirm! >= 0 && confirm! + ctx.measureText(confirmationLabel).width + 4 < mediaSize.width) {
            ctx.fillStyle = '#475569'; ctx.fillText(confirmationLabel, confirm! + 4, upper! + 15)
          }
          for (const touch of box.touches) {
            const px = x(touch.index)
            const py = y(touch.side === 'upper' ? rows[touch.index].high : rows[touch.index].low)
            if (px == null || py == null) continue
            ctx.fillStyle = touch.side === 'upper' ? '#8b5cf6' : '#0284c7'
            ctx.beginPath(); ctx.arc(px, py, 3, 0, Math.PI * 2); ctx.fill()
          }
        }
        ctx.restore()
      })
    },
  }
  const view: IPrimitivePaneView = { renderer: () => renderer, zOrder: () => 'top' }
  return { paneViews: () => [view] }
}

/**
 * 国内期货品种合约乘数与点数/金额盈亏核算工具
 */

export const DEFAULT_MULTIPLIERS: Record<string, number> = {
  // 黑色系
  RB: 10,   // 螺纹钢 10吨/手
  HC: 10,   // 热卷 10吨/手
  WR: 10,   // 线材 10吨/手
  I: 100,   // 铁矿石 100吨/手
  J: 100,   // 焦炭 100吨/手
  JM: 60,   // 焦煤 60吨/手
  SF: 5,    // 硅铁 5吨/手
  SM: 5,    // 锰硅 5吨/手
  SS: 5,    // 不锈钢 5吨/手

  // 有色金属
  CU: 5,    // 沪铜 5吨/手
  AL: 5,    // 沪铝 5吨/手
  ZN: 5,    // 沪锌 5吨/手
  PB: 5,    // 沪铅 5吨/手
  NI: 1,    // 沪镍 1吨/手
  SN: 1,    // 沪锡 1吨/手
  AO: 20,   // 氧化铝 20吨/手

  // 贵金属
  AU: 1000, // 沪金 1000克/手
  AG: 15,   // 沪银 15千克/手

  // 能源化工
  SC: 1000, // 原油 1000桶/手
  FU: 10,   // 燃油 10吨/手
  LU: 10,   // 低硫燃油 10吨/手
  BU: 10,   // 沥青 10吨/手
  PG: 20,   // LPG 20吨/手
  RU: 10,   // 橡胶 10吨/手
  BR: 5,    // 合成橡胶 5吨/手
  NR: 10,   // 20号胶 10吨/手
  SP: 10,   // 纸浆 10吨/手
  L: 5,     // 塑料 5吨/手
  PP: 5,    // 聚丙烯 5吨/手
  V: 5,     // PVC 5吨/手
  EB: 5,    // 苯乙烯 5吨/手
  EG: 10,   // 乙二醇 10吨/手
  PF: 5,    // 短纤 5吨/手
  TA: 5,    // PTA 5吨/手
  PX: 5,    // 对二甲苯 5吨/手
  SH: 30,   // 烧碱 30吨/手
  MA: 10,   // 甲醇 10吨/手
  FG: 20,   // 玻璃 20吨/手
  SA: 20,   // 纯碱 20吨/手
  UR: 20,   // 尿素 20吨/手

  // 农产品
  M: 10,    // 豆粕 10吨/手
  Y: 10,    // 豆油 10吨/手
  P: 10,    // 棕榈油 10吨/手
  RM: 10,   // 菜粕 10吨/手
  OI: 10,   // 菜油 10吨/手
  SR: 10,   // 白糖 10吨/手
  CF: 5,    // 棉花 5吨/手
  CY: 5,    // 棉纱 5吨/手
  C: 10,    // 玉米 10吨/手
  CS: 10,   // 淀粉 10吨/手
  JD: 5,    // 鸡蛋 5吨/手
  LH: 16,   // 生猪 16吨/手
  AP: 10,   // 苹果 10吨/手
  CJ: 5,    // 红枣 5吨/手
  PK: 5,    // 花生 5吨/手

  // 新能源/集运
  SI: 5,    // 工业硅 5吨/手
  LC: 1,    // 碳酸锂 1吨/手
  EC: 50,   // 集运指数 50点/手

  // 金融期货
  IF: 300,  // 沪深300 300元/点
  IH: 300,  // 上证50 300元/点
  IC: 200,  // 中证500 200元/点
  IM: 200,  // 中证1000 200元/点

  // 国债
  TS: 20000,// 2年期国债
  TF: 10000,// 5年期国债
  T: 10000, // 10年期国债
  TL: 10000,// 30年期国债
}

/** 提取代码前缀字母（如 CF0 -> CF, RB2501 -> RB, MA0 -> MA） */
export function getSymbolPrefix(symbol: string): string {
  if (!symbol) return ''
  const m = symbol.match(/^[A-Za-z]+/)
  return m ? m[0].toUpperCase() : symbol.toUpperCase()
}

/** 获取合约乘数 */
export function getSymbolMultiplier(symbol: string, variety?: string): number {
  const prefix = getSymbolPrefix(symbol)
  if (DEFAULT_MULTIPLIERS[prefix] != null) {
    return DEFAULT_MULTIPLIERS[prefix]
  }
  if (variety) {
    for (const [k, v] of Object.entries(DEFAULT_MULTIPLIERS)) {
      if (variety.toUpperCase().includes(k)) {
        return v
      }
    }
  }
  return 10 // 未收录默认按 10
}

export interface TradePnlResult {
  points: number
  amount: number
  pct: number
  isWin: boolean
}

/**
 * 计算单笔交易的点数与金额盈亏
 */
export function calcTradePnl(
  direction: string,
  entryPrice: number,
  exitPrice: number,
  symbol: string,
  lots: number = 1,
): TradePnlResult {
  const dir = direction === 'up' || direction === 'buy' || direction === 'long' ? 1 : -1
  const points = (exitPrice - entryPrice) * dir
  const multiplier = getSymbolMultiplier(symbol)
  const amount = points * multiplier * lots
  const pct = entryPrice > 0 ? (points / entryPrice) * 100 : 0
  const isWin = points > 0

  return {
    points,
    amount,
    pct,
    isWin,
  }
}

/**
 * 格式化点数展示（带正负号与一位小数）
 */
export function formatPnlPoints(points: number | null | undefined): string {
  if (points == null || Number.isNaN(points)) return '—'
  const sign = points > 0 ? '+' : ''
  const dec = Number.isInteger(points) ? points.toFixed(0) : points.toFixed(1)
  return `${sign}${dec} 点`
}

/**
 * 格式化金额展示（人民币元，带千分位与正负号）
 */
export function formatPnlAmount(amount: number | null | undefined): string {
  if (amount == null || Number.isNaN(amount)) return '—'
  const sign = amount > 0 ? '+' : ''
  const abs = Math.abs(amount)
  const formatted = abs >= 10000
    ? (amount / 10000).toFixed(2) + ' 万'
    : Math.round(amount).toLocaleString('zh-CN')
  return `${sign}¥${formatted}`
}

/**
 * 格式化 R 倍数
 */
export function formatRMultiple(r: number | null | undefined): string {
  if (r == null || Number.isNaN(r)) return '—'
  const sign = r >= 0 ? '+' : ''
  return `${sign}${r.toFixed(2)}R`
}

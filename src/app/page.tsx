import { readFileSync, existsSync } from 'fs'
import { join } from 'path'

// --- Types ---
interface Placement {
  model: string
  family: string
  href: string
  section: string
  slot: number
  total_in_section: number
  screenshot: string | null
  title?: string
}

interface Surface {
  surface: string
  url: string
  type?: string
  viewport_screenshot?: string
  placements: Placement[] | null
  self_check?: Record<string, unknown>
  error?: string
}

interface ScanData {
  timestamp: string
  surfaces: Surface[]
  scraper_health: { all_passed: boolean; failed_checks: unknown[] }
}

interface Change {
  type: 'new' | 'unchanged' | 'moved'
  delta?: number
  prev_slot?: number
}

// --- Constants ---
const SURFACE_ORDER = [
  'homepage', 'explore', 'search_all', 'search_video',
  'search_video_generation', 'search_text_to_video',
  'search_image_to_video', 'learn', 'blog',
]

const SURFACE_META: Record<string, { title: string; short: string }> = {
  homepage:                { title: 'Homepage', short: 'Home' },
  explore:                 { title: 'Explore Page', short: 'Explore' },
  search_all:              { title: 'Search: All Models', short: 'All' },
  search_video:            { title: 'Search: "video"', short: 'video' },
  search_video_generation: { title: 'Search: "video generation"', short: 'vid gen' },
  search_text_to_video:    { title: 'Search: "text to video"', short: 'T2V' },
  search_image_to_video:   { title: 'Search: "image to video"', short: 'I2V' },
  learn:                   { title: 'fal.ai/learn (Editorial)', short: 'Learn' },
  blog:                    { title: 'blog.fal.ai (Blog)', short: 'Blog' },
}

const MODEL_FAMILIES = ['HH 1.1', 'HH 1.0', 'Wan 2.7', 'Wan 2.6'] as const
const MODEL_COLORS: Record<string, string> = {
  'happy-horse-1.1': '#7c3aed',
  'happy-horse-1.0': '#a78bfa',
  'wan-2.7': '#0891b2',
  'wan-2.6': '#0ea5e9',
}
const FAM_COLOR_MAP: Record<string, string> = {
  'HH 1.1': 'happy-horse-1.1',
  'HH 1.0': 'happy-horse-1.0',
  'Wan 2.7': 'wan-2.7',
  'Wan 2.6': 'wan-2.6',
}

// --- Helpers ---
function getFamilyDisplay(model: string): string {
  if (model.startsWith('HH 1.1')) return 'HH 1.1'
  if (model.startsWith('HH 1.0')) return 'HH 1.0'
  if (model.startsWith('Wan 2.7')) return 'Wan 2.7'
  if (model.startsWith('Wan 2.6')) return 'Wan 2.6'
  return model
}

function slotClass(slot: number, total: number): string {
  if (total <= 0) return 'slot-bad'
  const ratio = slot / total
  if (ratio <= 0.33) return 'slot-good'
  if (ratio <= 0.66) return 'slot-ok'
  return 'slot-bad'
}

function modelColor(family: string): string {
  return MODEL_COLORS[family] || '#64748b'
}

// --- Comparison Logic ---
type PrevIndex = Map<string, Placement>

function buildPrevIndex(prevData: ScanData | null): PrevIndex {
  const index = new Map<string, Placement>()
  if (!prevData) return index
  for (const s of (prevData.surfaces || [])) {
    for (const p of s.placements || []) {
      const href = (p.href || '').replace(/\/+$/, '').toLowerCase()
      const key = `${s.surface}|${p.model}|${p.section}|${href}`
      index.set(key, p)
    }
  }
  return index
}

function buildCurrentIndex(data: ScanData): Set<string> {
  const index = new Set<string>()
  for (const s of data.surfaces) {
    for (const p of s.placements || []) {
      const href = (p.href || '').replace(/\/+$/, '').toLowerCase()
      index.add(`${s.surface}|${p.model}|${p.section}|${href}`)
    }
  }
  return index
}

function computeChange(p: Placement, surfaceName: string, prevIndex: PrevIndex): Change | null {
  if (prevIndex.size === 0) return null
  const href = (p.href || '').replace(/\/+$/, '').toLowerCase()
  const key = `${surfaceName}|${p.model}|${p.section}|${href}`
  const prev = prevIndex.get(key)
  if (!prev) return { type: 'new' }
  if (prev.slot === p.slot) return { type: 'unchanged' }
  const delta = prev.slot - p.slot
  return { type: 'moved', delta, prev_slot: prev.slot }
}

function getRemovals(surfaceName: string, prevData: ScanData | null, currentIndex: Set<string>): Placement[] {
  if (!prevData) return []
  const removals: Placement[] = []
  for (const s of (prevData.surfaces || [])) {
    if (s.surface !== surfaceName) continue
    for (const p of s.placements || []) {
      const href = (p.href || '').replace(/\/+$/, '').toLowerCase()
      const key = `${s.surface}|${p.model}|${p.section}|${href}`
      if (!currentIndex.has(key)) removals.push(p)
    }
  }
  return removals
}

// --- Data Loading ---
function loadData() {
  const dataDir = join(process.cwd(), 'data')
  const scanPath = join(dataDir, 'scan_v2.json')
  const prevPath = join(dataDir, 'scan_v2.prev.json')

  const data: ScanData = JSON.parse(readFileSync(scanPath, 'utf-8'))
  let prevData: ScanData | null = null
  if (existsSync(prevPath)) {
    try { prevData = JSON.parse(readFileSync(prevPath, 'utf-8')) } catch {}
  }
  return { data, prevData }
}

// --- Components ---
function ChangeIndicator({ change }: { change: Change | null }) {
  if (!change) return null
  if (change.type === 'new') return <span className="change-new inline-block px-1.5 py-0.5 rounded text-[9px] font-bold ml-1 align-middle">NEW</span>
  if (change.type === 'unchanged') return null
  if (change.type === 'moved' && change.delta !== undefined) {
    if (change.delta > 0) return <span className="change-up inline-block px-1.5 py-0.5 rounded text-[9px] font-bold ml-1 align-middle">&#9650; {change.delta}</span>
    return <span className="change-down inline-block px-1.5 py-0.5 rounded text-[9px] font-bold ml-1 align-middle">&#9660; {Math.abs(change.delta)}</span>
  }
  return null
}

function SlotBadge({ slot, total }: { slot: number; total: number }) {
  return (
    <span className={`${slotClass(slot, total)} inline-block px-1.5 py-0.5 rounded font-mono text-[10px] font-semibold`}>
      {slot}/{total}
    </span>
  )
}

function PlacementCard({ p, surfaceName, change }: { p: Placement; surfaceName: string; change: Change | null }) {
  const color = modelColor(p.family)
  const screenshotSrc = p.screenshot ? `/screenshots_v2/${p.screenshot}` : null
  const section = p.section || ''
  const showSection = section && section !== 'Search Results' && !['explore', 'learn'].includes(surfaceName)

  let titleHtml = null
  if (p.title) {
    let fullUrl = p.href
    if (p.href.startsWith('/learn/')) fullUrl = `https://fal.ai${p.href}`
    else if (p.href.startsWith('http')) fullUrl = p.href
    else fullUrl = `https://blog.fal.ai${p.href.startsWith('/') ? p.href : '/' + p.href}`
    titleHtml = <a href={fullUrl} target="_blank" rel="noopener" className="block px-3 pt-2.5 pb-1 text-[13px] font-semibold text-slate-900 hover:text-blue-600 hover:underline leading-tight">{p.title}</a>
  }

  return (
    <div className="border border-slate-200 rounded-lg overflow-hidden hover:shadow-md transition-shadow">
      <div className="relative">
        {screenshotSrc ? (
          <img src={screenshotSrc} alt={p.model} className="w-full block border-b border-slate-100" loading="lazy" />
        ) : (
          <div className="h-[120px] flex items-center justify-center text-slate-400 text-xs bg-slate-50">No screenshot</div>
        )}
      </div>
      {titleHtml}
      <div className="px-2.5 py-2 flex flex-wrap gap-1.5 items-center">
        <span className="px-2 py-0.5 rounded-full font-semibold text-[11px] border" style={{ background: `${color}18`, color, borderColor: color }}>{p.model}</span>
        <span className={`${slotClass(p.slot, p.total_in_section)} inline-block px-1.5 py-0.5 rounded font-mono text-[10px] font-semibold`}>slot {p.slot}/{p.total_in_section}</span>
        <ChangeIndicator change={change} />
        {showSection && <span className="text-[11px] text-slate-500">{section}</span>}
      </div>
    </div>
  )
}

function RemovedCard({ p }: { p: Placement }) {
  const color = modelColor(p.family)
  return (
    <div className="border border-red-300 rounded-lg overflow-hidden opacity-60 bg-red-50">
      <div className="h-[120px] flex items-center justify-center text-red-600 font-bold text-sm bg-red-50">REMOVED</div>
      <div className="px-2.5 py-2 flex flex-wrap gap-1.5 items-center">
        <span className="px-2 py-0.5 rounded-full font-semibold text-[11px] border" style={{ background: `${color}18`, color, borderColor: color }}>{p.model}</span>
        <span className="slot-bad inline-block px-1.5 py-0.5 rounded font-mono text-[10px] font-semibold">was {p.slot}/{p.total_in_section}</span>
        <span className="change-removed inline-block px-1.5 py-0.5 rounded text-[9px] font-bold ml-1">REMOVED</span>
      </div>
    </div>
  )
}

function Heatmap({ surfaces, prevIndex }: { surfaces: Surface[]; prevIndex: PrevIndex }) {
  const surfaceNames = surfaces.filter(s => s.type !== 'model_page' && !s.error).map(s => s.surface)

  // Build grid: family → surface → placements with changes
  const grid: Record<string, Record<string, { p: Placement; change: Change | null }[]>> = {}
  for (const fam of MODEL_FAMILIES) grid[fam] = {}

  for (const s of surfaces) {
    if (s.type === 'model_page') continue
    for (const p of s.placements || []) {
      const fam = getFamilyDisplay(p.model)
      if (!(fam in grid)) continue
      if (!grid[fam][s.surface]) grid[fam][s.surface] = []
      grid[fam][s.surface].push({ p, change: computeChange(p, s.surface, prevIndex) })
    }
  }

  return (
    <div className="bg-white rounded-xl p-5 mb-5 shadow-sm">
      <h2 className="text-[15px] font-semibold text-slate-600 mb-3">Position Overview</h2>
      <div className="overflow-x-auto -mx-1">
        <table className="w-full min-w-[480px] border-collapse text-xs">
          <thead>
            <tr>
              <th></th>
              {surfaceNames.map(n => (
                <th key={n} className="px-2 py-1.5 font-medium text-slate-500 text-center text-[11px]">
                  {SURFACE_META[n]?.short || n}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {MODEL_FAMILIES.map(fam => {
              const colorKey = FAM_COLOR_MAP[fam] || ''
              const color = MODEL_COLORS[colorKey] || '#64748b'
              return (
                <tr key={fam}>
                  <td className="font-semibold text-left pl-2.5 whitespace-nowrap border-t border-slate-100 py-2" style={{ borderLeft: `3px solid ${color}` }}>{fam}</td>
                  {surfaceNames.map(sname => {
                    const entries = grid[fam][sname] || []
                    if (!entries.length) return <td key={sname} className="text-center border-t border-slate-100 py-2 text-slate-300">&mdash;</td>
                    return (
                      <td key={sname} className="text-center border-t border-slate-100 py-2">
                        {entries.map(({ p, change }, i) => (
                          <span key={i} className="inline-flex items-center gap-0.5 mx-0.5">
                            <SlotBadge slot={p.slot} total={p.total_in_section} />
                            <ChangeIndicator change={change} />
                          </span>
                        ))}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-3 text-xs text-slate-500">
        Tracking: Happy Horse 1.0/1.1, Wan 2.6, Wan 2.7 across {surfaceNames.length} surfaces.
        Slot: <span className="slot-good inline-block px-1 py-0.5 rounded font-mono text-[10px] font-semibold">top 1/3</span>{' '}
        <span className="slot-ok inline-block px-1 py-0.5 rounded font-mono text-[10px] font-semibold">middle</span>{' '}
        <span className="slot-bad inline-block px-1 py-0.5 rounded font-mono text-[10px] font-semibold">bottom 1/3</span>
        {prevIndex.size > 0 && (
          <> &middot; Change: <span className="change-up inline-block px-1 py-0.5 rounded text-[9px] font-bold">&#9650; N</span> up{' '}
          <span className="change-down inline-block px-1 py-0.5 rounded text-[9px] font-bold">&#9660; N</span> down{' '}
          <span className="change-new inline-block px-1 py-0.5 rounded text-[9px] font-bold">NEW</span>{' '}
          <span className="change-removed inline-block px-1 py-0.5 rounded text-[9px] font-bold">REMOVED</span></>
        )}
      </div>
    </div>
  )
}

function SurfaceSection({ surface, prevIndex, currentIndex, prevData }: {
  surface: Surface; prevIndex: PrevIndex; currentIndex: Set<string>; prevData: ScanData | null
}) {
  const name = surface.surface
  const meta = SURFACE_META[name] || { title: name, short: name }
  const placements = surface.placements || []
  const removals = getRemovals(name, prevData, currentIndex)

  if (surface.error) {
    return (
      <section className="bg-white rounded-xl p-5 mb-4 shadow-sm">
        <h2 className="text-base font-semibold">{meta.title}</h2>
        <div className="text-red-600 text-sm p-3 bg-red-50 rounded-md mt-2">Error: {surface.error}</div>
      </section>
    )
  }

  if (surface.type === 'model_page') return null

  // Group placements by section for explore/learn
  const grouped = ['explore', 'learn'].includes(name)
  let content: React.ReactNode
  if (placements.length > 0) {
    if (grouped) {
      const sections: Record<string, Placement[]> = {}
      for (const p of placements) {
        const s = p.section || 'Other'
        if (!sections[s]) sections[s] = []
        sections[s].push(p)
      }
      content = Object.entries(sections).map(([sectionName, sectionPlacements]) => (
        <div key={sectionName} className="mb-4 last:mb-0">
          <h3 className="text-[13px] font-semibold text-slate-600 mb-2 pb-1 border-b border-slate-100">{sectionName}</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {sectionPlacements.map((p, i) => (
              <PlacementCard key={i} p={p} surfaceName={name} change={computeChange(p, name, prevIndex)} />
            ))}
          </div>
        </div>
      ))
    } else {
      content = (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {placements.map((p, i) => (
            <PlacementCard key={i} p={p} surfaceName={name} change={computeChange(p, name, prevIndex)} />
          ))}
        </div>
      )
    }
  } else {
    const vpSrc = surface.viewport_screenshot ? `/screenshots_v2/${surface.viewport_screenshot}` : null
    content = (
      <div className="text-slate-500 text-sm text-center py-4">
        No tracked models on this page.
        {vpSrc && <img src={vpSrc} alt="Viewport" className="block max-w-full max-h-[300px] mx-auto mt-3 rounded-lg border border-slate-200" loading="lazy" />}
      </div>
    )
  }

  return (
    <section className="bg-white rounded-xl p-5 mb-4 shadow-sm">
      <div className="flex items-baseline gap-3 flex-wrap mb-3">
        <h2 className="text-base font-semibold flex items-center gap-2">
          {meta.title}
          {placements.length > 0 ? (
            <span className="inline-flex items-center justify-center min-w-[22px] h-[22px] rounded-full bg-slate-900 text-white text-[11px] font-bold">{placements.length}</span>
          ) : (
            <span className="inline-flex items-center justify-center min-w-[22px] h-[22px] rounded-full bg-slate-200 text-slate-500 text-[11px] font-bold">0</span>
          )}
        </h2>
        <a href={surface.url} target="_blank" rel="noopener" className="text-[11px] text-slate-400 hover:underline">{surface.url}</a>
      </div>
      {content}
      {removals.length > 0 && (
        <div className="mt-4 pt-3 border-t-2 border-dashed border-red-300">
          <h3 className="text-[13px] font-semibold text-red-600 mb-2">Removed since last scan</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {removals.map((p, i) => <RemovedCard key={i} p={p} />)}
          </div>
        </div>
      )}
    </section>
  )
}

// --- Main Page ---
export default function Dashboard() {
  const { data, prevData } = loadData()
  const prevIndex = buildPrevIndex(prevData)
  const currentIndex = buildCurrentIndex(data)

  const surfaceMap = new Map(data.surfaces.map(s => [s.surface, s]))
  const ordered = SURFACE_ORDER.map(n => surfaceMap.get(n)).filter(Boolean) as Surface[]

  const nonModelSurfaces = ordered.filter(s => s.type !== 'model_page')
  const totalPlacements = nonModelSurfaces.reduce((sum, s) => sum + (s.placements?.length || 0), 0)
  const totalRemovals = nonModelSurfaces.reduce((sum, s) => sum + getRemovals(s.surface, prevData, currentIndex).length, 0)

  const scanTime = data.timestamp.slice(0, 16).replace('T', ' ')
  const prevTimestamp = prevData?.timestamp?.slice(0, 16).replace('T', ' ') || ''

  return (
    <>
      <header className="bg-slate-900 text-white px-7 py-5 flex items-baseline gap-4 flex-wrap">
        <h1 className="text-[22px] font-bold m-0">fal.ai &mdash; Model Placements</h1>
        <span className="text-xs opacity-60">
          Scanned {scanTime} UTC &middot; {totalPlacements} placements{totalRemovals > 0 && ` \u00b7 ${totalRemovals} removed`}
        </span>
        {prevData && (
          <span className="text-[11px] opacity-50 basis-full mt-0.5">Compared to {prevTimestamp} UTC</span>
        )}
      </header>
      <main className="max-w-[1200px] mx-auto p-5">
        <Heatmap surfaces={ordered} prevIndex={prevIndex} />
        {ordered.map(s => (
          <SurfaceSection key={s.surface} surface={s} prevIndex={prevIndex} currentIndex={currentIndex} prevData={prevData} />
        ))}
      </main>
    </>
  )
}

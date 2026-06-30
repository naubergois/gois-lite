/** Helpers for book creation wizard — errors, structure extraction, polling. */

export function formatHttpError(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message.trim()) {
    return err.message
  }
  const ax = err as { response?: { data?: unknown } }
  const data = ax.response?.data
  if (typeof data === 'string' && data.trim()) {
    return data
  }
  if (data && typeof data === 'object' && data !== null && 'detail' in data) {
    const detail = (data as { detail: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) => {
          if (typeof item === 'string') return item
          if (item && typeof item === 'object' && 'msg' in item) {
            return String((item as { msg?: string }).msg || '')
          }
          return ''
        })
        .filter(Boolean)
      if (parts.length) return parts.join('; ')
    }
  }
  return fallback
}

export function extractStructureFromStatusPayload(data: unknown): unknown[] | null {
  if (!data || typeof data !== 'object') return null
  const row = data as Record<string, unknown>
  const fs = row.final_state
  if (!fs || typeof fs !== 'object') return null
  const plan = (fs as Record<string, unknown>).book_plan
  if (!plan || typeof plan !== 'object') return null
  const p = plan as Record<string, unknown>
  for (const key of ['structure', 'chapters', 'table_of_contents']) {
    const raw = p[key]
    if (Array.isArray(raw) && raw.length > 0) return raw
  }
  return null
}

export function extractStructureFromBookPayload(data: unknown): unknown[] | null {
  if (!data || typeof data !== 'object') return null
  const row = data as Record<string, unknown>
  for (const key of ['structure', 'chapters', 'table_of_contents', 'capitulos']) {
    const raw = row[key]
    if (Array.isArray(raw) && raw.length > 0) return raw
  }
  const plan = row.book_plan
  if (plan && typeof plan === 'object') {
    return extractStructureFromStatusPayload({ final_state: { book_plan: plan } })
  }
  return null
}

export type WizardSectionShape = {
  title: string
  purpose: string
  content: string
  images: []
  code_blocks: []
}

export type WizardChapterShape = {
  id: string
  title: string
  description: string
  content: string
  creation_guide: string
  sections: WizardSectionShape[]
}

export function mapRawStructureToWizardChapters(rawStructure: unknown[]): WizardChapterShape[] {
  return rawStructure.map((ch, i) => {
    const row = ch as Record<string, unknown>
    const title =
      typeof ch === 'string'
        ? ch
        : String(row.title || row.chapter_title || `Capítulo ${i + 1}`)
    const description =
      typeof ch === 'string' ? '' : String(row.purpose || row.description || '')
    const sectionsRaw = typeof ch === 'string' ? [] : row.sections
    const sections: WizardSectionShape[] =
      Array.isArray(sectionsRaw) && sectionsRaw.length > 0
        ? sectionsRaw.map((s, si) => {
            const sec = s as Record<string, unknown>
            return {
              title: typeof s === 'string' ? s : String(sec.title || `Seção ${si + 1}`),
              purpose:
                typeof s === 'string'
                  ? ''
                  : String(sec.purpose || sec.objective || sec.content_directive || ''),
              content: typeof s === 'string' ? '' : String(sec.content || ''),
              images: [],
              code_blocks: [],
            }
          })
        : [{ title: 'Introdução', purpose: '', content: '', images: [], code_blocks: [] }]
    return {
      id: `chapter-${Date.now()}-${i}`,
      title,
      description,
      content: typeof ch === 'string' ? '' : String(row.content || ''),
      creation_guide: typeof ch === 'string' ? '' : String(row.creation_guide || '').trim(),
      sections,
    }
  })
}

/** Poll until structure appears, job fails, or max wait elapses. */
export async function pollBookStructureJob(
  jobId: string,
  fetchStatus: (id: string) => Promise<unknown>,
  fetchBook: (id: string) => Promise<unknown>,
  options?: { maxWaitMs?: number; intervalMs?: number },
): Promise<{
  chapters: WizardChapterShape[] | null
  lastStatus: Record<string, unknown> | null
  timedOut: boolean
  failed: boolean
}> {
  const maxWaitMs = options?.maxWaitMs ?? 10 * 60 * 1000
  const intervalMs = options?.intervalMs ?? 2500
  const started = Date.now()
  let lastStatus: Record<string, unknown> | null = null

  while (Date.now() - started < maxWaitMs) {
    try {
      const statusData = await fetchStatus(jobId)
      if (statusData && typeof statusData === 'object') {
        lastStatus = statusData as Record<string, unknown>
        const fromStatus = extractStructureFromStatusPayload(statusData)
        if (fromStatus?.length) {
          return {
            chapters: mapRawStructureToWizardChapters(fromStatus),
            lastStatus,
            timedOut: false,
            failed: false,
          }
        }
        const st = String(lastStatus.status || '')
        if (st === 'failed' || st === 'stopped') {
          return { chapters: null, lastStatus, timedOut: false, failed: true }
        }
      }
    } catch {
      /* status may lag; try books */
    }

    try {
      const bookData = await fetchBook(jobId)
      const fromBook = extractStructureFromBookPayload(bookData)
      if (fromBook?.length) {
        return {
          chapters: mapRawStructureToWizardChapters(fromBook),
          lastStatus,
          timedOut: false,
          failed: false,
        }
      }
    } catch {
      /* ignore */
    }

    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }

  return { chapters: null, lastStatus, timedOut: true, failed: false }
}

type StructureJobResolverDeps = {
  existingId?: string
  payload: Record<string, unknown>
  getStatus: (id: string) => Promise<unknown>
  queuePlanning: (id: string, payload: Record<string, unknown>) => Promise<{ job_id?: string }>
  createGenerate: (payload: Record<string, unknown>) => Promise<{ job_id?: string }>
  getBook?: (id: string) => Promise<unknown>
}

/** Reuse existing book job when possible; never create a duplicate silently. */
export async function resolveStructureJobId(deps: StructureJobResolverDeps): Promise<string> {
  const existingId = (deps.existingId || '').trim()
  if (!existingId) {
    const created = await deps.createGenerate(deps.payload)
    const jobId = (created.job_id || '').trim()
    if (!jobId) {
      throw new Error('Resposta inválida do servidor (job_id ausente).')
    }
    return jobId
  }

  let status: string | undefined
  let statusPayload: unknown = null
  try {
    statusPayload = await deps.getStatus(existingId)
    if (statusPayload && typeof statusPayload === 'object') {
      status = String((statusPayload as Record<string, unknown>).status || '')
    }
  } catch {
    status = undefined
  }

  if (!status && deps.getBook) {
    try {
      await deps.getBook(existingId)
      return existingId
    } catch {
      /* livro inexistente — criar novo abaixo */
    }
  }

  const existingStructure =
    extractStructureFromStatusPayload(statusPayload) ||
    (deps.getBook ? extractStructureFromBookPayload(await deps.getBook(existingId).catch(() => null)) : null)
  if (existingStructure?.length && (status === 'completed' || status === 'planned')) {
    return existingId
  }

  if (
    status === 'planning_draft' ||
    status === 'failed' ||
    status === 'stopped' ||
    status === 'completed' ||
    status === 'planned'
  ) {
    const queued = await deps.queuePlanning(existingId, deps.payload)
    return (queued.job_id || existingId).trim() || existingId
  }

  if (status === 'pending' || status === 'running') {
    return existingId
  }

  const created = await deps.createGenerate(deps.payload)
  const jobId = (created.job_id || '').trim()
  if (!jobId) {
    throw new Error('Resposta inválida do servidor (job_id ausente).')
  }
  return jobId
}

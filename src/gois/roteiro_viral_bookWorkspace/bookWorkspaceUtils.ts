import { getStoredGeminiApiKey } from '@/lib/apiKeys'

export interface SectionImage {
  path: string
  caption?: string
  source?: string
  uploaded_at?: string | number
  model?: string
}

export interface CodeBlock {
  language?: string
  title?: string
  content?: string
  created_at?: string | number
}

/** Item de prompt de slide (gerado por IA para uma seção). */
export interface SectionSlidePromptItem {
  index?: number
  title?: string
  text?: string
  code_text?: string
  prompt?: string
  background_prompt?: string
}

/**
 * Subseção: objetivo, conteúdo, prompts de slides e imagens (mesmo formato da seção; sem título).
 * O objetivo da seção é segmentado nos objetivos das subseções (cada subseção cobre uma parte do objetivo da seção).
 */
export interface BookSubsection {
  /** Título opcional (API / merge de planos; exibição quando existir). */
  title?: string
  objective: string
  content?: string
  /** Quantidade mínima de caracteres ao gerar texto da subseção. */
  min_text_length?: number
  /** Se a subseção deve incluir código fonte (geração com IA). */
  has_source_code?: boolean
  /** Estilos de autor para orientar a escrita (igual à seção). */
  author_styles?: string[]
  /** Prompts de slides da subseção (mesmo formato que na seção). */
  slide_prompts?: SectionSlidePromptItem[]
  /** Estilos visuais para os slides desta subseção (multi-estilo). Se vazio, usa book_slide_styles do livro. */
  slide_styles?: string[]
  /** Imagens da subseção (slides gerados e outras), mesmo formato que Section.images. */
  images?: SectionImage[]
}

/**
 * Seção de um capítulo. O objetivo do capítulo é segmentado nos objetivos das seções.
 * O objetivo da seção é segmentado nos objetivos das subseções.
 */
export interface BookSection {
  title?: string
  purpose?: string
  objective?: string
  content_directive?: string
  content?: string
  /** Subseções da seção (objetivo + conteúdo editável). */
  subsections?: BookSubsection[]
  /** Quantidade exata de subseções a gerar por seção (usado no planejamento IA). */
  num_subsections_per_section?: number
  author_styles?: string[]
  image_path?: string
  images?: SectionImage[]
  code_blocks?: CodeBlock[]
  questions?: string
  num_questions?: number
  question_board?: string
  question_type?: string
  question_difficulty?: string
  question_include_answers?: boolean
  question_include_explanation?: boolean
  reigenText?: string
  editedReigenText?: string
  isGeneratingReigenText?: boolean
  min_text_length?: number
  has_source_code?: boolean
  /** Prompts de slides da seção (persistidos em banco). */
  slide_prompts?: SectionSlidePromptItem[]
}

/**
 * Capítulo do livro. O objetivo do capítulo é segmentado nos objetivos das seções (cada seção cobre uma parte do objetivo do capítulo).
 */
export interface BookChapter {
  chapter?: number
  title?: string
  purpose?: string
  content?: string
  introduction?: string
  sections?: BookSection[]
  cover_path?: string
  epub_path?: string
}

/** Um prompt de livro persistido (nome, texto e estilos aplicados). */
export interface BookPromptItem {
  id: string
  name: string
  prompt_text: string
  style_ids: string[]
}

export interface BookPlan {
  title?: string
  subtitle?: string
  author?: string
  /** Descrição do livro (editável na tela de metadados; incluída na exportação). */
  description?: string
  /** Palavras-chave separadas por vírgula (editável na tela de metadados; incluído na exportação). */
  keywords?: string
  objective?: string
  draft?: string
  book_style?: string[]
  book_style_prompt?: string
  author_inspiration?: string
  author_styles?: string[]
  cover_designer_styles?: string[]
  target_audience?: string
  language?: string
  prologue?: string
  acknowledgments?: string
  cover_path?: string
  back_cover_path?: string
  full_epub_path?: string
  full_colab_notebook_path?: string
  /** Estilos de imagem para a geração do EPUB (nomes da tabela image_styles); as imagens geradas seguem esses estilos. */
  epub_image_styles?: string[]
  structure?: BookChapter[]
  chapters?: BookChapter[]
  table_of_contents?: BookChapter[]
  /** Número de capítulos desejado na geração com IA (persistido na edição). */
  num_chapters?: number
  /** Número de seções por capítulo na geração com IA (persistido na edição). */
  num_sections_per_chapter?: number
  /** Quantidade mínima de imagens por capítulo (padrão 1). Usado em redução de imagens e relatórios. */
  min_images_per_chapter?: number
  default_min_text_length?: number
  default_has_source_code?: boolean
  /** Número padrão de subseções por seção ao gerar com IA. */
  default_num_subsections_per_section?: number
  /** Estilo de escrita das seções: narrativa e inteligente ou tópificada. */
  default_section_writing_style?: 'narrative' | 'topical'
  /** Prompts do livro persistidos em banco (com estilos selecionados). */
  book_prompts?: BookPromptItem[]
  /** Estilos visuais padrão dos slides do livro (multi-estilo). */
  book_slide_styles?: string[]
  /** Base de fatos do livro (extraídos por agente a partir de texto; editáveis/deletáveis). */
  facts_base?: BookFact[]
  /** Base de bibliografia do livro (extraída por agente; editável/deletável). */
  bibliography_base?: BookBibliographyEntry[]
  /** Fontes Perplexity/web: lista global numerada; no texto só citações [n]; lista completa no final do EPUB. */
  source_library?: BookSourceLibraryEntry[]
  /** Prompt aplicado pela IA em todas as seções e subseções ao gerar ou reescrever texto. */
  global_section_prompt?: string
}

export interface BookSourceLibraryEntry {
  n?: number
  line?: string
  text?: string
  url?: string | null
}

export interface BookFact {
  id: string
  text: string
  source?: string
  created_at?: number
  /** Índices dos capítulos onde o fato foi citado. */
  used_in_chapters?: number[]
}

export interface BookBibliographyEntry {
  id: string
  text: string
  author?: string
  title?: string
  year?: string
  publisher?: string
  entry_type?: string
  citation?: string
  url?: string
  created_at?: number
  /** Índices dos capítulos onde a referência foi citada (formato AUTOR, ANO). */
  used_in_chapters?: number[]
  /** Resumo da referência (busca na internet); usado pelo agente ao escrever seções. */
  summary?: string
}

/** Regex para imagem em markdown: ![alt](path). Path pode ter espaços/quebras. */
const MARKDOWN_IMAGE_RE = /!\[([^\]]*)\]\(\s*([^)]+)\s*\)/g

/**
 * Extrai referências de imagem do conteúdo em markdown (![alt](path)).
 * Usado para exibir na edição da seção as mesmas imagens que aparecem no EPUB.
 */
export function extractImagesFromMarkdownContent(content: string | undefined): SectionImage[] {
  if (!content?.trim()) return []
  const out: SectionImage[] = []
  let m: RegExpExecArray | null
  MARKDOWN_IMAGE_RE.lastIndex = 0
  while ((m = MARKDOWN_IMAGE_RE.exec(content)) !== null) {
    const caption = (m[1] ?? '').trim()
    const path = (m[2] ?? '').trim().replace(/\s+/g, ' ').trim()
    if (path) out.push({ path, caption: caption || undefined })
  }
  return out
}

/** Normaliza path para comparação (trim, espaços unificados). */
function normalizePath(p: string): string {
  return (p ?? '').trim().replace(/\s+/g, ' ')
}

/** Retorna o basename do path (último segmento) para comparação quando paths absolutos diferem. */
function pathBasename(p: string): string {
  const n = normalizePath(p)
  const parts = n.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || n
}

/**
 * Substitui a legenda da primeira ocorrência de ![alt](path) pelo path dado.
 * Path é comparado após normalização. Retorna o novo conteúdo ou o original se não encontrar.
 */
export function replaceMarkdownImageCaption(content: string | undefined, imagePath: string, newCaption: string): string {
  if (!content?.trim()) return content ?? ''
  const wantPath = normalizePath(imagePath)
  const re = /!\[[^\]]*\]\s*\(\s*([^)]+)\s*\)/g
  let found = false
  const result = content.replace(re, (match) => {
    if (found) return match
    const pathMatch = /\]\s*\(\s*([^)]+)\s*\)/.exec(match)
    const path = pathMatch ? normalizePath(pathMatch[1]) : ''
    if (path === wantPath) {
      found = true
      const pathPart = pathMatch ? pathMatch[1] : ''
      return `![${newCaption}](${pathPart})`
    }
    return match
  })
  return result
}

/**
 * Substitui no markdown o path da imagem pelo novo path, preservando a legenda.
 * Compara por path normalizado e também por basename para cobrir diferenças relativas/absolutas.
 */
export function replaceMarkdownImagePath(
  content: string | undefined,
  imagePath: string,
  newImagePath: string,
): string {
  if (!content?.trim()) return content ?? ''
  const wantPath = normalizePath(imagePath)
  const wantBasename = pathBasename(imagePath)
  const replacementPath = String(newImagePath || '').trim()
  if (!replacementPath) return content

  let replaced = false
  const result = content.replace(MARKDOWN_IMAGE_RE, (match, caption, rawPath) => {
    if (replaced) return match
    const currentPath = normalizePath(String(rawPath || ''))
    const samePath = currentPath === wantPath
    const sameBasename = wantBasename && pathBasename(currentPath) === wantBasename
    if (!samePath && !sameBasename) return match
    replaced = true
    return `![${String(caption || '').trim()}](${replacementPath})`
  })
  return result
}

/**
 * Remove do conteúdo todas as ocorrências de ![alt](path) cujo path corresponde ao imagePath.
 * Compara path normalizado e também por basename para cobrir paths relativos vs absolutos.
 */
export function removeMarkdownImageByPath(content: string | undefined, imagePath: string): string {
  if (!content?.trim()) return content ?? ''
  const wantPath = normalizePath(imagePath)
  const wantBasename = pathBasename(imagePath)
  const re = /!\[[^\]]*\]\s*\(\s*([^)]+)\s*\)/g
  let out = content.replace(re, (match) => {
    const pathMatch = /\]\s*\(\s*([^)]+)\s*\)/.exec(match)
    const path = pathMatch ? normalizePath(pathMatch[1]) : ''
    const samePath = path === wantPath
    const sameBasename = wantBasename && pathBasename(path) === wantBasename
    return samePath || sameBasename ? '' : match
  })
  out = out.replace(/\n{3,}/g, '\n\n').trim()
  return out
}

export const extractCodeBlocks = (text: string): CodeBlock[] => {
  if (!text) return []
  const regex = /```([\w-]*)\n([\s\S]*?)```/g
  const blocks: CodeBlock[] = []
  let match: RegExpExecArray | null
  while ((match = regex.exec(text)) !== null) {
    blocks.push({
      language: match[1] || 'text',
      content: match[2]?.trim() || '',
      title: `Código (${match[1] || 'texto'})`,
    })
  }
  return blocks
}

/** Regex para bloco ```chart ... ``` (mesmo critério do backend). */
const CHART_BLOCK_RE = /```chart\s*\n[\s\S]*?```/i
/** Verifica se um bloco ```json contém especificação de gráfico (library + chart_type). */
function jsonBlockHasChartSpec(blockContent: string): boolean {
  const s = blockContent.trim()
  return s.includes('"library"') && s.includes('"chart_type"')
}

/**
 * Retorna true se o texto contém pelo menos um bloco de gráfico:
 * - ```chart ... ``` ou
 * - ```json ... ``` com "library" e "chart_type".
 */
export function contentHasChart(content: string | undefined): boolean {
  if (!content?.trim()) return false
  if (CHART_BLOCK_RE.test(content)) return true
  const jsonBlockRe = /```(?:json)?\s*\n([\s\S]*?)```/gi
  let m: RegExpExecArray | null
  while ((m = jsonBlockRe.exec(content)) !== null) {
    if (jsonBlockHasChartSpec(m[1] ?? '')) return true
  }
  return false
}

/** Seção contém gráfico se o content ou algum code_block tiver bloco de gráfico. */
export function sectionHasChart(section: BookSection): boolean {
  if (contentHasChart(section.content)) return true
  const blocks = section.code_blocks
  if (blocks?.length) {
    for (const b of blocks) {
      if (b.language === 'chart' || contentHasChart(b.content)) return true
    }
  }
  return false
}

/** Subseção contém gráfico se o content tiver bloco de gráfico. */
export function subsectionHasChart(sub: BookSubsection): boolean {
  return contentHasChart(sub.content)
}

/** Capítulo contém gráfico se alguma seção ou subseção tiver gráfico. */
export function chapterHasChart(chapter: BookChapter): boolean {
  const secs = chapter.sections
  if (!secs?.length) return false
  for (const sec of secs) {
    if (sectionHasChart(sec)) return true
    for (const sub of sec.subsections ?? []) {
      if (subsectionHasChart(sub)) return true
    }
  }
  return false
}

export const getChapterKey = (plan?: BookPlan | null): keyof BookPlan => {
  if (!plan) return 'structure'
  if (plan.structure) return 'structure'
  if (plan.chapters) return 'chapters'
  if (plan.table_of_contents) return 'table_of_contents'
  return 'structure'
}

/** Merge translate job progress.results into plan (meta, ch_*, sec_*, sub_*). Used to update UI as translation completes. */
export function mergeTranslateResultsIntoPlan(
  plan: BookPlan | null,
  results: Record<string, unknown> | undefined
): BookPlan | null {
  if (!plan || !results || typeof results !== 'object') return plan
  const key = getChapterKey(plan)
  const chapters = (plan[key] as BookChapter[] | undefined) || []
  if (chapters.length === 0) {
    const meta = results.meta as Record<string, unknown> | undefined
    if (meta && typeof meta === 'object') {
      const next: BookPlan = { ...plan }
      for (const k of ['title', 'subtitle', 'draft', 'prologue', 'acknowledgments', 'objective', 'description'] as const) {
        if (Object.prototype.hasOwnProperty.call(meta, k) && meta[k] != null) (next as Record<string, unknown>)[k] = meta[k]
      }
      return next
    }
    return plan
  }
  let nextPlan: BookPlan = { ...plan }
  const meta = results.meta as Record<string, unknown> | undefined
  if (meta && typeof meta === 'object') {
    for (const k of ['title', 'subtitle', 'draft', 'prologue', 'acknowledgments', 'objective', 'description'] as const) {
      if (Object.prototype.hasOwnProperty.call(meta, k) && meta[k] != null) (nextPlan as Record<string, unknown>)[k] = meta[k]
    }
  }
  const nextChapters = chapters.map((ch, ci) => {
    const chRes = results[`ch_${ci}`] as Record<string, unknown> | undefined
    let nextCh = ch
    if (chRes && typeof chRes === 'object') {
      nextCh = { ...ch }
      for (const k of ['title', 'purpose', 'introduction', 'content'] as const) {
        if (Object.prototype.hasOwnProperty.call(chRes, k) && chRes[k] != null) (nextCh as Record<string, unknown>)[k] = chRes[k]
      }
    }
    const secs = nextCh.sections || []
    nextCh = { ...nextCh, sections: secs.map((sec, si) => {
      const secRes = results[`sec_${ci}_${si}`] as Record<string, unknown> | undefined
      let nextSec = sec
      if (secRes && typeof secRes === 'object') {
        nextSec = { ...sec }
        for (const k of ['title', 'purpose', 'objective', 'content_directive', 'content'] as const) {
          if (Object.prototype.hasOwnProperty.call(secRes, k) && secRes[k] != null) (nextSec as Record<string, unknown>)[k] = secRes[k]
        }
      }
      const subs = nextSec.subsections || []
      nextSec = { ...nextSec, subsections: subs.map((sub, subi) => {
        const subRes = results[`sub_${ci}_${si}_${subi}`] as Record<string, unknown> | undefined
        if (!subRes || typeof subRes !== 'object') return sub
        let nextSub = { ...sub }
        for (const k of ['title', 'objective', 'content'] as const) {
          if (Object.prototype.hasOwnProperty.call(subRes, k) && subRes[k] != null) (nextSub as Record<string, unknown>)[k] = subRes[k]
        }
        return nextSub
      }) }
      return nextSec
    }) }
    return nextCh
  })
  return { ...nextPlan, [key]: nextChapters }
}

export const normalizePlan = (raw: any): { plan: BookPlan | null; planKey: string } => {
  if (!raw) return { plan: null, planKey: 'book_plan' }
  let planKey = 'book_plan'
  let plan: any = raw.book_plan ?? raw.final_book_plan ?? raw.final_script ?? raw.course_plan

  if (plan && typeof plan === 'string') {
    try {
      plan = JSON.parse(plan)
    } catch {
      plan = null
    }
  }

  if (Array.isArray(plan)) {
    plan = { structure: plan }
  }

  if (!plan || typeof plan !== 'object') {
    if (raw && typeof raw === 'object' && (raw.structure || raw.chapters || raw.table_of_contents)) {
      return { plan: raw as BookPlan, planKey }
    }
    return { plan: null, planKey }
  }

  if (raw.book_plan) planKey = 'book_plan'
  else if (raw.final_book_plan) planKey = 'final_book_plan'
  else if (raw.final_script) planKey = 'final_script'
  else if (raw.course_plan) planKey = 'course_plan'

  return { plan, planKey }
}

export const parseAuthorStyles = (value?: string): string[] => {
  if (!value) return []
  return value
    .split(',')
    .map((style) => style.trim())
    .filter(Boolean)
}

export const getApiKey = (job: any) => {
  return (
    getStoredGeminiApiKey() ||
    job?.request_payload?.api_key ||
    (job as any)?.final_state?.api_key ||
    ''
  )
}

/**
 * Normalizes section content that may have been stored as a tuple/array [text, cost]
 * due to a backend bug (book_section_writer_node returns (content, cost) tuple).
 */
const normalizeSectionContent = (section: BookSection): BookSection => {
  if (!section) return section
  let content = section.content as any
  if (Array.isArray(content)) {
    content = typeof content[0] === 'string' ? content[0] : String(content[0] ?? '')
  } else if (content != null && typeof content !== 'string') {
    content = String(content)
  }
  if (!content || (typeof content === 'string' && !content.trim())) {
    const anySection = section as any
    content =
      anySection.markdown_content ||
      anySection.text ||
      anySection.body ||
      anySection.conteudo ||
      anySection.texto ||
      anySection.section_content ||
      anySection.content ||
      ''
  }
  return content !== section.content ? { ...section, content } : section
}

export const getChaptersFromPlan = (targetPlan?: BookPlan | null) => {
  if (!targetPlan) return []
  const key = getChapterKey(targetPlan)
  const chapters = (targetPlan[key] as BookChapter[] | undefined) || []
  return chapters.map((chapter, index) => ({
    ...chapter,
    chapter: chapter.chapter ?? index + 1,
    sections: (chapter.sections || []).map(normalizeSectionContent),
  }))
}

/** Estatísticas de subseções derivadas do plano (resiliente: sempre a partir da estrutura). */
export interface SubsectionStats {
  /** Total de seções no livro. */
  totalSections: number
  /** Seções que têm pelo menos uma subseção. */
  sectionsWithSubsections: number
  /** Total de subseções em todas as seções. */
  totalSubsections: number
  /** Subseções que têm texto (content não vazio). */
  subsectionsWithText: number
}

/**
 * Calcula estatísticas de subseções a partir da lista de capítulos.
 * Usado para exibir percentual no Histórico e na tela do livro de forma resiliente.
 */
export function getSubsectionStats(
  chapters: Array<{ sections?: Array<{ subsections?: Array<{ content?: string }> }> }> | null | undefined
): SubsectionStats {
  const chs = Array.isArray(chapters) ? chapters : []
  let totalSections = 0
  let sectionsWithSubsections = 0
  let totalSubsections = 0
  let subsectionsWithText = 0
  for (const ch of chs) {
    const sections = ch?.sections ?? []
    for (const sec of sections) {
      totalSections += 1
      const subs = sec?.subsections ?? []
      if (subs.length > 0) {
        sectionsWithSubsections += 1
        totalSubsections += subs.length
        for (const sub of subs) {
          if ((sub?.content ?? '').trim().length > 0) subsectionsWithText += 1
        }
      }
    }
  }
  return {
    totalSections,
    sectionsWithSubsections,
    totalSubsections,
    subsectionsWithText,
  }
}

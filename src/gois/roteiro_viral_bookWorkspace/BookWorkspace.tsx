import { Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  BookOpen,
  BookMarked,
  Wand2,
  Image as ImageIcon,
  Plus,
  Trash2,
  Save,
  Loader2,
  Download,
  LayoutList,
  Pencil,
  RefreshCw,
  FileDown,
  Sparkles,
  Settings,
  FileText,
  X,
  Code,
  Zap,
  Layers,
  Target,
  Languages,
  ChevronUp,
  ChevronDown,
  ChevronRight,
  Link,
  Search,
  Palette,
  Upload,
  Table,
  BarChart2,
} from 'lucide-react'
import { ImageDropZone } from '@/components/ImageDropZone'
import { modelConfig } from '@/services/ModelConfigService'
import { useJob } from '@/hooks/useJobs'
import { useExecutionMode } from '@/hooks/useExecutionMode'
import { API_BASE_URL, endpoints, api } from '@/lib/api'
import { getStoredGeminiApiKey } from '@/lib/apiKeys'
import { useAppOptions } from '@/hooks/useAppOptions'
import { buildFileUrl } from '@/lib/files'
import { lazyNamed } from '@/lib/lazyPage'
import { getComicCharacterPromptSummary } from '@/lib/comicCharacterVisual'
import { cn } from '@/lib/utils'
import { splitNumberedReferenceLine } from '@/lib/bookSources'
import { MathFormulaField } from '@/components/MathFormulaField'
import { AuthorStyleSelector } from '@/components/AuthorStyleSelector'
import StyleGrid from '@/components/StyleGrid'
import { LogViewer, LogEntry } from '@/components/LogViewer'
import { SectionImageGeneratorPanel } from '@/components/SectionImageGeneratorPanel'
import { ScriptLabTextTools } from '@/components/ScriptLabTextTools'
import {
  BookChapter,
  BookPlan,
  BookSection,
  extractCodeBlocks,
  extractImagesFromMarkdownContent,
  removeMarkdownImageByPath,
  replaceMarkdownImageCaption,
  replaceMarkdownImagePath,
  getApiKey,
  getChapterKey,
  getChaptersFromPlan,
  getSubsectionStats,
  mergeTranslateResultsIntoPlan,
  normalizePlan,
  parseAuthorStyles,
  type BookSubsection,
} from './bookWorkspace/bookWorkspaceUtils'
import { WorkspaceTabs } from './bookWorkspace/WorkspaceTabs'
import { BookStructureTree } from './bookWorkspace/BookStructureTree'
import { CourseStructurePanel } from './courseWorkspace/CourseStructurePanel'
import { isCoursePlan, planToCoursePlan } from './courseWorkspace/courseWorkspaceUtils'
import { SectionImagePreview } from '@/components/SectionImagePreview'
import { SlideFontPresetSelect } from '@/components/SlideFontPresetSelect'
import { MarkdownField } from '@/components/MarkdownField'
import { UnifiedChat, type UnifiedChatAction } from '@/components/UnifiedChat'
import { createDeepResearchTool } from '@/lib/researchTool'
import { scheduleHeavyBookWork } from '@/lib/deferUi'
import {
  consumeAdvancedImageStudioPendingSync,
  createAdvancedImageStudioDraft,
} from '@/lib/advancedImageStudioSession'
import { useComicCharacters, useComicSagas } from '@/lib/hooks/useData'
import { useTranslateProgress } from '@/contexts/TranslateProgressContext'
import { DIDACTIC_CODE_SLIDE_MODELS, getDidacticSlideModelById } from '@/lib/didacticSlideModels'

const CodeExplainer = lazyNamed(
  () => import('@/components/CodeExplainer'),
  '@/components/CodeExplainer',
  'CodeExplainer',
)
const MermaidEditor = lazyNamed(
  () => import('@/components/MermaidDiagram'),
  '@/components/MermaidDiagram',
  'MermaidEditor',
)
const BookBaseTab = lazyNamed(
  () => import('./bookWorkspace/BookBaseTab'),
  './bookWorkspace/BookBaseTab',
  'BookBaseTab',
)
const BookMetadataTab = lazyNamed(
  () => import('./bookWorkspace/BookMetadataTab'),
  './bookWorkspace/BookMetadataTab',
  'BookMetadataTab',
)
const SectionActionsPanel = lazyNamed(
  () => import('./bookWorkspace/SectionActionsPanel'),
  './bookWorkspace/SectionActionsPanel',
  'SectionActionsPanel',
)
const SectionQuestionsPanel = lazyNamed(
  () => import('./bookWorkspace/SectionQuestionsPanel'),
  './bookWorkspace/SectionQuestionsPanel',
  'SectionQuestionsPanel',
)
const BookCoverDesigner = lazyNamed(
  () => import('@/components/BookCoverDesigner'),
  '@/components/BookCoverDesigner',
  'BookCoverDesigner',
)
const DiagramGenerator = lazyNamed(
  () => import('@/components/DiagramGenerator'),
  '@/components/DiagramGenerator',
  'DiagramGenerator',
)
const LessonSlidePreview = lazyNamed(
  () => import('@/components/LessonSlidePreview'),
  '@/components/LessonSlidePreview',
  'LessonSlidePreview',
)
const EpubPreview = lazyNamed(
  () => import('@/components/EpubPreview'),
  '@/components/EpubPreview',
  'EpubPreview',
)

type BookImageEditorTarget = {
  scope: 'section' | 'subsection' | 'chapter'
  kind: 'image' | 'slide'
  chapterIdx: number
  /** Obrigatório para seção/subseção; omitido para divisor de capítulo (`scope === 'chapter'`). */
  sectionIdx?: number
  subsectionIdx?: number
  imagePath: string
  title: string
  caption?: string
}

function normalizeBookImagePath(path: string): string {
  try {
    return decodeURIComponent(String(path || '').trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\?.*$/, ''))
  } catch {
    return String(path || '').trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\?.*$/, '')
  }
}

function isBookSlideImage(img: unknown): boolean {
  return (
    typeof img === 'object'
    && img !== null
    && (
      (img as { source?: string }).source === 'slide'
      || Boolean((img as { caption?: string }).caption?.startsWith('Slide '))
    )
  )
}

function reorderBookSlideImages<T>(images: T[], fromIndex: number, toIndex: number) {
  const slidePositions = images
    .map((img, index) => (isBookSlideImage(img) ? index : -1))
    .filter((index) => index >= 0)

  if (
    slidePositions.length <= 1
    || fromIndex < 0
    || fromIndex >= slidePositions.length
    || toIndex < 0
    || toIndex >= slidePositions.length
    || fromIndex === toIndex
  ) {
    return { images, moved: false, targetIndex: fromIndex, slidePaths: [] as string[] }
  }

  const slideEntries = slidePositions.map((position) => images[position])
  const [movedEntry] = slideEntries.splice(fromIndex, 1)
  slideEntries.splice(toIndex, 0, movedEntry)

  const nextImages = [...images]
  slidePositions.forEach((position, slideIdx) => {
    nextImages[position] = slideEntries[slideIdx]
  })

  const slidePaths = slideEntries.map((entry) => {
    if (typeof entry === 'object' && entry !== null && 'path' in (entry as object)) {
      return String((entry as { path?: string }).path ?? '')
    }
    return String(entry ?? '')
  })

  return { images: nextImages, moved: true, targetIndex: toIndex, slidePaths }
}

function DeferredBookPanel({ children }: { children: ReactNode }) {
  return (
    <Suspense
      fallback={(
        <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-300">
          <Loader2 className="h-4 w-4 animate-spin" />
          Carregando painel…
        </div>
      )}
    >
      {children}
    </Suspense>
  )
}

export default function BookWorkspace() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const {
    translateJobId: activeTranslateJobId,
    translateBookId,
    translateJobProgress,
    setActiveTranslateJob,
  } = useTranslateProgress()

  // Replace useJob with direct book loading
  const [job, setJob] = useState<any>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isError, setIsError] = useState(false)
  const [error, setError] = useState<any>(null)
  const jobRef = useRef<any>(job)
  jobRef.current = job
  const logsRef = useRef<LogEntry[]>([])
  const pendingRewriteRef = useRef<{ chapter: number; section: number; prevContent: string } | null>(null)
  /** When pendingChapterWrite === -1 (generate chapters AI), only clear loading after we've seen job running or failed */
  const chapterGenSeenRunningRef = useRef(false)
  /** After translation completes we refetch the book; when job updates, sync draftPlan from server so the UI shows translated text */
  const pendingTranslationSyncRef = useRef(false)
  /** Chaves já logadas como "traduzido e atualizado" no log da tela (evita duplicar) */
  const lastLoggedTranslateKeysRef = useRef<Set<string>>(new Set())

  const initialLoadDone = useRef(false)

  /** Aba visível (reduz polling em segundo plano para economizar memória/CPU) */
  const [isPageVisible, setIsPageVisible] = useState(
    () => (typeof document !== 'undefined' ? !document.hidden : true)
  )
  useEffect(() => {
    const onVisibility = () => setIsPageVisible(!document.hidden)
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  /** Label legível para unit_key de tradução (meta, ch_0, sec_0_1, sub_0_1_2) */
  const getTranslateUnitLabel = useCallback((unitKey: string): string => {
    if (unitKey === 'meta') return 'Metadados (título, objetivos)'
    if (unitKey.startsWith('ch_')) {
      const n = parseInt(unitKey.split('_')[1], 10) || 0
      return `Capítulo ${n + 1}`
    }
    if (unitKey.startsWith('sec_')) {
      const parts = unitKey.split('_')
      const c = (parseInt(parts[1], 10) || 0) + 1
      const s = (parseInt(parts[2], 10) || 0) + 1
      return `Cap. ${c} – Seção ${s}`
    }
    if (unitKey.startsWith('sub_')) {
      const parts = unitKey.split('_')
      const c = (parseInt(parts[1], 10) || 0) + 1
      const s = (parseInt(parts[2], 10) || 0) + 1
      const u = (parseInt(parts[3], 10) || 0) + 1
      return `Cap. ${c} – Sec. ${s} – Subseção ${u}`
    }
    return unitKey
  }, [])

  const refetch = useCallback(async (silent = false) => {
    if (!id) return

    // Only show loading spinner on initial load, never on background refreshes
    if (!silent && !initialLoadDone.current) {
      setIsLoading(true)
      setIsError(false)
      setError(null)
    }

    try {
      const jobResponse = await api.get(`/status/${id}`)
      const jobData = jobResponse.data
      // Se status=completed mas final_state vazio (sem book_plan), enriquecer com /books/{id}
      // preservando tool_progress e logs do /status
      const fs = jobData?.final_state || {}
      const hasPlan = fs.book_plan || fs.final_book_plan || fs.final_script || fs.course_plan || fs.structure || fs.chapters
      if (jobData?.status === 'completed' && !hasPlan) {
        try {
          const bookResponse = await api.get(`/books/${id}`)
          const bookData = bookResponse.data
          const chs = bookData.structure ?? bookData.chapters ?? []
          const planFromBooks: Record<string, unknown> = {
            title: bookData.title, subtitle: bookData.subtitle, author: bookData.author,
            author_inspiration: bookData.author_inspiration || bookData.author,
            author_styles: bookData.author_styles || [], cover_designer_styles: bookData.cover_designer_styles || [],
            chapters: bookData.chapters ?? chs, full_epub_path: bookData.full_epub_path,
            full_colab_notebook_path: bookData.full_colab_notebook_path, cover_path: bookData.cover_path,
            back_cover_path: bookData.back_cover_path, prologue: bookData.prologue,
            acknowledgments: bookData.acknowledgments, draft: bookData.draft, objective: bookData.objective,
            language: bookData.language, description: bookData.description, keywords: bookData.keywords,
            target_audience: bookData.target_audience, book_prompts: bookData.book_prompts,
            global_section_prompt: bookData.global_section_prompt,
            min_images_per_chapter: bookData.min_images_per_chapter ?? 1,
            default_min_text_length: bookData.default_min_text_length,
            default_has_source_code: bookData.default_has_source_code,
            default_num_subsections_per_section: bookData.default_num_subsections_per_section,
            default_section_writing_style: bookData.default_section_writing_style,
            source_library: bookData.source_library ?? [],
          }
          if (bookData.structure != null) planFromBooks.structure = bookData.structure
          // Mesclar: dados do /books + tool_progress/logs do /status
          jobData.final_state = { ...fs, book_plan: planFromBooks }
          if (!jobData.id) jobData.id = bookData.id
          if (!jobData.topic) jobData.topic = bookData.title
        } catch { /* se /books falhar, segue com dados do /status */ }
      }
      setJob((prev: any) => {
        if (prev && JSON.stringify(prev) === JSON.stringify(jobData)) return prev
        return jobData
      })
      initialLoadDone.current = true
      setIsLoading(false)
    } catch (jobError) {
      // Fallback to /books (for completed books in persistent storage)
      try {
        const bookResponse = await api.get(`/books/${id}`)
        const bookData = bookResponse.data

        const chs = bookData.structure ?? bookData.chapters ?? []
        const plan: Record<string, unknown> = {
          title: bookData.title,
          subtitle: bookData.subtitle,
          author: bookData.author,
          author_inspiration: bookData.author_inspiration || bookData.author,
          author_styles: bookData.author_styles || [],
          cover_designer_styles: bookData.cover_designer_styles || [],
          chapters: bookData.chapters ?? chs,
          full_epub_path: bookData.full_epub_path,
          full_colab_notebook_path: bookData.full_colab_notebook_path,
          cover_path: bookData.cover_path,
          back_cover_path: bookData.back_cover_path,
          prologue: bookData.prologue,
          acknowledgments: bookData.acknowledgments,
          draft: bookData.draft,
          objective: bookData.objective,
          language: bookData.language,
          description: bookData.description,
          keywords: bookData.keywords,
          target_audience: bookData.target_audience,
          book_prompts: bookData.book_prompts,
          global_section_prompt: bookData.global_section_prompt,
          min_images_per_chapter: bookData.min_images_per_chapter ?? 1,
              default_min_text_length: bookData.default_min_text_length,
              default_has_source_code: bookData.default_has_source_code,
              default_num_subsections_per_section: bookData.default_num_subsections_per_section,
              default_section_writing_style: bookData.default_section_writing_style,
              source_library: bookData.source_library ?? [],
            }
        if (bookData.structure != null) plan.structure = bookData.structure
        const transformed = {
          id: bookData.id,
          topic: bookData.title,
          status: 'completed',
          final_state: { book_plan: plan },
          request_payload: {}
        }
        setJob((prev: any) => {
          if (prev && JSON.stringify(prev) === JSON.stringify(transformed)) return prev
          return transformed
        })
        initialLoadDone.current = true
        setIsLoading(false)
      } catch (bookError) {
        if (!initialLoadDone.current) {
          setIsError(true)
          const errorMessage = bookError instanceof Error ? bookError.message : String(bookError)
          setError(errorMessage)
          setIsLoading(false)
        }
      }
    }
  }, [id])

  // --- CIRURGIA ATÔMICA DA SEÇÃO (enfileirado no histórico) ---
  const handleSurgicalRegenerateObjectives = async () => {
    if (isMock) {
      alert('Não disponível em mock')
      return
    }
    if (!id || currentSection === null) return
    const additionalPrompt = window.prompt("Instruções adicionais para a IA reescrever os Objetivos desta seção (opcional):", "")
    if (additionalPrompt === null) return

    pushStepLog(`🎯 Enfileirando regeneração de objetivos da Seção ${selectedSectionIdx + 1}...`, 'info')
    try {
      const res = await api.post<{ status: string; job_id?: string }>(
        `/books/${id}/chapters/${selectedChapterIdx}/sections/${selectedSectionIdx}/regenerate_objectives`,
        { instructions: additionalPrompt || undefined }
      )
      const jobId = res.data?.job_id
      if (jobId) {
        pushStepLog('Enfileirado. Acompanhe no Histórico; pode continuar usando a tela.', 'success')
        const t = setInterval(async () => {
            try {
            const statusRes = await api.get(`/status/${jobId}`).catch(() => null)
            const status = statusRes?.data?.status
            if (status === 'completed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
              const newObj = statusRes?.data?.final_state?.new_objective
              if (newObj) handleSectionFieldChange('purpose', newObj)
              await refetch(true)
            } else if (status === 'failed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
              await refetch(true)
            }
          } catch {
            /* ignore */
          }
        }, 2500)
        transientPollIntervalsRef.current.add(t)
      }
    } catch (e: any) {
      console.error(e)
      alert(e?.response?.data?.detail || e.message)
    }
  }

  const handleSurgicalRegenerateContent = async () => {
    if (isMock) {
      alert('Não disponível em mock')
      return
    }
    if (!id || currentSection === null) return
    const additionalPrompt = window.prompt("Instruções adicionais para a IA reescrever o Conteúdo desta seção (opcional):", "")
    if (additionalPrompt === null) return

    pushStepLog(`📝 Enfileirando regeneração de conteúdo da Seção ${selectedSectionIdx + 1}...`, 'info')
    try {
      const res = await api.post<{ status: string; job_id?: string }>(
        `/books/${id}/chapters/${selectedChapterIdx}/sections/${selectedSectionIdx}/regenerate_content`,
        { instructions: additionalPrompt || undefined }
      )
      const jobId = res.data?.job_id
      if (jobId) {
        pushStepLog('Enfileirado. Acompanhe no Histórico; pode continuar usando a tela.', 'success')
        const t = setInterval(async () => {
            try {
            const statusRes = await api.get(`/status/${jobId}`).catch(() => null)
            const status = statusRes?.data?.status
            if (status === 'completed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
              const newText = statusRes?.data?.final_state?.new_content
              if (newText) handleSectionFieldChange('content', newText)
              await refetch(true)
            } else if (status === 'failed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
              await refetch(true)
            }
          } catch {
            /* ignore */
          }
        }, 2500)
        transientPollIntervalsRef.current.add(t)
      }
    } catch (e: any) {
      console.error(e)
      alert(e?.response?.data?.detail || e.message)
    }
  }

  const handlePerplexityEnrichSection = async () => {
    if (isMock || !id || !currentSection) return
    setPerplexityBusy('enrich-sec')
    try {
      const res = await api.post(`/books/${id}/perplexity/enrich-section-sources`, {
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        subsection_index: null,
      })
      const jid = res.data?.job_id as string | undefined
      if (res.data?.status === 'queued' && jid) {
        pushStepLog(
          '📎 Fontes (Perplexity) enfileiradas para esta seção. O livro atualiza quando o job concluir.',
          'success'
        )
        window.open(`/jobs/${jid}`, '_blank', 'noopener,noreferrer')
      } else {
        pushStepLog(res.data?.message || 'Perplexity: solicitado.', 'success')
      }
    } catch (e: any) {
      alert(e?.response?.data?.detail || e?.message || 'Erro Perplexity')
    } finally {
      setPerplexityBusy(null)
    }
  }

  const handlePerplexityEnrichSubsection = async () => {
    if (isMock || !id || !currentSection) return
    const n = currentSection.subsections?.length ?? 0
    if (n === 0) {
      alert('Esta seção não tem subseções.')
      return
    }
    setPerplexityBusy('enrich-sub')
    try {
      const res = await api.post(`/books/${id}/perplexity/enrich-section-sources`, {
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        subsection_index: selectedSubsectionIdx,
      })
      const jid = res.data?.job_id as string | undefined
      if (res.data?.status === 'queued' && jid) {
        pushStepLog(
          `📎 Fontes (Perplexity) enfileiradas para a subseção ${selectedSubsectionIdx + 1}. Acompanhe o job.`,
          'success'
        )
        window.open(`/jobs/${jid}`, '_blank', 'noopener,noreferrer')
      } else {
        pushStepLog(res.data?.message || 'Perplexity: solicitado.', 'success')
      }
    } catch (e: any) {
      alert(e?.response?.data?.detail || e?.message || 'Erro Perplexity')
    } finally {
      setPerplexityBusy(null)
    }
  }

  const handlePerplexityEnrichBookBatch = async (targets: 'sections' | 'subsections') => {
    if (isMock || !id) return
    const label =
      targets === 'sections'
        ? 'TODAS as SEÇÕES (sem subseções)'
        : 'TODAS as SUBSEÇÕES'
    if (
      !window.confirm(
        `Buscar fontes (Perplexity) para ${label}? As entradas vão para a base de fontes do livro; o texto não é reescrito — só se acrescentam citações [n] ao fim de cada parágrafo. A lista completa sai no final do EPUB. Pode levar vários minutos. Será criado um job no Histórico.`
      )
    ) {
      return
    }
    setPerplexityBusy(targets === 'sections' ? 'enrich-sections-all' : 'enrich-subs-all')
    try {
      const res = await api.post(`/books/${id}/perplexity/enrich-all-sources`, { targets })
      const jid = res.data?.job_id as string | undefined
      if (res.data?.status === 'queued' && jid) {
        pushStepLog(
          res.data?.message ||
            'Job de fontes Perplexity enfileirado. O livro atualiza ao concluir; acompanhe no Histórico.',
          'success'
        )
        window.open(`/jobs/${jid}`, '_blank', 'noopener,noreferrer')
      } else {
        pushStepLog(res.data?.message || 'Perplexity: solicitado.', 'success')
      }
    } catch (e: any) {
      alert(e?.response?.data?.detail || e?.message || 'Erro Perplexity')
    } finally {
      setPerplexityBusy(null)
    }
  }

  const handlePerplexityWriteSectionsBook = async () => {
    if (isMock || !id) return
    const onlyEmpty = window.confirm(
      'OK = gerar texto Perplexity só nas SEÇÕES que estiverem vazias.\nCancelar = reescrever o texto de TODAS as seções (subseções não são alteradas).'
    )
    if (
      !window.confirm(
        onlyEmpty
          ? 'Confirmar: preencher apenas seções sem texto?'
          : 'Confirmar: sobrescrever o texto de todas as seções?'
      )
    ) {
      return
    }
    setPerplexityBusy('write-sections')
    try {
      const res = await api.post(`/books/${id}/perplexity/write-section-texts`, { only_empty: onlyEmpty })
      pushStepLog(res.data?.message || 'Perplexity: seções concluídas.', 'success')
      if (Array.isArray(res.data?.errors) && res.data.errors.length) {
        pushStepLog(`⚠️ Alguns itens falharam: ${res.data.errors.slice(0, 5).join(' | ')}`, 'warning')
      }
      await refetch(true)
    } catch (e: any) {
      alert(e?.response?.data?.detail || e?.message || 'Erro Perplexity')
    } finally {
      setPerplexityBusy(null)
    }
  }

  const handlePerplexityWriteSubsectionsBook = async () => {
    if (isMock || !id) return
    const onlyEmpty = window.confirm(
      'OK = gerar texto Perplexity só nas SUBSEÇÕES vazias.\nCancelar = reescrever TODAS as subseções (seções não são alteradas).'
    )
    if (
      !window.confirm(
        onlyEmpty
          ? 'Confirmar: preencher apenas subseções sem texto?'
          : 'Confirmar: sobrescrever o texto de todas as subseções?'
      )
    ) {
      return
    }
    setPerplexityBusy('write-subs')
    try {
      const res = await api.post(`/books/${id}/perplexity/write-subsection-texts`, { only_empty: onlyEmpty })
      pushStepLog(res.data?.message || 'Perplexity: subseções concluídas.', 'success')
      if (Array.isArray(res.data?.errors) && res.data.errors.length) {
        pushStepLog(`⚠️ Alguns itens falharam: ${res.data.errors.slice(0, 5).join(' | ')}`, 'warning')
      }
      await refetch(true)
    } catch (e: any) {
      alert(e?.response?.data?.detail || e?.message || 'Erro Perplexity')
    } finally {
      setPerplexityBusy(null)
    }
  }
  // ---------------------------------

  const handleChatActionComplete = useCallback(async () => {
    await refetch(true)
  }, [refetch])

  useEffect(() => {
    refetch()
  }, [refetch])

  // Livro já concluído: limpar estados de "em progresso" para não mostrar "Criando seções..." etc.
  useEffect(() => {
    if (!id || job?.status !== 'completed') return
    setIsPlanningSection(false)
    setIsGeneratingChapters(false)
    setIsWritingChapter(false)
    setIsWritingSectionIndex(null)
    setIsReplanningObjectives(false)
    setIsRewritingSectionIndex(null)
    setIsPlanningAllChaptersSections(false)
    setIsGeneratingAllSections(false)
    setPendingChapterWrite(null)
    setPendingChapterWriteJobId(null)
    setPendingSectionWriteJobId(null)
    setPlanAllChaptersStatus((prev) => {
      if (prev == null) return null
      if (prev.startsWith('Concluído') || prev.startsWith('Falha') || prev.startsWith('Erro')) return prev
      return null
    })
    setAllSectionsStatus((prev) => {
      if (prev == null) return null
      if (prev.startsWith('Concluído') || prev.startsWith('Falha') || prev.startsWith('Erro')) return prev
      return null
    })
  }, [id, job?.status])

  // Atualizar capítulos e seções à medida que são criados (polling quando o livro está em execução)
  // Em segundo plano: intervalo maior para reduzir uso de memória/CPU no navegador
  useEffect(() => {
    if (!id || job?.status !== 'running') return
    const intervalMs = isPageVisible ? 4000 : 30000
    const t = setInterval(() => refetch(true), intervalMs)
    return () => clearInterval(t)
  }, [id, job?.status, refetch, isPageVisible])

  // Push para atualizar a tela quando todas as seções forem apagadas (job ou remoção por capítulo)
  useEffect(() => {
    const onAllSectionsDeleted = (e: Event) => {
      const detail = (e as CustomEvent<{ bookId?: string }>).detail
      if (detail?.bookId && detail.bookId === id) refetch(true)
    }
    window.addEventListener('book-sections-all-deleted', onAllSectionsDeleted)
    return () => window.removeEventListener('book-sections-all-deleted', onAllSectionsDeleted)
  }, [id, refetch])

  // Push worker → aplicação: refetch do livro com debounce (vários eventos SSE seguidos → uma carga)
  const bookWorkerPushRefetchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    const onWorkerPush = (e: Event) => {
      const detail = (e as CustomEvent<{ book_id?: string; job_id?: string; type?: string }>).detail
      if (!id || !detail) return
      if (detail.book_id !== id) return
      if (bookWorkerPushRefetchTimerRef.current != null) clearTimeout(bookWorkerPushRefetchTimerRef.current)
      bookWorkerPushRefetchTimerRef.current = setTimeout(() => {
        bookWorkerPushRefetchTimerRef.current = null
        void refetch(true)
      }, 500)
    }
    window.addEventListener('worker-push', onWorkerPush)
    return () => {
      window.removeEventListener('worker-push', onWorkerPush)
      if (bookWorkerPushRefetchTimerRef.current != null) {
        clearTimeout(bookWorkerPushRefetchTimerRef.current)
        bookWorkerPushRefetchTimerRef.current = null
      }
    }
  }, [id, refetch])

  const { options } = useAppOptions()
  const { isMock, isFull, isEconomic, currentMode } = useExecutionMode('book')

  const BOOK_GENRES = [
    { id: 'comedy', name: 'Comédia' },
    { id: 'satire', name: 'Sátira' },
    { id: 'rom-com', name: 'Comédia Romântica' },
    { id: 'dark-humor', name: 'Humor Negro' },
    { id: 'parody', name: 'Paródia' },
    { id: 'cyberpunk', name: 'Cyberpunk' },
    { id: 'steampunk', name: 'Steampunk' },
    { id: 'solarpunk', name: 'Solarpunk' },
    { id: 'biopunk', name: 'Biopunk' },
    { id: 'dieselpunk', name: 'Dieselpunk' },
    { id: 'sci-fi', name: 'Ficção Científica' },
    { id: 'space-opera', name: 'Space Opera' },
    { id: 'hard-sci-fi', name: 'Hard Sci-Fi' },
    { id: 'soft-sci-fi', name: 'Soft Sci-Fi' },
    { id: 'time-travel', name: 'Viagem no Tempo' },
    { id: 'alternate-history', name: 'História Alternativa' },
    { id: 'post-apocalyptic', name: 'Pós-Apocalíptico' },
    { id: 'dystopian', name: 'Distopia' },
    { id: 'utopian', name: 'Utopia' },
    { id: 'military-sf', name: 'Ficção Científica Militar' },
    { id: 'ai-future', name: 'Futuro da IA' },
    { id: 'robotics', name: 'Robótica' },
    { id: 'space-exploration', name: 'Exploração Espacial' },
    { id: 'fantasy', name: 'Fantasia' },
    { id: 'epic-fantasy', name: 'Fantasia Épica' },
    { id: 'urban-fantasy', name: 'Fantasia Urbana' },
    { id: 'dark-fantasy', name: 'Fantasia Sombria' },
    { id: 'mythic-fantasy', name: 'Fantasia Mitológica' },
    { id: 'sword-sorcery', name: 'Espada e Feitiçaria' },
    { id: 'fairy-tale', name: 'Conto de Fadas' },
    { id: 'magical-realism', name: 'Realismo Mágico' },
    { id: 'romance', name: 'Romance' },
    { id: 'historical-romance', name: 'Romance Histórico' },
    { id: 'contemporary-romance', name: 'Romance Contemporâneo' },
    { id: 'paranormal-romance', name: 'Romance Paranormal' },
    { id: 'gothic-romance', name: 'Romance Gótico' },
    { id: 'romantic-suspense', name: 'Romance com Suspense' },
    { id: 'thriller', name: 'Thriller' },
    { id: 'psychological-thriller', name: 'Thriller Psicológico' },
    { id: 'crime', name: 'Crime' },
    { id: 'mystery', name: 'Mistério' },
    { id: 'detective', name: 'Investigação' },
    { id: 'noir', name: 'Noir' },
    { id: 'legal-thriller', name: 'Thriller Jurídico' },
    { id: 'political-thriller', name: 'Thriller Político' },
    { id: 'espionage', name: 'Espionagem' },
    { id: 'heist', name: 'Assalto/Heist' },
    { id: 'action-adventure', name: 'Ação e Aventura' },
    { id: 'adventure', name: 'Aventura' },
    { id: 'survival', name: 'Sobrevivência' },
    { id: 'war', name: 'Guerra' },
    { id: 'western', name: 'Faroeste' },
    { id: 'pirate', name: 'Piratas' },
    { id: 'spy', name: 'Agente Secreto' },
    { id: 'horror', name: 'Horror' },
    { id: 'gothic-horror', name: 'Horror Gótico' },
    { id: 'cosmic-horror', name: 'Horror Cósmico' },
    { id: 'slasher', name: 'Slasher' },
    { id: 'supernatural', name: 'Sobrenatural' },
    { id: 'ghost-story', name: 'Fantasma' },
    { id: 'vampire', name: 'Vampiros' },
    { id: 'werewolf', name: 'Lobisomens' },
    { id: 'zombie', name: 'Zumbis' },
    { id: 'young-adult', name: 'Jovem Adulto' },
    { id: 'children', name: 'Infantil' },
    { id: 'middle-grade', name: 'Infantojuvenil' },
    { id: 'coming-of-age', name: 'Amadurecimento' },
    { id: 'bildungsroman', name: 'Bildungsroman' },
    { id: 'classic', name: 'Clássico' },
    { id: 'literary', name: 'Literário' },
    { id: 'experimental', name: 'Experimental' },
    { id: 'short-stories', name: 'Contos' },
    { id: 'novella', name: 'Novela' },
    { id: 'poetry', name: 'Poesia' },
    { id: 'haiku', name: 'Haicai' },
    { id: 'essay', name: 'Ensaio' },
    { id: 'philosophy', name: 'Filosofia' },
    { id: 'psychology', name: 'Psicologia' },
    { id: 'self-help', name: 'Autoajuda' },
    { id: 'motivation', name: 'Motivacional' },
    { id: 'spirituality', name: 'Espiritualidade' },
    { id: 'religion', name: 'Religião' },
    { id: 'mindfulness', name: 'Mindfulness' },
    { id: 'business', name: 'Negócios' },
    { id: 'entrepreneurship', name: 'Empreendedorismo' },
    { id: 'marketing', name: 'Marketing' },
    { id: 'leadership', name: 'Liderança' },
    { id: 'finance', name: 'Finanças' },
    { id: 'investing', name: 'Investimentos' },
    { id: 'productivity', name: 'Produtividade' },
    { id: 'technical', name: 'Técnico' },
    { id: 'programming', name: 'Programação' },
    { id: 'data-science', name: 'Ciência de Dados' },
    { id: 'ai-ml', name: 'IA & Machine Learning' },
    { id: 'cybersecurity', name: 'Cibersegurança' },
    { id: 'devops', name: 'DevOps' },
    { id: 'cloud', name: 'Computação em Nuvem' },
    { id: 'ux-design', name: 'UX Design' },
    { id: 'history', name: 'Histórico' },
    { id: 'biography', name: 'Biografia' },
    { id: 'memoir', name: 'Memórias' },
    { id: 'documentary', name: 'Documentário' },
    { id: 'true-crime', name: 'True Crime' },
    { id: 'journalism', name: 'Jornalismo' },
    { id: 'travel', name: 'Viagens' },
    { id: 'nature', name: 'Natureza' },
    { id: 'science', name: 'Ciência' },
    { id: 'health', name: 'Saúde' },
    { id: 'nutrition', name: 'Nutrição' },
    { id: 'fitness', name: 'Fitness' },
    { id: 'parenting', name: 'Parentalidade' },
    { id: 'education', name: 'Educação' },
    { id: 'culture', name: 'Cultura' },
    { id: 'music', name: 'Música' },
    { id: 'art', name: 'Arte' },
    { id: 'photography', name: 'Fotografia' },
    { id: 'cooking', name: 'Culinária' },
    { id: 'food', name: 'Gastronomia' },
    { id: 'sports', name: 'Esportes' },
    { id: 'politics', name: 'Política' },
    { id: 'economics', name: 'Economia' },
    { id: 'law', name: 'Direito' },
    { id: 'sociology', name: 'Sociologia' },
    { id: 'anthropology', name: 'Antropologia' },
    { id: 'environment', name: 'Meio Ambiente' },
    { id: 'climate', name: 'Clima' },
    { id: 'urbanism', name: 'Urbanismo' },
    { id: 'architecture', name: 'Arquitetura' },
    { id: 'design', name: 'Design' },
    { id: 'education-guide', name: 'Guia Educacional' },
    { id: 'exam-prep', name: 'Preparatório' },
    { id: 'reference', name: 'Referência' },
    { id: 'handbook', name: 'Manual' },
    { id: 'guide', name: 'Guia' },
    { id: 'case-study', name: 'Estudo de Caso' },
    { id: 'anthology', name: 'Antologia' },
    { id: 'screenplay', name: 'Roteiro' },
    { id: 'comic', name: 'Quadrinhos' },
    { id: 'graphic-novel', name: 'Graphic Novel' },
    { id: 'interactive', name: 'Interativo' },
    { id: 'lgbtq', name: 'LGBTQIA+' },
    { id: 'diversity', name: 'Diversidade' },
    { id: 'afrofuturism', name: 'Afrofuturismo' },
    { id: 'indigenous', name: 'Perspectiva Indígena' },
    { id: 'latin', name: 'Literatura Latino-Americana' },
    { id: 'regional', name: 'Regionalista' },
    { id: 'folklore', name: 'Folclore' },
    { id: 'mythology', name: 'Mitologia' },
    { id: 'coming-of-age-lit', name: 'Amadurecimento (Literário)' },
    { id: 'inspirational', name: 'Inspirador' },
    { id: 'minimalist', name: 'Minimalista' },
    { id: 'slice-of-life', name: 'Slice of Life' },
    { id: 'workbook', name: 'Workbook' },
    { id: 'journal', name: 'Diário' },
    { id: 'notebook', name: 'Caderno' },
    { id: 'humor-essay', name: 'Ensaio Humorístico' },
    { id: 'microfiction', name: 'Microficção' },
    { id: 'flash-fiction', name: 'Flash Fiction' },
    { id: 'cli-fi', name: 'Cli-Fi (Ficção Climática)' },
    { id: 'metafiction', name: 'Metaficção' }
  ]

  const { plan, planKey } = useMemo(() => normalizePlan(job?.final_state || {}), [job])
  const [draftPlan, setDraftPlan] = useState<BookPlan | null>(plan)

  // Sincronizar draftPlan com o plano do servidor quando o livro carregar (job/refetch);
  // sem isso, draftPlan fica null e a tela não mostra capítulos/seções.
  useEffect(() => {
    if (!plan || !id) return
    const serverChapters = getChaptersFromPlan(plan)
    const hasServerContent = serverChapters.length > 0 || (plan.structure != null && Array.isArray(plan.structure) && plan.structure.length > 0)
    if (!hasServerContent) return
    setDraftPlan((prev) => {
      if (!prev) return plan
      const prevChapters = getChaptersFromPlan(prev)
      const lib = plan.source_library
      if (prevChapters.length > 0) {
        if (Array.isArray(lib) && lib.length > 0) {
          return { ...prev, source_library: lib }
        }
        return prev
      }
      return plan
    })
  }, [id, plan])

  // Livro concluído mas job veio sem book_plan (ex.: GET /status retornou mínimo): carregar plano da biblioteca (GET /books).
  const [loadingPlanFromBooks, setLoadingPlanFromBooks] = useState(false)
  const loadingPlanFromBooksRef = useRef(false)
  useEffect(() => {
    if (!id || job?.status !== 'completed') return
    const serverChapters = getChaptersFromPlan(plan)
    const hasContent = serverChapters.length > 0 || (plan?.structure != null && Array.isArray(plan.structure) && plan.structure.length > 0)
    if (hasContent || loadingPlanFromBooksRef.current) return
    loadingPlanFromBooksRef.current = true
    setLoadingPlanFromBooks(true)
    api.get(`/books/${id}`)
      .then((bookResponse) => {
        const bookData = bookResponse.data
        const chs = bookData.structure ?? bookData.chapters ?? []
        const planFromBooks: Record<string, unknown> = {
          title: bookData.title,
          subtitle: bookData.subtitle,
          author: bookData.author,
          author_inspiration: bookData.author_inspiration || bookData.author,
          author_styles: bookData.author_styles || [],
          cover_designer_styles: bookData.cover_designer_styles || [],
          chapters: bookData.chapters ?? chs,
          full_epub_path: bookData.full_epub_path,
          full_colab_notebook_path: bookData.full_colab_notebook_path,
          cover_path: bookData.cover_path,
          back_cover_path: bookData.back_cover_path,
          prologue: bookData.prologue,
          acknowledgments: bookData.acknowledgments,
          draft: bookData.draft,
          objective: bookData.objective,
          language: bookData.language,
          description: bookData.description,
          keywords: bookData.keywords,
          target_audience: bookData.target_audience,
          book_prompts: bookData.book_prompts,
          global_section_prompt: bookData.global_section_prompt,
          min_images_per_chapter: bookData.min_images_per_chapter ?? 1,
          default_min_text_length: bookData.default_min_text_length,
          default_has_source_code: bookData.default_has_source_code,
          default_num_subsections_per_section: bookData.default_num_subsections_per_section,
          default_section_writing_style: bookData.default_section_writing_style,
          source_library: bookData.source_library ?? [],
        }
        if (bookData.structure != null) planFromBooks.structure = bookData.structure
        const transformed = {
          id: bookData.id,
          topic: bookData.title,
          status: 'completed',
          final_state: { book_plan: planFromBooks },
          request_payload: {},
        }
        setJob(transformed)
      })
      .catch(() => {})
      .finally(() => {
        loadingPlanFromBooksRef.current = false
        setLoadingPlanFromBooks(false)
      })
  }, [id, job?.status, plan])

  const [activeTab, setActiveTab] = useState<'chapters' | 'structure' | 'facts' | 'bibliography' | 'design' | 'assembly' | 'section' | 'metadata' | 'subsections'>('chapters')
  const [assemblySubTab, setAssemblySubTab] = useState<'prologue' | 'acknowledgments' | 'epub'>('prologue')
  const [selectedChapterIdx, setSelectedChapterIdx] = useState(0)
  const [selectedSectionIdx, setSelectedSectionIdx] = useState(0)
  const [selectedSubsectionIdx, setSelectedSubsectionIdx] = useState(0)

  /** Deep-link from chat embed (?tab=section&chapter=2&section=3&subsection=1). */
  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const tabParam = params.get('tab')
    const embedTabs = new Set([
      'chapters',
      'structure',
      'facts',
      'bibliography',
      'design',
      'assembly',
      'section',
      'metadata',
      'subsections',
    ])
    if (tabParam && embedTabs.has(tabParam)) {
      setActiveTab(tabParam as typeof activeTab)
    }
    const ch = Number(params.get('chapter'))
    if (Number.isFinite(ch) && ch >= 1) setSelectedChapterIdx(ch - 1)
    const sec = Number(params.get('section'))
    if (Number.isFinite(sec) && sec >= 1) setSelectedSectionIdx(sec - 1)
    const sub = Number(params.get('subsection'))
    if (Number.isFinite(sub) && sub >= 1) setSelectedSubsectionIdx(sub - 1)
  }, [location.search])
  /** Perplexity: enriquecer fontes / escrever com busca web */
  const [perplexityBusy, setPerplexityBusy] = useState<
    | null
    | 'enrich-sec'
    | 'enrich-sub'
    | 'enrich-sections-all'
    | 'enrich-subs-all'
    | 'write-sections'
    | 'write-subs'
  >(null)
  /** Capítulo expandido na lista (aba Capítulos) para ver seções e subseções */
  const [expandedChapterIdx, setExpandedChapterIdx] = useState<number | null>(null)
  /** Seção expandida no formato "chIdx-secIdx" para ver subseções */
  const [expandedSectionKey, setExpandedSectionKey] = useState<string | null>(null)
  /** Painel de subseções expandido no formato "chIdx-secIdx" (recolhido por padrão) */
  const [expandedSubsectionsPanelKey, setExpandedSubsectionsPanelKey] = useState<string | null>(null)
  /** Painel de subseções na tela de seção (aba section): escondido no fim da tela, recolhido por padrão */
  const [sectionScreenSubsectionsPanelOpen, setSectionScreenSubsectionsPanelOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [isGeneratingChapters, setIsGeneratingChapters] = useState(false)
  const [isPlanningSection, setIsPlanningSection] = useState(false)
  const [isWritingChapter, setIsWritingChapter] = useState(false)
  const [isWritingSectionIndex, setIsWritingSectionIndex] = useState<number | null>(null)
  const [isApplyingAuthorStyles, setIsApplyingAuthorStyles] = useState(false)
  const [isApplyingAuthorStylesToSubsection, setIsApplyingAuthorStylesToSubsection] = useState(false)
  /** Índice da subseção em que "Aplicar estilos" está rodando (para loading no painel da aba section). */
  const [applyingAuthorStylesSubsectionIdx, setApplyingAuthorStylesSubsectionIdx] = useState<number | null>(null)
  const [pendingChapterWrite, setPendingChapterWrite] = useState<number | null>(null)
  /** When regerating chapter, API returns a child job_id; we poll this so the job appears in history */
  const [pendingChapterWriteJobId, setPendingChapterWriteJobId] = useState<string | null>(null)
  /** When writing section, API returns a child job_id; we poll this so the section appears in history */
  const [pendingSectionWriteJobId, setPendingSectionWriteJobId] = useState<string | null>(null)
  const [pendingImageJobs, setPendingImageJobs] = useState<Record<string, string[]>>({})
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [isCoverGenerating, setIsCoverGenerating] = useState(false)
  const [isCoverUploading, setIsCoverUploading] = useState(false)
  const [isGeneratingQuestions, setIsGeneratingQuestions] = useState(false)
  const [isPlanningEpub, setIsPlanningEpub] = useState(false)
  const [isPlanningEpubChapter, setIsPlanningEpubChapter] = useState(false)
  const [isPlanningSubsections, setIsPlanningSubsections] = useState(false)
  const [isGeneratingSubsectionsText, setIsGeneratingSubsectionsText] = useState(false)
  const [isRenderingCharts, setIsRenderingCharts] = useState(false)
  const [isRenderingImagePrompts, setIsRenderingImagePrompts] = useState(false)
  /** Progresso da geração de texto das subseções (job_ids enfileirados + concluídos + falhas). */
  const [subsectionTextProgress, setSubsectionTextProgress] = useState<{ jobIds: string[]; completed: number; failed: number } | null>(null)
  const [isGeneratingFullEpub, setIsGeneratingFullEpub] = useState(false)
  const [epubKeepOneImageInstructionPerChapter, setEpubKeepOneImageInstructionPerChapter] = useState(true)
  const [isExportingEpubAmazon, setIsExportingEpubAmazon] = useState(false)
  const [isGeneratingSectionObjective, setIsGeneratingSectionObjective] = useState(false)
  const [coverPrompts, setCoverPrompts] = useState({
    front: '',
    back: '',
    chapter: ''
  })
  const [coverVersion, setCoverVersion] = useState(0)
  const [captionSavedIndex, setCaptionSavedIndex] = useState<number | null>(null)
  const [isDiagramModalOpen, setIsDiagramModalOpen] = useState(false)
  const [isCodeExplainerModalOpen, setIsCodeExplainerModalOpen] = useState(false)
  const [isDiagramInlineOpen, setIsDiagramInlineOpen] = useState(false)
  const [isCodeExplainerInlineOpen, setIsCodeExplainerInlineOpen] = useState(false)
  const [isReplanningObjectives, setIsReplanningObjectives] = useState(false)
  const [isKontextModalOpen, setIsKontextModalOpen] = useState(false)
  const [kontextImageIndex, setKontextImageIndex] = useState(0)
  const [kontextPrompt, setKontextPrompt] = useState('')
  const [kontextLoading, setKontextLoading] = useState(false)
  const [isAddImageFromUrlModalOpen, setIsAddImageFromUrlModalOpen] = useState(false)
  const [addImageFromUrlUrl, setAddImageFromUrlUrl] = useState('')
  const [addImageFromUrlCaption, setAddImageFromUrlCaption] = useState('')
  const [addImageFromUrlLoading, setAddImageFromUrlLoading] = useState(false)
  const [addImageFromUrlSearchQuery, setAddImageFromUrlSearchQuery] = useState('')
  const [addImageFromUrlSearchLoading, setAddImageFromUrlSearchLoading] = useState(false)
  const [isRewritingSectionIndex, setIsRewritingSectionIndex] = useState<number | null>(null)
  const [editingSectionTitleIdx, setEditingSectionTitleIdx] = useState<number | null>(null)
  const [editingSectionTitleValue, setEditingSectionTitleValue] = useState('')
  const [desiredNumChapters, setDesiredNumChapters] = useState(5)
  const [desiredNumSectionsPerChapter, setDesiredNumSectionsPerChapter] = useState(3)
  const [isDownloadingForHeygen, setIsDownloadingForHeygen] = useState(false)
  const [isGeneratingAllSections, setIsGeneratingAllSections] = useState(false)
  const [isPlanningAllChaptersSections, setIsPlanningAllChaptersSections] = useState(false)
  const [regenerateAllBookSections, setRegenerateAllBookSections] = useState(false)
  const [allSectionsStatus, setAllSectionsStatus] = useState<string | null>(null)
  const [planAllChaptersStatus, setPlanAllChaptersStatus] = useState<string | null>(null)
  const [isGeneratingAllSectionImages, setIsGeneratingAllSectionImages] = useState(false)
  const [isGeneratingOneImagePerChapter, setIsGeneratingOneImagePerChapter] = useState(false)
  const [isDeletingAllSectionImages, setIsDeletingAllSectionImages] = useState(false)
  const [isReducingToOneImagePerChapter, setIsReducingToOneImagePerChapter] = useState(false)
  const [isGeneratingAllSlidePrompts, setIsGeneratingAllSlidePrompts] = useState(false)
  const [isTranslatingBook, setIsTranslatingBook] = useState(false)
  const [translateBookError, setTranslateBookError] = useState<string | null>(null)
  const [isTranslatingMismatched, setIsTranslatingMismatched] = useState(false)
  const [translateMismatchedError, setTranslateMismatchedError] = useState<string | null>(null)
  /** Quando true, ao iniciar tradução limpa ícones de unidades traduzidas (começar do zero). */
  const [translateFromScratch, setTranslateFromScratch] = useState(true)
  /** Unit keys that were translated (set when job completes; used to show Languages icon). */
  const [lastTranslatedUnitKeys, setLastTranslatedUnitKeys] = useState<string[]>([])
  const [extractingObjective, setExtractingObjective] = useState(false)

  const mergePlanWithSections = useCallback(
    (prevPlan: BookPlan | null, nextPlan: BookPlan | null, options?: { preferPrevTranslatedContent?: boolean }) => {
      if (!nextPlan) return prevPlan
      if (!prevPlan) return nextPlan

      const preferPrev = options?.preferPrevTranslatedContent === true

      const prevKey = getChapterKey(prevPlan)
      const nextKey = getChapterKey(nextPlan)
      const prevChapters = (prevPlan[prevKey] as BookChapter[] | undefined) || []
      const nextChapters = (nextPlan[nextKey] as BookChapter[] | undefined) || []
      const maxLen = Math.max(prevChapters.length, nextChapters.length)

      // Se o servidor enviou lista vazia (ex.: apagar todos os capítulos), respeitar e não restaurar prev
      if (nextChapters.length === 0) {
        return { ...nextPlan, [nextKey]: [] }
      }
      if (!prevChapters.length) {
        return { ...nextPlan, [nextKey]: nextChapters }
      }

      const merged = Array.from({ length: maxLen }).map((_, idx) => {
        const prevChapter = prevChapters[idx]
        const nextChapter = nextChapters[idx]
        if (!nextChapter) return prevChapter
        if (!prevChapter) return nextChapter

        const nextSections = nextChapter.sections || []
        const prevSections = prevChapter.sections || []

        if (!nextSections.length && prevSections.length) {
          return { ...nextChapter, sections: prevSections }
        }

        const mergedSections = Array.from({ length: Math.max(prevSections.length, nextSections.length) }).map((__, sectionIdx) => {
          const prevSection = prevSections[sectionIdx]
          const nextSection = nextSections[sectionIdx]
          if (!nextSection) return prevSection
          if (!prevSection) return nextSection

          const usePrevContent = preferPrev && (prevSection.content ?? '').trim() !== ''
          const prevSubs = prevSection.subsections ?? []
          const nextSubs = nextSection.subsections ?? []
          const usePrevSubsections = preferPrev && prevSubs.length > 0

          let subsections: typeof prevSection.subsections
          if (usePrevSubsections) {
            subsections = prevSection.subsections ?? []
          } else if (nextSubs.length > 0) {
            subsections = nextSubs.map((nextSub, subi) => {
              const prevSub = prevSubs[subi]
              if (!prevSub) return nextSub
              if (!preferPrev) return { ...nextSub, title: nextSub.title || prevSub.title, objective: nextSub.objective || prevSub.objective, content: nextSub.content || prevSub.content }
              const prevHasContent = (prevSub.content ?? '').trim() !== '' || (prevSub.title ?? '').trim() !== ''
              return prevHasContent
                ? { ...nextSub, title: (prevSub.title ?? '').trim() ? prevSub.title : nextSub.title, objective: (prevSub.objective ?? '').trim() ? prevSub.objective : nextSub.objective, content: (prevSub.content ?? '').trim() ? prevSub.content : nextSub.content }
                : { ...nextSub, title: nextSub.title || prevSub.title, objective: nextSub.objective || prevSub.objective, content: nextSub.content || prevSub.content }
            })
          } else {
            subsections = prevSubs.length > 0 ? prevSubs : nextSubs
          }

          return {
            ...nextSection,
            title: usePrevContent && (prevSection.title ?? '').trim() ? prevSection.title : (nextSection.title || prevSection.title),
            purpose: usePrevContent && (prevSection.purpose ?? '').trim() ? prevSection.purpose : (nextSection.purpose || prevSection.purpose),
            content_directive: usePrevContent && (prevSection.content_directive ?? '').trim() ? prevSection.content_directive : (nextSection.content_directive || prevSection.content_directive),
            content: usePrevContent ? prevSection.content : (nextSection.content || prevSection.content),
            author_styles: nextSection.author_styles?.length ? nextSection.author_styles : prevSection.author_styles,
            images: Array.isArray(nextSection.images) ? nextSection.images : (prevSection.images ?? []),
            code_blocks: nextSection.code_blocks?.length ? nextSection.code_blocks : prevSection.code_blocks,
            subsections,
          }
        }).filter(Boolean) as BookSection[]

        const usePrevChContent = preferPrev && prevChapter && ((prevChapter.title ?? '').trim() !== '' || (prevChapter.content ?? '').trim() !== '' || (prevChapter.purpose ?? '').trim() !== '')
        const mergedChapter = { ...nextChapter, sections: mergedSections }
        if (usePrevChContent && prevChapter) {
          if ((prevChapter.title ?? '').trim()) mergedChapter.title = prevChapter.title
          if ((prevChapter.purpose ?? '').trim()) mergedChapter.purpose = prevChapter.purpose
          if ((prevChapter.introduction ?? '').trim()) mergedChapter.introduction = prevChapter.introduction
          if ((prevChapter.content ?? '').trim()) mergedChapter.content = prevChapter.content
        }
        return mergedChapter
      }).filter(Boolean) as BookChapter[]

      const mergedPlan = { ...nextPlan, [nextKey]: merged }
      if (preferPrev) {
        const metaKeys = ['title', 'subtitle', 'draft', 'prologue', 'acknowledgments', 'objective', 'description'] as const
        for (const k of metaKeys) {
          const pv = (prevPlan as Record<string, unknown>)[k]
          if (pv != null && String(pv).trim() !== '') (mergedPlan as Record<string, unknown>)[k] = pv
        }
      } else {
        const nextObjective = (nextPlan.objective ?? '').trim()
        mergedPlan.objective = nextObjective !== '' ? nextPlan.objective : (prevPlan.objective ?? nextPlan.objective)
      }
      if ((prevPlan.draft ?? '').trim() !== '') mergedPlan.draft = prevPlan.draft
      if (prevPlan.facts_base != null) mergedPlan.facts_base = prevPlan.facts_base
      if (prevPlan.bibliography_base != null) mergedPlan.bibliography_base = prevPlan.bibliography_base
      return mergedPlan
    },
    []
  )
  const [selectedStyle, setSelectedStyle] = useState('')
  const [imageOptions, setImageOptions] = useState<Record<string, { styles: string[]; count: number; prompt: string; model: string }>>({})

  // Section slide workflow (prompts / code / slides) — key = sectionKey e.g. "c0-s0"
  type SlidePromptItem = { index?: number; title?: string; text?: string; code_text?: string; prompt?: string; background_prompt?: string }
  const [sectionSlidePrompts, setSectionSlidePrompts] = useState<Record<string, SlidePromptItem[]>>({})
  const hydratedSlidePromptsForJobIdRef = useRef<string | null>(null)
  const [sectionGeneratedSlideImages, setSectionGeneratedSlideImages] = useState<Record<string, string[]>>({})
  const [slideBeingDeletedIndex, setSlideBeingDeletedIndex] = useState<number | null>(null)
  const [sectionCodeImagePrompts, setSectionCodeImagePrompts] = useState<Record<string, { index: number; image_prompt: string }[]>>({})
  const [sectionCodeSourceEditorValue, setSectionCodeSourceEditorValue] = useState<Record<string, string>>({})
  const [sectionDidacticSlideModel, setSectionDidacticSlideModel] = useState<Record<string, string>>({})
  const [sectionSlideCounts, setSectionSlideCounts] = useState<Record<string, number>>({})
  const [sectionCodeSlideNoModel, setSectionCodeSlideNoModel] = useState<Record<string, boolean>>({})
  const [sectionSlideModel, setSectionSlideModel] = useState<Record<string, string>>({})
  const [sectionImagesWithoutText, setSectionImagesWithoutText] = useState<Record<string, boolean>>({})
  const [newSectionFromText, setNewSectionFromText] = useState('')
  const [newChapterFromText, setNewChapterFromText] = useState('')
  const [isGeneratingChapterFromPrompt, setIsGeneratingChapterFromPrompt] = useState(false)
  const [isGeneratingSectionFromPrompt, setIsGeneratingSectionFromPrompt] = useState(false)
  const [isGeneratingSectionPrompts, setIsGeneratingSectionPrompts] = useState(false)
  const [isGeneratingSectionCodeSource, setIsGeneratingSectionCodeSource] = useState(false)
  const [isGeneratingSectionDidacticCodePipeline, setIsGeneratingSectionDidacticCodePipeline] = useState(false)
  const [isGeneratingSectionSlides, setIsGeneratingSectionSlides] = useState(false)
  const [sectionSlidesJobs, setSectionSlidesJobs] = useState<Record<string, string>>({})
  const [isGeneratingSubsectionSlidesKey, setIsGeneratingSubsectionSlidesKey] = useState<string | null>(null)
  const [subsectionSlideDeletingIndex, setSubsectionSlideDeletingIndex] = useState<number | null>(null)
  const [subsectionDropZoneKey, setSubsectionDropZoneKey] = useState<string | null>(null)
  const [subsectionUploadingKey, setSubsectionUploadingKey] = useState<string | null>(null)
  const [creatingBlankSectionSlide, setCreatingBlankSectionSlide] = useState(false)
  const [creatingBlankSubsectionSlideKey, setCreatingBlankSubsectionSlideKey] = useState<string | null>(null)
  const [subsectionDataFileUploadKey, setSubsectionDataFileUploadKey] = useState<string | null>(null)

  // Personagens e locais (quadrinhos) para incluir no prompt das seções
  const { data: comicCharacters } = useComicCharacters()
  const { data: comicSagas } = useComicSagas()
  const [selectedComicCharacterIds, setSelectedComicCharacterIds] = useState<string[]>([])
  const [selectedComicSagaId, setSelectedComicSagaId] = useState('')
  const [selectedComicLocationKeys, setSelectedComicLocationKeys] = useState<string[]>([])

  // Image model selection
  const [imageModels, setImageModels] = useState<Array<{ id: string; name: string }>>([])
  const [imageProviders, setImageProviders] = useState<Array<{ id: string; name: string }>>([])
  const [selectedImageProvider, setSelectedImageProvider] = useState<string>('all')
  const [coverModel, setCoverModel] = useState<string>(modelConfig.getDefaultImageModel('economic') || 'imagen-4.0-ultra-generate-001')

  useEffect(() => {
    const providers = modelConfig.getImageProviders()
    setImageProviders(providers)
    const models = modelConfig.getImageModelsForSelect()
    setImageModels(models)
  }, [])

  useEffect(() => {
    const models = modelConfig.getImageModelsByProvider(selectedImageProvider)
    setImageModels(models)
  }, [selectedImageProvider])

  const pushStepLog = useCallback((message: string, level: LogEntry['level'] = 'info') => {
    const normalizedMessage = message.startsWith('[LOCAL]') ? message : `[LOCAL] ${message}`
    const newEntry: LogEntry = {
      timestamp: new Date().toISOString(),
      message: normalizedMessage,
      level,
      jobId: id,
    }
    setLogs((prev) => {
      const next = [...prev, newEntry]
      logsRef.current = next
      return next
    })
  }, [id])

  const logErrorToStepLogger = useCallback((context: string, err: unknown) => {
    const errorMessage = err instanceof Error ? err.message : String(err)
    const stackTrace = err instanceof Error && err.stack ? err.stack : ''
    pushStepLog(`❌ ${context}: ${errorMessage}`, 'error')
    if (stackTrace) {
      pushStepLog(stackTrace, 'error')
    }
  }, [pushStepLog])

  const buildComicContextForPrompt = useCallback((): string => {
    const parts: string[] = []
    type CharMeta = {
      character_id?: string
      id?: string
      name?: string
      visual_description?: string
      appearance?: string
      description?: string
      psychology?: string
      role?: string
      archetypes?: Array<{ name?: string }>
      codename?: string
      visual_analysis?: {
        reconstruction_text?: string
        structured_data?: {
          visual_description?: string
          appearance?: string
        }
      }
    }
    const chars = (comicCharacters ?? []) as CharMeta[]
    for (const id of selectedComicCharacterIds) {
      const c = chars.find((x) => (x.character_id ?? x.id) === id)
      if (!c) continue
      const name = c.name || c.codename || 'N/A'
      const visual = getComicCharacterPromptSummary(c, { maxLen: 8000 })
      const desc = (c.description ?? '').trim()
      const psychology = (c.psychology ?? '').trim()
      const role = (c.role ?? '').trim()
      const archetypesText = Array.isArray(c.archetypes)
        ? (c.archetypes as Array<{ name?: string }>).map((a) => a?.name).filter(Boolean).join(', ')
        : ''
      const metaParts: string[] = []
      if (visual) metaParts.push(`Aparência: ${visual}`)
      if (desc) metaParts.push(`Descrição: ${desc}`)
      if (psychology) metaParts.push(`Personalidade: ${psychology}`)
      if (role) metaParts.push(`Papel: ${role}`)
      if (archetypesText) metaParts.push(`Arquétipos: ${archetypesText}`)
      if (metaParts.length === 0) metaParts.push(`Nome: ${name}`)
      parts.push(`Personagem ${name}: ${metaParts.join('. ')}`)
    }
    const sagas = (comicSagas ?? []) as { saga_id?: string; id?: string; locations?: { name?: string; visual_description?: string; description?: string }[] }[]
    for (const key of selectedComicLocationKeys) {
      const [sagaId, locName] = key.split('|')
      const saga = sagas.find((s) => (s.saga_id ?? s.id) === sagaId)
      const loc = saga?.locations?.find((l) => (typeof l === 'string' ? l : l?.name) === locName)
      if (!loc || typeof loc === 'string') continue
      const desc = (loc.visual_description ?? loc.description ?? loc.name ?? '').trim()
      if (desc) parts.push(`Local ${locName}: ${desc}`)
    }
    return parts.length ? `PERSONAGENS E LOCAIS (use nas ilustrações da seção):\n${parts.join('\n')}` : ''
  }, [comicCharacters, comicSagas, selectedComicCharacterIds, selectedComicLocationKeys])

  const handleUploadCover = useCallback(async (file: File) => {
    if (!id) return
    if (isCoverUploading) return
    setIsCoverUploading(true)
    pushStepLog('📤 Enviando capa personalizada...', 'info')
    pushStepLog(`📄 Arquivo selecionado: ${file.name} (${Math.round(file.size / 1024)} KB)`, 'debug')
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('title', draftPlan?.title || job?.topic || '')
      formData.append('author', draftPlan?.author || job?.final_state?.book_plan?.author || '')

      pushStepLog('⬆️ Fazendo upload para o servidor...', 'info')

      const res = await api.post(`/books/${id}/cover`, formData, {
        timeout: 60000,
      })

      let coverPath = res.data?.cover_path
      if (coverPath && typeof coverPath === 'string') {
        coverPath = coverPath.replace(/\\/g, '/')
      }
      pushStepLog('📥 Resposta recebida do servidor.', 'debug')
      if (coverPath) {
        setDraftPlan(prev => ({ ...(prev || {}), cover_path: coverPath }))
        setJob((prev: any) => {
          if (!prev) return prev
          const nextPlan = { ...(prev.final_state?.book_plan || {}), cover_path: coverPath }
          return { ...prev, final_state: { ...(prev.final_state || {}), book_plan: nextPlan } }
        })
        pushStepLog('💾 Salvando capa no estado do job...', 'info')
        try {
          const currentFinalState = (job as any)?.final_state || {}
          const currentBookPlan = currentFinalState.book_plan || draftPlan || {}
          const updatedFinalState = {
            ...currentFinalState,
            book_plan: {
              ...currentBookPlan,
              cover_path: coverPath
            }
          }
          await api.post(`/jobs/${id}/update`, {
            final_state: updatedFinalState
          })
          pushStepLog('✅ Job atualizado com a nova capa.', 'success')
        } catch (updateErr) {
          console.error('Falha ao atualizar job com capa:', updateErr)
          logErrorToStepLogger('Erro ao atualizar job com a capa', updateErr)
        }
        setCoverVersion((v) => v + 1)
        pushStepLog(`✅ Capa enviada com sucesso: ${coverPath}`, 'success')
        pushStepLog('🖼️ Pré-visualização atualizada com a nova capa.', 'info')
        await refetch(true)
      } else {
        pushStepLog('⚠️ Upload concluído, mas sem caminho da capa.', 'warning')
        throw new Error('Resposta sem caminho da capa')
      }
    } catch (err) {
      console.error('Falha ao fazer upload da capa:', err)
      pushStepLog('❌ Falha no upload da capa.', 'error')
      logErrorToStepLogger('Erro ao fazer upload da capa', err)
      const msg = (err as { response?: { data?: { detail?: string }; status?: number }; message?: string })?.response?.data?.detail
        ?? (err as Error)?.message
        ?? 'Erro ao enviar capa. Verifique a conexão e se o backend está em execução.'
      pushStepLog(msg, 'error')
      if (err instanceof Error && err.stack) {
        pushStepLog(err.stack, 'error')
      }
    } finally {
      pushStepLog('🧹 Upload de capa finalizado.', 'debug')
      setIsCoverUploading(false)
    }
  }, [draftPlan, id, isCoverUploading, job, logErrorToStepLogger, pushStepLog, refetch])

  const hasTranslatedContentForThisBook =
    id != null && translateBookId === id && (translateJobProgress?.progress?.results != null || lastTranslatedUnitKeys.length > 0)

  useEffect(() => {
    setDraftPlan((prev) =>
      mergePlanWithSections(prev, plan, { preferPrevTranslatedContent: hasTranslatedContentForThisBook })
    )
  }, [mergePlanWithSections, plan, hasTranslatedContentForThisBook])

  // Restaurar imagens de slide do plano persistido (para não sumirem após refresh)
  useEffect(() => {
    const planChapters = getChaptersFromPlan(draftPlan)
    const fromPlan: Record<string, string[]> = {}
    planChapters.forEach((ch, chIdx) => {
      (ch.sections || []).forEach((sec, secIdx) => {
        const slideImages = (sec.images || [])
          .filter((img) => (typeof img === 'object' && (img as { source?: string }).source === 'slide') || (typeof img === 'object' && (img as { caption?: string }).caption?.startsWith('Slide ')))
          .map((img) => (typeof img === 'object' && (img as { path: string }).path) || String(img))
        if (slideImages.length > 0) fromPlan[`c${chIdx}-s${secIdx}`] = slideImages
      })
    })
    if (Object.keys(fromPlan).length > 0) {
      setSectionGeneratedSlideImages((prev) => ({ ...prev, ...fromPlan }))
    }
  }, [draftPlan])

  useEffect(() => {
    if (!id) return
    let isMounted = true
    let lastLogUpdateTs = 0
    const MIN_LOG_UPDATE_INTERVAL_MS = 2000

    const fetchLogs = async () => {
      try {
        const res = await api.get('/logs/execution', {
          params: {
            job_id: id,
            limit: 200,
          },
        })
        const entries = Array.isArray(res.data) ? res.data : []
        const parsed: LogEntry[] = entries.map((entry: any) => ({
          timestamp: entry.formatted_time || entry.timestamp || new Date().toISOString(),
          message: entry.message || entry.formatted_message || 'Log',
          level: entry.level || (String(entry.message || '').toLowerCase().includes('erro') ? 'error' : 'info'),
          jobId: entry.job_id,
        }))

        if (isMounted) {
          const prevLogs = logsRef.current
          const combined = [...parsed, ...prevLogs]
          const seen = new Set<string>()
          const deduped = combined.filter((entry) => {
            const key = `${entry.timestamp}-${entry.level}-${entry.message}`
            if (seen.has(key)) return false
            seen.add(key)
            return true
          })

          deduped.sort((a, b) => (
            new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
          ))

          const now = Date.now()
          const changed = deduped.length !== prevLogs.length || (deduped.length > 0 && (prevLogs.length === 0 || deduped[deduped.length - 1]?.message !== prevLogs[prevLogs.length - 1]?.message))
          const throttleOk = now - lastLogUpdateTs >= MIN_LOG_UPDATE_INTERVAL_MS
          logsRef.current = deduped
          if (changed && (throttleOk || deduped.length !== prevLogs.length)) {
            lastLogUpdateTs = now
            setLogs(deduped)
          }
        }
      } catch (err) {
        console.error('Failed to load book logs:', err)
      }
    }

    const silentRefetchJob = async () => {
      try {
        const res = await api.get(`/status/${id}`)
        if (!res.data || !isMounted) return
        const jobData = res.data
        // Preservar dados do job anterior que /status não retorna (book_plan, topic, type, id)
        const prev = jobRef.current
        if (prev) {
          const fs = jobData?.final_state || {}
          const hasPlan = fs.book_plan || fs.final_book_plan || fs.final_script || fs.course_plan || fs.structure || fs.chapters
          if (!hasPlan && prev?.final_state?.book_plan) {
            jobData.final_state = { ...fs, book_plan: prev.final_state.book_plan }
          }
          if (!jobData.topic && prev.topic) jobData.topic = prev.topic
          if (!jobData.type && prev.type) jobData.type = prev.type
          if (!jobData.id && prev.id) jobData.id = prev.id
        }
        setJob((prev: any) => {
          if (JSON.stringify(prev) === JSON.stringify(jobData)) return prev
          return jobData
        })
      } catch (err) {
        // Silent fail
      }
    }

    // Interval: respect tab visibility (evita travar aba em segundo plano); intervalos maiores evitam travar a UI
    const getIntervalMs = () => {
      if (!isPageVisible) return 15000
      const currentJob = jobRef.current
      const isGeneratingEpub = currentJob?.tool_progress?.epub_full && (currentJob?.tool_progress?.epub_full?.percent ?? 0) < 100
      const isBusy = isWritingChapter || isGeneratingChapters || isGeneratingEpub || isPlanningSection || isWritingSectionIndex !== null || isReplanningObjectives || isRewritingSectionIndex !== null
      return isBusy ? 3000 : 5000
    }

    let timeoutId: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      await Promise.all([fetchLogs(), silentRefetchJob()])
      if (isMounted) {
        timeoutId = setTimeout(poll, getIntervalMs())
      }
    }

    poll()

    return () => {
      isMounted = false
      if (timeoutId) clearTimeout(timeoutId)
    }
  }, [id, isPageVisible, isWritingChapter, isGeneratingChapters, isPlanningSection, isWritingSectionIndex, isReplanningObjectives, isRewritingSectionIndex])

  // Poll slides jobs (book_generate_section_slides | book_generate_section_code_slides): atualiza preview e ao concluir refetch do livro
  useEffect(() => {
    const jobs = sectionSlidesJobs
    const entries = Object.entries(jobs)
    if (entries.length === 0) return
    const poll = async () => {
      for (const [sectionKey, jobId] of entries) {
        try {
          const res = await api.get(`/status/${jobId}`)
          const data = res.data || {}
          const results = data.final_state?.results ?? []
          if (data.status === 'completed' || data.status === 'failed') {
            setSectionGeneratedSlideImages((prev) => ({
              ...prev,
              [sectionKey]: results.map((r: { image_path?: string }) => r.image_path).filter(Boolean),
            }))
            setSectionSlidesJobs((prev) => {
              const next = { ...prev }
              delete next[sectionKey]
              return next
            })
            refetch(true)
          } else {
            setSectionGeneratedSlideImages((prev) => ({
              ...prev,
              [sectionKey]: (results || []).map((r: { image_path?: string }) => r.image_path).filter(Boolean),
            }))
          }
        } catch {
          // ignore
        }
      }
    }
    const t = setInterval(poll, 3500)
    poll()
    return () => clearInterval(t)
  }, [sectionSlidesJobs, refetch])

  useEffect(() => {
    if (Object.keys(sectionSlidesJobs).length === 0) setIsGeneratingSectionSlides(false)
  }, [sectionSlidesJobs])

  useEffect(() => {
    if (!draftPlan) return
    const chapters = (draftPlan[getChapterKey(draftPlan)] as BookChapter[] | undefined) || []
    if (selectedChapterIdx >= chapters.length) {
      setSelectedChapterIdx(Math.max(0, chapters.length - 1))
      setSelectedSectionIdx(0)
    }
  }, [draftPlan, selectedChapterIdx])

  // Sincronizar número de capítulos/seções com o plano ao carregar livro já criado
  useEffect(() => {
    const ch = getChaptersFromPlan(draftPlan)
    if (draftPlan?.num_chapters != null) {
      setDesiredNumChapters(Math.max(1, Math.min(30, draftPlan.num_chapters)))
    } else if (ch.length > 0) {
      setDesiredNumChapters((prev) => (prev === 5 ? ch.length : prev))
    }
    if (draftPlan?.num_sections_per_chapter != null) {
      setDesiredNumSectionsPerChapter(Math.max(1, Math.min(15, draftPlan.num_sections_per_chapter)))
    } else if (ch.length > 0) {
      const firstSections = ch[0]?.sections?.length ?? 0
      if (firstSections > 0) {
        setDesiredNumSectionsPerChapter((prev) => (prev === 3 ? firstSections : prev))
      }
    }
  }, [draftPlan])

  const imageStyles = useMemo(() => {
    const styles = options.image?.styles || []
    return styles.map((style) => style.name)
  }, [options.image?.styles])

  const metadataAuthorStyles = useMemo(() => {
    if (!draftPlan) return []
    if (draftPlan.author_styles && draftPlan.author_styles.length > 0) {
      return draftPlan.author_styles
    }
    return parseAuthorStyles(draftPlan.author_inspiration)
  }, [draftPlan])

  const bookObjective = useMemo(() => {
    if (draftPlan?.objective) return draftPlan.objective
    const requestPayload = (job as any)?.request_payload
    return (
      requestPayload?.objective ||
      (job as any)?.final_state?.objective ||
      (job as any)?.objective ||
      (job as any)?.topic ||
      undefined
    )
  }, [draftPlan, job])


  const setChapters = useCallback((chapters: BookChapter[]) => {
    setDraftPlan((prev) => {
      if (!prev) return prev
      const key = getChapterKey(prev)
      return {
        ...prev,
        [key]: chapters,
      }
    })
  }, [])

  const savePlan = useCallback(async (nextPlan?: BookPlan | null) => {
    if (!id) return
    const planToSave = nextPlan ?? draftPlan
    if (!planToSave) return
    const chKey = getChapterKey(planToSave)
    const planChapters = (planToSave[chKey] as BookChapter[] | undefined) || []
    setSaving(true)
    try {
      const authorInspiration = planToSave.author_inspiration || planToSave.author || undefined
      const authorStyles = planToSave.author_styles || []
      await endpoints.jobs.update(id, {
        final_state: {
          book_plan: planToSave,
          author_inspiration: authorInspiration,
          author_styles: authorStyles,
        },
      })
      try {
        await endpoints.books.update(id, {
          title: planToSave.title,
          subtitle: planToSave.subtitle,
          author: planToSave.author,
          author_inspiration: authorInspiration,
          author_styles: authorStyles,
          description: planToSave.description ?? undefined,
          keywords: planToSave.keywords ?? undefined,
          language: planToSave.language,
          target_audience: planToSave.target_audience,
          prologue: planToSave.prologue,
          acknowledgments: planToSave.acknowledgments,
          draft: planToSave.draft,
          objective: planToSave.objective,
          default_min_text_length: planToSave.default_min_text_length ?? undefined,
          default_has_source_code: planToSave.default_has_source_code ?? undefined,
          default_num_subsections_per_section: planToSave.default_num_subsections_per_section ?? undefined,
          default_section_writing_style: planToSave.default_section_writing_style ?? undefined,
          cover_path: planToSave.cover_path,
          back_cover_path: planToSave.back_cover_path,
          full_epub_path: planToSave.full_epub_path,
          full_colab_notebook_path: planToSave.full_colab_notebook_path,
          ...(chKey === 'structure' ? { structure: planChapters } : { chapters: planChapters }),
          book_prompts: planToSave.book_prompts ?? undefined,
          global_section_prompt: planToSave.global_section_prompt ?? undefined,
          facts_base: planToSave.facts_base ?? undefined,
          bibliography_base: planToSave.bibliography_base ?? undefined,
          num_chapters: planToSave.num_chapters ?? desiredNumChapters,
          num_sections_per_chapter: planToSave.num_sections_per_chapter ?? desiredNumSectionsPerChapter,
          min_images_per_chapter: planToSave.min_images_per_chapter ?? 1,
          epub_image_styles: planToSave.epub_image_styles ?? undefined,
        })
      } catch (bookUpdateError) {
        console.error('Falha ao atualizar livro persistido:', bookUpdateError)
      }
      await refetch(true)
    } finally {
      setSaving(false)
    }
  }, [draftPlan, id, planKey, refetch, desiredNumChapters, desiredNumSectionsPerChapter])

  const handleExtractObjectiveFromDraft = useCallback(async () => {
    if (!id || !(draftPlan?.draft ?? '').trim()) {
      pushStepLog('Escreva ou cole o rascunho do livro antes de extrair o objetivo.', 'warning')
      return
    }
    setExtractingObjective(true)
    try {
      const res = await api.post<{ status: string; job_id?: string }>('/book/extract_objective_from_draft', {
        job_id: id,
        api_key: getApiKey(job) || undefined,
        draft: (draftPlan?.draft ?? '').trim() || undefined,
      })
      const jobId = res.data?.job_id
      if (jobId) {
        pushStepLog('Extração de objetivo enfileirada. Acompanhe no Histórico; pode continuar usando a tela.', 'success')
        const t = setInterval(async () => {
          try {
            const statusRes = await api.get(`/status/${jobId}`).catch(() => null)
            const status = statusRes?.data?.status
            if (status === 'completed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
              const obj = statusRes?.data?.final_state?.objective
              if (obj && draftPlan) {
                const updatedPlan = { ...draftPlan, objective: obj }
                setDraftPlan(updatedPlan)
                await savePlan(updatedPlan)
              }
              await refetch(true)
            } else if (status === 'failed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
            }
          } catch {
            /* ignore */
          }
        }, 2500)
        transientPollIntervalsRef.current.add(t)
      } else {
        pushStepLog('Resposta do servidor sem job_id.', 'warning')
      }
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'response' in err && typeof (err as { response?: { data?: { detail?: string } } }).response?.data?.detail === 'string'
        ? (err as { response: { data: { detail: string } } }).response.data.detail
        : err instanceof Error ? err.message : 'Erro ao extrair objetivo.'
      pushStepLog(`Erro ao extrair objetivo: ${msg}`, 'error')
    } finally {
      setExtractingObjective(false)
    }
  }, [id, draftPlan, job, pushStepLog, savePlan, refetch])

  const setSectionSlidePromptsAndPersist = useCallback((updates: Record<string, SlidePromptItem[]>) => {
    // Substituir completamente os prompts das seções (não mesclar com os antigos)
    const normalized: Record<string, SlidePromptItem[]> = {}
    for (const k of Object.keys(updates)) {
      const arr = updates[k]
      normalized[k] = Array.isArray(arr) ? [...arr] : []
    }
    setSectionSlidePrompts((prev) => {
      const next = { ...prev }
      for (const k of Object.keys(normalized)) next[k] = normalized[k]
      return next
    })
    if (!draftPlan) return
    const chKey = getChapterKey(draftPlan)
    const chs = (draftPlan[chKey] as BookChapter[] | undefined) || []
    const nextChapters = chs.map((ch, chIdx) => ({
      ...ch,
      sections: (ch.sections || []).map((sec, secIdx) => {
        const key = `c${chIdx}-s${secIdx}`
        if (key in normalized) return { ...sec, slide_prompts: normalized[key] }
        return sec
      }),
    }))
    const nextPlan = { ...draftPlan, [chKey]: nextChapters }
    setDraftPlan(nextPlan)
    void savePlan(nextPlan)
  }, [draftPlan, savePlan])

  const handleDeleteSectionPrompt = useCallback((sectionKey: string, indexToRemove: number) => {
    const current = sectionSlidePrompts[sectionKey] || []
    if (indexToRemove < 0 || indexToRemove >= current.length) return
    const next = current.filter((_, i) => i !== indexToRemove)
    setSectionSlidePromptsAndPersist({ [sectionKey]: next })
  }, [sectionSlidePrompts, setSectionSlidePromptsAndPersist])

  const handleClearSectionPrompts = useCallback((sectionKey: string) => {
    if (!window.confirm('Remover todos os prompts desta seção?')) return
    setSectionSlidePromptsAndPersist({ [sectionKey]: [] })
    setSectionGeneratedSlideImages((prev) => ({ ...prev, [sectionKey]: [] }))
    setSectionCodeImagePrompts((prev) => ({ ...prev, [sectionKey]: [] }))
  }, [setSectionSlidePromptsAndPersist])

  const getSubsectionKey = (secIdx: number, subIdx: number) =>
    `c${selectedChapterIdx}-s${secIdx}-sub${subIdx}`

  const [generatingSubsectionPromptsKey, setGeneratingSubsectionPromptsKey] = useState<string | null>(null)

  const handleRenameBook = useCallback(() => {
    if (!draftPlan) return
    const currentTitle = draftPlan.title || job?.topic || 'Livro'
    const nextTitle = window.prompt('Novo título do livro', currentTitle)?.trim()
    if (!nextTitle || nextTitle === currentTitle) return
    const nextPlan = { ...draftPlan, title: nextTitle }
    setDraftPlan(nextPlan)
    void savePlan(nextPlan)
  }, [draftPlan, job?.topic, savePlan])

  const handleTranslateBook = useCallback(() => {
    if (!id || !draftPlan?.language) return
    const apiKey = getStoredGeminiApiKey() || undefined
    setTranslateBookError(null)
    setIsTranslatingBook(true)
    pushStepLog('📤 Enviando pedido de tradução (não bloqueia a tela)...', 'info')
    const lang = draftPlan.language
    // Devolve o frame ao browser antes do POST para não “travar” a UI
    window.setTimeout(() => {
      api
        .post<{ status: string; job_id?: string }>(`/books/${id}/translate`, {
          target_language: lang,
          api_key: apiKey,
        }, { timeout: 30000 })
        .then((res) => {
          const data = res.data as { job_id?: string; translate_job_id?: string } | undefined
          const jobId = data?.job_id ?? data?.translate_job_id
          pushStepLog(
            jobId
              ? `✅ Tradução enfileirada. Acompanhe a barra de progresso no topo ou no Histórico.`
              : '✅ Tradução iniciada.',
            'success'
          )
          if (jobId && id) {
            setActiveTranslateJob(jobId, id)
            if (translateFromScratch) {
              setLastTranslatedUnitKeys([])
              try {
                localStorage.removeItem(`book_translated_units_${id}`)
              } catch (_) { /* ignore */ }
            }
            window.open(`/jobs/${jobId}`, '_blank', 'noopener,noreferrer')
          }
        })
        .catch((e: unknown) => {
          const msg =
            (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
            (e instanceof Error ? e.message : 'Erro ao traduzir')
          setTranslateBookError(String(msg))
          pushStepLog(`❌ Tradução: ${msg}`, 'error')
        })
        .finally(() => setIsTranslatingBook(false))
    }, 50)
  }, [id, draftPlan?.language, pushStepLog, setActiveTranslateJob, translateFromScratch])

  const handleTranslateBookMismatched = useCallback(() => {
    if (!id) return
    const apiKey = getStoredGeminiApiKey() || undefined
    setTranslateMismatchedError(null)
    setIsTranslatingMismatched(true)
    pushStepLog('🔍 Procurando seções em outro idioma e enfileirando tradução...', 'info')
    window.setTimeout(() => {
      endpoints.books
        .translateMismatched(id, { api_key: apiKey })
        .then((res) => {
          const data = res.data as { job_id?: string }
          const jobId = data?.job_id
          if (jobId && id) {
            setActiveTranslateJob(jobId, id)
            window.open(`/jobs/${jobId}`, '_blank', 'noopener,noreferrer')
            pushStepLog('✅ Tradução (seções em outro idioma) enfileirada. Acompanhe no topo ou no Histórico.', 'success')
            refetch(true)
          }
        })
        .catch((e: unknown) => {
          const msg =
            (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
            (e instanceof Error ? e.message : 'Erro ao traduzir seções')
          setTranslateMismatchedError(String(msg))
          pushStepLog(`❌ ${msg}`, 'error')
        })
        .finally(() => setIsTranslatingMismatched(false))
    }, 50)
  }, [id, pushStepLog, setActiveTranslateJob, refetch])

  // Reconnect translate progress bar when opening this book (e.g. another tab or no localStorage)
  useEffect(() => {
    if (!id) return
    if (translateBookId === id && activeTranslateJobId) return
    let cancelled = false
    api
      .get<{ job_id?: string; status?: string }>(`/books/${id}/active-translate-job`)
      .then((res) => {
        if (cancelled || !res?.data?.job_id) return
        setActiveTranslateJob(res.data.job_id, id)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [id, translateBookId, activeTranslateJobId, setActiveTranslateJob])

  // Limpar ref de keys logadas quando não é mais a tradução deste livro
  useEffect(() => {
    if (id !== translateBookId) lastLoggedTranslateKeysRef.current = new Set()
  }, [id, translateBookId])

  // Quando o job de tradução ativo é deste livro, mesclar progresso no draftPlan e ao concluir atualizar lastTranslatedUnitKeys e refetch.
  // Ícones (Languages): atualizar lastTranslatedUnitKeys incrementalmente a cada poll (progress.results) para que apareçam à medida que cada subseção/seção/capítulo é traduzida.
  useEffect(() => {
    if (!id || id !== translateBookId || !translateJobProgress) return
    const results = translateJobProgress.progress?.results && typeof translateJobProgress.progress.results === 'object'
      ? translateJobProgress.progress.results
      : {}
    const currentKeys = Object.keys(results)
    const prevLogged = lastLoggedTranslateKeysRef.current
    const newKeys = currentKeys.filter((k) => !prevLogged.has(k))
    if (newKeys.length > 0) {
      newKeys.sort((a, b) => {
        if (a === 'meta') return -1
        if (b === 'meta') return 1
        if (a.startsWith('ch_') && !b.startsWith('ch_')) return -1
        if (!a.startsWith('ch_') && b.startsWith('ch_')) return 1
        return a.localeCompare(b)
      })
      newKeys.forEach((key) => {
        pushStepLog(`✓ Traduzido e atualizado: ${getTranslateUnitLabel(key)}`, 'success')
      })
      lastLoggedTranslateKeysRef.current = new Set(currentKeys)
    }
    setDraftPlan((prev) => mergeTranslateResultsIntoPlan(prev, results))
    // Aplicar ícones incrementalmente: sempre que temos progress.results, persistir união com lastTranslatedUnitKeys para que os ícones apareçam à medida que as unidades são traduzidas (e sobrevivam a refresh).
    if (currentKeys.length > 0) {
      setLastTranslatedUnitKeys((prev) => {
        const merged = [...new Set([...prev, ...currentKeys])]
        try {
          if (id) localStorage.setItem(`book_translated_units_${id}`, JSON.stringify(merged))
        } catch (_) { /* ignore */ }
        return merged
      })
    }
    if (translateJobProgress.status === 'completed') {
      pendingTranslationSyncRef.current = true
      const progress = translateJobProgress.progress
      const keysFromProgress = progress?.unit_keys ?? Object.keys(results)
      if (keysFromProgress.length > 0) {
        setLastTranslatedUnitKeys(keysFromProgress)
        try {
          if (id) localStorage.setItem(`book_translated_units_${id}`, JSON.stringify(keysFromProgress))
        } catch (_) { /* ignore */ }
        pushStepLog(`✅ Tradução concluída. ${keysFromProgress.length} unidade(s) traduzida(s) e livro atualizado.`, 'success')
      }
      // Buscar job completo para obter lista final (polling pode ter parado antes do merge terminar)
      const jobId = activeTranslateJobId
      if (jobId) {
        api
          .get<{ progress?: { unit_keys?: string[]; results?: Record<string, unknown> } }>(`/jobs/${jobId}`)
          .then((res) => {
            const p = res.data?.progress
            const keys = p?.unit_keys ?? (p?.results ? Object.keys(p.results) : [])
            if (keys.length > 0) {
              setLastTranslatedUnitKeys(keys)
              try {
                if (id) localStorage.setItem(`book_translated_units_${id}`, JSON.stringify(keys))
              } catch (_) { /* ignore */ }
            }
          })
          .catch(() => {})
      }
      setTimeout(() => refetch(true), 1500)
    }
  }, [id, translateBookId, translateJobProgress, activeTranslateJobId, refetch, pushStepLog, getTranslateUnitLabel])

  // Após refetch disparado por tradução concluída, atualizar draftPlan com o plano do servidor (texto já traduzido)
  useEffect(() => {
    if (!job || !pendingTranslationSyncRef.current) return
    const { plan: nextPlan } = normalizePlan(job?.final_state || {})
    if (nextPlan) {
      setDraftPlan(nextPlan)
    }
    pendingTranslationSyncRef.current = false
  }, [job])

  useEffect(() => {
    const hasPending = pendingChapterWrite !== null || pendingSectionWriteJobId !== null
    if (!hasPending || !id) return

    let cancelled = false
    let attempts = 0
    const isBookLevelGen = pendingChapterWrite === -1
    /** When API returned a child job_id for chapter/section write, poll that job; otherwise poll the book job (legacy) */
    const pollJobId = pendingSectionWriteJobId || pendingChapterWriteJobId || id

    const poll = async () => {
      if (cancelled) return
      try {
        const statusResp = await api.get(`/status/${pollJobId}`)
        const status = statusResp.data?.status

        // When polling the book job, update job state; when polling child, refresh book after completion
        if (pollJobId === id && statusResp.data) {
          setJob((prev: any) => {
            if (prev && JSON.stringify(prev) === JSON.stringify(statusResp.data)) return prev
            return statusResp.data
          })
        }

        if (status === 'running' && isBookLevelGen) {
          chapterGenSeenRunningRef.current = true
        }

        if (status === 'completed' || status === 'failed') {
          const pendingRewrite = pendingRewriteRef.current
          if (pendingRewrite && pollJobId === id) {
            const { plan: nextPlan } = normalizePlan(statusResp.data?.final_state || {})
            const nextChapters = getChaptersFromPlan(nextPlan)
            const nextContent = nextChapters?.[pendingRewrite.chapter]?.sections?.[pendingRewrite.section]?.content || ''
            if (nextContent && nextContent !== pendingRewrite.prevContent) {
              pendingRewriteRef.current = null
              setIsWritingChapter(false)
              setIsGeneratingChapters(false)
              setIsWritingSectionIndex(null)
              setPendingChapterWrite(null)
              setPendingChapterWriteJobId(null)
              setPendingSectionWriteJobId(null)
              return
            }
          } else {
            const canClearBookLevel =
              !isBookLevelGen ||
              status === 'failed' ||
              chapterGenSeenRunningRef.current
            if (canClearBookLevel) {
              setIsWritingChapter(false)
              setIsGeneratingChapters(false)
              setIsWritingSectionIndex(null)
              setPendingChapterWrite(null)
              setPendingChapterWriteJobId(null)
              setPendingSectionWriteJobId(null)
              if (pollJobId !== id) {
                void api.get(`/status/${id}`).then((r) => r.data && setJob(r.data))
              }
              return
            }
          }
        }
      } catch (pollError) {
        console.error('Falha ao verificar status do job:', pollError)
      }

      attempts += 1
      if (attempts >= 240) {
        pendingRewriteRef.current = null
        chapterGenSeenRunningRef.current = false
        setIsWritingChapter(false)
        setIsGeneratingChapters(false)
        setIsWritingSectionIndex(null)
        setPendingChapterWrite(null)
        setPendingChapterWriteJobId(null)
        setPendingSectionWriteJobId(null)
        return
      }

      setTimeout(poll, 3000)
    }

    poll()

    return () => {
      cancelled = true
    }
  }, [pendingChapterWrite, pendingChapterWriteJobId, pendingSectionWriteJobId, id])

  const chapters = useMemo(() => getChaptersFromPlan(draftPlan), [draftPlan])

  const sourceLibrarySorted = useMemo(() => {
    const lib = draftPlan?.source_library
    if (!Array.isArray(lib) || lib.length === 0) return []
    return [...lib].sort((a, b) => (Number(a.n) || 0) - (Number(b.n) || 0))
  }, [draftPlan?.source_library])

  /** Unit keys that have translated content (during job from progress.results; after completion from lastTranslatedUnitKeys). Resiliente: união de results + lastTranslatedUnitKeys para ícones aparecerem à medida que cada subseção é traduzida. */
  const translatedUnitKeys = useMemo(() => {
    const fromResults =
      activeTranslateJobId && translateJobProgress?.progress?.results && typeof translateJobProgress.progress.results === 'object' && translateJobProgress.progress.results !== null
        ? Object.keys(translateJobProgress.progress.results)
        : []
    const fromStored = Array.isArray(lastTranslatedUnitKeys) ? lastTranslatedUnitKeys : []
    if (fromResults.length > 0 || fromStored.length > 0) {
      return [...new Set([...fromStored, ...fromResults])]
    }
    return []
  }, [activeTranslateJobId, translateJobProgress?.progress?.results, lastTranslatedUnitKeys])

  // Reset hydration ref and translated-unit state when switching book; rehydrate from localStorage if we have stored keys for this book
  useEffect(() => {
    hydratedSlidePromptsForJobIdRef.current = null
    if (!id) {
      setLastTranslatedUnitKeys([])
      return
    }
    try {
      const stored = localStorage.getItem(`book_translated_units_${id}`)
      const parsed = stored ? JSON.parse(stored) : null
      setLastTranslatedUnitKeys(Array.isArray(parsed) ? parsed : [])
    } catch (_) {
      setLastTranslatedUnitKeys([])
    }
  }, [id])

  // Hydrate sectionSlidePrompts from draftPlan uma vez ao carregar o livro (não mesclar ao regenerar)
  useEffect(() => {
    if (!id || !draftPlan) return
    if (hydratedSlidePromptsForJobIdRef.current === id) return
    const chKey = getChapterKey(draftPlan)
    const chs = (draftPlan[chKey] as BookChapter[] | undefined) || []
    const initial: Record<string, SlidePromptItem[]> = {}
    chs.forEach((ch, chIdx) => {
      (ch.sections || []).forEach((sec, secIdx) => {
        if (sec.slide_prompts?.length) {
          initial[`c${chIdx}-s${secIdx}`] = [...(sec.slide_prompts as SlidePromptItem[])]
        }
      })
    })
    if (Object.keys(initial).length > 0) {
      setSectionSlidePrompts((prev) => {
        const next = { ...prev }
        for (const k of Object.keys(initial)) next[k] = initial[k]
        return next
      })
    }
    hydratedSlidePromptsForJobIdRef.current = id
  }, [id, draftPlan])

  // Restaurar e atualizar barra de progresso de subseções ao carregar/voltar à página
  useEffect(() => {
    if (!id) return
    const pollSubsectionsProgress = async () => {
      try {
        const res = await api.get<{
          plan: { jobIds: string[]; completed: number; total: number }
          text: { jobIds: string[]; completed: number; total: number; failed: number }
        }>(`/book/${id}/subsections_progress`)
        const { plan, text } = res.data || {}
        if (plan && plan.total > 0) {
          setPlanSubsectionsJobProgress((prev) =>
            prev?.jobIds.length === plan.jobIds.length && prev?.completed === plan.completed
              ? prev
              : { jobIds: plan.jobIds, completed: plan.completed }
          )
        }
        if (text && text.total > 0) {
          setSubsectionTextProgress((prev) =>
            prev?.jobIds.length === text.jobIds.length && prev?.completed === text.completed && prev?.failed === text.failed
              ? prev
              : { jobIds: text.jobIds, completed: text.completed, failed: text.failed }
          )
        }
        const planDone = !plan || plan.total === 0 || plan.completed >= plan.total
        const textDone = !text || text.total === 0 || text.completed + text.failed >= text.total
        if (planDone && textDone) {
          if (plan?.total && plan.completed >= plan.total) setPlanSubsectionsJobProgress(null)
          if (text?.total && text.completed + text.failed >= text.total) setSubsectionTextProgress(null)
          return true
        }
        return false
      } catch {
        return false
      }
    }
    let intervalId: ReturnType<typeof setInterval> | null = null
    const run = async () => {
      const done = await pollSubsectionsProgress()
      if (done) {
        if (intervalId) clearInterval(intervalId)
        intervalId = null
        refetch(true)
        return
      }
      if (!intervalId) intervalId = setInterval(() => void run(), 3000)
    }
    void run()
    return () => {
      if (intervalId) clearInterval(intervalId)
    }
  }, [id, refetch])

  const bookDraft = useMemo(() => (
    draftPlan?.draft ||
    job?.final_state?.book_plan?.draft ||
    job?.request_payload?.draft ||
    job?.final_state?.draft ||
    ''
  ), [draftPlan?.draft, job?.final_state?.book_plan?.draft, job?.request_payload?.draft, job?.final_state?.draft])

  const applyCoverPath = useCallback((coverPath: string, target: 'front' | 'back' | 'chapter', chapterIndex?: number) => {
    if (target === 'front') {
      setCoverVersion((v) => v + 1)
      if (draftPlan) {
        const nextPlan = { ...draftPlan, cover_path: coverPath }
        setDraftPlan(nextPlan)
        void savePlan(nextPlan)
      } else {
        setDraftPlan((prev) => (prev ? { ...prev, cover_path: coverPath } : prev))
      }
    } else if (target === 'back') {
      if (draftPlan) {
        const nextPlan = { ...draftPlan, back_cover_path: coverPath }
        setDraftPlan(nextPlan)
        void savePlan(nextPlan)
      } else {
        setDraftPlan((prev) => (prev ? { ...prev, back_cover_path: coverPath } : prev))
      }
    } else if (target === 'chapter' && chapterIndex !== undefined) {
      const updated = [...chapters]
      const chapter = updated[chapterIndex]
      if (chapter) {
        chapter.cover_path = coverPath
        updated[chapterIndex] = chapter
        setChapters(updated)
        if (draftPlan) {
          const key = getChapterKey(draftPlan)
          const nextPlan = { ...draftPlan, [key]: updated }
          setDraftPlan(nextPlan)
          void savePlan(nextPlan)
        }
      }
    }
  }, [chapters, draftPlan, savePlan, setChapters])

  const runCoverGeneration = useCallback(async (prompt: string, target: 'front' | 'back' | 'chapter', chapterIndex?: number) => {
    if (isMock) {
      pushStepLog('⚠️ Geração de capa não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    if (!prompt || !prompt.trim()) {
      pushStepLog('⚠️ Defina um prompt antes de gerar a capa.', 'warning')
      return
    }
    setIsCoverGenerating(true)
    pushStepLog(`🎨 Enfileirando geração da capa (${target})...`, 'info')
    try {
      const res = await api.post('/book/generate_cover', {
        job_id: id,
        prompt: prompt.trim(),
        api_key: getApiKey(job) || undefined,
        target,
        chapter_index: chapterIndex,
        model_name: coverModel,
      })
      const coverJobId = res.data?.job_id
      const coverPathDirect = res.data?.file_path || res.data?.image_path
      if (coverPathDirect) {
        applyCoverPath(coverPathDirect, target, chapterIndex)
        pushStepLog('✅ Capa gerada.', 'success')
        await refetch(true)
        setIsCoverGenerating(false)
        return
      }
      if (!coverJobId) {
        throw new Error(res.data?.message || 'Resposta sem job_id.')
      }
      pushStepLog('✅ Tarefa enfileirada. Aguardando conclusão...', 'success')
      const poll = async (): Promise<void> => {
        try {
          const statusRes = await api.get(`/status/${coverJobId}`)
          const data = statusRes.data || {}
          if (data.status === 'completed') {
            const path = data.final_state?.file_path || data.final_state?.image_path
            if (path) {
              applyCoverPath(path, target, chapterIndex)
              pushStepLog('✅ Capa gerada.', 'success')
            }
            await refetch(true)
            setIsCoverGenerating(false)
            return
          }
          if (data.status === 'failed') {
            pushStepLog(`❌ Falha: ${data.error || 'Erro desconhecido'}`, 'error')
            setIsCoverGenerating(false)
            return
          }
          setTimeout(poll, 3000)
        } catch (e) {
          pushStepLog('Erro ao verificar status da capa.', 'error')
          setIsCoverGenerating(false)
        }
      }
      setTimeout(poll, 4000)
    } catch (err) {
      console.error('Failed to generate cover:', err)
      logErrorToStepLogger('Erro ao gerar capa', err)
      setIsCoverGenerating(false)
    }
  }, [applyCoverPath, coverModel, getApiKey, id, isMock, job, logErrorToStepLogger, pushStepLog, refetch])

  const buildBestSellerPrompt = useCallback((target: 'back' | 'chapter') => {
    if (!draftPlan) return ''
    const subject = bookObjective || draftPlan.objective || draftPlan.title || 'tema geral'
    const title = draftPlan.title || job?.topic || 'Untitled'
    const subtitle = draftPlan.subtitle || ''
    const author = draftPlan.author || draftPlan.author_inspiration || ''
    const bestSellers = draftPlan.cover_designer_styles || []
    const suffix = bestSellers.length > 0
      ? ` Inspirações de capa: ${bestSellers.join(', ')}.`
      : ''
    if (target === 'chapter') {
      const chapterTitle = chapters[selectedChapterIdx]?.title || `Capítulo ${selectedChapterIdx + 1}`
      return `Crie um divisor visual para "${chapterTitle}" do livro "${title}"${subtitle ? `: ${subtitle}` : ''}${author ? `, por ${author}` : ''} sobre "${subject}". Estilo editorial consistente, alto impacto visual, composição elegante.${suffix}`
    }
    return `Crie uma contracapa premium para o livro "${title}"${subtitle ? `: ${subtitle}` : ''}${author ? `, por ${author}` : ''} sobre "${subject}". Design editorial profissional, tipografia forte, espaço para sinopse e bio do autor.${suffix}`
  }, [bookObjective, chapters, draftPlan, job?.topic, selectedChapterIdx])
  const currentChapter = chapters[selectedChapterIdx]
  const currentSections = currentChapter?.sections || []
  const currentSection = currentSections[selectedSectionIdx]

  const openBookImageEditor = useCallback((target: BookImageEditorTarget) => {
    const imagePath = String(target.imagePath || '').trim()
    if (!imagePath || !id) return

    const chapterTitle = String(chapters[target.chapterIdx]?.title || `Capítulo ${target.chapterIdx + 1}`).trim()
    const secIdx = typeof target.sectionIdx === 'number' ? target.sectionIdx : 0
    const sectionTitle = String(
      chapters[target.chapterIdx]?.sections?.[secIdx]?.title || `Seção ${secIdx + 1}`,
    ).trim()
    const subsectionTitle = typeof target.subsectionIdx === 'number'
      ? String(
          chapters[target.chapterIdx]?.sections?.[secIdx]?.subsections?.[target.subsectionIdx]?.title ||
          chapters[target.chapterIdx]?.sections?.[secIdx]?.subsections?.[target.subsectionIdx]?.objective ||
          `Subseção ${target.subsectionIdx + 1}`,
        ).trim()
      : ''

    const sourceSection = chapters[target.chapterIdx]?.sections?.[secIdx]
    const sourceSubsection = typeof target.subsectionIdx === 'number'
      ? sourceSection?.subsections?.[target.subsectionIdx]
      : null

    const chapterBlock = chapters[target.chapterIdx]
    const sourcePrompt =
      target.scope === 'chapter'
        ? [
            target.title,
            target.caption,
            chapterBlock?.purpose,
            chapterBlock?.introduction,
            chapterBlock?.content,
          ]
            .map((item) => String(item || '').trim())
            .filter(Boolean)
            .join('\n\n')
        : [
            target.title,
            target.caption,
            sourceSubsection?.objective,
            sourceSubsection?.content,
            sourceSection?.objective,
            sourceSection?.content,
          ]
            .map((item) => String(item || '').trim())
            .filter(Boolean)
            .join('\n\n')

    const draft = createAdvancedImageStudioDraft({
      sourceType: 'book',
      originHref: `${location.pathname}${location.search}`,
      title: target.title,
      subtitle: target.caption || '',
      prompt: sourcePrompt,
      visualStyle: '',
      selectedStyleNames: [],
      selectedComicCharacterIds: [],
      characterDetailText: '',
      modelName: '',
      aspectRatio: '',
      imageUrl: imagePath,
      sourceMeta: {
        bookId: id,
        bookTitle: String(draftPlan?.title || job?.topic || 'Livro'),
        chapterIndex: target.chapterIdx,
        chapterTitle,
        ...(target.scope === 'chapter'
          ? {}
          : {
              sectionIndex: target.sectionIdx as number,
              sectionTitle,
              subsectionIndex: target.subsectionIdx,
              subsectionTitle: subsectionTitle || undefined,
            }),
        scope: target.scope,
        kind: target.kind,
        imagePath,
        originalImagePath: imagePath,
        caption: target.caption,
      },
    })

    navigate(`/image-studio/book/${encodeURIComponent(draft.id)}`)
  }, [chapters, draftPlan?.title, id, job?.topic, location.pathname, location.search, navigate])

  const handleBookImageEditorCommit = useCallback(
    async (target: BookImageEditorTarget, nextImagePath: string, meta?: { reason: string }) => {
      const trimmedNextPath = String(nextImagePath || '').trim()
      if (!target || !draftPlan || !trimmedNextPath) return

      if (target.scope === 'chapter') {
        const updatedCh = [...chapters]
        const ch = updatedCh[target.chapterIdx]
        if (!ch) return
        const cur = String(ch.cover_path || '').trim()
        if (normalizeBookImagePath(cur) === normalizeBookImagePath(trimmedNextPath)) return
        updatedCh[target.chapterIdx] = { ...ch, cover_path: trimmedNextPath }
        setChapters(updatedCh)
        await savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updatedCh })
        pushStepLog(
          `🖼️ ${target.title} atualizada${meta?.reason ? ` (${meta.reason})` : ''}.`,
          'success',
        )
        return
      }

      const oldPath = String(target.imagePath || '').trim()
      const oldNorm = normalizeBookImagePath(oldPath)
      const nextNorm = normalizeBookImagePath(trimmedNextPath)
      if (!oldNorm || oldNorm === nextNorm) {
        return
      }

      const buildFallbackImage = () => ({
        path: trimmedNextPath,
        caption: target.caption || '',
        ...(target.kind === 'slide' ? { source: 'slide' } : {}),
      })

      const replaceImageEntry = (entry: unknown) => {
        const rawPath =
          typeof entry === 'object' && entry !== null && 'path' in (entry as object)
            ? String((entry as { path?: string }).path ?? '')
            : typeof entry === 'string'
              ? entry
              : ''
        if (normalizeBookImagePath(rawPath) !== oldNorm) return entry
        if (typeof entry === 'object' && entry !== null) {
          return { ...(entry as Record<string, unknown>), path: trimmedNextPath }
        }
        return trimmedNextPath
      }

      const updated = [...chapters]
      const chapter = updated[target.chapterIdx]
      if (!chapter) return

      if (typeof target.sectionIdx !== 'number') return
      const sections = [...(chapter.sections || [])]
      const section = sections[target.sectionIdx]
      if (!section) return

      let changed = false
      let nextSection: BookSection = { ...section }

      if (target.scope === 'section') {
        let replacedInImages = false
        const sectionImages = Array.isArray(section.images) ? [...section.images] : []
        const nextImages = sectionImages.map((entry) => {
          const replaced = replaceImageEntry(entry)
          if (replaced !== entry) replacedInImages = true
          return replaced
        }) as NonNullable<BookSection['images']>

        if (replacedInImages) {
          nextSection.images = nextImages
          changed = true
        }

        if (normalizeBookImagePath(String((section as { image_path?: string }).image_path || '')) === oldNorm) {
          nextSection.image_path = trimmedNextPath
          changed = true
        }

        const nextContent = replaceMarkdownImagePath(section.content, oldPath, trimmedNextPath)
        if (nextContent !== (section.content || '')) {
          nextSection.content = nextContent
          changed = true
        }

        if (!changed) {
          nextSection.images = [...sectionImages, buildFallbackImage()]
          changed = true
        }

        sections[target.sectionIdx] = nextSection
      } else {
        const subsectionIdx = target.subsectionIdx
        if (typeof subsectionIdx !== 'number') return
        const subsections = [...(section.subsections || [])]
        const subsection = subsections[subsectionIdx]
        if (!subsection) return

        let replacedInImages = false
        const subsectionImages = Array.isArray(subsection.images) ? [...subsection.images] : []
        const nextImages = subsectionImages.map((entry) => {
          const replaced = replaceImageEntry(entry)
          if (replaced !== entry) replacedInImages = true
          return replaced
        }) as NonNullable<BookSubsection['images']>

        const nextContent = replaceMarkdownImagePath(subsection.content, oldPath, trimmedNextPath)
        const nextSubsection: BookSubsection = {
          ...subsection,
          ...(replacedInImages ? { images: nextImages } : {}),
          ...(nextContent !== (subsection.content || '') ? { content: nextContent } : {}),
        }

        if (!replacedInImages && nextContent === (subsection.content || '')) {
          nextSubsection.images = [...subsectionImages, buildFallbackImage()]
        }

        subsections[subsectionIdx] = nextSubsection
        nextSection = { ...section, subsections }
        sections[target.sectionIdx] = nextSection
        changed = true
      }

      if (!changed) return

      updated[target.chapterIdx] = { ...chapter, sections }
      setChapters(updated)

      const sectionKey = `c${target.chapterIdx}-s${target.sectionIdx}`
      setSectionGeneratedSlideImages((prev) => {
        const current = prev[sectionKey]
        if (!Array.isArray(current) || current.length === 0) return prev
        let hasReplacement = false
        const next = current.map((path) => {
          if (normalizeBookImagePath(path) !== oldNorm) return path
          hasReplacement = true
          return trimmedNextPath
        })
        return hasReplacement ? { ...prev, [sectionKey]: next } : prev
      })

      await savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
      pushStepLog(
        `🖼️ ${target.title} atualizada${meta?.reason ? ` (${meta.reason})` : ''}.`,
        'success',
      )
    },
    [chapters, draftPlan, savePlan, pushStepLog],
  )

  useEffect(() => {
    const sync = consumeAdvancedImageStudioPendingSync('book')
    if (!sync || sync.sourceType !== 'book') return

    const meta = sync.sourceMeta
    const isChapterScope = meta.scope === 'chapter'
    const replaceKeyPath = String(meta.originalImagePath || meta.imagePath || '').trim()
    const target: BookImageEditorTarget = {
      scope: meta.scope,
      kind: meta.kind,
      chapterIdx: meta.chapterIndex,
      ...(isChapterScope
        ? {}
        : { sectionIdx: meta.sectionIndex as number, subsectionIdx: meta.subsectionIndex }),
      imagePath: replaceKeyPath,
      title: String(
        sync.title ||
          (isChapterScope ? meta.chapterTitle || 'Divisor de capítulo' : meta.sectionTitle) ||
          'Imagem do livro',
      ),
      caption: String(meta.caption || sync.subtitle || '').trim() || undefined,
    }

    setSelectedChapterIdx(target.chapterIdx)
    if (!isChapterScope && typeof target.sectionIdx === 'number') {
      setSelectedSectionIdx(target.sectionIdx)
    }
    if (!isChapterScope && typeof target.subsectionIdx === 'number') {
      setSelectedSubsectionIdx(target.subsectionIdx)
    }

    void handleBookImageEditorCommit(
      target,
      String(sync.sourceMeta.imagePath || sync.imageUrl || '').trim(),
      { reason: 'editor avançado em tela cheia' },
    )
  }, [handleBookImageEditorCommit])

  /** Lista unificada: section.images + section.image_path + imagens do conteúdo (![alt](path)), para exibir na tela de edição (igual ao EPUB). */
  const currentSectionImagesAll = useMemo(() => {
    const base = currentSection ?? {}
    const arr = Array.isArray(base.images) ? base.images : []
    const normalized = arr.map((img) =>
      typeof img === 'object' && img !== null && 'path' in img
        ? { path: (img as { path: string }).path, caption: (img as { caption?: string }).caption }
        : { path: String(img), caption: '' }
    )
    const singlePath = typeof (base as { image_path?: string }).image_path === 'string' ? (base as { image_path: string }).image_path : ''
    if (singlePath && !normalized.some((img) => (img.path || '').trim() === (singlePath || '').trim())) {
      normalized.push({ path: singlePath, caption: '' })
    }
    const fromContent = extractImagesFromMarkdownContent(base.content)
    const seen = new Set<string>()
    for (const img of normalized) {
      const p = (img.path || '').trim()
      if (p) seen.add(p)
    }
    for (const img of fromContent) {
      const p = (img.path || '').trim()
      if (p && !seen.has(p)) {
        seen.add(p)
        normalized.push({ path: img.path, caption: img.caption ?? undefined })
      }
    }
    return normalized
  }, [currentSection])

  /** Seção com imagens do plano + image_path + imagens geradas como slide, para o preview EPUB da seção. */
  const sectionForEpubPreview = useMemo(() => {
    const base = currentSection ?? {}
    const sectionKey = `c${selectedChapterIdx}-s${selectedSectionIdx}`
    const slidePaths = sectionGeneratedSlideImages[sectionKey] || []
    const slideImages = slidePaths.map((path, i) => ({ path, caption: `Slide ${i + 1}` }))
    return { ...base, images: [...currentSectionImagesAll, ...slideImages] }
  }, [currentSection, currentSectionImagesAll, sectionGeneratedSlideImages, selectedChapterIdx, selectedSectionIdx])

  /** Todas as subseções da seção atual como objetos tipo seção para EpubPreview (aba Subseções e painel na aba Seção). */
  const subsectionSectionsForEpubPreview = useMemo(() => {
    const subs = currentSection?.subsections ?? []
    return subs.map((sub, subIdx) => {
      const images = (sub.images || []).map((img) =>
        typeof img === 'object' && img !== null && 'path' in img
          ? { path: (img as { path: string }).path, caption: (img as { caption?: string }).caption ?? '' }
          : { path: String(img), caption: '' }
      )
      return {
        title: sub.objective?.slice(0, 80) || `Subseção ${subIdx + 1}`,
        content: sub.content || '',
        images,
      }
    })
  }, [currentSection?.subsections])

  /** Subseção atual como objeto tipo seção para EpubPreview na aba Subseções. */
  const subsectionForEpubPreview = subsectionSectionsForEpubPreview[selectedSubsectionIdx] ?? null

  /** Por seção: objeto para EpubPreview (usado nos cards da lista de seções na aba Capítulos). */
  const sectionsForEpubPreview = useMemo(() => {
    return currentSections.map((sec, idx) => {
      const base = sec ?? {}
      const arr = Array.isArray(base.images) ? base.images : []
      const normalized = arr.map((img) =>
        typeof img === 'object' && img !== null && 'path' in img
          ? { path: (img as { path: string }).path, caption: (img as { caption?: string }).caption }
          : { path: String(img), caption: '' }
      )
      const singlePath = typeof (base as { image_path?: string }).image_path === 'string' ? (base as { image_path: string }).image_path : ''
      if (singlePath && !normalized.some((img) => (img.path || '').trim() === (singlePath || '').trim())) {
        normalized.push({ path: singlePath, caption: '' })
      }
      const fromContent = extractImagesFromMarkdownContent(base.content)
      const seen = new Set<string>()
      for (const img of normalized) {
        const p = (img.path || '').trim()
        if (p) seen.add(p)
      }
      for (const img of fromContent) {
        const p = (img.path || '').trim()
        if (p && !seen.has(p)) {
          seen.add(p)
          normalized.push({ path: img.path, caption: img.caption ?? undefined })
        }
      }
      const sectionKey = `c${selectedChapterIdx}-s${idx}`
      const slidePaths = sectionGeneratedSlideImages[sectionKey] || []
      const slideImages = slidePaths.map((path, i) => ({ path, caption: `Slide ${i + 1}` }))
      return { ...base, images: [...normalized, ...slideImages] }
    })
  }, [currentSections, sectionGeneratedSlideImages, selectedChapterIdx])

  const getImageOptions = (sectionIndex: number) => {
    const key = `c${selectedChapterIdx}-s${sectionIndex}`
    return imageOptions[key] || { styles: [], count: 1, prompt: '', model: 'imagen-4.0-ultra-generate-001' }
  }

  const setImageOption = (sectionIndex: number, next: Partial<{ styles: string[]; count: number; prompt: string; model: string }>) => {
    const key = `c${selectedChapterIdx}-s${sectionIndex}`
    setImageOptions((prev) => ({
      ...prev,
      [key]: { ...(prev[key] || { styles: [], count: 1, prompt: '', model: 'imagen-4.0-ultra-generate-001' }), ...next },
    }))
  }

  const getSectionKey = () => `c${selectedChapterIdx}-s${selectedSectionIdx}`

  const resolveSectionSlideImageUrl = (data: unknown): string => {
    if (!data) return ''
    if (typeof data === 'string') return data.startsWith('http') || data.startsWith('data:') ? data : buildFileUrl(data)
    const d = data as Record<string, unknown>
    if (d.status === 'error') return ''
    const inner = d.data as Record<string, unknown> | undefined
    const imageData = d.image_data ?? inner?.image_data
    if (typeof imageData === 'string' && imageData.startsWith('data:')) return imageData
    const path = d.file_path ?? d.image_path ?? d.image_url ?? d.path ?? d.url ?? d.src ?? inner?.file_path ?? inner?.image_path
    if (typeof path === 'string') return path.startsWith('http') || path.startsWith('data:') ? path : buildFileUrl(path)
    return ''
  }

  /** Path bruto da resposta da API (para persistir em section.images). */
  const getRawPathFromSlideResponse = (data: unknown): string => {
    if (!data || typeof data !== 'object') return ''
    const d = data as Record<string, unknown>
    const inner = d.data as Record<string, unknown> | undefined
    const path = d.file_path ?? d.image_path ?? d.path ?? inner?.file_path ?? inner?.image_path ?? d.url ?? d.image_url
    return typeof path === 'string' ? path : ''
  }

  const handleGenerateSectionPrompts = useCallback(async () => {
    const sectionKey = getSectionKey()
    const text = currentSection?.content || currentSection?.title || ''
    if (!text.trim()) {
      alert('Preencha o conteúdo da seção para gerar os prompts.')
      return
    }
    const slideCount = sectionSlideCounts[sectionKey] ?? 3
    const selectedStyles = getImageOptions(selectedSectionIdx).styles || []
    const visualStyle = selectedStyles.length > 0 ? selectedStyles.join(', ') : 'Clean Modern'
    setIsGeneratingSectionPrompts(true)
    try {
      const includeText = !(sectionImagesWithoutText[sectionKey] ?? false)
      const res = await api.post('/course/generate-lesson-prompts', {
        lesson_content: text,
        slides: slideCount,
        lesson_title: currentSection?.title || 'Seção',
        visual_style: visualStyle,
        include_text: includeText,
        job_id: id,
        execution_mode: currentMode,
        api_key: getApiKey(job) || undefined,
        prompt_type: 'general',
      })
      const prompts = Array.isArray(res.data?.prompts) ? res.data.prompts : []
      if (prompts.length > 0) {
        setSectionSlidePromptsAndPersist({ [sectionKey]: prompts })
        setSectionGeneratedSlideImages((prev) => ({ ...prev, [sectionKey]: [] }))
      } else {
        alert('Não foi possível gerar os prompts da seção.')
      }
    } catch (err) {
      console.error(err)
      alert('Erro ao gerar prompts com IA')
    } finally {
      setIsGeneratingSectionPrompts(false)
    }
  }, [currentSection, currentMode, getImageOptions, id, job, sectionImagesWithoutText, selectedChapterIdx, selectedSectionIdx, sectionSlideCounts, setSectionSlidePromptsAndPersist])

  const handleGenerateAllSlidePrompts = useCallback(async () => {
    if (isMock) {
      pushStepLog('⚠️ Gerar todos os prompts de slides não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    const totalSections = chapters.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
    const sectionsWithContent: { chIdx: number; secIdx: number; key: string; title: string; content: string }[] = []
    chapters.forEach((ch, chIdx) => {
      (ch.sections || []).forEach((sec, secIdx) => {
        const key = `c${chIdx}-s${secIdx}`
        const content = (sec.content || '').trim()
        if (!content) return
        sectionsWithContent.push({
          chIdx,
          secIdx,
          key,
          title: (sec.title || `Seção ${secIdx + 1}`).trim(),
          content,
        })
      })
    })
    if (sectionsWithContent.length === 0) {
      alert('Nenhuma seção com conteúdo. Preencha o conteúdo das seções antes de gerar os prompts.')
      return
    }
    const visualStyle = (draftPlan?.book_slide_styles?.length ? draftPlan.book_slide_styles.join(', ') : 'Clean Modern')
    pushStepLog(`🖼️ Gerando prompts de slides para ${sectionsWithContent.length} seção(ões) (estilo: ${visualStyle})...`, 'info')
    setIsGeneratingAllSlidePrompts(true)
    try {
      let done = 0
      const updates: Record<string, SlidePromptItem[]> = {}
      for (const { key, title, content } of sectionsWithContent) {
        const slideCount = sectionSlideCounts[key] ?? 3
        try {
          const res = await api.post('/course/generate-lesson-prompts', {
            lesson_content: content,
            slides: slideCount,
            lesson_title: title,
            visual_style: visualStyle,
            include_text: true,
            job_id: id,
            execution_mode: currentMode,
            api_key: getApiKey(job) || undefined,
            prompt_type: 'general',
          })
          if (res.data?.prompts?.length) {
            updates[key] = res.data.prompts
            setSectionGeneratedSlideImages((prev) => ({ ...prev, [key]: [] }))
            done += 1
          }
        } catch (err) {
          console.error(`Erro ao gerar prompts da seção ${key}:`, err)
          pushStepLog(`⚠️ Falha na seção ${key}`, 'warning')
        }
      }
      if (Object.keys(updates).length > 0) {
        setSectionSlidePromptsAndPersist(updates)
      }
      pushStepLog(`✅ Prompts de slides gerados: ${done}/${sectionsWithContent.length} seções.`, 'success')
    } catch (err) {
      console.error(err)
      logErrorToStepLogger('Erro ao gerar todos os prompts de slides', err)
    } finally {
      setIsGeneratingAllSlidePrompts(false)
    }
  }, [chapters, currentMode, draftPlan?.book_slide_styles, id, job, pushStepLog, sectionSlideCounts, getApiKey, logErrorToStepLogger, isMock, setSectionSlidePromptsAndPersist])

  const handleGenerateAllSectionSlides = useCallback(async () => {
    if (isMock) {
      pushStepLog('⚠️ Criar todos os slides não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    const defaultModel = imageModels[0]?.id
    const sectionsWithPrompts: { sectionKey: string; chIdx: number; secIdx: number; prompts: SlidePromptItem[]; codeImageList: { index: number; image_prompt: string }[] }[] = []
    chapters.forEach((ch, chIdx) => {
      (ch.sections || []).forEach((_, secIdx) => {
        const sectionKey = `c${chIdx}-s${secIdx}`
        const prompts = sectionSlidePrompts[sectionKey] || []
        if (prompts.length === 0) return
        const codeImageList = sectionCodeImagePrompts[sectionKey] || []
        sectionsWithPrompts.push({ sectionKey, chIdx, secIdx, prompts, codeImageList })
      })
    })
    if (sectionsWithPrompts.length === 0) {
      alert('Nenhuma seção com prompts de slides. Gere os prompts ("Gerar todos os prompts de slides das seções") antes.')
      return
    }
    const visualStyle = (draftPlan?.book_slide_styles?.length ? draftPlan.book_slide_styles.join(', ') : 'Clean Modern')
    pushStepLog(`🖼️ Enfileirando geração de slides para ${sectionsWithPrompts.length} seção(ões)...`, 'info')
    setIsGeneratingSectionSlides(true)
    let enqueued = 0
    try {
      for (const { sectionKey, chIdx, secIdx, prompts, codeImageList } of sectionsWithPrompts) {
        const hasCodeSlides = prompts.some((p) => (p.code_text || '').trim().length > 0)
        const useNoModel = sectionCodeSlideNoModel[sectionKey] ?? false
        const effectiveModel = sectionSlideModel[sectionKey] || defaultModel
        const sectionStyles = imageOptions[sectionKey]?.styles
        const sectionVisualStyle = (sectionStyles?.length ? sectionStyles.join(', ') : null) || visualStyle
        if (!useNoModel && !effectiveModel) continue
        try {
          if (!hasCodeSlides) {
            const res = await api.post('/book/generate-section-slides-background', {
              job_id: id,
              chapter_index: chIdx,
              section_index: secIdx,
              prompts: prompts.map((p, idx) => ({ prompt: (p.prompt || p.background_prompt || p.text || '').trim(), index: p.index ?? idx + 1 })),
              model_name: effectiveModel || undefined,
              visual_style: sectionVisualStyle,
              include_text: !(sectionImagesWithoutText[sectionKey] ?? false),
              execution_mode: currentMode,
              api_key: getApiKey(job) || undefined,
            })
            const jobId = res.data?.job_id
            if (jobId) {
              setSectionSlidesJobs((prev) => ({ ...prev, [sectionKey]: jobId }))
              enqueued += 1
            }
          } else {
            const res = await api.post('/book/generate-section-code-slides-background', {
              job_id: id,
              chapter_index: chIdx,
              section_index: secIdx,
              prompts: prompts.map((p, idx) => ({ code_text: p.code_text || p.text || '', index: p.index ?? idx + 1 })),
              code_image_prompts: (codeImageList || []).map((item) => ({ index: item.index, image_prompt: item.image_prompt || '' })),
              model_name: effectiveModel || undefined,
              skip_model_text: useNoModel,
              execution_mode: currentMode,
              api_key: getApiKey(job) || undefined,
              lesson_title: chapters[chIdx]?.sections?.[secIdx]?.title,
            })
            const jobId = res.data?.job_id
            if (jobId) {
              setSectionSlidesJobs((prev) => ({ ...prev, [sectionKey]: jobId }))
              enqueued += 1
            }
          }
        } catch (err) {
          console.error(`Erro ao enfileirar slides da seção ${sectionKey}:`, err)
          pushStepLog(`⚠️ Falha ao enfileirar seção ${sectionKey}`, 'warning')
        }
      }
      pushStepLog(`✅ ${enqueued} slide(s) enfileirado(s). Acompanhe o progresso abaixo.`, 'success')
    } catch (err) {
      console.error(err)
      logErrorToStepLogger('Erro ao criar todos os slides', err)
      setIsGeneratingSectionSlides(false)
    }
  }, [chapters, currentMode, draftPlan?.book_slide_styles, id, job, imageModels, imageOptions, sectionSlidePrompts, sectionCodeImagePrompts, sectionCodeSlideNoModel, sectionSlideModel, sectionImagesWithoutText, getApiKey, pushStepLog, logErrorToStepLogger, isMock])

  const handleGenerateSectionCodeSource = useCallback(async () => {
    const sectionKey = getSectionKey()
    const text = currentSection?.content || currentSection?.title || ''
    const pastedSourceCode = (sectionCodeSourceEditorValue[sectionKey] || '').trim()
    if (!text.trim() && !pastedSourceCode) {
      alert('Preencha o conteúdo da seção para gerar o código fonte.')
      return
    }
    const sourceCodeInput = pastedSourceCode || text
    const slideCount = sectionSlideCounts[sectionKey] ?? 3
    const selectedDidacticModelId = sectionDidacticSlideModel[sectionKey] || DIDACTIC_CODE_SLIDE_MODELS[0].id
    const selectedDidacticModel = getDidacticSlideModelById(selectedDidacticModelId)
    setIsGeneratingSectionCodeSource(true)
    try {
      const res = await api.post('/course/generate-lesson-code-source', {
        lesson_content: text,
        source_code: sourceCodeInput,
        slides: slideCount,
        lesson_title: currentSection?.title || 'Seção',
        job_id: id,
        execution_mode: currentMode,
        api_key: getApiKey(job) || undefined,
      })
      if (res.data?.prompts?.length) {
        setSectionSlidePromptsAndPersist({ [sectionKey]: res.data.prompts })
        setSectionGeneratedSlideImages((prev) => ({ ...prev, [sectionKey]: [] }))
        const selectedStyles = getImageOptions(selectedSectionIdx).styles || []
        const visualStyle = selectedStyles.length > 0
          ? `${selectedDidacticModel.prompt}, ${selectedStyles.join(', ')}`
          : selectedDidacticModel.prompt
        const promptRes = await api.post('/course/generate-lesson-code-image-prompts', {
          slides: res.data.prompts.map((p: SlidePromptItem, idx: number) => ({
            index: p.index ?? idx + 1,
            title: p.title,
            code_text: p.code_text || '',
            explanation: p.text || '',
          })),
          visual_style: visualStyle,
          lesson_title: currentSection?.title || 'Seção',
          job_id: id,
          execution_mode: currentMode,
          api_key: getApiKey(job) || undefined,
          prompt_instructions: buildComicContextForPrompt() || undefined,
        })
        if (promptRes.data?.prompts?.length) {
          setSectionCodeImagePrompts((prev) => ({ ...prev, [sectionKey]: promptRes.data.prompts }))
        }
      } else {
        alert('Não foi possível gerar o código fonte da seção.')
      }
    } catch (err) {
      console.error(err)
      alert('Erro ao gerar código fonte com IA')
    } finally {
      setIsGeneratingSectionCodeSource(false)
    }
  }, [buildComicContextForPrompt, currentSection, currentMode, getImageOptions, id, job, selectedChapterIdx, selectedSectionIdx, sectionCodeSourceEditorValue, sectionDidacticSlideModel, sectionSlideCounts, setSectionSlidePromptsAndPersist])

  const handleGenerateSectionDidacticCodeSlidesOneClick = useCallback(async () => {
    const sectionKey = getSectionKey()
    const text = currentSection?.content || currentSection?.title || ''
    const pastedSourceCode = (sectionCodeSourceEditorValue[sectionKey] || '').trim()
    if (!text.trim() && !pastedSourceCode) {
      alert('Preencha o conteúdo da seção para gerar os slides didáticos de código.')
      return
    }
    const sourceCodeInput = pastedSourceCode || text

    const slideCount = sectionSlideCounts[sectionKey] ?? 3
    const selectedDidacticModelId = sectionDidacticSlideModel[sectionKey] || DIDACTIC_CODE_SLIDE_MODELS[0].id
    const selectedDidacticModel = getDidacticSlideModelById(selectedDidacticModelId)
    const selectedStyles = getImageOptions(selectedSectionIdx).styles || []
    const visualStyle = selectedStyles.length > 0
      ? `${selectedDidacticModel.prompt}, ${selectedStyles.join(', ')}`
      : selectedDidacticModel.prompt

    setIsGeneratingSectionDidacticCodePipeline(true)
    try {
      const sourceRes = await api.post('/course/generate-lesson-code-source', {
        lesson_content: text,
        source_code: sourceCodeInput,
        slides: slideCount,
        lesson_title: currentSection?.title || 'Seção',
        job_id: id,
        execution_mode: currentMode,
        api_key: getApiKey(job) || undefined,
      })

      const prompts = sourceRes.data?.prompts || []
      if (!prompts.length) {
        alert('Não foi possível gerar o código fonte da seção.')
        return
      }

      setSectionSlidePromptsAndPersist({ [sectionKey]: prompts })
      setSectionGeneratedSlideImages((prev) => ({ ...prev, [sectionKey]: [] }))

      const promptRes = await api.post('/course/generate-lesson-code-image-prompts', {
        slides: prompts.map((p: SlidePromptItem, idx: number) => ({
          index: p.index ?? idx + 1,
          title: p.title,
          code_text: p.code_text || '',
          explanation: p.text || '',
        })),
        visual_style: visualStyle,
        lesson_title: currentSection?.title || 'Seção',
        job_id: id,
        execution_mode: currentMode,
        api_key: getApiKey(job) || undefined,
        prompt_instructions: buildComicContextForPrompt() || undefined,
      })

      const codeImagePrompts = promptRes.data?.prompts || []
      setSectionCodeImagePrompts((prev) => ({ ...prev, [sectionKey]: codeImagePrompts }))

      const useNoModel = sectionCodeSlideNoModel[sectionKey] ?? false
      const effectiveModel = sectionSlideModel[sectionKey] || getImageOptions(selectedSectionIdx).model || imageModels[0]?.id
      if (!useNoModel && !effectiveModel) {
        alert('Selecione um modelo de imagem para gerar os slides didáticos de código.')
        return
      }

      setIsGeneratingSectionSlides(true)
      const slidesRes = await api.post('/book/generate-section-code-slides-background', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        prompts: prompts.map((p: SlidePromptItem, idx: number) => ({ code_text: p.code_text || p.text || '', index: p.index ?? idx + 1 })),
        code_image_prompts: codeImagePrompts.map((item: any) => ({ index: item.index, image_prompt: item.image_prompt || '' })),
        model_name: effectiveModel || undefined,
        skip_model_text: useNoModel,
        execution_mode: currentMode,
        api_key: getApiKey(job) || undefined,
        lesson_title: currentSection?.title,
      })

      const slidesJobId = slidesRes.data?.job_id
      if (slidesJobId) {
        setSectionSlidesJobs((prev) => ({ ...prev, [sectionKey]: slidesJobId }))
        pushStepLog('✅ Pipeline de código didático enfileirado com sucesso.', 'success')
      } else {
        setIsGeneratingSectionSlides(false)
        alert('Falha ao enfileirar a geração de slides didáticos de código.')
      }
    } catch (err) {
      console.error(err)
      setIsGeneratingSectionSlides(false)
      alert('Erro no pipeline de código didático da seção.')
    } finally {
      setIsGeneratingSectionDidacticCodePipeline(false)
    }
  }, [buildComicContextForPrompt, currentSection, currentMode, getImageOptions, getApiKey, id, imageModels, job, sectionCodeSlideNoModel, sectionCodeSourceEditorValue, sectionDidacticSlideModel, sectionSlideCounts, sectionSlideModel, selectedChapterIdx, selectedSectionIdx, setSectionSlidePromptsAndPersist, pushStepLog])

  const handleGenerateSectionSlides = useCallback(async () => {
    const sectionKey = getSectionKey()
    const prompts = sectionSlidePrompts[sectionKey] || []
    if (!prompts.length) {
      alert('Gere os prompts ("Gerar Prompts") ou o código fonte ("Gerar Código Fonte") da seção primeiro.')
      return
    }
    const codeImageList = sectionCodeImagePrompts[sectionKey] || []
    const hasCodeWithImagePrompts = codeImageList.length > 0 && codeImageList.some((item) => (item.image_prompt || '').trim().length > 0)
    const hasCodeSlides = prompts.some((p) => (p.code_text || '').trim().length > 0)
    const useNoModel = sectionCodeSlideNoModel[sectionKey] ?? false
    const effectiveModel = sectionSlideModel[sectionKey] || getImageOptions(selectedSectionIdx).model || imageModels[0]?.id
    if (!useNoModel && !effectiveModel) {
      alert('Selecione um modelo de imagem para gerar os slides (ou marque "Texto Inserido sem Modelo" para slides de código).')
      return
    }

    setIsGeneratingSectionSlides(true)
    setSectionGeneratedSlideImages((prev) => ({ ...prev, [sectionKey]: [] }))
    const selectedStyles = getImageOptions(selectedSectionIdx).styles || []
    const visualStyle = selectedStyles.length > 0 ? selectedStyles.join(', ') : 'Clean Modern'

    let currentChapters = [...chapters]
    const persistSlideImage = (rawPath: string, caption: string) => {
      const next = currentChapters.map((c, ci) => {
        if (ci !== selectedChapterIdx) return c
        const ch = { ...c, sections: [...(c.sections || [])] }
        const sec = ch.sections[selectedSectionIdx]
        if (!sec) return c
        ch.sections[selectedSectionIdx] = { ...sec, images: [...(sec.images || []), { path: rawPath, caption, source: 'slide' }] }
        return ch
      })
      currentChapters = next
      setChapters(next)
      if (draftPlan) void savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: next })
    }

    try {
      if (!hasCodeSlides) {
        const res = await api.post('/book/generate-section-slides-background', {
          job_id: id,
          chapter_index: selectedChapterIdx,
          section_index: selectedSectionIdx,
          prompts: prompts.map((p, idx) => ({ prompt: (p.prompt || p.background_prompt || p.text || '').trim(), index: p.index ?? idx + 1 })),
          model_name: effectiveModel || undefined,
          visual_style: visualStyle,
          include_text: !(sectionImagesWithoutText[sectionKey] ?? false),
          execution_mode: currentMode,
          api_key: getApiKey(job) || undefined,
        })
        const slidesJobId = res.data?.job_id
        if (slidesJobId) {
          setSectionSlidesJobs((prev) => ({ ...prev, [sectionKey]: slidesJobId }))
          return
        }
        setIsGeneratingSectionSlides(false)
        pushStepLog('⚠️ Resposta da API sem job_id. Tente novamente.', 'warning')
        return
      }
      // Slides de código (prompts gerados como código fonte): sempre em background para não travar a tela
      if (hasCodeSlides) {
        const res = await api.post('/book/generate-section-code-slides-background', {
          job_id: id,
          chapter_index: selectedChapterIdx,
          section_index: selectedSectionIdx,
          prompts: prompts.map((p, idx) => ({ code_text: p.code_text || p.text || '', index: p.index ?? idx + 1 })),
          code_image_prompts: (codeImageList || []).map((item) => ({ index: item.index, image_prompt: item.image_prompt || '' })),
          model_name: effectiveModel || undefined,
          skip_model_text: useNoModel,
          execution_mode: currentMode,
          api_key: getApiKey(job) || undefined,
          lesson_title: currentSection?.title,
        })
        const slidesJobId = res.data?.job_id
        if (slidesJobId) {
          setSectionSlidesJobs((prev) => ({ ...prev, [sectionKey]: slidesJobId }))
        } else {
          setIsGeneratingSectionSlides(false)
          pushStepLog('⚠️ Resposta da API sem job_id (slides de código). Tente novamente.', 'warning')
        }
        return
      }
      {
        for (let idx = 0; idx < prompts.length; idx++) {
          const prompt = prompts[idx]
          const generatedPrompt = prompt.prompt || prompt.background_prompt || ''
          if (!generatedPrompt.trim()) continue
          const includeText = !(sectionImagesWithoutText[sectionKey] ?? false)
          const res = await api.post('/course/generate-lesson-asset', {
            lesson_content: generatedPrompt,
            asset_type: 'slide',
            include_text: includeText,
            visual_style: visualStyle,
            prompt_override: generatedPrompt,
            model_name: effectiveModel,
            job_id: id,
            execution_mode: currentMode,
            api_key: getApiKey(job) || undefined,
            lesson_title: currentSection?.title,
          })
          const url = resolveSectionSlideImageUrl(res.data)
          const rawPath = getRawPathFromSlideResponse(res.data)
          if (url) {
            setSectionGeneratedSlideImages((prev) => ({
              ...prev,
              [sectionKey]: [...(prev[sectionKey] || []), url],
            }))
          }
          if (rawPath) persistSlideImage(rawPath, `Slide ${idx + 1}`)
        }
      }
    } catch (err) {
      console.error(err)
      alert('Erro ao gerar slides')
      setIsGeneratingSectionSlides(false)
    }
  }, [chapters, currentSection, draftPlan, currentMode, getImageOptions, id, job, imageModels, sectionImagesWithoutText, sectionSlidePrompts, sectionCodeImagePrompts, sectionCodeSlideNoModel, sectionSlideModel, selectedChapterIdx, selectedSectionIdx, savePlan, pushStepLog])

  const handleGenerateSubsectionSlides = useCallback(
    async (secIdx: number, subIdx: number) => {
      const section = chapters[selectedChapterIdx]?.sections?.[secIdx]
      const sub = section?.subsections?.[subIdx]
      const prompts = (sub?.slide_prompts || []) as SlidePromptItem[]
      if (!prompts.length) {
        alert('Gere os prompts desta subseção ("Gerar Prompts") primeiro.')
        return
      }
      const subKey = getSubsectionKey(secIdx, subIdx)
      const defaultModel = imageModels[0]?.id || ''
      if (!defaultModel) {
        alert('Nenhum modelo de imagem disponível. Configure em Configurações.')
        return
      }
      const effectiveModel = sectionSlideModel[subKey] || defaultModel
      const stylesSource = (sub as { slide_styles?: string[] }).slide_styles ?? draftPlan?.book_slide_styles
      const selectedStyles = (stylesSource as { name?: string }[] | undefined)?.map((s) => (typeof s === 'object' && s?.name ? s.name : String(s)))?.slice(0, 3) || []
      const visualStyle = selectedStyles.length > 0 ? selectedStyles.join(', ') : 'Clean Modern'
      const includeText = !(sectionImagesWithoutText[subKey] ?? false)

      const promptsPayload = prompts
        .map((p, i) => ({
          prompt: (p.prompt || p.background_prompt || (p.text || '').trim()).trim(),
          index: i + 1,
        }))
        .filter((p) => p.prompt.length > 0)
      if (!promptsPayload.length) {
        alert('Nenhum prompt com texto nesta subseção.')
        return
      }

      setIsGeneratingSubsectionSlidesKey(subKey)
      try {
        const res = await api.post('/book/generate-subsection-slides-background', {
          job_id: id,
          chapter_index: selectedChapterIdx,
          section_index: secIdx,
          subsection_index: subIdx,
          prompts: promptsPayload,
          model_name: effectiveModel || undefined,
          visual_style: visualStyle,
          include_text: includeText,
          execution_mode: currentMode,
          api_key: getApiKey(job) || undefined,
          lesson_title: (sub?.objective ?? '').slice(0, 80) || 'Subseção',
        })
        const slidesJobId = res.data?.job_id
        setIsGeneratingSubsectionSlidesKey(null)
        if (slidesJobId) {
          pushStepLog?.('Slides da subseção enfileirados. Acompanhe no Histórico.', 'success')
        } else {
          pushStepLog?.('⚠️ Resposta da API sem job_id. Tente novamente.', 'warning')
        }
      } catch (err) {
        setIsGeneratingSubsectionSlidesKey(null)
        const e = err as { response?: { data?: { detail?: string }; status?: number }; message?: string }
        const detail = e?.response?.data?.detail
        const msg =
          typeof detail === 'string'
            ? detail
            : Array.isArray(detail)
              ? (detail as unknown[])
                  .map((x) =>
                    typeof x === 'object' && x != null && 'msg' in x
                      ? String((x as { msg?: string }).msg ?? '')
                      : String(x),
                  )
                  .join(', ')
              : e?.message || 'Erro ao enfileirar slides da subseção'
        console.error('Erro ao enfileirar slides da subseção:', err)
        alert(`Erro ao enfileirar slides da subseção: ${msg}`)
      }
    },
    [chapters, draftPlan, id, job, currentMode, sectionImagesWithoutText, sectionSlideModel, imageModels, selectedChapterIdx, getSubsectionKey, getApiKey, pushStepLog]
  )

  const handleDeleteSubsectionSlideImage = useCallback(
    (secIdx: number, subIdx: number, path: string) => {
      const pathToDelete = typeof path === 'string' ? path : ''
      const pathNorm = pathToDelete.trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\?.*$/, '')
      if (!pathNorm) return

      const ch = chapters[selectedChapterIdx]
      const sec = ch?.sections?.[secIdx]
      const sub = sec?.subsections?.[subIdx]
      if (!sub?.images?.length) return

      const normalize = (p: string) => {
        try {
          const s = (p || '').trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\?.*$/, '')
          return decodeURIComponent(s)
        } catch {
          return (p || '').trim().replace(/\\/g, '/').replace(/^\/+/, '')
        }
      }
      const pathNormDecoded = normalize(pathToDelete)
      const images = sub.images.filter((img) => {
        const imgPath = typeof img === 'object' && img !== null && 'path' in (img as object)
          ? String((img as { path?: string }).path ?? '')
          : typeof img === 'string'
            ? img
            : ''
        return normalize(imgPath) !== pathNormDecoded
      })
      if (images.length === sub.images.length) return

      const subs = [...(sec?.subsections || [])]
      if (subs[subIdx]) subs[subIdx] = { ...subs[subIdx], images }
      const newSec = { ...sec, subsections: subs }
      const newSections = [...(ch?.sections || [])]
      newSections[secIdx] = newSec
      const newCh = { ...ch, sections: newSections }
      const updated = chapters.map((c, i) => (i === selectedChapterIdx ? newCh : c))
      setChapters(updated)
      if (draftPlan) void savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
    },
    [chapters, draftPlan, selectedChapterIdx, savePlan]
  )

  const handleMoveSectionSlide = useCallback(async (sectionIdx: number, fromIndex: number, toIndex: number) => {
    if (!draftPlan || fromIndex === toIndex) return

    const nextChapters = [...chapters]
    const chapter = nextChapters[selectedChapterIdx]
    const section = chapter?.sections?.[sectionIdx]
    if (!chapter || !section) return

    const currentImages = Array.isArray(section.images) ? [...section.images] : []
    const reordered = reorderBookSlideImages(currentImages, fromIndex, toIndex)
    if (!reordered.moved) return

    const nextSections = [...(chapter.sections || [])]
    nextSections[sectionIdx] = { ...section, images: reordered.images as NonNullable<BookSection['images']> }
    nextChapters[selectedChapterIdx] = { ...chapter, sections: nextSections }

    const nextPlan = {
      ...draftPlan,
      [getChapterKey(draftPlan)]: nextChapters,
    }

    setChapters(nextChapters)
    setDraftPlan(nextPlan)
    setSectionGeneratedSlideImages((prev) => ({
      ...prev,
      [`c${selectedChapterIdx}-s${sectionIdx}`]: reordered.slidePaths,
    }))
    await savePlan(nextPlan)
  }, [chapters, draftPlan, savePlan, selectedChapterIdx])

  const handleMoveSubsectionSlide = useCallback(async (secIdx: number, subIdx: number, fromIndex: number, toIndex: number) => {
    if (!draftPlan || fromIndex === toIndex) return

    const nextChapters = [...chapters]
    const chapter = nextChapters[selectedChapterIdx]
    const section = chapter?.sections?.[secIdx]
    const subsection = section?.subsections?.[subIdx]
    if (!chapter || !section || !subsection) return

    const currentImages = Array.isArray(subsection.images) ? [...subsection.images] : []
    const reordered = reorderBookSlideImages(currentImages, fromIndex, toIndex)
    if (!reordered.moved) return

    const nextSubsections = [...(section.subsections || [])]
    nextSubsections[subIdx] = {
      ...subsection,
      images: reordered.images as NonNullable<BookSubsection['images']>,
    }
    const nextSections = [...(chapter.sections || [])]
    nextSections[secIdx] = { ...section, subsections: nextSubsections }
    nextChapters[selectedChapterIdx] = { ...chapter, sections: nextSections }

    const nextPlan = {
      ...draftPlan,
      [getChapterKey(draftPlan)]: nextChapters,
    }

    setChapters(nextChapters)
    setDraftPlan(nextPlan)
    await savePlan(nextPlan)
  }, [chapters, draftPlan, savePlan, selectedChapterIdx])

  const handleChapterFieldChange = (field: keyof BookChapter, value: string) => {
    const updated = [...chapters]
    if (!updated[selectedChapterIdx]) return
    updated[selectedChapterIdx] = {
      ...updated[selectedChapterIdx],
      [field]: value,
    }
    setChapters(updated)
  }

  const handleSectionFieldChange = (field: keyof BookSection, value: string) => {
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) return
    const sections = [...(chapter.sections || [])]
    if (!sections[selectedSectionIdx]) return
    sections[selectedSectionIdx] = {
      ...sections[selectedSectionIdx],
      [field]: value,
    }
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
  }

  const updateSectionAtIndex = (index: number, patch: Partial<BookSection>) => {
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) return
    const sections = [...(chapter.sections || [])]
    if (!sections[index]) return
    sections[index] = { ...sections[index], ...patch }
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
  }

  const handleGenerateSubsectionPrompts = useCallback(
    async (secIdx: number, subIdx: number) => {
      const section = currentSections[secIdx]
      const sub = section?.subsections?.[subIdx]
      const text = (sub?.content || sub?.objective || '').trim()
      if (!text) {
        alert('Preencha o objetivo ou o conteúdo da subseção para gerar os prompts.')
        return
      }
      const key = getSubsectionKey(secIdx, subIdx)
      const slideCount = sectionSlideCounts[key] ?? 3
      const stylesSource = (sub as { slide_styles?: string[] }).slide_styles ?? draftPlan?.book_slide_styles
      const selectedStyles = stylesSource?.length
        ? (stylesSource as { name?: string }[]).map((s) => (typeof s === 'object' && s?.name ? s.name : String(s))).slice(0, 3)
        : []
      const visualStyle = selectedStyles.length > 0 ? selectedStyles.join(', ') : 'Clean Modern'
      setGeneratingSubsectionPromptsKey(`${secIdx}-${subIdx}`)
      try {
        const includeText = !(sectionImagesWithoutText[key] ?? false)
        const res = await api.post('/course/generate-lesson-prompts', {
          lesson_content: text,
          slides: slideCount,
          lesson_title: sub?.objective?.slice(0, 80) || 'Subseção',
          visual_style: visualStyle,
          include_text: includeText,
          job_id: id,
          execution_mode: currentMode,
          api_key: getApiKey(job) || undefined,
          prompt_type: 'general',
        })
        const prompts = Array.isArray(res.data?.prompts) ? res.data.prompts : []
        const subs = [...(section?.subsections || [])]
        if (subs[subIdx]) {
          subs[subIdx] = { ...subs[subIdx], slide_prompts: prompts }
          updateSectionAtIndex(secIdx, { subsections: subs })
          if (draftPlan) {
            const chKey = getChapterKey(draftPlan)
            const chs = (draftPlan[chKey] as BookChapter[]) || []
            const updated = chs.map((ch, cIdx) => {
              if (cIdx !== selectedChapterIdx) return ch
              const sections = (ch.sections || []).map((s, sIdx) => (sIdx === secIdx ? { ...s, subsections: subs } : s))
              return { ...ch, sections }
            })
            void savePlan({ ...draftPlan, [chKey]: updated })
          }
        }
      } catch (err) {
        console.error(err)
        alert('Erro ao gerar prompts da subseção')
      } finally {
        setGeneratingSubsectionPromptsKey(null)
      }
    },
    [
      currentSections,
      draftPlan,
      sectionSlideCounts,
      sectionImagesWithoutText,
      selectedChapterIdx,
      id,
      currentMode,
      getApiKey,
      job,
      updateSectionAtIndex,
      savePlan,
      getChapterKey,
    ]
  )

  const handleDeleteSubsectionPrompt = useCallback(
    (secIdx: number, subIdx: number, promptIdx: number) => {
      const section = currentSections[secIdx]
      const sub = section?.subsections?.[subIdx]
      const prompts = [...(sub?.slide_prompts || [])]
      prompts.splice(promptIdx, 1)
      const subs = [...(section?.subsections || [])]
      if (subs[subIdx]) {
        subs[subIdx] = { ...subs[subIdx], slide_prompts: prompts }
        updateSectionAtIndex(secIdx, { subsections: subs })
      }
      void savePlan()
    },
    [currentSections, updateSectionAtIndex, savePlan]
  )

  const handleClearSubsectionPrompts = useCallback(
    (secIdx: number, subIdx: number) => {
      if (!window.confirm('Remover todos os prompts desta subseção?')) return
      const section = currentSections[secIdx]
      const subs = [...(section?.subsections || [])]
      if (subs[subIdx]) {
        subs[subIdx] = { ...subs[subIdx], slide_prompts: [] }
        updateSectionAtIndex(secIdx, { subsections: subs })
      }
      void savePlan()
    },
    [currentSections, updateSectionAtIndex, savePlan]
  )

  const updateSectionAt = (chapterIdx: number, sectionIdx: number, patch: Partial<BookSection>) => {
    const updated = [...chapters]
    const chapter = updated[chapterIdx]
    if (!chapter) return
    const sections = [...(chapter.sections || [])]
    if (!sections[sectionIdx]) return
    sections[sectionIdx] = { ...sections[sectionIdx], ...patch }
    chapter.sections = sections
    updated[chapterIdx] = chapter
    setChapters(updated)
  }

  const startEditingSectionTitle = (idx: number) => {
    const section = currentSections[idx]
    setEditingSectionTitleIdx(idx)
    setEditingSectionTitleValue(section?.title || `Seção ${idx + 1}`)
  }

  const commitSectionTitleEdit = () => {
    if (editingSectionTitleIdx == null) return
    const value = editingSectionTitleValue.trim()
    const fallback = currentSections[editingSectionTitleIdx]?.title || `Seção ${editingSectionTitleIdx + 1}`
    updateSectionAtIndex(editingSectionTitleIdx, { title: value || fallback })
    setEditingSectionTitleIdx(null)
    void savePlan()
  }

  const handleGenerateQuestions = () => {
    const chapter = chapters[selectedChapterIdx]
    const section = currentSection
    if (!chapter || !section) return

    setIsGeneratingQuestions(true)
    scheduleHeavyBookWork(async () => {
      try {
        const response = await api.post('/book/generate_questions', {
          content: (section.content || '').substring(0, 8000),
          board_id: section.question_board || 'cespe-cebraspe',
          question_type: section.question_type || 'multiple-choice',
          difficulty: section.question_difficulty || 'medio',
          num_questions: section.num_questions || 5,
          include_answers: section.question_include_answers !== false,
          include_explanation: section.question_include_explanation !== false,
          section_title: section.title || '',
          chapter_title: chapter.title || '',
          model_name: job?.request_payload?.model_text || 'gemini-3.5-flash',
          api_key: getApiKey(job) || undefined,
        })

        const generatedQuestions = response.data?.questions || ''
        updateSectionAtIndex(selectedSectionIdx, { questions: generatedQuestions })
        pushStepLog(`✅ ${section.num_questions || 5} questões geradas para "${section.title}".`, 'success')

        if (draftPlan) {
          const updatedChapters = [...chapters]
          const ch = updatedChapters[selectedChapterIdx]
          if (ch) {
            const secs = [...(ch.sections || [])]
            secs[selectedSectionIdx] = { ...secs[selectedSectionIdx], questions: generatedQuestions }
            ch.sections = secs
            updatedChapters[selectedChapterIdx] = ch
            const planKey = getChapterKey(draftPlan)
            void savePlan({ ...draftPlan, [planKey]: updatedChapters })
          }
        }
      } catch (err) {
        console.error('Failed to generate questions:', err)
        pushStepLog(`❌ Erro ao gerar questões: ${err instanceof Error ? err.message : 'Erro desconhecido'}`, 'error')
      } finally {
        setIsGeneratingQuestions(false)
      }
    })
  }

  const handleSectionContentUpdate = (nextContent: string) => {
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) return
    const sections = [...(chapter.sections || [])]
    if (!sections[selectedSectionIdx]) return
    sections[selectedSectionIdx] = { ...sections[selectedSectionIdx], content: nextContent }
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
    pushStepLog('🧪 Conteúdo atualizado pela ferramenta de laboratório.', 'info')

    if (draftPlan) {
      const planKey = getChapterKey(draftPlan)
      void savePlan({
        ...draftPlan,
        [planKey]: updated,
      })
    }
  }

  const handleGenerateReigenText = async () => {
    if (!id || !currentSection) return

    const content = currentSection.content || ''
    if (!content.trim()) {
      pushStepLog('⚠️ O conteúdo da seção está vazio.', 'warning')
      return
    }

    // Atualizar estado de geração
    const updatedSections = [...(chapters[selectedChapterIdx]?.sections || [])]
    updatedSections[selectedSectionIdx] = {
      ...currentSection,
      isGeneratingReigenText: true
    }
    const updatedChapters = [...chapters]
    updatedChapters[selectedChapterIdx] = {
      ...chapters[selectedChapterIdx],
      sections: updatedSections
    }
    setChapters(updatedChapters)

    try {
      const res = await api.post('/concurso/generate-reigen-text', {
        text: content,
        normalize_numbers: true
      })

      const cleanText = res.data?.clean_text || ''

      // Atualizar texto Reigen da seção
      const finalSections = [...(chapters[selectedChapterIdx]?.sections || [])]
      finalSections[selectedSectionIdx] = {
        ...currentSection,
        reigenText: cleanText,
        editedReigenText: cleanText,
        isGeneratingReigenText: false
      }
      const finalChapters = [...chapters]
      finalChapters[selectedChapterIdx] = {
        ...chapters[selectedChapterIdx],
        sections: finalSections
      }
      setChapters(finalChapters)
      pushStepLog('✅ Texto Reigen gerado com sucesso.', 'success')
    } catch (err: any) {
      console.error('Failed to generate Reigen text:', err)
      pushStepLog(`⚠️ Erro ao gerar texto Reigen: ${err?.response?.data?.detail || err.message}`, 'error')
      // Reverter estado de geração
      const errorSections = [...(chapters[selectedChapterIdx]?.sections || [])]
      errorSections[selectedSectionIdx] = {
        ...currentSection,
        isGeneratingReigenText: false
      }
      const errorChapters = [...chapters]
      errorChapters[selectedChapterIdx] = {
        ...chapters[selectedChapterIdx],
        sections: errorSections
      }
      setChapters(errorChapters)
    }
  }

  const handleDownloadReigenText = () => {
    const textToDownload = currentSection?.editedReigenText || currentSection?.reigenText || ''
    if (!textToDownload) return

    const blob = new Blob([textToDownload], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const sectionTitle = currentSection?.title || `Seção_${selectedSectionIdx + 1}`
    a.download = `${sectionTitle}_reigen.txt`.replace(/[^a-z0-9]/gi, '_').toLowerCase()
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const handleDownloadForHeygen = async () => {
    const existingClean = currentSection?.editedReigenText || currentSection?.reigenText || ''
    const content = currentSection?.content || ''

    if (existingClean.trim()) {
      const blob = new Blob([existingClean], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const sectionTitle = currentSection?.title || `Seção_${selectedSectionIdx + 1}`
      a.download = `${sectionTitle}_heygen.txt`.replace(/[^a-z0-9]/gi, '_').toLowerCase()
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      return
    }

    if (!content.trim()) {
      pushStepLog('⚠️ O conteúdo da seção está vazio. Preencha o texto ou gere o Texto Reigen antes de exportar para HeyGen.', 'warning')
      return
    }

    setIsDownloadingForHeygen(true)
    try {
      const res = await api.post('/concurso/generate-reigen-text', {
        text: content,
        normalize_numbers: true
      })
      const cleanText = res.data?.clean_text || ''
      const blob = new Blob([cleanText], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const sectionTitle = currentSection?.title || `Seção_${selectedSectionIdx + 1}`
      a.download = `${sectionTitle}_heygen.txt`.replace(/[^a-z0-9]/gi, '_').toLowerCase()
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      pushStepLog('✅ Texto exportado para HeyGen com sucesso.', 'success')
    } catch (err: any) {
      console.error('Failed to export for HeyGen:', err)
      pushStepLog(`❌ Erro ao exportar para HeyGen: ${err?.response?.data?.detail || err.message}`, 'error')
    } finally {
      setIsDownloadingForHeygen(false)
    }
  }

  const handleApplyAuthorStylesToSection = () => {
    if (isMock) {
      pushStepLog('⚠️ Reescrita por estilo não está disponível no modo mock.', 'warning')
      return
    }
    if (!id || !currentSection) return

    const styles = currentSection.author_styles || []
    if (!styles.length) {
      pushStepLog('⚠️ Selecione estilos de autor antes de aplicar.', 'warning')
      return
    }

    const content = currentSection.content || ''
    if (!content.trim()) {
      pushStepLog('⚠️ O conteúdo da seção está vazio.', 'warning')
      return
    }

    const objective = currentSection.purpose || currentSection.content_directive || ''
    const draftBlock = bookDraft?.trim() ? `\nRASCUNHO DO LIVRO (contexto):\n${bookDraft.trim()}\n\n` : ''
    const prompt = `Você é um editor especializado em livros. Reescreva o texto abaixo no estilo de: ${styles.join(', ')}.\n\n${draftBlock}OBJETIVO DA SEÇÃO:\n${objective || 'Não informado'}\n\nTEXTO ORIGINAL:\n"""\n${content}\n"""\n\nRetorne APENAS o texto reescrito, em português (Brasil), mantendo a estrutura e a clareza.`

    pushStepLog(`✨ Aplicando estilos de autor (${styles.join(', ')}) na seção...`, 'info')
    setIsApplyingAuthorStyles(true)
    scheduleHeavyBookWork(async () => {
      try {
        const response = await api.post('/generate_text', {
          prompt,
          api_key: getApiKey(job) || undefined,
          model_name: job?.request_payload?.model_text || 'gemini-3.5-flash',
          job_id: id,
        })
        const updatedText = response.data?.text
        if (updatedText) {
          handleSectionFieldChange('content', updatedText)
          pushStepLog('✅ Estilos de autor aplicados com sucesso.', 'success')
        } else {
          pushStepLog('⚠️ A IA não retornou texto reescrito.', 'warning')
        }
      } catch (err) {
        console.error('Failed to apply author styles:', err)
        logErrorToStepLogger('Erro ao aplicar estilos de autor', err)
      } finally {
        setIsApplyingAuthorStyles(false)
      }
    })
  }

  const applyAuthorStylesToSubsectionAt = useCallback((subIdx: number) => {
    if (isMock) {
      pushStepLog('⚠️ Reescrita por estilo não está disponível no modo mock.', 'warning')
      return
    }
    const sub = currentSection?.subsections?.[subIdx]
    if (!sub) return

    const styles = sub.author_styles || []
    if (!styles.length) {
      pushStepLog('⚠️ Selecione estilos de autor antes de aplicar.', 'warning')
      return
    }

    const content = sub.content || ''
    if (!content.trim()) {
      pushStepLog('⚠️ O conteúdo da subseção está vazio.', 'warning')
      return
    }

    const objective = sub.objective || ''
    const draftBlock = bookDraft?.trim() ? `\nRASCUNHO DO LIVRO (contexto):\n${bookDraft.trim()}\n\n` : ''
    const prompt = `Você é um editor especializado em livros. Reescreva o texto abaixo no estilo de: ${styles.join(', ')}.\n\n${draftBlock}OBJETIVO DA SUBSEÇÃO:\n${objective || 'Não informado'}\n\nTEXTO ORIGINAL:\n"""\n${content}\n"""\n\nRetorne APENAS o texto reescrito, em português (Brasil), mantendo a estrutura e a clareza.`

    pushStepLog(`✨ Aplicando estilos de autor (${styles.join(', ')}) na subseção...`, 'info')
    setIsApplyingAuthorStylesToSubsection(true)
    setApplyingAuthorStylesSubsectionIdx(subIdx)
    scheduleHeavyBookWork(async () => {
      try {
        const response = await api.post('/generate_text', {
          prompt,
          api_key: getApiKey(job) || undefined,
          model_name: job?.request_payload?.model_text || 'gemini-3.5-flash',
          job_id: id,
        })
        const updatedText = response.data?.text
        if (updatedText) {
          updateSubsectionAtIndex(undefined, subIdx, { content: updatedText })
          pushStepLog('✅ Estilos de autor aplicados com sucesso.', 'success')
        } else {
          pushStepLog('⚠️ A IA não retornou texto reescrito.', 'warning')
        }
      } catch (err) {
        console.error('Failed to apply author styles to subsection:', err)
        logErrorToStepLogger('Erro ao aplicar estilos de autor na subseção', err)
      } finally {
        setIsApplyingAuthorStylesToSubsection(false)
        setApplyingAuthorStylesSubsectionIdx(null)
      }
    })
  }, [currentSection?.subsections, bookDraft, isMock, job, getApiKey, pushStepLog, updateSubsectionAtIndex, scheduleHeavyBookWork, logErrorToStepLogger])

  const handleApplyAuthorStylesToSubsection = () => {
    applyAuthorStylesToSubsectionAt(selectedSubsectionIdx)
  }

  const handleAddChapter = async () => {
    if (!isMock && id) {
      try {
        pushStepLog('☁️ Criando capítulo no backend...', 'info')
        const response = await endpoints.books.addChapter({
          job_id: id,
          title: `Capítulo ${chapters.length + 1}`,
          purpose: '',
          api_key: getApiKey(job) || undefined,
        })
        await refetch(true)
        const createdIndex = Number(response?.data?.chapter_index)
        if (Number.isFinite(createdIndex) && createdIndex >= 0) {
          setSelectedChapterIdx(createdIndex)
          setSelectedSectionIdx(0)
        }
        pushStepLog('✅ Capítulo criado e sincronizado com o backend.', 'success')
        return
      } catch (err) {
        logErrorToStepLogger('Falha ao criar capítulo via endpoint /add_chapter. Aplicando fallback local', err)
      }
    }

    const updated = [...chapters]
    updated.push({
      chapter: updated.length + 1,
      title: `Capítulo ${updated.length + 1}`,
      purpose: '',
      sections: [],
      content: '',
    })
    setChapters(updated)
    setSelectedChapterIdx(updated.length - 1)
    setSelectedSectionIdx(0)
  }

  const handleMoveChapter = useCallback(
    (fromIdx: number, direction: 'up' | 'down') => {
      const toIdx = direction === 'up' ? fromIdx - 1 : fromIdx + 1
      if (toIdx < 0 || toIdx >= chapters.length) return
      const next = [...chapters]
      const [removed] = next.splice(fromIdx, 1)
      next.splice(toIdx, 0, removed)
      setChapters(next)
      if (selectedChapterIdx === fromIdx) setSelectedChapterIdx(toIdx)
      else if (selectedChapterIdx === toIdx) setSelectedChapterIdx(fromIdx)
      pushStepLog(
        direction === 'up'
          ? `📌 Capítulo "${removed.title || fromIdx + 1}" movido para cima.`
          : `📌 Capítulo "${removed.title || fromIdx + 1}" movido para baixo.`,
        'success'
      )
      if (draftPlan) {
        const key = getChapterKey(draftPlan)
        void savePlan({ ...draftPlan, [key]: next })
      }
    },
    [chapters, draftPlan, savePlan, selectedChapterIdx, setChapters, getChapterKey]
  )

  const handleDeleteChapter = async (index: number) => {
    if (!isMock && id) {
      try {
        pushStepLog(`☁️ Removendo capítulo ${index + 1} no backend...`, 'info')
        await endpoints.books.deleteChapter({
          job_id: id,
          chapter_index: index,
          api_key: getApiKey(job) || undefined,
        })
        await refetch(true)
        setSelectedChapterIdx(Math.max(0, index - 1))
        setSelectedSectionIdx(0)
        pushStepLog('✅ Capítulo removido e sincronizado com o backend.', 'success')
        return
      } catch (err) {
        logErrorToStepLogger('Falha ao remover capítulo via endpoint /delete_chapter. Aplicando fallback local', err)
      }
    }

    const updated = chapters.filter((_, idx) => idx !== index)
    updated.forEach((chapter, idx) => (chapter.chapter = idx + 1))
    setChapters(updated)
    setSelectedChapterIdx(Math.max(0, index - 1))
    setSelectedSectionIdx(0)
  }

  const handleDeleteAllChapters = async () => {
    if (!id) return
    if (!window.confirm('Apagar todos os capítulos (e seções) do livro? O plano e metadados serão mantidos. Esta ação não pode ser desfeita.')) return
    try {
      pushStepLog('☁️ Removendo todos os capítulos no backend...', 'info')
      await endpoints.books.deleteAllChapters({ job_id: id })
      await refetch(true)
      setSelectedChapterIdx(0)
      setSelectedSectionIdx(0)
      pushStepLog('✅ Todos os capítulos foram removidos.', 'success')
    } catch (err) {
      logErrorToStepLogger('Falha ao apagar todos os capítulos', err)
    }
  }

  const handleAddSection = () => {
    pushStepLog('➕ Adicionando nova seção manual...', 'info')
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) {
      pushStepLog('⚠️ Capítulo não encontrado para adicionar seção.', 'warning')
      return
    }
    const sections = [...(chapter.sections || [])]
    sections.push({
      title: `Seção ${sections.length + 1}`,
      purpose: '',
      content: '',
      images: [],
      code_blocks: [],
      min_text_length: draftPlan?.default_min_text_length,
      has_source_code: draftPlan?.default_has_source_code ?? false,
    })
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
    setSelectedSectionIdx(sections.length - 1)
    pushStepLog('✅ Seção adicionada manualmente.', 'success')
  }

  const handleAddSectionAI = (sectionTitle?: string) => {
    if (isMock) {
      pushStepLog('⚠️ Criação de seção por IA não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    pushStepLog(sectionTitle ? `🤖 Criando seção: "${sectionTitle}"...` : '🤖 Solicitando nova seção com IA...', 'info')
    setIsPlanningSection(true)
    scheduleHeavyBookWork(async () => {
      try {
        const response = await api.post('/book/plan_section', {
          job_id: id,
          chapter_index: selectedChapterIdx,
          api_key: getApiKey(job) || undefined,
          section_title: sectionTitle || undefined,
        })

        const newSection = response.data?.section
        if (newSection) {
          pushStepLog('📥 Seção recebida da IA. Atualizando lista...', 'info')
          const updated = [...chapters]
          const chapter = updated[selectedChapterIdx]
          if (!chapter) {
            pushStepLog('⚠️ Capítulo não encontrado ao inserir seção IA.', 'warning')
            return
          }
          const sections = [...(chapter.sections || [])]
          sections.push({
            title: newSection.title || `Seção ${sections.length + 1}`,
            purpose: newSection.purpose || newSection.objective || newSection.content_directive || '',
            content_directive: newSection.content_directive || newSection.objective || newSection.purpose || '',
            content: '',
            images: [],
            code_blocks: [],
            min_text_length: draftPlan?.default_min_text_length,
            has_source_code: draftPlan?.default_has_source_code ?? false,
          })
          chapter.sections = sections
          updated[selectedChapterIdx] = chapter
          setChapters(updated)
          setSelectedSectionIdx(sections.length - 1)
          setActiveTab('section')
          pushStepLog('✅ Seção criada com IA e adicionada à lista.', 'success')
        } else {
          pushStepLog('⚠️ IA não retornou uma seção válida.', 'warning')
        }
      } catch (err) {
        console.error('Failed to create AI section:', err)
        logErrorToStepLogger('Erro ao criar seção com IA', err)
      } finally {
        setIsPlanningSection(false)
      }
    })
  }

  const handleAddSectionFromText = () => {
    const text = newSectionFromText.trim()
    if (!text) return
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) {
      pushStepLog('⚠️ Selecione um capítulo para adicionar a seção.', 'warning')
      return
    }
    if (isMock) {
      pushStepLog('⚠️ Criar seção com agente não está disponível no modo mock', 'warning')
      const firstLine = text.split(/\n/)[0]?.trim() || ''
      const title = firstLine.length > 60 ? `${firstLine.slice(0, 57)}...` : firstLine || `Seção ${(chapter.sections?.length ?? 0) + 1}`
      pushStepLog('➕ Adicionando seção a partir do texto...', 'info')
      const sections = [...(chapter.sections || [])]
      sections.push({
        title: title || `Seção ${sections.length + 1}`,
        purpose: '',
        content: text,
        images: [],
        code_blocks: [],
        min_text_length: draftPlan?.default_min_text_length,
        has_source_code: draftPlan?.default_has_source_code ?? false,
      })
      chapter.sections = sections
      updated[selectedChapterIdx] = chapter
      setChapters(updated)
      setSelectedSectionIdx(sections.length - 1)
      setNewSectionFromText('')
      pushStepLog('✅ Seção criada a partir do texto.', 'success')
      return
    }
    if (!id) return
    setIsGeneratingSectionFromPrompt(true)
    pushStepLog('🤖 Gerando seção com agente (contexto do livro e do capítulo)...', 'info')
    scheduleHeavyBookWork(async () => {
      try {
        const res = await api.post('/book/generate_section_from_prompt', {
          job_id: id,
          chapter_index: selectedChapterIdx,
          api_key: getApiKey(job) || undefined,
          section_prompt: text,
        })
        const generated = res.data
        if (!generated || !generated.title) {
          pushStepLog('⚠️ Resposta sem seção válida.', 'warning')
          return
        }
        const sections = [...(chapter.sections || [])]
        sections.push({
          title: generated.title,
          purpose: generated.purpose || '',
          content: generated.content || '',
          images: [],
          code_blocks: [],
          min_text_length: draftPlan?.default_min_text_length,
          has_source_code: draftPlan?.default_has_source_code ?? false,
        })
        chapter.sections = sections
        updated[selectedChapterIdx] = chapter
        setChapters(updated)
        setSelectedSectionIdx(sections.length - 1)
        setNewSectionFromText('')
        pushStepLog(`✅ Seção criada: ${generated.title}`, 'success')
      } catch (err: unknown) {
        console.error('Failed to generate section from prompt:', err)
        logErrorToStepLogger('Erro ao gerar seção com agente', err)
      } finally {
        setIsGeneratingSectionFromPrompt(false)
      }
    })
  }

  /** Cria um capítulo e seções a partir do texto, sem IA: primeira linha = título do capítulo; blocos separados por linha em branco = seções (primeira linha do bloco = título da seção). */
  const handleAddChapterManuallyFromText = () => {
    const text = newChapterFromText.trim()
    if (!text) return
    const lines = text.split(/\n/)
    const firstLine = lines[0]?.trim() || ''
    const chapterTitle = firstLine || `Capítulo ${chapters.length + 1}`
    const rest = lines.slice(1).join('\n').trim()
    const rawBlocks = rest ? rest.split(/\n\s*\n/) : []
    const sections: { title: string; purpose: string; content: string; images: never[]; code_blocks: never[] }[] = []
    if (rawBlocks.length === 0) {
      sections.push({
        title: 'Seção 1',
        purpose: '',
        content: rest || '',
        images: [],
        code_blocks: [],
      })
    } else {
      for (let i = 0; i < rawBlocks.length; i++) {
        const block = rawBlocks[i].trim()
        const blockLines = block.split('\n')
        const sectionTitle = blockLines[0]?.trim() || `Seção ${i + 1}`
        const sectionContent = blockLines.length > 1 ? blockLines.slice(1).join('\n').trim() : block
        sections.push({
          title: sectionTitle,
          purpose: '',
          content: sectionContent,
          images: [],
          code_blocks: [],
        })
      }
    }
    const updated = [...chapters]
    updated.push({
      chapter: updated.length + 1,
      title: chapterTitle,
      purpose: '',
      sections,
      content: '',
    })
    setChapters(updated)
    setSelectedChapterIdx(updated.length - 1)
    setSelectedSectionIdx(0)
    setNewChapterFromText('')
    if (draftPlan) {
      const chKey = getChapterKey(draftPlan)
      void savePlan({ ...draftPlan, [chKey]: updated })
    }
    pushStepLog(`✅ Capítulo criado manualmente: "${chapterTitle}" com ${sections.length} seção(ões).`, 'success')
  }

  const handleAddChapterFromText = () => {
    const text = newChapterFromText.trim()
    if (!text) return
    if (!id) return
    if (isMock) {
      pushStepLog('⚠️ Criar capítulo com agente não está disponível no modo mock', 'warning')
      return
    }
    setIsGeneratingChapterFromPrompt(true)
    pushStepLog('🤖 Gerando capítulo com agente (contexto do livro)...', 'info')
    scheduleHeavyBookWork(async () => {
      try {
        const res = await api.post('/book/generate_chapter_from_prompt', {
          job_id: id,
          api_key: getApiKey(job) || undefined,
          chapter_prompt: text,
        })
        const generated = res.data
        if (!generated || !generated.title) {
          pushStepLog('⚠️ Resposta sem capítulo válido.', 'warning')
          return
        }
        const normalizedSections = (generated.sections || []).map((s: { title?: string; purpose?: string; content?: string; objective?: string; content_directive?: string }) => ({
          title: s.title || 'Seção',
          purpose: s.purpose || s.objective || s.content_directive || '',
          content: s.content || '',
          images: [],
          code_blocks: [],
        }))
        if (normalizedSections.length === 0) {
          normalizedSections.push({ title: 'Seção 1', purpose: '', content: text.slice(0, 2000), images: [], code_blocks: [] })
        }
        const updated = [...chapters]
        updated.push({
          chapter: updated.length + 1,
          title: generated.title,
          purpose: generated.purpose || '',
          sections: normalizedSections,
          content: generated.content || '',
        })
        setChapters(updated)
        setSelectedChapterIdx(updated.length - 1)
        setSelectedSectionIdx(0)
        setNewChapterFromText('')
        if (draftPlan) {
          const chKey = getChapterKey(draftPlan)
          await savePlan({ ...draftPlan, [chKey]: updated })
        }
        pushStepLog(`✅ Capítulo criado: ${generated.title}`, 'success')
      } catch (err: unknown) {
        console.error('Failed to generate chapter from prompt:', err)
        logErrorToStepLogger('Erro ao gerar capítulo com agente', err)
      } finally {
        setIsGeneratingChapterFromPrompt(false)
      }
    })
  }

  const handleDeleteSection = (index: number) => {
    pushStepLog(`🗑️ Removendo seção ${index + 1}...`, 'info')
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) {
      pushStepLog('⚠️ Capítulo não encontrado ao remover seção.', 'warning')
      return
    }
    const sections = [...(chapter.sections || [])]
    sections.splice(index, 1)
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
    setSelectedSectionIdx(Math.max(0, index - 1))
    pushStepLog('✅ Seção removida.', 'success')
  }

  /** Remove seção em um capítulo qualquer (para a lista expandida na aba Capítulos). */
  const deleteSectionAtChapter = (chapterIdx: number, sectionIdx: number) => {
    const ch = chapters[chapterIdx]
    const sections = [...(ch?.sections || [])]
    if (sectionIdx < 0 || sectionIdx >= sections.length) return
    if (sections.length <= 1) return
    if (!window.confirm(`Excluir a seção "${sections[sectionIdx]?.title || `Seção ${sectionIdx + 1}`}"?`)) return
    sections.splice(sectionIdx, 1)
    const updated = [...chapters]
    updated[chapterIdx] = { ...ch, sections }
    setChapters(updated)
    if (selectedChapterIdx === chapterIdx) setSelectedSectionIdx(Math.max(0, sectionIdx - 1))
  }

  /** Remove subseção em um capítulo/seção quaisquer (para a lista expandida na aba Capítulos). */
  const removeSubsectionAt = (chapterIdx: number, sectionIdx: number, subIdx: number) => {
    const updated = [...chapters]
    const ch = updated[chapterIdx]
    const sections = [...(ch?.sections || [])]
    const sec = sections[sectionIdx]
    const subs = (sec?.subsections || []).filter((_, i) => i !== subIdx)
    sections[sectionIdx] = { ...sec, subsections: subs }
    updated[chapterIdx] = { ...ch, sections }
    setChapters(updated)
    if (selectedChapterIdx === chapterIdx && selectedSectionIdx === sectionIdx) setSelectedSubsectionIdx(Math.max(0, (sec?.subsections?.length ?? 1) - 2))
  }

  const handleDeleteAllSectionsInChapter = useCallback(() => {
    const chapter = chapters[selectedChapterIdx]
    const count = chapter?.sections?.length ?? 0
    if (count === 0) return
    if (!window.confirm(`Apagar todas as ${count} seção(ões) deste capítulo?\n\nEsta ação não pode ser desfeita.`)) return
    const updated = [...chapters]
    if (updated[selectedChapterIdx]) updated[selectedChapterIdx] = { ...updated[selectedChapterIdx], sections: [] }
    const totalSections = updated.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
    setChapters(updated)
    setSelectedSectionIdx(0)
    pushStepLog('✅ Todas as seções do capítulo foram removidas.', 'success')
    if (draftPlan) {
      const key = getChapterKey(draftPlan)
      void savePlan({ ...draftPlan, [key]: updated })
    }
    if (totalSections === 0 && id) {
      refetch(true).then(() => {
        window.dispatchEvent(new CustomEvent('book-sections-all-deleted', { detail: { bookId: id } }))
      })
    }
  }, [chapters, selectedChapterIdx, draftPlan, savePlan, id, refetch])

  const handleDeleteAllSectionsInBook = useCallback(async () => {
    const total = chapters.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
    if (total === 0) return
    if (!window.confirm(`Apagar todas as ${total} seção(ões) do livro (todos os capítulos)?\n\nEsta ação não pode ser desfeita.`)) return
    if (!id) return
    pushStepLog('🗑️ Enfileirando remoção de todas as seções...', 'info')
    try {
      const res = await api.post('/book/delete_all_sections', { job_id: id })
      const deleteJobId = (res.data as { job_id?: string })?.job_id
      if (!deleteJobId) {
        pushStepLog('❌ Resposta sem job_id.', 'error')
        return
      }
      pushStepLog('✅ Apagar todas as seções enfileirado. Acompanhe no Histórico.', 'success')
      const poll = async () => {
        try {
          const statusRes = await api.get(`/status/${deleteJobId}`)
          const data = statusRes.data || {}
          if (data.status === 'completed') {
            if (deleteAllSectionsJobPollRef.current) {
              clearInterval(deleteAllSectionsJobPollRef.current)
              deleteAllSectionsJobPollRef.current = null
            }
            pushStepLog('✅ Todas as seções do livro foram removidas.', 'success')
            await refetch(true)
            window.dispatchEvent(new CustomEvent('book-sections-all-deleted', { detail: { bookId: id } }))
            return
          }
          if (data.status === 'failed') {
            if (deleteAllSectionsJobPollRef.current) {
              clearInterval(deleteAllSectionsJobPollRef.current)
              deleteAllSectionsJobPollRef.current = null
            }
            pushStepLog(`❌ Falha: ${(data as { error?: string }).error || 'Erro'}`, 'error')
            return
          }
        } catch {
          /* ignore */
        }
      }
      deleteAllSectionsJobPollRef.current = setInterval(poll, 3000)
      poll()
    } catch (err) {
      console.error('Failed to enqueue delete all sections:', err)
      logErrorToStepLogger('Erro ao enfileirar apagar todas as seções', err)
    }
  }, [chapters, id, refetch])

  const moveSection = (from: number, to: number) => {
    pushStepLog(`↕️ Movendo seção ${from + 1} para posição ${to + 1}...`, 'info')
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) {
      pushStepLog('⚠️ Capítulo não encontrado ao mover seção.', 'warning')
      return
    }
    const sections = [...(chapter.sections || [])]
    if (to < 0 || to >= sections.length) {
      pushStepLog('⚠️ Posição de destino inválida para mover seção.', 'warning')
      return
    }
    const [item] = sections.splice(from, 1)
    sections.splice(to, 0, item)
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
    setSelectedSectionIdx(to)
    pushStepLog('✅ Seção reordenada.', 'success')
  }

  const insertSectionAt = (index: number) => {
    pushStepLog(`➕ Inserindo nova seção na posição ${index + 1}...`, 'info')
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) {
      pushStepLog('⚠️ Capítulo não encontrado ao inserir seção.', 'warning')
      return
    }
    const sections = [...(chapter.sections || [])]
    sections.splice(index, 0, { title: 'Nova Seção', purpose: '', content: '', images: [], code_blocks: [] })
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
    setSelectedSectionIdx(index)
    pushStepLog('✅ Seção inserida.', 'success')
  }

  const clearSectionContent = (index: number) => {
    pushStepLog(`🧹 Limpando conteúdo da seção ${index + 1}...`, 'info')
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    if (!chapter) {
      pushStepLog('⚠️ Capítulo não encontrado ao limpar seção.', 'warning')
      return
    }
    const sections = [...(chapter.sections || [])]
    if (!sections[index]) {
      pushStepLog('⚠️ Seção não encontrada para limpeza.', 'warning')
      return
    }
    sections[index] = { ...sections[index], content: '', images: [], code_blocks: [] }
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
    pushStepLog('✅ Conteúdo da seção limpo.', 'success')
  }

  /** Apaga apenas o texto (content) de todas as seções do livro; mantém títulos, objetivos, imagens, etc. */
  const clearAllSectionsText = () => {
    const total = chapters.reduce((acc, ch) => acc + (ch.sections?.length ?? 0), 0)
    if (total === 0) return
    if (!window.confirm(`Apagar o texto de todas as ${total} seções? Títulos, objetivos e imagens serão mantidos.`)) return
    const updated = chapters.map((ch) => ({
      ...ch,
      sections: (ch.sections || []).map((sec) => ({ ...sec, content: '' })),
    }))
    setChapters(updated)
    if (draftPlan) void savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
    pushStepLog(`✅ Texto de ${total} seção(ões) apagado.`, 'success')
  }

  const handlePlanChapter = async () => {
    if (isMock) {
      pushStepLog('⚠️ Planejamento de capítulo não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    pushStepLog('🧠 Planejando seções do capítulo com IA...', 'info')
    setIsWritingChapter(true)
    setPendingChapterWrite(selectedChapterIdx)
    try {
      await api.post('/plan_chapter', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        num_sections: 5,
        api_key: getApiKey(job) || undefined,
      })
      pushStepLog('✅ Planejamento iniciado. Aguardando atualização...', 'success')
    } catch (err) {
      console.error('Failed to plan chapter sections:', err)
      logErrorToStepLogger('Erro ao planejar as seções', err)
      setIsWritingChapter(false)
      setPendingChapterWrite(null)
    }
  }

  const handleGenerateChaptersAI = async (numChapters: number = 5, numSections: number = 3) => {
    if (isMock) {
      pushStepLog('⚠️ Geração de capítulos não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    pushStepLog('📚 Enfileirando geração de capítulos e seções com IA...', 'info')
    try {
      const authorInsp = draftPlan?.author_inspiration || (metadataAuthorStyles.length > 0 ? metadataAuthorStyles[0] : undefined)
      const res = await api.post('/book/generate_chapters_ai', {
        job_id: id,
        api_key: getApiKey(job) || undefined,
        num_chapters: numChapters,
        num_sections_per_chapter: numSections,
        author_inspiration: authorInsp,
        author_styles: metadataAuthorStyles.length > 0 ? metadataAuthorStyles : undefined,
        book_objective: bookObjective,
        language: draftPlan?.language || undefined,
      })
      const chapterJobId = (res.data as { job_id?: string })?.job_id
      if (!chapterJobId) {
        pushStepLog('❌ Resposta sem job_id.', 'error')
        return
      }
      pushStepLog('✅ Regenerar com IA enfileirado. Acompanhe no Histórico.', 'success')
      const poll = async () => {
        try {
          const statusRes = await api.get(`/status/${chapterJobId}`)
          const data = statusRes.data || {}
          if (data.status === 'completed') {
            if (chapterGenJobPollRef.current) {
              clearInterval(chapterGenJobPollRef.current)
              chapterGenJobPollRef.current = null
            }
            pushStepLog('✅ Capítulos e seções gerados.', 'success')
            await refetch(true)
            return
          }
          if (data.status === 'failed') {
            if (chapterGenJobPollRef.current) {
              clearInterval(chapterGenJobPollRef.current)
              chapterGenJobPollRef.current = null
            }
            pushStepLog(`❌ Falha: ${(data as { error?: string }).error || 'Erro'}`, 'error')
            return
          }
        } catch {
          /* ignore */
        }
      }
      chapterGenJobPollRef.current = setInterval(poll, 3000)
      poll()
    } catch (err) {
      console.error('Failed to enqueue generate chapters:', err)
      logErrorToStepLogger('Erro ao enfileirar geração de capítulos', err)
    }
  }

  const handleReplanBookStyle = async () => {
    if (isMock) {
      pushStepLog('⚠️ Replanejamento não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    const chaptersCount = getChaptersFromPlan(draftPlan).length || 5
    pushStepLog('🧠 Enfileirando replanejamento de capítulos com base no estilo do livro...', 'info')
    try {
      const replanAuthorInsp = draftPlan?.author_inspiration || (draftPlan?.author_styles?.length ? draftPlan.author_styles[0] : undefined)
      const res = await api.post('/book/generate_chapters_ai', {
        job_id: id,
        api_key: getApiKey(job) || undefined,
        num_chapters: chaptersCount,
        num_sections_per_chapter: 3,
        author_inspiration: replanAuthorInsp,
        author_styles: (draftPlan?.author_styles?.length ? draftPlan.author_styles : undefined),
        book_objective: bookObjective,
        language: draftPlan?.language || undefined,
      })
      const chapterJobId = (res.data as { job_id?: string })?.job_id
      if (!chapterJobId) {
        pushStepLog('❌ Resposta sem job_id.', 'error')
        return
      }
      pushStepLog('✅ Replanejamento enfileirado. Acompanhe no Histórico.', 'success')
      const poll = async () => {
        try {
          const statusRes = await api.get(`/status/${chapterJobId}`)
          const data = statusRes.data || {}
          if (data.status === 'completed') {
            if (chapterGenJobPollRef.current) {
              clearInterval(chapterGenJobPollRef.current)
              chapterGenJobPollRef.current = null
            }
            pushStepLog('✅ Replanejamento concluído.', 'success')
            await refetch(true)
            return
          }
          if (data.status === 'failed') {
            if (chapterGenJobPollRef.current) {
              clearInterval(chapterGenJobPollRef.current)
              chapterGenJobPollRef.current = null
            }
            pushStepLog(`❌ Falha: ${(data as { error?: string }).error || 'Erro'}`, 'error')
            return
          }
        } catch {
          /* ignore */
        }
      }
      chapterGenJobPollRef.current = setInterval(poll, 3000)
      poll()
    } catch (err) {
      console.error('Failed to enqueue replan chapters:', err)
      logErrorToStepLogger('Erro ao enfileirar replanejamento', err)
    }
  }

  const handleWriteChapter = async () => {
    if (isMock) {
      pushStepLog('⚠️ Geração de capítulo não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    setIsWritingChapter(true)
    try {
      await savePlan()
      const res = await api.post('/write_chapter', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        api_key: getApiKey(job) || undefined,
      })
      const childJobId = res.data?.job_id
      if (childJobId) setPendingChapterWriteJobId(childJobId)
      setPendingChapterWrite(selectedChapterIdx)
    } catch (err) {
      console.error('Failed to write chapter:', err)
      setIsWritingChapter(false)
      setPendingChapterWrite(null)
      setPendingChapterWriteJobId(null)
      logErrorToStepLogger('Erro ao iniciar a geração do capítulo', err)
    }
  }

  const handleWriteConclusionChapter = async () => {
    if (isMock) {
      pushStepLog('⚠️ Geração de capítulo não está disponível no modo mock', 'warning')
      return
    }
    if (!id || chapters.length === 0) return
    const conclusionIdx = chapters.length - 1
    setIsWritingChapter(true)
    try {
      await savePlan()
      const res = await api.post('/write_chapter', {
        job_id: id,
        chapter_index: conclusionIdx,
        api_key: getApiKey(job) || undefined,
      })
      const childJobId = res.data?.job_id
      if (childJobId) setPendingChapterWriteJobId(childJobId)
      setPendingChapterWrite(conclusionIdx)
      setSelectedChapterIdx(conclusionIdx)
      setSelectedSectionIdx(0)
    } catch (err) {
      console.error('Failed to write conclusion chapter:', err)
      setIsWritingChapter(false)
      setPendingChapterWrite(null)
      setPendingChapterWriteJobId(null)
      logErrorToStepLogger('Erro ao gerar capítulo conclusão', err)
    }
  }

  const handleWriteSection = async (sectionIndex: number) => {
    if (isMock) {
      pushStepLog('⚠️ Geração de seção não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    pushStepLog(`✍️ Solicitando geração da seção ${sectionIndex + 1}...`, 'info')
    setIsWritingSectionIndex(sectionIndex)
    try {
      const res = await api.post('/write_section', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: sectionIndex,
        api_key: getApiKey(job) || undefined,
        writing_style: draftPlan?.default_section_writing_style ?? 'narrative',
      })
      const childJobId = res.data?.job_id
      if (childJobId) setPendingSectionWriteJobId(childJobId)
      setPendingChapterWrite(selectedChapterIdx)
      setIsWritingChapter(true)
      pushStepLog('✅ Escrita da seção iniciada. Aguardando conclusão...', 'success')
    } catch (err) {
      console.error('Failed to write section:', err)
      logErrorToStepLogger('Erro ao iniciar a escrita da seção', err)
    } finally {
      setIsWritingSectionIndex(null)
    }
  }

  const handleWriteSectionAndSave = async (sectionIndex: number) => {
    pushStepLog('💾 Salvando alterações antes de gerar seção...', 'info')
    await savePlan()
    pushStepLog('🚀 Disparando geração da seção com IA...', 'info')
    await handleWriteSection(sectionIndex)
  }

  const allSectionsPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const planAllChaptersPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const planSubsectionsPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const generateSubsectionsTextPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const deleteAllSectionsJobPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const chapterGenJobPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  /** Intervalos iniciados por handlers (regenerar objetivo/conteúdo, etc.); limpos no unmount para evitar vazamento de memória */
  const transientPollIntervalsRef = useRef<Set<ReturnType<typeof setInterval>>>(new Set())
  useEffect(() => {
    return () => {
      transientPollIntervalsRef.current.forEach((t) => clearInterval(t))
      transientPollIntervalsRef.current.clear()
      if (allSectionsPollRef.current) {
        clearInterval(allSectionsPollRef.current)
        allSectionsPollRef.current = null
      }
      if (planAllChaptersPollRef.current) {
        clearInterval(planAllChaptersPollRef.current)
        planAllChaptersPollRef.current = null
      }
      if (planSubsectionsPollRef.current) {
        clearInterval(planSubsectionsPollRef.current)
        planSubsectionsPollRef.current = null
      }
      if (generateSubsectionsTextPollRef.current) {
        clearInterval(generateSubsectionsTextPollRef.current)
        generateSubsectionsTextPollRef.current = null
      }
      setSubsectionTextProgress(null)
      if (deleteAllSectionsJobPollRef.current) {
        clearInterval(deleteAllSectionsJobPollRef.current)
        deleteAllSectionsJobPollRef.current = null
      }
      if (chapterGenJobPollRef.current) {
        clearInterval(chapterGenJobPollRef.current)
        chapterGenJobPollRef.current = null
      }
    }
  }, [])

  const handlePlanAllChaptersSections = async () => {
    if (isMock) {
      setPlanAllChaptersStatus('Indisponível no modo mock.')
      pushStepLog('⚠️ Criar seções (estrutura) não está disponível no modo mock', 'warning')
      return
    }
    if (!id) {
      setPlanAllChaptersStatus('Erro: nenhum livro aberto.')
      pushStepLog('❌ Nenhum livro aberto (id ausente).', 'error')
      return
    }
    if (chapters.length === 0) {
      setPlanAllChaptersStatus(null)
      alert('Não há capítulos. Gere os capítulos antes de criar seções.')
      return
    }
    setPlanAllChaptersStatus('Salvando e enfileirando...')
    setIsPlanningAllChaptersSections(true)
    pushStepLog('📝 Enfileirando criação de seções (estrutura) para todos os capítulos...', 'info')
    try {
      await savePlan()
      const numSections = draftPlan?.num_sections_per_chapter ?? desiredNumSectionsPerChapter
      const res = await api.post('/book/plan_all_chapters', {
        job_id: id,
        api_key: getApiKey(job) || undefined,
        num_sections_per_chapter: numSections,
      })
      const planJobId = (res.data as { job_id?: string })?.job_id
      if (!planJobId) {
        setPlanAllChaptersStatus('Erro: resposta sem job_id.')
        pushStepLog('❌ Resposta sem job_id.', 'error')
        setIsPlanningAllChaptersSections(false)
        return
      }
      setPlanAllChaptersStatus('Enfileirado. Aguardando conclusão...')
      pushStepLog('✅ Planejamento de seções enfileirado. Aguardando...', 'success')
      setIsPlanningAllChaptersSections(false)

      const poll = async () => {
        try {
          const statusRes = await api.get(`/status/${planJobId}`)
          const data = statusRes.data || {}
          if (data.status === 'completed') {
            if (planAllChaptersPollRef.current) {
              clearInterval(planAllChaptersPollRef.current)
              planAllChaptersPollRef.current = null
            }
            const fs = data.final_state as { chapters_planned?: number } | undefined
            const n = fs?.chapters_planned ?? chapters.length
            setPlanAllChaptersStatus(`Concluído: seções criadas para ${n} capítulo(s).`)
            pushStepLog(`✅ Seções (estrutura) criadas para ${n} capítulo(s). Use "Gerar texto de todas as seções" para gerar o conteúdo.`, 'success')
            await refetch(true)
            return
          }
          if (data.status === 'failed') {
            if (planAllChaptersPollRef.current) {
              clearInterval(planAllChaptersPollRef.current)
              planAllChaptersPollRef.current = null
            }
            setPlanAllChaptersStatus(`Falha: ${(data as { error?: string }).error || 'Erro desconhecido'}`)
            pushStepLog(`❌ Job falhou: ${(data as { error?: string }).error || 'Erro'}`, 'error')
            return
          }
        } catch {
          /* ignore */
        }
      }
      planAllChaptersPollRef.current = setInterval(poll, 4000)
      poll()
    } catch (err) {
      console.error('Failed to enqueue plan all chapters:', err)
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      const errStr = msg || (err instanceof Error ? err.message : 'Erro ao enfileirar')
      setPlanAllChaptersStatus(`Erro: ${errStr}`)
      if (msg) pushStepLog(`❌ Servidor: ${msg}`, 'error')
      logErrorToStepLogger('Erro ao enfileirar planejamento de seções', err)
      setIsPlanningAllChaptersSections(false)
    }
  }

  const handleGenerateAllSections = async () => {
    if (isMock) {
      setAllSectionsStatus('Indisponível no modo mock.')
      pushStepLog('⚠️ Gerar texto de todas as seções não está disponível no modo mock', 'warning')
      return
    }
    if (!id) {
      setAllSectionsStatus('Erro: nenhum livro aberto.')
      pushStepLog('❌ Nenhum livro aberto (id ausente).', 'error')
      return
    }
    const totalSections = chapters.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
    if (totalSections === 0) {
      setAllSectionsStatus(null)
      alert('Não há seções. Use "Criar todas as seções do livro" antes para criar a estrutura, depois gere os textos.')
      return
    }
    setAllSectionsStatus('Salvando e enfileirando...')
    setIsGeneratingAllSections(true)
    pushStepLog('📝 Enfileirando geração de texto para seções sem conteúdo...', 'info')
    try {
      await savePlan()
      const res = await api.post('/book/generate_all_sections', {
        job_id: id,
        api_key: getApiKey(job) || undefined,
        min_reading_time: 2,
        regenerate_all: regenerateAllBookSections,
      })
      const sectionsJobId = (res.data as { job_id?: string })?.job_id
      if (!sectionsJobId) {
        setAllSectionsStatus('Erro: resposta sem job_id.')
        pushStepLog('❌ Resposta sem job_id.', 'error')
        setIsGeneratingAllSections(false)
        return
      }
      setAllSectionsStatus('Enfileirado. Acompanhe no Histórico.')
      pushStepLog('✅ Geração de todas as seções enfileirada. Acompanhe no Histórico; as seções aparecem à medida que são criadas.', 'success')
      setIsGeneratingAllSections(false)

      // Poll em background: quando o job terminar, atualizar o livro na tela
      const poll = async () => {
        try {
          const statusRes = await api.get(`/status/${sectionsJobId}`)
          const data = statusRes.data || {}
          if (data.status === 'completed') {
            if (allSectionsPollRef.current) {
              clearInterval(allSectionsPollRef.current)
              allSectionsPollRef.current = null
            }
            const fs = data.final_state as { sections_generated?: number; sections_enqueued?: number } | undefined
            const n = fs?.sections_enqueued ?? fs?.sections_generated ?? 0
            const enqueued = fs?.sections_enqueued != null
            setAllSectionsStatus(enqueued ? `Concluído: ${n} seção(ões) enfileiradas (cada uma em job independente).` : `Concluído: ${n} seção(ões) geradas.`)
            pushStepLog(enqueued ? `✅ ${n} seção(ões) enfileiradas. Acompanhe no Histórico; cada seção usa o output máximo por seção.` : `✅ Job concluído: ${n} seção(ões) com texto gerado.`, 'success')
            await refetch(true)
            return
          }
          if (data.status === 'failed') {
            if (allSectionsPollRef.current) {
              clearInterval(allSectionsPollRef.current)
              allSectionsPollRef.current = null
            }
            setAllSectionsStatus(`Falha: ${(data as { error?: string }).error || 'Erro desconhecido'}`)
            pushStepLog(`❌ Job falhou: ${(data as { error?: string }).error || 'Erro'}`, 'error')
            return
          }
        } catch {
          // ignore network errors during poll
        }
      }
      allSectionsPollRef.current = setInterval(poll, 5000)
      poll()
    } catch (err) {
      console.error('Failed to enqueue generate all sections:', err)
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      const errStr = msg || (err instanceof Error ? err.message : 'Erro ao enfileirar')
      setAllSectionsStatus(`Erro: ${errStr}`)
      if (msg) pushStepLog(`❌ Servidor: ${msg}`, 'error')
      logErrorToStepLogger('Erro ao enfileirar geração de seções', err)
      setIsGeneratingAllSections(false)
    }
  }

  const handleGenerateAllSectionImages = async () => {
    if (isMock) {
      pushStepLog('⚠️ Gerar todas as imagens não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    const totalSections = chapters.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
    if (totalSections === 0) {
      alert('Não há seções. Crie capítulos e seções antes de gerar imagens.')
      return
    }
    pushStepLog(`🖼️ Enfileirando geração de imagens para todas as seções (${totalSections})...`, 'info')
    setIsGeneratingAllSectionImages(true)
    try {
      const res = await api.post('/book/generate_all_section_images', {
        job_id: id,
        api_key: getApiKey(job) || undefined,
        model_name: job?.request_payload?.model_image || 'imagen-4.0-ultra-generate-001',
      })
      const allImagesJobId = res.data?.job_id
      if (!allImagesJobId) {
        pushStepLog('❌ Resposta sem job_id.', 'error')
        setIsGeneratingAllSectionImages(false)
        return
      }
      pushStepLog('✅ Tarefa enfileirada. Aguardando conclusão...', 'success')
      const poll = async () => {
        try {
          const statusRes = await api.get(`/status/${allImagesJobId}`)
          const data = statusRes.data || {}
          if (data.status === 'completed') {
            pushStepLog(`✅ Imagens das seções geradas (${data.final_state?.images_generated ?? totalSections} seções).`, 'success')
            setIsGeneratingAllSectionImages(false)
            await refetch(true)
            return
          }
          if (data.status === 'failed') {
            pushStepLog(`❌ Falha: ${data.error || 'Erro desconhecido'}`, 'error')
            setIsGeneratingAllSectionImages(false)
            return
          }
          setTimeout(poll, 5000)
        } catch (e) {
          pushStepLog('Erro ao verificar status.', 'error')
          setIsGeneratingAllSectionImages(false)
        }
      }
      setTimeout(poll, 5000)
    } catch (err) {
      console.error('Failed to generate all section images:', err)
      logErrorToStepLogger('Erro ao enfileirar geração de imagens', err)
      setIsGeneratingAllSectionImages(false)
    }
  }

  const buildChapterDividerPromptByIndex = useCallback((chapterIndex: number): string => {
    const chapterTitle = String(chapters[chapterIndex]?.title || `Capítulo ${chapterIndex + 1}`).trim()
    const subject = String(bookObjective || draftPlan?.objective || draftPlan?.title || job?.topic || 'tema geral').trim()
    const title = String(draftPlan?.title || job?.topic || 'Untitled').trim()
    const subtitle = String(draftPlan?.subtitle || '').trim()
    const author = String(draftPlan?.author || draftPlan?.author_inspiration || '').trim()
    const bestSellers = draftPlan?.cover_designer_styles || []
    const suffix = bestSellers.length > 0 ? ` Inspirações de capa: ${bestSellers.join(', ')}.` : ''
    return `Crie um divisor visual para "${chapterTitle}" do livro "${title}"${subtitle ? `: ${subtitle}` : ''}${author ? `, por ${author}` : ''} sobre "${subject}". Estilo editorial consistente, alto impacto visual, composição elegante.${suffix}`
  }, [bookObjective, chapters, draftPlan, job?.topic])

  const handleGenerateOneImagePerChapter = async () => {
    if (isMock) {
      pushStepLog('⚠️ Gerar 1 imagem por capítulo não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    if (chapters.length === 0) {
      alert('Não há capítulos. Crie capítulos antes de gerar imagens.')
      return
    }

    setIsGeneratingOneImagePerChapter(true)
    pushStepLog(`🖼️ Gerando 1 divisor por capítulo (${chapters.length})...`, 'info')
    try {
      const jobIds: string[] = []
      let directCount = 0

      for (let chIdx = 0; chIdx < chapters.length; chIdx += 1) {
        const prompt = buildChapterDividerPromptByIndex(chIdx)
        const res = await api.post('/book/generate_cover', {
          job_id: id,
          prompt,
          api_key: getApiKey(job) || undefined,
          target: 'chapter',
          chapter_index: chIdx,
          model_name: coverModel,
        })
        const subJobId = res.data?.job_id
        const directPath = res.data?.file_path || res.data?.image_path
        if (subJobId) {
          jobIds.push(subJobId)
        } else if (directPath) {
          directCount += 1
        }
      }

      if (!jobIds.length) {
        pushStepLog(`✅ Divisores gerados para ${directCount || chapters.length} capítulo(s).`, 'success')
        await refetch(true)
        return
      }

      let completed = 0
      let failed = 0
      const maxAttempts = 180
      for (let attempt = 0; attempt < maxAttempts && completed + failed < jobIds.length; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 3000))
        for (const subJobId of jobIds) {
          if (!subJobId) continue
          try {
            const statusRes = await api.get(`/status/${subJobId}`)
            const status = statusRes.data?.status
            if (status === 'completed') {
              completed += 1
              const idx = jobIds.indexOf(subJobId)
              if (idx >= 0) jobIds[idx] = ''
            } else if (status === 'failed') {
              failed += 1
              const idx = jobIds.indexOf(subJobId)
              if (idx >= 0) jobIds[idx] = ''
            }
          } catch {
            // ignore polling errors for this iteration
          }
        }
      }

      const totalSuccess = directCount + completed
      if (totalSuccess > 0) {
        pushStepLog(`✅ Divisores gerados para ${totalSuccess} capítulo(s).`, 'success')
      }
      if (failed > 0) {
        pushStepLog(`⚠️ ${failed} capítulo(s) falharam ao gerar divisor.`, 'warning')
      }
      await refetch(true)
    } catch (err) {
      console.error('Failed to generate one chapter image per chapter:', err)
      logErrorToStepLogger('Erro ao gerar divisores por capítulo', err)
    } finally {
      setIsGeneratingOneImagePerChapter(false)
    }
  }

  const handleDeleteAllSectionImages = async () => {
    if (isMock) {
      pushStepLog('⚠️ Apagar todas as imagens não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    if (!window.confirm('Remover todas as imagens de todas as seções do livro? Esta ação não pode ser desfeita.')) return
    setIsDeletingAllSectionImages(true)
    try {
      const res = await api.post('/book/delete_all_section_images', { job_id: id })
      const removed = res.data?.removed_count ?? 0
      pushStepLog(`✅ Imagens removidas. (${removed} arquivo(s) apagado(s).)`, 'success')
      await refetch(true)
    } catch (err) {
      console.error('Failed to delete all section images:', err)
      logErrorToStepLogger('Erro ao apagar imagens', err)
    } finally {
      setIsDeletingAllSectionImages(false)
    }
  }

  const runReduceToOneImagePromptPerChapter = useCallback(async (): Promise<{ ok: boolean; data?: any; error?: string }> => {
    if (!id) return { ok: false, error: 'ID do livro ausente.' }
    const res = await api.post('/book/reduce_to_one_image_prompt_per_chapter_queued', {
      job_id: id,
      api_key: getApiKey(job) || undefined,
    })
    const reduceJobId = res.data?.job_id
    if (!reduceJobId) return { ok: false, error: 'Resposta sem job_id ao enfileirar redução.' }

    pushStepLog('✅ Redução enfileirada. Aguardando conclusão para continuar...', 'success')
    const maxAttempts = 180
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5000))
      const statusRes = await api.get(`/status/${reduceJobId}`)
      const data = statusRes.data || {}
      if (data.status === 'completed') return { ok: true, data }
      if (data.status === 'failed') return { ok: false, error: data.error || 'Erro desconhecido' }
    }
    return { ok: false, error: 'Tempo limite aguardando conclusão da redução.' }
  }, [id, job, pushStepLog])

  const handleReduceToOneImagePerChapter = async () => {
    if (isMock) {
      pushStepLog('⚠️ Reduzir instruções de imagem não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    if (!window.confirm('Deixar apenas 1 instrução de geração de imagem por capítulo (```image_prompt/```imagem)? As demais serão convertidas em texto e o texto das seções será reescrito para não referenciar a imagem removida.')) return
    setIsReducingToOneImagePerChapter(true)
    try {
      pushStepLog('🧹 Enfileirando redução para 1 instrução de imagem por capítulo...', 'info')
      const result = await runReduceToOneImagePromptPerChapter()
      if (!result.ok) {
        pushStepLog(`❌ Falha na redução: ${result.error || 'Erro desconhecido'}`, 'error')
        return
      }
      const replaced = result.data?.final_state?.blocks_replaced ?? 0
      const kept = result.data?.final_state?.chapters_with_instruction ?? 0
      const rewritten = result.data?.final_state?.sections_rewritten ?? 0
      let msg = `✅ Redução aplicada: ${kept} capítulo(s) com 1 instrução de imagem; ${replaced} bloco(s) convertido(s) em texto.`
      if (rewritten > 0) msg += ` ${rewritten} seção(ões) reescrita(s) sem referência à imagem.`
      pushStepLog(msg, 'success')
      await refetch(true)
    } catch (err) {
      console.error('Failed to reduce to one image prompt per chapter:', err)
      logErrorToStepLogger('Erro ao reduzir instruções de imagem', err)
    } finally {
      setIsReducingToOneImagePerChapter(false)
    }
  }

  const handleReplanObjectives = async () => {
    if (isMock) {
      pushStepLog('⚠️ Replanejamento de objetivos não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    pushStepLog('🎯 Enfileirando replanejamento de objetivos...', 'info')
    setIsReplanningObjectives(true)
    try {
      await savePlan()
      const res = await api.post<{ status: string; job_id?: string }>('/book/replan_objectives', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        api_key: getApiKey(job) || undefined,
      })
      const jobId = res.data?.job_id
      if (jobId) {
        pushStepLog('Replanejamento enfileirado. Acompanhe no Histórico; pode continuar usando a tela.', 'success')
        const t = setInterval(async () => {
          try {
            const statusRes = await api.get(`/status/${jobId}`).catch(() => null)
            const status = statusRes?.data?.status
            if (status === 'completed' || status === 'failed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
              await refetch(true)
            }
          } catch {
            /* ignore */
          }
        }, 2500)
        transientPollIntervalsRef.current.add(t)
      }
    } catch (err) {
      console.error('Failed to replan objectives:', err)
      logErrorToStepLogger('Erro ao replanejar objetivos', err)
    } finally {
      setIsReplanningObjectives(false)
    }
  }

  const handleRewriteSection = async (sectionIndex: number) => {
    if (isMock) {
      pushStepLog('⚠️ Reescrita de seção não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    const section = currentSections[sectionIndex]
    if (!section) {
      pushStepLog('⚠️ Seção não encontrada.', 'warning')
      return
    }
    if (!section.content?.trim()) {
      pushStepLog('📝 Seção sem conteúdo. Gerando texto com IA...', 'info')
      await handleWriteSectionAndSave(sectionIndex)
      return
    }
    pushStepLog(`🔄 Enfileirando reescrita da seção ${sectionIndex + 1}...`, 'info')
    setIsRewritingSectionIndex(sectionIndex)
    try {
      await savePlan()
      const res = await api.post<{ status: string; job_id?: string }>('/book/rewrite_section', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        num_sections: sectionIndex,
        api_key: getApiKey(job) || undefined,
      })
      const jobId = res.data?.job_id
      if (jobId) {
        pushStepLog('Reescrita enfileirada. Acompanhe no Histórico; pode continuar usando a tela.', 'success')
        const t = setInterval(async () => {
          try {
            const statusRes = await api.get(`/status/${jobId}`).catch(() => null)
            const status = statusRes?.data?.status
            if (status === 'completed' || status === 'failed') {
              transientPollIntervalsRef.current.delete(t)
              clearInterval(t)
              await refetch(true)
            }
          } catch {
            /* ignore */
          }
        }, 2500)
        transientPollIntervalsRef.current.add(t)
      }
    } catch (err) {
      console.error('Failed to rewrite section:', err)
      logErrorToStepLogger('Erro ao reescrever seção', err)
    } finally {
      setIsRewritingSectionIndex(null)
    }
  }

  const sectionChatTools = useMemo<UnifiedChatAction[]>(
    () => [
      {
        id: 'criar-capitulo',
        label: 'Criar Capítulo',
        description: 'Adiciona um novo capítulo ao livro',
        endpoint: '/add_chapter',
        keywords: ['criar capitulo', 'novo capitulo', 'adicionar capitulo'],
        run: () => {
          void handleAddChapter()
          return 'Capítulo criado.'
        },
      },
      {
        id: 'deletar-capitulo',
        label: 'Deletar Capítulo',
        description: 'Remove um capítulo (informe o número)',
        endpoint: '/delete_chapter',
        keywords: ['deletar capitulo', 'excluir capitulo', 'remover capitulo'],
        example: '/deletar-capitulo 2',
        run: ({ numbers }) => {
          const target = numbers[0] ? numbers[0] - 1 : selectedChapterIdx
          if (target < 0 || target >= chapters.length) {
            return 'Número de capítulo inválido.'
          }
          void handleDeleteChapter(target)
          return `Capítulo ${target + 1} removido.`
        },
      },
      {
        id: 'replanejar-capitulo',
        label: 'Replanejar Capítulo',
        description: 'Replaneja as seções do capítulo atual',
        endpoint: '/plan_chapter',
        keywords: ['replanejar capitulo', 'planejar capitulo'],
        run: () => {
          handlePlanChapter()
          return 'Replanejamento do capítulo iniciado.'
        },
      },
      {
        id: 'criar-secao',
        label: 'Criar Seção',
        description: 'Cria nova seção (use "ia" para gerar, ou informe o nome)',
        endpoint: '/book/plan_section',
        keywords: ['criar secao', 'nova secao', 'adicionar secao'],
        example: '/criar-secao ia: Introdução ao tema',
        run: ({ text }) => {
          // Parse the text to extract section name
          // Formats supported:
          // - /criar-secao ia: Nome da Seção
          // - /criar-secao ia Nome da Seção
          // - /criar-secao Nome da Seção (manual, no AI)
          const cleaned = text.replace(/^[^\s]+\s*/i, '').trim()

          if (cleaned.toLowerCase().startsWith('ia')) {
            // Remove "ia" prefix and any separator (: or space)
            let sectionName = cleaned.replace(/^ia[\s:]+/i, '').trim()
            if (sectionName) {
              void handleAddSectionAI(sectionName)
              return `Criando seção com IA: "${sectionName}"`
            } else {
              void handleAddSectionAI()
              return 'Solicitei criação de seção com IA.'
            }
          }

          // If no "ia" keyword but has text, treat as section title for manual creation
          if (cleaned) {
            handleAddSection()
            // Update the last section's title
            const updated = [...chapters]
            const chapter = updated[selectedChapterIdx]
            if (chapter && chapter.sections && chapter.sections.length > 0) {
              const lastIdx = chapter.sections.length - 1
              chapter.sections[lastIdx].title = cleaned
              setChapters(updated)
            }
            return `Seção "${cleaned}" criada.`
          }

          handleAddSection()
          return 'Seção criada.'
        },
      },

      {
        id: 'deletar-secao',
        label: 'Deletar Seção',
        description: 'Remove uma seção (informe o número)',
        endpoint: 'local',
        keywords: ['deletar secao', 'excluir secao', 'remover secao'],
        example: '/deletar-secao 3',
        run: ({ numbers }) => {
          const target = numbers[0] ? numbers[0] - 1 : selectedSectionIdx
          if (target < 0 || target >= currentSections.length) {
            return 'Número de seção inválido.'
          }
          handleDeleteSection(target)
          return `Seção ${target + 1} removida.`
        },
      },
      {
        id: 'replanejar-secoes',
        label: 'Replanejar Seções',
        description: 'Replaneja objetivos das seções do capítulo',
        endpoint: '/book/replan_objectives',
        keywords: ['replanejar secoes', 'replanejar objetivos'],
        run: () => {
          handleReplanObjectives()
          return 'Replanejamento de objetivos iniciado.'
        },
      },
      {
        id: 'replanejar-secao',
        label: 'Replanejar Seção',
        description: 'Reescreve a seção atual com IA',
        endpoint: '/book/rewrite_section',
        keywords: ['replanejar secao', 'reescrever secao'],
        run: ({ numbers }) => {
          const target = numbers[0] ? numbers[0] - 1 : selectedSectionIdx
          if (target < 0 || target >= currentSections.length) {
            return 'Número de seção inválido.'
          }
          handleRewriteSection(target)
          return `Replanejamento da seção ${target + 1} iniciado.`
        },
      },
      {
        id: 'definir-objetivo-secao',
        label: 'Definir Objetivo da Seção',
        description: 'Atualiza o objetivo da seção (informe o número)',
        endpoint: '/jobs/{id}/update + /books/{id}',
        keywords: ['definir objetivo', 'objetivo da secao', 'objetivo seção'],
        example: '/definir-objetivo-secao 2: explicar conceitos-chave',
        run: ({ text, numbers }) => {
          const target = numbers[0] ? numbers[0] - 1 : selectedSectionIdx
          if (target < 0 || target >= currentSections.length) {
            return 'Número de seção inválido.'
          }
          const cleaned = text.replace(/^[^\s]+\s*/i, '').trim()
          const objective = cleaned.includes(':') ? cleaned.split(':').slice(1).join(':').trim() : cleaned
          if (!objective) return 'Informe o objetivo após o comando.'
          updateSectionAtIndex(target, {
            purpose: objective,
            content_directive: objective,
            objective,
          })
          void savePlan()
          return `Objetivo da seção ${target + 1} atualizado.`
        },
      },
      createDeepResearchTool(id),
    ],
    [
      chapters.length,
      currentSections.length,
      handleAddChapter,
      handleAddSection,
      handleAddSectionAI,
      handleDeleteChapter,
      handleDeleteSection,
      handlePlanChapter,
      handleReplanObjectives,
      handleRewriteSection,
      selectedChapterIdx,
      selectedSectionIdx,
      savePlan,
      updateSectionAtIndex,
    ]
  )

  const handleGenerateSectionObjective = useCallback(async () => {
    if (isMock) {
      pushStepLog('⚠️ Gerar objetivo não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    setIsGeneratingSectionObjective(true)
    pushStepLog('🎯 Gerando objetivo da seção com IA...', 'info')
    try {
      const response = await api.post('/book/generate_section_objective', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        api_key: getApiKey(job) || undefined,
      })
      const objective = response.data?.objective
      if (objective) {
        const updated = [...chapters]
        const chapter = updated[selectedChapterIdx]
        if (chapter) {
          const sections = [...(chapter.sections || [])]
          const sec = sections[selectedSectionIdx]
          if (sec) {
            sections[selectedSectionIdx] = {
              ...sec,
              purpose: objective,
              content_directive: objective,
            }
            chapter.sections = sections
            updated[selectedChapterIdx] = chapter
            setChapters(updated)
            pushStepLog('✅ Objetivo da seção gerado e preenchido.', 'success')
          }
        }
      } else {
        pushStepLog('⚠️ Resposta sem objetivo.', 'warning')
      }
    } catch (err) {
      console.error('Failed to generate section objective:', err)
      logErrorToStepLogger('Erro ao gerar objetivo da seção', err)
    } finally {
      setIsGeneratingSectionObjective(false)
    }
  }, [chapters, id, job, selectedChapterIdx, selectedSectionIdx, getApiKey, pushStepLog, logErrorToStepLogger])

  const handlePlanEpubSection = async (sectionIndex: number) => {
    if (isMock) {
      pushStepLog('⚠️ Planejamento de EPUB não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    const section = currentSections[sectionIndex]
    if (!section) {
      pushStepLog('⚠️ Seção não encontrada.', 'warning')
      return
    }
    if (!section.content || !section.content.trim()) {
      pushStepLog('⚠️ Seção sem conteúdo para planejar imagens.', 'warning')
      return
    }
    const hasSectionImages = (section.images?.length ?? 0) > 0 || !!(section as { image_path?: string }).image_path
    if (!hasSectionImages) {
      pushStepLog('⚠️ Seção sem imagens para planejar.', 'warning')
      return
    }
    setIsPlanningEpub(true)
    try {
      const response = await api.post<{ job_id?: string; message?: string }>('/book/plan_epub_section_queue', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: sectionIndex,
        api_key: getApiKey(job) || undefined,
      })
      pushStepLog(response.data?.message || 'Planejamento EPUB enfileirado. Acompanhe no Histórico.', 'success')
    } catch (err) {
      console.error('Failed to plan EPUB section:', err)
      logErrorToStepLogger('Erro ao planejar EPUB da seção', err)
    } finally {
      setIsPlanningEpub(false)
    }
  }

  function getEffectiveSectionIdx(sectionIndex?: number) { return sectionIndex ?? selectedSectionIdx }
  const handlePlanSubsections = async (sectionIndex?: number) => {
    const effIdx = getEffectiveSectionIdx(sectionIndex)
    const section = currentSections[effIdx]
    if (!id || section === undefined) return
    if ((section?.subsections?.length ?? 0) > 0) {
      pushStepLog('Esta seção já tem subseções. Use "Apagar subseções" se quiser gerar de novo.', 'info')
      return
    }
    setIsPlanningSubsections(true)
    let startedPolling = false
    try {
      const res = await api.post<{ subsections?: BookSubsection[]; status?: string; job_id?: string }>('/book/plan_subsections', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: effIdx,
        api_key: getApiKey(job) || undefined,
        ...(section.num_subsections_per_section || draftPlan?.default_num_subsections_per_section ? { num_subsections: section.num_subsections_per_section || draftPlan?.default_num_subsections_per_section } : {}),
        ...(section.has_source_code != null ? { has_source_code: section.has_source_code } : draftPlan?.default_has_source_code != null ? { has_source_code: draftPlan.default_has_source_code } : {}),
      })
      const data = res.data || {}
      if (data.status === 'queued' && data.job_id) {
        startedPolling = true
        pushStepLog('Criando subseções em background...', 'info')
        const subsectionsJobId = data.job_id
        const poll = async () => {
          try {
            const statusRes = await api.get(`/status/${subsectionsJobId}`)
            const jobData = statusRes.data || {}
            if (jobData.status === 'completed') {
              if (planSubsectionsPollRef.current) {
                clearInterval(planSubsectionsPollRef.current)
                planSubsectionsPollRef.current = null
              }
              const result = jobData.result as { subsections?: unknown[]; text_job_ids?: string[] } | undefined
              const count = result?.subsections?.length ?? 0
              pushStepLog(`✅ ${count} subseções criadas.`, 'success')
              const textJobIds = result?.text_job_ids ?? []
              if (textJobIds.length > 0) {
                setSubsectionTextProgress({ jobIds: textJobIds, completed: 0, failed: 0 })
                pushStepLog(`${textJobIds.length} job(s) de texto em background. Acompanhe pela barra abaixo; pode continuar usando a tela.`, 'info')
              }
              await refetch(true)
              setIsPlanningSubsections(false)
              return
            }
            if (jobData.status === 'failed') {
              if (planSubsectionsPollRef.current) {
                clearInterval(planSubsectionsPollRef.current)
                planSubsectionsPollRef.current = null
              }
              pushStepLog(`❌ Falha: ${(jobData as { error?: string }).error || 'Erro ao criar subseções'}`, 'error')
              setIsPlanningSubsections(false)
              return
            }
          } catch {
            /* ignore */
          }
        }
        planSubsectionsPollRef.current = setInterval(poll, 3000)
        poll()
        setIsPlanningSubsections(false)
        return
      }
      const list = data.subsections || []
      updateSectionAtIndex(effIdx, { subsections: list })
      pushStepLog(`✅ ${list.length} subseções planejadas.`, 'success')
      await savePlan()
    } catch (err) {
      console.error('Plan subsections:', err)
      logErrorToStepLogger('Erro ao planejar subseções', err)
    } finally {
      if (!startedPolling) setIsPlanningSubsections(false)
    }
  }

  const handleGenerateSubsectionsText = async (sectionIndex?: number) => {
    const effIdx = getEffectiveSectionIdx(sectionIndex)
    const section = currentSections[effIdx]
    if (!id || section === undefined) return
    const subs = section?.subsections || []
    if (!subs.length) {
      pushStepLog('⚠️ Defina subseções antes (Gerar subseções).', 'warning')
      return
    }
    setIsGeneratingSubsectionsText(true)
    let startedPolling = false
    try {
      const res = await api.post<{ subsections?: BookSubsection[]; message?: string; status?: string; job_id?: string; job_ids?: string[] }>('/book/generate_subsections_text', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: effIdx,
        api_key: getApiKey(job) || undefined,
      })
      const data = res.data || {}
      if (data.status === 'queued' && (data.job_ids?.length || data.job_id)) {
        startedPolling = true
        const jobIds = data.job_ids?.length ? data.job_ids : (data.job_id ? [data.job_id] : [])
        setSubsectionTextProgress({ jobIds, completed: 0, failed: 0 })
        pushStepLog(
          jobIds.length > 1
            ? `${jobIds.length} jobs enfileirados para gerar texto de cada subseção. Acompanhe o progresso abaixo.`
            : 'Gerando texto da subseção em background...',
          'info'
        )
        const poll = async () => {
          try {
            const statuses = await Promise.all(jobIds.map((jid) => api.get(`/status/${jid}`).then((r) => r.data?.status as string)))
            const completedCount = statuses.filter((s) => s === 'completed').length
            const failedCount = statuses.filter((s) => s === 'failed').length
            setSubsectionTextProgress((prev) => (prev ? { ...prev, completed: completedCount, failed: failedCount } : null))
            const allDone = statuses.every((s) => s === 'completed' || s === 'failed')
            if (allDone) {
              if (generateSubsectionsTextPollRef.current) {
                clearInterval(generateSubsectionsTextPollRef.current)
                generateSubsectionsTextPollRef.current = null
              }
              setSubsectionTextProgress(null)
              await refetch(true)
              if (failedCount > 0) {
                pushStepLog(`✅ ${completedCount} de ${jobIds.length} texto(s) gerado(s). ${failedCount} falharam. Regerar a seção para tentar novamente.`, failedCount === jobIds.length ? 'error' : 'warning')
              } else {
                pushStepLog(`✅ Texto de ${jobIds.length} subseção(ões) gerado(s).`, 'success')
              }
              setIsGeneratingSubsectionsText(false)
              return
            }
          } catch {
            /* ignore */
          }
        }
        generateSubsectionsTextPollRef.current = setInterval(poll, 3000)
        poll()
        setIsGeneratingSubsectionsText(false)
        return
      }
      if (data.status === 'ok' && data.message) pushStepLog(data.message, 'info')
      if (data.status === 'error' && data.message) pushStepLog(data.message, 'error')
      const list = data.subsections || []
      if (list.length) {
        updateSectionAtIndex(effIdx, { subsections: list })
        pushStepLog('✅ Textos das subseções gerados.', 'success')
        await savePlan()
      }
    } catch (err) {
      console.error('Generate subsections text:', err)
      logErrorToStepLogger('Erro ao gerar textos das subseções', err)
    } finally {
      if (!startedPolling) setIsGeneratingSubsectionsText(false)
    }
  }

  const handleRenderCharts = useCallback(async () => {
    if (!id || isMock) return
    setIsRenderingCharts(true)
    try {
      const res = await api.post<{ updated?: boolean; book_plan?: BookPlan; message?: string }>('/book/render_charts', { job_id: id })
      const data = res.data || {}
      if (data.updated && data.book_plan) {
        setDraftPlan(data.book_plan)
        pushStepLog('✅ Gráficos gerados: código substituído por imagens nas seções e subseções.', 'success')
      } else {
        pushStepLog(data.message || 'Nenhum gráfico encontrado para renderizar.', 'info')
      }
    } catch (err) {
      console.error('Render charts:', err)
      logErrorToStepLogger('Erro ao montar gráficos', err)
    } finally {
      setIsRenderingCharts(false)
    }
  }, [id, isMock, pushStepLog, logErrorToStepLogger])

  const handleRenderImagePrompts = useCallback(async () => {
    if (!id || isMock) return
    const apiKey = getApiKey(job) || getStoredGeminiApiKey()
    if (!apiKey) {
      pushStepLog('⚠️ Configure uma API key (Gemini/Imagen) para gerar imagens a partir dos prompts.', 'warning')
      return
    }
    setIsRenderingImagePrompts(true)
    try {
      const res = await api.post<{ updated?: boolean; book_plan?: BookPlan; message?: string }>('/book/render_image_prompts', { job_id: id, api_key: apiKey })
      const data = res.data || {}
      if (data.updated && data.book_plan) {
        setDraftPlan(data.book_plan)
        pushStepLog('✅ Instruções de imagem substituídas pelas imagens geradas.', 'success')
      } else {
        pushStepLog(data.message || 'Nenhum bloco de prompt de imagem encontrado.', 'info')
      }
    } catch (err) {
      console.error('Render image prompts:', err)
      logErrorToStepLogger('Erro ao substituir prompts por imagens', err)
    } finally {
      setIsRenderingImagePrompts(false)
    }
  }, [id, isMock, job, pushStepLog, logErrorToStepLogger])

  const subsectionSaveDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const SUBSECTION_SAVE_DEBOUNCE_MS = 1200
  useEffect(() => () => {
    if (subsectionSaveDebounceRef.current) clearTimeout(subsectionSaveDebounceRef.current)
  }, [])

  function updateSubsectionAtIndex(sectionIdx: number | undefined, subIdx: number, patch: Partial<BookSubsection>) {
    const effIdx = getEffectiveSectionIdx(sectionIdx)
    const section = currentSections[effIdx]
    const subs = [...(section?.subsections || [])]
    if (subIdx < 0 || subIdx >= subs.length) return
    subs[subIdx] = { ...subs[subIdx], ...patch }
    updateSectionAtIndex(effIdx, { subsections: subs })
    if (subsectionSaveDebounceRef.current) clearTimeout(subsectionSaveDebounceRef.current)
    subsectionSaveDebounceRef.current = setTimeout(() => {
      subsectionSaveDebounceRef.current = null
      void savePlan()
    }, SUBSECTION_SAVE_DEBOUNCE_MS)
  }

  function addSubsection(sectionIndex?: number) {
    const effIdx = getEffectiveSectionIdx(sectionIndex)
    const section = currentSections[effIdx]
    const subs = [...(section?.subsections || [])]
    subs.push({ objective: '', content: '' })
    updateSectionAtIndex(effIdx, { subsections: subs })
    const chKey = getChapterKey(draftPlan)
    const updatedChapters = chapters.map((ch, cIdx) => {
      if (cIdx !== selectedChapterIdx) return ch
      const secs = (ch.sections || []).map((s, sIdx) => (sIdx === effIdx ? { ...s, subsections: subs } : s))
      return { ...ch, sections: secs }
    })
    if (draftPlan) void savePlan({ ...draftPlan, [chKey]: updatedChapters })
  }

  const removeSubsection = (sectionIdx: number | undefined, subIdx: number) => {
    const effIdx = getEffectiveSectionIdx(sectionIdx)
    const section = currentSections[effIdx]
    const subs = (section?.subsections || []).filter((_, i) => i !== subIdx)
    updateSectionAtIndex(effIdx, { subsections: subs })
    const chKey = getChapterKey(draftPlan)
    const updatedChapters = chapters.map((ch, cIdx) => {
      if (cIdx !== selectedChapterIdx) return ch
      const secs = (ch.sections || []).map((s, sIdx) => (sIdx === effIdx ? { ...s, subsections: subs } : s))
      return { ...ch, sections: secs }
    })
    if (draftPlan) void savePlan({ ...draftPlan, [chKey]: updatedChapters })
  }

  const [isPlanningAllSubsections, setIsPlanningAllSubsections] = useState(false)
  const [isGeneratingAllSubsectionsText, setIsGeneratingAllSubsectionsText] = useState(false)
  const [planSubsectionsEnqueueProgress, setPlanSubsectionsEnqueueProgress] = useState<{ current: number; total: number } | null>(null)
  /** Progresso dos jobs de planejamento de subseções (X/Y concluídos) enquanto aguardamos. */
  const [planSubsectionsJobProgress, setPlanSubsectionsJobProgress] = useState<{ jobIds: string[]; completed: number } | null>(null)

  const handlePlanAllSubsectionsInChapter = async () => {
    if (!id || currentSections.length === 0) return
    const toEnqueue = currentSections.filter((s) => !(s?.subsections?.length ?? 0)).length
    if (toEnqueue === 0) {
      pushStepLog(`Todas as ${currentSections.length} seções do capítulo já têm subseções.`, 'success')
      return
    }
    setIsPlanningAllSubsections(true)
    try {
      const res = await api.post<{ status?: string; job_id?: string }>('/book/plan_all_subsections_chapter_queued', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: 0,
        api_key: getApiKey(job) || undefined,
      })
      const data = res.data || {}
      if (data.status === 'queued' && data.job_id) {
        pushStepLog(`Planejamento de subseções do capítulo enfileirado (job ${data.job_id}). Em background. Pode sair da página; acompanhe no Histórico.`, 'success')
      } else {
        pushStepLog('Planejamento de subseções do capítulo enfileirado.', 'info')
      }
    } catch (err) {
      console.error('Plan all subsections (chapter):', err)
      logErrorToStepLogger('Erro ao planejar subseções do capítulo', err)
    } finally {
      setIsPlanningAllSubsections(false)
    }
  }

  const handleGenerateAllSubsectionsTextInChapter = async () => {
    if (!id || currentSections.length === 0) return
    const withSubsections = currentSections.filter((s) => (s?.subsections?.length ?? 0) > 0).length
    if (withSubsections === 0) {
      pushStepLog('⚠️ Nenhuma seção tem subseções. Use "Gerar todas as subseções" antes.', 'warning')
      return
    }
    setIsGeneratingAllSubsectionsText(true)
    try {
      const res = await api.post<{ status?: string; job_id?: string }>('/book/generate_all_subsections_text_chapter_queued', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: 0,
        api_key: getApiKey(job) || undefined,
      })
      const data = res.data || {}
      if (data.status === 'queued' && data.job_id) {
        pushStepLog(`Geração de texto das subseções do capítulo enfileirada (job ${data.job_id}). Em background. Pode sair da página; acompanhe no Histórico.`, 'success')
      } else {
        pushStepLog('Geração de texto das subseções do capítulo enfileirada.', 'info')
      }
    } catch (err) {
      console.error('Generate all subsections text (chapter):', err)
      logErrorToStepLogger('Erro ao gerar textos das subseções do capítulo', err)
    } finally {
      setIsGeneratingAllSubsectionsText(false)
    }
  }

  const handleClearAllSubsectionsInChapter = () => {
    if (currentSections.length === 0) return
    const total = currentSections.reduce((acc, s) => acc + (s?.subsections?.length ?? 0), 0)
    if (total === 0) {
      pushStepLog('Nenhuma subseção para apagar neste capítulo.', 'info')
      return
    }
    if (!window.confirm(`Apagar todas as subseções do capítulo? (${total} subseção(ões) em ${currentSections.length} seção(ões))`)) return
    const updated = [...chapters]
    const ch = updated[selectedChapterIdx]
    if (!ch?.sections) return
    ch.sections = ch.sections.map((sec) => ({ ...sec, subsections: [] }))
    updated[selectedChapterIdx] = ch
    setChapters(updated)
    const chKey = draftPlan ? getChapterKey(draftPlan) : 'structure'
    if (draftPlan) void savePlan({ ...draftPlan, [chKey]: updated })
    pushStepLog(`Subseções do capítulo apagadas.`, 'success')
  }

  const handleClearAllSubsectionsInBook = async () => {
    const total = chapters.reduce((acc, ch) => acc + (ch.sections || []).reduce((sacc, s) => sacc + (s?.subsections?.length ?? 0), 0), 0)
    const sectionsCount = chapters.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
    if (total === 0) {
      pushStepLog('Nenhuma subseção no livro para apagar.', 'info')
      return
    }
    if (!window.confirm(`Apagar todas as subseções do livro? (${total} subseção(ões) em ${sectionsCount} seção(ões))`)) return
    if (!id) return
    try {
      const res = await api.post<{ status?: string; job_id?: string }>('/book/clear_all_subsections_book_queued', {
        job_id: id,
      })
      const data = res.data || {}
      if (data.status === 'queued' && data.job_id) {
        pushStepLog(`Job enfileirado (${data.job_id}). Apagar subseções em background; acompanhe no Histórico.`, 'success')
        const clearJobId = data.job_id
        const poll = setInterval(async () => {
          try {
            const statusRes = await api.get(`/status/${clearJobId}`).catch(() => null)
            const status = statusRes?.data?.status
            if (status === 'completed') {
              transientPollIntervalsRef.current.delete(poll)
              clearInterval(poll)
              await refetch(true)
              pushStepLog('Todas as subseções do livro foram apagadas.', 'success')
            } else if (status === 'failed') {
              transientPollIntervalsRef.current.delete(poll)
              clearInterval(poll)
              pushStepLog(statusRes?.data?.error || 'Job falhou.', 'error')
            }
          } catch {
            /* ignore */
          }
        }, 2000)
        transientPollIntervalsRef.current.add(poll)
        setTimeout(() => {
          if (transientPollIntervalsRef.current.has(poll)) {
            transientPollIntervalsRef.current.delete(poll)
            clearInterval(poll)
          }
        }, 120000)
      } else {
        pushStepLog('Job de apagar subseções enfileirado.', 'info')
      }
    } catch (err) {
      console.error('clear_all_subsections_book_queued:', err)
      logErrorToStepLogger('Erro ao enfileirar apagar subseções do livro', err)
    }
  }

  const [isPlanningAllSubsectionsBook, setIsPlanningAllSubsectionsBook] = useState(false)
  const [isGeneratingAllSubsectionsTextBook, setIsGeneratingAllSubsectionsTextBook] = useState(false)

  const handlePlanAllSubsectionsInBook = async () => {
    if (!id) return
    const totalSectionsCount = chapters.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
    if (totalSectionsCount === 0) {
      pushStepLog('Não há seções no livro. Crie capítulos e seções antes.', 'warning')
      return
    }
    setIsPlanningAllSubsectionsBook(true)
    try {
      const res = await api.post<{ status?: string; job_id?: string }>('/book/plan_all_subsections_book_queued', {
        job_id: id,
        chapter_index: 0,
        section_index: 0,
        api_key: getApiKey(job) || undefined,
      })
      const data = res.data || {}
      if (data.status === 'queued' && data.job_id) {
        pushStepLog(`Planejamento de subseções do livro enfileirado (job ${data.job_id}). Em background. Pode sair da página; acompanhe no Histórico.`, 'success')
      } else {
        pushStepLog('Planejamento de subseções do livro enfileirado.', 'info')
      }
    } catch (err) {
      console.error('Plan subsections (book):', err)
      logErrorToStepLogger('Erro ao planejar subseções do livro', err)
    } finally {
      setIsPlanningAllSubsectionsBook(false)
    }
  }

  const handleGenerateAllSubsectionsTextInBook = async () => {
    if (!id) return
    const sectionsWithSubs = chapters.reduce(
      (acc, ch, chIdx) =>
        acc + (ch.sections || []).filter((sec) => (sec?.subsections?.length ?? 0) > 0).length,
      0
    )
    if (sectionsWithSubs === 0) {
      pushStepLog('Nenhuma seção tem subseções. Use "Gerar todas as subseções do livro" antes.', 'warning')
      return
    }
    setIsGeneratingAllSubsectionsTextBook(true)
    try {
      const res = await api.post<{ status?: string; job_id?: string }>('/book/generate_all_subsections_text_book_queued', {
        job_id: id,
        chapter_index: 0,
        section_index: 0,
        api_key: getApiKey(job) || undefined,
      })
      const data = res.data || {}
      if (data.status === 'queued' && data.job_id) {
        pushStepLog(`Geração de texto das subseções do livro enfileirada (job ${data.job_id}). Em background. Pode sair da página; acompanhe no Histórico.`, 'success')
      } else {
        pushStepLog('Geração de texto das subseções do livro enfileirada.', 'info')
      }
    } catch (err) {
      console.error('Generate subsections text (book):', err)
      logErrorToStepLogger('Erro ao gerar textos das subseções do livro', err)
    } finally {
      setIsGeneratingAllSubsectionsTextBook(false)
    }
  }

  const handlePlanEpubChapter = async (chapterIndex: number) => {
    if (isMock) {
      pushStepLog('⚠️ Planejamento de EPUB não está disponível no modo mock', 'warning')
      return
    }
    if (!id) return
    const chapter = chapters[chapterIndex]
    const sections = chapter?.sections || []
    if (!sections.length) {
      pushStepLog('⚠️ Capítulo sem seções para planejar.', 'warning')
      return
    }
    setIsPlanningEpubChapter(true)
    pushStepLog(`🧭 Planejando EPUB para o capítulo ${chapterIndex + 1}...`, 'info')
    try {
      const updatedChapters = [...chapters]
      for (let secIdx = 0; secIdx < sections.length; secIdx += 1) {
        const sec = sections[secIdx]
        if (!sec?.content || !sec.content.trim()) {
          continue
        }
        const response = await api.post('/book/plan_epub_section', {
          job_id: id,
          chapter_index: chapterIndex,
          section_index: secIdx,
          api_key: getApiKey(job) || undefined,
        })
        const planned = response.data?.content_with_markers
        if (planned !== undefined) {
          const ch = updatedChapters[chapterIndex]
          if (!ch) continue
          const nextSections = [...(ch.sections || [])]
          if (nextSections[secIdx]) {
            nextSections[secIdx] = { ...nextSections[secIdx], content: planned }
            ch.sections = nextSections
            updatedChapters[chapterIndex] = ch
          }
        }
      }
      setChapters(updatedChapters)
      pushStepLog('✅ Planejamento do EPUB do capítulo concluído.', 'success')
    } catch (err) {
      console.error('Failed to plan EPUB chapter:', err)
      logErrorToStepLogger('Erro ao planejar EPUB do capítulo', err)
    } finally {
      setIsPlanningEpubChapter(false)
    }
  }

  const handleGenerateSectionImages = async (sectionIndex: number, count: number, styles?: string[], prompt?: string, imageModelName?: string) => {
    if (isMock) {
      alert('Geração de imagens não está disponível no modo mock')
      return
    }
    if (!id) return
    const jobs: string[] = []
    const styleName = styles && styles.length > 0 ? styles.join(', ') : null
    const modelToUse = imageModelName || job?.request_payload?.model_image || 'imagen-4.0-ultra-generate-001'
    for (let i = 0; i < count; i += 1) {
      const response = await api.post('/book/generate_section_image', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: sectionIndex,
        api_key: getApiKey(job) || undefined,
        model_name: modelToUse,
        style_name: styleName,
        custom_prompt: prompt || null,
        prompt_instructions: buildComicContextForPrompt() || undefined,
      })
      const subJobId = response.data?.job_id
      if (subJobId) jobs.push(subJobId)
    }
    if (jobs.length) {
      const key = `c${selectedChapterIdx}-s${sectionIndex}`
      setPendingImageJobs((prev) => ({
        ...prev,
        [key]: [...(prev[key] || []), ...jobs],
      }))
    }
  }

  useEffect(() => {
    const intervalMs = isPageVisible ? 4000 : 15000
    const interval = setInterval(async () => {
      const entries = Object.entries(pendingImageJobs)
      if (!entries.length) return
      const updated: Record<string, string[]> = { ...pendingImageJobs }
      let shouldRefetch = false
      for (const [key, jobIds] of entries) {
        const remaining: string[] = []
        for (const subJobId of jobIds) {
          try {
            const statusResp = await api.get(`/status/${subJobId}`)
            const status = statusResp.data?.status
            if (status === 'completed' || status === 'failed') {
              shouldRefetch = true
            } else {
              remaining.push(subJobId)
            }
          } catch {
            remaining.push(subJobId)
          }
        }
        if (remaining.length) {
          updated[key] = remaining
        } else {
          delete updated[key]
        }
      }
      if (shouldRefetch) {
        await refetch(true)
      }
      setPendingImageJobs(updated)
    }, intervalMs)
    return () => clearInterval(interval)
  }, [pendingImageJobs, refetch, isPageVisible])

  const handleDeleteImage = async (sectionIndex: number, imagePath: string) => {
    if (!id) return
    try {
      await api.post('/book/delete_section_image', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: sectionIndex,
        image_path: imagePath,
      })
      // Atualizar estado local imediatamente para a imagem sumir da UI
      const updated = [...chapters]
      const ch = updated[selectedChapterIdx]
      if (ch?.sections?.[sectionIndex]) {
        const section = ch.sections[sectionIndex]
        const images = (section.images || []).filter(
          (img) => (typeof img === 'object' && img?.path !== imagePath) || (typeof img === 'string' && img !== imagePath)
        )
        const clearImagePath = (section as { image_path?: string }).image_path === imagePath
        const contentWithoutImage = removeMarkdownImageByPath(section.content, imagePath)
        const contentChanged = contentWithoutImage !== (section.content ?? '')
        ch.sections[sectionIndex] = {
          ...section,
          images,
          ...(clearImagePath ? { image_path: undefined } : {}),
          ...(contentChanged ? { content: contentWithoutImage } : {}),
        }
        updated[selectedChapterIdx] = ch
        setChapters(updated)
        if (contentChanged && draftPlan) await savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
      }
      // Remover também do preview de slides da seção para o EPUB não mostrar a imagem deletada
      const sectionKey = `c${selectedChapterIdx}-s${sectionIndex}`
      setSectionGeneratedSlideImages((prev) => {
        const paths = prev[sectionKey] || []
        const nextPaths = paths.filter((p) => p !== imagePath)
        if (nextPaths.length === paths.length) return prev
        return { ...prev, [sectionKey]: nextPaths }
      })
      await refetch(true)
    } catch (err) {
      console.error('Erro ao remover imagem:', err)
      alert((err as any)?.response?.data?.detail ?? (err as Error)?.message ?? 'Não foi possível remover a imagem.')
    }
  }

  const handleRemoveBackground = async (sectionIndex: number, image: { path: string }) => {
    if (!id) return
    await api.post('/book/remove_background_section_image', {
      job_id: id,
      chapter_index: selectedChapterIdx,
      section_index: sectionIndex,
      image_path: image.path,
    })
    await refetch(true)
    await savePlan()
  }

  const handleKontextSectionSubmit = async () => {
    if (!id || !kontextPrompt.trim()) return
    setKontextLoading(true)
    try {
      await api.post('/config/fal-kontext/book-section', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        image_index: kontextImageIndex,
        prompt: kontextPrompt.trim(),
      })
      await refetch(true)
      setIsKontextModalOpen(false)
      setKontextPrompt('')
    } catch (e: any) {
      console.error('FAL Kontext book section:', e)
      alert(e?.response?.data?.detail || e?.message || 'Erro ao editar imagem com FAL Kontext.')
    } finally {
      setKontextLoading(false)
    }
  }

  const [restyleWithStylesLoading, setRestyleWithStylesLoading] = useState(false)
  const handleRestyleWithSectionStyles = async (image: { path: string; caption?: string }, imageIndex: number) => {
    if (!id) return
    const styleNames = getImageOptions(selectedSectionIdx).styles || []
    if (styleNames.length === 0) {
      alert('Selecione ao menos um estilo na seção (Seleção de Estilos) antes de reestilizar.')
      return
    }
    let indexToUse = imageIndex
    const baseImages = currentSection?.images || []
    const sectionImagePath = (currentSection as { image_path?: string })?.image_path
    if (sectionImagePath != null && imageIndex >= baseImages.length) {
      const updated = [...chapters]
      const ch = updated[selectedChapterIdx]
      if (ch?.sections?.[selectedSectionIdx]) {
        const sec = ch.sections[selectedSectionIdx]
        ch.sections[selectedSectionIdx] = {
          ...sec,
          images: [...(sec.images || []), { path: sectionImagePath, caption: image.caption || '' }],
          image_path: undefined,
        }
        updated[selectedChapterIdx] = ch
        setChapters(updated)
        if (draftPlan) await savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
      }
      indexToUse = baseImages.length
    }
    setRestyleWithStylesLoading(true)
    try {
      const res = await api.post<{ status?: string; job_id?: string; message?: string }>(
        '/config/fal-kontext/book-section-restyle-with-styles-queue',
        {
          job_id: id,
          chapter_index: selectedChapterIdx,
          section_index: selectedSectionIdx,
          image_index: indexToUse,
          style_names: styleNames,
          api_key: getApiKey(job) || undefined,
        }
      )
      pushStepLog(res.data?.message || 'Reestilização enfileirada. Acompanhe no Histórico.', 'success')
      const restyleJobId = res.data?.job_id
      if (restyleJobId) {
        const sectionKey = getSectionKey()
        setPendingImageJobs((prev) => ({
          ...prev,
          [sectionKey]: [...(prev[sectionKey] || []), restyleJobId],
        }))
      }
    } catch (e: any) {
      console.error('Restyle with section styles:', e)
      alert(e?.response?.data?.detail || e?.message || 'Erro ao reestilizar com estilos da seção.')
    } finally {
      setRestyleWithStylesLoading(false)
    }
  }

  const handleRestyleWithSubsectionStyles = async (
    secIdx: number,
    subIdx: number,
    image: { path: string; caption?: string },
    imageIndex: number
  ) => {
    if (!id) return
    const styleNames = getImageOptions(secIdx).styles || []
    if (styleNames.length === 0) {
      alert('Selecione ao menos um estilo na seção (Estilo da imagem) antes de reestilizar.')
      return
    }
    setRestyleWithStylesLoading(true)
    try {
      const res = await api.post<{ status?: string; job_id?: string; message?: string }>(
        '/config/fal-kontext/book-section-restyle-with-styles-queue',
        {
          job_id: id,
          chapter_index: selectedChapterIdx,
          section_index: secIdx,
          subsection_index: subIdx,
          image_index: imageIndex,
          style_names: styleNames,
          api_key: getApiKey(job) || undefined,
        }
      )
      pushStepLog(res.data?.message || 'Reestilização enfileirada. Acompanhe no Histórico.', 'success')
      const restyleJobId = res.data?.job_id
      if (restyleJobId) {
        const subKey = getSubsectionKey(secIdx, subIdx)
        setPendingImageJobs((prev) => ({
          ...prev,
          [subKey]: [...(prev[subKey] || []), restyleJobId],
        }))
      }
    } catch (e: any) {
      console.error('Restyle subsection with section styles:', e)
      alert(e?.response?.data?.detail || e?.message || 'Erro ao reestilizar com estilos da seção.')
    } finally {
      setRestyleWithStylesLoading(false)
    }
  }

  const [placeImagesInTextLoading, setPlaceImagesInTextLoading] = useState(false)
  const handlePlaceImagesInText = async () => {
    if (!id || !currentSection?.content?.trim() || !currentSectionImagesAll.length) return
    setPlaceImagesInTextLoading(true)
    try {
      const res = await api.post<{ job_id?: string; message?: string }>('/book/place_images_queue', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        api_key: getApiKey(job) || undefined,
      })
      pushStepLog(res.data?.message || 'Posicionamento enfileirado. Acompanhe no Histórico.', 'success')
    } catch (e: any) {
      console.error('Place images in text:', e)
      alert(e?.response?.data?.detail || e?.message || 'Erro ao enfileirar posicionamento de imagens.')
    } finally {
      setPlaceImagesInTextLoading(false)
    }
  }

  const handleUploadImage = async (sectionIndex: number, file: File, caption?: string) => {
    if (!id) return
    const form = new FormData()
    form.append('file', file)
    form.append('job_id', id)
    form.append('chapter_index', String(selectedChapterIdx))
    form.append('section_index', String(sectionIndex))
    if (caption) form.append('caption', caption)

    await api.post('/book/upload_section_image', form, { timeout: 60000 })
    await refetch(true)
    await savePlan()
  }

  const handleUploadSubsectionImage = useCallback(
    async (secIdx: number, subIdx: number, file: File, caption?: string) => {
      if (!id) return
      const key = getSubsectionKey(secIdx, subIdx)
      setSubsectionUploadingKey(key)
      try {
        const form = new FormData()
        form.append('file', file)
        form.append('job_id', id)
        form.append('chapter_index', String(selectedChapterIdx))
        form.append('section_index', String(secIdx))
        form.append('subsection_index', String(subIdx))
        if (caption) form.append('caption', caption)
        await api.post('/book/upload_subsection_image', form, { timeout: 60000 })
        await refetch(true)
        await savePlan()
      } finally {
        setSubsectionUploadingKey(null)
      }
    },
    [id, selectedChapterIdx, savePlan, refetch, getSubsectionKey]
  )

  const uploadBlankBookSlideAsset = useCallback(async () => {
    if (!id) {
      throw new Error('Livro não encontrado para criar o slide.')
    }

    const { createBlankSlidePngBlob } = await import('@/lib/blankSlide')
    const blob = await createBlankSlidePngBlob()
    const file = new File([blob], `blank_slide_${Date.now()}.png`, { type: 'image/png' })
    const form = new FormData()
    form.append('file', file)
    form.append('job_id', id)

    const res = await api.post('/tools/upload-image', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    })

    const rawPath = String(
      res.data?.path
      || (typeof res.data?.url === 'string' ? res.data.url : '')
      || ''
    ).trim()

    const normalizedPath = normalizeBookImagePath(rawPath).replace(/^output\//, '')
    if (!normalizedPath) {
      throw new Error('O servidor não devolveu o caminho do slide vazio.')
    }

    return normalizedPath
  }, [id])

  const handleCreateBlankSectionSlide = useCallback(async (sectionIdx: number) => {
    if (!draftPlan) return

    setCreatingBlankSectionSlide(true)
    try {
      const uploadedPath = await uploadBlankBookSlideAsset()
      const nextChapters = [...chapters]
      const chapter = nextChapters[selectedChapterIdx]
      const section = chapter?.sections?.[sectionIdx]
      if (!chapter || !section) {
        throw new Error('Seção não encontrada para inserir o slide.')
      }

      const slideCount = (section.images || []).filter(isBookSlideImage).length
      const caption = `Slide ${slideCount + 1}`
      const nextSection: BookSection = {
        ...section,
        images: [...(section.images || []), { path: uploadedPath, caption, source: 'slide' }],
      }

      const nextSections = [...(chapter.sections || [])]
      nextSections[sectionIdx] = nextSection
      nextChapters[selectedChapterIdx] = { ...chapter, sections: nextSections }

      const nextPlan = {
        ...draftPlan,
        [getChapterKey(draftPlan)]: nextChapters,
      }

      setChapters(nextChapters)
      setDraftPlan(nextPlan)
      await savePlan(nextPlan)

      openBookImageEditor({
        scope: 'section',
        kind: 'slide',
        chapterIdx: selectedChapterIdx,
        sectionIdx,
        imagePath: uploadedPath,
        title: `${section.title || `Seção ${sectionIdx + 1}`} — ${caption.toLowerCase()}`,
        caption,
      })
    } catch (e: any) {
      console.error('Create blank section slide:', e)
      alert(e?.response?.data?.detail || e?.message || 'Não foi possível criar o slide vazio da seção.')
    } finally {
      setCreatingBlankSectionSlide(false)
    }
  }, [chapters, draftPlan, openBookImageEditor, savePlan, selectedChapterIdx, uploadBlankBookSlideAsset])

  const handleCreateBlankSubsectionSlide = useCallback(async (sectionIdx: number, subsectionIdx: number) => {
    if (!draftPlan) return

    const subsectionKey = getSubsectionKey(sectionIdx, subsectionIdx)
    setCreatingBlankSubsectionSlideKey(subsectionKey)
    try {
      const uploadedPath = await uploadBlankBookSlideAsset()
      const nextChapters = [...chapters]
      const chapter = nextChapters[selectedChapterIdx]
      const section = chapter?.sections?.[sectionIdx]
      const subsection = section?.subsections?.[subsectionIdx]
      if (!chapter || !section || !subsection) {
        throw new Error('Subseção não encontrada para inserir o slide.')
      }

      const slideCount = (subsection.images || []).filter(isBookSlideImage).length
      const caption = `Slide ${slideCount + 1}`
      const nextSubsection: BookSubsection = {
        ...subsection,
        images: [...(subsection.images || []), { path: uploadedPath, caption, source: 'slide' }],
      }

      const nextSubsections = [...(section.subsections || [])]
      nextSubsections[subsectionIdx] = nextSubsection
      const nextSections = [...(chapter.sections || [])]
      nextSections[sectionIdx] = { ...section, subsections: nextSubsections }
      nextChapters[selectedChapterIdx] = { ...chapter, sections: nextSections }

      const nextPlan = {
        ...draftPlan,
        [getChapterKey(draftPlan)]: nextChapters,
      }

      setChapters(nextChapters)
      setDraftPlan(nextPlan)
      await savePlan(nextPlan)

      openBookImageEditor({
        scope: 'subsection',
        kind: 'slide',
        chapterIdx: selectedChapterIdx,
        sectionIdx,
        subsectionIdx,
        imagePath: uploadedPath,
        title: `${subsection.title || subsection.objective || `Subseção ${subsectionIdx + 1}`} — ${caption.toLowerCase()}`,
        caption,
      })
    } catch (e: any) {
      console.error('Create blank subsection slide:', e)
      alert(e?.response?.data?.detail || e?.message || 'Não foi possível criar o slide vazio da subseção.')
    } finally {
      setCreatingBlankSubsectionSlideKey((current) => (current === subsectionKey ? null : current))
    }
  }, [chapters, draftPlan, getSubsectionKey, openBookImageEditor, savePlan, selectedChapterIdx, uploadBlankBookSlideAsset])

  const handleSubsectionDataFileUpload = useCallback(
    async (file: File) => {
      const key = getSubsectionKey(selectedSectionIdx, selectedSubsectionIdx)
      setSubsectionDataFileUploadKey(key)
      try {
        const { parseDataFile } = await import('@/lib/dataFileToMarkdown')
        const result = await parseDataFile(file)
        const sub = currentSection?.subsections?.[selectedSubsectionIdx]
        const currentContent = (sub?.content || '').trim()
        const newContent = currentContent
          ? `${currentContent}\n\n${result.suggestedIntro}\n\n${result.markdownTable}`
          : `${result.suggestedIntro}\n\n${result.markdownTable}`
        updateSubsectionAtIndex(undefined, selectedSubsectionIdx, { content: newContent })
        pushStepLog(`✅ Dados de ${file.name} inseridos na subseção (${result.headers.length} colunas, ${result.rows.length} linhas).`, 'success')
      } catch (e) {
        pushStepLog('Erro ao processar Excel/CSV: ' + (e instanceof Error ? e.message : String(e)), 'error')
      } finally {
        setSubsectionDataFileUploadKey(null)
      }
    },
    [selectedSectionIdx, selectedSubsectionIdx, currentSection, updateSubsectionAtIndex, getSubsectionKey, pushStepLog]
  )

  const renderInsertBlankSlideButton = (
    onClick: () => void | Promise<void>,
    loading: boolean,
    label = 'Inserir slide vazio'
  ) => (
    <button
      type="button"
      onClick={() => void onClick()}
      disabled={loading}
      className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-white px-3 py-1.5 text-xs font-medium text-indigo-700 transition-colors hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-60 dark:border-indigo-800 dark:bg-slate-900 dark:text-indigo-200 dark:hover:bg-indigo-950/40"
      title="Cria um slide em branco e abre o editor avançado para você montar a arte."
    >
      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
      {label}
    </button>
  )

  const handleSubsectionImageCaptionChange = useCallback(
    (secIdx: number, subIdx: number, imageIndex: number, caption: string) => {
      const ch = chapters[selectedChapterIdx]
      const sec = ch?.sections?.[secIdx]
      const sub = sec?.subsections?.[subIdx]
      const images = sub?.images ? [...sub.images] : []
      const img = images[imageIndex]
      if (img == null) return
      const updatedImg = typeof img === 'object' && img !== null
        ? { ...(img as object), caption }
        : { path: String(img), caption }
      images[imageIndex] = updatedImg as { path: string; caption?: string }
      const subs = [...(sec?.subsections || [])]
      if (subs[subIdx]) subs[subIdx] = { ...subs[subIdx], images }
      const newSec = { ...sec, subsections: subs }
      const newSections = [...(ch?.sections || [])]
      newSections[secIdx] = newSec
      const newCh = { ...ch, sections: newSections }
      const updated = chapters.map((c, i) => (i === selectedChapterIdx ? newCh : c))
      setChapters(updated)
      if (draftPlan) void savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
    },
    [chapters, draftPlan, selectedChapterIdx, savePlan]
  )

  const handleAddSectionImageFromUrl = async () => {
    if (!id || !addImageFromUrlUrl.trim()) return
    setAddImageFromUrlLoading(true)
    try {
      await api.post('/book/add_section_image_from_url', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        image_url: addImageFromUrlUrl.trim(),
        caption: addImageFromUrlCaption.trim() || undefined,
      })
      await refetch(true)
      await savePlan()
      setIsAddImageFromUrlModalOpen(false)
      setAddImageFromUrlUrl('')
      setAddImageFromUrlCaption('')
      setAddImageFromUrlSearchQuery('')
    } catch (e: unknown) {
      alert((e as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail || (e as Error)?.message || 'Erro ao adicionar imagem por URL.')
    } finally {
      setAddImageFromUrlLoading(false)
    }
  }

  const handleGenerateSectionSearchQuery = async () => {
    if (!id) return
    setAddImageFromUrlSearchLoading(true)
    try {
      const res = await api.post<{ query: string }>('/book/section_image_search_query', {
        job_id: id,
        chapter_index: selectedChapterIdx,
        section_index: selectedSectionIdx,
        api_key: getApiKey(job) || undefined,
      })
      const q = res.data?.query?.trim()
      if (q) setAddImageFromUrlSearchQuery(q)
    } catch (e: unknown) {
      alert((e as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail || (e as Error)?.message || 'Erro ao gerar resumo da seção.')
    } finally {
      setAddImageFromUrlSearchLoading(false)
    }
  }

  const handleCompileChapterEpub = async () => {
    if (isMock) {
      alert('Compilação de EPUB não está disponível no modo mock')
      return
    }
    if (!id) return
    await api.post('/generate_chapter_epub', {
      job_id: id,
      chapter_index: selectedChapterIdx,
      api_key: getApiKey(job) || undefined,
    })
  }

  const handleCompileFullEpub = async (generateCover: boolean, generateImages: boolean = true) => {
    if (isMock) {
      alert('Compilação de EPUB não está disponível no modo mock')
      return
    }
    if (!id) return
    setIsGeneratingFullEpub(true)
    try {
      if (epubKeepOneImageInstructionPerChapter) {
        setIsReducingToOneImagePerChapter(true)
        pushStepLog('🧹 Executando redução para 1 instrução de imagem por capítulo antes do EPUB...', 'info')
        const reduceResult = await runReduceToOneImagePromptPerChapter()
        setIsReducingToOneImagePerChapter(false)
        if (!reduceResult.ok) {
          pushStepLog(`❌ Não foi possível preparar o EPUB: ${reduceResult.error || 'falha na redução de instruções.'}`, 'error')
          return
        }
        const replaced = reduceResult.data?.final_state?.blocks_replaced ?? 0
        const kept = reduceResult.data?.final_state?.chapters_with_instruction ?? 0
        pushStepLog(`✅ Pré-processamento concluído: ${kept} capítulo(s) com instrução única; ${replaced} bloco(s) convertido(s).`, 'success')
        await refetch(true)
      }
      await api.post('/generate_full_epub', {
        job_id: id,
        author: draftPlan?.author_inspiration || draftPlan?.author || 'Autor',
        prologue: draftPlan?.prologue || '',
        acknowledgments: draftPlan?.acknowledgments || '',
        generate_cover: generateCover,
        generate_images: generateImages,
        api_key: generateImages ? (getApiKey(job) || undefined) : undefined,
        image_styles: (draftPlan?.epub_image_styles?.length ? draftPlan.epub_image_styles : undefined) || undefined,
      })
      pushStepLog(
        generateImages
          ? '📚 EPUB enfileirado. Acompanhe no Histórico; ao concluir, o link de download aparecerá aqui.'
          : '📚 EPUB enfileirado (sem gerar imagens). Acompanhe no Histórico.',
        'success'
      )
      await refetch(true)
      setTimeout(() => refetch(true), 3000)
      setTimeout(() => refetch(true), 8000)
    } catch (err) {
      console.error('Failed to generate full EPUB:', err)
      logErrorToStepLogger('Erro ao gerar EPUB completo', err)
    } finally {
      setIsReducingToOneImagePerChapter(false)
      setIsGeneratingFullEpub(false)
    }
  }

  const handleClearEpubPreviewAndRegenerate = async () => {
    if (!draftPlan || !id) return
    const nextPlan = { ...draftPlan, full_epub_path: '' }
    setDraftPlan(nextPlan)
    await savePlan(nextPlan)
    await handleCompileFullEpub(false)
  }

  const handleExportEpubAmazonKdp = async () => {
    if (!id) return
    setIsExportingEpubAmazon(true)
    try {
      const title = draftPlan?.title || (job as any)?.topic || 'Livro'
      const author = draftPlan?.author || draftPlan?.author_inspiration || 'Autor'
      const response = await api.post(`/books/${id}/export-epub`, {
        title,
        author,
        prologue: draftPlan?.prologue,
        acknowledgments: draftPlan?.acknowledgments,
        format: 'amazon_kdp',
      }, { responseType: 'blob' })
      const blob = new Blob([response.data], { type: 'application/epub+zip' })
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${String(title).replace(/\s+/g, '_')}_amazon_kdp.epub`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      window.URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Export EPUB Amazon KDP failed:', err)
      logErrorToStepLogger('Erro ao exportar EPUB formato Amazon KDP', err)
    } finally {
      setIsExportingEpubAmazon(false)
    }
  }

  const handleExtractCodes = () => {
    if (!currentSection) return
    const extracted = extractCodeBlocks(currentSection.content || '')
    const updated = [...chapters]
    const chapter = updated[selectedChapterIdx]
    const sections = [...(chapter.sections || [])]
    sections[selectedSectionIdx] = {
      ...currentSection,
      code_blocks: [...(currentSection.code_blocks || []), ...extracted],
    }
    chapter.sections = sections
    updated[selectedChapterIdx] = chapter
    setChapters(updated)
  }

  if (!id) {
    return <div>ID do livro ausente.</div>
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin" />
      </div>
    )
  }

  if (isError) {
    const message = (error as any)?.response?.data?.detail || (error as any)?.message || 'Erro ao carregar livro.'
    return (
      <div className="space-y-4">
        <button onClick={() => navigate('/books')} className="flex items-center gap-2 text-sm text-gray-500">
          <ArrowLeft className="w-4 h-4" />
          Voltar à Biblioteca
        </button>
        <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-3">
          <div className="font-semibold text-gray-900 dark:text-white">Não foi possível carregar o livro</div>
          <div className="text-sm text-gray-600 dark:text-gray-300 break-words">{message}</div>
          <button
            onClick={() => refetch()}
            className="px-3 py-2 border rounded-lg text-sm flex items-center gap-2"
          >
            <RefreshCw className="w-4 h-4" />
            Tentar novamente
          </button>
        </div>
      </div>
    )
  }

  if (!job) {
    return (
      <div className="space-y-4">
        <button onClick={() => navigate('/books')} className="flex items-center gap-2 text-sm text-gray-500">
          <ArrowLeft className="w-4 h-4" />
          Voltar à Biblioteca
        </button>
        <div className="bg-white dark:bg-gray-800 border rounded-lg p-6">
          <div className="font-semibold text-gray-900 dark:text-white">Livro não encontrado.</div>
        </div>
      </div>
    )
  }

  if (!draftPlan) {
    const isCompletedLoadingPlan = job?.status === 'completed' && loadingPlanFromBooks
    return (
      <div className="space-y-6">
        <button onClick={() => navigate('/books')} className="flex items-center gap-2 text-sm text-gray-500">
          <ArrowLeft className="w-4 h-4" />
          Voltar à Biblioteca
        </button>
        <div className="bg-white dark:bg-gray-800 border rounded-lg p-6">
          <p className="text-gray-600 dark:text-gray-300 flex items-center gap-2">
            {isCompletedLoadingPlan && <Loader2 className="w-4 h-4 animate-spin shrink-0" />}
            {isCompletedLoadingPlan
              ? 'Carregando plano do livro da biblioteca...'
              : 'O plano do livro ainda não está pronto. Tente atualizar em alguns segundos.'}
          </p>
          <div className="mt-4 flex items-center justify-between gap-3 flex-wrap">
            <div className="text-sm text-gray-500">
              Status: <span className="font-medium">{job.status}</span>
            </div>
            <button
              onClick={() => refetch()}
              className="px-3 py-2 border rounded-lg text-sm flex items-center gap-2"
            >
              <RefreshCw className="w-4 h-4" />
              Atualizar
            </button>
          </div>
          {Array.isArray((job as any).logs) && (job as any).logs.length > 0 && (
            <div className="mt-4">
              <div className="text-xs uppercase tracking-wide text-gray-400 mb-2">Logs</div>
              <div className="max-h-48 overflow-auto rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-3 text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                {(job as any).logs
                  .slice(-30)
                  .map((l: unknown) => (typeof l === 'string' ? l : (l && typeof l === 'object' && 'message' in (l as object) ? (l as { message?: string }).message : JSON.stringify(l)) ?? ''))
                  .join('\n')}
              </div>
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <button
            onClick={() => navigate('/books')}
            className="flex items-center gap-2 text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
          >
            <ArrowLeft className="w-4 h-4" />
            Voltar à Biblioteca
          </button>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white mt-2 flex items-center gap-2">
            <BookOpen className="w-6 h-6" />
            {draftPlan.title || job.topic || 'Livro'}
          </h1>
          {draftPlan.subtitle && (
            <p className="text-gray-500 dark:text-gray-300">{draftPlan.subtitle}</p>
          )}
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => navigate(`/book/${id}/acts`)}
            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm flex items-center gap-2 hover:bg-gray-50 dark:hover:bg-gray-700"
          >
            <FileText className="w-4 h-4" />
            Ver Atos
          </button>
          <button
            onClick={() => refetch()}
            className="px-3 py-2 border rounded-lg text-sm flex items-center gap-2"
          >
            <RefreshCw className="w-4 h-4" />
            Atualizar
          </button>
          <button
            onClick={() => savePlan()}
            className="px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm flex items-center gap-2"
            disabled={saving}
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Salvar
          </button>
        </div>
      </div>

      {/* Tabs */}
      <WorkspaceTabs activeTab={activeTab} setActiveTab={setActiveTab} />

      {/* Barra de progresso da tradução — sempre visível no livro quando há job ativo para este livro */}
      {id && translateBookId === id && activeTranslateJobId && (() => {
        const raw = translateJobProgress?.progress
        const progress = raw && typeof raw === 'object' && !Array.isArray(raw) && ('unit_keys' in raw || 'results' in raw) ? raw : null
        const unitKeys = Array.isArray(progress?.unit_keys) ? progress.unit_keys : []
        const results = progress?.results && typeof progress?.results === 'object' ? progress.results : {}
        const total = unitKeys.length
        const done = total ? unitKeys.filter((k: string) => Object.prototype.hasOwnProperty.call(results, k)).length : 0
        const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
        const status = translateJobProgress?.status ?? 'pending'
        const isRunning = status === 'running' || status === 'pending'
        const isCompleted = status === 'completed'
        const isFailed = status === 'failed'
        const metaDone = unitKeys.filter((k) => k === 'meta').some((k) => Object.prototype.hasOwnProperty.call(results, k)) ? 1 : 0
        const metaTotal = unitKeys.some((k) => k === 'meta') ? 1 : 0
        const chKeys = unitKeys.filter((k) => k.startsWith('ch_') && k.split('_').length === 2)
        const chDone = chKeys.filter((k) => Object.prototype.hasOwnProperty.call(results, k)).length
        const secKeys = unitKeys.filter((k) => k.startsWith('sec_') && k.split('_').length === 3)
        const secDone = secKeys.filter((k) => Object.prototype.hasOwnProperty.call(results, k)).length
        const subKeys = unitKeys.filter((k) => k.startsWith('sub_') && k.split('_').length === 4)
        const subDone = subKeys.filter((k) => Object.prototype.hasOwnProperty.call(results, k)).length
        return (
          <div className="mt-3 px-4 py-3 rounded-xl border border-indigo-200 dark:border-indigo-800 bg-indigo-50/80 dark:bg-indigo-950/40">
            <div className="flex items-center gap-2 mb-2">
              <Languages className="h-5 w-5 text-indigo-600 dark:text-indigo-400 shrink-0" />
              <span className="text-sm font-semibold text-indigo-900 dark:text-indigo-100">
                {isCompleted ? 'Tradução concluída' : isFailed ? 'Tradução falhou' : 'Progresso da tradução'}
              </span>
            </div>
            <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2.5 min-h-[10px] mb-2">
              <div
                className={cn(
                  'h-2.5 rounded-full transition-all duration-500 min-w-0',
                  isFailed ? 'bg-red-500' : 'bg-gradient-to-r from-indigo-500 to-purple-500'
                )}
                style={{ width: total === 0 && isRunning ? '30%' : `${Math.max(pct, 2)}%` }}
              />
            </div>
            <p className="text-sm text-indigo-700 dark:text-indigo-300">
              {total === 0 && isRunning
                ? 'Preparando unidades…'
                : isCompleted
                  ? `${total} unidade(s) traduzida(s).`
                  : isFailed
                    ? 'Ver detalhes no Histórico.'
                    : `${done}/${total} unidades (${pct}%)`}
            </p>
            {(metaTotal > 0 || chKeys.length > 0 || secKeys.length > 0 || subKeys.length > 0) && (
              <p className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                Meta: {metaDone}/{metaTotal}
                {chKeys.length > 0 && ` · Capítulos: ${chDone}/${chKeys.length}`}
                {secKeys.length > 0 && ` · Seções: ${secDone}/${secKeys.length}`}
                {subKeys.length > 0 && ` · Subseções: ${subDone}/${subKeys.length}`}
              </p>
            )}
          </div>
        )
      })()}

      {/* Chapters Tab */}
      {activeTab === 'chapters' && (
        <div className="space-y-6">
          {/* Edição do livro: nome e objetivo */}
          <div className="bg-white dark:bg-gray-800 border rounded-lg p-4 space-y-4">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-white">📖 Livro</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Título do livro</label>
                <input
                  value={draftPlan?.title || job?.topic || ''}
                  onChange={(e) => setDraftPlan((p) => (p ? { ...p, title: e.target.value } : { title: e.target.value }))}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  placeholder="Título do livro"
                />
              </div>
              <div className="md:col-span-1">
                <div className="flex items-center justify-between gap-2 mb-1">
                  <label className="block text-xs font-medium text-gray-600 dark:text-gray-400">Objetivo do livro</label>
                  {id && (
                    <button
                      type="button"
                      onClick={handleExtractObjectiveFromDraft}
                      disabled={extractingObjective || !(draftPlan?.draft ?? '').trim()}
                      className="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-medium rounded border border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-200 dark:hover:bg-amber-900/50 disabled:opacity-50"
                    >
                      {extractingObjective ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                      Extrair do rascunho
                    </button>
                  )}
                </div>
                <textarea
                  value={draftPlan?.objective ?? ''}
                  onChange={(e) => setDraftPlan((p) => (p ? { ...p, objective: e.target.value } : { objective: e.target.value }))}
                  rows={12}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white resize-y min-h-[280px]"
                  placeholder="Objetivo principal e subobjetivos do livro. Use «Extrair do rascunho» para gerar um objetivo detalhado com subobjetivos."
                />
              </div>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400">Altere e clique em Salvar para persistir. O objetivo orienta a geração de capítulos com IA.</p>

            {/* Prompt de alteração — aplicado em todas as seções e subseções ao gerar/reescrever */}
            {draftPlan && (
              <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Prompt de alteração (todas as seções e subseções)
                </label>
                <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                  Instruções que a IA usará ao gerar ou reescrever o texto de cada seção e subseção (ex.: tom, nível técnico, evitar jargões).
                </p>
                <textarea
                  value={draftPlan.global_section_prompt ?? ''}
                  onChange={(e) => setDraftPlan((prev) => (prev ? { ...prev, global_section_prompt: e.target.value } : prev))}
                  onBlur={() => savePlan()}
                  placeholder="Ex.: Manter linguagem acessível; evitar termos em inglês sem tradução; incluir um exemplo prático por seção."
                  rows={2}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 resize-y"
                />
                <div className="mt-2 flex justify-end">
                  <button
                    type="button"
                    onClick={async () => {
                      await savePlan()
                      if (!id) return
                      try {
                        const res = await api.post<{ status?: string; job_id?: string; job_ids?: string[]; message?: string }>('/book/apply_global_prompt_queued', {
                          job_id: id,
                          api_key: getApiKey(job) || undefined,
                        })
                        const jobIds = res.data?.job_ids ?? (res.data?.job_id ? [res.data.job_id] : [])
                        if (jobIds.length > 0) {
                          pushStepLog(`${jobIds.length} job(s) enfileirado(s) (um por seção/subseção). O worker vai aplicar o prompt em breve.`, 'success')
                          pushStepLog('Acompanhe o status em Histórico.', 'info')
                          navigate('/history', { state: { highlightJobId: jobIds[0] } })
                        } else {
                          pushStepLog(res.data?.message || 'Nenhuma seção/subseção no livro ou resposta sem jobs.', 'warning')
                        }
                      } catch (e: any) {
                        pushStepLog(e?.response?.data?.detail || e?.message || 'Erro ao registrar job.', 'error')
                      }
                    }}
                    disabled={saving || !id}
                    className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
                  >
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    Aplicar prompt
                  </button>
                </div>
              </div>
            )}

            {/* Idioma e tradução — visível na tela principal de edição */}
            <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
              <h3 className="text-sm font-semibold text-gray-900 dark:text-white mb-2">Idioma e tradução</h3>
              <div className="flex flex-wrap items-end gap-4">
                <div className="min-w-[200px]">
                  <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Idioma do livro</label>
                  <select
                    value={draftPlan?.language || 'Português (Brasil)'}
                    onChange={(e) => setDraftPlan((p) => (p ? { ...p, language: e.target.value } : { language: e.target.value } as BookPlan))}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  >
                    <option value="Português (Brasil)">Português (Brasil)</option>
                    <option value="English">English</option>
                    <option value="Español">Español</option>
                    <option value="Français">Français</option>
                    <option value="Deutsch">Deutsch</option>
                    <option value="Italiano">Italiano</option>
                    <option value="日本語">日本語</option>
                  </select>
                </div>
                {id && (
                  <div className="flex flex-col gap-2">
                    <label className="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={translateFromScratch}
                        onChange={(e) => setTranslateFromScratch(e.target.checked)}
                        className="rounded border-gray-300 dark:border-gray-600 text-indigo-600"
                      />
                      Começar tradução do zero (limpar ícones de capítulos/seções/subseções já traduzidos)
                    </label>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={handleTranslateBook}
                        disabled={isTranslatingBook || isTranslatingMismatched || !draftPlan?.language}
                        className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-blue-200 bg-blue-50 text-blue-700 text-sm font-medium hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-900/30 dark:text-blue-200 dark:hover:bg-blue-900/50 disabled:opacity-50 w-fit"
                      >
                        {isTranslatingBook ? <Loader2 className="w-4 h-4 animate-spin" /> : <Languages className="w-4 h-4" />}
                        Traduzir todo o conteúdo para o idioma selecionado
                      </button>
                      <button
                        type="button"
                        onClick={handleTranslateBookMismatched}
                        disabled={isTranslatingBook || isTranslatingMismatched || !draftPlan?.language}
                        className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-amber-200 bg-amber-50 text-amber-800 text-sm font-medium hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200 dark:hover:bg-amber-900/50 disabled:opacity-50 w-fit"
                      >
                        {isTranslatingMismatched ? <Loader2 className="w-4 h-4 animate-spin" /> : <Languages className="w-4 h-4" />}
                        Traduzir apenas seções em outro idioma
                      </button>
                    </div>
                  </div>
                )}
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Altere o idioma e salve, ou use o botão para traduzir todo o conteúdo (capítulos, seções, prólogo, etc.). Ou traduza só o que estiver em outro idioma. A barra de progresso aparece no topo ao iniciar.</p>
              {translateBookError && <p className="text-xs text-red-500 mt-1">{translateBookError}</p>}
              {translateMismatchedError && <p className="text-xs text-red-500 mt-1">{translateMismatchedError}</p>}
              {/* Barra de progresso de tradução (capítulos, seções, subseções) — visível na tela do livro */}
              {id && translateBookId === id && activeTranslateJobId && translateJobProgress && (() => {
                const raw = translateJobProgress.progress
                const progress = raw && typeof raw === 'object' && !Array.isArray(raw) && ('unit_keys' in raw || 'results' in raw) ? raw : null
                const unitKeys = Array.isArray(progress?.unit_keys) ? progress.unit_keys : []
                const results = progress?.results && typeof progress.results === 'object' ? progress.results : {}
                const total = unitKeys.length
                const done = total ? unitKeys.filter((k: string) => Object.prototype.hasOwnProperty.call(results, k)).length : 0
                const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
                const status = translateJobProgress?.status ?? 'pending'
                const isRunning = status === 'running' || status === 'pending'
                const isCompleted = status === 'completed'
                const isFailed = status === 'failed'
                const metaDone = unitKeys.filter((k) => k === 'meta').some((k) => Object.prototype.hasOwnProperty.call(results, k)) ? 1 : 0
                const metaTotal = unitKeys.some((k) => k === 'meta') ? 1 : 0
                const chKeys = unitKeys.filter((k) => k.startsWith('ch_') && k.split('_').length === 2)
                const chDone = chKeys.filter((k) => Object.prototype.hasOwnProperty.call(results, k)).length
                const secKeys = unitKeys.filter((k) => k.startsWith('sec_') && k.split('_').length === 3)
                const secDone = secKeys.filter((k) => Object.prototype.hasOwnProperty.call(results, k)).length
                const subKeys = unitKeys.filter((k) => k.startsWith('sub_') && k.split('_').length === 4)
                const subDone = subKeys.filter((k) => Object.prototype.hasOwnProperty.call(results, k)).length
                return (
                  <div className="mt-3 px-3 py-2 rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50/60 dark:bg-indigo-950/30">
                    <div className="flex items-center gap-2 mb-1.5">
                      <Languages className="h-4 w-4 text-indigo-600 dark:text-indigo-400 shrink-0" />
                      <span className="text-sm font-medium text-indigo-800 dark:text-indigo-200">
                        {isCompleted ? 'Tradução concluída' : isFailed ? 'Tradução falhou' : 'Tradução em andamento'}
                      </span>
                    </div>
                    <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2 min-h-[8px] mb-2">
                      <div
                        className={cn(
                          'h-2 rounded-full transition-all duration-500 min-w-0',
                          isFailed ? 'bg-red-500' : 'bg-gradient-to-r from-indigo-500 to-purple-500'
                        )}
                        style={{ width: total === 0 && isRunning ? '30%' : `${Math.max(pct, 2)}%` }}
                      />
                    </div>
                    <p className="text-xs text-indigo-700 dark:text-indigo-300">
                      {total === 0 && isRunning
                        ? 'Preparando unidades…'
                        : isCompleted
                          ? `${total} unidade(s) traduzida(s).`
                          : isFailed
                            ? 'Ver detalhes no Histórico.'
                            : `${done}/${total} unidades (${pct}%)`}
                    </p>
                    {(metaTotal > 0 || chKeys.length > 0 || secKeys.length > 0 || subKeys.length > 0) && (
                      <p className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                        Meta: {metaDone}/{metaTotal}
                        {chKeys.length > 0 && ` · Capítulos: ${chDone}/${chKeys.length}`}
                        {secKeys.length > 0 && ` · Seções: ${secDone}/${secKeys.length}`}
                        {subKeys.length > 0 && ` · Subseções: ${subDone}/${subKeys.length}`}
                      </p>
                    )}
                  </div>
                )
              })()}
            </div>

            {/* Estilo de autor — livro inteiro (capítulos, seções e textos) */}
            {draftPlan && (
              <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
                <h3 className="text-sm font-semibold text-gray-900 dark:text-white mb-2">Estilo de autor (livro inteiro)</h3>
                <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                  Usado ao gerar capítulos, seções e textos. Defina um autor de referência e/ou selecione estilos abaixo.
                </p>
                <div className="space-y-2 mb-2">
                  <label className="block text-xs font-medium text-gray-600 dark:text-gray-400">Autor de referência principal (opcional)</label>
                  <input
                    type="text"
                    value={draftPlan.author_inspiration ?? ''}
                    onChange={(e) => setDraftPlan({ ...draftPlan, author_inspiration: e.target.value.trim() || undefined })}
                    placeholder="Ex.: Malcolm Gladwell, Yuval Harari"
                    className="w-full max-w-md px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder:text-gray-400"
                  />
                </div>
                <AuthorStyleSelector
                  selectedStyles={draftPlan.author_styles || []}
                  onChange={(styles) => setDraftPlan({ ...draftPlan, author_styles: styles })}
                  label="Estilos / personalidades para emular"
                  description="Selecione autores ou personagens. Se não preencher o campo acima, o primeiro selecionado será usado como referência principal."
                />
              </div>
            )}

          </div>

          {/* Número de capítulos e seções (para gerar/regenerar com IA) */}
          <div className="bg-white dark:bg-gray-800 border rounded-lg p-4">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-white mb-3">Estrutura a gerar com IA</h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">Indique quantos capítulos e quantas seções por capítulo deseja. Depois clique em &quot;Gerar Capítulos com IA&quot; ou &quot;Regenerar com IA&quot;.</p>
            <div className="flex flex-wrap items-end gap-6">
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Número de capítulos</label>
                <input
                  type="number"
                  min={1}
                  max={30}
                  value={desiredNumChapters}
                  onChange={(e) => {
                    const v = Math.max(1, Math.min(30, parseInt(e.target.value, 10) || 1))
                    setDesiredNumChapters(v)
                    if (draftPlan) setDraftPlan((p) => (p ? { ...p, num_chapters: v } : p))
                  }}
                  className="w-20 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  aria-label="Número de capítulos"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Seções por capítulo</label>
                <input
                  type="number"
                  min={1}
                  max={15}
                  value={desiredNumSectionsPerChapter}
                  onChange={(e) => {
                    const v = Math.max(1, Math.min(15, parseInt(e.target.value, 10) || 1))
                    setDesiredNumSectionsPerChapter(v)
                    if (draftPlan) setDraftPlan((p) => (p ? { ...p, num_sections_per_chapter: v } : p))
                  }}
                  className="w-20 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  aria-label="Seções por capítulo"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Mín. imagens por capítulo</label>
                <input
                  type="number"
                  min={0}
                  max={20}
                  value={draftPlan?.min_images_per_chapter ?? 1}
                  onChange={(e) => {
                    const v = Math.max(0, Math.min(20, parseInt(e.target.value, 10) || 0))
                    if (draftPlan) setDraftPlan((p) => (p ? { ...p, min_images_per_chapter: v } : p))
                    void savePlan(draftPlan ? { ...draftPlan, min_images_per_chapter: v } : ({ min_images_per_chapter: v } as BookPlan))
                  }}
                  className="w-20 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  aria-label="Quantidade mínima de imagens por capítulo"
                  title="Quantidade mínima de imagens por capítulo (padrão 1). Usado em redução e relatórios."
                />
              </div>
              <div className="text-sm text-gray-600 dark:text-gray-400">
                → Serão gerados <strong>{desiredNumChapters}</strong> capítulo{desiredNumChapters !== 1 ? 's' : ''} com <strong>{desiredNumSectionsPerChapter}</strong> seção{desiredNumSectionsPerChapter !== 1 ? 'ões' : ''} cada (total de <strong>{desiredNumChapters * desiredNumSectionsPerChapter}</strong> seções).
              </div>
            </div>

            {/* Padrões das seções: quantidade mínima de palavras e código fonte */}
            <div className="bg-white dark:bg-gray-800 border rounded-lg p-4 space-y-3">
              <h2 className="text-sm font-semibold text-gray-900 dark:text-white">Padrões das seções (geração com IA e novas seções)</h2>
              <div className="flex flex-wrap items-center gap-6">
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <span>Mín. caracteres (seção e subseção):</span>
                  <input
                    type="number"
                    min={0}
                    step={50}
                    placeholder="Ex.: 400 (vazio = padrão)"
                    value={draftPlan?.default_min_text_length ?? ''}
                    onChange={(e) => {
                      const v = e.target.value
                      const num = v === '' ? undefined : Math.max(0, parseInt(v, 10) || 0)
                      const next = draftPlan ? { ...draftPlan, default_min_text_length: num } : ({ default_min_text_length: num } as BookPlan)
                      setDraftPlan(next)
                      void savePlan(next)
                    }}
                    className="w-28 px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  />
                </label>
                <label className="flex items-center gap-2 cursor-pointer text-sm text-gray-700 dark:text-gray-300">
                  <input
                    type="checkbox"
                    checked={draftPlan?.default_has_source_code ?? false}
                    onChange={(e) => {
                      const next = draftPlan ? { ...draftPlan, default_has_source_code: e.target.checked } : ({ default_has_source_code: e.target.checked } as BookPlan)
                      setDraftPlan(next)
                      void savePlan(next)
                    }}
                    className="rounded border-gray-300 dark:border-gray-600 text-slate-600 focus:ring-slate-500"
                  />
                  <span>Seções incluem código fonte (padrão)</span>
                </label>
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <span>Nº de subseções por seção:</span>
                  <input
                    type="number"
                    min={2}
                    max={20}
                    placeholder="Auto (2-6)"
                    value={draftPlan?.default_num_subsections_per_section ?? ''}
                    onChange={(e) => {
                      const v = e.target.value
                      const num = v === '' ? undefined : Math.min(20, Math.max(2, parseInt(v, 10) || 2))
                      const next = draftPlan ? { ...draftPlan, default_num_subsections_per_section: num } : ({ default_num_subsections_per_section: num } as BookPlan)
                      setDraftPlan(next)
                      void savePlan(next)
                    }}
                    title="Quantidade exata de subseções geradas por seção (2-20). Vazio = automático (2-6)"
                    className="w-28 px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  />
                </label>
              </div>
              <div className="flex flex-wrap items-center gap-4 pt-1">
                <span className="text-sm text-gray-700 dark:text-gray-300">Estilo de escrita ao gerar seções:</span>
                <label className="flex items-center gap-2 cursor-pointer text-sm">
                  <input
                    type="radio"
                    name="section_writing_style"
                    checked={(draftPlan?.default_section_writing_style ?? 'narrative') === 'narrative'}
                    onChange={() => {
                      const next = draftPlan ? { ...draftPlan, default_section_writing_style: 'narrative' as const } : ({ default_section_writing_style: 'narrative' } as BookPlan)
                      setDraftPlan(next)
                      void savePlan(next)
                    }}
                    className="border-gray-300 dark:border-gray-600 text-amber-600 focus:ring-amber-500"
                  />
                  <span className="text-gray-700 dark:text-gray-300">Narrativa e inteligente</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer text-sm">
                  <input
                    type="radio"
                    name="section_writing_style"
                    checked={(draftPlan?.default_section_writing_style ?? 'narrative') === 'topical'}
                    onChange={() => {
                      const next = draftPlan ? { ...draftPlan, default_section_writing_style: 'topical' as const } : ({ default_section_writing_style: 'topical' } as BookPlan)
                      setDraftPlan(next)
                      void savePlan(next)
                    }}
                    className="border-gray-300 dark:border-gray-600 text-amber-600 focus:ring-amber-500"
                  />
                  <span className="text-gray-700 dark:text-gray-300">Tópificada</span>
                </label>
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                <strong>Narrativa:</strong> texto contínuo, fluido e reflexivo. <strong>Tópificada:</strong> tópicos, listas e parágrafos curtos para leitura rápida. Aplicado ao gerar uma seção ou todas.
              </p>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Aplicado ao criar novas seções e na geração de conteúdo com IA. Cada seção pode ser ajustada no editor da seção.
              </p>
              <div className="pt-2 border-t border-gray-200 dark:border-gray-600">
                <button
                  type="button"
                  onClick={clearAllSectionsText}
                  className="px-3 py-2 border border-amber-200 dark:border-amber-700 rounded-md text-sm flex items-center gap-2 text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20"
                  title="Apagar apenas o texto de todas as seções (mantém títulos, objetivos e imagens)"
                >
                  <FileText className="w-4 h-4" />
                  Apagar textos das seções
                </button>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Remove só o conteúdo em markdown de todas as seções. Títulos, objetivos e imagens são mantidos.</p>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-6">
            <div className="bg-white dark:bg-gray-800 border rounded-lg p-4 space-y-3">
              <div className="rounded-lg border border-gray-200 dark:border-gray-600 p-3 space-y-2 bg-gray-50 dark:bg-gray-800/50">
                <label className="text-xs font-semibold text-gray-700 dark:text-gray-300 block">
                  Novo capítulo a partir de texto
                </label>
                <textarea
                  value={newChapterFromText}
                  onChange={(e) => setNewChapterFromText(e.target.value)}
                  placeholder={
                    'Cole ou digite o texto do capítulo.\n\n' +
                    '• Com IA: descreva a ideia/tema; o agente gera título e seções.\n' +
                    '• Manual: 1ª linha = título do capítulo; blocos separados por linha em branco = seções (1ª linha do bloco = título da seção).'
                  }
                  rows={6}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 resize-y min-h-[120px]"
                />
                <div className="flex flex-col gap-2">
                  <button
                    type="button"
                    onClick={handleAddChapterFromText}
                    disabled={!newChapterFromText.trim() || isGeneratingChapterFromPrompt}
                    className="w-full px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                    title="Usa o contexto do livro para gerar título e seções com IA"
                  >
                    {isGeneratingChapterFromPrompt ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" />
                        Gerando capítulo...
                      </>
                    ) : (
                      <>
                        <Sparkles className="w-4 h-4" />
                        Criar capítulo com IA
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={handleAddChapterManuallyFromText}
                    disabled={!newChapterFromText.trim()}
                    className="w-full px-3 py-2 bg-slate-600 text-white rounded-lg text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                    title="Primeira linha = título do capítulo; parágrafos separados por linha em branco = seções"
                  >
                    <Pencil className="w-4 h-4" />
                    Criar capítulo manualmente
                  </button>
                </div>
              </div>
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <h2 className="text-sm font-semibold">Capítulos</h2>
                <div className="flex items-center gap-2">
                  {chapters.length > 0 && (
                    <button
                      type="button"
                      onClick={handleDeleteAllChapters}
                      className="text-sm flex items-center gap-1 text-red-600 hover:text-red-700 dark:text-red-400"
                      title="Remover todos os capítulos e seções"
                    >
                      <Trash2 className="w-4 h-4" />
                      Apagar todos
                    </button>
                  )}
                  <button
                    onClick={handleAddChapter}
                    className="text-sm flex items-center gap-1 text-emerald-600"
                  >
                    <Plus className="w-4 h-4" />
                    Adicionar
                  </button>
                </div>
              </div>
              {subsectionTextProgress && subsectionTextProgress.jobIds.length > 0 && (
                <div className="rounded-lg border border-emerald-200/60 dark:border-emerald-800/60 bg-emerald-50/40 dark:bg-emerald-900/15 p-2.5 mb-2">
                  <p className="text-xs font-medium text-emerald-700 dark:text-emerald-300 mb-1.5 flex items-center gap-2">
                    <span className="inline-block w-3 h-3 rounded-full bg-emerald-500 animate-pulse" />
                    Gerando texto das subseções: {subsectionTextProgress.completed}/{subsectionTextProgress.jobIds.length} ({subsectionTextProgress.jobIds.length ? Math.round((subsectionTextProgress.completed / subsectionTextProgress.jobIds.length) * 100) : 0}%)
                    {subsectionTextProgress.failed > 0 && (
                      <span className="text-amber-600 dark:text-amber-400">• {subsectionTextProgress.failed} falha(s)</span>
                    )}
                  </p>
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-emerald-500 dark:bg-emerald-400 rounded-full transition-all duration-300"
                        style={{ width: `${subsectionTextProgress.jobIds.length ? Math.round((subsectionTextProgress.completed / subsectionTextProgress.jobIds.length) * 100) : 0}%` }}
                      />
                    </div>
                    <span className="text-xs text-emerald-600 dark:text-emerald-400 tabular-nums font-medium">
                      {subsectionTextProgress.completed}/{subsectionTextProgress.jobIds.length}
                    </span>
                  </div>
                </div>
              )}
              {(() => {
                const subStats = getSubsectionStats(chapters)
                if (subStats.totalSections === 0) return null
                const pctSections = subStats.totalSections ? Math.round((subStats.sectionsWithSubsections / subStats.totalSections) * 100) : 0
                const pctText = subStats.totalSubsections ? Math.round((subStats.subsectionsWithText / subStats.totalSubsections) * 100) : 0
                return (
                  <div className="rounded-lg border border-purple-200/60 dark:border-purple-800/60 bg-purple-50/30 dark:bg-purple-900/10 p-2.5 mb-2">
                    <p className="text-xs font-medium text-purple-700 dark:text-purple-300 mb-1.5">
                      Subseções: {subStats.sectionsWithSubsections}/{subStats.totalSections} seções ({pctSections}%)
                      {subStats.totalSubsections > 0 && (
                        <> • texto: {subStats.subsectionsWithText}/{subStats.totalSubsections} ({pctText}%)</>
                      )}
                    </p>
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] text-gray-500 dark:text-gray-400 w-16">Seções</span>
                        <div className="flex-1 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                          <div className="h-full bg-purple-500 dark:bg-purple-400 rounded-full transition-all" style={{ width: `${pctSections}%` }} />
                        </div>
                        <span className="text-[10px] text-gray-600 dark:text-gray-300 tabular-nums">{pctSections}%</span>
                      </div>
                      {subStats.totalSubsections > 0 && (
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] text-gray-500 dark:text-gray-400 w-16">Texto</span>
                          <div className="flex-1 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                            <div className="h-full bg-emerald-500 dark:bg-emerald-400 rounded-full transition-all" style={{ width: `${pctText}%` }} />
                          </div>
                          <span className="text-[10px] text-gray-600 dark:text-gray-300 tabular-nums">{pctText}%</span>
                        </div>
                      )}
                    </div>
                  </div>
                )
              })()}
              <div className="space-y-2">
                {chapters.map((chapter, idx) => {
                  const sections = chapter.sections || []
                  const isChapterExpanded = expandedChapterIdx === idx
                  return (
                    <div
                      key={`chapter-${idx}`}
                      className={cn(
                        'rounded-lg border text-sm',
                        idx === selectedChapterIdx
                          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 dark:border-blue-600'
                          : 'border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800'
                      )}
                    >
                      <div className="flex items-center gap-1 px-2 py-2">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            setExpandedChapterIdx((prev) => (prev === idx ? null : idx))
                            if (expandedChapterIdx !== idx) {
                              setExpandedSectionKey(null)
                              setExpandedSubsectionsPanelKey(null)
                            }
                          }}
                          className="p-0.5 rounded text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-600 flex-shrink-0"
                          aria-label={isChapterExpanded ? 'Recolher' : 'Expandir seções'}
                          title={isChapterExpanded ? 'Recolher' : 'Expandir para ver seções e subseções'}
                        >
                          {isChapterExpanded ? (
                            <ChevronDown className="w-4 h-4" />
                          ) : (
                            <ChevronRight className="w-4 h-4" />
                          )}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            const newChapters = [...chapters]
                            if (newChapters[idx] && !newChapters[idx].content && newChapters[idx].introduction) {
                              newChapters[idx] = { ...newChapters[idx], content: newChapters[idx].introduction }
                              setChapters(newChapters)
                            }
                            setSelectedChapterIdx(idx)
                            setSelectedSectionIdx(0)
                          }}
                          className="min-w-0 flex-1 text-left break-words hover:opacity-90 flex items-center gap-1.5"
                        >
                          <span className={cn(idx === selectedChapterIdx && 'text-blue-700 dark:text-blue-300 font-medium')}>
                            {chapter.title || `Capítulo ${idx + 1}`}
                          </span>
                          {translatedUnitKeys.includes(`ch_${idx}`) && (
                            <span title="Traduzido">
                              <Languages className="w-3.5 h-3.5 shrink-0 text-indigo-500 dark:text-indigo-400" />
                            </span>
                          )}
                        </button>
                        {(sections || []).some((sec) => {
                          const secOk = (sec?.content?.trim() ?? '') || (sec?.images?.length ?? 0) > 0 || !!(sec as { image_path?: string })?.image_path
                          const subOk = (sec?.subsections || []).some((sub) => (sub?.content?.trim() ?? '') || (sub?.slide_prompts?.length ?? 0) > 0 || (sub?.images?.length ?? 0) > 0)
                          return secOk || subOk
                        }) ? (
                          <span className="flex-shrink-0 flex items-center gap-0.5">
                            {(sections || []).some((sec) => (sec?.images?.length ?? 0) > 0 || !!(sec as { image_path?: string })?.image_path || (sec?.subsections || []).some((sub) => (sub?.images?.length ?? 0) > 0)) ? (
                              <span className="text-amber-500 dark:text-amber-400" title="Capítulo tem imagem(ns)">
                                <ImageIcon className="w-3.5 h-3.5" />
                              </span>
                            ) : null}
                            <span className="text-emerald-500 dark:text-emerald-400" title="Capítulo tem texto ou imagem">
                              <FileText className="w-3.5 h-3.5" />
                            </span>
                          </span>
                        ) : null}
                        <div className="flex items-center gap-0.5 flex-shrink-0">
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); handleMoveChapter(idx, 'up') }}
                            disabled={idx === 0}
                            className="p-0.5 rounded text-gray-500 hover:text-gray-700 hover:bg-gray-100 dark:hover:bg-gray-600 disabled:opacity-30 disabled:pointer-events-none"
                            aria-label="Subir capítulo"
                            title="Subir"
                          >
                            <ChevronUp className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); handleMoveChapter(idx, 'down') }}
                            disabled={idx === chapters.length - 1}
                            className="p-0.5 rounded text-gray-500 hover:text-gray-700 hover:bg-gray-100 dark:hover:bg-gray-600 disabled:opacity-30 disabled:pointer-events-none"
                            aria-label="Descer capítulo"
                            title="Descer"
                          >
                            <ChevronDown className="w-4 h-4" />
                          </button>
                          <span className="text-xs text-gray-400 w-5 text-center" title={`${sections.length} seção(ões)`}>
                            {sections.length}
                          </span>
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation()
                              void handleDeleteChapter(idx)
                            }}
                            className="text-xs text-red-500 hover:text-red-600 p-0.5"
                            aria-label={`Excluir capítulo ${idx + 1}`}
                            title="Excluir capítulo"
                          >
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </div>
                      </div>
                      {isChapterExpanded && sections.length > 0 && (
                        <div className="border-t border-gray-200 dark:border-gray-600 bg-gray-50/50 dark:bg-gray-900/30 px-2 py-2 pl-6 space-y-1">
                          {sections.map((section, secIdx) => {
                            const secKey = `${idx}-${secIdx}`
                            const isSectionExpanded = expandedSectionKey === secKey
                            const subsections = section.subsections || []
                            return (
                              <div key={`sec-${secIdx}`} className="rounded border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 overflow-hidden">
                                <div className="flex items-center gap-1 px-2 py-1.5">
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      setExpandedSectionKey((prev) => (prev === secKey ? null : secKey))
                                    }}
                                    className="p-0.5 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-600 flex-shrink-0"
                                    aria-label={isSectionExpanded ? 'Recolher subseções' : 'Expandir subseções'}
                                    title={subsections.length ? (isSectionExpanded ? 'Recolher' : 'Ver subseções') : 'Sem subseções'}
                                    disabled={subsections.length === 0}
                                  >
                                    {subsections.length > 0 ? (
                                      isSectionExpanded ? (
                                        <ChevronDown className="w-3.5 h-3.5" />
                                      ) : (
                                        <ChevronRight className="w-3.5 h-3.5" />
                                      )
                                    ) : (
                                      <span className="w-3.5 h-3.5 inline-block" />
                                    )}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => {
                                      setSelectedChapterIdx(idx)
                                      setSelectedSectionIdx(secIdx)
                                    }}
                                    className={cn(
                                      'min-w-0 flex-1 text-left text-xs truncate flex items-center gap-1',
                                      idx === selectedChapterIdx && secIdx === selectedSectionIdx
                                        ? 'text-blue-600 dark:text-blue-400 font-medium'
                                        : 'text-gray-700 dark:text-gray-300'
                                    )}
                                  >
                                    {section.title || `Seção ${secIdx + 1}`}
                                    {translatedUnitKeys.includes(`sec_${idx}_${secIdx}`) && (
                                      <span title="Traduzido">
                                        <Languages className="w-3 h-3 shrink-0 text-indigo-500 dark:text-indigo-400" />
                                      </span>
                                    )}
                                  </button>
                                  {((section.content?.trim() || section.images?.length || (section as { image_path?: string }).image_path) || (subsections || []).some((sub) => (sub?.content?.trim() ?? '') || (sub?.slide_prompts?.length ?? 0) > 0 || (sub?.images?.length ?? 0) > 0)) ? (
                                    <span className="flex-shrink-0 flex items-center gap-0.5">
                                      {((section.images?.length ?? 0) > 0 || !!(section as { image_path?: string }).image_path || (subsections || []).some((sub) => (sub?.images?.length ?? 0) > 0)) ? (
                                        <span className="text-amber-500 dark:text-amber-400" title="Seção tem imagem(ns)">
                                          <ImageIcon className="w-3.5 h-3.5" />
                                        </span>
                                      ) : null}
                                      <span className="text-emerald-500 dark:text-emerald-400" title="Seção tem texto ou imagem">
                                        <FileText className="w-3.5 h-3.5" />
                                      </span>
                                    </span>
                                  ) : null}
                                  <span className="text-[10px] text-gray-400 w-4 text-center flex-shrink-0">
                                    {subsections.length}
                                  </span>
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      setSelectedChapterIdx(idx)
                                      setSelectedSectionIdx(secIdx)
                                      setActiveTab('section')
                                    }}
                                    className="p-0.5 rounded text-gray-500 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/30 flex-shrink-0"
                                    title="Editar seção"
                                  >
                                    <Pencil className="w-3 h-3" />
                                  </button>
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      deleteSectionAtChapter(idx, secIdx)
                                    }}
                                    disabled={sections.length <= 1}
                                    className="p-0.5 rounded text-gray-500 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 flex-shrink-0 disabled:opacity-30 disabled:pointer-events-none"
                                    title="Excluir seção"
                                  >
                                    <Trash2 className="w-3 h-3" />
                                  </button>
                                </div>
                                {isSectionExpanded && subsections.length > 0 && (
                                  <div className="border-t border-gray-100 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/80 overflow-hidden">
                                    <button
                                      type="button"
                                      onClick={(e) => {
                                        e.stopPropagation()
                                        setExpandedSubsectionsPanelKey((prev) => (prev === secKey ? null : secKey))
                                      }}
                                      className="w-full flex items-center gap-1.5 pl-6 pr-2 py-1.5 text-left text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700/50"
                                    >
                                      {expandedSubsectionsPanelKey === secKey ? (
                                        <ChevronDown className="w-3.5 h-3.5 shrink-0" />
                                      ) : (
                                        <ChevronRight className="w-3.5 h-3.5 shrink-0" />
                                      )}
                                      <span>Subseções ({subsections.length})</span>
                                    </button>
                                    {expandedSubsectionsPanelKey === secKey && (
                                      <div className="pl-6 pr-2 pb-1.5 space-y-1">
                                        {subsections.map((sub, subIdx) => (
                                          <div
                                            key={`sub-${subIdx}`}
                                            className="flex items-center gap-1 text-[11px] text-gray-600 dark:text-gray-400 border-b border-gray-100 dark:border-gray-700/50 pb-1 last:border-0 min-w-0"
                                          >
                                            <span className="text-gray-400 dark:text-gray-500 shrink-0">{subIdx + 1}.</span>
                                            <span className="truncate flex-1" title={sub.objective || sub.content?.slice(0, 200)}>
                                              {(sub.objective || '').trim() || (sub.content?.slice(0, 80) ?? '') || '—'}
                                            </span>
                                            {translatedUnitKeys.includes(`sub_${idx}_${secIdx}_${subIdx}`) && (
                                              <span title="Traduzido">
                                                <Languages className="w-3 h-3 shrink-0 text-indigo-500 dark:text-indigo-400" />
                                              </span>
                                            )}
                                            {((sub.content?.trim() ?? '') || (sub.slide_prompts?.length ?? 0) > 0 || (sub.images?.length ?? 0) > 0) ? (
                                              <span className="shrink-0 flex items-center gap-0.5">
                                                {(sub.images?.length ?? 0) > 0 ? (
                                                  <span className="text-amber-500 dark:text-amber-400" title="Subseção tem imagem(ns)">
                                                    <ImageIcon className="w-3 h-3" />
                                                  </span>
                                                ) : null}
                                                <span className="text-emerald-500 dark:text-emerald-400" title="Subseção tem texto ou imagem">
                                                  <FileText className="w-3 h-3" />
                                                </span>
                                              </span>
                                            ) : null}
                                            <button
                                              type="button"
                                              onClick={(e) => {
                                                e.stopPropagation()
                                                setSelectedChapterIdx(idx)
                                                setSelectedSectionIdx(secIdx)
                                                setSelectedSubsectionIdx(subIdx)
                                                setActiveTab('subsections')
                                              }}
                                              className="p-0.5 rounded shrink-0 text-gray-400 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/30"
                                              title="Editar subseção"
                                            >
                                              <Pencil className="w-3 h-3" />
                                            </button>
                                            <button
                                              type="button"
                                              onClick={(e) => {
                                                e.stopPropagation()
                                                if (window.confirm('Remover esta subseção?')) removeSubsectionAt(idx, secIdx, subIdx)
                                              }}
                                              className="p-0.5 rounded shrink-0 text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
                                              title="Excluir subseção"
                                            >
                                              <Trash2 className="w-3 h-3" />
                                            </button>
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      )}
                      {isChapterExpanded && sections.length === 0 && (
                        <div className="border-t border-gray-200 dark:border-gray-600 bg-gray-50/50 dark:bg-gray-900/30 px-2 py-2 pl-6">
                          <p className="text-xs text-gray-500 dark:text-gray-400">Nenhuma seção. Use &quot;IA&quot; ou &quot;Adicionar&quot; seção abaixo.</p>
                        </div>
                      )}
                    </div>
                  )
                })}
                {chapters.length === 0 && (
                  <div className="space-y-3">
                    <p className="text-xs text-gray-500">Nenhum capítulo encontrado.</p>
                    <button
                      onClick={() => handleGenerateChaptersAI(desiredNumChapters, desiredNumSectionsPerChapter)}
                      disabled={isGeneratingChapters}
                      className="w-full px-3 py-2 bg-gradient-to-r from-purple-500 to-indigo-600 text-white rounded-lg text-sm flex items-center justify-center gap-2 hover:from-purple-600 hover:to-indigo-700 disabled:opacity-50"
                    >
                      {isGeneratingChapters ? (
                        <>
                          <Loader2 className="w-4 h-4 animate-spin" />
                          Gerando...
                        </>
                      ) : (
                        <>
                          <Sparkles className="w-4 h-4" />
                          Gerar Capítulos com IA ({desiredNumChapters} cap., {desiredNumSectionsPerChapter} seç./cap.)
                        </>
                      )}
                    </button>
                  </div>
                )}
              </div>

              {/* Quick AI Generate button when chapters exist */}
              {chapters.length > 0 && (
                <>
                  <button
                    onClick={() => handleGenerateChaptersAI(desiredNumChapters, desiredNumSectionsPerChapter)}
                    disabled={isGeneratingChapters}
                    className="w-full px-3 py-2 border border-purple-300 text-purple-600 dark:text-purple-400 dark:border-purple-500 rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-purple-50 dark:hover:bg-purple-900/20 disabled:opacity-50"
                    title={`Regenerar estrutura: ${desiredNumChapters} capítulos, ${desiredNumSectionsPerChapter} seções por capítulo`}
                  >
                    {isGeneratingChapters ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando...
                      </>
                    ) : (
                      <>
                        <Sparkles className="w-3 h-3" />
                        Regenerar com IA ({desiredNumChapters} cap., {desiredNumSectionsPerChapter} seç./cap.)
                      </>
                    )}
                  </button>
                  {/* Estilo visual dos slides (livro) — multi-estilo */}
                  {draftPlan && (
                    <div className="space-y-2 mb-3">
                      <div className="text-xs font-medium text-gray-600 dark:text-gray-400">Estilo visual dos slides (livro)</div>
                      <StyleGrid
                        selectedStyles={draftPlan.book_slide_styles || []}
                        onChange={(styles) => setDraftPlan((prev) => (prev ? { ...prev, book_slide_styles: styles } : prev))}
                        maxSelection={10}
                        showSearch={true}
                        showCategoryFilter={true}
                        defaultCategory="all"
                        columns={3}
                        cardHeight="140px"
                      />
                      <SlideFontPresetSelect className="mt-3" />
                    </div>
                  )}
                  <div className="text-xs font-medium text-gray-600 dark:text-gray-400 mt-2 mb-1">Ações em lote</div>
                  <button
                    type="button"
                    onClick={() => void handleGenerateAllSlidePrompts()}
                    disabled={isGeneratingAllSlidePrompts || chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 bg-indigo-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-indigo-600 disabled:opacity-50"
                    title="Gera prompts de slides (IA) para todas as seções que têm conteúdo"
                  >
                    {isGeneratingAllSlidePrompts ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando prompts...
                      </>
                    ) : (
                      <>
                        <Wand2 className="w-3 h-3" />
                        Gerar todos os prompts de slides das seções
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleGenerateAllSectionSlides()}
                    disabled={isGeneratingSectionSlides || Object.keys(sectionSlidePrompts).filter((k) => (sectionSlidePrompts[k]?.length ?? 0) > 0).length === 0}
                    className="w-full px-3 py-2 bg-emerald-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-emerald-600 disabled:opacity-50"
                    title="Gera as imagens dos slides para todas as seções que já têm prompts"
                  >
                    {isGeneratingSectionSlides ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Criando slides...
                      </>
                    ) : (
                      <>
                        <Layers className="w-3 h-3" />
                        Criar todos os slides das seções
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handlePlanAllChaptersSections()}
                    disabled={isPlanningAllChaptersSections || chapters.length === 0}
                    className="w-full px-3 py-2 bg-amber-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-amber-600 disabled:opacity-50"
                    title="Cria a estrutura de seções (títulos/objetivos) para cada capítulo, sem gerar textos. Use depois 'Gerar texto de todas as seções'."
                  >
                    {isPlanningAllChaptersSections ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Criando seções...
                      </>
                    ) : (
                      <>
                        <FileText className="w-3 h-3" />
                        Criar todas as seções do livro
                      </>
                    )}
                  </button>
                  {planAllChaptersStatus && (
                    <p className={cn(
                      'text-xs mt-1 px-2 py-1 rounded',
                      planAllChaptersStatus.startsWith('Erro') || planAllChaptersStatus.startsWith('Falha')
                        ? 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20'
                        : planAllChaptersStatus.startsWith('Concluído')
                          ? 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20'
                          : 'text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/20'
                    )}>
                      {planAllChaptersStatus}
                    </p>
                  )}
                  <label className="flex items-center gap-2 text-xs text-amber-800 dark:text-amber-200 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={regenerateAllBookSections}
                      onChange={(e) => setRegenerateAllBookSections(e.target.checked)}
                      className="rounded border-amber-400"
                    />
                    Incluir seções que já têm texto (um job por seção)
                  </label>
                  <button
                    type="button"
                    onClick={handleGenerateAllSections}
                    disabled={isGeneratingAllSections || chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 bg-amber-600/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-amber-700 disabled:opacity-50"
                    title="Gera o texto (com IA) de todas as seções do livro. Crie antes a estrutura com 'Criar todas as seções do livro'."
                  >
                    {isGeneratingAllSections ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando textos...
                      </>
                    ) : (
                      <>
                        <Sparkles className="w-3 h-3" />
                        Gerar texto de todas as seções
                      </>
                    )}
                  </button>
                  <div className="rounded-lg border border-violet-200 dark:border-violet-800 bg-violet-50/70 dark:bg-violet-950/25 p-3 space-y-2 mt-2">
                    <div className="text-xs font-semibold text-violet-900 dark:text-violet-100 flex items-center gap-2">
                      <BookMarked className="w-3.5 h-3.5 shrink-0" />
                      Base de fontes (Perplexity)
                    </div>
                    <p className="text-[11px] leading-snug text-violet-800/90 dark:text-violet-200/85">
                      As referências completas ficam aqui e no <strong className="font-medium">final do EPUB</strong>. No texto das seções entram só as citações{' '}
                      <code className="text-[10px] bg-violet-100/80 dark:bg-violet-900/40 px-1 rounded">[1]</code>
                      <code className="text-[10px] bg-violet-100/80 dark:bg-violet-900/40 px-1 rounded">[2]</code>…
                    </p>
                    {sourceLibrarySorted.length === 0 ? (
                      <p className="text-[11px] text-violet-700/80 dark:text-violet-300/70 italic">Nenhuma fonte na base ainda. Use os botões Perplexity abaixo.</p>
                    ) : (
                      <ul className="max-h-40 overflow-y-auto space-y-1.5 text-[11px] text-violet-900 dark:text-violet-100 pr-1">
                        {sourceLibrarySorted.flatMap((entry, entryIdx) => {
                          const line = (entry.line || entry.text || '').trim()
                          const url = (entry.url || '').trim()
                          const chunks = splitNumberedReferenceLine(line || '')
                          const rows = chunks.length ? chunks : ['']
                          return rows.map((chunk, j) => {
                            const bracket = /^\[(\d+)\]\s*(.*)$/s.exec(chunk)
                            const label = bracket ? bracket[1] : String(entry.n ?? entryIdx + 1)
                            const body = bracket ? bracket[2].trim() : chunk
                            return (
                              <li
                                key={`${entryIdx}-${j}`}
                                className="border-b border-violet-200/50 dark:border-violet-800/50 pb-1.5 last:border-0"
                              >
                                <span className="font-mono font-semibold text-violet-600 dark:text-violet-300">
                                  [{label}]
                                </span>{' '}
                                <span className="whitespace-pre-wrap break-words">{body || '(sem texto)'}</span>
                                {url && j === rows.length - 1 ? (
                                  <a
                                    href={url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="block mt-0.5 text-violet-600 dark:text-violet-400 underline truncate"
                                  >
                                    {url}
                                  </a>
                                ) : null}
                              </li>
                            )
                          })
                        })}
                      </ul>
                    )}
                  </div>
                  <div className="text-xs font-medium text-sky-800 dark:text-sky-200 mt-2 mb-1">Perplexity (livro inteiro — fontes)</div>
                  <button
                    type="button"
                    onClick={() => void handlePerplexityEnrichBookBatch('sections')}
                    disabled={
                      isMock ||
                      !id ||
                      perplexityBusy !== null ||
                      chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0
                    }
                    className="w-full px-3 py-2 bg-sky-600/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-sky-700 disabled:opacity-50"
                    title="Fontes na web só nos corpos das seções (não nas subseções)"
                  >
                    {perplexityBusy === 'enrich-sections-all' ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Fontes nas seções...
                      </>
                    ) : (
                      <>
                        <Search className="w-3 h-3" />
                        Fontes — todas as seções
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handlePerplexityEnrichBookBatch('subsections')}
                    disabled={
                      isMock ||
                      !id ||
                      perplexityBusy !== null ||
                      chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0
                    }
                    className="w-full px-3 py-2 bg-cyan-600/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-cyan-700 disabled:opacity-50"
                    title="Fontes na web em cada subseção (não repete busca no texto da seção)"
                  >
                    {perplexityBusy === 'enrich-subs-all' ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Fontes nas subseções...
                      </>
                    ) : (
                      <>
                        <Search className="w-3 h-3" />
                        Fontes — todas as subseções
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handlePerplexityWriteSectionsBook()}
                    disabled={
                      isMock ||
                      !id ||
                      perplexityBusy !== null ||
                      chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0
                    }
                    className="w-full px-3 py-2 border border-sky-500 dark:border-sky-600 text-sky-800 dark:text-sky-200 rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-sky-50 dark:hover:bg-sky-950/40 disabled:opacity-50"
                    title="Gera texto com Perplexity apenas nos corpos das seções (não altera subseções)"
                  >
                    {perplexityBusy === 'write-sections' ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Perplexity nas seções...
                      </>
                    ) : (
                      <>
                        <Sparkles className="w-3 h-3" />
                        Texto Perplexity — seções
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handlePerplexityWriteSubsectionsBook()}
                    disabled={
                      isMock ||
                      !id ||
                      perplexityBusy !== null ||
                      chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0
                    }
                    className="w-full px-3 py-2 border border-cyan-600/70 dark:border-cyan-500/60 text-cyan-900 dark:text-cyan-100 rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-cyan-50 dark:hover:bg-cyan-950/40 disabled:opacity-50"
                    title="Gera texto com Perplexity apenas nas subseções (não altera o texto das seções)"
                  >
                    {perplexityBusy === 'write-subs' ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Perplexity nas subseções...
                      </>
                    ) : (
                      <>
                        <Sparkles className="w-3 h-3" />
                        Texto Perplexity — subseções
                      </>
                    )}
                  </button>
                  {allSectionsStatus && (
                    <p className={cn(
                      'text-xs mt-1 px-2 py-1 rounded',
                      allSectionsStatus.startsWith('Erro') || allSectionsStatus.startsWith('Falha')
                        ? 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20'
                        : allSectionsStatus.startsWith('Concluído')
                          ? 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20'
                          : 'text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/20'
                    )}>
                      {allSectionsStatus}
                    </p>
                  )}
                  <button
                    type="button"
                    onClick={() => void handlePlanAllSubsectionsInBook()}
                    disabled={isPlanningAllSubsectionsBook || chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 bg-indigo-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-indigo-600 disabled:opacity-50"
                    title="Gera (com IA) as subseções para todas as seções do livro"
                  >
                    {isPlanningAllSubsectionsBook ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando subseções...
                      </>
                    ) : (
                      <>
                        <Wand2 className="w-3 h-3" />
                        Gerar todas as subseções do livro
                      </>
                    )}
                  </button>
                  {planSubsectionsEnqueueProgress && planSubsectionsEnqueueProgress.total > 0 && (
                    <div className="w-full mt-1.5 px-1">
                      <div className="flex justify-between text-[10px] text-indigo-600 dark:text-indigo-400 mb-0.5">
                        <span>Enfileirando jobs</span>
                        <span>{planSubsectionsEnqueueProgress.current}/{planSubsectionsEnqueueProgress.total}</span>
                      </div>
                      <div className="h-1.5 bg-indigo-200 dark:bg-indigo-800 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-indigo-500 dark:bg-indigo-400 transition-all duration-200"
                          style={{ width: `${Math.min(100, (100 * planSubsectionsEnqueueProgress.current) / planSubsectionsEnqueueProgress.total)}%` }}
                        />
                      </div>
                    </div>
                  )}
                  {planSubsectionsJobProgress && planSubsectionsJobProgress.jobIds.length > 0 && (
                    <div className="w-full mt-1.5 px-1">
                      <div className="flex justify-between text-[10px] text-indigo-600 dark:text-indigo-400 mb-0.5">
                        <span>Planejamento subseções</span>
                        <span>{planSubsectionsJobProgress.completed}/{planSubsectionsJobProgress.jobIds.length}</span>
                      </div>
                      <div className="h-1.5 bg-indigo-200 dark:bg-indigo-800 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-indigo-500 dark:bg-indigo-400 transition-all duration-200"
                          style={{ width: `${planSubsectionsJobProgress.jobIds.length ? Math.round((planSubsectionsJobProgress.completed / planSubsectionsJobProgress.jobIds.length) * 100) : 0}%` }}
                        />
                      </div>
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => void handleGenerateAllSubsectionsTextInBook()}
                    disabled={isGeneratingAllSubsectionsTextBook || chapters.every((ch) => (ch.sections || []).every((s) => !(s?.subsections?.length)))}
                    className="w-full px-3 py-2 bg-emerald-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-emerald-600 disabled:opacity-50"
                    title="Gera o texto (com IA) de todas as subseções do livro"
                  >
                    {isGeneratingAllSubsectionsTextBook ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando textos...
                      </>
                    ) : (
                      <>
                        <Sparkles className="w-3 h-3" />
                        Gerar todos os textos das subseções
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={handleClearAllSubsectionsInBook}
                    disabled={chapters.every((ch) => (ch.sections || []).every((s) => !(s?.subsections?.length)))}
                    className="w-full px-3 py-2 bg-red-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-red-600 disabled:opacity-50"
                    title="Apaga todas as subseções de todas as seções do livro"
                  >
                    <Trash2 className="w-3 h-3" />
                    Apagar todas as subseções do livro
                  </button>
                  <button
                    type="button"
                    onClick={handleGenerateAllSectionImages}
                    disabled={isGeneratingAllSectionImages || chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 bg-violet-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-violet-600 disabled:opacity-50"
                    title="Gera uma imagem para cada seção de cada capítulo do livro"
                  >
                    {isGeneratingAllSectionImages ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando imagens...
                      </>
                    ) : (
                      <>
                        <ImageIcon className="w-3 h-3" />
                        Criar todas as imagens do livro
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={handleGenerateOneImagePerChapter}
                    disabled={isGeneratingOneImagePerChapter || isCoverGenerating || chapters.length === 0}
                    className="w-full px-3 py-2 bg-fuchsia-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-fuchsia-600 disabled:opacity-50"
                    title="Gera um divisor (imagem de capítulo) para cada capítulo"
                  >
                    {isGeneratingOneImagePerChapter ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando divisores...
                      </>
                    ) : (
                      <>
                        <ImageIcon className="w-3 h-3" />
                        Gerar 1 imagem por capítulo
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleRenderCharts()}
                    disabled={isRenderingCharts || isMock || chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 border border-amber-300 dark:border-amber-600 rounded-lg text-xs flex items-center justify-center gap-2 text-amber-700 dark:text-amber-300 hover:bg-amber-50 dark:hover:bg-amber-900/30 disabled:opacity-50"
                    title="Gera imagens dos gráficos (blocos ```chart e JSON) e substitui o código pelas imagens em seções e subseções"
                  >
                    {isRenderingCharts ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Montando gráficos...
                      </>
                    ) : (
                      <>
                        <BarChart2 className="w-3 h-3" />
                        Montar gráficos
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleRenderImagePrompts()}
                    disabled={isRenderingImagePrompts || isMock}
                    className="w-full px-3 py-2 border border-emerald-300 text-emerald-700 dark:text-emerald-400 dark:border-emerald-600 rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-emerald-50 dark:hover:bg-emerald-900/20 disabled:opacity-40 disabled:cursor-not-allowed"
                    title="Substitui blocos ```image_prompt ou ```imagem e parágrafos que parecem prompts de imagem pelas imagens geradas (Imagen). Requer API key."
                  >
                    {isRenderingImagePrompts ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando imagens...
                      </>
                    ) : (
                      <>
                        <ImageIcon className="w-3 h-3" />
                        Substituir prompts por imagens
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleReduceToOneImagePerChapter()}
                    disabled={isReducingToOneImagePerChapter || chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 border border-emerald-300 text-emerald-700 dark:text-emerald-400 dark:border-emerald-600 rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-emerald-50 dark:hover:bg-emerald-900/20 disabled:opacity-40 disabled:cursor-not-allowed"
                    title="Mantém apenas a primeira instrução ```image_prompt/```imagem por capítulo; as demais viram texto"
                  >
                    {isReducingToOneImagePerChapter ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Reduzindo instruções...
                      </>
                    ) : (
                      <>
                        <ImageIcon className="w-3 h-3" />
                        Deixar 1 instrução de imagem por capítulo
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleDeleteAllSectionImages()}
                    disabled={isDeletingAllSectionImages || chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 border border-amber-300 text-amber-700 dark:text-amber-400 dark:border-amber-600 rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-amber-50 dark:hover:bg-amber-900/20 disabled:opacity-40 disabled:cursor-not-allowed"
                    title="Remove todas as imagens de todas as seções do livro"
                  >
                    {isDeletingAllSectionImages ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Apagando imagens...
                      </>
                    ) : (
                      <>
                        <Trash2 className="w-3 h-3" />
                        Apagar todas as imagens do livro
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleWriteConclusionChapter()}
                    disabled={isWritingChapter || chapters.length === 0}
                    className="w-full px-3 py-2 bg-sky-500/90 text-white rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-sky-600 disabled:opacity-50"
                    title="Gera o último capítulo do livro (conclusão) com IA"
                  >
                    {isWritingChapter && pendingChapterWrite === chapters.length - 1 ? (
                      <>
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Gerando conclusão...
                      </>
                    ) : (
                      <>
                        <Wand2 className="w-3 h-3" />
                        Gerar capítulo conclusão
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={handleDeleteAllSectionsInBook}
                    disabled={chapters.reduce((a, ch) => a + (ch.sections?.length || 0), 0) === 0}
                    className="w-full px-3 py-2 border border-red-300 text-red-600 dark:text-red-400 dark:border-red-600 rounded-lg text-xs flex items-center justify-center gap-2 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-40 disabled:cursor-not-allowed"
                    title="Remove todas as seções de todos os capítulos do livro"
                  >
                    <Trash2 className="w-3 h-3" />
                    Apagar todas as seções do livro
                  </button>
                </>
              )}
            </div>

            <div className="space-y-6">
              <div className="bg-white dark:bg-gray-800 border rounded-lg p-4">
                <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
                  <span className="text-sm font-semibold text-gray-700 dark:text-gray-200">
                    Rascunho do Livro (contexto para geração de capítulos e seções)
                  </span>
                  {draftPlan && (
                    <div className="flex items-center gap-2">
                      {id && (
                        <button
                          type="button"
                          onClick={handleExtractObjectiveFromDraft}
                          disabled={extractingObjective || !(draftPlan?.draft ?? '').trim()}
                          className="px-3 py-1.5 text-sm rounded-lg border border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-200 dark:hover:bg-amber-900/50 disabled:opacity-50 flex items-center gap-1.5"
                        >
                          {extractingObjective ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                          Extrair objetivos do rascunho
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => savePlan()}
                        disabled={saving}
                        className="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-60 flex items-center gap-1"
                      >
                        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                        Salvar rascunho
                      </button>
                    </div>
                  )}
                </div>
                {draftPlan ? (
                  <MarkdownField
                    value={draftPlan.draft ?? ''}
                    onChange={(v) => setDraftPlan((prev) => (prev ? { ...prev, draft: v } : prev))}
                    placeholder="Cole ou escreva o rascunho do livro. Será usado como contexto em todas as gerações (capítulos, seções, conteúdos)."
                    rows={18}
                    minHeight="420px"
                    showPreview={false}
                    className="text-sm font-mono"
                  />
                ) : (
                  <p className="text-sm text-gray-500 dark:text-gray-400">Carregando plano do livro...</p>
                )}
              </div>
              <UnifiedChat
                title="Chat do Capítulo"
                description="Gerencie capítulos e seções com linguagem natural."
                contextHint={`Capítulo ${selectedChapterIdx + 1}`}
                tools={sectionChatTools}
                placeholder="Ex: criar capítulo, replanejar capítulo, deletar seção 2"
                useAgent={true}
                onActionComplete={handleChatActionComplete}
                agentContext={{ apiKey: getApiKey(job) || undefined, modelName: modelConfig.getDefaultTextModel('full') }}
                agentInstructions="Use as ferramentas para gerenciar capítulos e seções."
                agentMetadata={`Livro: ${draftPlan?.title || job?.topic || ''}\nCapítulo: ${currentChapter?.title || `Capítulo ${selectedChapterIdx + 1}`}\nSeções: ${currentSections.length}`}
                imageModels={modelConfig.getImageModelsForSelect()}
                defaultImageModel={coverModel}
                imageJobId={id}
              />
              <div className="w-full min-w-0 bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-4">
                <div className="flex items-center justify-between">
                  <h2 className="text-lg font-semibold">Detalhes do Capítulo</h2>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={handleRenameBook}
                      className="text-sm flex items-center gap-1 border rounded-md px-3 py-2 hover:bg-gray-50"
                    >
                      <Pencil className="w-4 h-4" />
                      Renomear Livro
                    </button>
                    <DeferredBookPanel>
                      <EpubPreview
                        mode="chapter"
                        jobId={id}
                        apiKey={getApiKey(job)}
                        chapter={currentChapter}
                        chapterNumber={selectedChapterIdx + 1}
                      />
                    </DeferredBookPanel>
                    <button
                      onClick={() => void handleDeleteChapter(selectedChapterIdx)}
                      className="text-sm text-red-500 flex items-center gap-1"
                    >
                      <Trash2 className="w-4 h-4" />
                      Excluir
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="text-xs font-medium text-gray-600">Título</label>
                    <input
                      value={currentChapter?.title || ''}
                      onChange={(e) => handleChapterFieldChange('title', e.target.value)}
                      className="w-full px-3 py-2 border rounded-md text-sm"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-medium text-gray-600">Objetivo</label>
                    <input
                      value={currentChapter?.purpose || ''}
                      onChange={(e) => handleChapterFieldChange('purpose', e.target.value)}
                      className="w-full px-3 py-2 border rounded-md text-sm"
                    />
                  </div>
                </div>
                <div className="rounded-lg border border-blue-100 bg-blue-50/60 px-4 py-3 text-sm text-blue-800">
                  <span className="font-semibold">Objetivo do Capítulo:</span>{' '}
                  {currentChapter?.purpose || 'Defina o objetivo acima para orientar a escrita com IA.'}
                </div>

                {/* Introduction Field (Explicit) */}
                <div>
                  <label className="text-xs font-medium text-gray-600">Introdução (Gerada/Editável)</label>
                  <textarea
                    value={currentChapter?.introduction || ''}
                    onChange={(e) => handleChapterFieldChange('introduction', e.target.value)}
                    rows={4}
                    className="w-full px-3 py-2 border rounded-md text-sm"
                    placeholder="A introdução do capítulo aparecerá aqui..."
                  />
                  <p className="text-[10px] text-gray-400 mt-1">
                    Esta introdução é usada como base para o conteúdo.
                  </p>
                </div>
                {draftPlan && (
                  <AuthorStyleSelector
                    selectedStyles={draftPlan.author_styles || []}
                    onChange={(styles) => setDraftPlan({ ...draftPlan, author_styles: styles })}
                    label="✨ Estilo de escrita (autores)"
                    description="Mesmo estilo do livro definido na barra lateral; aplicado a este capítulo e seções."
                  />
                )}
                <div>
                  <label className="text-xs font-medium text-gray-600">Conteúdo</label>
                  <textarea
                    value={currentChapter?.content || ''}
                    onChange={(e) => handleChapterFieldChange('content', e.target.value)}
                    rows={10}
                    className="w-full px-3 py-2 border rounded-md text-sm font-mono"
                  />
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={handleWriteChapter}
                    disabled={isWritingChapter}
                    className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 disabled:opacity-60"
                  >
                    {isWritingChapter ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Wand2 className="w-4 h-4" />
                    )}
                    {isWritingChapter ? 'Gerando...' : 'Gerar com IA'}
                  </button>
                  <button
                    onClick={handlePlanChapter}
                    className="px-3 py-2 border rounded-md text-sm flex items-center gap-2"
                  >
                    <Sparkles className="w-4 h-4" />
                    Planejar Seções
                  </button>
                  <button
                    onClick={() => handlePlanEpubChapter(selectedChapterIdx)}
                    disabled={isPlanningEpubChapter || currentSections.length === 0}
                    className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 text-slate-700 border-slate-200 hover:bg-slate-50 disabled:opacity-60"
                    title="Tags nas seções: [IMAGE:1], [IMAGE:2] = posição da imagem; [IMAGE: descrição] = próxima na ordem"
                  >
                    {isPlanningEpubChapter ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Sparkles className="w-4 h-4" />
                    )}
                    {isPlanningEpubChapter ? 'Planejando EPUB...' : 'Planejar EPUB'}
                  </button>
                  <button
                    onClick={handleReplanObjectives}
                    disabled={isReplanningObjectives || currentSections.length === 0}
                    className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 text-indigo-600 border-indigo-200 hover:bg-indigo-50 disabled:opacity-60 disabled:cursor-not-allowed"
                    title="Replanejar apenas os objetivos das seções existentes"
                  >
                    {isReplanningObjectives ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <RefreshCw className="w-4 h-4" />
                    )}
                    {isReplanningObjectives ? 'Replanejando...' : '🎯 Replanejar Objetivos'}
                  </button>
                  <button
                    onClick={handleCompileChapterEpub}
                    className="px-3 py-2 border rounded-md text-sm flex items-center gap-2"
                  >
                    <FileDown className="w-4 h-4" />
                    Compilar EPUB
                  </button>
                  <DeferredBookPanel>
                    <EpubPreview
                      mode="chapter"
                      jobId={id}
                      apiKey={getApiKey(job)}
                      chapter={currentChapter}
                      chapterNumber={selectedChapterIdx + 1}
                    />
                  </DeferredBookPanel>
                  {currentChapter?.epub_path && (
                    <a
                      href={buildFileUrl(currentChapter.epub_path)}
                      className="px-3 py-2 border rounded-md text-sm flex items-center gap-2"
                    >
                      <Download className="w-4 h-4" />
                      Baixar EPUB
                    </a>
                  )}
                </div>
                <LogViewer
                  logs={logs}
                  title="📋 Logs de Execução"
                  initiallyExpanded={true}
                  maxHeight="260px"
                  className="mt-4"
                />
              </div>

              <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-4">
                <LogViewer
                  logs={logs}
                  title="Logs de Execução (Seções)"
                  maxHeight="220px"
                  autoScroll={true}
                  initiallyExpanded={true}
                />
                <div className="rounded-lg border border-gray-200 dark:border-gray-600 p-4 space-y-3 bg-gray-50 dark:bg-gray-800/50 mb-4">
                  <label className="text-sm font-semibold text-gray-700 dark:text-gray-300 block">Nova seção a partir de texto</label>
                  <textarea
                    value={newSectionFromText}
                    onChange={(e) => setNewSectionFromText(e.target.value)}
                    placeholder="Descreva a ideia ou o conteúdo da seção. O agente usará o contexto do livro e do capítulo para criar título, objetivo e texto."
                    rows={6}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 resize-y min-h-[120px]"
                  />
                  <button
                    type="button"
                    onClick={handleAddSectionFromText}
                    disabled={!newSectionFromText.trim() || isGeneratingSectionFromPrompt}
                    className="w-full px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    <Plus className="w-4 h-4" />
                    {isGeneratingSectionFromPrompt ? 'Gerando seção...' : 'Criar seção'}
                  </button>
                </div>
                <div className="flex items-center justify-between">
                  <h2 className="text-lg font-semibold">Seções</h2>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => void handleRenderCharts()}
                      disabled={isRenderingCharts || isMock}
                      className="text-sm text-amber-600 dark:text-amber-400 flex items-center gap-1 disabled:opacity-60 border border-amber-300 dark:border-amber-600 rounded-md px-2 py-1.5 hover:bg-amber-50 dark:hover:bg-amber-900/30"
                      title="Gera imagens dos gráficos (blocos ```chart e JSON) em seções e subseções"
                    >
                      {isRenderingCharts ? <Loader2 className="w-4 h-4 animate-spin" /> : <BarChart2 className="w-4 h-4" />}
                      Montar gráficos
                    </button>
                    <button
                      type="button"
                      onClick={handleDeleteAllSectionsInChapter}
                      disabled={(currentSections?.length ?? 0) === 0}
                      className="text-sm text-red-600 dark:text-red-400 flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed"
                      title="Apagar todas as seções deste capítulo"
                    >
                      <Trash2 className="w-4 h-4" />
                      Apagar todas as seções
                    </button>
                    <button
                      onClick={() => handleAddSectionAI()}
                      disabled={isPlanningSection}
                      className="text-sm text-purple-600 flex items-center gap-1 disabled:opacity-60"
                    >
                      {isPlanningSection ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                      {isPlanningSection ? 'Criando...' : 'Criar com IA'}
                    </button>

                    <button
                      onClick={handleAddSection}
                      className="text-sm text-emerald-600 flex items-center gap-1"
                    >
                      <Plus className="w-4 h-4" />
                      Nova seção
                    </button>
                  </div>
                </div>
                {/* Subseções: painel visível logo abaixo do título Seções (antes da lista de cards) */}
                {currentSections.length > 0 && currentSection && (
                  <div className="mb-4 p-4 border border-indigo-200 dark:border-indigo-700 rounded-lg bg-indigo-50/50 dark:bg-indigo-900/20 space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-semibold text-indigo-800 dark:text-indigo-200">Subseções</span>
                      {currentSections.length > 1 && (
                        <select value={selectedSectionIdx} onChange={(e) => setSelectedSectionIdx(Number(e.target.value))} className="text-xs px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800">
                          {currentSections.map((sec, i) => (
                            <option key={i} value={i}>{sec.title || `Seção ${i + 1}`}</option>
                          ))}
                        </select>
                      )}
                      <span className="text-xs text-indigo-600 dark:text-indigo-300 inline-flex items-center gap-0.5">
                        — {currentSection?.title || `Seção ${selectedSectionIdx + 1}`}
                        {translatedUnitKeys.includes(`sec_${selectedChapterIdx}_${selectedSectionIdx}`) && (
                          <span title="Seção traduzida">
                            <Languages className="w-3 h-3 text-indigo-500 dark:text-indigo-400 shrink-0" />
                          </span>
                        )}
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <button type="button" onClick={() => addSubsection()} className="px-2 py-1.5 text-xs border border-gray-300 dark:border-gray-600 rounded-md hover:bg-white dark:hover:bg-gray-800">Adicionar subseção</button>
                      <button type="button" onClick={() => void handlePlanSubsections()} disabled={isPlanningSubsections} className="px-2 py-1.5 text-xs border border-indigo-300 dark:border-indigo-600 rounded-md text-indigo-700 dark:text-indigo-300 hover:bg-indigo-100 dark:hover:bg-indigo-900/40 disabled:opacity-60 flex items-center gap-1">
                        {isPlanningSubsections ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                        Gerar subseções
                      </button>
                      <button type="button" onClick={() => void handleGenerateSubsectionsText()} disabled={isGeneratingSubsectionsText || !(currentSection?.subsections?.length)} className="px-2 py-1.5 text-xs border border-emerald-300 dark:border-emerald-600 rounded-md text-emerald-700 dark:text-emerald-300 hover:bg-emerald-100 dark:hover:bg-emerald-900/40 disabled:opacity-60 flex items-center gap-1">
                        {isGeneratingSubsectionsText ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                        Gerar texto das subseções
                      </button>
                      <button type="button" onClick={() => void handleRenderCharts()} disabled={isRenderingCharts || isMock} className="px-2 py-1.5 text-xs border border-amber-300 dark:border-amber-600 rounded-md text-amber-700 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/40 disabled:opacity-60 flex items-center gap-1" title="Gera imagens dos gráficos (blocos ```chart e JSON) e substitui o código pelas imagens em seções e subseções">
                        {isRenderingCharts ? <Loader2 className="w-3 h-3 animate-spin" /> : <BarChart2 className="w-3 h-3" />}
                        Montar gráficos
                      </button>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-indigo-200 dark:border-indigo-800">
                      <span className="text-xs font-medium text-indigo-700 dark:text-indigo-300">No capítulo:</span>
                      <button type="button" onClick={() => void handlePlanAllSubsectionsInChapter()} disabled={isPlanningAllSubsections || currentSections.length === 0} className="px-2 py-1.5 text-xs border border-indigo-400 dark:border-indigo-500 rounded-md text-indigo-800 dark:text-indigo-200 hover:bg-indigo-100 dark:hover:bg-indigo-900/50 disabled:opacity-60 flex items-center gap-1">
                        {isPlanningAllSubsections ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                        Gerar todas as subseções
                      </button>
                      {planSubsectionsEnqueueProgress && planSubsectionsEnqueueProgress.total > 0 && (
                        <div className="w-full min-w-[120px] flex items-center gap-2">
                          <div className="flex-1 h-1.5 bg-indigo-200 dark:bg-indigo-800 rounded-full overflow-hidden min-w-[60px]">
                            <div
                              className="h-full bg-indigo-500 dark:bg-indigo-400 transition-all duration-200"
                              style={{ width: `${Math.min(100, (100 * planSubsectionsEnqueueProgress.current) / planSubsectionsEnqueueProgress.total)}%` }}
                            />
                          </div>
                          <span className="text-[10px] text-indigo-600 dark:text-indigo-400 tabular-nums">{planSubsectionsEnqueueProgress.current}/{planSubsectionsEnqueueProgress.total}</span>
                        </div>
                      )}
                      {planSubsectionsJobProgress && planSubsectionsJobProgress.jobIds.length > 0 && (
                        <div className="w-full min-w-[120px] flex items-center gap-2">
                          <span className="text-[10px] text-indigo-600 dark:text-indigo-400">Planej.:</span>
                          <div className="flex-1 h-1.5 bg-indigo-200 dark:bg-indigo-800 rounded-full overflow-hidden min-w-[60px]">
                            <div className="h-full bg-indigo-500 dark:bg-indigo-400 transition-all duration-200" style={{ width: `${planSubsectionsJobProgress.jobIds.length ? Math.round((planSubsectionsJobProgress.completed / planSubsectionsJobProgress.jobIds.length) * 100) : 0}%` }} />
                          </div>
                          <span className="text-[10px] text-indigo-600 dark:text-indigo-400 tabular-nums">{planSubsectionsJobProgress.completed}/{planSubsectionsJobProgress.jobIds.length}</span>
                        </div>
                      )}
                      <button type="button" onClick={() => void handleGenerateAllSubsectionsTextInChapter()} disabled={isGeneratingAllSubsectionsText || currentSections.every((s) => !(s?.subsections?.length))} className="px-2 py-1.5 text-xs border border-emerald-400 dark:border-emerald-500 rounded-md text-emerald-800 dark:text-emerald-200 hover:bg-emerald-100 dark:hover:bg-emerald-900/50 disabled:opacity-60 flex items-center gap-1">
                        {isGeneratingAllSubsectionsText ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                        Gerar todos os textos
                      </button>
                      <button type="button" onClick={handleClearAllSubsectionsInChapter} disabled={currentSections.every((s) => !(s?.subsections?.length))} className="px-2 py-1.5 text-xs border border-red-300 dark:border-red-600 rounded-md text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-900/30 disabled:opacity-50 flex items-center gap-1" title="Apagar todas as subseções do capítulo">
                        <Trash2 className="w-3 h-3" />
                        Apagar subseções do capítulo
                      </button>
                    </div>
                  </div>
                )}
                <div className="space-y-4 w-full min-w-0">
                  {currentSections.map((section, idx) => {
                    const sectionObjective = section.purpose || section.objective || section.content_directive || ''
                    const isEditingTitle = editingSectionTitleIdx === idx
                    return (
                      <div key={`section-${idx}`} className="w-full min-w-0 border rounded-lg p-4 space-y-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="space-y-1 flex-1">
                            {isEditingTitle ? (
                              <div className="flex items-center gap-2 flex-wrap">
                                <input
                                  value={editingSectionTitleValue}
                                  onChange={(e) => setEditingSectionTitleValue(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') commitSectionTitleEdit()
                                    if (e.key === 'Escape') { setEditingSectionTitleIdx(null) }
                                  }}
                                  onBlur={commitSectionTitleEdit}
                                  className="text-sm font-semibold px-2 py-1 border rounded w-full max-w-xs dark:bg-gray-800 dark:border-gray-600"
                                  autoFocus
                                />
                                <button type="button" onClick={commitSectionTitleEdit} className="text-xs text-emerald-600">Salvar</button>
                                <button type="button" onClick={() => setEditingSectionTitleIdx(null)} className="text-xs text-gray-500">Cancelar</button>
                              </div>
                            ) : (
                              <div className="flex items-center gap-2">
                                <div className="text-sm font-semibold text-gray-900 dark:text-white flex items-center gap-1.5">
                                  {section.title || `Seção ${idx + 1}`}
                                  {translatedUnitKeys.includes(`sec_${selectedChapterIdx}_${idx}`) && (
                                    <span title="Traduzido">
                                      <Languages className="w-3.5 h-3.5 text-indigo-500 dark:text-indigo-400 shrink-0" />
                                    </span>
                                  )}
                                </div>
                                <button
                                  type="button"
                                  onClick={() => startEditingSectionTitle(idx)}
                                  className="text-gray-400 hover:text-blue-600 p-0.5"
                                  title="Renomear seção"
                                >
                                  <Pencil className="w-3.5 h-3.5" />
                                </button>
                              </div>
                            )}
                            {sectionObjective ? (
                              <div className="flex items-start gap-1.5 mt-1">
                                <span className="text-sm shrink-0">🎯</span>
                                <span className="text-xs text-indigo-700 dark:text-indigo-300 font-medium leading-relaxed">
                                  {sectionObjective}
                                </span>
                              </div>
                            ) : (
                              <div className="flex items-center gap-1.5 mt-1">
                                <span className="text-sm shrink-0">⚠️</span>
                                <span className="text-xs text-amber-600 dark:text-amber-400 italic">
                                  Sem objetivo definido — use "Replanejar Objetivos" para gerar
                                </span>
                              </div>
                            )}
                          </div>
                          <div className="flex items-center gap-2 flex-wrap">
                            <button
                              onClick={() => {
                                setSelectedSectionIdx(idx)
                                setActiveTab('section')
                              }}
                              className="text-xs px-2 py-1 border rounded hover:bg-gray-50"
                            >
                              Detalhes
                            </button>
                            <DeferredBookPanel>
                              <EpubPreview
                                mode="section"
                                jobId={id}
                                apiKey={getApiKey(job)}
                                section={sectionsForEpubPreview[idx]}
                                chapterNumber={selectedChapterIdx + 1}
                                sectionNumber={idx + 1}
                              />
                            </DeferredBookPanel>
                            <button
                              onClick={() => handleWriteSection(idx)}
                              className="text-xs px-2 py-1 border rounded disabled:opacity-60 flex items-center gap-1 hover:bg-blue-50 text-blue-600"
                              disabled={isWritingSectionIndex === idx}
                              title="Gerar conteúdo com IA"
                            >
                              {isWritingSectionIndex === idx ? (
                                <>
                                  <Loader2 className="w-3 h-3 animate-spin" />
                                  Gerando
                                </>
                              ) : (
                                <>
                                  <Wand2 className="w-3 h-3" />
                                  IA
                                </>
                              )}
                            </button>
                            <button
                              onClick={() => handleRewriteSection(idx)}
                              className="text-xs px-2 py-1 border rounded disabled:opacity-60 flex items-center gap-1 hover:bg-orange-50 text-orange-600"
                              disabled={isRewritingSectionIndex === idx || isWritingSectionIndex === idx}
                              title="Reescrever texto da seção com IA"
                            >
                              {isRewritingSectionIndex === idx ? (
                                <>
                                  <Loader2 className="w-3 h-3 animate-spin" />
                                  Reescrevendo
                                </>
                              ) : (
                                <>
                                  <RefreshCw className="w-3 h-3" />
                                  Recriar
                                </>
                              )}
                            </button>
                            <button
                              onClick={() => {
                                if (currentSections.length <= 1) return
                                if (window.confirm(`Excluir a seção "${section.title || `Seção ${idx + 1}`}"?`)) {
                                  handleDeleteSection(idx)
                                }
                              }}
                              disabled={currentSections.length <= 1}
                              className="text-xs px-2 py-1 border rounded text-red-500 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed"
                              title="Excluir seção"
                            >
                              <Trash2 className="w-3 h-3" />
                            </button>
                          </div>
                        </div>
                        {section.content ? (
                          <div className="text-xs text-gray-600 dark:text-gray-400 line-clamp-3 bg-gray-50 dark:bg-gray-900 rounded p-2">
                            {section.content}
                          </div>
                        ) : (
                          <div className="text-xs text-gray-400 italic">
                            Sem conteúdo ainda — clique em "IA" para gerar.
                          </div>
                        )}
                        {/* Subseções: botões e lista por seção (visíveis na tela de edição do livro) */}
                        <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-600 space-y-2 w-full min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-xs font-semibold text-gray-600 dark:text-gray-400">Subseções</span>
                            <div className="flex items-center gap-1">
                              <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400">Qtd:</label>
                              <input
                                type="number"
                                min={2}
                                max={20}
                                value={section.num_subsections_per_section ?? ''}
                                onChange={(e) => {
                                  const raw = e.target.value
                                  const num = raw === '' ? undefined : Math.min(20, Math.max(2, Number(raw) || 2))
                                  updateSectionAtIndex(idx, { num_subsections_per_section: num })
                                }}
                                placeholder="Auto"
                                title="Quantidade exata de subseções a gerar (2-20). Vazio = automático (2-6)"
                                className="w-16 px-1.5 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800"
                              />
                            </div>
                            <button
                              type="button"
                              onClick={() => addSubsection(idx)}
                              className="px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700"
                            >
                              Adicionar
                            </button>
                            <button
                              type="button"
                              onClick={() => void handlePlanSubsections(idx)}
                              disabled={isPlanningSubsections}
                              className="px-2 py-1 text-xs border border-indigo-200 dark:border-indigo-700 rounded-md text-indigo-700 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 disabled:opacity-60 flex items-center gap-1"
                            >
                              {isPlanningSubsections ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                              Gerar subseções
                            </button>
                            <button
                              type="button"
                              onClick={() => void handleGenerateSubsectionsText(idx)}
                              disabled={isGeneratingSubsectionsText || !(section.subsections?.length)}
                              className="px-2 py-1 text-xs border border-emerald-200 dark:border-emerald-700 rounded-md text-emerald-700 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 disabled:opacity-60 flex items-center gap-1"
                            >
                              {isGeneratingSubsectionsText ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                              Gerar texto
                            </button>
                          </div>
                          {((section.subsections?.length ?? 0) > 0) && (
                            <ul className="space-y-2 w-full min-w-0">
                              {(section.subsections || []).map((sub, subIdx) => {
                                const subKey = getSubsectionKey(idx, subIdx)
                                const subPrompts = (sub.slide_prompts || []) as SlidePromptItem[]
                                const isGeneratingSub = generatingSubsectionPromptsKey === `${idx}-${subIdx}`
                                return (
                                <li key={subIdx} className="w-full min-w-0 border border-gray-200 dark:border-gray-600 rounded-md p-3 bg-white dark:bg-gray-800 space-y-2">
                                  <div className="flex items-start gap-2 w-full min-w-0">
                                    <input
                                      value={sub.objective || ''}
                                      onChange={(e) => updateSubsectionAtIndex(idx, subIdx, { objective: e.target.value })}
                                      placeholder="Objetivo"
                                      className="flex-1 min-w-0 px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 w-full"
                                    />
                                    <button
                                      type="button"
                                      onClick={() => removeSubsection(idx, subIdx)}
                                      className="shrink-0 p-1 text-gray-400 hover:text-red-600 dark:hover:text-red-400"
                                      title="Remover subseção"
                                    >
                                      <Trash2 className="w-3 h-3" />
                                    </button>
                                  </div>
                                  <div className="flex flex-wrap items-center gap-3">
                                    <div>
                                      <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 mr-1">Mín. caracteres</label>
                                      <input
                                        type="number"
                                        min={0}
                                        value={sub.min_text_length ?? ''}
                                        onChange={(e) => {
                                          const raw = e.target.value
                                          const num = raw === '' ? undefined : Math.max(0, Number(raw) || 0)
                                          updateSubsectionAtIndex(idx, subIdx, { min_text_length: num })
                                        }}
                                        placeholder="—"
                                        className="w-20 px-1.5 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800"
                                      />
                                    </div>
                                    <label className="flex items-center gap-1.5 cursor-pointer">
                                      <input
                                        type="checkbox"
                                        checked={sub.has_source_code ?? false}
                                        onChange={(e) => updateSubsectionAtIndex(idx, subIdx, { has_source_code: e.target.checked })}
                                        className="h-3 w-3 rounded border-gray-300"
                                      />
                                      <span className="text-xs text-gray-600 dark:text-gray-400">Código fonte</span>
                                    </label>
                                  </div>
                                  <div className="w-full min-w-0">
                                    <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1">Conteúdo (Markdown)</label>
                                    <MarkdownField
                                      value={sub.content || ''}
                                      onChange={(v) => updateSubsectionAtIndex(idx, subIdx, { content: v })}
                                      placeholder="Conteúdo da subseção (edite ou use Gerar texto das subseções). Suporta **negrito**, *itálico*, títulos."
                                      rows={12}
                                      minHeight="14rem"
                                      showPreview={true}
                                      className="text-xs font-mono w-full"
                                    />
                                  </div>
                                  {/* Prompts de slides da subseção (mesmo formato da seção) */}
                                  <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-600 space-y-2 w-full min-w-0">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <span className="text-xs font-semibold text-gray-600 dark:text-gray-400">Prompts de slides</span>
                                      <input
                                        type="number"
                                        min={1}
                                        max={10}
                                        value={sectionSlideCounts[subKey] ?? 3}
                                        onChange={(e) => {
                                          const v = Math.min(10, Math.max(1, Number(e.target.value) || 3))
                                          setSectionSlideCounts((prev) => ({ ...prev, [subKey]: v }))
                                        }}
                                        className="w-10 px-1.5 py-1 border rounded text-xs"
                                      />
                                      <span className="text-xs text-gray-500">slides</span>
                                      <label className="flex items-center gap-1 cursor-pointer">
                                        <input
                                          type="checkbox"
                                          checked={sectionImagesWithoutText[subKey] ?? false}
                                          onChange={(e) =>
                                            setSectionImagesWithoutText((prev) => ({ ...prev, [subKey]: e.target.checked }))
                                          }
                                          className="h-3.5 w-3.5 rounded border-gray-300"
                                        />
                                        <span className="text-xs">Sem texto</span>
                                      </label>
                                      <button
                                        type="button"
                                        onClick={() => void handleGenerateSubsectionPrompts(idx, subIdx)}
                                        disabled={isGeneratingSub}
                                        className="px-2 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1"
                                      >
                                        {isGeneratingSub ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                                        Gerar Prompts
                                      </button>
                                      {subPrompts.length > 0 && (
                                        <button
                                          type="button"
                                          onClick={() => handleClearSubsectionPrompts(idx, subIdx)}
                                          className="px-2 py-1 text-xs text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded flex items-center gap-1"
                                        >
                                          <Trash2 className="w-3 h-3" /> Remover todos
                                        </button>
                                      )}
                                    </div>
                                    <div className="space-y-1.5 mt-2">
                                      <div className="text-xs font-medium text-gray-600 dark:text-gray-400">Estilos visuais (multi-estilo)</div>
                                      <StyleGrid
                                        selectedStyles={(sub as { slide_styles?: string[] }).slide_styles ?? draftPlan?.book_slide_styles ?? []}
                                        onChange={(styles) => updateSubsectionAtIndex(idx, subIdx, { slide_styles: styles })}
                                        maxSelection={10}
                                        showSearch={true}
                                        showCategoryFilter={true}
                                        defaultCategory="all"
                                        columns={4}
                                        cardHeight="100px"
                                      />
                                    </div>
                                    {subPrompts.length > 0 && (
                                      <div className="flex flex-wrap items-center gap-3 py-2 px-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 mt-2">
                                        <label className="flex items-center gap-2 cursor-pointer">
                                          <input
                                            type="checkbox"
                                            checked={sectionCodeSlideNoModel[subKey] ?? false}
                                            onChange={(e) =>
                                              setSectionCodeSlideNoModel((prev) => ({ ...prev, [subKey]: e.target.checked }))
                                            }
                                            className="h-3.5 w-3.5 rounded border-gray-300"
                                          />
                                          <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Texto inserido sem modelo</span>
                                        </label>
                                        <div className="flex items-center gap-2">
                                          <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Modelo para slides:</span>
                                          <select
                                            value={sectionSlideModel[subKey] || imageModels[0]?.id || ''}
                                            onChange={(e) =>
                                              setSectionSlideModel((prev) => ({ ...prev, [subKey]: e.target.value }))
                                            }
                                            className="px-2 py-1 border rounded text-xs bg-white dark:bg-gray-800"
                                          >
                                            {imageModels.length ? (
                                              imageModels.map((m) => (
                                                <option key={m.id} value={m.id}>{m.name}</option>
                                              ))
                                            ) : (
                                              <option value="">Carregando...</option>
                                            )}
                                          </select>
                                        </div>
                                      </div>
                                    )}
                                    {subPrompts.length > 0 && (
                                      <div className="space-y-2">
                                        {subPrompts.map((prompt, pIdx) => (
                                          <div
                                            key={pIdx}
                                            className="rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/60 p-2"
                                          >
                                            <div className="flex items-center justify-between text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">
                                              <span>Slide {prompt.index ?? pIdx + 1}</span>
                                              <button
                                                type="button"
                                                onClick={() => handleDeleteSubsectionPrompt(idx, subIdx, pIdx)}
                                                className="p-0.5 text-gray-400 hover:text-red-600 dark:hover:text-red-400 rounded"
                                                title="Remover este prompt"
                                              >
                                                <Trash2 className="w-3 h-3" />
                                              </button>
                                            </div>
                                            {(prompt.title || prompt.text) && (
                                              <div className="text-xs text-gray-700 dark:text-gray-300 line-clamp-2">
                                                {prompt.title ? `${prompt.title}: ` : ''}{prompt.text ?? ''}
                                              </div>
                                            )}
                                            {(prompt.prompt || prompt.background_prompt) && (
                                              <div className="mt-1 text-[11px] text-gray-500 dark:text-gray-400 rounded bg-white dark:bg-gray-800 px-1.5 py-1 max-h-16 overflow-y-auto">
                                                {prompt.prompt || prompt.background_prompt}
                                              </div>
                                            )}
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                </li>
                                )
                              })}
                            </ul>
                          )}
                        </div>
                      </div>
                    )
                  })}
                  {currentSections.length === 0 && (
                    <p className="text-sm text-gray-500">Nenhuma seção neste capítulo.</p>
                  )}
                </div>

                {/* Subseções na tela de edição do livro (aba Capítulos): criar, gerar textos, deletar */}
                {currentSections.length > 0 && currentSection && (
                  <div className="mt-6 w-full min-w-0 border border-gray-200 dark:border-gray-600 rounded-lg p-4 space-y-3 bg-gray-50/50 dark:bg-gray-900/30">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200">Subseções</h3>
                        {currentSections.length > 1 && (
                          <select
                            value={selectedSectionIdx}
                            onChange={(e) => setSelectedSectionIdx(Number(e.target.value))}
                            className="text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-200"
                          >
                            {currentSections.map((sec, i) => (
                              <option key={i} value={i}>{sec.title || `Seção ${i + 1}`}</option>
                            ))}
                          </select>
                        )}
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          onClick={() => addSubsection()}
                          className="px-2 py-1.5 text-xs border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700"
                        >
                          Adicionar subseção
                        </button>
                        <button
                          type="button"
                          onClick={() => void handlePlanSubsections()}
                          disabled={isPlanningSubsections}
                          className="px-2 py-1.5 text-xs border border-indigo-200 dark:border-indigo-700 rounded-md text-indigo-700 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 disabled:opacity-60 flex items-center gap-1"
                        >
                          {isPlanningSubsections ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                          Gerar subseções
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleGenerateSubsectionsText()}
                          disabled={isGeneratingSubsectionsText || !(currentSection?.subsections?.length)}
                          className="px-2 py-1.5 text-xs border border-emerald-200 dark:border-emerald-700 rounded-md text-emerald-700 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 disabled:opacity-60 flex items-center gap-1"
                        >
                          {isGeneratingSubsectionsText ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                          Gerar texto das subseções
                        </button>
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-gray-200 dark:border-gray-600">
                      <span className="text-xs font-medium text-gray-600 dark:text-gray-400">No capítulo:</span>
                      <button type="button" onClick={() => void handlePlanAllSubsectionsInChapter()} disabled={isPlanningAllSubsections || currentSections.length === 0} className="px-2 py-1.5 text-xs border border-indigo-200 dark:border-indigo-700 rounded-md text-indigo-700 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 disabled:opacity-60 flex items-center gap-1">
                        {isPlanningAllSubsections ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                        Gerar todas as subseções
                      </button>
                      {planSubsectionsEnqueueProgress && planSubsectionsEnqueueProgress.total > 0 && (
                        <div className="min-w-[100px] flex items-center gap-2">
                          <div className="w-20 h-1.5 bg-indigo-200 dark:bg-indigo-800 rounded-full overflow-hidden">
                            <div className="h-full bg-indigo-500 dark:bg-indigo-400 transition-all duration-200" style={{ width: `${Math.min(100, (100 * planSubsectionsEnqueueProgress.current) / planSubsectionsEnqueueProgress.total)}%` }} />
                          </div>
                          <span className="text-[10px] text-indigo-600 dark:text-indigo-400 tabular-nums">{planSubsectionsEnqueueProgress.current}/{planSubsectionsEnqueueProgress.total}</span>
                        </div>
                      )}
                      {planSubsectionsJobProgress && planSubsectionsJobProgress.jobIds.length > 0 && (
                        <div className="min-w-[100px] flex items-center gap-2">
                          <span className="text-[10px] text-indigo-600 dark:text-indigo-400">Planej.:</span>
                          <div className="w-20 h-1.5 bg-indigo-200 dark:bg-indigo-800 rounded-full overflow-hidden">
                            <div className="h-full bg-indigo-500 dark:bg-indigo-400 transition-all duration-200" style={{ width: `${planSubsectionsJobProgress.jobIds.length ? Math.round((planSubsectionsJobProgress.completed / planSubsectionsJobProgress.jobIds.length) * 100) : 0}%` }} />
                          </div>
                          <span className="text-[10px] text-indigo-600 dark:text-indigo-400 tabular-nums">{planSubsectionsJobProgress.completed}/{planSubsectionsJobProgress.jobIds.length}</span>
                        </div>
                      )}
                      <button type="button" onClick={() => void handleGenerateAllSubsectionsTextInChapter()} disabled={isGeneratingAllSubsectionsText || currentSections.every((s) => !(s?.subsections?.length))} className="px-2 py-1.5 text-xs border border-emerald-200 dark:border-emerald-700 rounded-md text-emerald-700 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 disabled:opacity-60 flex items-center gap-1">
                        {isGeneratingAllSubsectionsText ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                        Gerar todos os textos
                      </button>
                      <button type="button" onClick={handleClearAllSubsectionsInChapter} disabled={currentSections.every((s) => !(s?.subsections?.length))} className="px-2 py-1.5 text-xs border border-red-200 dark:border-red-700 rounded-md text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50 flex items-center gap-1" title="Apagar todas as subseções do capítulo">
                        <Trash2 className="w-3 h-3" />
                        Apagar subseções do capítulo
                      </button>
                    </div>
                    {((currentSection?.subsections?.length ?? 0) > 0) ? (
                      <ul className="space-y-3 w-full min-w-0">
                        {(currentSection?.subsections || []).map((sub, subIdx) => (
                          <li key={subIdx} className="w-full min-w-0 border border-gray-200 dark:border-gray-600 rounded-md p-3 bg-white dark:bg-gray-800 space-y-2">
                            <div className="flex items-start gap-2 w-full min-w-0">
                              <span className="text-xs font-medium text-gray-500 dark:text-gray-400 shrink-0 pt-1.5">Objetivo</span>
                              <input
                                value={sub.objective || ''}
                                onChange={(e) => updateSubsectionAtIndex(undefined, subIdx, { objective: e.target.value })}
                                placeholder="Objetivo desta subseção"
                                className="flex-1 min-w-0 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800"
                              />
                              <button
                                type="button"
                                onClick={() => removeSubsection(undefined, subIdx)}
                                className="shrink-0 p-1 text-gray-400 hover:text-red-600 dark:hover:text-red-400"
                                title="Remover subseção"
                              >
                                <Trash2 className="w-4 h-4" />
                              </button>
                            </div>
                            <div className="flex flex-wrap items-center gap-4">
                              <div>
                                <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1">Mín. caracteres</label>
                                <input
                                  type="number"
                                  min={0}
                                  value={sub.min_text_length ?? ''}
                                  onChange={(e) => {
                                    const raw = e.target.value
                                    const num = raw === '' ? undefined : Math.max(0, Number(raw) || 0)
                                    updateSubsectionAtIndex(undefined, subIdx, { min_text_length: num })
                                  }}
                                  placeholder="Padrão do livro"
                                  className="w-24 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800"
                                />
                              </div>
                              <label className="flex items-center gap-2 cursor-pointer pt-5">
                                <input
                                  type="checkbox"
                                  checked={sub.has_source_code ?? false}
                                  onChange={(e) => updateSubsectionAtIndex(undefined, subIdx, { has_source_code: e.target.checked })}
                                  className="h-3.5 w-3.5 rounded border-gray-300"
                                />
                                <span className="text-xs text-gray-600 dark:text-gray-400">Incluir código fonte</span>
                              </label>
                            </div>
                            <div className="w-full min-w-0">
                              <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1">Texto</label>
                              <textarea
                                value={sub.content || ''}
                                onChange={(e) => updateSubsectionAtIndex(undefined, subIdx, { content: e.target.value })}
                                placeholder="Conteúdo da subseção (edite ou use Gerar texto das subseções)"
                                rows={3}
                                className="w-full min-w-0 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 resize-y"
                              />
                            </div>
                            {/* Slides da subseção (gerar prompts, gerar slides, preview) */}
                            <div className="border-t border-gray-200 dark:border-gray-600 pt-3 mt-3 space-y-2">
                              <div className="flex flex-wrap items-center gap-2">
                                <h4 className="text-xs font-semibold text-gray-600 dark:text-gray-400">Slides</h4>
                                <input
                                  type="number"
                                  min={1}
                                  max={10}
                                  value={sectionSlideCounts[getSubsectionKey(selectedSectionIdx, subIdx)] ?? 3}
                                  onChange={(e) => {
                                    const v = Math.min(10, Math.max(1, Number(e.target.value) || 3))
                                    setSectionSlideCounts((prev) => ({ ...prev, [getSubsectionKey(selectedSectionIdx, subIdx)]: v }))
                                  }}
                                  className="w-10 px-1 py-0.5 border rounded text-xs"
                                />
                                <span className="text-xs text-gray-500">slides</span>
                                <label className="flex items-center gap-1 cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={sectionImagesWithoutText[getSubsectionKey(selectedSectionIdx, subIdx)] ?? false}
                                    onChange={(e) => setSectionImagesWithoutText((prev) => ({ ...prev, [getSubsectionKey(selectedSectionIdx, subIdx)]: e.target.checked }))}
                                    className="h-3 w-3 rounded border-gray-300"
                                  />
                                  <span className="text-xs">Sem texto</span>
                                </label>
                                <div className="flex items-center gap-1.5">
                                  <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Modelo:</span>
                                  <select
                                    value={sectionSlideModel[getSubsectionKey(selectedSectionIdx, subIdx)] || imageModels[0]?.id || ''}
                                    onChange={(e) =>
                                      setSectionSlideModel((prev) => ({
                                        ...prev,
                                        [getSubsectionKey(selectedSectionIdx, subIdx)]: e.target.value,
                                      }))
                                    }
                                    className="px-2 py-1 border rounded text-xs bg-white dark:bg-gray-800"
                                    title="Modelo de imagem para gerar slides desta subseção"
                                  >
                                    {imageModels.length ? (
                                      imageModels.map((m) => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                      ))
                                    ) : (
                                      <option value="">Carregando...</option>
                                    )}
                                  </select>
                                </div>
                                <button
                                  type="button"
                                  onClick={() => void handleGenerateSubsectionPrompts(selectedSectionIdx, subIdx)}
                                  disabled={generatingSubsectionPromptsKey === `${selectedSectionIdx}-${subIdx}`}
                                  className="px-2 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1"
                                >
                                  {generatingSubsectionPromptsKey === `${selectedSectionIdx}-${subIdx}` ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                                  Gerar prompts
                                </button>
                                {(sub.slide_prompts?.length ?? 0) > 0 && (
                                  <>
                                    <button
                                      type="button"
                                      onClick={() => void handleGenerateSubsectionSlides(selectedSectionIdx, subIdx)}
                                      disabled={isGeneratingSubsectionSlidesKey !== null}
                                      className="px-2 py-1 text-xs bg-emerald-600 text-white rounded hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-1"
                                      title="Gerar imagens dos slides"
                                    >
                                      {isGeneratingSubsectionSlidesKey === getSubsectionKey(selectedSectionIdx, subIdx) ? <Loader2 className="w-3 h-3 animate-spin" /> : <Layers className="w-3 h-3" />}
                                      Gerar slides
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => handleClearSubsectionPrompts(selectedSectionIdx, subIdx)}
                                      className="px-2 py-1 text-xs text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded flex items-center gap-1"
                                    >
                                      <Trash2 className="w-3 h-3" /> Remover
                                    </button>
                                  </>
                                )}
                              </div>
                              {(() => {
                                const subKey = getSubsectionKey(selectedSectionIdx, subIdx)
                                const subPrompts = (sub.slide_prompts || []) as SlidePromptItem[]
                                const subImages = sub?.images || []
                                const isSlideImg = (img: unknown) =>
                                  typeof img === 'object' && img !== null && ((img as { source?: string }).source === 'slide' || (img as { caption?: string }).caption?.startsWith('Slide '))
                                const slideEntries = subImages.filter(isSlideImg).map((img, j) => ({
                                  path: (img as { path: string }).path,
                                  caption: (img as { caption?: string }).caption ?? `Slide ${j + 1}`,
                                }))
                                const displaySlides = slideEntries.map((slide, i) => ({
                                  ...slide,
                                  prompt: (subPrompts[i]?.prompt ?? subPrompts[i]?.text ?? '').trim() || undefined,
                                }))
                                return (subPrompts.length > 0 || displaySlides.length > 0) ? (
                                  <div className="rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-2">
                                    <div className="mb-2 flex justify-end">
                                      {renderInsertBlankSlideButton(
                                        () => handleCreateBlankSubsectionSlide(selectedSectionIdx, subIdx),
                                        creatingBlankSubsectionSlideKey === subKey,
                                      )}
                                    </div>
                                    <DeferredBookPanel>
                                      <DeferredBookPanel>
                                        <LessonSlidePreview
                                          slides={displaySlides}
                                          isGenerating={isGeneratingSubsectionSlidesKey === subKey}
                                          expectedCount={subPrompts.length}
                                          onMove={(fromIndex: number, toIndex: number) =>
                                            handleMoveSubsectionSlide(selectedSectionIdx, subIdx, fromIndex, toIndex)
                                          }
                                          onEdit={(slideIdx: number, path: string) =>
                                            openBookImageEditor({
                                              scope: 'subsection',
                                              kind: 'slide',
                                              chapterIdx: selectedChapterIdx,
                                              sectionIdx: selectedSectionIdx,
                                              subsectionIdx: subIdx,
                                              imagePath: path,
                                              title: `${sub.title || sub.objective || `Subseção ${subIdx + 1}`} — slide ${slideIdx + 1}`,
                                              caption: displaySlides[slideIdx]?.caption ?? `Slide ${slideIdx + 1}`,
                                            })
                                          }
                                          onDelete={async (slideIdx: number, path: string) => {
                                            setSubsectionSlideDeletingIndex(slideIdx)
                                            await handleDeleteSubsectionSlideImage(selectedSectionIdx, subIdx, path)
                                            setSubsectionSlideDeletingIndex(null)
                                          }}
                                          deletingIndex={subsectionSlideDeletingIndex}
                                        />
                                      </DeferredBookPanel>
                                    </DeferredBookPanel>
                                    {displaySlides.length === 0 && isGeneratingSubsectionSlidesKey !== subKey && (
                                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Nenhum slide gerado. Clique em &quot;Gerar slides&quot;.</p>
                                    )}
                                  </div>
                                ) : null
                              })()}
                            </div>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-xs text-gray-500 dark:text-gray-400">Nenhuma subseção. Clique em &quot;Adicionar subseção&quot; ou &quot;Gerar subseções&quot; (IA).</p>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )
      }

      {/* Aba Subseções: mesma estrutura da tela de seções (sidebar + editor), ocupa toda a tela */}
      {activeTab === 'subsections' && (
        <div className="flex flex-col min-h-[calc(100vh-14rem)] w-full">
          {chapters.length === 0 ? (
            <p className="text-sm text-amber-600 dark:text-amber-400">Crie pelo menos um capítulo na aba Capítulos.</p>
          ) : currentSections.length === 0 ? (
            <p className="text-sm text-amber-600 dark:text-amber-400">O capítulo selecionado não tem seções. Adicione seções na aba Capítulos.</p>
          ) : (
            <div className="flex flex-col flex-1 min-h-0 w-full">
              {/* Barra superior fixa (igual à aba Seção): navegação + ações + EpubPreview */}
              <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 py-3 px-4 -mx-4 mt-2 mb-2 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 rounded-lg shadow-sm">
                <div className="flex items-center gap-3 min-w-0">
                  <button
                    onClick={() => setActiveTab('chapters')}
                    className="flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors shrink-0"
                  >
                    <ArrowLeft className="w-4 h-4" />
                    Voltar
                  </button>
                  <span className="text-sm text-gray-500 dark:text-gray-400 truncate hidden sm:inline flex items-center gap-1 flex-wrap">
                    <span className="inline-flex items-center gap-0.5">
                      {currentChapter?.title || `Capítulo ${selectedChapterIdx + 1}`}
                      {translatedUnitKeys.includes(`ch_${selectedChapterIdx}`) && (
                        <span title="Capítulo traduzido">
                          <Languages className="w-3.5 h-3.5 text-indigo-500 dark:text-indigo-400 shrink-0" />
                        </span>
                      )}
                    </span>
                    <span>•</span>
                    <span className="inline-flex items-center gap-0.5">
                      {currentSection?.title || `Seção ${selectedSectionIdx + 1}`}
                      {translatedUnitKeys.includes(`sec_${selectedChapterIdx}_${selectedSectionIdx}`) && (
                        <span title="Seção traduzida">
                          <Languages className="w-3.5 h-3.5 text-indigo-500 dark:text-indigo-400 shrink-0" />
                        </span>
                      )}
                    </span>
                    {((currentSection?.subsections?.length ?? 0) > 0) && (
                      <>
                        <span>•</span>
                        <span className="inline-flex items-center gap-0.5">
                          Subseção {selectedSubsectionIdx + 1}
                          {translatedUnitKeys.includes(`sub_${selectedChapterIdx}_${selectedSectionIdx}_${selectedSubsectionIdx}`) && (
                            <span title="Subseção traduzida">
                              <Languages className="w-3.5 h-3.5 text-indigo-500 dark:text-indigo-400 shrink-0" />
                            </span>
                          )}
                        </span>
                      </>
                    )}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => savePlan()}
                    className="px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium flex items-center gap-2 hover:bg-emerald-700 transition-colors"
                  >
                    <Save className="w-4 h-4" />
                    Salvar
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleRenderCharts()}
                    disabled={isRenderingCharts || isMock}
                    className="px-3 py-2 border border-amber-300 dark:border-amber-600 rounded-lg text-sm font-medium text-amber-700 dark:text-amber-300 hover:bg-amber-50 dark:hover:bg-amber-900/30 disabled:opacity-60 flex items-center gap-2 transition-colors"
                    title="Gera imagens dos gráficos (blocos ```chart e JSON) e substitui o código pelas imagens em seções e subseções"
                  >
                    {isRenderingCharts ? <Loader2 className="w-4 h-4 animate-spin" /> : <BarChart2 className="w-4 h-4" />}
                    Montar gráficos
                  </button>
                  {subsectionForEpubPreview && (
                    <DeferredBookPanel>
                      <EpubPreview
                        mode="section"
                        jobId={id}
                        apiKey={getApiKey(job)}
                        section={subsectionForEpubPreview}
                        chapterNumber={selectedChapterIdx + 1}
                        sectionNumber={selectedSectionIdx + 1}
                      />
                    </DeferredBookPanel>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      if (window.confirm(`Excluir a subseção ${selectedSubsectionIdx + 1}?`)) {
                        const newLen = (currentSection?.subsections?.length ?? 1) - 1
                        removeSubsection(undefined, selectedSubsectionIdx)
                        setSelectedSubsectionIdx(Math.max(0, Math.min(selectedSubsectionIdx, newLen - 1)))
                      }
                    }}
                    disabled={(currentSection?.subsections?.length ?? 0) <= 1}
                    className="px-3 py-2 border border-red-200 dark:border-red-800 rounded-lg text-sm font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 disabled:opacity-40 flex items-center gap-2 transition-colors"
                  >
                    <Trash2 className="w-4 h-4" />
                    Excluir
                  </button>
                </div>
              </div>
              <div className="rounded-lg border border-sky-200 dark:border-sky-800 bg-sky-50/80 dark:bg-sky-950/30 p-4 space-y-2">
                <div className="text-sm font-semibold text-sky-900 dark:text-sky-100 flex items-center gap-2">
                  <Search className="w-4 h-4 shrink-0" />
                  Perplexity — esta subseção
                </div>
                <p className="text-xs text-sky-800/90 dark:text-sky-200/80">
                  Chave em{' '}
                  <button
                    type="button"
                    onClick={() => navigate('/settings')}
                    className="underline font-medium text-sky-900 dark:text-sky-100 hover:opacity-80"
                  >
                    Configurações
                  </button>
                  . Não altera o texto: grava fontes na base e acrescenta citações [n] ao fim de cada parágrafo.
                </p>
                <button
                  type="button"
                  onClick={() => void handlePerplexityEnrichSubsection()}
                  disabled={
                    isMock ||
                    !id ||
                    !currentSection ||
                    (currentSection.subsections?.length ?? 0) === 0 ||
                    perplexityBusy !== null
                  }
                  className="px-3 py-2 border border-sky-300 dark:border-sky-600 rounded-lg text-sm font-medium text-sky-800 dark:text-sky-200 hover:bg-sky-100 dark:hover:bg-sky-900/40 disabled:opacity-50 flex items-center gap-2"
                >
                  {perplexityBusy === 'enrich-sub' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                  Fontes nesta subseção
                </button>
              </div>
              <div className="grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)] gap-6 flex-1 min-h-0">
              {/* Sidebar: capítulo, seção, lista de subseções (igual à lista de seções) */}
              <div className="bg-white dark:bg-gray-800 border rounded-lg p-4 space-y-3 flex flex-col min-h-0 min-w-[260px]">
                <div className="flex items-center justify-between">
                  <h2 className="text-sm font-semibold text-gray-900 dark:text-white">Subseções</h2>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => void handlePlanSubsections()}
                      disabled={isPlanningSubsections}
                      className="text-xs text-purple-600 dark:text-purple-400 flex items-center gap-1 hover:bg-purple-50 dark:hover:bg-purple-900/30 px-2 py-1 rounded border border-purple-200 dark:border-purple-700 disabled:opacity-50"
                    >
                      {isPlanningSubsections ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                      IA
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        addSubsection()
                        setSelectedSubsectionIdx(currentSection?.subsections?.length ?? 0)
                      }}
                      className="text-xs text-emerald-600 dark:text-emerald-400 flex items-center gap-1 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 px-2 py-1 rounded border border-emerald-200 dark:border-emerald-700"
                    >
                      <Plus className="w-3 h-3" />
                      Adicionar
                    </button>
                  </div>
                </div>
                <select
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700"
                  value={selectedChapterIdx}
                  onChange={(e) => {
                    setSelectedChapterIdx(Number(e.target.value))
                    setSelectedSectionIdx(0)
                    setSelectedSubsectionIdx(0)
                  }}
                >
                  {chapters.map((ch, i) => (
                    <option key={i} value={i}>{ch.title || `Capítulo ${i + 1}`}</option>
                  ))}
                </select>
                <select
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700"
                  value={selectedSectionIdx}
                  onChange={(e) => {
                    setSelectedSectionIdx(Number(e.target.value))
                    setSelectedSubsectionIdx(0)
                  }}
                >
                  {currentSections.map((sec, i) => (
                    <option key={i} value={i}>{sec.title || `Seção ${i + 1}`}</option>
                  ))}
                </select>
                <div className="space-y-2 flex-1 min-h-0 overflow-y-auto">
                  {(currentSection?.subsections || []).map((sub, idx) => (
                    <button
                      key={idx}
                      type="button"
                      onClick={() => setSelectedSubsectionIdx(idx)}
                      className={cn(
                        'w-full text-left px-3 py-2 rounded-lg border text-sm flex items-center justify-between gap-2',
                        idx === selectedSubsectionIdx
                          ? 'border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:border-blue-600'
                          : 'border-gray-200 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-700/50'
                      )}
                    >
                      <span className="truncate flex-1">Subseção {idx + 1}</span>
                      {translatedUnitKeys.includes(`sub_${selectedChapterIdx}_${selectedSectionIdx}_${idx}`) && (
                        <span title="Traduzido">
                          <Languages className="w-3.5 h-3.5 shrink-0 text-indigo-500 dark:text-indigo-400" />
                        </span>
                      )}
                      {((sub.content?.trim() ?? '') || (sub.slide_prompts?.length ?? 0) > 0 || (sub.images?.length ?? 0) > 0) ? (
                        <span className="shrink-0 flex items-center gap-0.5">
                          {(sub.images?.length ?? 0) > 0 ? (
                            <span className="text-amber-500 dark:text-amber-400" title="Subseção tem imagem(ns)">
                              <ImageIcon className="w-3.5 h-3.5" />
                            </span>
                          ) : null}
                          <span className="text-emerald-500 dark:text-emerald-400" title="Subseção tem texto ou imagem">
                            <FileText className="w-3.5 h-3.5" />
                          </span>
                        </span>
                      ) : null}
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation()
                          if (window.confirm('Remover esta subseção?')) removeSubsection(undefined, idx)
                          if (idx === selectedSubsectionIdx) setSelectedSubsectionIdx(Math.max(0, idx - 1))
                        }}
                        className="text-gray-400 hover:text-red-600 dark:hover:text-red-400 shrink-0 p-0.5"
                        title="Excluir subseção"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </button>
                  ))}
                  <button
                    type="button"
                    onClick={() => {
                      addSubsection()
                      setSelectedSubsectionIdx(currentSection?.subsections?.length ?? 0)
                    }}
                    className="w-full mt-2 px-3 py-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700/50 flex items-center justify-center gap-1"
                  >
                    <Plus className="w-4 h-4" />
                    Nova subseção
                  </button>
                </div>
                <div className="border-t border-gray-200 dark:border-gray-600 pt-3 space-y-2">
                  <span className="text-xs font-medium text-gray-600 dark:text-gray-400">No capítulo:</span>
                  <div className="flex flex-wrap gap-1">
                    <button type="button" onClick={() => void handlePlanAllSubsectionsInChapter()} disabled={isPlanningAllSubsections || currentSections.length === 0} className="px-2 py-1 text-xs border border-indigo-300 dark:border-indigo-600 rounded text-indigo-700 dark:text-indigo-300 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 disabled:opacity-60">
                      {isPlanningAllSubsections ? <Loader2 className="w-3 h-3 inline animate-spin" /> : null} Gerar todas
                    </button>
                    <button type="button" onClick={() => void handleGenerateAllSubsectionsTextInChapter()} disabled={isGeneratingAllSubsectionsText || currentSections.every((s) => !(s?.subsections?.length))} className="px-2 py-1 text-xs border border-emerald-300 dark:border-emerald-600 rounded text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 disabled:opacity-60">
                      {isGeneratingAllSubsectionsText ? <Loader2 className="w-3 h-3 inline animate-spin" /> : null} Gerar textos
                    </button>
                    <button type="button" onClick={handleClearAllSubsectionsInChapter} disabled={currentSections.every((s) => !(s?.subsections?.length))} className="px-2 py-1 text-xs border border-red-300 dark:border-red-600 rounded text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50" title="Apagar todas as subseções">
                      <Trash2 className="w-3 h-3 inline" />
                    </button>
                  </div>
                </div>
              </div>

              {/* Main: editor da subseção selecionada (igual ao editor de seção) */}
              <div className="space-y-6 min-w-0 min-h-0 overflow-auto flex-1 flex flex-col">
                {((currentSection?.subsections?.length ?? 0) === 0) ? (
                  <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 text-center">
                    <p className="text-gray-500 dark:text-gray-400 mb-4">Nenhuma subseção. Use &quot;Adicionar&quot; ou &quot;Gerar subseções (IA)&quot; na barra lateral.</p>
                    <button
                      type="button"
                      onClick={() => {
                        addSubsection()
                        setSelectedSubsectionIdx(0)
                      }}
                      className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700 flex items-center gap-2 mx-auto"
                    >
                      <Plus className="w-4 h-4" />
                      Adicionar subseção
                    </button>
                  </div>
                ) : (() => {
                  const sub = currentSection?.subsections?.[selectedSubsectionIdx]
                  const secIdx = selectedSectionIdx
                  const subIdx = selectedSubsectionIdx
                  const subKey = getSubsectionKey(secIdx, subIdx)
                  const subPrompts = (sub?.slide_prompts || []) as SlidePromptItem[]
                  const isGeneratingSub = generatingSubsectionPromptsKey === `${secIdx}-${subIdx}`
                  if (!sub) return null
                  return (
                    <div className="space-y-6">
                      <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-4">
                        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Editor Avançado</h2>
                          <div className="flex flex-wrap items-center gap-2">
                            <button
                              type="button"
                              onClick={() => void handleGenerateSubsectionsText()}
                              disabled={isGeneratingSubsectionsText}
                              className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 disabled:opacity-60"
                            >
                              {isGeneratingSubsectionsText ? <Loader2 className="w-4 h-4 animate-spin" /> : <Wand2 className="w-4 h-4" />}
                              Gerar texto (IA)
                            </button>
                            <button
                              type="button"
                              onClick={() => savePlan()}
                              className="px-3 py-2 bg-emerald-600 text-white rounded-md text-sm flex items-center gap-2"
                            >
                              <Save className="w-4 h-4" />
                              Salvar
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                if (window.confirm(`Excluir a subseção ${selectedSubsectionIdx + 1}?`)) {
                                  const newLen = (currentSection?.subsections?.length ?? 1) - 1
                                  removeSubsection(undefined, selectedSubsectionIdx)
                                  setSelectedSubsectionIdx(Math.max(0, Math.min(selectedSubsectionIdx, newLen - 1)))
                                }
                              }}
                              disabled={(currentSection?.subsections?.length ?? 0) <= 1}
                              className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-40"
                            >
                              <Trash2 className="w-4 h-4" />
                              Excluir
                            </button>
                          </div>
                        </div>

                        <div className="grid grid-cols-1 gap-4 w-full min-w-0">
                          <div className="w-full min-w-0">
                            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-2">Dados (Excel/CSV)</label>
                            <div className="flex flex-wrap items-center gap-2">
                              <input
                                id={`subsection-data-file-${subKey}`}
                                type="file"
                                accept=".csv,.xlsx"
                                className="hidden"
                                onChange={(e) => {
                                  const f = e.target.files?.[0]
                                  if (f) {
                                    void handleSubsectionDataFileUpload(f)
                                    e.target.value = ''
                                  }
                                }}
                              />
                              <button
                                type="button"
                                onClick={() => document.getElementById(`subsection-data-file-${subKey}`)?.click()}
                                disabled={subsectionDataFileUploadKey === subKey}
                                className="inline-flex items-center gap-2 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-60"
                                title="Enviar planilha Excel ou CSV; os dados serão convertidos em tabela Markdown e um texto explicativo será adicionado ao conteúdo."
                              >
                                {subsectionDataFileUploadKey === subKey ? (
                                  <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                  <>
                                    <Upload className="w-4 h-4" />
                                    <Table className="w-4 h-4" />
                                  </>
                                )}
                                Subir Excel/CSV
                              </button>
                              <span className="text-xs text-gray-500 dark:text-gray-400">
                                Converte para tabela Markdown e adiciona texto explicativo ao conteúdo.
                              </span>
                            </div>
                          </div>
                          <div className="w-full min-w-0">
                            <MarkdownField
                              label="Objetivo da Subseção"
                              value={sub.objective || ''}
                              onChange={(v) => updateSubsectionAtIndex(undefined, subIdx, { objective: v })}
                              placeholder="Objetivo desta subseção"
                              rows={2}
                              showPreview={true}
                            />
                          </div>
                          <div className="flex flex-wrap items-end gap-4">
                            <div>
                              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Mín. caracteres</label>
                              <input
                                type="number"
                                min={0}
                                value={sub.min_text_length ?? ''}
                                onChange={(e) => {
                                  const raw = e.target.value
                                  const num = raw === '' ? undefined : Math.max(0, Number(raw) || 0)
                                  updateSubsectionAtIndex(undefined, subIdx, { min_text_length: num })
                                }}
                                placeholder="Padrão do livro"
                                className="w-28 px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700"
                              />
                            </div>
                            <label className="flex items-center gap-2 cursor-pointer pb-1">
                              <input
                                type="checkbox"
                                checked={sub.has_source_code ?? false}
                                onChange={(e) => updateSubsectionAtIndex(undefined, subIdx, { has_source_code: e.target.checked })}
                                className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
                              />
                              <span className="text-sm text-gray-700 dark:text-gray-300">Incluir código fonte</span>
                            </label>
                          </div>
                        </div>

                        <div className="w-full min-w-0">
                          <MarkdownField
                            label="Conteúdo (Markdown)"
                            value={sub.content || ''}
                            onChange={(v) => updateSubsectionAtIndex(undefined, subIdx, { content: v })}
                            placeholder="Conteúdo da subseção (edite ou use Gerar texto das subseções). Suporta **negrito**, *itálico*, títulos."
                            rows={28}
                            minHeight="32rem"
                            showPreview={true}
                            className="text-sm font-mono w-full"
                          />
                        </div>

                        <AuthorStyleSelector
                          selectedStyles={sub.author_styles || []}
                          onChange={(styles) => updateSubsectionAtIndex(undefined, subIdx, { author_styles: styles })}
                          label="✨ Estilos de Autor"
                          description="Selecione estilos para orientar a escrita desta subseção"
                        />
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={handleApplyAuthorStylesToSubsection}
                            disabled={isApplyingAuthorStylesToSubsection || !(sub.author_styles || []).length}
                            className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 disabled:opacity-60"
                          >
                            {isApplyingAuthorStylesToSubsection ? (
                              <>
                                <Loader2 className="w-4 h-4 animate-spin" />
                                Aplicando estilos...
                              </>
                            ) : (
                              <>
                                <Wand2 className="w-4 h-4" />
                                Aplicar estilos no texto
                              </>
                            )}
                          </button>
                          <span className="text-xs text-gray-500">Reescreve o conteúdo com os estilos selecionados</span>
                        </div>

                        {/* Painel de imagens/slides da subseção (igual ao da seção) */}
                        <SectionImageGeneratorPanel
                          title="🖼️ Imagens da Subseção"
                          countLabel={subPrompts.length > 0 ? `${(sub?.images || []).filter((img: unknown) => typeof img === 'object' && img !== null && ((img as { source?: string }).source === 'slide' || (img as { caption?: string }).caption?.startsWith('Slide '))).length}/${subPrompts.length} slides` : undefined}
                          preview={
                            <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-4">
                              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                                <h4 className="text-xs font-semibold text-gray-600 dark:text-gray-400 uppercase">Preview dos slides</h4>
                                {renderInsertBlankSlideButton(
                                  () => handleCreateBlankSubsectionSlide(secIdx, subIdx),
                                  creatingBlankSubsectionSlideKey === subKey,
                                )}
                              </div>
                              {subPrompts.length > 0 ? (() => {
                                const subImages = sub?.images || []
                                const isSlideImg = (img: unknown) =>
                                  typeof img === 'object' && img !== null && ((img as { source?: string }).source === 'slide' || (img as { caption?: string }).caption?.startsWith('Slide '))
                                const slideEntries = subImages.filter(isSlideImg).map((img, j) => ({
                                  path: (img as { path: string }).path,
                                  caption: (img as { caption?: string }).caption ?? `Slide ${j + 1}`,
                                }))
                                const displaySlides = slideEntries.map((slide, i) => ({
                                  ...slide,
                                  prompt: (subPrompts[i]?.prompt ?? subPrompts[i]?.text ?? '').trim() || undefined,
                                }))
                                return (
                                  <>
                                    <DeferredBookPanel>
                                      <LessonSlidePreview
                                        slides={displaySlides}
                                        isGenerating={isGeneratingSubsectionSlidesKey === subKey}
                                        expectedCount={subPrompts.length}
                                        onMove={(fromIndex: number, toIndex: number) =>
                                          handleMoveSubsectionSlide(secIdx, subIdx, fromIndex, toIndex)
                                        }
                                        onDelete={async (slideIdx: number, path: string) => {
                                          setSubsectionSlideDeletingIndex(slideIdx)
                                          await handleDeleteSubsectionSlideImage(secIdx, subIdx, path)
                                          setSubsectionSlideDeletingIndex(null)
                                        }}
                                        deletingIndex={subsectionSlideDeletingIndex}
                                      />
                                    </DeferredBookPanel>
                                    {displaySlides.length === 0 && !(isGeneratingSubsectionSlidesKey === subKey) && (
                                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">Nenhum slide gerado ainda. Clique em &quot;Gerar slides&quot; abaixo.</p>
                                    )}
                                  </>
                                )
                              })() : (
                                <p className="text-xs text-gray-500 dark:text-gray-400">Nenhum slide ainda. Defina a quantidade de slides e clique em &quot;Gerar Prompts&quot; abaixo.</p>
                              )}
                              <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
                                <h4 className="text-xs font-semibold text-gray-600 dark:text-gray-400 uppercase mb-2">Imagens da subseção</h4>
                                <div
                                  role="button"
                                  tabIndex={0}
                                  className={`rounded-lg border-2 border-dashed p-4 text-center transition-colors ${subsectionDropZoneKey === subKey ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/20' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'}`}
                                  onDragEnter={(e) => { e.preventDefault(); e.stopPropagation(); setSubsectionDropZoneKey(subKey) }}
                                  onDragOver={(e) => { e.preventDefault(); e.stopPropagation() }}
                                  onDragLeave={(e) => { e.preventDefault(); e.stopPropagation(); setSubsectionDropZoneKey(null) }}
                                  onDrop={(e) => {
                                    e.preventDefault()
                                    e.stopPropagation()
                                    setSubsectionDropZoneKey(null)
                                    const files = e.dataTransfer?.files
                                    if (files) {
                                      for (let i = 0; i < files.length; i++) {
                                        const f = files[i]
                                        if (f.type.startsWith('image/')) void handleUploadSubsectionImage(secIdx, subIdx, f)
                                      }
                                    }
                                  }}
                                  onClick={(e) => { e.preventDefault(); document.getElementById(`subsection-file-input-${subKey}`)?.click() }}
                                >
                                  <input
                                    id={`subsection-file-input-${subKey}`}
                                    type="file"
                                    accept="image/*"
                                    multiple
                                    className="hidden"
                                    onChange={(e) => {
                                      const fileList = e.target.files
                                      if (fileList) {
                                        for (let i = 0; i < fileList.length; i++) {
                                          const file = fileList[i]
                                          if (file?.type.startsWith('image/')) void handleUploadSubsectionImage(secIdx, subIdx, file)
                                        }
                                      }
                                      e.target.value = ''
                                    }}
                                  />
                                  {subsectionUploadingKey === subKey ? (
                                    <span className="text-xs text-gray-500 dark:text-gray-400 flex items-center justify-center gap-2">
                                      <Loader2 className="w-4 h-4 animate-spin" />
                                      Enviando...
                                    </span>
                                  ) : (
                                    <span className="text-xs text-gray-500 dark:text-gray-400">Arraste imagens ou clique para enviar</span>
                                  )}
                                </div>
                                {(sub?.images?.length ?? 0) > 0 ? (
                                  <SectionImagePreview
                                    images={(sub.images || []).map((img: unknown) =>
                                      typeof img === 'object' && img !== null && 'path' in (img as object)
                                        ? { path: (img as { path: string }).path, caption: (img as { caption?: string }).caption }
                                        : { path: String(img), caption: '' }
                                    )}
                                    onEditAdvanced={(img, displayIndex) =>
                                      openBookImageEditor({
                                        scope: 'subsection',
                                        kind: img.caption?.startsWith('Slide ') ? 'slide' : 'image',
                                        chapterIdx: selectedChapterIdx,
                                        sectionIdx: secIdx,
                                        subsectionIdx: subIdx,
                                        imagePath: img.path,
                                        title: `${sub.title || sub.objective || `Subseção ${subIdx + 1}`} — imagem ${displayIndex + 1}`,
                                        caption: img.caption,
                                      })
                                    }
                                    onRestyleWithSectionStyles={(img, displayIndex) => handleRestyleWithSubsectionStyles(secIdx, subIdx, img, displayIndex)}
                                    restyleWithSectionStylesLoading={restyleWithStylesLoading}
                                    onDelete={(imagePath) => void handleDeleteSubsectionSlideImage(secIdx, subIdx, imagePath)}
                                    onCaptionChange={(index, caption) => handleSubsectionImageCaptionChange(secIdx, subIdx, index, caption)}
                                    compact
                                  />
                                ) : (
                                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">Nenhuma imagem. Arraste ou clique na área acima para enviar.</p>
                                )}
                              </div>
                            </div>
                          }
                          controls={
                            <div className="space-y-4">
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Slides da subseção:</span>
                                <div className="flex flex-wrap items-center gap-2">
                                  <input
                                    type="number"
                                    min={1}
                                    max={10}
                                    value={sectionSlideCounts[subKey] ?? 3}
                                    onChange={(e) => {
                                      const v = Math.min(10, Math.max(1, Number(e.target.value) || 3))
                                      setSectionSlideCounts((prev) => ({ ...prev, [subKey]: v }))
                                    }}
                                    className="w-12 px-1.5 py-1 border rounded text-xs"
                                  />
                                  <span className="text-xs text-gray-500">slides</span>
                                  <label className="flex items-center gap-1.5 cursor-pointer">
                                    <input
                                      type="checkbox"
                                      checked={sectionImagesWithoutText[subKey] ?? false}
                                      onChange={(e) => setSectionImagesWithoutText((prev) => ({ ...prev, [subKey]: e.target.checked }))}
                                      className="h-3.5 w-3.5 rounded border-gray-300"
                                    />
                                    <span className="text-xs">Sem texto</span>
                                  </label>
                                  <div className="flex items-center gap-1.5">
                                    <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Modelo:</span>
                                    <select
                                      value={sectionSlideModel[subKey] || imageModels[0]?.id || ''}
                                      onChange={(e) => setSectionSlideModel((prev) => ({ ...prev, [subKey]: e.target.value }))}
                                      className="px-2 py-1 border rounded text-xs bg-white dark:bg-gray-800"
                                      title="Modelo de imagem para gerar slides desta subseção"
                                    >
                                      {imageModels.length ? (
                                        imageModels.map((m) => (
                                          <option key={m.id} value={m.id}>{m.name}</option>
                                        ))
                                      ) : (
                                        <option value="">Carregando...</option>
                                      )}
                                    </select>
                                  </div>
                                  <button
                                    type="button"
                                    onClick={() => void handleGenerateSubsectionPrompts(secIdx, subIdx)}
                                    disabled={isGeneratingSub}
                                    className="px-2 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1"
                                  >
                                    {isGeneratingSub ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                                    Gerar Prompts
                                  </button>
                                  {subPrompts.length > 0 && (
                                    <>
                                      <button
                                        type="button"
                                        onClick={() => void handleGenerateSubsectionSlides(secIdx, subIdx)}
                                        disabled={isGeneratingSubsectionSlidesKey !== null || !subPrompts.length}
                                        className="px-2 py-1 text-xs bg-emerald-600 text-white rounded hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-1"
                                        title="Gerar imagens dos slides"
                                      >
                                        {isGeneratingSubsectionSlidesKey === subKey ? <Loader2 className="w-3 h-3 animate-spin" /> : <Layers className="w-3 h-3" />}
                                        Gerar slides
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => handleClearSubsectionPrompts(secIdx, subIdx)}
                                        className="px-2 py-1 text-xs text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded flex items-center gap-1"
                                      >
                                        <Trash2 className="w-3 h-3" /> Remover todos
                                      </button>
                                    </>
                                  )}
                                  {renderInsertBlankSlideButton(
                                    () => handleCreateBlankSubsectionSlide(secIdx, subIdx),
                                    creatingBlankSubsectionSlideKey === subKey,
                                  )}
                                </div>
                              </div>
                              <div className="space-y-1.5">
                                <div className="text-xs font-medium text-gray-600 dark:text-gray-400">Estilos visuais (multi-estilo)</div>
                                <StyleGrid
                                  selectedStyles={(sub as { slide_styles?: string[] }).slide_styles ?? draftPlan?.book_slide_styles ?? []}
                                  onChange={(styles) => updateSubsectionAtIndex(undefined, subIdx, { slide_styles: styles })}
                                  maxSelection={10}
                                  showSearch={true}
                                  showCategoryFilter={true}
                                  defaultCategory="all"
                                  columns={4}
                                  cardHeight="120px"
                                />
                              </div>
                              {subPrompts.length > 0 && (
                                <div className="flex flex-wrap items-center gap-3 py-2 px-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                                  <label className="flex items-center gap-2 cursor-pointer">
                                    <input
                                      type="checkbox"
                                      checked={sectionCodeSlideNoModel[subKey] ?? false}
                                      onChange={(e) =>
                                        setSectionCodeSlideNoModel((prev) => ({ ...prev, [subKey]: e.target.checked }))
                                      }
                                      className="h-3.5 w-3.5 rounded border-gray-300"
                                    />
                                    <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Texto inserido sem modelo</span>
                                  </label>
                                  <div className="flex items-center gap-2">
                                    <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Modelo para slides:</span>
                                    <select
                                      value={sectionSlideModel[subKey] || imageModels[0]?.id || ''}
                                      onChange={(e) =>
                                        setSectionSlideModel((prev) => ({ ...prev, [subKey]: e.target.value }))
                                      }
                                      className="px-2 py-1 border rounded text-xs bg-white dark:bg-gray-800"
                                    >
                                      {imageModels.length ? (
                                        imageModels.map((m) => (
                                          <option key={m.id} value={m.id}>{m.name}</option>
                                        ))
                                      ) : (
                                        <option value="">Carregando...</option>
                                      )}
                                    </select>
                                  </div>
                                </div>
                              )}
                              {subPrompts.length > 0 && (
                                <div className="space-y-2 border-t border-gray-200 dark:border-gray-700 pt-3">
                                  <h4 className="text-xs font-semibold text-gray-600 dark:text-gray-400">Prompts gerados</h4>
                                  {subPrompts.map((prompt, pIdx) => (
                                    <div key={pIdx} className="rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/60 p-3">
                                      <div className="flex items-center justify-between text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">
                                        <span>Slide {prompt.index ?? pIdx + 1}</span>
                                        <button type="button" onClick={() => handleDeleteSubsectionPrompt(secIdx, subIdx, pIdx)} className="p-0.5 text-gray-400 hover:text-red-600 dark:hover:text-red-400 rounded" title="Remover este prompt">
                                          <Trash2 className="w-3 h-3" />
                                        </button>
                                      </div>
                                      {(prompt.title || prompt.text) && (
                                        <div className="text-sm text-gray-700 dark:text-gray-300 line-clamp-2">{prompt.title ? `${prompt.title}: ` : ''}{prompt.text ?? ''}</div>
                                      )}
                                      {(prompt.prompt || prompt.background_prompt) && (
                                        <div className="mt-2 text-xs text-gray-500 dark:text-gray-400 rounded bg-gray-100 dark:bg-gray-800 px-2 py-1.5 max-h-20 overflow-y-auto">
                                          {prompt.prompt || prompt.background_prompt}
                                        </div>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
                          }
                        />

                        <LogViewer logs={logs} title="Logs" maxHeight="180px" autoScroll />
                      </div>
                    </div>
                  )
                })()}
              </div>
            </div>
            </div>
          )}
        </div>
      )}

      {/* Aba Estrutura: árvore do livro ou do curso */}
      {activeTab === 'structure' && (
        <div className="space-y-4">
          {isCoursePlan(draftPlan, planKey) ? (
            <CourseStructurePanel
              courseId={id}
              draftPlan={planToCoursePlan(draftPlan)}
              onPlanChange={(next) => setDraftPlan(next)}
              minHeight={480}
            />
          ) : (
            <>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Estrutura do livro (árvore)</h2>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Visualização em árvore dos capítulos e seções. Atualize na aba Capítulos e volte aqui para ver as mudanças.
              </p>
              <BookStructureTree draftPlan={draftPlan} minHeight={480} translatedUnitKeys={translatedUnitKeys} />
            </>
          )}
        </div>
      )}

      {activeTab === 'facts' && draftPlan && (
        <DeferredBookPanel>
          <BookBaseTab
            type="facts"
            draftPlan={draftPlan}
            onUpdatePlan={(updater: (current: BookPlan) => BookPlan) => setDraftPlan(updater(draftPlan))}
            onSave={(planToSave: BookPlan | null | undefined) => savePlan(planToSave ?? undefined)}
            jobId={id}
            getApiKey={() => getApiKey(job)}
          />
        </DeferredBookPanel>
      )}

      {activeTab === 'bibliography' && draftPlan && (
        <DeferredBookPanel>
          <BookBaseTab
            type="bibliography"
            draftPlan={draftPlan}
            onUpdatePlan={(updater: (current: BookPlan) => BookPlan) => setDraftPlan(updater(draftPlan))}
            onSave={(planToSave: BookPlan | null | undefined) => savePlan(planToSave ?? undefined)}
            jobId={id}
            getApiKey={() => getApiKey(job)}
          />
        </DeferredBookPanel>
      )}

      {/* Design Tab */}
      {
        activeTab === 'design' && (
          <div className="space-y-8">
            <DeferredBookPanel>
              <BookCoverDesigner
                bookTitle={draftPlan.title || job.topic || ''}
                bookSubject={bookObjective || draftPlan.objective || ''}
                bookSubtitle={draftPlan?.subtitle}
                bookAuthor={draftPlan?.author}
                imageModels={imageModels}
                selectedImageModel={coverModel}
                onImageModelChange={setCoverModel}
                currentCoverUrl={id && draftPlan.cover_path ? `${API_BASE_URL || ''}/books/${id}/cover?v=${coverVersion}` : undefined}
                initialPrompt={coverPrompts.front}
                isGenerating={isMock ? false : (isCoverGenerating || job?.tool_progress?.cover_generation?.status === 'running')}
                selectedDesigners={draftPlan.cover_designer_styles || []}
                onDesignersChange={(styles: string[]) => setDraftPlan({ ...draftPlan, cover_designer_styles: styles })}
                onUploadCover={handleUploadCover}
                onGenerate={async (prompt: string) => {
                  setCoverPrompts(prev => ({ ...prev, front: prompt }))
                await runCoverGeneration(prompt, 'front')
                }}
              />
            </DeferredBookPanel>

            {/* Logs for Cover Generation */}
            {logs.length > 0 && (
              <div className="bg-white dark:bg-gray-800 border rounded-lg p-4">
                <LogViewer
                  logs={logs}
                  maxHeight="300px"
                  title="Logs de Execução (Capa/Livro)"
                  autoScroll={true}
                />
              </div>
            )}

            {/* Legacy/Advanced Design Options */}
            <div className="border-t pt-6 grid grid-cols-1 lg:grid-cols-2 gap-6">

              {/* Back Cover */}
              <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-4">
                <h2 className="text-lg font-semibold">Capa Traseira (Contracapa)</h2>
                <div className="space-y-2">
                  <label className="text-xs font-medium text-gray-600">Prompt Personalizado</label>
                  <textarea
                    value={coverPrompts.back}
                    onChange={(e) => setCoverPrompts((prev) => ({ ...prev, back: e.target.value }))}
                    rows={4}
                    className="w-full px-3 py-2 border rounded-md text-sm"
                    placeholder="Descreva a contracapa..."
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={async () => {
                        const res = await api.post('/book/plan_cover', {
                          job_id: id,
                          style_names: [],
                          api_key: getApiKey(job) || undefined,
                          target: 'back',
                        })
                        setCoverPrompts((prev) => ({ ...prev, back: res.data?.prompt || '' }))
                      }}
                      className="px-3 py-2 border rounded-md text-sm"
                    >
                      Sugerir Prompt
                    </button>
                    <button
                      onClick={() => setCoverPrompts((prev) => ({ ...prev, back: buildBestSellerPrompt('back') }))}
                      className="px-3 py-2 border rounded-md text-sm text-emerald-700 border-emerald-200 hover:bg-emerald-50"
                    >
                      Gerar com best-sellers
                    </button>
                    <button
                      onClick={async () => {
                        await runCoverGeneration(coverPrompts.back, 'back')
                      }}
                      className="px-3 py-2 bg-emerald-600 text-white rounded-md text-sm"
                    >
                      Gerar Contracapa
                    </button>
                  </div>
                  {draftPlan.back_cover_path && (
                    <div className="space-y-2 mt-2">
                      <img
                        src={buildFileUrl(draftPlan.back_cover_path)}
                        alt="Capa traseira do livro"
                        role="img"
                        className="w-full rounded-lg border max-h-60 object-contain bg-gray-50"
                      />
                      <button
                        onClick={() => setDraftPlan({ ...draftPlan, back_cover_path: '' })}
                        className="text-xs text-red-500"
                      >
                        Remover
                      </button>
                    </div>
                  )}
                </div>
              </div>

              {/* Chapter Dividers */}
              <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-4">
                <h2 className="text-lg font-semibold">Divisores de Capítulo</h2>
                <div className="flex flex-col gap-3">
                  <select
                    className="px-3 py-2 border rounded-md text-sm"
                    value={selectedChapterIdx}
                    onChange={(e) => setSelectedChapterIdx(Number(e.target.value))}
                  >
                    {chapters.map((chapter, idx) => (
                      <option key={`chapter-select-${idx}`} value={idx}>
                        {chapter.title || `Capítulo ${idx + 1}`}
                      </option>
                    ))}
                  </select>
                  <div className="space-y-2">
                    <label className="text-xs font-medium text-gray-600">Prompt do Divisor</label>
                    <textarea
                      value={coverPrompts.chapter}
                      onChange={(e) => setCoverPrompts((prev) => ({ ...prev, chapter: e.target.value }))}
                      rows={3}
                      className="w-full px-3 py-2 border rounded-md text-sm"
                    />
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={async () => {
                        const res = await api.post('/book/plan_cover', {
                          job_id: id,
                          style_names: [],
                          api_key: getApiKey(job) || undefined,
                          target: 'chapter',
                          chapter_index: selectedChapterIdx,
                        })
                        setCoverPrompts((prev) => ({ ...prev, chapter: res.data?.prompt || prev.chapter }))
                      }}
                      className="px-3 py-2 border rounded-md text-sm"
                    >
                      Sugerir Prompt
                    </button>
                    <button
                      onClick={() => setCoverPrompts((prev) => ({ ...prev, chapter: buildBestSellerPrompt('chapter') }))}
                      className="px-3 py-2 border rounded-md text-sm text-emerald-700 border-emerald-200 hover:bg-emerald-50"
                    >
                      Gerar com best-sellers
                    </button>
                    <button
                      onClick={async () => {
                        await runCoverGeneration(coverPrompts.chapter, 'chapter', selectedChapterIdx)
                      }}
                      className="px-3 py-2 bg-emerald-600 text-white rounded-md text-sm"
                    >
                      Gerar Divisor
                    </button>
                  </div>
                  {currentChapter?.cover_path && (
                    <div className="space-y-2 mt-2">
                      <img
                        src={buildFileUrl(currentChapter.cover_path)}
                        alt={`Divisor de capítulo: ${currentChapter?.title || `Capítulo ${selectedChapterIdx + 1}`}`}
                        role="img"
                        className="w-full rounded-lg border max-h-60 object-contain bg-gray-50"
                      />
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          onClick={() =>
                            openBookImageEditor({
                              scope: 'chapter',
                              kind: 'image',
                              chapterIdx: selectedChapterIdx,
                              imagePath: currentChapter.cover_path!,
                              title: `Divisor — ${currentChapter?.title || `Capítulo ${selectedChapterIdx + 1}`}`,
                            })
                          }
                          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium border border-violet-200 bg-violet-50 text-violet-800 hover:bg-violet-100 dark:border-violet-700 dark:bg-violet-900/30 dark:text-violet-200 dark:hover:bg-violet-900/50"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                          Editar no Studio
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            const updated = [...chapters]
                            const chapter = updated[selectedChapterIdx]
                            if (!chapter) return
                            chapter.cover_path = ''
                            updated[selectedChapterIdx] = chapter
                            setChapters(updated)
                          }}
                          className="text-xs text-red-500"
                        >
                          Remover Divisor
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )
      }

      {/* Assembly Tab */}
      {
        activeTab === 'assembly' && (
          <div className="space-y-6">
            <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-4">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Prólogo, Agradecimentos e EPUB</h2>
              <div className="flex border-b border-gray-200 dark:border-gray-600 mb-4">
                {[
                  { id: 'prologue' as const, label: 'Prólogo' },
                  { id: 'acknowledgments' as const, label: 'Agradecimentos' },
                  { id: 'epub' as const, label: 'Compilar EPUB' },
                ].map((tab) => (
                  <button
                    key={tab.id}
                    onClick={() => setAssemblySubTab(tab.id)}
                    className={cn(
                      'px-4 py-2 text-sm font-medium border-b-2 transition-colors',
                      assemblySubTab === tab.id
                        ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                        : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
                    )}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>

              {assemblySubTab === 'prologue' && (
                <div className="space-y-3">
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">Prólogo do livro</label>
                  <textarea
                    value={draftPlan?.prologue || ''}
                    onChange={(e) => setDraftPlan((p) => (p ? { ...p, prologue: e.target.value } : p))}
                    placeholder="Texto do prólogo (opcional). Será incluído no início do EPUB."
                    rows={18}
                    className="w-full px-4 py-3 border border-gray-300 dark:border-gray-600 rounded-lg text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white resize-y min-h-[20rem]"
                  />
                  <button
                    onClick={async () => {
                      await savePlan({
                        ...draftPlan,
                        prologue: draftPlan?.prologue || '',
                        acknowledgments: draftPlan?.acknowledgments || '',
                      })
                    }}
                    className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm flex items-center gap-2 hover:bg-green-700"
                  >
                    <Save className="w-4 h-4" />
                    Salvar Prólogo
                  </button>
                </div>
              )}

              {assemblySubTab === 'acknowledgments' && (
                <div className="space-y-3">
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">Agradecimentos do livro</label>
                  <textarea
                    value={draftPlan?.acknowledgments || ''}
                    onChange={(e) => setDraftPlan((p) => (p ? { ...p, acknowledgments: e.target.value } : p))}
                    placeholder="Texto de agradecimentos (opcional). Será incluído no EPUB antes dos capítulos."
                    rows={18}
                    className="w-full px-4 py-3 border border-gray-300 dark:border-gray-600 rounded-lg text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white resize-y min-h-[20rem]"
                  />
                  <button
                    onClick={async () => {
                      await savePlan({
                        ...draftPlan,
                        prologue: draftPlan?.prologue || '',
                        acknowledgments: draftPlan?.acknowledgments || '',
                      })
                    }}
                    className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm flex items-center gap-2 hover:bg-green-700"
                  >
                    <Save className="w-4 h-4" />
                    Salvar Agradecimentos
                  </button>
                </div>
              )}

              {assemblySubTab === 'epub' && (
                <div className="space-y-4">
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    Prólogo e agradecimentos são gravados nas abas acima. Aqui você gera e baixa o EPUB completo.
                  </p>
                  <div className="space-y-2">
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                      Estilos das imagens no EPUB
                    </label>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Selecione um ou mais estilos; as imagens geradas a partir de blocos de instrução seguirão esses estilos. Deixe vazio para usar o prompt sem modificador de estilo. (Mesmo componente do Storyboard.)
                    </p>
                    <StyleGrid
                      selectedStyles={draftPlan?.epub_image_styles || []}
                      onChange={(styles) => setDraftPlan((p) => (p ? { ...p, epub_image_styles: styles.length ? styles : undefined } : p))}
                      maxSelection={10}
                      showSearch={true}
                      showCategoryFilter={true}
                      columns={4}
                      cardHeight="180px"
                      initiallyExpanded={false}
                    />
                  </div>
                  <label className="flex items-start gap-2 p-3 rounded-lg border border-emerald-200 dark:border-emerald-800 bg-emerald-50/70 dark:bg-emerald-900/20">
                    <input
                      type="checkbox"
                      checked={epubKeepOneImageInstructionPerChapter}
                      onChange={(e) => setEpubKeepOneImageInstructionPerChapter(e.target.checked)}
                      disabled={isGeneratingFullEpub || isReducingToOneImagePerChapter}
                      className="mt-0.5"
                    />
                    <span className="text-sm text-emerald-900 dark:text-emerald-200">
                      Deixar apenas 1 instrução de imagem por capítulo antes de compilar o EPUB
                      <span className="block text-xs opacity-80">
                        Marcado por padrão. Quando ativo, este passo roda primeiro e só depois o EPUB é enfileirado.
                      </span>
                    </span>
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <button
                      onClick={() => handleCompileFullEpub(false, true)}
                      disabled={isGeneratingFullEpub || isReducingToOneImagePerChapter}
                      className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm flex items-center gap-2 hover:bg-emerald-700 disabled:opacity-60"
                    >
                      {isGeneratingFullEpub ? <Loader2 className="w-4 h-4 animate-spin" /> : <BookMarked className="w-4 h-4" />}
                      {isGeneratingFullEpub ? 'Compilando...' : 'Gerar EPUB Completo'}
                    </button>
                    <button
                      onClick={() => handleCompileFullEpub(false, false)}
                      disabled={isGeneratingFullEpub || isReducingToOneImagePerChapter}
                      className="px-4 py-2 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 rounded-lg text-sm flex items-center gap-2 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-60"
                      title="Compila o EPUB sem gerar imagens a partir de blocos ```image_prompt/```imagem (os blocos ficam como texto no EPUB)"
                    >
                      {isGeneratingFullEpub ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileText className="w-4 h-4" />}
                      {isGeneratingFullEpub ? 'Compilando...' : 'Gerar EPUB (sem imagens)'}
                    </button>
                    {draftPlan?.full_epub_path && (
                      <button
                        onClick={() => handleClearEpubPreviewAndRegenerate()}
                        className="px-4 py-2 border border-amber-500 text-amber-700 dark:text-amber-400 rounded-lg text-sm flex items-center gap-2 hover:bg-amber-50 dark:hover:bg-amber-900/20"
                        title="Remove o EPUB atual e gera um novo"
                      >
                        <Trash2 className="w-4 h-4" />
                        Apagar preview e regenerar
                      </button>
                    )}
                  </div>
                  <DeferredBookPanel>
                    <EpubPreview
                      mode="book"
                      jobId={id}
                      apiKey={getApiKey(job)}
                      bookTitle={draftPlan?.title || job?.topic}
                      bookSubtitle={draftPlan?.subtitle}
                      bookAuthor={draftPlan?.author}
                      chapters={chapters}
                    />
                  </DeferredBookPanel>
                  <div className="mt-4">
                    <LogViewer
                      logs={logs}
                      maxHeight="240px"
                      title="Logs de Execução (Assembly)"
                      autoScroll={true}
                    />
                  </div>
                  {(job as any).tool_progress?.epub_full && (
                    <div className="mt-4">
                      <div className="text-xs text-gray-600 dark:text-gray-400">
                        {(job as any).tool_progress.epub_full.message || 'Compilando...'}
                        {(job as any).tool_progress.epub_full.detail && (
                          <span className="ml-1 text-gray-500 dark:text-gray-500">— {(job as any).tool_progress.epub_full.detail}</span>
                        )}
                      </div>
                      <div className="w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-full mt-1">
                        <div
                          className="h-2 bg-emerald-500 rounded-full transition-all duration-300"
                          style={{ width: `${Math.min(100, Math.max(0, (job as any).tool_progress.epub_full.percent ?? 0))}%` }}
                        />
                      </div>
                      <div className="text-xs text-gray-500 dark:text-gray-500 mt-0.5">
                        {Math.round((job as any).tool_progress.epub_full.percent ?? 0)}%
                        {((job as any).tool_progress.epub_full.current != null && (job as any).tool_progress.epub_full.total != null) && (
                          <> — {(job as any).tool_progress.epub_full.current}/{(job as any).tool_progress.epub_full.total} etapas</>
                        )}
                      </div>
                    </div>
                  )}
                  {draftPlan?.full_epub_path && (
                    <a
                      href={buildFileUrl(draftPlan.full_epub_path)}
                      className="inline-flex items-center gap-2 px-3 py-2 border rounded-md text-sm"
                    >
                      <Download className="w-4 h-4" />
                      Baixar EPUB Completo
                    </a>
                  )}
                  {id && (
                    <button
                      type="button"
                      onClick={handleExportEpubAmazonKdp}
                      disabled={isExportingEpubAmazon}
                      className="inline-flex items-center gap-2 px-3 py-2 border rounded-md text-sm bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200 hover:bg-amber-100 dark:hover:bg-amber-900/30 disabled:opacity-50"
                      title="Exportar EPUB no formato Amazon KDP (livro físico/e-book)"
                    >
                      {isExportingEpubAmazon ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                      EPUB Amazon KDP
                    </button>
                  )}
                  {id && (
                    <a
                      href={`${API_BASE_URL}/books/${id}/export/pdf`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-2 px-3 py-2 border rounded-md text-sm"
                      title="Gera e baixa o livro em PDF (mesmo conteúdo do EPUB)"
                    >
                      <Download className="w-4 h-4" />
                      Baixar PDF
                    </a>
                  )}
                  {draftPlan?.full_colab_notebook_path && (
                    <a
                      href={buildFileUrl(draftPlan.full_colab_notebook_path)}
                      className="inline-flex items-center gap-2 px-3 py-2 border rounded-md text-sm"
                      title="Abrir no Google Colab ou baixar para testar os códigos do livro"
                    >
                      <Download className="w-4 h-4" />
                      Baixar notebook Colab (códigos)
                    </a>
                  )}
                </div>
              )}
            </div>
          </div>
        )
      }

      {/* Section Editor */}
      {activeTab === 'section' && (
          <div className="space-y-4">
            {/* Barra superior fixa: navegação + ações principais */}
            <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 py-3 px-4 -mx-4 mt-2 mb-2 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 rounded-lg shadow-sm">
              <div className="flex items-center gap-3 min-w-0">
                <button
                  onClick={() => setActiveTab('chapters')}
                  className="flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors shrink-0"
                >
                  <ArrowLeft className="w-4 h-4" />
                  Voltar
                </button>
                <span className="text-sm text-gray-500 dark:text-gray-400 truncate hidden sm:inline flex items-center gap-1">
                  <span className="inline-flex items-center gap-0.5">
                    {currentChapter?.title || `Capítulo ${selectedChapterIdx + 1}`}
                    {translatedUnitKeys.includes(`ch_${selectedChapterIdx}`) && (
                      <span title="Capítulo traduzido">
                        <Languages className="w-3.5 h-3.5 text-indigo-500 dark:text-indigo-400 shrink-0" />
                      </span>
                    )}
                  </span>
                  <span>•</span>
                  <span className="inline-flex items-center gap-0.5">
                    {currentSection?.title || `Seção ${selectedSectionIdx + 1}`}
                    {translatedUnitKeys.includes(`sec_${selectedChapterIdx}_${selectedSectionIdx}`) && (
                      <span title="Seção traduzida">
                        <Languages className="w-3.5 h-3.5 text-indigo-500 dark:text-indigo-400 shrink-0" />
                      </span>
                    )}
                  </span>
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  onClick={() => handleWriteSectionAndSave(selectedSectionIdx)}
                  className="px-3 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium flex items-center gap-2 hover:bg-indigo-700 disabled:opacity-60 transition-colors"
                  disabled={isWritingSectionIndex === selectedSectionIdx}
                >
                  {isWritingSectionIndex === selectedSectionIdx ? (
                    <><Loader2 className="w-4 h-4 animate-spin" /> Gerando...</>
                  ) : (
                    <><Wand2 className="w-4 h-4" /> Gerar com IA</>
                  )}
                </button>
                <button
                  onClick={() => handleRewriteSection(selectedSectionIdx)}
                  disabled={isRewritingSectionIndex === selectedSectionIdx}
                  className="px-3 py-2 border border-orange-200 dark:border-orange-700 rounded-lg text-sm font-medium text-orange-600 dark:text-orange-400 hover:bg-orange-50 dark:hover:bg-orange-900/30 disabled:opacity-60 flex items-center gap-2 transition-colors"
                  title="Reescrever o conteúdo da seção com IA"
                >
                  {isRewritingSectionIndex === selectedSectionIdx ? (
                    <><Loader2 className="w-4 h-4 animate-spin" /> Reescrevendo...</>
                  ) : (
                    <><RefreshCw className="w-4 h-4" /> Recriar texto</>
                  )}
                </button>
                <button
                  onClick={() => handlePlanEpubSection(selectedSectionIdx)}
                  disabled={isPlanningEpub}
                  className="px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-60 flex items-center gap-2 transition-colors"
                  title="Use tags no texto para posição das imagens: [IMAGE:1], [IMAGE:2], ou [IMAGE: descrição]"
                >
                  {isPlanningEpub ? (
                    <><Loader2 className="w-4 h-4 animate-spin" /> Planejando...</>
                  ) : (
                    <><Sparkles className="w-4 h-4" /> Planejar EPUB</>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => void handleRenderCharts()}
                  disabled={isRenderingCharts || isMock}
                  className="px-3 py-2 border border-amber-300 dark:border-amber-600 rounded-lg text-sm font-medium text-amber-700 dark:text-amber-300 hover:bg-amber-50 dark:hover:bg-amber-900/30 disabled:opacity-60 flex items-center gap-2 transition-colors"
                  title="Gera imagens dos gráficos (blocos ```chart e JSON) e substitui o código pelas imagens em seções e subseções"
                >
                  {isRenderingCharts ? <Loader2 className="w-4 h-4 animate-spin" /> : <BarChart2 className="w-4 h-4" />}
                  Montar gráficos
                </button>
                <button
                  onClick={() => savePlan()}
                  className="px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium flex items-center gap-2 hover:bg-emerald-700 transition-colors"
                >
                  <Save className="w-4 h-4" />
                  Salvar
                </button>
                <DeferredBookPanel>
                  <EpubPreview
                    mode="section"
                    jobId={id}
                    apiKey={getApiKey(job)}
                    section={sectionForEpubPreview}
                    chapterNumber={selectedChapterIdx + 1}
                    sectionNumber={selectedSectionIdx + 1}
                  />
                </DeferredBookPanel>
                <button
                  onClick={() => {
                    if (currentSections.length <= 1) return
                    if (window.confirm(`Excluir a seção "${currentSection?.title || `Seção ${selectedSectionIdx + 1}`}"?`)) {
                      handleDeleteSection(selectedSectionIdx)
                    }
                  }}
                  disabled={currentSections.length <= 1}
                  className="px-3 py-2 border border-red-200 dark:border-red-800 rounded-lg text-sm font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-2 transition-colors"
                >
                  <Trash2 className="w-4 h-4" />
                  Excluir
                </button>
              </div>
            </div>
            <div className="rounded-lg border border-sky-200 dark:border-sky-800 bg-sky-50/80 dark:bg-sky-950/30 p-4 space-y-2">
              <div className="text-sm font-semibold text-sky-900 dark:text-sky-100 flex items-center gap-2">
                <Search className="w-4 h-4 shrink-0" />
                Perplexity (fontes com busca web)
              </div>
              <p className="text-xs text-sky-800/90 dark:text-sky-200/80">
                Usa a chave salva em{' '}
                <button
                  type="button"
                  onClick={() => navigate('/settings')}
                  className="underline font-medium text-sky-900 dark:text-sky-100 hover:opacity-80"
                >
                  Configurações
                </button>
                . Grava fontes na <strong className="font-medium">base do livro</strong> e acrescenta citações{' '}
                <code className="text-[10px] bg-sky-100/80 dark:bg-sky-900/40 px-1 rounded">[n]</code> ao fim de cada parágrafo (sem reescrever o texto). Referências completas no EPUB (final).
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void handlePerplexityEnrichSection()}
                  disabled={isMock || !id || !currentSection || perplexityBusy !== null}
                  className="px-3 py-2 border border-sky-300 dark:border-sky-600 rounded-lg text-sm font-medium text-sky-800 dark:text-sky-200 hover:bg-sky-100 dark:hover:bg-sky-900/40 disabled:opacity-50 flex items-center gap-2"
                >
                  {perplexityBusy === 'enrich-sec' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                  Fontes nesta seção
                </button>
                <button
                  type="button"
                  onClick={() => void handlePerplexityEnrichSubsection()}
                  disabled={
                    isMock ||
                    !id ||
                    !currentSection ||
                    (currentSection.subsections?.length ?? 0) === 0 ||
                    perplexityBusy !== null
                  }
                  className="px-3 py-2 border border-sky-300 dark:border-sky-600 rounded-lg text-sm font-medium text-sky-800 dark:text-sky-200 hover:bg-sky-100 dark:hover:bg-sky-900/40 disabled:opacity-50 flex items-center gap-2"
                  title="Usa a subseção atualmente selecionada na lista à esquerda (aba Seção)"
                >
                  {perplexityBusy === 'enrich-sub' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                  Fontes na subseção atual
                </button>
              </div>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)] gap-6">
              <div className="bg-white dark:bg-gray-800 border rounded-lg p-4 space-y-3 shrink-0">
                <div className="rounded-lg border border-gray-200 dark:border-gray-600 p-3 space-y-2 bg-gray-50 dark:bg-gray-800/50">
                  <label className="text-xs font-semibold text-gray-700 dark:text-gray-300 block">Nova seção a partir de texto</label>
                  <textarea
                    value={newSectionFromText}
                    onChange={(e) => setNewSectionFromText(e.target.value)}
                    placeholder="Descreva a ideia ou o conteúdo da seção. O agente usará o contexto do livro e do capítulo."
                    rows={4}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 resize-y min-h-[80px]"
                  />
                  <button
                    type="button"
                    onClick={handleAddSectionFromText}
                    disabled={!newSectionFromText.trim() || isGeneratingSectionFromPrompt}
                    className="w-full px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-1"
                  >
                    <Plus className="w-3 h-3" />
                    {isGeneratingSectionFromPrompt ? 'Gerando seção...' : 'Criar seção'}
                  </button>
                </div>
                <div className="flex items-center justify-between">
                  <h2 className="text-sm font-semibold">Seções</h2>
                  <div className="flex gap-2">
                    <button
                      onClick={handlePlanChapter}
                      disabled={isWritingChapter}
                      className="text-sm text-purple-600 flex items-center gap-1 hover:bg-purple-50 px-2 py-1 rounded border border-purple-200 disabled:opacity-50 disabled:cursor-not-allowed"
                      title="Planejar seções com IA"
                    >
                      {isWritingChapter ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : (
                        <Sparkles className="w-3 h-3" />
                      )}
                      <span>{isWritingChapter ? 'Gerando...' : 'IA'}</span>
                    </button>
                    <button
                      onClick={handleAddSection}
                      className="text-sm text-emerald-600 flex items-center gap-1 hover:bg-emerald-50 px-2 py-1 rounded border border-emerald-200"
                      title="Adicionar seção manual"
                    >
                      <Plus className="w-3 h-3" />
                      <span>Adicionar</span>
                    </button>
                  </div>
                </div>
                <select
                  className="w-full px-3 py-2 border rounded-md text-sm"
                  value={selectedChapterIdx}
                  onChange={(e) => {
                    setSelectedChapterIdx(Number(e.target.value))
                    setSelectedSectionIdx(0)
                  }}
                >
                  {chapters.map((chapter, idx) => (
                    <option key={`chapter-${idx}`} value={idx}>
                      {chapter.title || `Capítulo ${idx + 1}`}
                    </option>
                  ))}
                </select>
                <div className="space-y-2">
                  {currentSections.map((section, idx) => {
                    const isEditingTitle = editingSectionTitleIdx === idx
                    return (
                      <div key={`section-item-${idx}`} className="space-y-1">
                        {isEditingTitle ? (
                          <div className="flex items-center gap-1">
                            <input
                              value={editingSectionTitleValue}
                              onChange={(e) => setEditingSectionTitleValue(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') commitSectionTitleEdit()
                                if (e.key === 'Escape') setEditingSectionTitleIdx(null)
                              }}
                              onBlur={commitSectionTitleEdit}
                              className="flex-1 px-2 py-1.5 text-sm border rounded dark:bg-gray-800 dark:border-gray-600"
                              autoFocus
                            />
                            <button type="button" onClick={commitSectionTitleEdit} className="text-xs text-emerald-600 shrink-0">Ok</button>
                          </div>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setSelectedSectionIdx(idx)}
                            className={cn(
                              'w-full text-left px-3 py-2 rounded-lg border text-sm space-y-1 flex items-center gap-2',
                              idx === selectedSectionIdx
                                ? 'border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:border-blue-600'
                                : 'border-gray-200 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-700/50'
                            )}
                          >
                            <span className="font-medium flex-1 truncate">{section.title || `Seção ${idx + 1}`}</span>
                            {translatedUnitKeys.includes(`sec_${selectedChapterIdx}_${idx}`) && (
                              <span title="Traduzido">
                                <Languages className="w-3.5 h-3.5 shrink-0 text-indigo-500 dark:text-indigo-400" />
                              </span>
                            )}
                            {((section.content?.trim() || section.images?.length || (section as { image_path?: string }).image_path) || ((section.subsections || []).some((sub) => (sub?.content?.trim() ?? '') || (sub?.slide_prompts?.length ?? 0) > 0 || (sub?.images?.length ?? 0) > 0))) ? (
                              <span className="shrink-0 flex items-center gap-0.5">
                                {((section.images?.length ?? 0) > 0 || !!(section as { image_path?: string }).image_path || (section.subsections || []).some((sub) => (sub?.images?.length ?? 0) > 0)) ? (
                                  <span className="text-amber-500 dark:text-amber-400" title="Seção tem imagem(ns)">
                                    <ImageIcon className="w-3.5 h-3.5" />
                                  </span>
                                ) : null}
                                <span className="text-emerald-500 dark:text-emerald-400" title="Seção tem texto ou imagem">
                                  <FileText className="w-3.5 h-3.5" />
                                </span>
                              </span>
                            ) : null}
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); startEditingSectionTitle(idx) }}
                              className="text-gray-400 hover:text-blue-600 shrink-0 p-0.5"
                              title="Renomear seção"
                            >
                              <Pencil className="w-3 h-3" />
                            </button>
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation()
                                if (currentSections.length <= 1) return
                                if (window.confirm(`Excluir a seção "${section.title || `Seção ${idx + 1}`}"?`)) {
                                  handleDeleteSection(idx)
                                }
                              }}
                              className="text-gray-400 hover:text-red-600 shrink-0 p-0.5 disabled:opacity-40 disabled:cursor-not-allowed"
                              title="Excluir seção"
                              disabled={currentSections.length <= 1}
                            >
                              <Trash2 className="w-3 h-3" />
                            </button>
                          </button>
                        )}
                        {!isEditingTitle && (section.purpose || section.objective || section.content_directive) && (
                          <div className="text-[10px] text-indigo-600 dark:text-indigo-400 line-clamp-2 pl-3">
                            🎯 {section.purpose || section.objective || section.content_directive}
                          </div>
                        )}
                      </div>
                    )
                  })}
                  <button
                    type="button"
                    onClick={handleAddSection}
                    className="w-full mt-2 px-3 py-2 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700/50 flex items-center justify-center gap-1"
                  >
                    <Plus className="w-4 h-4" />
                    Nova seção
                  </button>
                </div>
              </div>

              <div className="space-y-6 min-w-0 overflow-x-auto">
                <UnifiedChat
                  title="Chat da Seção"
                  description="Controle capítulos e seções com comandos rápidos."
                  contextHint={`Capítulo ${selectedChapterIdx + 1} • Seção ${selectedSectionIdx + 1}`}
                  tools={sectionChatTools}
                  placeholder="Ex: /criar-secao ia ou deletar seção 2"
                  useAgent={true}
                  onActionComplete={handleChatActionComplete}
                  agentContext={{ apiKey: getApiKey(job) || undefined, modelName: modelConfig.getDefaultTextModel('full') }}
                  agentInstructions="Use as ferramentas para gerenciar capítulos e seções. Se o usuário pedir algo fora das ferramentas, responda com a ferramenta mais próxima."
                  agentMetadata={`Livro: ${draftPlan?.title || job?.topic || ''}\nCapítulo: ${currentChapter?.title || `Capítulo ${selectedChapterIdx + 1}`}\nSeção: ${currentSection?.title || `Seção ${selectedSectionIdx + 1}`}\nObjetivo: ${currentSection?.purpose || currentSection?.objective || ''}`}
                  imageModels={modelConfig.getImageModelsForSelect()}
                  defaultImageModel={coverModel}
                  imageJobId={id}
                />

                  <div className="flex flex-wrap items-center gap-2">
                      <button
                        onClick={clearAllSectionsText}
                        className="px-3 py-2 border border-amber-200 dark:border-amber-700 rounded-md text-sm flex items-center gap-2 text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20"
                        title="Apagar apenas o texto de todas as seções (mantém títulos, objetivos e imagens)"
                      >
                        <FileText className="w-4 h-4" />
                        Apagar textos
                      </button>
                    </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <div className="flex items-center gap-2 mb-2">
                        <button
                          type="button"
                          onClick={() => handleGenerateSectionObjective()}
                          disabled={isGeneratingSectionObjective}
                          className="px-3 py-2 border border-indigo-200 rounded-md text-sm flex items-center gap-2 text-indigo-700 hover:bg-indigo-50 disabled:opacity-60"
                          title="Gerar objetivo da seção com IA"
                        >
                          {isGeneratingSectionObjective ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                          ) : (
                            <Target className="w-4 h-4" />
                          )}
                          Gerar objetivo da seção
                        </button>
                      </div>
                      <MarkdownField
                        label="🎯 Objetivo da Seção"
                        value={currentSection?.purpose || currentSection?.objective || currentSection?.content_directive || ''}
                        onChange={(value) => {
                          const updated = [...chapters]
                          const chapter = updated[selectedChapterIdx]
                          if (!chapter) return
                          const sections = [...(chapter.sections || [])]
                          sections[selectedSectionIdx] = {
                            ...currentSection,
                            purpose: value,
                            content_directive: value,
                          }
                          chapter.sections = sections
                          updated[selectedChapterIdx] = chapter
                          setChapters(updated)
                        }}
                        placeholder="Ex: Introduzir o conceito X... (**negrito**, # título)"
                        rows={2}
                        showPreview={true}
                      />
                    </div>
                    <div>
                      <MarkdownField
                        label="Título"
                        value={currentSection?.title || ''}
                        onChange={(v) => handleSectionFieldChange('title', v)}
                        placeholder="Título da seção (**negrito**, *itálico*)"
                        rows={1}
                        showPreview={true}
                      />
                    </div>
                  </div>

                  <AuthorStyleSelector
                    selectedStyles={currentSection?.author_styles || []}
                    onChange={(styles) => updateSectionAtIndex(selectedSectionIdx, { author_styles: styles })}
                    label="✨ Estilos de Autor"
                    description="Selecione estilos para orientar a escrita desta seção"
                  />
                  <div className="flex items-center gap-2">
                    <button
                      onClick={handleApplyAuthorStylesToSection}
                      disabled={isApplyingAuthorStyles || !(currentSection?.author_styles || []).length}
                      className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 disabled:opacity-60"
                    >
                      {isApplyingAuthorStyles ? (
                        <>
                          <Loader2 className="w-4 h-4 animate-spin" />
                          Aplicando estilos...
                        </>
                      ) : (
                        <>
                          <Wand2 className="w-4 h-4" />
                          Aplicar estilos no texto
                        </>
                      )}
                    </button>
                    <span className="text-xs text-gray-500">Reescreve o conteúdo com os estilos selecionados</span>
                  </div>

                  <div>
                    <MarkdownField
                      label="Conteúdo (Markdown)"
                      value={currentSection?.content || ''}
                      onChange={(v) => handleSectionFieldChange('content', v)}
                      placeholder="Conteúdo da seção... (negrito, itálico, títulos)"
                      rows={28}
                      minHeight="32rem"
                      showPreview={true}
                      className="text-sm font-mono"
                    />
                  </div>

                  {/* Texto Reigen */}
                  {currentSection?.reigenText && (
                    <div className="border-t pt-4 mt-4">
                      <div className="flex items-center justify-between mb-2">
                        <label className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                          Texto Reigen (para leitura do robô)
                        </label>
                        <div className="flex items-center gap-2">
                          <button
                            onClick={handleDownloadReigenText}
                            className="px-3 py-1 text-xs bg-green-600 text-white rounded hover:bg-green-700 flex items-center gap-1"
                            title="Download do texto Reigen"
                          >
                            <Download className="w-3 h-3" />
                            Download
                          </button>
                          <button
                            onClick={() => void handleDownloadForHeygen()}
                            disabled={isDownloadingForHeygen}
                            className="px-3 py-1 text-xs bg-violet-600 text-white rounded hover:bg-violet-700 flex items-center gap-1 disabled:opacity-50"
                            title="Download do texto limpo para HeyGen"
                          >
                            {isDownloadingForHeygen ? (
                              <Loader2 className="w-3 h-3 animate-spin" />
                            ) : (
                              <Download className="w-3 h-3" />
                            )}
                            HeyGen
                          </button>
                        </div>
                      </div>
                      <textarea
                        value={currentSection.editedReigenText || currentSection.reigenText}
                        onChange={(e) => {
                          const updatedSections = [...(chapters[selectedChapterIdx]?.sections || [])]
                          updatedSections[selectedSectionIdx] = {
                            ...currentSection,
                            editedReigenText: e.target.value
                          }
                          const updatedChapters = [...chapters]
                          updatedChapters[selectedChapterIdx] = {
                            ...chapters[selectedChapterIdx],
                            sections: updatedSections
                          }
                          setChapters(updatedChapters)
                        }}
                        className="w-full p-2 text-sm border rounded dark:bg-gray-800 dark:border-gray-600 dark:text-gray-200"
                        rows={8}
                        placeholder="Texto limpo para leitura do robô Reigen..."
                      />
                    </div>
                  )}

                  {currentSection?.content && !currentSection?.reigenText && (
                    <div className="border-t pt-4 mt-4">
                      <div className="flex items-center justify-between">
                        <label className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                          Gerar Texto Reigen
                        </label>
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => void handleDownloadForHeygen()}
                            disabled={isDownloadingForHeygen}
                            className="px-3 py-2 text-xs bg-violet-600 text-white rounded hover:bg-violet-700 flex items-center gap-1 disabled:opacity-50"
                            title="Gerar e baixar texto limpo para HeyGen"
                          >
                            {isDownloadingForHeygen ? (
                              <>
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Baixando...
                              </>
                            ) : (
                              <>
                                <Download className="w-3 h-3" />
                                Download HeyGen
                              </>
                            )}
                          </button>

                          {/* AÇÕES DE CIRURGIA DE IA NA SEÇÃO */}
                          <div className="flex flex-wrap gap-2 pt-2 mt-2 border-t border-gray-200 dark:border-gray-700 w-full">
                            <span className="text-xs font-semibold text-blue-800 dark:text-blue-300 w-full mb-1 flex items-center gap-1"><Wand2 className="w-3.5 h-3.5" /> Edição Cirúrgica com IA</span>
                            <button
                              type="button"
                              onClick={handleSurgicalRegenerateObjectives}
                              className="text-[11px] px-3 py-1.5 bg-white dark:bg-gray-800 border border-indigo-200 dark:border-indigo-800 text-indigo-700 dark:text-indigo-400 rounded hover:bg-indigo-50 dark:hover:bg-indigo-900/50 flex items-center gap-1.5 transition-colors shadow-sm"
                              title="A IA irá analisar o contexto do capítulo e recriar apenas os Objetivos (Diretrizes) desta seção"
                            >
                              <Target className="w-3.5 h-3.5" />
                              Melhorar Objetivos com IA
                            </button>
                            <button
                              type="button"
                              onClick={handleSurgicalRegenerateContent}
                              className="text-[11px] px-3 py-1.5 bg-white dark:bg-gray-800 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-400 rounded hover:bg-emerald-50 dark:hover:bg-emerald-900/50 flex items-center gap-1.5 transition-colors shadow-sm"
                              title="A IA irá reescrever integralmente o manuscrito desta seção iterativamente"
                            >
                              <RefreshCw className="w-3.5 h-3.5" />
                              Reescrever Conteúdo com IA
                            </button>
                          </div>

                          <button
                            onClick={handleGenerateReigenText}
                            disabled={currentSection.isGeneratingReigenText}
                            className="px-3 py-2 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 flex items-center gap-1 disabled:opacity-50"
                            title="Gerar texto limpo para leitura do robô Reigen"
                          >
                            {currentSection.isGeneratingReigenText ? (
                              <>
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Gerando...
                              </>
                            ) : (
                              <>
                                <FileText className="w-3 h-3" />
                                Gerar Texto Reigen
                              </>
                            )}
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Personagens e Locais (Quadrinhos) — incluir no prompt das imagens da seção */}
                  <div className="mb-4 p-4 bg-gray-50 dark:bg-gray-800/50 rounded-xl border border-gray-200 dark:border-gray-700">
                    <h4 className="text-sm font-semibold text-gray-900 dark:text-white mb-2 flex items-center gap-2">
                      <span>📚</span> Personagens e Locais (Quadrinhos)
                    </h4>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
                      Marque para incluir nos prompts das imagens da seção. Metadados completos serão usados.
                    </p>
                    {comicCharacters && comicCharacters.length > 0 && (
                      <div className="mb-3">
                        <label className="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-2">Personagens</label>
                        <div className="flex flex-wrap gap-2 max-h-20 overflow-y-auto">
                          {(comicCharacters as { id?: string; character_id?: string; name?: string }[]).map((c) => {
                            const charId = c.character_id ?? c.id ?? ''
                            if (!charId) return null
                            const checked = selectedComicCharacterIds.includes(charId)
                            return (
                              <label
                                key={charId}
                                className={cn(
                                  'inline-flex items-center gap-1.5 px-2 py-1 rounded border text-xs cursor-pointer',
                                  checked ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30' : 'border-gray-300 dark:border-gray-600'
                                )}
                              >
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={(e) => {
                                    if (e.target.checked) setSelectedComicCharacterIds((prev) => [...prev, charId])
                                    else setSelectedComicCharacterIds((prev) => prev.filter((x) => x !== charId))
                                  }}
                                  className="rounded"
                                />
                                {c.name || 'Sem nome'}
                              </label>
                            )
                          })}
                        </div>
                      </div>
                    )}
                    {comicSagas && comicSagas.length > 0 && (
                      <>
                        <div className="mb-2">
                          <label className="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Saga (locais)</label>
                          <select
                            value={selectedComicSagaId}
                            onChange={(e) => {
                              setSelectedComicSagaId(e.target.value)
                              setSelectedComicLocationKeys([])
                            }}
                            className="w-full p-2 border border-gray-300 dark:border-gray-600 rounded text-sm bg-white dark:bg-gray-700"
                          >
                            <option value="">— Selecione —</option>
                            {(comicSagas as { saga_id?: string; id?: string; name?: string }[]).map((s) => (
                              <option key={s.saga_id ?? s.id} value={s.saga_id ?? s.id}>{s.name || 'Saga'}</option>
                            ))}
                          </select>
                        </div>
                        {selectedComicSagaId && (() => {
                          const saga = (comicSagas as { saga_id?: string; id?: string; locations?: { name?: string }[] }[]).find(
                            (s) => (s.saga_id ?? s.id) === selectedComicSagaId
                          )
                          const locations = saga?.locations ?? []
                          if (locations.length === 0) return null
                          return (
                            <div>
                              <label className="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-2">Locais</label>
                              <div className="flex flex-wrap gap-2 max-h-16 overflow-y-auto">
                                {locations.map((loc: { name?: string }) => {
                                  const locName = (typeof loc === 'string' ? loc : loc?.name) || ''
                                  if (!locName) return null
                                  const key = `${selectedComicSagaId}|${locName}`
                                  const checked = selectedComicLocationKeys.includes(key)
                                  return (
                                    <label
                                      key={key}
                                      className={cn(
                                        'inline-flex items-center gap-1.5 px-2 py-1 rounded border text-xs cursor-pointer',
                                        checked ? 'border-amber-500 bg-amber-50 dark:bg-amber-900/30' : 'border-gray-300 dark:border-gray-600'
                                      )}
                                    >
                                      <input
                                        type="checkbox"
                                        checked={checked}
                                        onChange={(e) => {
                                          if (e.target.checked) setSelectedComicLocationKeys((prev) => [...prev, key])
                                          else setSelectedComicLocationKeys((prev) => prev.filter((x) => x !== key))
                                        }}
                                        className="rounded"
                                      />
                                      {locName}
                                    </label>
                                  )
                                })}
                              </div>
                            </div>
                          )
                        })()}
                      </>
                    )}
                    {(!comicCharacters || comicCharacters.length === 0) && (!comicSagas || comicSagas.length === 0) && (
                      <p className="text-xs text-gray-500 dark:text-gray-400">Crie personagens e sagas em Quadrinhos → Studio para usar aqui.</p>
                    )}
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <SectionImageGeneratorPanel
                      title="🖼️ Imagens da Seção"
                      countLabel={(() => {
                        const sk = getSectionKey()
                        const prompts = sectionSlidePrompts[sk] || []
                        const imgs = sectionGeneratedSlideImages[sk] || []
                        if (prompts.length) return `${imgs.length}/${prompts.length} slides`
                        return undefined
                      })()}
                      preview={(
                        <>
                          {(() => {
                            const sk = getSectionKey()
                            const sectionImages = currentSectionImagesAll
                            const isSlideImg = (img: unknown) =>
                              typeof img === 'object' && img !== null && ((img as { source?: string }).source === 'slide' || (img as { caption?: string }).caption?.startsWith('Slide '))
                            const slideIndicesInSection = sectionImages.map((_, i) => i).filter((i) => isSlideImg(sectionImages[i]))
                            const slideEntries = slideIndicesInSection.map((idx, j) => {
                              const img = sectionImages[idx] as { path: string; caption?: string }
                              return { path: img.path, caption: img.caption ?? `Slide ${j + 1}` }
                            })
                            const slideImgs = sectionGeneratedSlideImages[sk] || []
                            const prompts = sectionSlidePrompts[sk] || []
                            const baseSlides = slideEntries.length > 0 ? slideEntries : slideImgs.map((path, j) => ({ path, caption: `Slide ${j + 1}` }))
                            const displaySlides = baseSlides.map((slide, i) => ({
                              ...slide,
                              prompt: (prompts[i]?.prompt ?? prompts[i]?.text ?? '').trim() || undefined,
                            }))
                            if (prompts.length > 0) {
                              return (
                                <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-4 mb-3">
                                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                                    <h4 className="text-xs font-semibold text-gray-600 dark:text-gray-400 uppercase">Preview dos slides</h4>
                                    {renderInsertBlankSlideButton(
                                      () => handleCreateBlankSectionSlide(selectedSectionIdx),
                                      creatingBlankSectionSlide,
                                    )}
                                  </div>
                                  <DeferredBookPanel>
                                    <LessonSlidePreview
                                      slides={displaySlides}
                                      isGenerating={isGeneratingSectionSlides}
                                      expectedCount={prompts.length}
                                      onMove={(fromIndex: number, toIndex: number) =>
                                        handleMoveSectionSlide(selectedSectionIdx, fromIndex, toIndex)
                                      }
                                      onEdit={(slideIdx: number, path: string) =>
                                        openBookImageEditor({
                                          scope: 'section',
                                          kind: 'slide',
                                          chapterIdx: selectedChapterIdx,
                                          sectionIdx: selectedSectionIdx,
                                          imagePath: path,
                                          title: `${currentSection?.title || `Seção ${selectedSectionIdx + 1}`} — slide ${slideIdx + 1}`,
                                          caption: displaySlides[slideIdx]?.caption ?? `Slide ${slideIdx + 1}`,
                                        })
                                      }
                                      onDelete={(slideIdx: number, path: string) => {
                                        setSlideBeingDeletedIndex(slideIdx)
                                        handleDeleteImage(selectedSectionIdx, path).finally(() => setSlideBeingDeletedIndex(null))
                                      }}
                                      deletingIndex={slideBeingDeletedIndex}
                                      onCaptionChange={
                                        slideEntries.length > 0
                                          ? (slideIdx: number, caption: string) => {
                                            const imageIndexInSection = slideIndicesInSection[slideIdx]
                                            if (imageIndexInSection == null) return
                                            const updated = [...chapters]
                                            const ch = updated[selectedChapterIdx]
                                            const sec = ch?.sections?.[selectedSectionIdx]
                                            if (!sec?.images) return
                                            const images = sec.images.map((img, i) =>
                                              i === imageIndexInSection && typeof img === 'object' && img !== null && 'path' in img
                                                ? { ...img, caption }
                                                : img
                                            ) as NonNullable<BookSection['images']>
                                            ch.sections[selectedSectionIdx] = { ...sec, images }
                                            updated[selectedChapterIdx] = { ...ch }
                                            setChapters(updated)
                                            if (draftPlan) void savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
                                          }
                                          : undefined
                                      }
                                    />
                                  </DeferredBookPanel>
                                  {displaySlides.length === 0 && slideImgs.length === 0 && !isGeneratingSectionSlides && (
                                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">Nenhum slide gerado ainda. Clique em &quot;Gerar slides&quot; abaixo.</p>
                                  )}
                                </div>
                              )
                            }
                            return null
                          })()}
                          {(() => {
                            const sectionImages = currentSectionImagesAll
                            const isSlideImg = (img: unknown) =>
                              typeof img === 'object' && img !== null && ((img as { source?: string }).source === 'slide' || (img as { caption?: string }).caption?.startsWith('Slide '))
                            const hasSlidePreview = (() => {
                              const sk = getSectionKey()
                              return (sectionSlidePrompts[sk] || []).length > 0
                            })()
                            const previewImages = hasSlidePreview
                              ? sectionImages.filter((img) => !isSlideImg(img))
                              : sectionImages
                            const previewIndexToReal = hasSlidePreview
                              ? sectionImages.map((_, i) => i).filter((i) => !isSlideImg(sectionImages[i]))
                              : sectionImages.map((_, i) => i)
                            if (previewImages.length === 0 && hasSlidePreview) return null
                            return (
                              <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-4 mt-3">
                                <SectionImagePreview
                                  images={previewImages}
                                  onEditAdvanced={(img, displayIndex) => {
                                    const realIndex = previewIndexToReal[displayIndex]
                                    const realImage = realIndex == null ? img : currentSectionImagesAll[realIndex] as { path: string; caption?: string }
                                    openBookImageEditor({
                                      scope: 'section',
                                      kind: img.caption?.startsWith('Slide ') ? 'slide' : 'image',
                                      chapterIdx: selectedChapterIdx,
                                      sectionIdx: selectedSectionIdx,
                                      imagePath: realImage?.path || img.path,
                                      title: `${currentSection?.title || `Seção ${selectedSectionIdx + 1}`} — imagem ${displayIndex + 1}`,
                                      caption: realImage?.caption ?? img.caption,
                                    })
                                  }}
                                  onEditWithKontext={(_, displayIndex) => {
                                    const realIndex = previewIndexToReal[displayIndex]
                                    if (realIndex == null) return
                                    setKontextImageIndex(realIndex)
                                    setKontextPrompt('')
                                    setIsKontextModalOpen(true)
                                  }}
                                  onRestyleWithSectionStyles={(img, displayIndex) => {
                                    const realIndex = previewIndexToReal[displayIndex]
                                    if (realIndex == null) return
                                    handleRestyleWithSectionStyles(img, realIndex)
                                  }}
                                  restyleWithSectionStylesLoading={restyleWithStylesLoading}
                                  onDelete={(imagePath) => handleDeleteImage(selectedSectionIdx, imagePath)}
                                  onRemoveBackground={(img) => handleRemoveBackground(selectedSectionIdx, img)}
                                  onInsertInContent={(img) => {
                                    const caption = img.caption || 'Imagem'
                                    const snippet = `\n\n![${caption}](${img.path})\n\n`
                                    handleSectionFieldChange('content', `${currentSection?.content || ''}${snippet}`)
                                    void savePlan()
                                  }}
                                  onCaptionChange={(displayIndex, caption) => {
                                    const realIndex = previewIndexToReal[displayIndex]
                                    if (realIndex == null) return
                                    const img = currentSectionImagesAll[realIndex] as { path: string; caption?: string } | undefined
                                    const imgPath = (img?.path ?? '').trim()
                                    const baseImages = currentSection?.images || []
                                    const sectionImagePath = (currentSection as { image_path?: string })?.image_path
                                    const pathInSection = baseImages.some((i) => (typeof i === 'object' && i !== null && 'path' in i && (i.path ?? '').trim() === imgPath) || (typeof i === 'string' && (i as string).trim() === imgPath))
                                    const pathIsImagePath = (sectionImagePath ?? '').trim() === imgPath
                                    const isFromContent = imgPath && !pathInSection && !pathIsImagePath
                                    if (isFromContent) {
                                      const newContent = replaceMarkdownImageCaption(currentSection?.content, imgPath, caption)
                                      handleSectionFieldChange('content', newContent)
                                      if (draftPlan) void savePlan()
                                      return
                                    }
                                    const updated = [...chapters]
                                    const chapter = updated[selectedChapterIdx]
                                    if (!chapter) return
                                    const sections = [...(chapter.sections || [])]
                                    const isFromImagePath = sectionImagePath != null && realIndex >= baseImages.length
                                    let nextImages = [...baseImages]
                                    if (isFromImagePath) {
                                      nextImages = [...baseImages, { path: sectionImagePath!, caption }]
                                      sections[selectedSectionIdx] = { ...currentSection, images: nextImages, image_path: undefined }
                                    } else {
                                      if (realIndex < nextImages.length) nextImages[realIndex] = { ...nextImages[realIndex], caption }
                                      sections[selectedSectionIdx] = { ...currentSection, images: nextImages }
                                    }
                                    chapter.sections = sections
                                    updated[selectedChapterIdx] = chapter
                                    setChapters(updated)
                                    if (draftPlan) {
                                      const planKey = getChapterKey(draftPlan)
                                      void savePlan({
                                        ...draftPlan,
                                        [planKey]: updated,
                                      })
                                    }
                                  }}
                                />
                              </div>
                            )
                          })()}
                        </>
                      )}
                      controls={(
                        <div className="grid grid-cols-1 gap-2">
                          {/* Gerar Prompts / Código / Slides */}
                          <div className="flex flex-wrap items-center gap-2 mb-3 pb-3 border-b border-gray-200 dark:border-gray-700">
                            <span className="text-xs font-medium text-gray-600 dark:text-gray-400 mr-1">Slides da seção:</span>
                            <input
                              type="number"
                              min={1}
                              max={10}
                              value={sectionSlideCounts[getSectionKey()] ?? 3}
                              onChange={(e) => {
                                const v = Math.min(10, Math.max(1, Number(e.target.value) || 3))
                                setSectionSlideCounts((prev) => ({ ...prev, [getSectionKey()]: v }))
                              }}
                              className="w-12 px-1.5 py-1 border rounded text-xs"
                            />
                            <span className="text-xs text-gray-500">slides</span>
                            <label className="flex items-center gap-1.5 cursor-pointer ml-1" title="Se marcado, os prompts serão gerados para imagens sem texto sobreposto.">
                              <input
                                type="checkbox"
                                checked={sectionImagesWithoutText[getSectionKey()] ?? false}
                                onChange={(e) =>
                                  setSectionImagesWithoutText((prev) => ({
                                    ...prev,
                                    [getSectionKey()]: e.target.checked,
                                  }))
                                }
                                className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                              />
                              <span className="text-xs font-medium text-gray-700 dark:text-gray-300 whitespace-nowrap">
                                Prompts sem texto
                              </span>
                            </label>
                            <button
                              type="button"
                              onClick={() => handleGenerateSectionPrompts()}
                              disabled={isGeneratingSectionPrompts}
                              className="px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1.5 text-xs font-medium"
                            >
                              {isGeneratingSectionPrompts ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
                              Gerar Prompts
                            </button>
                            <button
                              type="button"
                              onClick={() => handleGenerateSectionCodeSource()}
                              disabled={isGeneratingSectionCodeSource}
                              className="px-3 py-1.5 bg-slate-700 text-white rounded-lg hover:bg-slate-800 disabled:opacity-50 flex items-center gap-1.5 text-xs font-medium"
                            >
                              {isGeneratingSectionCodeSource ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5 text-blue-400" />}
                              Gerar Código Fonte
                            </button>
                            <button
                              type="button"
                              onClick={() => handleGenerateSectionDidacticCodeSlidesOneClick()}
                              disabled={isGeneratingSectionDidacticCodePipeline}
                              className="px-3 py-1.5 bg-gradient-to-r from-blue-600 to-cyan-600 text-white rounded-lg hover:from-blue-700 hover:to-cyan-700 disabled:opacity-50 flex items-center gap-1.5 text-xs font-semibold"
                              title={"Checklist do Pipeline:\n1) Extrai o código-fonte da aula\n2) Divide em slides didáticos\n3) Gera título e explicação por slide\n4) Cria prompts visuais sem texto\n5) Gera as imagens dos slides automaticamente"}
                            >
                              {isGeneratingSectionDidacticCodePipeline ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Code className="w-3.5 h-3.5" />}
                              Código Fonte para Slides Didáticos
                            </button>
                            <button
                              type="button"
                              onClick={() => handleGenerateSectionSlides()}
                              disabled={isGeneratingSectionSlides || !(sectionSlidePrompts[getSectionKey()]?.length)}
                              className="px-3 py-1.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-1.5 text-xs font-medium"
                              title="Gere Prompts ou Código Fonte antes."
                            >
                              {isGeneratingSectionSlides ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Layers className="w-3.5 h-3.5" />}
                              Gerar Slides
                            </button>
                            {renderInsertBlankSlideButton(
                              () => handleCreateBlankSectionSlide(selectedSectionIdx),
                              creatingBlankSectionSlide,
                            )}
                          </div>
                          {/* Opções para Gerar Slides (com/sem modelo + escolha do modelo) */}
                          {((sectionSlidePrompts[getSectionKey()]?.length ?? 0) > 0) && (
                            <div className="flex flex-wrap items-center gap-3 py-2 px-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                              <label className="flex items-center gap-2 cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={sectionCodeSlideNoModel[getSectionKey()] ?? false}
                                  onChange={(e) =>
                                    setSectionCodeSlideNoModel((prev) => ({
                                      ...prev,
                                      [getSectionKey()]: e.target.checked,
                                    }))
                                  }
                                  className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                                />
                                <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
                                  Texto Inserido sem Modelo
                                </span>
                                <span className="text-xs text-gray-500 dark:text-gray-400">(fundo branco imediato)</span>
                              </label>
                              <div className="flex items-center gap-2">
                                <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Modelo para slides:</span>
                                <select
                                  value={sectionSlideModel[getSectionKey()] || getImageOptions(selectedSectionIdx).model || imageModels[0]?.id || ''}
                                  onChange={(e) =>
                                    setSectionSlideModel((prev) => ({
                                      ...prev,
                                      [getSectionKey()]: e.target.value,
                                    }))
                                  }
                                  disabled={sectionCodeSlideNoModel[getSectionKey()] ?? false}
                                  className="px-2 py-1 border rounded text-xs bg-white dark:bg-gray-800 disabled:opacity-50"
                                  title="Usado quando gerar slides com modelo de imagem"
                                >
                                  {imageModels.length ? (
                                    imageModels.map((m) => (
                                      <option key={m.id} value={m.id}>
                                        {m.name}
                                      </option>
                                    ))
                                  ) : (
                                    <option value="">Carregando...</option>
                                  )}
                                </select>
                              </div>
                            </div>
                          )}
                          {/* Lista de prompts gerados (Gerar Prompts / Gerar Código Fonte) */}
                          {((sectionSlidePrompts[getSectionKey()]?.length ?? 0) > 0) && (
                            <div className="mt-3 space-y-3 border-t border-gray-200 dark:border-gray-700 pt-3">
                              <div className="flex items-center justify-between gap-2">
                                <span className="text-xs font-semibold text-gray-600 dark:text-gray-400">Prompts gerados</span>
                                <button
                                  type="button"
                                  onClick={() => handleClearSectionPrompts(getSectionKey())}
                                  className="inline-flex items-center gap-1 px-2 py-1 text-xs text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition-colors"
                                  title="Remover todos os prompts desta seção"
                                >
                                  <Trash2 className="w-3.5 h-3.5" /> Remover todos
                                </button>
                              </div>
                              {(sectionSlidePrompts[getSectionKey()] || []).map((prompt, idx) => {
                                const slideIndex = prompt.index ?? idx + 1
                                const codeImageItem = (sectionCodeImagePrompts[getSectionKey()] || []).find((item) => item.index === slideIndex)
                                const isFromCodeSource = Boolean(prompt.code_text && codeImageItem)
                                const displayTitle = prompt.title
                                const displayText = isFromCodeSource ? (prompt.text ?? '') : (prompt.text ?? '')
                                const displayPrompt = isFromCodeSource ? (codeImageItem?.image_prompt ?? '') : (prompt.prompt ?? prompt.background_prompt ?? '')
                                return (
                                  <div
                                    key={`section-prompt-${idx}`}
                                    className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800/60 p-3"
                                  >
                                    <div className="flex items-center justify-between text-xs font-semibold text-gray-500 dark:text-gray-400 mb-2">
                                      <span>Slide {slideIndex}</span>
                                      <div className="flex items-center gap-1">
                                        {isFromCodeSource && <span className="text-blue-600 dark:text-blue-400">Código fonte</span>}
                                        <button
                                          type="button"
                                          onClick={() => handleDeleteSectionPrompt(getSectionKey(), idx)}
                                          className="p-1 text-gray-400 hover:text-red-600 dark:hover:text-red-400 rounded transition-colors"
                                          title="Remover este prompt"
                                        >
                                          <Trash2 className="w-3.5 h-3.5" />
                                        </button>
                                      </div>
                                    </div>
                                    <div className="grid gap-1.5 text-sm">
                                      {displayTitle ? <div className="font-medium text-gray-800 dark:text-gray-200">{displayTitle}</div> : null}
                                      {displayText ? <div className="text-gray-600 dark:text-gray-300 whitespace-pre-wrap line-clamp-2">{displayText}</div> : null}
                                      {displayPrompt ? (
                                        <div className="mt-1">
                                          <div className="text-xs text-gray-500 dark:text-gray-400 mb-0.5">Prompt visual</div>
                                          <div className="rounded border border-gray-100 dark:border-gray-600 bg-gray-50 dark:bg-gray-800 px-2 py-1.5 text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap max-h-24 overflow-y-auto">
                                            {displayPrompt}
                                          </div>
                                        </div>
                                      ) : null}
                                      {prompt.code_text ? (
                                        <div className="mt-1">
                                          <div className="text-xs text-gray-500 dark:text-gray-400 mb-0.5">Código</div>
                                          <pre className="rounded border border-gray-100 dark:border-gray-600 bg-gray-50 dark:bg-gray-800 p-2 text-xs overflow-x-auto max-h-20 overflow-y-auto">
                                            {prompt.code_text}
                                          </pre>
                                        </div>
                                      ) : null}
                                    </div>
                                  </div>
                                )
                              })}
                            </div>
                          )}
                          {/* Model selector */}
                          <div className="space-y-1">
                            <div className="text-xs font-medium text-gray-600">Modelo de Imagem</div>
                            <div className="flex gap-1">
                              <select
                                value={selectedImageProvider}
                                onChange={(e) => setSelectedImageProvider(e.target.value)}
                                className="px-2 py-1 border rounded text-xs w-[100px]"
                              >
                                <option value="all">Todos</option>
                                {imageProviders.map((p) => (
                                  <option key={p.id} value={p.id}>{p.name}</option>
                                ))}
                              </select>
                              <select
                                value={getImageOptions(selectedSectionIdx).model || 'imagen-4.0-ultra-generate-001'}
                                onChange={(e) => setImageOption(selectedSectionIdx, { model: e.target.value })}
                                className="px-2 py-1 border rounded text-xs flex-1"
                              >
                                {imageModels.map((m) => (
                                  <option key={m.id} value={m.id}>{m.name}</option>
                                ))}
                              </select>
                            </div>
                          </div>
                          <input
                            value={getImageOptions(selectedSectionIdx).prompt}
                            onChange={(e) => setImageOption(selectedSectionIdx, { prompt: e.target.value })}
                            className="px-2 py-1 border rounded text-sm"
                            placeholder="Prompt opcional para a imagem"
                          />
                          <div className="space-y-2">
                            <div className="text-xs font-medium text-gray-600">Estilo da imagem</div>
                            <StyleGrid
                              selectedStyles={getImageOptions(selectedSectionIdx).styles || []}
                              onChange={(styles) => setImageOption(selectedSectionIdx, { styles })}
                              maxSelection={10}
                              showSearch={true}
                              showCategoryFilter={true}
                              defaultCategory="all"
                              columns={4}
                              cardHeight="160px"
                            />
                          </div>
                          <div className="grid grid-cols-[1fr_120px] gap-2">
                            <div className="text-xs font-medium text-gray-600">Qtd. imagens</div>
                            <input
                              type="number"
                              min={1}
                              max={8}
                              value={getImageOptions(selectedSectionIdx).count}
                              onChange={(e) => setImageOption(selectedSectionIdx, { count: Number(e.target.value) || 1 })}
                              className="px-2 py-1 border rounded text-sm"
                            />
                          </div>
                          <button
                            onClick={() => {
                              const options = getImageOptions(selectedSectionIdx)
                              handleGenerateSectionImages(selectedSectionIdx, options.count, options.styles, options.prompt, options.model)
                            }}
                            className="px-3 py-2 bg-indigo-600 text-white rounded-md text-sm flex items-center gap-2 hover:bg-indigo-700"
                          >
                            <ImageIcon className="w-4 h-4" />
                            Gerar Imagem com IA
                          </button>
                          {/* Upload drag-and-drop */}
                          <ImageDropZone
                            jobId={id}
                            onImageInsert={async (markdownSnippet, imagePath) => {
                              if (imagePath) {
                                // Add image to section images array
                                const updated = [...chapters]
                                const chapter = updated[selectedChapterIdx]
                                if (!chapter) return
                                const sections = [...(chapter.sections || [])]
                                const images = [...(currentSection?.images || []), { path: imagePath, caption: '' }]
                                sections[selectedSectionIdx] = { ...currentSection, images }
                                chapter.sections = sections
                                updated[selectedChapterIdx] = chapter
                                setChapters(updated)
                              }
                              handleSectionFieldChange('content', `${currentSection?.content || ''}${markdownSnippet}`)
                            }}
                            onApplyStyles={async (image) => {
                              const updated = [...chapters]
                              const chapter = updated[selectedChapterIdx]
                              if (!chapter) return
                              const sections = [...(chapter.sections || [])]
                              const sec = sections[selectedSectionIdx]
                              const images = [...(sec?.images || []), { path: image.path, caption: image.name?.replace(/\.[^.]+$/, '') || '' }]
                              const newIndex = images.length - 1
                              sections[selectedSectionIdx] = { ...sec, images }
                              chapter.sections = sections
                              updated[selectedChapterIdx] = chapter
                              setChapters(updated)
                              if (draftPlan) await savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
                              await handleRestyleWithSectionStyles({ path: image.path }, newIndex)
                            }}
                            applyStylesLoading={restyleWithStylesLoading}
                          />
                          <div className="text-[10px] text-gray-400 text-center">ou selecione um arquivo</div>
                          <input
                            type="file"
                            accept="image/*"
                            onChange={(e) => {
                              const file = e.target.files?.[0]
                              if (file) {
                                handleUploadImage(selectedSectionIdx, file)
                              }
                            }}
                            className="text-xs"
                          />
                          <button
                            type="button"
                            onClick={() => {
                              setAddImageFromUrlUrl('')
                              setAddImageFromUrlCaption('')
                              setIsAddImageFromUrlModalOpen(true)
                            }}
                            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm flex items-center gap-2 hover:bg-gray-50 dark:hover:bg-gray-800"
                          >
                            <Link className="w-4 h-4" />
                            Buscar imagens na web
                          </button>
                        </div>
                      )}
                      savedTitle="Imagens da seção"
                      savedImages={(() => {
                        const sectionImages = currentSectionImagesAll
                        const isSlideImg = (img: unknown) =>
                          typeof img === 'object' && img !== null && ((img as { source?: string }).source === 'slide' || (img as { caption?: string }).caption?.startsWith('Slide '))
                        const sk = getSectionKey()
                        const hasSlidePreview = (sectionSlidePrompts[sk] || []).length > 0
                        const savedListImages = hasSlidePreview ? sectionImages.filter((img) => !isSlideImg(img)) : sectionImages
                        const baseImages = currentSection?.images || []
                        const sectionImagePath = (currentSection as { image_path?: string })?.image_path
                        const isFromContent = (path: string) => {
                          const p = (path || '').trim()
                          const inSection = baseImages.some((i) => ((typeof i === 'object' && (i as { path?: string })?.path) || String(i)).trim() === p)
                          return p && !inSection && (sectionImagePath ?? '').trim() !== p
                        }
                        const sectionImageIndexByPath = (path: string) =>
                          baseImages.findIndex((i) => ((typeof i === 'object' && (i as { path?: string })?.path) || String(i)).trim() === (path || '').trim())
                        return savedListImages.map((img, idx) => {
                          const path = typeof img === 'object' && img != null && 'path' in img ? (img as { path: string }).path : String(img)
                          const caption = typeof img === 'object' && img != null && 'caption' in img ? (img as { caption?: string }).caption : ''
                          const fromContent = isFromContent(path)
                          const realIdxInSection = sectionImageIndexByPath(path)
                          return {
                            key: `img-${idx}-${path}`,
                            src: buildFileUrl(path),
                            label: caption || `Imagem ${idx + 1}`,
                            meta: (
                              <div className="space-y-2">
                                <label className="text-xs font-medium text-gray-500 dark:text-gray-400">Legenda</label>
                                <input
                                  value={caption || ''}
                                  onChange={(e) => {
                                    const newCaption = e.target.value
                                    if (fromContent) {
                                      const newContent = replaceMarkdownImageCaption(currentSection?.content, path, newCaption)
                                      handleSectionFieldChange('content', newContent)
                                      if (draftPlan) void savePlan()
                                    } else {
                                      const updated = [...chapters]
                                      const chapter = updated[selectedChapterIdx]
                                      if (!chapter) return
                                      const sections = [...(chapter.sections || [])]
                                      const sec = sections[selectedSectionIdx]
                                      const images = [...(sec?.images || [])]
                                      if (realIdxInSection >= 0 && realIdxInSection < images.length) {
                                        images[realIdxInSection] = { ...images[realIdxInSection], caption: newCaption }
                                        sections[selectedSectionIdx] = { ...sec, images }
                                      } else if ((sectionImagePath ?? '').trim() === path.trim()) {
                                        sections[selectedSectionIdx] = { ...sec, images: [...images, { path, caption: newCaption }], image_path: undefined }
                                      } else return
                                      chapter.sections = sections
                                      updated[selectedChapterIdx] = chapter
                                      setChapters(updated)
                                      if (draftPlan) {
                                        void savePlan({ ...draftPlan, [getChapterKey(draftPlan)]: updated })
                                        setCaptionSavedIndex(realIdxInSection >= 0 ? realIdxInSection : images.length)
                                        setTimeout(() => setCaptionSavedIndex(null), 1500)
                                      }
                                    }
                                  }}
                                  className="w-full px-2 py-1 border rounded text-xs"
                                  placeholder="Legenda da imagem"
                                />
                                {!fromContent && captionSavedIndex === realIdxInSection && (
                                  <div className="text-[11px] text-emerald-600">Legenda salva ✅</div>
                                )}
                                <button
                                  type="button"
                                  onClick={() => {
                                    const cap = caption || 'Imagem'
                                    handleSectionFieldChange('content', `${currentSection?.content || ''}\n\n![${cap}](${path})\n\n`)
                                    void savePlan()
                                  }}
                                  className="text-xs text-emerald-600 hover:underline"
                                >
                                  Inserir no texto
                                </button>
                              </div>
                            ),
                            actions: (
                              <div className="flex items-center gap-2">
                                <button
                                  type="button"
                                  onClick={() => handleRestyleWithSectionStyles({ path, caption }, realIdxInSection >= 0 ? realIdxInSection : 0)}
                                  disabled={restyleWithStylesLoading}
                                  className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300 hover:bg-indigo-200 dark:hover:bg-indigo-800/50 disabled:opacity-50"
                                  title="Aplicar os estilos escolhidos na seção a esta imagem"
                                >
                                  <Palette className="w-3.5 h-3.5" />
                                  {restyleWithStylesLoading ? 'Aplicando...' : 'Aplicar estilos'}
                                </button>
                                <button
                                  onClick={() => handleDeleteImage(selectedSectionIdx, path)}
                                  className="text-xs text-red-500"
                                >
                                  Remover
                                </button>
                              </div>
                            ),
                          }
                        })
                      })()}
                    />
                    {(currentSection?.images || []).length > 0 && (
                      <div className="flex flex-wrap gap-2">
                        <button
                          onClick={() => savePlan()}
                          className="flex-1 min-w-[140px] px-3 py-2 bg-emerald-600 text-white rounded-md text-sm flex items-center justify-center gap-2 hover:bg-emerald-700"
                        >
                          <Save className="w-4 h-4" />
                          Salvar Imagens
                        </button>
                        {currentSection?.content?.trim() && (
                          <button
                            type="button"
                            onClick={handlePlaceImagesInText}
                            disabled={placeImagesInTextLoading}
                            className="flex-1 min-w-[180px] px-3 py-2 bg-amber-600 text-white rounded-md text-sm flex items-center justify-center gap-2 hover:bg-amber-700 disabled:opacity-50"
                            title="Usa IA para inserir as imagens da seção entre os parágrafos do texto e atualiza o conteúdo."
                          >
                            {placeImagesInTextLoading ? (
                              <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                              <FileText className="w-4 h-4" />
                            )}
                            {placeImagesInTextLoading ? 'Posicionando...' : 'Posicionar imagens no texto (IA)'}
                          </button>
                        )}
                      </div>
                    )}

                    <div className="space-y-3">
                      <h3 className="text-sm font-semibold">Ferramentas</h3>
                      <ScriptLabTextTools
                        text={currentSection?.content || ''}
                        onTextChange={handleSectionContentUpdate}
                        jobId={id}
                      />

                      {/* Diagram Generator - inline collapsible */}
                      <div className="border rounded-md overflow-hidden">
                        <button
                          onClick={() => setIsDiagramInlineOpen(!isDiagramInlineOpen)}
                          className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 flex items-center gap-2 justify-between"
                        >
                          <div className="flex items-center gap-2">
                            <LayoutList className="w-4 h-4 text-purple-600" />
                            <span>Diagrama / Mapa Mental (IA)</span>
                          </div>
                          <div className="flex items-center gap-1">
                            <button
                              onClick={(e) => { e.stopPropagation(); setIsDiagramModalOpen(true) }}
                              className="text-[10px] text-gray-400 hover:text-gray-600 px-1"
                              title="Abrir em tela cheia"
                            >
                              ⛶
                            </button>
                            <span className="text-xs text-gray-400">{isDiagramInlineOpen ? '▲' : '▼'}</span>
                          </div>
                        </button>
                        {isDiagramInlineOpen && (
                          <div className="border-t p-3">
                            <DeferredBookPanel>
                              <DiagramGenerator
                                initialText={currentSection?.content || ''}
                                onInsert={(content: string) => {
                                  handleSectionContentUpdate(`${currentSection?.content || ''}\n${content}`)
                                  setIsDiagramInlineOpen(false)
                                }}
                                onSaveImage={async (imageBase64: string, caption: string) => {
                                  if (!id) return
                                  const byteString = atob(imageBase64)
                                  const ab = new ArrayBuffer(byteString.length)
                                  const ia = new Uint8Array(ab)
                                  for (let i = 0; i < byteString.length; i++) {
                                    ia[i] = byteString.charCodeAt(i)
                                  }
                                  const blob = new Blob([ab], { type: 'image/png' })
                                  const file = new File([blob], `diagram-${Date.now()}.png`, { type: 'image/png' })
                                  await handleUploadImage(selectedSectionIdx, file, caption)
                                }}
                              />
                            </DeferredBookPanel>
                          </div>
                        )}
                      </div>

                      {/* Code Explainer - inline collapsible */}
                      <div className="border rounded-md overflow-hidden">
                        <button
                          onClick={() => setIsCodeExplainerInlineOpen(!isCodeExplainerInlineOpen)}
                          className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 flex items-center gap-2 justify-between"
                        >
                          <div className="flex items-center gap-2">
                            <Code className="w-4 h-4 text-purple-600" />
                            <span>Código Fonte / Tutorial (IA)</span>
                          </div>
                          <div className="flex items-center gap-1">
                            <button
                              onClick={(e) => { e.stopPropagation(); setIsCodeExplainerModalOpen(true) }}
                              className="text-[10px] text-gray-400 hover:text-gray-600 px-1"
                              title="Abrir em tela cheia"
                            >
                              ⛶
                            </button>
                            <span className="text-xs text-gray-400">{isCodeExplainerInlineOpen ? '▲' : '▼'}</span>
                          </div>
                        </button>
                        {isCodeExplainerInlineOpen && (
                          <div className="border-t">
                            <DeferredBookPanel>
                              <CodeExplainer
                                initialText={currentSection?.content || ''}
                                context={`Livro: ${draftPlan?.title}. Capítulo: ${currentChapter?.title}. Seção: ${currentSection?.title}`}
                                onInsert={(content: string) => {
                                  handleSectionFieldChange('content', `${currentSection?.content || ''}\n${content}`)
                                  setIsCodeExplainerInlineOpen(false)
                                }}
                              />
                            </DeferredBookPanel>
                          </div>
                        )}
                      </div>

                      <MathFormulaField
                        onInsert={(latex, mode) => {
                          const snippet = mode === 'block'
                            ? `\n\n$$\n${latex}\n$$\n\n`
                            : `$${latex}$`
                          handleSectionFieldChange('content', `${currentSection?.content || ''}${snippet}`)
                        }}
                      />
                      <DeferredBookPanel>
                        <MermaidEditor
                          onInsert={(snippet: string) => {
                            handleSectionFieldChange('content', `${currentSection?.content || ''}${snippet}`)
                          }}
                        />
                      </DeferredBookPanel>
                    </div>
                  </div>

                  <div className="bg-gray-50 border rounded-lg p-4 space-y-3">
                    <h3 className="text-sm font-semibold">Códigos</h3>
                    <div className="flex flex-wrap gap-2">
                      <button
                        onClick={handleExtractCodes}
                        className="px-3 py-2 border rounded-md text-sm flex items-center gap-2"
                      >
                        <Sparkles className="w-4 h-4" />
                        Extrair
                      </button>
                      <button
                        onClick={() => {
                          const updated = [...chapters]
                          const chapter = updated[selectedChapterIdx]
                          if (!chapter) return
                          const sections = [...(chapter.sections || [])]
                          const codes = [...(currentSection?.code_blocks || []), { language: 'text', title: 'Novo código', content: '' }]
                          sections[selectedSectionIdx] = { ...currentSection, code_blocks: codes }
                          chapter.sections = sections
                          updated[selectedChapterIdx] = chapter
                          setChapters(updated)
                        }}
                        className="px-3 py-2 border rounded-md text-sm flex items-center gap-2"
                      >
                        <Plus className="w-4 h-4" />
                        Adicionar
                      </button>
                    </div>
                    <div className="space-y-3">
                      {(currentSection?.code_blocks || []).map((code, idx) => (
                        <div key={`code-${idx}`} className="border rounded-md p-3 space-y-2">
                          <input
                            value={code.title || ''}
                            onChange={(e) => {
                              const updated = [...chapters]
                              const chapter = updated[selectedChapterIdx]
                              if (!chapter) return
                              const sections = [...(chapter.sections || [])]
                              const codes = [...(currentSection?.code_blocks || [])]
                              codes[idx] = { ...codes[idx], title: e.target.value }
                              sections[selectedSectionIdx] = { ...currentSection, code_blocks: codes }
                              chapter.sections = sections
                              updated[selectedChapterIdx] = chapter
                              setChapters(updated)
                            }}
                            className="w-full px-2 py-1 border rounded text-sm"
                            placeholder="Título"
                          />
                          <input
                            value={code.language || ''}
                            onChange={(e) => {
                              const updated = [...chapters]
                              const chapter = updated[selectedChapterIdx]
                              if (!chapter) return
                              const sections = [...(chapter.sections || [])]
                              const codes = [...(currentSection?.code_blocks || [])]
                              codes[idx] = { ...codes[idx], language: e.target.value }
                              sections[selectedSectionIdx] = { ...currentSection, code_blocks: codes }
                              chapter.sections = sections
                              updated[selectedChapterIdx] = chapter
                              setChapters(updated)
                            }}
                            className="w-full px-2 py-1 border rounded text-sm"
                            placeholder="Linguagem"
                          />
                          <textarea
                            value={code.content || ''}
                            onChange={(e) => {
                              const updated = [...chapters]
                              const chapter = updated[selectedChapterIdx]
                              if (!chapter) return
                              const sections = [...(chapter.sections || [])]
                              const codes = [...(currentSection?.code_blocks || [])]
                              codes[idx] = { ...codes[idx], content: e.target.value }
                              sections[selectedSectionIdx] = { ...currentSection, code_blocks: codes }
                              chapter.sections = sections
                              updated[selectedChapterIdx] = chapter
                              setChapters(updated)
                            }}
                            rows={6}
                            className="w-full px-2 py-1 border rounded text-sm font-mono"
                          />
                          <button
                            onClick={() => {
                              const lang = code.language || ''
                              const snippet = `\n\n\`\`\`${lang}\n${code.content || ''}\n\`\`\`\n\n`
                              handleSectionFieldChange('content', `${currentSection?.content || ''}${snippet}`)
                            }}
                            className="text-xs text-emerald-600"
                          >
                            Inserir no texto
                          </button>
                          <button
                            onClick={() => {
                              const updated = [...chapters]
                              const chapter = updated[selectedChapterIdx]
                              if (!chapter) return
                              const sections = [...(chapter.sections || [])]
                              const codes = [...(currentSection?.code_blocks || [])]
                              codes.splice(idx, 1)
                              sections[selectedSectionIdx] = { ...currentSection, code_blocks: codes }
                              chapter.sections = sections
                              updated[selectedChapterIdx] = chapter
                              setChapters(updated)
                            }}
                            className="text-xs text-red-500"
                          >
                            Remover
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

                <DeferredBookPanel>
                  <SectionQuestionsPanel
                    currentSection={currentSection}
                    selectedSectionIdx={selectedSectionIdx}
                    isGeneratingQuestions={isGeneratingQuestions}
                    onGenerateQuestions={handleGenerateQuestions}
                    onUpdateSectionAtIndex={updateSectionAtIndex}
                    onSavePlan={savePlan}
                    onAppendQuestionsToContent={() => {
                      if (!currentSection?.questions) return
                      const snippet = `\n\n---\n\n## 📝 Questões de Estudo\n\n${currentSection.questions}\n`
                      handleSectionFieldChange('content', `${currentSection?.content || ''}${snippet}`)
                    }}
                  />
                </DeferredBookPanel>

                <DeferredBookPanel>
                  <SectionActionsPanel
                    selectedSectionIdx={selectedSectionIdx}
                    currentSectionsLength={currentSections.length}
                    currentSectionTitle={currentSection?.title}
                    onMove={moveSection}
                    onInsertAt={insertSectionAt}
                    onClear={clearSectionContent}
                    onDelete={handleDeleteSection}
                  />
                </DeferredBookPanel>

                {/* Subseções: painel escondido no fim da tela (recolhido por padrão) */}
                <div className="border border-gray-200 dark:border-gray-600 rounded-lg overflow-hidden bg-gray-50/50 dark:bg-gray-900/30">
                  <button
                    type="button"
                    onClick={() => setSectionScreenSubsectionsPanelOpen((prev) => !prev)}
                    className="w-full flex items-center justify-between gap-2 px-4 py-3 text-left text-sm font-semibold text-gray-800 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/50"
                  >
                    <span>Subseções ({(currentSection?.subsections?.length ?? 0)})</span>
                    {sectionScreenSubsectionsPanelOpen ? <ChevronDown className="w-4 h-4 shrink-0" /> : <ChevronRight className="w-4 h-4 shrink-0" />}
                  </button>
                  {sectionScreenSubsectionsPanelOpen && (
                    <div className="border-t border-gray-200 dark:border-gray-600 p-4 space-y-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200">Subseções</h3>
                        <div className="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            onClick={() => addSubsection()}
                            className="px-2 py-1.5 text-xs border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700"
                          >
                            Adicionar subseção
                          </button>
                          <button
                            type="button"
                            onClick={() => void handlePlanSubsections()}
                            disabled={isPlanningSubsections}
                            className="px-2 py-1.5 text-xs border border-indigo-200 dark:border-indigo-700 rounded-md text-indigo-700 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 disabled:opacity-60 flex items-center gap-1"
                          >
                            {isPlanningSubsections ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                            Gerar subseções
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleGenerateSubsectionsText()}
                            disabled={isGeneratingSubsectionsText || !(currentSection?.subsections?.length)}
                            className="px-2 py-1.5 text-xs border border-emerald-200 dark:border-emerald-700 rounded-md text-emerald-700 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 disabled:opacity-60 flex items-center gap-1"
                          >
                            {isGeneratingSubsectionsText ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                            Gerar texto das subseções
                          </button>
                        </div>
                      </div>
                      {((currentSection?.subsections?.length ?? 0) > 0) && (
                        <ul className="space-y-3 w-full min-w-0">
                          {(currentSection?.subsections || []).map((sub, subIdx) => {
                            const subKey = getSubsectionKey(selectedSectionIdx, subIdx)
                            return (
                            <li key={subIdx} className="w-full min-w-0 border border-gray-200 dark:border-gray-600 rounded-md p-3 bg-white dark:bg-gray-800 space-y-2">
                              <div className="flex items-start gap-2 w-full min-w-0">
                                <span className="text-xs font-medium text-gray-500 dark:text-gray-400 shrink-0 pt-1.5">Objetivo</span>
                                <input
                                  value={sub.objective || ''}
                                  onChange={(e) => updateSubsectionAtIndex(undefined, subIdx, { objective: e.target.value })}
                                  placeholder="Objetivo desta subseção"
                                  className="flex-1 min-w-0 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 w-full"
                                />
                                <button
                                  type="button"
                                  onClick={() => removeSubsection(undefined, subIdx)}
                                  className="shrink-0 p-1 text-gray-400 hover:text-red-600 dark:hover:text-red-400"
                                  title="Remover subseção"
                                >
                                  <Trash2 className="w-4 h-4" />
                                </button>
                              </div>
                              <div className="flex flex-wrap items-center gap-4">
                                <div>
                                  <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 mr-1">Mín. caracteres</label>
                                  <input
                                    type="number"
                                    min={0}
                                    value={sub.min_text_length ?? ''}
                                    onChange={(e) => {
                                      const raw = e.target.value
                                      const num = raw === '' ? undefined : Math.max(0, Number(raw) || 0)
                                      updateSubsectionAtIndex(undefined, subIdx, { min_text_length: num })
                                    }}
                                    placeholder="Padrão"
                                    className="w-24 px-1.5 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800"
                                  />
                                </div>
                                <label className="flex items-center gap-2 cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={sub.has_source_code ?? false}
                                    onChange={(e) => updateSubsectionAtIndex(undefined, subIdx, { has_source_code: e.target.checked })}
                                    className="h-3.5 w-3.5 rounded border-gray-300"
                                  />
                                  <span className="text-xs text-gray-600 dark:text-gray-400">Incluir código fonte</span>
                                </label>
                              </div>
                              <div className="w-full min-w-0">
                                <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1">Conteúdo (Markdown)</label>
                                <MarkdownField
                                  value={sub.content || ''}
                                  onChange={(v) => updateSubsectionAtIndex(undefined, subIdx, { content: v })}
                                  placeholder="Conteúdo da subseção (edite ou use Gerar texto das subseções). Suporta **negrito**, *itálico*, títulos."
                                  rows={14}
                                  minHeight="16rem"
                                  showPreview={true}
                                  className="text-sm font-mono w-full"
                                />
                              </div>
                              <AuthorStyleSelector
                                selectedStyles={sub.author_styles || []}
                                onChange={(styles) => updateSubsectionAtIndex(undefined, subIdx, { author_styles: styles })}
                                label="✨ Estilos de Autor"
                                description="Selecione estilos para orientar a escrita desta subseção"
                              />
                              <div className="flex items-center gap-2">
                                <button
                                  type="button"
                                  onClick={() => applyAuthorStylesToSubsectionAt(subIdx)}
                                  disabled={applyingAuthorStylesSubsectionIdx !== null || !(sub.author_styles || []).length}
                                  className="px-3 py-2 border rounded-md text-sm flex items-center gap-2 disabled:opacity-60"
                                >
                                  {applyingAuthorStylesSubsectionIdx === subIdx ? (
                                    <>
                                      <Loader2 className="w-4 h-4 animate-spin" />
                                      Aplicando estilos...
                                    </>
                                  ) : (
                                    <>
                                      <Wand2 className="w-4 h-4" />
                                      Aplicar estilos no texto
                                    </>
                                  )}
                                </button>
                                <span className="text-xs text-gray-500">Reescreve o conteúdo com os estilos selecionados</span>
                              </div>
                              {/* Slides da subseção (gerar prompts, gerar slides, preview) */}
                              <div className="border-t border-gray-200 dark:border-gray-600 pt-3 mt-3 space-y-2">
                                <div className="flex flex-wrap items-center gap-2">
                                  <h4 className="text-xs font-semibold text-gray-600 dark:text-gray-400">Slides</h4>
                                  <input
                                    type="number"
                                    min={1}
                                    max={10}
                                    value={sectionSlideCounts[getSubsectionKey(selectedSectionIdx, subIdx)] ?? 3}
                                    onChange={(e) => {
                                      const v = Math.min(10, Math.max(1, Number(e.target.value) || 3))
                                      setSectionSlideCounts((prev) => ({ ...prev, [getSubsectionKey(selectedSectionIdx, subIdx)]: v }))
                                    }}
                                    className="w-10 px-1 py-0.5 border rounded text-xs"
                                  />
                                  <span className="text-xs text-gray-500">slides</span>
                                  <label className="flex items-center gap-1 cursor-pointer">
                                    <input
                                      type="checkbox"
                                      checked={sectionImagesWithoutText[getSubsectionKey(selectedSectionIdx, subIdx)] ?? false}
                                      onChange={(e) => setSectionImagesWithoutText((prev) => ({ ...prev, [getSubsectionKey(selectedSectionIdx, subIdx)]: e.target.checked }))}
                                      className="h-3 w-3 rounded border-gray-300"
                                    />
                                    <span className="text-xs">Sem texto</span>
                                  </label>
                                  <div className="flex items-center gap-1.5">
                                    <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Modelo:</span>
                                    <select
                                      value={sectionSlideModel[getSubsectionKey(selectedSectionIdx, subIdx)] || imageModels[0]?.id || ''}
                                      onChange={(e) =>
                                        setSectionSlideModel((prev) => ({
                                          ...prev,
                                          [getSubsectionKey(selectedSectionIdx, subIdx)]: e.target.value,
                                        }))
                                      }
                                      className="px-2 py-1 border rounded text-xs bg-white dark:bg-gray-800"
                                      title="Modelo de imagem para gerar slides desta subseção"
                                    >
                                      {imageModels.length ? (
                                        imageModels.map((m) => (
                                          <option key={m.id} value={m.id}>{m.name}</option>
                                        ))
                                      ) : (
                                        <option value="">Carregando...</option>
                                      )}
                                    </select>
                                  </div>
                                  <button
                                    type="button"
                                    onClick={() => void handleGenerateSubsectionPrompts(selectedSectionIdx, subIdx)}
                                    disabled={generatingSubsectionPromptsKey === `${selectedSectionIdx}-${subIdx}`}
                                    className="px-2 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1"
                                  >
                                    {generatingSubsectionPromptsKey === `${selectedSectionIdx}-${subIdx}` ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                                    Gerar prompts
                                  </button>
                                  {(sub.slide_prompts?.length ?? 0) > 0 && (
                                    <>
                                      <button
                                        type="button"
                                        onClick={() => void handleGenerateSubsectionSlides(selectedSectionIdx, subIdx)}
                                        disabled={isGeneratingSubsectionSlidesKey !== null}
                                        className="px-2 py-1 text-xs bg-emerald-600 text-white rounded hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-1"
                                        title="Gerar imagens dos slides"
                                      >
                                        {isGeneratingSubsectionSlidesKey === getSubsectionKey(selectedSectionIdx, subIdx) ? <Loader2 className="w-3 h-3 animate-spin" /> : <Layers className="w-3 h-3" />}
                                        Gerar slides
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => handleClearSubsectionPrompts(selectedSectionIdx, subIdx)}
                                        className="px-2 py-1 text-xs text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded flex items-center gap-1"
                                      >
                                        <Trash2 className="w-3 h-3" /> Remover
                                      </button>
                                    </>
                                  )}
                                </div>
                                {/* Multi-estilo: estilos visuais para os slides desta subseção */}
                                <div className="space-y-1.5">
                                  <div className="text-xs font-medium text-gray-600 dark:text-gray-400">Estilos visuais (multi-estilo)</div>
                                  <StyleGrid
                                    selectedStyles={(sub as { slide_styles?: string[] }).slide_styles ?? draftPlan?.book_slide_styles ?? []}
                                    onChange={(styles) => updateSubsectionAtIndex(undefined, subIdx, { slide_styles: styles })}
                                    maxSelection={10}
                                    showSearch={true}
                                    showCategoryFilter={true}
                                    defaultCategory="all"
                                    columns={4}
                                    cardHeight="120px"
                                  />
                                </div>
                                {(sub.slide_prompts?.length ?? 0) > 0 && (
                                  <div className="flex flex-wrap items-center gap-3 py-2 px-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                                    <label className="flex items-center gap-2 cursor-pointer">
                                      <input
                                        type="checkbox"
                                        checked={sectionCodeSlideNoModel[getSubsectionKey(selectedSectionIdx, subIdx)] ?? false}
                                        onChange={(e) =>
                                          setSectionCodeSlideNoModel((prev) => ({
                                            ...prev,
                                            [getSubsectionKey(selectedSectionIdx, subIdx)]: e.target.checked,
                                          }))
                                        }
                                        className="h-3.5 w-3.5 rounded border-gray-300"
                                      />
                                      <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Texto inserido sem modelo</span>
                                    </label>
                                    <div className="flex items-center gap-2">
                                      <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Modelo para slides:</span>
                                      <select
                                        value={sectionSlideModel[getSubsectionKey(selectedSectionIdx, subIdx)] || imageModels[0]?.id || ''}
                                        onChange={(e) =>
                                          setSectionSlideModel((prev) => ({
                                            ...prev,
                                            [getSubsectionKey(selectedSectionIdx, subIdx)]: e.target.value,
                                          }))
                                        }
                                        className="px-2 py-1 border rounded text-xs bg-white dark:bg-gray-800"
                                        title="Usado ao gerar slides desta subseção"
                                      >
                                        {imageModels.length ? (
                                          imageModels.map((m) => (
                                            <option key={m.id} value={m.id}>
                                              {m.name}
                                            </option>
                                          ))
                                        ) : (
                                          <option value="">Carregando...</option>
                                        )}
                                      </select>
                                    </div>
                                  </div>
                                )}
                                {(() => {
                                  const subPrompts = (sub.slide_prompts || []) as SlidePromptItem[]
                                  const subImages = sub?.images || []
                                  const isSlideImg = (img: unknown) =>
                                    typeof img === 'object' && img !== null && ((img as { source?: string }).source === 'slide' || (img as { caption?: string }).caption?.startsWith('Slide '))
                                  const slideEntries = subImages.filter(isSlideImg).map((img, j) => ({
                                    path: (img as { path: string }).path,
                                    caption: (img as { caption?: string }).caption ?? `Slide ${j + 1}`,
                                  }))
                                  const displaySlides = slideEntries.map((slide, i) => ({
                                    ...slide,
                                    prompt: (subPrompts[i]?.prompt ?? subPrompts[i]?.text ?? '').trim() || undefined,
                                  }))
                                  return (subPrompts.length > 0 || displaySlides.length > 0) ? (
                                    <div className="rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-2">
                                      <div className="mb-2 flex justify-end">
                                        {renderInsertBlankSlideButton(
                                          () => handleCreateBlankSubsectionSlide(selectedSectionIdx, subIdx),
                                          creatingBlankSubsectionSlideKey === subKey,
                                        )}
                                      </div>
                                      <LessonSlidePreview
                                        slides={displaySlides}
                                        isGenerating={isGeneratingSubsectionSlidesKey === subKey}
                                        expectedCount={subPrompts.length}
                                        onMove={(fromIndex: number, toIndex: number) =>
                                          handleMoveSubsectionSlide(selectedSectionIdx, subIdx, fromIndex, toIndex)
                                        }
                                        onEdit={(slideIdx: number, path: string) =>
                                          openBookImageEditor({
                                            scope: 'subsection',
                                            kind: 'slide',
                                            chapterIdx: selectedChapterIdx,
                                            sectionIdx: selectedSectionIdx,
                                            subsectionIdx: subIdx,
                                            imagePath: path,
                                            title: `${sub.title || sub.objective || `Subseção ${subIdx + 1}`} — slide ${slideIdx + 1}`,
                                            caption: displaySlides[slideIdx]?.caption ?? `Slide ${slideIdx + 1}`,
                                          })
                                        }
                                        onDelete={async (slideIdx: number, path: string) => {
                                          setSubsectionSlideDeletingIndex(slideIdx)
                                          await handleDeleteSubsectionSlideImage(selectedSectionIdx, subIdx, path)
                                          setSubsectionSlideDeletingIndex(null)
                                        }}
                                        deletingIndex={subsectionSlideDeletingIndex}
                                      />
                                      {displaySlides.length === 0 && isGeneratingSubsectionSlidesKey !== subKey && (
                                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Nenhum slide gerado. Clique em &quot;Gerar slides&quot;.</p>
                                      )}
                                    </div>
                                  ) : null
                                })()}
                              </div>
                              <div className="border-t border-gray-200 dark:border-gray-600 pt-3 mt-3">
                                <h4 className="text-xs font-semibold text-gray-600 dark:text-gray-400 uppercase mb-2">Imagens da subseção</h4>
                                <div
                                  role="button"
                                  tabIndex={0}
                                  className={`rounded-lg border-2 border-dashed p-4 text-center transition-colors ${subsectionDropZoneKey === subKey ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/20' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'}`}
                                  onDragEnter={(e) => { e.preventDefault(); e.stopPropagation(); setSubsectionDropZoneKey(subKey) }}
                                  onDragOver={(e) => { e.preventDefault(); e.stopPropagation() }}
                                  onDragLeave={(e) => { e.preventDefault(); e.stopPropagation(); setSubsectionDropZoneKey(null) }}
                                  onDrop={(e) => {
                                    e.preventDefault()
                                    e.stopPropagation()
                                    setSubsectionDropZoneKey(null)
                                    const files = e.dataTransfer?.files
                                    if (files) {
                                      for (let i = 0; i < files.length; i++) {
                                        const f = files[i]
                                        if (f.type.startsWith('image/')) void handleUploadSubsectionImage(selectedSectionIdx, subIdx, f)
                                      }
                                    }
                                  }}
                                  onClick={(e) => { e.preventDefault(); document.getElementById(`subsection-file-input-list-${subKey}`)?.click() }}
                                >
                                  <input
                                    id={`subsection-file-input-list-${subKey}`}
                                    type="file"
                                    accept="image/*"
                                    multiple
                                    className="hidden"
                                    onChange={(e) => {
                                      const fileList = e.target.files
                                      if (fileList) {
                                        for (let i = 0; i < fileList.length; i++) {
                                          const file = fileList[i]
                                          if (file?.type.startsWith('image/')) void handleUploadSubsectionImage(selectedSectionIdx, subIdx, file)
                                        }
                                      }
                                      e.target.value = ''
                                    }}
                                  />
                                  {subsectionUploadingKey === subKey ? (
                                    <span className="text-xs text-gray-500 dark:text-gray-400 flex items-center justify-center gap-2">
                                      <Loader2 className="w-4 h-4 animate-spin" />
                                      Enviando...
                                    </span>
                                  ) : (
                                    <span className="text-xs text-gray-500 dark:text-gray-400">Arraste imagens ou clique para enviar</span>
                                  )}
                                </div>
                                {(sub?.images?.length ?? 0) > 0 ? (
                                  <SectionImagePreview
                                    images={(sub.images || []).map((img: unknown) =>
                                      typeof img === 'object' && img !== null && 'path' in (img as object)
                                        ? { path: (img as { path: string }).path, caption: (img as { caption?: string }).caption }
                                        : { path: String(img), caption: '' }
                                    )}
                                    onEditAdvanced={(img, displayIndex) =>
                                      openBookImageEditor({
                                        scope: 'subsection',
                                        kind: img.caption?.startsWith('Slide ') ? 'slide' : 'image',
                                        chapterIdx: selectedChapterIdx,
                                        sectionIdx: selectedSectionIdx,
                                        subsectionIdx: subIdx,
                                        imagePath: img.path,
                                        title: `${sub.title || sub.objective || `Subseção ${subIdx + 1}`} — imagem ${displayIndex + 1}`,
                                        caption: img.caption,
                                      })
                                    }
                                    onRestyleWithSectionStyles={(img, displayIndex) => handleRestyleWithSubsectionStyles(selectedSectionIdx, subIdx, img, displayIndex)}
                                    restyleWithSectionStylesLoading={restyleWithStylesLoading}
                                    onDelete={(imagePath) => void handleDeleteSubsectionSlideImage(selectedSectionIdx, subIdx, imagePath)}
                                    onCaptionChange={(index, caption) => handleSubsectionImageCaptionChange(selectedSectionIdx, subIdx, index, caption)}
                                    compact
                                  />
                                ) : (
                                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">Nenhuma imagem. Arraste ou clique na área acima para enviar.</p>
                                )}
                              </div>
                              {/* Preview EPUB desta subseção */}
                              {subsectionSectionsForEpubPreview[subIdx] && (
                                <div className="border-t border-gray-200 dark:border-gray-600 pt-3 mt-3">
                                  <DeferredBookPanel>
                                    <EpubPreview
                                      mode="section"
                                      jobId={id}
                                      apiKey={getApiKey(job)}
                                      section={subsectionSectionsForEpubPreview[subIdx]}
                                      chapterNumber={selectedChapterIdx + 1}
                                      sectionNumber={selectedSectionIdx + 1}
                                    />
                                  </DeferredBookPanel>
                                </div>
                              )}
                            </li>
                            )
                          })}
                        </ul>
                      )}
                    </div>
                  )}
                </div>

                {/* Log Viewer for Section Screen */}
                <LogViewer logs={logs} initiallyExpanded={false} title={'Logs de Execução (Seção)'} />
              </div>
        )
      }

      {/* Metadata Tab */}
      {activeTab === 'metadata' && (
        <DeferredBookPanel>
          <BookMetadataTab
            draftPlan={draftPlan}
            setDraftPlan={setDraftPlan}
            savePlan={savePlan}
            saving={saving}
            metadataAuthorStyles={metadataAuthorStyles}
            BOOK_GENRES={BOOK_GENRES}
            handleReplanBookStyle={handleReplanBookStyle}
            isGeneratingChapters={isGeneratingChapters}
            plan={plan}
            logs={logs}
            bookId={id}
            onTranslateStarted={(jobId: string, options?: { fromScratch?: boolean }) => {
              if (id) {
                setActiveTranslateJob(jobId, id)
                if (options?.fromScratch !== false) {
                  setLastTranslatedUnitKeys([])
                  try {
                    localStorage.removeItem(`book_translated_units_${id}`)
                  } catch (_) { /* ignore */ }
                }
              }
            }}
            onTranslateComplete={() => refetch(true)}
            getApiKey={() => getApiKey(job)}
          />
        </DeferredBookPanel>
      )}
      {/* Diagram Generator Modal */}
      {isDiagramModalOpen && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
          <div className="bg-white dark:bg-gray-900 rounded-2xl w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden border border-gray-200 dark:border-gray-800 animate-in fade-in zoom-in duration-200">
            <div className="flex items-center justify-between p-6 border-b border-gray-100 dark:border-gray-800">
              <h3 className="text-xl font-bold text-gray-900 dark:text-white">Gerar Diagrama / Mapa Mental</h3>
              <button
                onClick={() => setIsDiagramModalOpen(false)}
                className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-full transition-colors"
              >
                <X className="w-6 h-6 text-gray-400" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              <DeferredBookPanel>
                <DiagramGenerator
                  initialText={currentSection?.content || ''}
                  onInsert={(content: string) => {
                    handleSectionContentUpdate(`${currentSection?.content || ''}\n${content}`)
                    setIsDiagramModalOpen(false)
                  }}
                  onSaveImage={async (imageBase64: string, caption: string) => {
                    if (!id) return
                    // Convert base64 to File and upload as section image
                    const byteString = atob(imageBase64)
                    const ab = new ArrayBuffer(byteString.length)
                    const ia = new Uint8Array(ab)
                    for (let i = 0; i < byteString.length; i++) {
                      ia[i] = byteString.charCodeAt(i)
                    }
                    const blob = new Blob([ab], { type: 'image/png' })
                    const file = new File([blob], `diagram-${Date.now()}.png`, { type: 'image/png' })
                    await handleUploadImage(selectedSectionIdx, file, caption)
                  }}
                />
              </DeferredBookPanel>
            </div>
          </div>
        </div>
      )}
      {/* Code Explainer Modal */}
      {isCodeExplainerModalOpen && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
          <div className="bg-white dark:bg-gray-900 rounded-2xl w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden border border-gray-200 dark:border-gray-800 animate-in fade-in zoom-in duration-200">
            <div className="flex items-center justify-between p-6 border-b border-gray-100 dark:border-gray-800">
              <h3 className="text-xl font-bold text-gray-900 dark:text-white">Explicação de Código / Tutorial</h3>
              <button
                onClick={() => setIsCodeExplainerModalOpen(false)}
                className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-full transition-colors"
              >
                <X className="w-6 h-6 text-gray-400" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">
              <DeferredBookPanel>
                <CodeExplainer
                  initialText={currentSection?.content || ''}
                  context={`Livro: ${draftPlan?.title}. Capítulo: ${currentChapter?.title}. Seção: ${currentSection?.title}`}
                  onInsert={(content: string) => {
                    handleSectionFieldChange('content', `${currentSection?.content || ''}\n${content}`)
                    setIsCodeExplainerModalOpen(false)
                  }}
                />
              </DeferredBookPanel>
            </div>
          </div>
        </div>
      )}
      {/* FAL Kontext Modal (editar imagem da seção) */}
      {isKontextModalOpen && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
          <div className="bg-white dark:bg-gray-900 rounded-2xl w-full max-w-md flex flex-col shadow-2xl border border-gray-200 dark:border-gray-800">
            <div className="flex items-center justify-between p-4 border-b border-gray-100 dark:border-gray-800">
              <h3 className="text-lg font-bold text-gray-900 dark:text-white flex items-center gap-2">
                <Wand2 className="w-5 h-5 text-amber-500" />
                Editar com FAL Kontext
              </h3>
              <button
                onClick={() => { setIsKontextModalOpen(false); setKontextPrompt('') }}
                className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-full transition-colors"
              >
                <X className="w-5 h-5 text-gray-400" />
              </button>
            </div>
            <div className="p-4 space-y-3">
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Imagem {kontextImageIndex + 1} da seção. Descreva a alteração desejada.
              </p>
              <textarea
                value={kontextPrompt}
                onChange={(e) => setKontextPrompt(e.target.value)}
                placeholder="Ex: Troque o céu por um pôr do sol."
                rows={3}
                className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm"
              />
            </div>
            <div className="flex justify-end gap-2 p-4 border-t border-gray-100 dark:border-gray-800">
              <button
                onClick={() => { setIsKontextModalOpen(false); setKontextPrompt('') }}
                className="px-3 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg"
              >
                Cancelar
              </button>
              <button
                onClick={handleKontextSectionSubmit}
                disabled={kontextLoading || !kontextPrompt.trim()}
                className="flex items-center gap-2 px-4 py-2 bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:opacity-50 text-sm"
              >
                {kontextLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Wand2 className="w-4 h-4" />}
                {kontextLoading ? 'Aplicando...' : 'Aplicar'}
              </button>
            </div>
          </div>
        </div>
      )}
      {/* Modal: Adicionar imagem por URL (buscar na web via Google) */}
      {isAddImageFromUrlModalOpen && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
          <div className="bg-white dark:bg-gray-900 rounded-2xl w-full max-w-md flex flex-col shadow-2xl border border-gray-200 dark:border-gray-800">
            <div className="flex items-center justify-between p-4 border-b border-gray-100 dark:border-gray-800">
              <h3 className="text-lg font-bold text-gray-900 dark:text-white flex items-center gap-2">
                <Link className="w-5 h-5 text-blue-500" />
                Buscar imagens na web
              </h3>
              <button
                onClick={() => { setIsAddImageFromUrlModalOpen(false); setAddImageFromUrlUrl(''); setAddImageFromUrlCaption(''); setAddImageFromUrlSearchQuery('') }}
                className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-full transition-colors"
              >
                <X className="w-5 h-5 text-gray-400" />
              </button>
            </div>
            <div className="p-4 space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Buscar no Google Imagens</label>
                <div className="flex gap-2 flex-wrap">
                  <input
                    type="text"
                    value={addImageFromUrlSearchQuery}
                    onChange={(e) => setAddImageFromUrlSearchQuery(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); window.open(`https://www.google.com/search?tbm=isch&q=${encodeURIComponent(addImageFromUrlSearchQuery.trim() || '')}`, '_blank', 'noopener,noreferrer') } }}
                    placeholder="Ex: diagrama célula animal, paisagem medieval..."
                    className="flex-1 min-w-[140px] rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm"
                  />
                  <button
                    type="button"
                    onClick={handleGenerateSectionSearchQuery}
                    disabled={addImageFromUrlSearchLoading}
                    className="px-3 py-2 rounded-lg bg-emerald-100 dark:bg-emerald-900/40 hover:bg-emerald-200 dark:hover:bg-emerald-800/50 text-emerald-800 dark:text-emerald-200 text-sm font-medium flex items-center gap-1.5 shrink-0 disabled:opacity-50"
                    title="Gera 6 palavras a partir do título e conteúdo da seção (IA)"
                  >
                    {addImageFromUrlSearchLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                    {addImageFromUrlSearchLoading ? 'Gerando...' : 'Resumo da seção (IA)'}
                  </button>
                  <button
                    type="button"
                    onClick={() => window.open(`https://www.google.com/search?tbm=isch&q=${encodeURIComponent(addImageFromUrlSearchQuery.trim() || '')}`, '_blank', 'noopener,noreferrer')}
                    className="px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-sm font-medium flex items-center gap-1.5 shrink-0"
                  >
                    <Search className="w-4 h-4" />
                    Abrir
                  </button>
                </div>
                <p className="mt-1.5 text-xs text-gray-500 dark:text-gray-400">
                  Use &quot;Resumo da seção (IA)&quot; para preencher a busca com 6 palavras geradas pelo modelo. O Google Imagens abrirá em nova aba; clique com o botão direito na imagem → &quot;Copiar endereço da imagem&quot;, depois cole abaixo.
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">URL da imagem</label>
                <input
                  type="url"
                  value={addImageFromUrlUrl}
                  onChange={(e) => setAddImageFromUrlUrl(e.target.value)}
                  placeholder="https://... (cole o endereço copiado do Google Imagens)"
                  className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm mt-1"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">Legenda (opcional)</label>
                <input
                  type="text"
                  value={addImageFromUrlCaption}
                  onChange={(e) => setAddImageFromUrlCaption(e.target.value)}
                  placeholder="Legenda da imagem"
                  className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm mt-1"
                />
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t border-gray-100 dark:border-gray-800">
              <button
                onClick={() => { setIsAddImageFromUrlModalOpen(false); setAddImageFromUrlUrl(''); setAddImageFromUrlCaption(''); setAddImageFromUrlSearchQuery('') }}
                className="px-3 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg"
              >
                Cancelar
              </button>
              <button
                onClick={handleAddSectionImageFromUrl}
                disabled={addImageFromUrlLoading || !addImageFromUrlUrl.trim()}
                className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm"
              >
                {addImageFromUrlLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Link className="w-4 h-4" />}
                {addImageFromUrlLoading ? 'Adicionando...' : 'Adicionar'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

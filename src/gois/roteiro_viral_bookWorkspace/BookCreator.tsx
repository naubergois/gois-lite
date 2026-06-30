/**
 * Book Creator Page
 * 
 * AI-powered book creation with chapters, covers, and dividers.
 * Corresponds to "📚 Criador de Livros" in Streamlit (books.py).
 * 
 * Features:
 * - Book information and structure wizard
 * - Design studio with tabs for:
 *   - 🖼️ Capa Frontal (Front Cover)
 *   - 📑 Capa Traseira (Back Cover)
 *   - 📖 Divisores de Capítulo (Chapter Dividers)
 * - Chapter content editor
 * - EPUB compilation
 */

import React, { useRef, useState, useEffect, useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import * as Dialog from '@radix-ui/react-dialog'
import { modelConfig } from '../services/ModelConfigService'
import {
  BookOpen,
  FileText,
  List,
  Loader2,
  Plus,
  Trash2,
  GripVertical,
  CheckCircle,
  Image,
  Sparkles,
  Download,
  Wand2,
  Palette,
  BookMarked,
  Edit3,
  FileDown,
  BarChart3,
  Bold,
  Italic,
  Heading1,
  Quote,
  Eye,
  EyeOff,
  RefreshCw,
  HelpCircle,
  MessageSquare,
} from 'lucide-react'
import { AuthorStyleSelector } from '../components/AuthorStyleSelector'
import { useExecutionMode } from '@/hooks/useExecutionMode'
import { useAppOptions } from '@/hooks/useAppOptions'
import { api, endpoints, ConfigSummary } from '@/lib/api'
import { getStoredGeminiApiKey } from '@/lib/apiKeys'
import { cn } from '@/lib/utils'
import { bookCreatorPath, bookLibraryEditPath } from '@/lib/bookRoutes'
import { buildFileUrl } from '@/lib/files'
import { MathFormulaField } from '@/components/MathFormulaField'
import { MermaidEditor } from '@/components/MermaidDiagram'
import { ImageDropZone } from '@/components/ImageDropZone'
import { ImageAssetsPanel } from '@/components/ImageAssetsPanel'
import MultiSelect from '@/components/MultiSelect'
import StyleGrid from '@/components/StyleGrid'
import { apiKeyService } from '@/services/ApiKeyService'
import QuestionStyleSelector, { DEFAULT_QUESTION_CONFIG, type QuestionConfig } from '@/components/QuestionStyleSelector'
import { EpubPreview } from '@/components/EpubPreview'
import { UnifiedChat, type UnifiedChatAction } from '@/components/UnifiedChat'
import { MarkdownField } from '@/components/MarkdownField'

import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css' // Ensure katex styling
import type { Section, Chapter, BookConfig, CoverConfig } from './BookCreator/types'
import {
  formatHttpError,
  pollBookStructureJob,
  resolveStructureJobId,
} from './bookCreatorFlowUtils'

/** Serializa capítulos do assistente para PUT /books (estrutura + orientação por capítulo). */
function buildApiStructureFromChapters(chapters: Chapter[]): Record<string, unknown>[] {
  return chapters.map((ch) => {
    const guide = (ch.creation_guide || '').trim()
    const row: Record<string, unknown> = {
      title: ch.title,
      purpose: ch.description || '',
      objective: ch.objective || '',
      description: ch.description || '',
      content: ch.content || '',
      sections: (ch.sections || []).map((s) => {
        const purpose = s.purpose || ''
        return {
          title: s.title,
          purpose,
          objective: purpose,
          content_directive: s.content_directive || purpose,
          content: s.content || '',
          min_text_length: s.min_text_length,
          has_source_code: s.has_source_code,
          questions: s.questions,
          num_questions: s.num_questions,
          question_board: s.question_board,
          question_type: s.question_type,
          question_difficulty: s.question_difficulty,
          question_include_answers: s.question_include_answers,
          question_include_explanation: s.question_include_explanation,
          images: s.images || [],
          code_blocks: s.code_blocks || [],
        }
      }),
    }
    if (guide) row.creation_guide = guide
    return row
  })
}

function buildWizardPlanningPayloadFromConfig(
  config: BookConfig,
  apiKey: string,
  executionMode: string,
): Record<string, unknown> {
  const author_inspiration =
    config.selectedAuthorStyles.length > 0
      ? config.selectedAuthorStyles.join(', ')
      : (config.authorStyle || config.authorName || null)
  return {
    title: config.title,
    topic: config.topic,
    subtitle: config.subtitle,
    draft: config.draft || undefined,
    audience: config.targetAudience || 'Público geral',
    tone: config.style || 'practical',
    num_chapters: config.numChapters || 10,
    category: 'Não-ficção',
    language: config.language || 'Português (Brasil)',
    depth: 'Standard',
    author: config.authorName || undefined,
    author_inspiration,
    author_styles: config.selectedAuthorStyles.length ? config.selectedAuthorStyles : undefined,
    api_key: apiKey || undefined,
    model_name:
      executionMode === 'full'
        ? modelConfig.getDefaultTextModel('full')
        : modelConfig.getDefaultTextModel('economic'),
    execution_mode: executionMode,
    default_min_text_length: config.defaultMinTextLength,
    default_has_source_code: config.defaultHasSourceCode,
    chapter_planning_instructions: (config.chapterPlanningInstructions || '').trim(),
  }
}

const applyImageMarkersForPreview = (
  content: string,
  images: Array<{ path: string; caption?: string }>,
  sectionLabel: string
) => {
  if (!content) return content
  if (!images || images.length === 0) return content
  if (!content.includes('[IMAGE:')) return content

  const tagRe = /\[IMAGE:\s*([^\]]+)\]/g
  let nextOrder = 0
  const replacements = images.map((img, idx) => {
    const baseCaption = img.caption || `Imagem ${idx + 1}`
    const caption = baseCaption.toLowerCase().startsWith('seção') || baseCaption.toLowerCase().startsWith('secao')
      ? baseCaption
      : `${sectionLabel} - Figura ${idx + 1}: ${baseCaption}`
    return `![${caption}](${buildFileUrl(img.path)})`
  })
  return content.replace(tagRe, (_, inner) => {
    const trimmed = (inner || '').trim()
    if (/^\d+$/.test(trimmed)) {
      const n = parseInt(trimmed, 10)
      if (n >= 1 && n <= replacements.length) return replacements[n - 1]
    }
    const repl = replacements[nextOrder % replacements.length]
    nextOrder += 1
    return repl
  })
}

export default function BookCreator() {
  const navigate = useNavigate()
  const { bookId } = useParams<{ bookId?: string }>()

  // ===== UI STATE =====
  const [isCreating, setIsCreating] = useState(false)
  const [isSavingPlanning, setIsSavingPlanning] = useState(false)
  const [isPlanningStructure, setIsPlanningStructure] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [successMessage, setSuccessMessage] = useState<string | null>(null)
  const [apiKey, setApiKey] = useState<string>('')
  const [structureJobId, setStructureJobId] = useState<string | null>(null)
  const [loadingBook, setLoadingBook] = useState(false)

  // ===== LOAD OPTIONS FROM BACKEND =====
  const { options } = useAppOptions()
  const { isMock, isFull, isEconomic } = useExecutionMode('book')

  // Load API key from singleton service
  useEffect(() => {
    const loadApiKey = async () => {
      try {
        await apiKeyService.loadApiKeys()
        const currentKey = apiKeyService.getCurrentKey()
        if (currentKey) {
          setApiKey(currentKey)
          console.log('✅ API Key carregada via ApiKeyService (BookCreator)')
        }
      } catch (err) {
        console.error('❌ Erro ao carregar API key:', err)
      }
    }
    loadApiKey()

    // Subscribe to key changes
    const unsubscribe = apiKeyService.subscribe(() => {
      const newKey = apiKeyService.getCurrentKey()
      if (newKey) setApiKey(newKey)
    })

    return () => unsubscribe()
  }, [])

  // Get options from backend (no fallbacks in production)
  const BOOK_STYLES = options.book?.styles?.map(s => ({ id: s.id, name: s.name, description: s.description || '' })) || []
  const IMAGE_STYLES = options.image?.categories?.map(s => ({ id: s.id, name: s.name })) || []
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

  // ===== EXISTING BOOKS STATE =====
  const [existingBooks, setExistingBooks] = useState<any[]>([])
  const [loadingBooks, setLoadingBooks] = useState(false)
  const [showExistingBooks, setShowExistingBooks] = useState(true)

  // ===== AUTHOR STYLES STATE =====
  // Delegated to <AuthorStyleSelector />

  // Load existing books
  useEffect(() => {
    const loadBooks = async () => {
      setLoadingBooks(true)
      try {
        const response = await endpoints.books.list()
        // Map to expected format if needed
        const bookList = (response.data || []).map((book: any) => ({
          ...book,
          topic: book.title || book.topic, // Ensure title is displayed
        }))
        setExistingBooks(bookList)
      } catch (err) {
        console.error('Error loading books:', err)
      } finally {
        setLoadingBooks(false)
      }
    }
    loadBooks()
  }, [])

  const handleDeleteAllBooks = async () => {
    if (window.confirm('TEM CERTEZA? Isso apagará TODOS os livros da biblioteca. Esta ação é irreversível.')) {
      try {
        await endpoints.books.deleteAll()
        // Reload list
        const response = await endpoints.books.list()
        setExistingBooks(response.data || [])
        if (bookId) navigate('/book')
      } catch (e) {
        alert('Erro ao apagar biblioteca')
        console.error(e)
      }
    }
  }

  // ===== MAIN WIZARD STATE =====
  const [config, setConfig] = useState<BookConfig>({
    title: '',
    subtitle: '',
    topic: '',
    draft: '',
    targetAudience: '',
    numChapters: 10,
    style: 'practical',
    bookStyle: [],
    bookStylePrompt: '',
    includeImages: true,
    chapters: [],
    authorName: '',
    authorStyle: '',
    selectedAuthorStyles: [],
    prologue: '',
    acknowledgments: '',
    language: 'Português (Brasil)',
    chapterPlanningInstructions: '',
  })

  const [chapterPlanningDialogOpen, setChapterPlanningDialogOpen] = useState(false)

  // Log Modal State
  const [showLogModal, setShowLogModal] = useState(false)
  const [logs, setLogs] = useState<string[]>([])

  // Polling for logs
  useEffect(() => {
    let interval: ReturnType<typeof setInterval>
    if (showLogModal && structureJobId) {
      const fetchLogs = async () => {
        try {
          const res = await api.get(`/system_logs?job_id=${structureJobId}`)
          if (res.data?.logs) {
            setLogs(res.data.logs.map((l: any) => l.message || JSON.stringify(l)))
          }
        } catch (e) {
          console.error("Failed to fetch logs", e)
        }
      }
      fetchLogs()
      interval = setInterval(fetchLogs, 2000)
    }
    return () => clearInterval(interval)
  }, [showLogModal, structureJobId])

  const [step, setStep] = useState<'info' | 'structure' | 'sections' | 'design' | 'content' | 'confirm'>('info')

  /** Apaga apenas o texto (content) de todas as seções. Mantém títulos, objetivos, imagens, etc. */
  const clearAllSectionsText = useCallback(() => {
    const total = config.chapters.reduce((acc, ch) => acc + (ch.sections?.length ?? 0), 0)
    if (total === 0) return
    if (!window.confirm(`Apagar o texto de todas as ${total} seções? Títulos, objetivos e imagens serão mantidos.`)) return
    const updatedChapters = config.chapters.map((ch) => ({
      ...ch,
      sections: (ch.sections || []).map((s) => ({ ...s, content: '' })),
    }))
    setConfig({ ...config, chapters: updatedChapters })
  }, [config])

  // ===== DESIGN STUDIO STATE =====
  const [designTab, setDesignTab] = useState<'front' | 'back' | 'chapters'>('front')
  const [coverConfig, setCoverConfig] = useState<CoverConfig>({
    frontPrompt: '',
    frontImagePath: null,
    backPrompt: '',
    backImagePath: null,
    selectedStyles: ['digital-art'],
    selectedDesigners: []
  })
  const [coverDesignerOptions, setCoverDesignerOptions] = useState<Array<{ label: string; value: string }>>([])
  const [coverDesignerDetails, setCoverDesignerDetails] = useState<Record<string, any>>({})
  const [loadingCoverDesigners, setLoadingCoverDesigners] = useState(false)
  const [selectedChapterIdx, setSelectedChapterIdx] = useState(0)
  const [isGeneratingCover, setIsGeneratingCover] = useState(false)
  const [isGeneratingChapterCover, setIsGeneratingChapterCover] = useState(false)
  const [coverModel, setCoverModel] = useState<string>(modelConfig.getDefaultImageModel('full'))

  // ===== CONTENT EDITOR STATE =====
  const [editingChapterIdx, setEditingChapterIdx] = useState<number | null>(null)
  const [editingSectionIdx, setEditingSectionIdx] = useState<number>(0)
  const [sectionTab, setSectionTab] = useState<'content' | 'images' | 'code' | 'settings'>('content')
  const [chapterOverviewTab, setChapterOverviewTab] = useState<'chapters' | 'prologue' | 'acknowledgments'>('chapters')
  const [showPreview, setShowPreview] = useState(false) // Preview mode toggle
  const [chapterContent, setChapterContent] = useState('') // Main editor buffer
  const [isGeneratingContent, setIsGeneratingContent] = useState(false)
  const [isPlanningEpub, setIsPlanningEpub] = useState(false)
  const [isGeneratingPrologue, setIsGeneratingPrologue] = useState(false)
  const [isSavingFrontMatter, setIsSavingFrontMatter] = useState(false)
  const [isGeneratingImage, setIsGeneratingImage] = useState(false)
  const [sectionImagePrompts, setSectionImagePrompts] = useState<Record<string, string>>({})
  const [sectionImageStyles, setSectionImageStyles] = useState<Record<string, string[]>>({})
  const [sectionImageModels, setSectionImageModels] = useState<Record<string, string>>({})
  const [isCompilingEpub, setIsCompilingEpub] = useState(false)
  const [epubDownloadUrl, setEpubDownloadUrl] = useState<string | null>(null)
  const [contentPanelTab, setContentPanelTab] = useState<'assets' | 'code' | 'math'>('math')
  const [codeBlocks, setCodeBlocks] = useState<{ language: string; code: string; title?: string }[]>([])

  // ===== PROMPT GENERATION STATE =====
  const [generatedPrompt, setGeneratedPrompt] = useState<string>('')
  const [promptTarget, setPromptTarget] = useState<{ chapterIdx: number; sectionIdx: number } | null>(null)
  const [isGeneratingPrompt, setIsGeneratingPrompt] = useState(false)
  const [showPromptModal, setShowPromptModal] = useState(false)
  const [isGeneratingQuestions, setIsGeneratingQuestions] = useState(false)
  const [isAddingBookSection, setIsAddingBookSection] = useState(false)

  useEffect(() => {
    const loadCoverDesigners = async () => {
      setLoadingCoverDesigners(true)
      try {
        const response = await endpoints.styles.coverDesigners()
        const items = Array.isArray(response.data) ? response.data : []
        const options = items
          .map((item: any) => {
            const name = item?.name || item?.label || item?.value
            if (!name) return null
            return { label: name, value: name }
          })
          .filter(Boolean) as Array<{ label: string; value: string }>
        const detailsMap: Record<string, any> = {}
        items.forEach((item: any) => {
          const name = item?.name || item?.label || item?.value
          if (!name) return
          detailsMap[name] = item?.details || item?.detalhes || item?.meta || item?.description || item?.notes || {
            color: item?.color || item?.colors,
            image_style: item?.image_style || item?.style,
            description: item?.description || item?.notes,
          }
        })
        setCoverDesignerOptions(options)
        setCoverDesignerDetails(detailsMap)
      } catch (err) {
        console.error('Erro ao carregar designers de capa:', err)
      } finally {
        setLoadingCoverDesigners(false)
      }
    }
    loadCoverDesigners()
  }, [])

  const chapterContentRef = useRef<HTMLTextAreaElement | null>(null)

  // ===== EXECUTION MODE STATE =====
  const [executionMode, setExecutionMode] = useState<string>('economic')
  const [modesConfig, setModesConfig] = useState<ConfigSummary | null>(null)
  const [loadingModes, setLoadingModes] = useState(false)

  // Load execution modes from backend
  const loadExecutionModes = useCallback(async () => {
    try {
      setLoadingModes(true)
      const response = await endpoints.config.features()
      setModesConfig(response.data)
      setExecutionMode(response.data.global_default)
    } catch (err) {
      console.error('Error loading execution modes:', err)
    } finally {
      setLoadingModes(false)
    }
  }, [])

  // Handle mode change
  const handleModeChange = async (newMode: string) => {
    try {
      await endpoints.config.setGlobalMode(newMode)
      setExecutionMode(newMode)
    } catch (err: any) {
      console.error('Error changing execution mode:', err)
    }
  }

  useEffect(() => {
    loadExecutionModes()
  }, [loadExecutionModes])

  const resolveBookDraft = (bookData: any) =>
    bookData?.draft ||
    bookData?.book_plan?.draft ||
    bookData?.request_payload?.draft ||
    bookData?.final_state?.book_plan?.draft ||
    bookData?.final_state?.draft ||
    ''

  const resolveChapterPlanningInstructions = (bookData: any) =>
    bookData?.chapter_planning_instructions ||
    bookData?.book_plan?.chapter_planning_instructions ||
    bookData?.request_payload?.chapter_planning_instructions ||
    bookData?.final_state?.book_plan?.chapter_planning_instructions ||
    ''

  // Load existing book if bookId is provided in URL
  useEffect(() => {
    const loadBook = async () => {
      if (!bookId || bookId === 'undefined') {
        console.log('⏭️  No bookId in URL, skipping load')
        return
      }

      console.log('🔍 BookCreator: Loading book with ID:', bookId)
      setLoadingBook(true)
      setError(null)

      try {
        console.log('📡 Fetching from /books/' + bookId)
        const response = await api.get(`/books/${bookId}`)
        const bookData = response.data
        console.log('✅ Book data received:', bookData)

        if (!bookData) {
          throw new Error("Livro não encontrado")
        }

        const loadedBook: BookConfig = {
          id: bookData.id || bookData.job_id || bookId,
          title: bookData.title || '',
          topic: bookData.topic || bookData.title || '',
          draft: resolveBookDraft(bookData),
          targetAudience: bookData.audience || '',
          numChapters: bookData.total_chapters || bookData.chapters?.length || 10,
          style: bookData.tone || 'practical',
          includeImages: true,
          authorName: bookData.author || '',
          authorStyle: bookData.author || '',
          selectedAuthorStyles: [],
          prologue: bookData.prologue || '',
          acknowledgments: bookData.acknowledgments || '',
          chapters: [],
          language:
            bookData.language ||
            (bookData as { plan?: { language?: string } }).plan?.language ||
            'Português (Brasil)',
          chapterPlanningInstructions: resolveChapterPlanningInstructions(bookData),
        }

        // Load chapters if available
        if (bookData.chapters && Array.isArray(bookData.chapters)) {
          loadedBook.chapters = bookData.chapters.map((ch: any, idx: number) => ({
            id: ch.id || `chapter-${idx}`,
            title: ch.title || `Capítulo ${idx + 1}`,
            description: ch.description || ch.purpose || '',
            objective: ch.objective || '',
            creation_guide: (ch.creation_guide || '').trim(),
            coverPath: ch.cover_path || ch.image_path,
            coverPrompt: ch.cover_prompt || '',
            content: ch.content || '',
            sections: Array.isArray(ch.sections) ? ch.sections.map((s: any, si: number) => {
              if (typeof s === 'string') {
                return { title: s, purpose: '', content: '', images: [], code_blocks: [] }
              }
              return {
                ...s,
                title: s.title || `Seção ${si + 1}`,
                purpose: s.purpose || s.objective || s.content_directive || '',
                content: s.content || '',
                images: s.images || [],
                code_blocks: s.code_blocks || []
              }
            }) : []
          }))
        }

        setConfig(loadedBook)
        setCoverConfig((prev) => ({
          ...prev,
          frontImagePath: bookData.cover_path || prev.frontImagePath,
          backImagePath: bookData.back_cover_path || prev.backImagePath
        }))
        setStructureJobId(bookId)

        // Go to appropriate step based on what's available
        if (loadedBook.chapters && loadedBook.chapters.length > 0) {
          setStep('structure')
        } else if (loadedBook.title) {
          setStep('structure')
        } else {
          setStep('info')
        }

        console.log('✅ Livro carregado:', loadedBook.title)
      } catch (err) {
        console.error('❌ Erro ao carregar livro:', err)
        setError(`Erro ao carregar livro: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
      } finally {
        setLoadingBook(false)
      }
    }

    loadBook()
  }, [bookId]) // Only depend on bookId from URL

  // Function to load existing book
  const loadExistingBook = async (jobId: string) => {
    console.log('🔍 loadExistingBook called with ID:', jobId)
    setLoadingBook(true)
    setError(null)
    try {
      // Use new dedicated book endpoint
      console.log('📡 Fetching book from /books/' + jobId)
      const response = await endpoints.books.get(jobId)
      const bookData = response.data
      console.log('✅ Book data received:', bookData)

      if (!bookData) throw new Error("Livro não encontrado")

      const loadedBook: BookConfig = {
        id: bookData.id || bookData.job_id || jobId,
        title: bookData.title || '',
        topic: bookData.topic || bookData.title || '',
        draft: resolveBookDraft(bookData),
        targetAudience: bookData.audience || '',
        numChapters: bookData.total_chapters || bookData.chapters?.length || 10,
        style: bookData.tone || 'practical',
        includeImages: true,
        authorName: bookData.author || '',
        authorStyle: bookData.author || '',
        selectedAuthorStyles: [], // Initialize empty, could be populated from backend if saved
        prologue: bookData.prologue || '',
        acknowledgments: bookData.acknowledgments || '',
        chapters: [],
        language:
          bookData.language ||
          (bookData as { plan?: { language?: string } }).plan?.language ||
          'Português (Brasil)',
        chapterPlanningInstructions: resolveChapterPlanningInstructions(bookData),
      }

      // Load chapters if available
      if (bookData.chapters && Array.isArray(bookData.chapters)) {
        loadedBook.chapters = bookData.chapters.map((ch: any, idx: number) => ({
          id: ch.id || `chapter-${idx}`,
          title: ch.title || `Capítulo ${idx + 1}`,
          description: ch.description || ch.purpose || '',
          objective: ch.objective || '', // Map objective
          creation_guide: (ch.creation_guide || '').trim(),
          coverPath: ch.cover_path || ch.image_path,
          coverPrompt: ch.cover_prompt || '',
          content: ch.content || '',
          sections: Array.isArray(ch.sections) ? ch.sections.map((s: any, si: number) => {
            if (typeof s === 'string') {
              return { title: s, purpose: '', content: '', images: [], code_blocks: [] }
            }
            return {
              ...s,
              title: s.title || `Seção ${si + 1}`,
              purpose: s.purpose || s.objective || s.content_directive || '',
              content: s.content || '',
              images: s.images || [],
              code_blocks: s.code_blocks || []
            }
          }) : []
        }))
      }

      setConfig(loadedBook)
      setCoverConfig((prev) => ({
        ...prev,
        frontImagePath: bookData.cover_path || prev.frontImagePath,
        backImagePath: bookData.back_cover_path || prev.backImagePath
      }))
      setStructureJobId(jobId)

      // Go to appropriate step based on what's available
      if (loadedBook.chapters && loadedBook.chapters.length > 0) {
        setStep('structure')
      } else if (loadedBook.title) {
        setStep('structure')
      } else {
        setStep('info')
      }

      console.log('✅ Livro carregado:', loadedBook.title)
    } catch (err) {
      console.error('❌ Erro ao carregar livro:', err)
      setError(`Erro ao carregar livro: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
      setStep('info')
    } finally {
      setLoadingBook(false)
    }
  }

  // ===== HELPERS =====
  const insertIntoChapterContent = (textToInsert: string, suffix: string = '') => {
    const textarea = chapterContentRef.current
    if (!textarea) return

    const start = textarea.selectionStart
    const end = textarea.selectionEnd
    const currentSection = config.chapters[editingChapterIdx || 0].sections![editingSectionIdx || 0]
    const text = currentSection.content || ''

    // If text is selected, wrap it
    if (start !== end) {
      const selectedText = text.substring(start, end)
      const before = text.substring(0, start)
      const after = text.substring(end)
      const newContent = before + textToInsert + selectedText + suffix + after

      const updatedChapters = [...config.chapters]
      updatedChapters[editingChapterIdx || 0].sections![editingSectionIdx || 0].content = newContent
      setConfig({ ...config, chapters: updatedChapters })
      setChapterContent(newContent)

      // Restore selection (including wrapper)
      setTimeout(() => {
        textarea.focus()
        textarea.setSelectionRange(start, end + textToInsert.length + selectedText.length + suffix.length)
      }, 0)
    } else {
      // Just insert
      const before = text.substring(0, start)
      const after = text.substring(end)
      const insertion = textToInsert + suffix
      const newContent = before + insertion + after

      const updatedChapters = [...config.chapters]
      updatedChapters[editingChapterIdx || 0].sections![editingSectionIdx || 0].content = newContent
      setConfig({ ...config, chapters: updatedChapters })
      setChapterContent(newContent)

      setTimeout(() => {
        textarea.focus()
        textarea.setSelectionRange(start + textToInsert.length, start + textToInsert.length)
      }, 0)
    }
  }

  const handleGenerateSectionImage = async (
    chapterIdx: number,
    sectionIdx: number,
    prompt: string,
    styleNames: string[] = [],
    modelName?: string
  ) => {
    if (!prompt) return
    setIsGeneratingImage(true)
    try {
      const response = await api.post('/book/generate_cover', {
        job_id: config.id || 'temp-job',
        target: 'section',
        chapter_index: chapterIdx,
        section_index: sectionIdx,
        prompt: prompt,
        api_key: apiKey || undefined,
        model_name: modelName || modelConfig.getDefaultImageModel(executionMode as any),
        style_names: styleNames,
        execution_mode: executionMode
      })

      const imagePath = response.data.file_path
      if (imagePath) {
        const updated = [...config.chapters]
        const currentImages = updated[chapterIdx].sections![sectionIdx].images || []
        updated[chapterIdx].sections![sectionIdx].images = [
          ...currentImages,
          {
            path: imagePath,
            caption: prompt,
            source: 'ai',
            uploaded_at: new Date().toISOString()
          }
        ]
        setConfig({ ...config, chapters: updated })
      }
    } catch (err) {
      console.error("Image generation failed", err)
      setError(`Erro ao gerar imagem: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    } finally {
      setIsGeneratingImage(false)
    }
  }

  const handleGenerateSectionContent = async (chapterIdx: number, sectionIdx: number, mode: 'generate' | 'expand' | 'rewrite' = 'generate', customPromptOverride?: string) => {
    setIsGeneratingContent(true)
    setError(null)
    setSuccessMessage(null)
    try {
      const chapter = config.chapters[chapterIdx]
      const section = chapter.sections![sectionIdx]

      // Use the override if provided, otherwise reconstruct it (fallback)
      const prompt = customPromptOverride || `Escreva o conteúdo completo para a seção "${section.title}" do capítulo "${chapter.title}". 
      Objetivo/Diretrizes: ${section.purpose}.
      Descrição do Capítulo: ${chapter.description}. 
      Público: ${config.targetAudience}. 
      Estilo: ${config.style}.
      ${config.authorStyle ? `Inspiração/Estilo de Autor: ${config.authorStyle}` : ''}`

      const response = await api.post('/book/generate_section', {
        book_title: config.title,
        chapter_title: chapter.title,
        chapter_description: chapter.description,
        style: config.style,
        target_audience: config.targetAudience,
        api_key: apiKey || undefined,
        custom_prompt: prompt,
        execution_mode: executionMode
      })

      const generatedContent = response.data.content || ''

      const updatedChapters = [...config.chapters]
      updatedChapters[chapterIdx].sections![sectionIdx].content = generatedContent
      setConfig({ ...config, chapters: updatedChapters })
      setSuccessMessage(`Conteúdo da seção "${section.title}" gerado com sucesso.`)
    } catch (err) {
      console.error('Failed to generate section content:', err)
      setError(`Erro ao gerar conteúdo da seção: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    } finally {
      setIsGeneratingContent(false)
    }
  }

  const handlePlanEpubSection = async (chapterIdx: number, sectionIdx: number) => {
    const resolvedBookId = config.id || bookId
    if (!resolvedBookId) {
      setError('Livro não identificado para planejar EPUB.')
      return
    }
    setIsPlanningEpub(true)
    setError(null)
    setSuccessMessage(null)
    try {
      const response = await api.post<{ message?: string }>('/book/plan_epub_section_queue', {
        job_id: resolvedBookId,
        chapter_index: chapterIdx,
        section_index: sectionIdx,
        api_key: apiKey || undefined,
      })
      setSuccessMessage(response.data?.message || 'Planejamento EPUB enfileirado. Acompanhe no Histórico.')
    } catch (err) {
      console.error('Falha ao planejar EPUB da seção:', err)
      setError(`Erro ao planejar EPUB: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    } finally {
      setIsPlanningEpub(false)
    }
  }

  const constructSectionPrompt = (chapterIdx: number, sectionIdx: number) => {
    const chapter = config.chapters[chapterIdx]
    const section = chapter.sections![sectionIdx]

    // Combine base author style with multi-selected styles
    const authorStyles = [
      config.authorStyle,
      ...(config.selectedAuthorStyles || [])
    ].filter(Boolean).join(', ')

    return `Escreva o conteúdo completo para a seção "${section.title}" do capítulo "${chapter.title}".
Objetivo do Capítulo: ${chapter.objective || chapter.description || 'Não especificado'}.
Objetivo da Seção: ${section.purpose || 'Desenvolver o tópico'}.
Público Alvo: ${config.targetAudience}.
Estilo de Escrita: ${config.style}.
${authorStyles ? `Inspiração de Escritores / Estilo de Autor: ${authorStyles}.` : ''}
    `.trim()
  }

  const handleGeneratePrompt = async (chapterIdx: number, sectionIdx: number) => {
    setIsGeneratingPrompt(true)
    setPromptTarget({ chapterIdx, sectionIdx })

    await new Promise(r => setTimeout(r, 600)) // UX delay

    const prompt = constructSectionPrompt(chapterIdx, sectionIdx)
    setGeneratedPrompt(prompt)
    setIsGeneratingPrompt(false)
    setShowPromptModal(true)
  }

  const handleGenerateQuestions = async (chapterIdx: number, sectionIdx: number) => {
    const chapter = config.chapters[chapterIdx]
    const section = chapter.sections![sectionIdx]

    setIsGeneratingQuestions(true)
    setError(null)
    try {
      const response = await api.post('/book/generate_questions', {
        content: (section.content || '').substring(0, 8000),
        board_id: section.question_board || 'cespe-cebraspe',
        question_type: section.question_type || 'multiple-choice',
        difficulty: section.question_difficulty || 'medio',
        num_questions: section.num_questions || 5,
        include_answers: section.question_include_answers !== false,
        include_explanation: section.question_include_explanation !== false,
        section_title: section.title,
        chapter_title: chapter.title,
        model_name: modelConfig.getDefaultTextModel('economic'),
        api_key: apiKey || undefined,
      })

      const generatedQuestions = response.data?.questions || ''
      const updated = [...config.chapters]
      updated[chapterIdx].sections![sectionIdx].questions = generatedQuestions
      setConfig({ ...config, chapters: updated })
      const boardUsed = response.data?.board_id || section.question_board || 'cespe-cebraspe'
      setSuccessMessage(`✅ ${section.num_questions || 5} questões (${boardUsed.toUpperCase()}) geradas para "${section.title}".`)
    } catch (err) {
      console.error('Failed to generate questions:', err)
      setError(`Erro ao gerar questões: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    } finally {
      setIsGeneratingQuestions(false)
    }
  }

  // ===== HANDLERS =====

  const handleExtractSectionCodeBlocks = () => {
    if (editingChapterIdx === null) return
    const section = config.chapters[editingChapterIdx].sections![editingSectionIdx]
    const content = section.content || ''
    // Match code blocks ```lang ... ```
    const regex = /```([\w-]*)\n([\s\S]*?)```/g
    let match: RegExpExecArray | null
    const newBlocks: { language: string; title: string; content: string }[] = []

    while ((match = regex.exec(content)) !== null) {
      newBlocks.push({
        language: match[1] || 'text',
        title: `Snippet Extraído (${match[1] || 'text'})`,
        content: match[2]?.trim() || ''
      })
    }

    if (newBlocks.length > 0) {
      const updated = [...config.chapters]
      const currentBlocks = updated[editingChapterIdx].sections![editingSectionIdx].code_blocks || []
      // Optional: Avoid duplicates? For now just append as requested
      updated[editingChapterIdx].sections![editingSectionIdx].code_blocks = [
        ...currentBlocks,
        ...newBlocks.map(b => ({ ...b, created_at: new Date().toISOString() })) // Add timestamps
      ]
      setConfig({ ...config, chapters: updated })
    }
  }

  const handleGenerateStructure = async () => {
    setError(null)
    setSuccessMessage(null)
    if (!config.title.trim() || !config.topic.trim()) {
      setError('Preencha o Título do Livro e o Tema/Assunto Principal para gerar a estrutura.')
      return
    }

    const storedApiKey = apiKey || getStoredGeminiApiKey()
    if (!storedApiKey) {
      console.warn('⚠️ API Key local não encontrada. Usando chave configurada no MongoDB.')
    }

    setIsPlanningStructure(true)

    try {
      const wizardBase = buildWizardPlanningPayloadFromConfig(config, storedApiKey || '', executionMode)
      const topicComposed = config.draft.trim()
        ? `${config.title}: ${config.topic}\n\nRASCUNHO DO LIVRO:\n${config.draft.trim()}`
        : `${config.title}: ${config.topic}`
      const payload: Record<string, unknown> = {
        ...wizardBase,
        topic: topicComposed,
        draft: config.draft || undefined,
        author_inspiration: config.selectedAuthorStyles.length > 0
          ? config.selectedAuthorStyles.join(', ')
          : (config.authorStyle || config.authorName || null),
        author_styles: config.selectedAuthorStyles.length ? config.selectedAuthorStyles : undefined,
        api_key: storedApiKey || '',
      }

      const existingId = config.id || bookId
      const jobId = await resolveStructureJobId({
        existingId: existingId || undefined,
        payload,
        getStatus: async (id) => (await api.get(`/status/${id}`)).data,
        queuePlanning: async (id, body) => (await endpoints.books.queueStructurePlanning(id, body)).data,
        createGenerate: async (body) => (await api.post('/book/generate', body)).data,
        getBook: async (id) => (await api.get(`/books/${id}`)).data,
      })

      setStructureJobId(jobId)
      setConfig((prev) => ({ ...prev, id: jobId }))

      const poll = await pollBookStructureJob(
        jobId,
        async (id) => (await api.get(`/status/${id}`)).data,
        async (id) => (await api.get(`/books/${id}`)).data,
        { maxWaitMs: 10 * 60 * 1000, intervalMs: 2500 },
      )

      if (poll.chapters?.length) {
        setConfig({ ...config, id: jobId, chapters: poll.chapters as Chapter[], numChapters: poll.chapters.length })
        setStep('structure')
        setSuccessMessage('Estrutura de capítulos gerada com sucesso.')
        return
      }

      if (poll.failed) {
        const errText =
          (poll.lastStatus?.error as string | undefined) ||
          'O planejamento falhou. Verifique o Histórico ou o workspace do livro.'
        setError(errText)
        return
      }

      setConfig({ ...config, id: jobId })
      if (poll.timedOut) {
        const st = String(poll.lastStatus?.status || '')
        if (st === 'pending' || st === 'running') {
          setSuccessMessage('Planejamento em andamento no servidor. Abrindo o workspace para acompanhar.')
          navigate(`/book/${jobId}`)
          return
        }
      }
      setSuccessMessage('Planejamento iniciado. Acompanhe no workspace do livro.')
      navigate(`/book/${jobId}`)
    } catch (err: unknown) {
      setError(formatHttpError(err, 'Erro ao gerar estrutura do livro'))
      console.error('Failed to generate book structure:', err)
    } finally {
      setIsPlanningStructure(false)
    }
  }

  /**
   * Generate chapter content using AI
   */
  const handleGenerateChapterContent = async (idx: number) => {
    const chapter = config.chapters[idx]
    if (!chapter) return

    setIsGeneratingContent(true)
    setError(null)
    setSuccessMessage(null)

    try {
      const prompt = `Escreva o conteúdo completo do capítulo "${chapter.title}". Descrição: ${chapter.description}. Público: ${config.targetAudience}. Estilo: ${config.style}.`
      const response = await api.post('/book/generate_section', {
        book_title: config.title,
        chapter_title: chapter.title,
        chapter_description: chapter.description,
        style: config.style,
        target_audience: config.targetAudience,
        api_key: apiKey || undefined,
        custom_prompt: prompt,
        execution_mode: executionMode
      })

      const generatedContent = response.data.content || `# ${chapter.title}\n\n${chapter.description}\n\n[Conteúdo gerado aqui...]`

      // Update chapter with generated content
      const updatedChapters = [...config.chapters]
      updatedChapters[idx] = { ...chapter, content: generatedContent }
      setConfig({ ...config, chapters: updatedChapters })
      setChapterContent(generatedContent)
      setSuccessMessage(`Conteúdo do capítulo "${chapter.title}" gerado com sucesso.`)
    } catch (err) {
      console.error('Failed to generate chapter content:', err)
      setError(`Erro ao gerar conteúdo do capítulo: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    } finally {
      setIsGeneratingContent(false)
    }
  }

  /**
   * Save edited chapter content
   */
  const handleSaveChapterContent = async () => {
    if (editingChapterIdx === null) return

    const updatedChapters = [...config.chapters]
    updatedChapters[editingChapterIdx] = {
      ...updatedChapters[editingChapterIdx],
      content: chapterContent
    }
    setConfig({ ...config, chapters: updatedChapters })
    setEditingChapterIdx(null)

    // Persist to backend if we have a book ID
    if (bookId && bookId !== 'undefined') {
      try {
        await endpoints.books.update(bookId, {
          chapters: updatedChapters,
          draft: config.draft
        })
        console.log('✅ Book chapters persisted successfully')
      } catch (err) {
        console.error('❌ Failed to persist book chapters:', err)
      }
    }
  }

  const handleSaveFrontMatter = async () => {
    if (!bookId && !config.id) return
    const resolvedBookId = config.id || bookId
    if (!resolvedBookId) return

    setIsSavingFrontMatter(true)
    try {
      await endpoints.books.update(resolvedBookId, {
        prologue: config.prologue,
        acknowledgments: config.acknowledgments,
        draft: config.draft
      })
      setSuccessMessage('Prólogo e agradecimentos salvos.')
    } catch (err) {
      console.error('❌ Failed to persist front matter:', err)
      setError('Erro ao salvar prólogo e agradecimentos.')
    } finally {
      setIsSavingFrontMatter(false)
    }
  }

  const handleGeneratePrologue = async () => {
    if (isGeneratingPrologue) return
    setIsGeneratingPrologue(true)
    setError(null)
    try {
      const prompt = `Escreva um prólogo envolvente para o livro "${config.title}".\n` +
        `Tema central: ${config.topic}.\n` +
        `Público-alvo: ${config.targetAudience || 'leitores gerais'}.\n` +
        `Estilo: ${config.style}.\n` +
        `Autor: ${config.authorName || 'Autor'}.\n` +
        `O prólogo deve preparar o leitor, criar expectativa e explicar o propósito do livro em português (Brasil).`

      const response = await api.post('/book/generate_section', {
        book_title: config.title,
        chapter_title: 'Prólogo',
        section_title: 'Prólogo',
        section_purpose: 'Introduzir o leitor e contextualizar o livro',
        style: config.style,
        target_audience: config.targetAudience,
        api_key: apiKey || undefined,
        custom_prompt: prompt,
        model_name: modelConfig.getDefaultTextModel('economic'),
        execution_mode: executionMode
      })

      const generated = response.data?.content || ''
      setConfig({ ...config, prologue: generated })
      setChapterOverviewTab('prologue')
    } catch (err) {
      console.error('Failed to generate prologue:', err)
      setError('Erro ao gerar prólogo por IA.')
    } finally {
      setIsGeneratingPrologue(false)
    }
  }



  const handleInsertFormula = (latex: string, mode: 'inline' | 'block') => {
    if (!latex.trim()) return
    const snippet = mode === 'block'
      ? `\n\n$$\n${latex}\n$$\n\n`
      : `$${latex}$`
    insertIntoChapterContent(snippet)
  }

  const handleInsertDiagram = (markdownSnippet: string) => {
    insertIntoChapterContent(markdownSnippet)
  }

  const handleInsertImage = (markdownSnippet: string) => {
    insertIntoChapterContent(markdownSnippet)
  }

  const handleExtractCodeBlocks = () => {
    const blocks: { language: string; code: string }[] = []
    const regex = /```([\\w-]*)\\n([\\s\\S]*?)```/g
    let match: RegExpExecArray | null
    while ((match = regex.exec(chapterContent)) !== null) {
      blocks.push({
        language: match[1] || 'text',
        code: match[2]?.trim() || ''
      })
    }
    setCodeBlocks(blocks)
    setContentPanelTab('code')
  }

  /**
   * Compile EPUB from chapters.
   * @param format - 'amazon_kdp' para formato Amazon (livro físico/KDP), omitir para EPUB padrão.
   */
  const handleCompileEpub = async (format?: 'amazon_kdp') => {
    setIsCompilingEpub(true)
    setError(null)

    try {
      const resolvedBookId = config.id || bookId
      if (!resolvedBookId) {
        throw new Error('Livro não identificado para exportação.')
      }

      const body: Record<string, unknown> = {
        title: config.title,
        author: config.authorName || 'Unknown Author',
        prologue: config.prologue,
        acknowledgments: config.acknowledgments,
        cover_image: coverConfig.frontImagePath || undefined
      }
      if (format === 'amazon_kdp') body.format = 'amazon_kdp'

      const response = await api.post(`/books/${resolvedBookId}/export-epub`, body, {
        responseType: 'blob'
      })

      // Create download link
      const blob = new Blob([response.data], { type: 'application/epub+zip' })
      const url = window.URL.createObjectURL(blob)
      setEpubDownloadUrl(url)

      const baseName = config.title.replace(/\s+/g, '_')
      const filename = format === 'amazon_kdp' ? `${baseName}_amazon_kdp.epub` : `${baseName}.epub`

      // Auto-download
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
    } catch (err) {
      console.error('Failed to compile EPUB:', err)
      setError('Erro ao compilar EPUB. Verifique se o conteúdo dos capítulos está preenchido.')
    } finally {
      setIsCompilingEpub(false)
    }
  }

  /**
   * Create Book - calls /book/generate endpoint
   * Matches BookRequest model in api.py:
   *   topic, audience, tone, num_chapters, category, language, depth, author_inspiration, api_key, model_name
   */
  const handleCreateBook = async () => {
    if (!config.title.trim() || !config.topic.trim()) return

    setIsCreating(true)
    setError(null)

    try {
      const wizardBase = buildWizardPlanningPayloadFromConfig(config, apiKey || '', executionMode)
      const topicComposed = config.draft.trim()
        ? `${config.title}: ${config.topic}\n\nRASCUNHO DO LIVRO:\n${config.draft.trim()}`
        : `${config.title}: ${config.topic}`
      const payload: Record<string, unknown> = {
        ...wizardBase,
        topic: topicComposed,
        draft: config.draft || undefined,
        author_inspiration: config.selectedAuthorStyles.length > 0
          ? config.selectedAuthorStyles.join(', ')
          : (config.authorStyle || config.authorName || null),
        author_styles: config.selectedAuthorStyles.length ? config.selectedAuthorStyles : undefined,
        api_key: apiKey || undefined,
        model_name: modelConfig.getDefaultTextModel('economic'),
        execution_mode: executionMode,
      }

      const existingId = config.id || bookId
      const jobId = await resolveStructureJobId({
        existingId: existingId || undefined,
        payload,
        getStatus: async (id) => (await api.get(`/status/${id}`)).data,
        queuePlanning: async (id, body) => (await endpoints.books.queueStructurePlanning(id, body)).data,
        createGenerate: async (body) => (await api.post('/book/generate', body)).data,
        getBook: async (id) => (await api.get(`/books/${id}`)).data,
      })

      setConfig((prev) => ({ ...prev, id: jobId }))
      navigate(`/book/${jobId}`)
    } catch (err: unknown) {
      setError(formatHttpError(err, 'Erro ao criar livro'))
      console.error('Failed to create book:', err)
    } finally {
      setIsCreating(false)
    }
  }

  /** Garante um livro na biblioteca (planning_draft) para poder salvar metadados/capítulos antes da IA. */
  const ensureBookIdForPlanning = async (): Promise<string | null> => {
    const currentId = config.id || bookId
    if (currentId) return currentId
    if (!config.title.trim() && !config.topic.trim() && !config.draft.trim()) {
      setError('Preencha título, tema ou rascunho para salvar o planejamento.')
      return null
    }
    setError(null)
    try {
      const body = buildWizardPlanningPayloadFromConfig(config, apiKey, executionMode)
      const resp = await endpoints.books.createPlanningDraft(body)
      const newId = resp.data?.job_id
      if (!newId) {
        setError('Não foi possível criar o rascunho no servidor.')
        return null
      }
      setConfig((c) => ({ ...c, id: newId }))
      setStructureJobId(newId)
      setSuccessMessage('Rascunho criado na biblioteca. Continue editando ou use «Gerar Estrutura do Livro».')
      return newId
    } catch (err: unknown) {
      const msg =
        err instanceof Error
          ? err.message
          : (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
            'Erro ao criar rascunho'
      setError(msg)
      console.error('create planning draft failed:', err)
      return null
    }
  }

  const persistPlanningToServer = async (opts: { exitToLibrary: boolean }) => {
    setError(null)
    if (!opts.exitToLibrary) setSuccessMessage(null)
    let id = config.id || bookId
    if (!id) {
      const created = await ensureBookIdForPlanning()
      if (!created) return
      id = created
    }
    setIsSavingPlanning(true)
    try {
      await endpoints.books.update(id, {
        title: config.title,
        draft: config.draft || undefined,
        author: config.authorName || undefined,
        target_audience: config.targetAudience || undefined,
        language: config.language || undefined,
        prologue: config.prologue || undefined,
        acknowledgments: config.acknowledgments || undefined,
        structure: buildApiStructureFromChapters(config.chapters),
        default_min_text_length: config.defaultMinTextLength,
        default_has_source_code: config.defaultHasSourceCode,
        chapter_planning_instructions: (config.chapterPlanningInstructions || '').trim(),
        ...(opts.exitToLibrary ? { status: 'planning_saved' as const } : {}),
      })
      if (opts.exitToLibrary) {
        navigate('/books')
      } else {
        navigate(bookCreatorPath(id), { replace: true })
        setSuccessMessage('Planejamento salvo no servidor. Você pode continuar editando.')
      }
    } catch (err: unknown) {
      const msg =
        err instanceof Error
          ? err.message
          : (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
            'Erro ao salvar planejamento'
      setError(msg)
      console.error('save planning failed:', err)
    } finally {
      setIsSavingPlanning(false)
    }
  }

  /** Grava o planejamento atual e permanece na tela. */
  const handleSavePlanningProgress = async () => {
    await persistPlanningToServer({ exitToLibrary: false })
  }

  /** Grava capítulos, seções e orientações e volta à biblioteca. */
  const handleSavePlanningAndExit = async () => {
    await persistPlanningToServer({ exitToLibrary: true })
  }

  const updateChapter = (id: string, updates: Partial<Chapter>) => {
    setConfig({
      ...config,
      chapters: config.chapters.map(c => c.id === id ? { ...c, ...updates } : c)
    })
  }

  const deleteChapter = (id: string) => {
    const updatedChapters = config.chapters.filter(c => c.id !== id)
    setConfig({
      ...config,
      chapters: updatedChapters,
      numChapters: updatedChapters.length
    })
  }

  const addChapter = () => {
    const newChapter: Chapter = {
      id: `chapter-${Date.now()}`,
      title: `Novo Capítulo`,
      description: '',
      sections: [{ title: 'Introdução', purpose: '', content: '', images: [], code_blocks: [] }]
    }
    const updatedChapters = [...config.chapters, newChapter]
    setConfig({ ...config, chapters: updatedChapters, numChapters: updatedChapters.length })
  }

  const handlePlanCover = async (target: 'front' | 'back') => {
    setIsGeneratingCover(true)

    const designerSuffix = coverConfig.selectedDesigners.length > 0
      ? ` Inspirações de capa: ${coverConfig.selectedDesigners.join(', ')}.`
      : ''
    const detailsSuffix = coverConfig.selectedDesigners
      .map((name) => {
        const details = coverDesignerDetails[name]
        const fullDetails = formatCoverDesignerDetails(details)
        if (!fullDetails) return null
        return `${name}: ${fullDetails}`
      })
      .filter(Boolean)
      .join(' | ')
    const prompt = target === 'front'
      ? `Professional book cover for "${config.title}"${config.subtitle ? `: ${config.subtitle}` : ''} by ${config.authorName || 'Author'}. ${coverConfig.selectedStyles.join(', ')} style. High quality, publishing ready.${designerSuffix}${detailsSuffix ? ` Full best-seller cover details: ${detailsSuffix}.` : ''}`
      : `Back cover design for "${config.title}"${config.subtitle ? `: ${config.subtitle}` : ''} by ${config.authorName || 'Author'}. Include space for synopsis and author bio. ${coverConfig.selectedStyles.join(', ')} style.${designerSuffix}${detailsSuffix ? ` Full best-seller cover details: ${detailsSuffix}.` : ''}`

    if (target === 'front') {
      setCoverConfig({ ...coverConfig, frontPrompt: prompt })
    } else {
      setCoverConfig({ ...coverConfig, backPrompt: prompt })
    }
    setIsGeneratingCover(false)
  }

  const formatCoverDesignerDetails = (details: any) => {
    if (!details) return ''
    if (typeof details === 'string') return details
    if (typeof details === 'object') {
      const parts = []
      if (details.color || details.colors) parts.push(`cores: ${details.color || details.colors}`)
      if (details.image_style) parts.push(`estilo de imagem: ${details.image_style}`)
      if (details.description) parts.push(`descrição: ${details.description}`)
      return parts.join('; ')
    }
    return String(details)
  }

  const handleBestSellerPrompt = () => {
    const subject = config.topic || config.targetAudience || 'tema geral'
    const title = config.title || 'Untitled'
    const subtitle = config.subtitle || ''
    const author = config.authorName || config.authorStyle || 'Autor'
    const designerSuffix = coverConfig.selectedDesigners.length > 0
      ? ` Inspirações de capa: ${coverConfig.selectedDesigners.join(', ')}.`
      : ''
    const detailsSuffix = coverConfig.selectedDesigners
      .map((name) => {
        const details = coverDesignerDetails[name]
        const fullDetails = formatCoverDesignerDetails(details)
        if (!fullDetails) return null
        return `${name}: ${fullDetails}`
      })
      .filter(Boolean)
      .join(' | ')
    const meta = `Título: ${title}.${subtitle ? ` Subtítulo: ${subtitle}.` : ''} Autor: ${author}.`
    const prompt = `Crie uma capa premium para o livro "${title}" sobre "${subject}". ${meta} Design editorial profissional, tipografia forte, hierarquia clara, composição equilibrada, acabamento de livraria.${designerSuffix}${detailsSuffix ? ` Detalhes completos dos best-sellers: ${detailsSuffix}.` : ''}`
    setCoverConfig({ ...coverConfig, frontPrompt: prompt })
  }

  const pollCoverJob = async (coverJobId: string): Promise<string | null> => {
    for (let i = 0; i < 60; i++) {
      const statusRes = await api.get(`/status/${coverJobId}`)
      const data = statusRes.data || {}
      if (data.status === 'completed') {
        return data.final_state?.file_path || data.final_state?.image_path || null
      }
      if (data.status === 'failed') {
        throw new Error(data.error || 'Falha na geração da capa')
      }
      await new Promise((r) => setTimeout(r, 3000))
    }
    throw new Error('Timeout aguardando geração da capa')
  }

  const handleGenerateCover = async (target: 'front' | 'back') => {
    const prompt = target === 'front' ? coverConfig.frontPrompt : coverConfig.backPrompt
    if (!prompt || !prompt.trim()) {
      setError('Defina um prompt para a capa antes de gerar.')
      return
    }
    setIsGeneratingCover(true)
    try {
      const response = await api.post('/book/generate_cover', {
        job_id: config.id || 'temp-job',
        target,
        prompt,
        api_key: apiKey || undefined,
        model_name: coverModel,
        style_names: coverConfig.selectedStyles,
        execution_mode: executionMode
      })

      let imagePath = response.data?.image_path || response.data?.file_path
      if (!imagePath && response.data?.job_id) {
        imagePath = await pollCoverJob(response.data.job_id)
      }
      if (!imagePath) {
        throw new Error(response.data?.message || 'Resposta sem caminho da imagem.')
      }

      if (target === 'front') {
        setCoverConfig({ ...coverConfig, frontImagePath: imagePath })
      } else {
        setCoverConfig({ ...coverConfig, backImagePath: imagePath })
      }

      const resolvedBookId = config.id || bookId
      if (resolvedBookId) {
        try {
          await endpoints.books.update(resolvedBookId, {
            cover_path: target === 'front' ? imagePath : coverConfig.frontImagePath,
            back_cover_path: target === 'back' ? imagePath : coverConfig.backImagePath
          })
        } catch (err) {
          console.error('❌ Failed to persist cover paths:', err)
        }
      }
    } catch (err) {
      setError(`Erro ao gerar capa: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    } finally {
      setIsGeneratingCover(false)
    }
  }

  const coverChatTools = React.useMemo<UnifiedChatAction[]>(() => [
    {
      id: 'planejar-capa-frontal',
      label: 'Planejar Capa Frontal',
      description: 'Gera o prompt da capa frontal',
      endpoint: 'local',
      keywords: ['planejar capa frontal', 'prompt frontal'],
      run: () => {
        void handlePlanCover('front')
        return 'Planejamento da capa frontal iniciado.'
      }
    },
    {
      id: 'gerar-capa-frontal',
      label: 'Gerar Capa Frontal',
      description: 'Gera a imagem da capa frontal',
      endpoint: '/book/generate_cover',
      keywords: ['gerar capa frontal', 'criar capa frontal'],
      run: () => {
        void handleGenerateCover('front')
        return 'Geração da capa frontal iniciada.'
      }
    },
    {
      id: 'planejar-capa-traseira',
      label: 'Planejar Capa Traseira',
      description: 'Gera o prompt da capa traseira',
      endpoint: 'local',
      keywords: ['planejar capa traseira', 'prompt traseira'],
      run: () => {
        void handlePlanCover('back')
        return 'Planejamento da capa traseira iniciado.'
      }
    },
    {
      id: 'gerar-capa-traseira',
      label: 'Gerar Capa Traseira',
      description: 'Gera a imagem da capa traseira',
      endpoint: '/book/generate_cover',
      keywords: ['gerar capa traseira', 'criar capa traseira'],
      run: () => {
        void handleGenerateCover('back')
        return 'Geração da capa traseira iniciada.'
      }
    },
    {
      id: 'definir-prompt-frontal',
      label: 'Definir Prompt Frontal',
      description: 'Atualiza o prompt da capa frontal',
      endpoint: 'local',
      keywords: ['definir prompt frontal', 'prompt frontal'],
      example: '/definir-prompt-frontal capa minimalista',
      run: ({ text }) => {
        const cleaned = text.replace(/^[^\\s]+\\s*/i, '').trim()
        if (!cleaned) return 'Informe o prompt após o comando.'
        setCoverConfig({ ...coverConfig, frontPrompt: cleaned })
        return 'Prompt frontal atualizado.'
      }
    },
    {
      id: 'definir-prompt-traseiro',
      label: 'Definir Prompt Traseiro',
      description: 'Atualiza o prompt da capa traseira',
      endpoint: 'local',
      keywords: ['definir prompt traseiro', 'prompt traseiro'],
      example: '/definir-prompt-traseiro capa com sinopse',
      run: ({ text }) => {
        const cleaned = text.replace(/^[^\\s]+\\s*/i, '').trim()
        if (!cleaned) return 'Informe o prompt após o comando.'
        setCoverConfig({ ...coverConfig, backPrompt: cleaned })
        return 'Prompt traseiro atualizado.'
      }
    }
  ], [coverConfig, handleGenerateCover, handlePlanCover])

  const handlePlanChapterCover = async (idx: number) => {
    setIsGeneratingChapterCover(true)

    const chapter = config.chapters[idx]
    const prompt = `Chapter divider illustration for "${chapter.title}". ${coverConfig.selectedStyles.join(', ')} style. Book interior design.`

    updateChapter(chapter.id, { coverPrompt: prompt })
    setIsGeneratingChapterCover(false)
  }

  const handleGenerateChapterCover = async (idx: number) => {
    setIsGeneratingChapterCover(true)
    try {
      const chapter = config.chapters[idx]
      const response = await api.post('/book/generate_cover', {
        job_id: config.id || 'temp-job',
        target: 'chapter',
        chapter_index: idx,
        prompt: chapter.coverPrompt || '',
        api_key: apiKey || undefined,
        model_name: coverModel,
        style_names: coverConfig.selectedStyles,
        execution_mode: executionMode
      })

      let imagePath = response.data?.image_path || response.data?.file_path
      if (!imagePath && response.data?.job_id) {
        imagePath = await pollCoverJob(response.data.job_id)
      }
      if (imagePath) {
        updateChapter(chapter.id, { coverPath: imagePath })
      } else {
        throw new Error(response.data?.message || 'Resposta sem caminho da imagem')
      }
    } catch (err) {
      setError(`Erro ao gerar capa do capítulo: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    }
    setIsGeneratingChapterCover(false)
  }

  const handleGenerateSectionContentNew = async (chapterIdx: number, sectionIdx: number) => {
    setIsGeneratingContent(true)
    setError(null)
    setSuccessMessage(null)
    try {
      const chapter = config.chapters[chapterIdx]
      const section = chapter.sections![sectionIdx]

      const prompt = `Escreva o conteúdo completo para a seção "${section.title}" do capítulo "${chapter.title}". 
      Objetivo/Diretrizes: ${section.purpose}.
      Descrição do Capítulo: ${chapter.description}. 
      Público: ${config.targetAudience}. 
      Estilo: ${config.style}.
      ${config.authorStyle ? `Inspiração/Estilo de Autor: ${config.authorStyle}` : ''}`

      const response = await api.post('/book/generate_section', {
        book_title: config.title,
        chapter_title: chapter.title,
        chapter_description: chapter.description,
        style: config.style,
        target_audience: config.targetAudience,
        api_key: apiKey || undefined,
        custom_prompt: prompt,
        execution_mode: executionMode
      })

      const generatedContent = response.data.content || ''

      const updatedChapters = [...config.chapters]
      updatedChapters[chapterIdx].sections![sectionIdx].content = generatedContent
      setConfig({ ...config, chapters: updatedChapters })
      setSuccessMessage(`Conteúdo da seção "${section.title}" gerado com sucesso.`)
    } catch (err) {
      console.error('Failed to generate section content:', err)
      setError(`Erro ao gerar conteúdo da seção: ${err instanceof Error ? err.message : 'Erro desconhecido'}`)
    } finally {
      setIsGeneratingContent(false)
    }
  }



  // ===== RENDER =====
  return (
    <div className="w-full space-y-6">
      {/* Loading State */}
      {loadingBook && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center">
          <div className="bg-white dark:bg-gray-800 rounded-xl p-8 shadow-2xl max-w-md w-full mx-4">
            <div className="flex flex-col items-center gap-4">
              <Loader2 className="w-12 h-12 text-slate-500 animate-spin" />
              <h3 className="text-xl font-semibold text-gray-900 dark:text-white">
                Carregando Livro...
              </h3>
              <p className="text-gray-500 dark:text-gray-400 text-center">
                Por favor, aguarde enquanto carregamos os dados do livro
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="text-center">
        <div className="inline-flex items-center justify-center w-16 h-16 bg-gradient-to-br from-slate-600 to-slate-700 rounded-2xl mb-4">
          <BookOpen className="w-8 h-8 text-white" />
        </div>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-white">
          Criador de Livros
        </h1>
        <p className="text-gray-500 dark:text-gray-400 mt-2">
          Crie livros completos com IA em minutos
        </p>
      </div>

      {successMessage && (
        <div className="p-4 bg-slate-50 dark:bg-slate-900/20 border border-slate-200 dark:border-slate-800 rounded-lg text-slate-700 dark:text-slate-300">
          <div className="flex items-center gap-2">
            <CheckCircle className="w-5 h-5" />
            <span className="font-medium">{successMessage}</span>
          </div>
        </div>
      )}

      {/* Existing Books Section */}
      {existingBooks.length > 0 && !bookId && (
        <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
          <button
            onClick={() => setShowExistingBooks(!showExistingBooks)}
            className="w-full px-6 py-4 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
          >
            <div className="flex items-center gap-3">
              <BookMarked className="w-5 h-5 text-slate-500" />
              <span className="font-medium text-gray-900 dark:text-white">
                Meus Livros ({existingBooks.length})
              </span>
            </div>
            <RefreshCw
              className={cn(
                "w-4 h-4 text-gray-400 transition-transform",
                showExistingBooks && "rotate-180"
              )}
            />
          </button>

          {showExistingBooks && (
            <div className="border-t border-gray-200 dark:border-gray-700 p-4">
              {loadingBooks ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="w-6 h-6 animate-spin text-slate-500" />
                  <span className="ml-2 text-gray-500">Carregando livros...</span>
                </div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {existingBooks.map((book) => (
                    <div
                      key={book.id}
                      className="group relative bg-gradient-to-br from-slate-50 to-slate-100 dark:from-gray-700 dark:to-gray-600 rounded-lg p-4 border border-slate-200 dark:border-gray-600 hover:shadow-lg transition-all cursor-pointer"
                      onClick={() => navigate(bookLibraryEditPath(book.id, book.status))}
                    >
                      <div className="flex items-start gap-4">
                        <div className="w-12 h-16 bg-gradient-to-br from-slate-600 to-slate-700 rounded flex items-center justify-center flex-shrink-0">
                          <BookOpen className="w-6 h-6 text-white" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <h3 className="font-semibold text-gray-900 dark:text-white truncate">
                            {book.topic || book.title || 'Sem título'}
                          </h3>
                          <p className="text-sm text-gray-600 dark:text-gray-300 mt-1">
                            {book.num_chapters || 0} capítulos
                          </p>
                          <div className="flex items-center gap-2 mt-2">
                            <span className={cn(
                              "text-xs px-2 py-1 rounded-full",
                              book.status === 'completed'
                                ? "bg-slate-100 text-slate-700 dark:bg-slate-900/30 dark:text-slate-300"
                                : "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-300"
                            )}>
                              {book.status === 'completed' ? 'Completo' : 'Em progresso'}
                            </span>
                            {book.created_at && (
                              <span className="text-xs text-gray-500">
                                {new Date(book.created_at * 1000).toLocaleDateString('pt-BR')}
                              </span>
                            )}
                          </div>

                          {/* Explicit Actions */}
                          <div className="flex items-center gap-2 mt-3 pt-3 border-t border-slate-200 dark:border-gray-500/50">
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                navigate(bookLibraryEditPath(book.id, book.status))
                              }}
                              className="px-3 py-1.5 bg-white dark:bg-gray-600 text-slate-600 dark:text-slate-300 text-xs font-semibold rounded border border-slate-200 dark:border-gray-500 hover:bg-slate-50 dark:hover:bg-gray-500 flex items-center gap-1 transition-colors"
                            >
                              <Edit3 className="w-3 h-3" /> Editar
                            </button>
                            <div className="text-xs text-gray-400 ml-auto">
                              Clicar para abrir
                            </div>
                          </div>
                        </div>
                      </div>
                      <div className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity">
                        <p className="text-xs text-slate-600 font-medium bg-white/90 px-2 py-1 rounded shadow-sm">
                          Abrir livro
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Create New Book Button */}
              <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setShowExistingBooks(false)
                    setStep('info')
                  }}
                  className="w-full py-3 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg hover:from-slate-700 hover:to-slate-800 transition-all flex items-center justify-center gap-2 font-medium shadow-lg"
                >
                  <Plus className="w-5 h-5" />
                  Criar Novo Livro
                </button>

                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    navigate('/books')
                  }}
                  className="mt-3 w-full py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 transition-all flex items-center justify-center gap-2 font-medium"
                >
                  <BookOpen className="w-4 h-4" />
                  Ver Biblioteca Completa
                </button>
              </div>

              {existingBooks.length > 0 && (
                <div className="mt-2 text-center">
                  <button
                    onClick={handleDeleteAllBooks}
                    className="text-xs text-red-500 hover:text-red-700 hover:underline flex items-center justify-center gap-1 mx-auto"
                  >
                    <Trash2 className="w-3 h-3" />
                    Apagar Biblioteca (Irreversível)
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )
      }

      {/* Back Button when viewing existing book */}
      {
        bookId && (
          <button
            onClick={() => navigate('/books')}
            className="flex items-center gap-2 text-slate-600 hover:text-slate-700 dark:text-slate-300 dark:hover:text-slate-200 font-medium"
          >
            ← Voltar à Biblioteca
          </button>
        )
      }

      {/* Progress Steps */}
      <div className="flex items-center justify-center gap-4 flex-wrap">
        {[
          { id: 'info', label: 'Informações' },
          { id: 'structure', label: 'Estrutura' },
          { id: 'sections', label: 'Seções' },
          { id: 'design', label: 'Design' },
          { id: 'confirm', label: 'Confirmar' },
        ].map((s, i) => (
          <div key={s.id} className="flex items-center">
            <button
              onClick={() => {
                if (s.id === 'info') setStep('info')
                else if (s.id === 'structure') setStep('structure')
                else if (s.id === 'sections' && config.chapters.length > 0) setStep('sections')
                else if (s.id === 'design' && config.chapters.length > 0) setStep('design')
              }}
              className={cn(
                'flex items-center gap-2 px-4 py-2 rounded-lg transition-all',
                step === s.id
                  ? 'bg-slate-100 text-slate-700 dark:bg-slate-900/20 dark:text-slate-300'
                  : 'text-gray-500 hover:text-gray-700'
              )}
            >
              <span className={cn(
                'w-6 h-6 rounded-full flex items-center justify-center text-sm font-medium',
                step === s.id ? 'bg-slate-600 text-white' : 'bg-gray-200 text-gray-600'
              )}>
                {i + 1}
              </span>
              <span className="hidden md:inline">{s.label}</span>
            </button>
            {i < 4 && <div className="w-8 h-px bg-gray-200 mx-2" />}
          </div>
        ))}
      </div>

      {/* Step 1: Book Info */}
      {
        step === 'info' && (
          <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6 space-y-6">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Rascunho do Livro
              </label>
              <textarea
                value={config.draft}
                onChange={(e) => setConfig({ ...config, draft: e.target.value })}
                placeholder="Cole aqui o rascunho completo do livro. Esse texto será usado como base para capítulos, seções, planejamentos e conteúdos."
                rows={6}
                className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
              />
            </div>

            {/* Title */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Título do Livro
              </label>
              <input
                type="text"
                value={config.title}
                onChange={(e) => setConfig({ ...config, title: e.target.value })}
                placeholder="Ex: Guia Completo de Python para Iniciantes"
                className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
              />
            </div>

            {/* Author Name */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Nome do Autor
              </label>
              <input
                type="text"
                value={config.authorName}
                onChange={(e) => setConfig({ ...config, authorName: e.target.value })}
                placeholder="Seu nome"
                className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
              />
            </div>

            {/* Topic */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Tema/Assunto Principal
              </label>
              <textarea
                value={config.topic}
                onChange={(e) => setConfig({ ...config, topic: e.target.value })}
                placeholder="Descreva o tema principal do livro e os pontos que devem ser abordados..."
                rows={3}
                className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
              />
            </div>

            {/* Target Audience */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Público Alvo
              </label>
              <input
                type="text"
                value={config.targetAudience}
                onChange={(e) => setConfig({ ...config, targetAudience: e.target.value })}
                placeholder="Ex: Desenvolvedores iniciantes, estudantes de tecnologia"
                className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
              />
            </div>

            {/* Idioma do livro (padrão: português) */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Idioma do livro
              </label>
              <select
                value={config.language || 'Português (Brasil)'}
                onChange={(e) => setConfig({ ...config, language: e.target.value })}
                className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
              >
                {[
                  'Português (Brasil)',
                  'English',
                  'Español',
                  'Français',
                  'Deutsch',
                  'Italiano',
                  '日本語',
                ].map((lang) => (
                  <option key={lang} value={lang}>
                    {lang}
                  </option>
                ))}
              </select>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Por padrão o conteúdo é gerado em português; altere apenas se quiser outro idioma.
              </p>
            </div>

            {/* Style Selection */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
                Estilo de Escrita
              </label>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                {BOOK_STYLES.map((style) => (
                  <button
                    key={style.id}
                    onClick={() => setConfig({ ...config, style: style.id })}
                    className={cn(
                      'p-3 rounded-lg border text-left transition-all',
                      config.style === style.id
                        ? 'border-slate-500 bg-slate-50 dark:bg-slate-900/20'
                        : 'border-gray-200 dark:border-gray-600 hover:border-gray-300'
                    )}
                  >
                    <p className="font-medium text-gray-900 dark:text-white text-sm">
                      {style.name}
                    </p>
                    <p className="text-xs text-gray-500 mt-1">{style.description}</p>
                  </button>
                ))}
              </div>
            </div>

            {/* Book Genre / Estilo do Livro (metadata) */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
                Estilo do Livro (Gênero)
              </label>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
                {BOOK_GENRES.map((g) => (
                  <button
                    key={g.id}
                    onClick={() => {
                      const current = config.bookStyle || []
                      const next = current.includes(g.id)
                        ? current.filter((id) => id !== g.id)
                        : [...current, g.id]
                      setConfig({ ...config, bookStyle: next })
                    }}
                    className={cn(
                      'p-2 rounded-lg border text-left transition-all text-sm',
                      (config.bookStyle || []).includes(g.id)
                        ? 'border-slate-500 bg-slate-50 dark:bg-slate-900/20'
                        : 'border-gray-200 dark:border-gray-600 hover:border-gray-300'
                    )}
                  >
                    {g.name}
                  </button>
                ))}
              </div>

              <div className="mb-3">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Descrição / Diretrizes do Estilo</label>
                <textarea
                  value={config.bookStylePrompt || ''}
                  onChange={(e) => setConfig({ ...config, bookStylePrompt: e.target.value })}
                  placeholder="Ex: Tom leve e sarcástico, foco em personagens..."
                  rows={4}
                  className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                />
              </div>

              <div className="flex gap-3">
                <button
                  onClick={async () => {
                    // Re-plan chapters using backend AI
                    setIsPlanningStructure(true)
                    setError(null)
                    try {
                      const resolvedBookId = config.id || bookId
                      if (!resolvedBookId) throw new Error('Livro não identificado para replanejamento.')
                      await api.post('/book/generate_chapters_ai', {
                        job_id: resolvedBookId,
                        api_key: apiKey || undefined,
                        num_chapters: config.numChapters,
                        num_sections_per_chapter: 3,
                        author_inspiration: config.bookStylePrompt || undefined,
                        author_styles: (config.bookStyle && config.bookStyle.length > 0) ? config.bookStyle : undefined,
                        book_objective: undefined
                      })
                      setSuccessMessage('Replanejamento iniciado. Verifique os logs para progresso.')
                    } catch (err) {
                      console.error('Failed to replan chapters:', err)
                      setError(err instanceof Error ? err.message : 'Erro ao replanejar capítulos')
                    }
                    setIsPlanningStructure(false)
                  }}
                  disabled={isPlanningStructure}
                  className="px-4 py-3 bg-indigo-600 text-white rounded-lg disabled:opacity-50"
                >
                  {isPlanningStructure ? 'Replanejando...' : '✳️ Planejar Capítulos'}
                </button>
                <button
                  onClick={() => setConfig({ ...config, bookStyle: [], bookStylePrompt: '' })}
                  className="px-4 py-3 border rounded-lg"
                >Limpar</button>
              </div>
            </div>

            {/* Author Style / Inspiration - Multi Select */}
            <AuthorStyleSelector
              selectedStyles={config.selectedAuthorStyles}
              onChange={(styles) => setConfig({ ...config, selectedAuthorStyles: styles })}
              className="mb-6"
            />

            {/* Default Manual Input (Hidden or separate, maybe just remove) */}
            {/* We keep the old input as an "Custom" option optional or just rely on selection */}

            {/* Execution Mode */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                <BarChart3 className="w-4 h-4 inline mr-1" />
                Modo de Execução
              </label>
              <div className="flex gap-2">
                {[
                  { id: 'mock', label: '🧪 Mock', desc: 'Rápido, sem custo (teste)' },
                  { id: 'economic', label: '💰 Econômico', desc: 'Rápido, baixo custo' },
                  { id: 'full', label: '🚀 Full', desc: 'Melhor qualidade (mais lento)' }
                ].map((mode) => (
                  <button
                    key={mode.id}
                    onClick={() => handleModeChange(mode.id)}
                    className={cn(
                      'flex-1 p-3 rounded-lg border text-left transition-all',
                      executionMode === mode.id
                        ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/20'
                        : 'border-gray-200 dark:border-gray-600 hover:border-gray-300'
                    )}
                  >
                    <div className="font-medium text-gray-900 dark:text-white capitalize">
                      {mode.label}
                    </div>
                    <div className="text-xs text-gray-500">
                      {mode.desc}
                    </div>
                  </button>
                ))}
              </div>
            </div>

            {/* Number of Chapters */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                <List className="w-4 h-4 inline mr-1" />
                Número de Capítulos
              </label>
              <div className="flex flex-col gap-3">
                <input
                  type="range"
                  min={5}
                  max={25}
                  value={config.numChapters}
                  onChange={(e) => setConfig({ ...config, numChapters: parseInt(e.target.value) })}
                  className="w-full"
                />
                <div className="flex items-center justify-between text-sm text-gray-500">
                  <span>5 capítulos</span>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={5}
                      max={25}
                      value={config.numChapters}
                      onChange={(e) => {
                        const value = Number(e.target.value)
                        if (Number.isNaN(value)) return
                        const clamped = Math.min(25, Math.max(5, value))
                        setConfig({ ...config, numChapters: clamped })
                      }}
                      className="w-20 px-2 py-1 border rounded-md text-sm text-gray-700 dark:bg-gray-700 dark:border-gray-600 dark:text-gray-200"
                    />
                    <span className="font-medium text-slate-600">capítulos</span>
                  </div>
                  <span>25 capítulos</span>
                </div>
              </div>
            </div>

            {/* Padrões das seções: mínimo de palavras e código fonte */}
            <div className="space-y-3 p-4 rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50/50 dark:bg-gray-900/30">
              <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200">
                Padrões das seções (para geração com IA)
              </h3>
              <div className="flex flex-wrap items-center gap-6">
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <span>Quantidade mínima de palavras por seção:</span>
                  <input
                    type="number"
                    min={0}
                    step={50}
                    placeholder="Ex.: 400 (vazio = padrão)"
                    value={config.defaultMinTextLength ?? ''}
                    onChange={(e) => {
                      const v = e.target.value
                      const num = v === '' ? undefined : Math.max(0, parseInt(v, 10) || 0)
                      setConfig({ ...config, defaultMinTextLength: num })
                    }}
                    className="w-28 px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
                  />
                </label>
                <label className="flex items-center gap-2 cursor-pointer text-sm text-gray-700 dark:text-gray-300">
                  <input
                    type="checkbox"
                    checked={config.defaultHasSourceCode ?? false}
                    onChange={(e) => setConfig({ ...config, defaultHasSourceCode: e.target.checked })}
                    className="rounded border-gray-300 dark:border-gray-600 text-slate-600 focus:ring-slate-500"
                  />
                  <span>Seções devem incluir código fonte (padrão)</span>
                </label>
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Estes valores são usados como padrão ao criar novas seções e na geração de conteúdo com IA. Cada seção pode ser ajustada depois na aba Capítulos.
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
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Remove só o conteúdo em markdown de todas as seções.</p>
              </div>
            </div>

            {/* Include Images */}
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={config.includeImages}
                onChange={(e) => setConfig({ ...config, includeImages: e.target.checked })}
                className="w-4 h-4 rounded border-gray-300 text-slate-600 focus:ring-slate-500"
              />
              <div>
                <p className="font-medium text-gray-900 dark:text-white">
                  Incluir Ilustrações
                </p>
                <p className="text-sm text-gray-500">
                  Gerar imagens para cada capítulo
                </p>
              </div>
            </label>

            {/* Execution Mode Selector */}
            <div className="pt-4 border-t border-gray-200 dark:border-gray-700">
              <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2 mb-3">
                ⚙️ Modo de Execução
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
                Selecione o modo para as operações de IA
              </p>
              <div className="grid grid-cols-3 gap-2">
                {(['mock', 'economic', 'full'] as const).map((mode) => (
                  <button
                    key={mode}
                    onClick={() => handleModeChange(mode)}
                    disabled={loadingModes}
                    className={cn(
                      "px-3 py-2 text-sm font-medium rounded-lg transition-all flex items-center justify-center gap-1",
                      executionMode === mode
                        ? mode === 'mock' ? 'bg-purple-600 text-white' :
                          mode === 'economic' ? 'bg-slate-600 text-white' :
                            'bg-slate-700 text-white'
                        : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                    )}
                  >
                    {mode === 'mock' ? '🧪' : mode === 'economic' ? '💰' : '🚀'}
                    <span>{mode === 'mock' ? 'Mock' : mode === 'economic' ? 'Econômico' : 'Full'}</span>
                  </button>
                ))}
              </div>
              {modesConfig && (
                <p className="text-xs text-gray-400 mt-2">
                  Custo estimado: {Math.round(modesConfig.estimated_cost_multiplier * 100)}%
                </p>
              )}
            </div>

            <div className="flex flex-col sm:flex-row gap-3">
              <button
                type="button"
                onClick={handleSavePlanningProgress}
                disabled={isSavingPlanning || isPlanningStructure}
                className="sm:flex-1 py-3 px-4 border-2 border-slate-300 dark:border-slate-600 rounded-lg font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800/50 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {isSavingPlanning ? <Loader2 className="w-5 h-5 animate-spin" /> : <BookMarked className="w-5 h-5" />}
                Salvar rascunho na biblioteca
              </button>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 -mt-2">
              Grava título, tema, rascunho e preferências sem gastar IA. Depois use «Gerar Estrutura» no mesmo livro (reutiliza o rascunho).
            </p>

            <div className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-center">
              <button
                type="button"
                onClick={() => setChapterPlanningDialogOpen(true)}
                className="py-2.5 px-4 rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50 dark:bg-indigo-950/40 text-indigo-800 dark:text-indigo-200 text-sm font-medium hover:bg-indigo-100 dark:hover:bg-indigo-900/40 flex items-center justify-center gap-2"
              >
                <MessageSquare className="w-4 h-4 shrink-0" />
                Orientações para o agente (capítulos)
              </button>
              {config.chapterPlanningInstructions?.trim() ? (
                <span className="text-xs text-indigo-600 dark:text-indigo-400 sm:ml-1">
                  Instruções ativas — usadas ao gerar a estrutura
                </span>
              ) : null}
            </div>

            <Dialog.Root open={chapterPlanningDialogOpen} onOpenChange={setChapterPlanningDialogOpen}>
              <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 z-[80] bg-black/40 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
                <Dialog.Content className="fixed left-1/2 top-1/2 z-[80] flex max-h-[min(90vh,720px)] w-[min(100%,min(96vw,560px))] -translate-x-1/2 -translate-y-1/2 flex-col rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-xl p-0">
                  <Dialog.Title className="sr-only">Orientações para planejamento dos capítulos</Dialog.Title>
                  <Dialog.Description className="sr-only">
                    Texto enviado ao agente que define títulos, propósitos e sequência dos capítulos.
                  </Dialog.Description>
                  <div className="px-5 pt-5 pb-3 border-b border-gray-200 dark:border-gray-700 flex items-start justify-between gap-3">
                    <div>
                      <h3 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
                        <MessageSquare className="w-5 h-5 text-indigo-600 dark:text-indigo-400" />
                        Orientações para o agente
                      </h3>
                      <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                        Descreva ritmo, temas por capítulo, tom, exclusões ou ordem pedagógica. Isso orienta a IA ao gerar a estrutura (cada capítulo).
                      </p>
                    </div>
                    <Dialog.Close asChild>
                      <button
                        type="button"
                        className="rounded-lg p-2 text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
                        aria-label="Fechar"
                      >
                        <span className="text-lg leading-none">×</span>
                      </button>
                    </Dialog.Close>
                  </div>
                  <div className="px-5 py-4 flex-1 min-h-0 flex flex-col gap-2">
                    <textarea
                      className="w-full min-h-[220px] flex-1 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 p-3 text-sm resize-y focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                      value={config.chapterPlanningInstructions || ''}
                      onChange={(e) =>
                        setConfig((c) => ({ ...c, chapterPlanningInstructions: e.target.value }))
                      }
                      placeholder="Ex.: Capítulos 1–3 só fundamentos; capítulo 4 um estudo de caso; evitar jargão antes do cap. 5; incluir um capítulo sobre ética antes do encerramento…"
                    />
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      {(config.chapterPlanningInstructions || '').length} caracteres — salve o planejamento ou gere a estrutura para persistir no livro.
                    </p>
                  </div>
                  <div className="px-5 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-2">
                    <Dialog.Close asChild>
                      <button
                        type="button"
                        className="px-4 py-2 rounded-lg bg-slate-700 text-white text-sm font-medium hover:bg-slate-800"
                      >
                        Concluído
                      </button>
                    </Dialog.Close>
                  </div>
                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>

            {/* Generate Button */}
            <button
              onClick={handleGenerateStructure}
              disabled={isPlanningStructure}
              className="w-full py-4 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2 hover:from-slate-700 hover:to-slate-800 disabled:opacity-50"
            >
              {isPlanningStructure ? (
                <Loader2 className="w-5 h-5 animate-spin" />
              ) : (
                <FileText className="w-5 h-5" />
              )}
              {isPlanningStructure ? 'Gerando Estrutura...' : 'Gerar Estrutura do Livro'}
            </button>

            {/* Logs Inline Display */}
            {(showLogModal || logs.length > 0) && (
              <div className="mt-4 p-4 bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 max-h-60 overflow-y-auto font-mono text-sm">
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2 flex items-center gap-2">
                  <Loader2 className={cn("w-3 h-3", isPlanningStructure ? "animate-spin" : "")} />
                  Processamento
                </h3>
                <div className="space-y-1">
                  {logs.length === 0 && isPlanningStructure && (
                    <p className="text-gray-400 italic">Iniciando agentes...</p>
                  )}
                  {logs.map((log, i) => (
                    <div key={i} className="text-gray-600 dark:text-gray-400 border-l-2 border-slate-200 pl-2">
                      {log}
                    </div>
                  ))}
                  <div ref={(el) => el?.scrollIntoView({ behavior: 'smooth' })} />
                </div>
              </div>
            )}

            {error && (
              <div className="mt-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-400">
                <div className="flex items-start gap-3">
                  <div className="flex-shrink-0">
                    ⚠️
                  </div>
                  <div className="flex-1">
                    <p className="font-medium">{error}</p>
                    {error.includes('API Key não configurada') && (
                      <div className="mt-3 flex gap-2">
                        <button
                          onClick={() => navigate('/system')}
                          className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors text-sm font-medium"
                        >
                          🔑 Configurar API Keys
                        </button>
                        <button
                          onClick={() => setError(null)}
                          className="px-4 py-2 border border-red-300 dark:border-red-700 rounded-lg hover:bg-red-100 dark:hover:bg-red-900/40 transition-colors text-sm"
                        >
                          Fechar
                        </button>
                      </div>
                    )}
                    {structureJobId && !error.includes('API Key não configurada') && (
                      <div className="mt-3">
                        <button
                          onClick={() => navigate(`/book/${structureJobId}`)}
                          className="px-3 py-2 border border-red-200 dark:border-red-800 rounded-lg text-sm"
                        >
                          Abrir Workspace do Livro
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        )
      }

      {/* Step 2: Chapter Structure */}
      {
        step === 'structure' && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                Estrutura do Livro ({config.chapters.length} capítulos)
              </h2>
              <div className="flex gap-2">
                <button
                  onClick={() => setStep('info')}
                  className="px-4 py-2 border rounded-lg hover:bg-gray-50"
                >
                  Voltar
                </button>
                <button
                  onClick={addChapter}
                  className="flex items-center gap-2 px-4 py-2 bg-slate-100 text-slate-700 rounded-lg hover:bg-slate-200"
                >
                  <Plus className="w-4 h-4" />
                  Adicionar Capítulo
                </button>
              </div>
            </div>

            {/* Padrões das seções: visível na tela de estrutura */}
            <div className="p-4 rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50/50 dark:bg-gray-900/30 space-y-3">
              <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200">
                Padrões das seções (geração com IA e novas seções)
              </h3>
              <div className="flex flex-wrap items-center gap-6">
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <span>Quantidade mínima de palavras por seção:</span>
                  <input
                    type="number"
                    min={0}
                    step={50}
                    placeholder="Ex.: 400 (vazio = padrão)"
                    value={config.defaultMinTextLength ?? ''}
                    onChange={(e) => {
                      const v = e.target.value
                      const num = v === '' ? undefined : Math.max(0, parseInt(v, 10) || 0)
                      setConfig({ ...config, defaultMinTextLength: num })
                    }}
                    className="w-28 px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
                  />
                </label>
                <label className="flex items-center gap-2 cursor-pointer text-sm text-gray-700 dark:text-gray-300">
                  <input
                    type="checkbox"
                    checked={config.defaultHasSourceCode ?? false}
                    onChange={(e) => setConfig({ ...config, defaultHasSourceCode: e.target.checked })}
                    className="rounded border-gray-300 dark:border-gray-600 text-slate-600 focus:ring-slate-500"
                  />
                  <span>Seções incluem código fonte (padrão)</span>
                </label>
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Aplicado ao criar novas seções e na geração de conteúdo com IA. Cada seção pode ser ajustada na aba Capítulos.
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

            {config.draft && (
              <div className="bg-slate-50 dark:bg-slate-900/20 border border-slate-200 dark:border-slate-700 rounded-lg p-4">
                <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 mb-2">
                  Rascunho do Livro
                </div>
                <textarea
                  value={config.draft}
                  readOnly
                  rows={5}
                  className="w-full px-3 py-2 border rounded-lg text-sm bg-white/80 dark:bg-gray-800/60 dark:border-gray-600"
                />
              </div>
            )}

            {config.chapters.map((chapter, index) => (
              <div
                key={chapter.id}
                className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
              >
                <div className="flex items-start gap-4">
                  <div className="flex-shrink-0 cursor-grab">
                    <GripVertical className="w-5 h-5 text-gray-400" />
                  </div>
                  <div className="w-10 h-10 bg-slate-100 dark:bg-slate-900/20 rounded-lg flex items-center justify-center flex-shrink-0">
                    <span className="font-bold text-slate-600">{index + 1}</span>
                  </div>
                  <div className="flex-1 space-y-2">
                    <input
                      type="text"
                      value={chapter.title}
                      onChange={(e) => updateChapter(chapter.id, { title: e.target.value })}
                      className="w-full px-3 py-2 border rounded-lg font-medium dark:bg-gray-700 dark:border-gray-600"
                      placeholder="Título do capítulo"
                    />
                    <textarea
                      value={chapter.description}
                      onChange={(e) => updateChapter(chapter.id, { description: e.target.value })}
                      rows={2}
                      className="w-full px-3 py-2 border rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 mb-2"
                      placeholder="Descrição do conteúdo do capítulo..."
                    />
                    <div className="relative">
                      <span className="absolute top-2 left-2 text-xs font-bold text-gray-400 uppercase">Objetivo</span>
                      <textarea
                        value={chapter.objective || ''}
                        onChange={(e) => updateChapter(chapter.id, { objective: e.target.value })}
                        rows={2}
                        className="w-full pl-3 pt-6 pb-2 pr-3 border rounded-lg text-sm bg-blue-50/50 dark:bg-blue-900/10 border-blue-100 dark:border-blue-900/30 text-gray-700 dark:text-gray-300"
                        placeholder="Defina o objetivo pedagógico ou narrativo deste capítulo..."
                      />
                    </div>
                    <div className="relative">
                      <span className="absolute top-2 left-2 text-xs font-bold text-gray-400 uppercase">Orientação para a IA (opcional)</span>
                      <textarea
                        value={chapter.creation_guide || ''}
                        onChange={(e) => updateChapter(chapter.id, { creation_guide: e.target.value })}
                        rows={3}
                        className="w-full pl-3 pt-6 pb-2 pr-3 border rounded-lg text-sm bg-amber-50/40 dark:bg-amber-900/10 border-amber-100 dark:border-amber-900/25 text-gray-700 dark:text-gray-300"
                        placeholder="Ex.: tom, exemplos obrigatórios, o que evitar, público específico deste capítulo… Usado ao planejar seções e ao gerar texto no workspace."
                      />
                    </div>
                  </div>
                  <button
                    onClick={() => deleteChapter(chapter.id)}
                    className="p-2 text-gray-400 hover:text-red-600"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}

            {/* Continue / save draft */}
            <div className="flex flex-col sm:flex-row gap-3">
              <button
                type="button"
                onClick={handleSavePlanningProgress}
                disabled={isSavingPlanning}
                className="sm:flex-1 py-3 px-4 border border-slate-300 dark:border-slate-600 rounded-lg font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800/50 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {isSavingPlanning ? <Loader2 className="w-5 h-5 animate-spin" /> : <BookMarked className="w-5 h-5" />}
                Salvar planejamento (continuar)
              </button>
              <button
                type="button"
                onClick={handleSavePlanningAndExit}
                disabled={isSavingPlanning}
                className="sm:flex-1 py-3 px-4 border-2 border-slate-300 dark:border-slate-600 rounded-lg font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800/50 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {isSavingPlanning ? <Loader2 className="w-5 h-5 animate-spin" /> : <BookMarked className="w-5 h-5" />}
                Salvar planejamento e sair
              </button>
              <button
                type="button"
                onClick={() => setStep('sections')}
                className="sm:flex-[1.4] py-4 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2 hover:from-slate-700 hover:to-slate-800"
              >
                <List className="w-5 h-5" />
                Ir para Planejamento de Seções
              </button>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 text-center">
              O salvamento cria ou atualiza o livro na biblioteca (pode ser só metadados ou capítulos já editados).
            </p>
          </div>
        )
      }

      {/* Step 3: Sections Planning */}
      {
        step === 'sections' && (
          <div className="space-y-6">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-xl font-bold text-gray-900 dark:text-white">
                  Planejamento de Seções
                </h2>
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  Detalhe o conteúdo de cada capítulo antes de escrever.
                </p>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => {
                    const total = config.chapters.reduce((acc, ch) => acc + (ch.sections?.length || 0), 0)
                    if (total === 0) return
                    if (!window.confirm(`Apagar todas as ${total} seção(ões) do livro (todos os capítulos)?\n\nEsta ação não pode ser desfeita.`)) return
                    const updated = config.chapters.map((ch) => ({ ...ch, sections: [] }))
                    setConfig({ ...config, chapters: updated })
                  }}
                  disabled={config.chapters.every((ch) => !(ch.sections?.length))}
                  className="px-4 py-2 border border-red-200 text-red-700 rounded-lg hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-900/20 disabled:opacity-50 disabled:cursor-not-allowed"
                  title="Remove todas as seções de todos os capítulos"
                >
                  <Trash2 className="w-4 h-4 inline mr-1" />
                  Apagar todas as seções do livro
                </button>
                <button
                  onClick={() => setStep('structure')}
                  className="px-4 py-2 border rounded-lg hover:bg-gray-50"
                >
                  Voltar
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-6 h-[600px]">
              {/* Left: Chapter List */}
              <div className="col-span-1 bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 flex flex-col overflow-hidden">
                <div className="p-4 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50">
                  <h3 className="font-semibold text-gray-700 dark:text-gray-300">Capítulos</h3>
                </div>
                <div className="flex-1 overflow-y-auto p-2 space-y-1">
                  {config.chapters.map((chapter, idx) => (
                    <button
                      key={chapter.id}
                      onClick={() => setSelectedChapterIdx(idx)}
                      className={cn(
                        "w-full text-left px-3 py-3 rounded-lg text-sm transition-colors flex items-start gap-3",
                        selectedChapterIdx === idx
                          ? "bg-slate-100 text-slate-900 dark:bg-slate-900/30 dark:text-slate-100 ring-1 ring-slate-500"
                          : "hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-600 dark:text-gray-400"
                      )}
                    >
                      <div className={cn(
                        "flex-shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold",
                        selectedChapterIdx === idx ? "bg-slate-600 text-white" : "bg-gray-200 text-gray-500"
                      )}>
                        {idx + 1}
                      </div>
                      <span className="font-medium line-clamp-2">{chapter.title}</span>
                    </button>
                  ))}
                </div>
              </div>

              {/* Right: Sections Editor */}
              <div className="col-span-1 md:col-span-2 bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 flex flex-col overflow-hidden">
                {config.chapters[selectedChapterIdx] ? (
                  <>
                    <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center bg-gray-50 dark:bg-gray-900/50">
                      <div>
                        <h3 className="font-bold text-lg text-gray-900 dark:text-white">
                          Capítulo {selectedChapterIdx + 1}: {config.chapters[selectedChapterIdx].title}
                        </h3>
                        {/* Success Message Area */}
                        {successMessage && (
                          <div className="mb-4 p-3 bg-green-50 text-green-700 border border-green-200 rounded-lg text-sm flex items-center gap-2 animate-in fade-in slide-in-from-top-2 duration-300">
                            <CheckCircle className="w-4 h-4" />
                            {successMessage}
                            <button 
                              onClick={() => setSuccessMessage(null)} 
                              className="ml-auto text-green-600 hover:text-green-800"
                            >
                              ✕
                            </button>
                          </div>
                        )}
                        {/* Error Message Area */}
                        {error && (
                          <div className="mb-4 p-3 bg-red-50 text-red-700 border border-red-200 rounded-lg text-sm flex items-center gap-2 animate-in fade-in slide-in-from-top-2 duration-300">
                            <HelpCircle className="w-4 h-4" />
                            {error}
                            <button 
                              onClick={() => setError(null)} 
                              className="ml-auto text-red-600 hover:text-red-800"
                            >
                              ✕
                            </button>
                          </div>
                        )}
                        <p className="text-sm text-gray-600 dark:text-gray-400 mb-1">
                          {config.chapters[selectedChapterIdx].description}
                        </p>
                        {config.chapters[selectedChapterIdx].objective && (
                          <div className="flex items-start gap-1 text-xs text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/10 px-2 py-1 rounded">
                            <span className="font-bold uppercase">Objetivo:</span>
                            {config.chapters[selectedChapterIdx].objective}
                          </div>
                        )}
                        <div className="mt-2 space-y-1">
                          <label className="text-xs font-semibold text-amber-800 dark:text-amber-200/90">
                            Orientação para a IA neste capítulo (opcional)
                          </label>
                          <textarea
                            value={config.chapters[selectedChapterIdx].creation_guide || ''}
                            onChange={(e) => {
                              const updated = [...config.chapters]
                              updated[selectedChapterIdx] = {
                                ...updated[selectedChapterIdx],
                                creation_guide: e.target.value,
                              }
                              setConfig({ ...config, chapters: updated })
                            }}
                            rows={3}
                            placeholder="Diretrizes extras para planejamento e escrita deste capítulo…"
                            className="w-full text-sm border rounded-lg p-2 dark:bg-gray-900/40 dark:border-amber-900/40 border-amber-200/80"
                          />
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <EpubPreview
                          mode="chapter"
                          jobId={config.id || bookId || undefined}
                          apiKey={apiKey || undefined}
                          chapter={config.chapters[selectedChapterIdx]}
                          chapterNumber={selectedChapterIdx + 1}
                        />
                        <button
                          onClick={() => {
                            const updated = [...config.chapters]
                            const newSection: Section = {
                              title: `Nova Seção`,
                              purpose: '',
                              content: '',
                              images: [],
                              code_blocks: [],
                              min_text_length: config.defaultMinTextLength,
                              has_source_code: config.defaultHasSourceCode ?? false
                            }
                            const currentSections = updated[selectedChapterIdx].sections || []
                            updated[selectedChapterIdx].sections = [...currentSections, newSection]
                            setConfig({ ...config, chapters: updated })
                          }}
                          className="flex items-center gap-1 px-3 py-1.5 bg-slate-100 text-slate-700 rounded-lg text-sm hover:bg-slate-200"
                        >
                          <Plus className="w-4 h-4" />
                          Add Seção
                        </button>
                        <button
                          onClick={() => {
                            const count = (config.chapters[selectedChapterIdx].sections || []).length
                            if (count === 0) return
                            if (!window.confirm(`Apagar todas as ${count} seção(ões) deste capítulo?\n\nEsta ação não pode ser desfeita.`)) return
                            const updated = [...config.chapters]
                            updated[selectedChapterIdx].sections = []
                            setConfig({ ...config, chapters: updated })
                          }}
                          disabled={(config.chapters[selectedChapterIdx].sections || []).length === 0}
                          className="flex items-center gap-1 px-3 py-1.5 bg-red-50 text-red-700 rounded-lg text-sm hover:bg-red-100 dark:bg-red-900/20 dark:text-red-300 dark:hover:bg-red-900/40 disabled:opacity-50 disabled:cursor-not-allowed"
                          title="Remove todas as seções do capítulo selecionado"
                        >
                          <Trash2 className="w-4 h-4" />
                          Apagar todas as seções
                        </button>
                      </div>
                    </div>

                    <div className="flex-1 overflow-y-auto p-4 space-y-6">
                      {(config.chapters[selectedChapterIdx].sections || []).length === 0 ? (
                        <div className="h-full flex flex-col items-center justify-center text-gray-400 space-y-2 opacity-60">
                          <List className="w-12 h-12" />
                          <p>Nenhuma seção definida.</p>
                        </div>
                      ) : (
                        (config.chapters[selectedChapterIdx].sections || []).map((section, sIdx) => (
                          <div key={sIdx} className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 relative group bg-gray-50/50 dark:bg-gray-900/50 space-y-4">
                            <div className="absolute top-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity flex gap-2">
                              <button
                                onClick={() => {
                                  const updated = [...config.chapters]
                                  updated[selectedChapterIdx].sections = updated[selectedChapterIdx].sections!.filter((_, i) => i !== sIdx)
                                  setConfig({ ...config, chapters: updated })
                                }}
                                className="p-1 text-gray-400 hover:text-red-600 rounded"
                              >
                                <Trash2 className="w-4 h-4" />
                              </button>
                            </div>

                            {/* Header: Title & Purpose */}
                            <div className="flex gap-3">
                              <div className="pt-2">
                                <div className="w-6 h-6 bg-gray-200 dark:bg-gray-700 rounded text-xs flex items-center justify-center font-mono text-gray-500">
                                  {selectedChapterIdx + 1}.{sIdx + 1}
                                </div>
                              </div>
                              <div className="flex-1 space-y-2">
                                <input
                                  type="text"
                                  value={section.title}
                                  onChange={(e) => {
                                    const updated = [...config.chapters]
                                    updated[selectedChapterIdx].sections![sIdx].title = e.target.value
                                    setConfig({ ...config, chapters: updated })
                                  }}
                                  className="w-full bg-transparent font-medium text-gray-900 dark:text-white border-b border-transparent focus:border-slate-500 focus:outline-none px-1 py-0.5"
                                  placeholder="Título da Seção"
                                />
                                <textarea
                                  value={section.purpose || section.objective || section.content_directive || ''}
                                  onChange={(e) => {
                                    const updated = [...config.chapters]
                                    updated[selectedChapterIdx].sections![sIdx].purpose = e.target.value
                                    
                                    // Sync other fields if needed, but 'purpose' is primary
                                    if (updated[selectedChapterIdx].sections![sIdx].objective) {
                                      updated[selectedChapterIdx].sections![sIdx].objective = e.target.value
                                    }
                                    setConfig({ ...config, chapters: updated })
                                  }}
                                  rows={1}
                                  className="w-full text-sm text-gray-600 dark:text-gray-400 bg-transparent border-none focus:ring-0 px-1 py-0 resize-none"
                                  placeholder="Objetivo da seção..."
                                />
                                {/* Tamanho mínimo e código fonte — visíveis na aba Capítulos */}
                                <div className="flex flex-wrap items-center gap-4 mt-2 text-xs">
                                  <label className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
                                    <span>Mín. palavras:</span>
                                    <input
                                      type="number"
                                      min={0}
                                      step={50}
                                      placeholder="—"
                                      value={section.min_text_length ?? ''}
                                      onChange={(e) => {
                                        const v = e.target.value
                                        const num = v === '' ? undefined : Math.max(0, parseInt(v, 10) || 0)
                                        const updated = [...config.chapters]
                                        updated[selectedChapterIdx].sections![sIdx].min_text_length = num
                                        setConfig({ ...config, chapters: updated })
                                      }}
                                      className="w-20 px-2 py-1 rounded border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
                                    />
                                  </label>
                                  <label className="flex items-center gap-2 cursor-pointer text-gray-500 dark:text-gray-400">
                                    <input
                                      type="checkbox"
                                      checked={section.has_source_code ?? false}
                                      onChange={(e) => {
                                        const updated = [...config.chapters]
                                        updated[selectedChapterIdx].sections![sIdx].has_source_code = e.target.checked
                                        setConfig({ ...config, chapters: updated })
                                      }}
                                      className="rounded border-gray-300 dark:border-gray-600 text-slate-600 focus:ring-slate-500"
                                    />
                                    <span>Tem código fonte</span>
                                  </label>
                                </div>
                              </div>
                            </div>

                            {/* Editors Grid */}
                            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 pl-9">
                              {/* 1. Content Editor */}
                              <div className="space-y-2">
                                <label className="text-xs font-semibold text-gray-500 uppercase flex justify-between items-center">
                                  <span>Conteúdo (Texto)</span>
                                  <button
                                    onClick={() => handleGeneratePrompt(selectedChapterIdx, sIdx)}
                                    className="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded hover:bg-purple-200 flex items-center gap-1"
                                    disabled={isGeneratingPrompt}
                                  >
                                    {isGeneratingPrompt ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                                    Mágica
                                  </button>
                                </label>
                                <textarea
                                  value={section.content || ''}
                                  onChange={(e) => {
                                    const updated = [...config.chapters]
                                    updated[selectedChapterIdx].sections![sIdx].content = e.target.value
                                    setConfig({ ...config, chapters: updated })
                                  }}
                                  rows={6}
                                  className="w-full text-sm border rounded-lg p-2 dark:bg-gray-800 dark:border-gray-600"
                                  placeholder="Escreva ou gere o conteúdo aqui..."
                                />
                              </div>

                              {/* 2. Images Editor */}
                              <div className="space-y-2">
                                <label className="text-xs font-semibold text-gray-500 uppercase">Imagens ({section.images?.length || 0})</label>
                                <div className="border rounded-lg p-2 dark:border-gray-600 bg-white dark:bg-gray-800 space-y-2">
                                  {/* Image List */}
                                  <div className="flex gap-2 overflow-x-auto pb-2">
                                    {(section.images || []).map((img, i) => (
                                      <div key={i} className="relative flex-shrink-0 w-16 h-16 rounded overflow-hidden border">
                                        <img src={img.path} alt={(img as { caption?: string }).caption || `Imagem ${i + 1} da seção`} role="img" className="w-full h-full object-cover" />
                                      </div>
                                    ))}
                                    <button className="flex-shrink-0 w-16 h-16 border border-dashed rounded flex items-center justify-center text-gray-400 hover:bg-gray-50">
                                      <Plus className="w-4 h-4" />
                                    </button>
                                  </div>
                                  {/* Generator */}
                                  <div className="flex gap-2">
                                    <select
                                      className="text-xs border rounded p-1 max-w-[100px]"
                                      onChange={(e) => {
                                        // Optional: Handle local state or reuse existing
                                      }}
                                      id={`img-style-${selectedChapterIdx}-${sIdx}`}
                                    >
                                      <option value="">Estilo...</option>
                                      {IMAGE_STYLES.slice(0, 10).map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                                    </select>
                                    <input
                                      type="text"
                                      placeholder="Prompt imagem..."
                                      className="flex-1 text-xs border rounded p-1"
                                      id={`img-prompt-${selectedChapterIdx}-${sIdx}`}
                                    />
                                    <button
                                      onClick={() => {
                                        const input = document.getElementById(`img-prompt-${selectedChapterIdx}-${sIdx}`) as HTMLInputElement
                                        const styleSelect = document.getElementById(`img-style-${selectedChapterIdx}-${sIdx}`) as HTMLSelectElement
                                        const selectedStyle = styleSelect.value
                                        const styleName = selectedStyle
                                          ? (IMAGE_STYLES.find(s => s.id === selectedStyle)?.name || selectedStyle)
                                          : ''
                                        handleGenerateSectionImage(
                                          selectedChapterIdx,
                                          sIdx,
                                          input.value,
                                          styleName ? [styleName] : []
                                        )
                                      }}
                                      className="p-1 bg-slate-100 text-slate-700 rounded hover:bg-slate-200"
                                    >
                                      <Wand2 className="w-3 h-3" />
                                    </button>
                                  </div>
                                </div>
                              </div>

                              {/* 3. Code Blocks */}
                              <div className="space-y-2 col-span-1 lg:col-span-2">
                                <label className="text-xs font-semibold text-gray-500 uppercase flex justify-between">
                                  <span>Blocos de Código</span>
                                  <button
                                    onClick={() => {
                                      const updated = [...config.chapters]
                                      const currentBlocks = updated[selectedChapterIdx].sections![editingSectionIdx].code_blocks || []
                                      updated[selectedChapterIdx].sections![editingSectionIdx].code_blocks = [
                                        ...currentBlocks,
                                        { language: 'python', title: 'Novo Snippet', content: '# código aqui' }
                                      ]
                                      setConfig({ ...config, chapters: updated })
                                    }}
                                    className="text-xs text-blue-600 hover:underline"
                                  >
                                    + Adicionar
                                  </button>
                                </label>
                                {(section.code_blocks || []).length > 0 && (
                                  <div className="grid grid-cols-1 gap-2">
                                    {section.code_blocks!.map((block, bIdx) => (
                                      <div key={bIdx} className="bg-gray-900 rounded p-2 text-xs text-gray-300 font-mono">
                                        <div className="flex justify-between border-b border-gray-700 pb-1 mb-1">
                                          <span>{block.title} ({block.language})</span>
                                          <button onClick={() => {
                                            const updated = [...config.chapters]
                                            updated[selectedChapterIdx].sections![editingSectionIdx].code_blocks = updated[selectedChapterIdx].sections![editingSectionIdx].code_blocks!.filter((_, i) => i !== bIdx)
                                            setConfig({ ...config, chapters: updated })
                                          }} className="text-red-400 hover:text-red-300">x</button>
                                        </div>
                                        <textarea
                                          value={block.content}
                                          onChange={(e) => {
                                            const updated = [...config.chapters]
                                            updated[selectedChapterIdx].sections![editingSectionIdx].code_blocks![bIdx].content = e.target.value
                                            setConfig({ ...config, chapters: updated })
                                          }}
                                          className="w-full bg-transparent resize-y h-16 focus:outline-none"
                                        />
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </div>

                              {/* 4. Questões de Estudo */}
                              <div className="space-y-3 col-span-1 lg:col-span-2">
                                <label className="text-xs font-semibold text-gray-500 uppercase flex items-center gap-2">
                                  <HelpCircle className="w-3.5 h-3.5" />
                                  <span>Questões de Estudo</span>
                                </label>

                                {/* Question Style Selector */}
                                <QuestionStyleSelector
                                  compact
                                  config={{
                                    boardId: section.question_board || DEFAULT_QUESTION_CONFIG.boardId,
                                    questionType: section.question_type || DEFAULT_QUESTION_CONFIG.questionType,
                                    difficulty: section.question_difficulty || DEFAULT_QUESTION_CONFIG.difficulty,
                                    numQuestions: section.num_questions || DEFAULT_QUESTION_CONFIG.numQuestions,
                                    includeAnswers: section.question_include_answers !== false,
                                    includeExplanation: section.question_include_explanation !== false,
                                  }}
                                  onChange={(qCfg: QuestionConfig) => {
                                    const updated = [...config.chapters]
                                    const s = updated[selectedChapterIdx].sections![sIdx]
                                    s.question_board = qCfg.boardId
                                    s.question_type = qCfg.questionType
                                    s.question_difficulty = qCfg.difficulty
                                    s.num_questions = qCfg.numQuestions
                                    s.question_include_answers = qCfg.includeAnswers
                                    s.question_include_explanation = qCfg.includeExplanation
                                    setConfig({ ...config, chapters: updated })
                                  }}
                                />

                                <div className="flex items-center gap-2">
                                  <button
                                    onClick={() => handleGenerateQuestions(selectedChapterIdx, sIdx)}
                                    disabled={isGeneratingQuestions || !(section.content?.trim())}
                                    className="flex items-center gap-1 px-3 py-1.5 bg-emerald-100 text-emerald-700 rounded-lg text-xs hover:bg-emerald-200 disabled:opacity-50"
                                    title={!(section.content?.trim()) ? 'Preencha o conteúdo da seção antes de gerar questões' : `Gerar ${section.num_questions || 5} questões`}
                                  >
                                    {isGeneratingQuestions ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                                    Gerar Questões
                                  </button>
                                  {section.questions && (
                                    <button
                                      onClick={() => {
                                        const updated = [...config.chapters]
                                        updated[selectedChapterIdx].sections![sIdx].questions = ''
                                        setConfig({ ...config, chapters: updated })
                                      }}
                                      className="p-1.5 text-gray-400 hover:text-red-600 rounded"
                                      title="Limpar questões"
                                    >
                                      <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                  )}
                                </div>
                                {section.questions && (
                                  <textarea
                                    value={section.questions}
                                    onChange={(e) => {
                                      const updated = [...config.chapters]
                                      updated[selectedChapterIdx].sections![sIdx].questions = e.target.value
                                      setConfig({ ...config, chapters: updated })
                                    }}
                                    rows={10}
                                    className="w-full text-sm border rounded-lg p-2 dark:bg-gray-800 dark:border-gray-600 font-mono"
                                    placeholder="As questões geradas aparecerão aqui..."
                                  />
                                )}
                              </div>
                            </div>

                          </div>
                        ))
                      )}
                    </div>
                  </>
                ) : (
                  <div className="flex items-center justify-center h-full text-gray-400">
                    Selecione um capítulo
                  </div>
                )}
              </div>
            </div>

            <div className="flex flex-col sm:flex-row gap-3">
              <button
                type="button"
                onClick={handleSavePlanningProgress}
                disabled={isSavingPlanning}
                className="sm:flex-1 py-3 px-4 border border-slate-300 dark:border-slate-600 rounded-lg font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800/50 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {isSavingPlanning ? <Loader2 className="w-5 h-5 animate-spin" /> : <BookMarked className="w-5 h-5" />}
                Salvar planejamento (continuar)
              </button>
              <button
                type="button"
                onClick={handleSavePlanningAndExit}
                disabled={isSavingPlanning}
                className="sm:flex-1 py-3 px-4 border-2 border-slate-300 dark:border-slate-600 rounded-lg font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800/50 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {isSavingPlanning ? <Loader2 className="w-5 h-5 animate-spin" /> : <BookMarked className="w-5 h-5" />}
                Salvar planejamento e sair
              </button>
              <button
                type="button"
                onClick={() => setStep('design')}
                className="sm:flex-[1.4] py-4 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2 hover:from-slate-700 hover:to-slate-800"
              >
                <Palette className="w-5 h-5" />
                Continuar para Design
              </button>
            </div>
          </div>
        )
      }

      {/* Step 3: Design Studio (Renamed to 4 in logic but kept id 'design') */}
      {
        step === 'design' && (
          <div className="space-y-6">
            {/* Design Studio Header */}
            <div className="bg-gradient-to-r from-purple-500 to-indigo-600 rounded-xl p-6 text-white">
              <div className="flex items-center gap-3 mb-2">
                <Palette className="w-8 h-8" />
                <h2 className="text-2xl font-bold">🎨 Estúdio de Design do Livro</h2>
              </div>
              <p className="opacity-90">
                Crie o design completo do seu livro: Capas e divisores de capítulo.
              </p>
            </div>

            <UnifiedChat
              title="Chat das Capas"
              description="Planeje e gere capas com comandos rápidos."
              contextHint={`Aba: ${designTab === 'front' ? 'Capa Frontal' : designTab === 'back' ? 'Capa Traseira' : 'Divisores'}`}
              tools={coverChatTools}
              placeholder="Ex: /planejar-capa-frontal ou /gerar-capa-traseira"
              useAgent={true}
              agentContext={{ modelName: modelConfig.getDefaultTextModel('full') }}
              agentInstructions="Use as ferramentas para planejar e gerar capas do livro."
              agentMetadata={`Livro: ${config.title || config.topic || ''}\nSubtítulo: ${config.subtitle || ''}\nAutor: ${config.authorName || ''}\nAba: ${designTab}`}
              imageModels={modelConfig.getImageModelsForSelect()}
              defaultImageModel={coverModel}
              imageJobId={config.id || bookId || undefined}
            />

            {/* Design Tabs */}
            <div className="flex border-b border-gray-200 dark:border-gray-700">
              {[
                { id: 'front', label: '🖼️ Capa Frontal', icon: Image },
                { id: 'back', label: '📑 Capa Traseira', icon: BookMarked },
                { id: 'chapters', label: '📖 Divisores de Capítulo', icon: BookOpen }
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setDesignTab(tab.id as typeof designTab)}
                  className={cn(
                    'flex items-center gap-2 px-6 py-3 border-b-2 transition-colors font-medium',
                    designTab === tab.id
                      ? 'border-purple-600 text-purple-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700'
                  )}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {/* Style Selection (shared across tabs) */}
            <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
              <h3 className="font-semibold text-gray-900 dark:text-white mb-3">
                Estilos de Imagem (máx. 10)
              </h3>
              <StyleGrid
                selectedStyles={coverConfig.selectedStyles}
                onChange={(styles: string[]) => setCoverConfig({ ...coverConfig, selectedStyles: styles })}
                maxSelection={10}
                showSearch={true}
                showCategoryFilter={true}
                columns={4}
                cardHeight="160px"
                defaultCategory="all"
              />
            </div>

            <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6 space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-gray-900 dark:text-white">
                  Inspirações de Capas Best-sellers
                </h3>
                {loadingCoverDesigners && (
                  <span className="text-xs text-gray-400">Carregando...</span>
                )}
              </div>
              <MultiSelect
                label="Designers inspirados nos mais vendidos"
                options={coverDesignerOptions}
                selected={coverConfig.selectedDesigners}
                onChange={(values) => setCoverConfig({ ...coverConfig, selectedDesigners: values })}
                placeholder="Buscar best-sellers..."
              />
              {coverConfig.selectedDesigners.length > 0 && (
                <p className="text-xs text-gray-500">
                  Essas inspirações serão anexadas ao prompt de geração.
                </p>
              )}
            </div>

            {/* Model Selection for Cover */}
            <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-gray-900 dark:text-white">
                  🤖 Modelo de Geração de Imagem
                </h3>
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-3">
                Selecione o modelo de IA para gerar as capas e imagens de capítulos.
              </p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {modelConfig.getImageModelsForSelect().map((model: { id: string; name: string }) => (
                  <button
                    key={model.id}
                    onClick={() => setCoverModel(model.id)}
                    className={cn(
                      'px-4 py-3 rounded-lg text-sm font-medium border transition-all text-left',
                      coverModel === model.id
                        ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 ring-2 ring-purple-500/30'
                        : 'border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:border-purple-300'
                    )}
                  >
                    <span className="block">{model.name}</span>
                    {coverModel === model.id && (
                      <span className="text-xs text-purple-500 dark:text-purple-400">✓ Selecionado</span>
                    )}
                  </button>
                ))}
              </div>
            </div>

            {/* Front Cover Tab */}
            {designTab === 'front' && (
              <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
                  🖼️ Capa Frontal
                </h3>

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  {/* Controls */}
                  <div className="space-y-4">
                    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                      <label className="block text-xs font-semibold text-gray-500 uppercase mb-2">
                        Modelo de Imagem
                      </label>
                      <select
                        value={coverModel}
                        onChange={(e) => setCoverModel(e.target.value)}
                        className="w-full rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-100"
                      >
                        {modelConfig.getImageModelsForSelect().map((model: { id: string; name: string }) => (
                          <option key={model.id} value={model.id}>
                            {model.name}
                          </option>
                        ))}
                      </select>
                    </div>

                    <button
                      onClick={() => handlePlanCover('front')}
                      disabled={isGeneratingCover}
                      className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-purple-100 text-purple-700 rounded-lg hover:bg-purple-200 disabled:opacity-50"
                    >
                      {isGeneratingCover ? (
                        <Loader2 className="w-5 h-5 animate-spin" />
                      ) : (
                        <Wand2 className="w-5 h-5" />
                      )}
                      🪄 Planejar Capa Frontal
                    </button>

                    <button
                      onClick={handleBestSellerPrompt}
                      className="w-full flex items-center justify-center gap-2 px-4 py-3 border border-emerald-200 text-emerald-700 rounded-lg hover:bg-emerald-50"
                    >
                      <Sparkles className="w-5 h-5" />
                      Gerar prompt com best-sellers
                    </button>

                    {coverConfig.frontPrompt && (
                      <>
                        <div>
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            Ajuste o prompt:
                          </label>
                          <textarea
                            value={coverConfig.frontPrompt}
                            onChange={(e) => setCoverConfig({ ...coverConfig, frontPrompt: e.target.value })}
                            rows={4}
                            className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                          />
                        </div>

                        <button
                          onClick={() => handleGenerateCover('front')}
                          disabled={isGeneratingCover}
                          className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-gradient-to-r from-purple-600 to-indigo-600 text-white rounded-lg hover:from-purple-700 hover:to-indigo-700 disabled:opacity-50"
                        >
                          {isGeneratingCover ? (
                            <Loader2 className="w-5 h-5 animate-spin" />
                          ) : (
                            <Sparkles className="w-5 h-5" />
                          )}
                          🎨 Gerar Capa Frontal
                        </button>
                      </>
                    )}
                  </div>

                  {/* Preview */}
                  <div className="flex items-center justify-center">
                    {coverConfig.frontImagePath ? (
                      <div className="relative">
                        <img
                          src={buildFileUrl(coverConfig.frontImagePath)}
                          alt="Capa frontal"
                          className="w-64 h-96 object-contain rounded-lg shadow-xl bg-white"
                        />
                        <span className="absolute -top-2 -right-2 px-2 py-1 bg-slate-600 text-white text-xs rounded-full">
                          ✓ Gerada
                        </span>
                      </div>
                    ) : (
                      <div className="w-64 h-96 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg flex items-center justify-center">
                        <div className="text-center text-gray-400">
                          <Image className="w-12 h-12 mx-auto mb-2" />
                          <p>Preview da Capa</p>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* Back Cover Tab */}
            {designTab === 'back' && (
              <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
                  📑 Capa Traseira
                </h3>

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  {/* Controls */}
                  <div className="space-y-4">
                    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                      <label className="block text-xs font-semibold text-gray-500 uppercase mb-2">
                        Modelo de Imagem
                      </label>
                      <select
                        value={coverModel}
                        onChange={(e) => setCoverModel(e.target.value)}
                        className="w-full rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-100"
                      >
                        {modelConfig.getImageModelsForSelect().map((model: { id: string; name: string }) => (
                          <option key={model.id} value={model.id}>
                            {model.name}
                          </option>
                        ))}
                      </select>
                    </div>

                    <button
                      onClick={() => handlePlanCover('back')}
                      disabled={isGeneratingCover}
                      className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-purple-100 text-purple-700 rounded-lg hover:bg-purple-200 disabled:opacity-50"
                    >
                      {isGeneratingCover ? (
                        <Loader2 className="w-5 h-5 animate-spin" />
                      ) : (
                        <Wand2 className="w-5 h-5" />
                      )}
                      🪄 Planejar Capa Traseira
                    </button>

                    {coverConfig.backPrompt && (
                      <>
                        <div>
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            Ajuste o prompt:
                          </label>
                          <textarea
                            value={coverConfig.backPrompt}
                            onChange={(e) => setCoverConfig({ ...coverConfig, backPrompt: e.target.value })}
                            rows={4}
                            className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                          />
                        </div>

                        <button
                          onClick={() => handleGenerateCover('back')}
                          disabled={isGeneratingCover}
                          className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-gradient-to-r from-purple-600 to-indigo-600 text-white rounded-lg hover:from-purple-700 hover:to-indigo-700 disabled:opacity-50"
                        >
                          {isGeneratingCover ? (
                            <Loader2 className="w-5 h-5 animate-spin" />
                          ) : (
                            <Sparkles className="w-5 h-5" />
                          )}
                          🎨 Gerar Capa Traseira
                        </button>
                      </>
                    )}
                  </div>

                  {/* Preview */}
                  <div className="flex items-center justify-center">
                    {coverConfig.backImagePath ? (
                      <div className="relative">
                        <img
                          src={buildFileUrl(coverConfig.backImagePath)}
                          alt="Contracapa"
                          className="w-64 h-96 object-contain rounded-lg shadow-xl bg-white"
                        />
                        <span className="absolute -top-2 -right-2 px-2 py-1 bg-slate-600 text-white text-xs rounded-full">
                          ✓ Gerada
                        </span>
                      </div>
                    ) : (
                      <div className="w-64 h-96 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg flex items-center justify-center">
                        <div className="text-center text-gray-400">
                          <BookMarked className="w-12 h-12 mx-auto mb-2" />
                          <p>Preview da Contracapa</p>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* Chapter Dividers Tab */}
            {designTab === 'chapters' && (
              <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
                  📖 Divisores de Capítulo
                </h3>

                {config.chapters.length === 0 ? (
                  <div className="text-center py-8 text-gray-500">
                    <BookOpen className="w-12 h-12 mx-auto mb-2" />
                    <p>Planeje os capítulos primeiro.</p>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    {/* Chapter Selector */}
                    <div className="space-y-4">
                      <div>
                        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                          Selecione o Capítulo
                        </label>
                        <select
                          value={selectedChapterIdx}
                          onChange={(e) => setSelectedChapterIdx(parseInt(e.target.value))}
                          className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
                        >
                          {config.chapters.map((ch, idx) => (
                            <option key={ch.id} value={idx}>
                              Cap. {idx + 1}: {ch.title}
                            </option>
                          ))}
                        </select>
                      </div>

                      <button
                        onClick={() => handlePlanChapterCover(selectedChapterIdx)}
                        disabled={isGeneratingChapterCover}
                        className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-purple-100 text-purple-700 rounded-lg hover:bg-purple-200 disabled:opacity-50"
                      >
                        {isGeneratingChapterCover ? (
                          <Loader2 className="w-5 h-5 animate-spin" />
                        ) : (
                          <Wand2 className="w-5 h-5" />
                        )}
                        🪄 Planejar Divisor
                      </button>

                      {config.chapters[selectedChapterIdx]?.coverPrompt && (
                        <>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Ajuste o prompt:
                            </label>
                            <textarea
                              value={config.chapters[selectedChapterIdx].coverPrompt || ''}
                              onChange={(e) => updateChapter(config.chapters[selectedChapterIdx].id, { coverPrompt: e.target.value })}
                              rows={3}
                              className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                            />
                          </div>

                          <button
                            onClick={() => handleGenerateChapterCover(selectedChapterIdx)}
                            disabled={isGeneratingChapterCover}
                            className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-gradient-to-r from-purple-600 to-indigo-600 text-white rounded-lg hover:from-purple-700 hover:to-indigo-700 disabled:opacity-50"
                          >
                            {isGeneratingChapterCover ? (
                              <Loader2 className="w-5 h-5 animate-spin" />
                            ) : (
                              <Sparkles className="w-5 h-5" />
                            )}
                            🎨 Gerar Divisor Cap. {selectedChapterIdx + 1}
                          </button>
                        </>
                      )}
                    </div>

                    {/* Preview */}
                    <div className="flex items-center justify-center">
                      {config.chapters[selectedChapterIdx]?.coverPath ? (
                        <div className="relative">
                          <div className="w-64 h-48 bg-gradient-to-br from-indigo-400 to-purple-500 rounded-lg shadow-xl flex items-center justify-center">
                            <div className="text-white text-center">
                              <p className="text-4xl font-bold mb-2">{selectedChapterIdx + 1}</p>
                              <p className="font-medium">{config.chapters[selectedChapterIdx].title}</p>
                            </div>
                          </div>
                          <span className="absolute -top-2 -right-2 px-2 py-1 bg-slate-600 text-white text-xs rounded-full">
                            ✓ Gerado
                          </span>
                        </div>
                      ) : (
                        <div className="w-64 h-48 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg flex items-center justify-center">
                          <div className="text-center text-gray-400">
                            <BookOpen className="w-12 h-12 mx-auto mb-2" />
                            <p>Preview do Divisor</p>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Navigation */}
            <div className="flex gap-4">
              <button
                onClick={() => setStep('structure')}
                className="flex-1 py-3 border rounded-lg hover:bg-gray-50"
              >
                ← Voltar para Estrutura
              </button>
              <button
                onClick={() => setStep('content')}
                className="flex-1 py-3 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2"
              >
                <Edit3 className="w-5 h-5" />
                Editar Conteúdo →
              </button>
            </div>
          </div>
        )
      }

      {/* Step 4: Content Editor (Sections) */}
      {
        step === 'content' && editingChapterIdx !== null && config.chapters[editingChapterIdx] && (
          <div className="flex flex-col lg:flex-row gap-6 h-[calc(100vh-200px)] min-h-[600px]">
            {/* Left Sidebar: Section List */}
            <div className="w-full lg:w-1/4 bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 flex flex-col">
              <div className="p-4 border-b border-gray-200 dark:border-gray-700">
                <h3 className="font-semibold text-gray-900 dark:text-white truncate" title={config.chapters[editingChapterIdx].title}>
                  {config.chapters[editingChapterIdx].title}
                </h3>
                <p className="text-xs text-gray-500 mt-1">
                  {config.chapters[editingChapterIdx].sections?.length || 0} seções
                </p>
              </div>

              <div className="flex-1 overflow-y-auto p-2 space-y-2">
                {(config.chapters[editingChapterIdx].sections || []).map((sec, idx) => (
                  <button
                    key={idx}
                    onClick={() => setEditingSectionIdx(idx)}
                    className={cn(
                      "w-full text-left px-3 py-2 rounded-lg text-sm transition-colors flex items-center gap-2",
                      editingSectionIdx === idx
                        ? "bg-slate-100 text-slate-800 dark:bg-slate-900/40 dark:text-slate-300 font-medium"
                        : "hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-300"
                    )}
                  >
                    <span className="w-6 h-6 flex items-center justify-center bg-white/50 rounded-full text-xs">
                      {idx + 1}
                    </span>
                    <span className="truncate flex-1">{sec.title || 'Sem título'}</span>
                    {sec.content ? <CheckCircle className="w-3 h-3 text-slate-500" /> : <div className="w-3 h-3 rounded-full border border-gray-300" />}
                  </button>
                ))}

                <button
                  onClick={async () => {
                    setIsAddingBookSection(true)
                    try {
                      const chapterTitle = config.chapters[editingChapterIdx]?.title || ''
                      const currentSections = config.chapters[editingChapterIdx].sections || []
                      const sectionNum = currentSections.length + 1
                      const newTitle = `Seção ${sectionNum}`

                      // Auto-generate objective via AI
                      let generatedPurpose = ''
                      try {
                        const res = await api.post('/courses/lesson/generate', {
                          objective: `Gerar APENAS o objetivo pedagógico (1-2 frases curtas) para a seção ${sectionNum} do capítulo "${chapterTitle}" de um livro sobre "${config.title || ''}"`,
                          target: 'content',
                          style: 'Objetiva e Clara',
                          module_objective: chapterTitle,
                          author_styles: []
                        })
                        generatedPurpose = (res.data.content || res.data.text || '').trim()
                        const sentences = generatedPurpose.split(/(?<=[.!?])\s+/)
                        generatedPurpose = sentences.slice(0, 2).join(' ')
                      } catch {
                        console.warn('Falha ao gerar objetivo da seção do livro')
                      }

                      const newSec: Section = {
                        title: newTitle,
                        purpose: generatedPurpose,
                        content: '',
                        images: [],
                        code_blocks: [],
                        min_text_length: config.defaultMinTextLength,
                        has_source_code: config.defaultHasSourceCode ?? false
                      }
                      const updatedChapters = [...config.chapters]
                      updatedChapters[editingChapterIdx].sections = [...currentSections, newSec]
                      setConfig({ ...config, chapters: updatedChapters })
                      setEditingSectionIdx(currentSections.length)
                    } catch (err) {
                      console.error('Falha ao adicionar seção:', err)
                    } finally {
                      setIsAddingBookSection(false)
                    }
                  }}
                  disabled={isAddingBookSection}
                  className="w-full flex items-center justify-center gap-2 p-2 mt-2 border border-dashed border-gray-300 rounded-lg text-gray-500 hover:text-slate-600 hover:border-slate-400 text-sm disabled:opacity-50"
                >
                  {isAddingBookSection ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
                  {isAddingBookSection ? 'Criando Seção...' : 'Adicionar Seção'}
                </button>
              </div>

              <div className="p-4 border-t border-gray-200 dark:border-gray-700">
                <button
                  onClick={() => setEditingChapterIdx(null)}
                  className="w-full py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg text-sm hover:bg-gray-200"
                >
                  ← Voltar aos Capítulos
                </button>
              </div>
            </div>

            {/* Right Main: Section Editor */}
            <div className="flex-1 bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 flex flex-col overflow-hidden">
              {config.chapters[editingChapterIdx].sections && config.chapters[editingChapterIdx].sections![editingSectionIdx] ? (
                <>
                  <div className="flex items-center justify-between px-6 py-3 border-b border-gray-200 dark:border-gray-700 bg-white/70 dark:bg-gray-900/70">
                    <div className="text-sm text-gray-600 dark:text-gray-300">
                      Exportar EPUB do livro completo
                    </div>
                    <div className="flex items-center gap-2">
                      <EpubPreview
                        mode="section"
                        jobId={config.id || bookId || undefined}
                        apiKey={apiKey || undefined}
                        section={config.chapters[editingChapterIdx].sections![editingSectionIdx]}
                        chapterNumber={editingChapterIdx + 1}
                        sectionNumber={editingSectionIdx + 1}
                      />
                      <button
                        onClick={() => handleCompileEpub()}
                        disabled={isCompilingEpub}
                        className="px-4 py-2 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg text-sm flex items-center gap-2 disabled:opacity-50"
                      >
                        {isCompilingEpub ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileDown className="w-4 h-4" />}
                        Baixar EPUB
                      </button>
                      <button
                        onClick={() => handleCompileEpub('amazon_kdp')}
                        disabled={isCompilingEpub}
                        className="px-4 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm flex items-center gap-2 disabled:opacity-50"
                        title="Formato preparado para Amazon KDP (livro físico/e-book)"
                      >
                        {isCompilingEpub ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileDown className="w-4 h-4" />}
                        EPUB Amazon KDP
                      </button>
                    </div>
                  </div>
                  {/* Editor Tabs */}
                  <div className="flex border-b border-gray-200 dark:border-gray-700">
                    {[
                      { id: 'content', label: '📝 Conteúdo' },
                      { id: 'images', label: '🖼️ Imagens' },
                      { id: 'code', label: '💻 Códigos' },
                      { id: 'settings', label: '⚙️ Config' }
                    ].map(tab => (
                      <button
                        key={tab.id}
                        onClick={() => setSectionTab(tab.id as any)}
                        className={cn(
                          "px-6 py-3 text-sm font-medium border-b-2 transition-colors",
                          sectionTab === tab.id
                            ? "border-slate-500 text-slate-600 dark:text-slate-300"
                            : "border-transparent text-gray-500 hover:text-gray-700"
                        )}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>

                  {/* Tab Content */}
                  <div className="flex-1 overflow-y-auto p-6">

                    {/* === CONTENT TAB === */}
                    {sectionTab === 'content' && (
                      <div className="space-y-4">
                        <div className="grid grid-cols-2 gap-4">
                          <div>
                            <label className="text-xs font-semibold text-gray-500 uppercase">Título</label>
                            <input
                              value={config.chapters[editingChapterIdx].sections![editingSectionIdx].title}
                              onChange={e => {
                                const updated = [...config.chapters]
                                updated[editingChapterIdx].sections![editingSectionIdx].title = e.target.value
                                setConfig({ ...config, chapters: updated })
                              }}
                              className="w-full px-3 py-2 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
                            />
                          </div>
                          <div>
                            <label className="text-xs font-semibold text-gray-500 uppercase">Objetivo</label>
                            <input
                              value={config.chapters[editingChapterIdx].sections![editingSectionIdx].purpose}
                              onChange={e => {
                                const updated = [...config.chapters]
                                updated[editingChapterIdx].sections![editingSectionIdx].purpose = e.target.value
                                setConfig({ ...config, chapters: updated })
                              }}
                              className="w-full px-3 py-2 border rounded-lg dark:bg-gray-700 dark:border-gray-600"
                              placeholder="Ex: Introduzir o conceito X"
                            />
                          </div>
                        </div>

                        {/* AI Toolbar */}
                        <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg flex items-center gap-2 flex-wrap">
                          <Sparkles className="w-4 h-4 text-slate-500" />
                          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">IA Assistente:</span>
                          <button
                            onClick={() => handleGeneratePrompt(editingChapterIdx!, editingSectionIdx)}
                            disabled={isGeneratingPrompt}
                            className="px-3 py-1 bg-gradient-to-r from-purple-600 to-indigo-600 text-white border border-transparent rounded text-sm hover:from-purple-700 hover:to-indigo-700 flex items-center gap-1 shadow-sm"
                          >
                            {isGeneratingPrompt ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                            Mágica (Prompt + Texto)
                          </button>
                          <button
                            onClick={() => handleGenerateSectionContent(editingChapterIdx!, editingSectionIdx, 'rewrite')}
                            disabled={isGeneratingContent || !config.chapters[editingChapterIdx!].sections![editingSectionIdx].content}
                            className="px-3 py-1 bg-white dark:bg-gray-600 border border-gray-200 dark:border-gray-500 rounded text-sm hover:border-slate-500 disabled:opacity-50 flex items-center gap-1"
                          >
                            {isGeneratingContent ? <Loader2 className="w-3 h-3 animate-spin" /> : <span>🔄</span>}
                            Reescrever
                          </button>
                          <button
                            onClick={() => handleGenerateSectionContent(editingChapterIdx!, editingSectionIdx, 'expand')}
                            disabled={isGeneratingContent || !config.chapters[editingChapterIdx!].sections![editingSectionIdx].content}
                            className="px-3 py-1 bg-white dark:bg-gray-600 border border-gray-200 dark:border-gray-500 rounded text-sm hover:border-slate-500 disabled:opacity-50 flex items-center gap-1"
                          >
                            {isGeneratingContent ? <Loader2 className="w-3 h-3 animate-spin" /> : <span>📈</span>}
                            Expandir
                          </button>
                          <button
                            onClick={() => handlePlanEpubSection(editingChapterIdx!, editingSectionIdx)}
                            disabled={isPlanningEpub || !(config.chapters[editingChapterIdx!].sections![editingSectionIdx].content?.trim())}
                            className="px-3 py-1 bg-white dark:bg-gray-600 border border-gray-200 dark:border-gray-500 rounded text-sm hover:border-slate-500 disabled:opacity-50 flex items-center gap-1"
                            title="Posição das imagens: [IMAGE:1] = 1ª imagem, [IMAGE:2] = 2ª, ou [IMAGE: descrição] na ordem"
                          >
                            {isPlanningEpub ? <Loader2 className="w-3 h-3 animate-spin" /> : <span>🧭</span>}
                            Planejar EPUB
                          </button>
                        </div>

                        {/* Content Toolbar */}
                        <div className="flex items-center gap-1 p-1 bg-gray-50 dark:bg-gray-700/50 border border-b-0 rounded-t-lg">
                          <button
                            onClick={() => insertIntoChapterContent('**', '**')}
                            className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-700 dark:text-gray-300"
                            title="Negrito"
                          >
                            <Bold className="w-4 h-4" />
                          </button>
                          <button
                            onClick={() => insertIntoChapterContent('*', '*')}
                            className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-700 dark:text-gray-300"
                            title="Itálico"
                          >
                            <Italic className="w-4 h-4" />
                          </button>
                          <button
                            onClick={() => insertIntoChapterContent('\n## ', '')}
                            className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-700 dark:text-gray-300"
                            title="Título"
                          >
                            <Heading1 className="w-4 h-4" />
                          </button>
                          <div className="w-px h-4 bg-gray-300 dark:bg-gray-600 mx-1" />
                          <button
                            onClick={() => insertIntoChapterContent('\n- ', '')}
                            className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-700 dark:text-gray-300"
                            title="Lista"
                          >
                            <List className="w-4 h-4" />
                          </button>
                          <button
                            onClick={() => insertIntoChapterContent('\n> ', '')}
                            className="p-1.5 hover:bg-gray-200 dark:hover:bg-gray-600 rounded text-gray-700 dark:text-gray-300"
                            title="Citação"
                          >
                            <Quote className="w-4 h-4" />
                          </button>

                          <div className="flex-1" />

                          <button
                            onClick={() => setShowPreview(!showPreview)}
                            className={cn(
                              "px-3 py-1 text-xs font-medium rounded transition-colors flex items-center gap-1",
                              showPreview
                                ? "bg-slate-100 text-slate-700 dark:bg-slate-900/30 dark:text-slate-300"
                                : "hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-600 dark:text-gray-400"
                            )}
                          >
                            {showPreview ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                            {showPreview ? 'Editar' : 'Preview'}
                          </button>
                        </div>

                        {showPreview ? (
                          <div className="w-full h-[500px] p-6 border rounded-b-lg dark:bg-gray-900 dark:border-gray-700 overflow-y-auto prose dark:prose-invert max-w-none">
                            <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
                              {applyImageMarkersForPreview(
                                config.chapters[editingChapterIdx].sections![editingSectionIdx].content || '*Sem conteúdo*',
                                config.chapters[editingChapterIdx].sections![editingSectionIdx].images || [],
                                `Seção ${editingChapterIdx + 1}.${editingSectionIdx + 1}`
                              )}
                            </ReactMarkdown>
                          </div>
                        ) : (
                          <div className="border rounded-b-lg border-t-0 dark:border-gray-700 overflow-hidden">
                            <MarkdownField
                              ref={chapterContentRef}
                              value={config.chapters[editingChapterIdx].sections![editingSectionIdx].content}
                              onChange={v => {
                                const updated = [...config.chapters]
                                updated[editingChapterIdx].sections![editingSectionIdx].content = v
                                setConfig({ ...config, chapters: updated })
                                setChapterContent(v)
                              }}
                              placeholder="# Comece a escrever seu conteúdo... (use a barra para negrito, itálico, títulos)"
                              rows={20}
                              minHeight="500px"
                              showPreview={false}
                              className="font-mono text-sm [&_.focus-within]:ring-2 [&_.focus-within]:ring-slate-500"
                            />
                          </div>
                        )}
                      </div>
                    )}

                    {/* === IMAGES TAB === */}
                    {sectionTab === 'images' && (
                      <div className="space-y-6">
                        {(() => {
                          const sectionKey = `${editingChapterIdx}-${editingSectionIdx}`
                          const currentSectionImages = config.chapters[editingChapterIdx].sections![editingSectionIdx].images || []
                          const promptValue = sectionImagePrompts[sectionKey] || ''
                          const selectedStyles = sectionImageStyles[sectionKey] || []
                          const selectedModel =
                            sectionImageModels[sectionKey] || modelConfig.getDefaultImageModel(executionMode as any)
                          const availableModels = modelConfig.getImageModelsForSelect()

                          return (
                            <ImageAssetsPanel
                              title="Imagens da Seção"
                              countLabel={`${currentSectionImages.length} salvas`}
                              controls={(
                                <div className="space-y-4">
                                  <div className="rounded-xl border border-dashed border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-700/30 p-4">
                                    <ImageDropZone
                                      onImageInsert={(url) => {
                                        const newImg = {
                                          path: url,
                                          caption: '',
                                          source: 'upload' as const,
                                          uploaded_at: new Date().toISOString()
                                        }
                                        const updated = [...config.chapters]
                                        const currentImgs = updated[editingChapterIdx].sections![editingSectionIdx].images || []
                                        updated[editingChapterIdx].sections![editingSectionIdx].images = [...currentImgs, newImg]
                                        setConfig({ ...config, chapters: updated })
                                      }}
                                      jobId={config.chapters[editingChapterIdx].id}
                                    />
                                  </div>

                                  <div>
                                    <label className="text-xs font-semibold text-gray-500 uppercase">Estilos</label>
                                    <StyleGrid
                                      selectedStyles={selectedStyles}
                                      onChange={(styles: string[]) =>
                                        setSectionImageStyles(prev => ({
                                          ...prev,
                                          [sectionKey]: styles
                                        }))
                                      }
                                      maxSelection={4}
                                      showSearch={true}
                                      showCategoryFilter={true}
                                      columns={4}
                                      cardHeight="90px"
                                    />
                                  </div>

                                  <div className="grid gap-3 sm:grid-cols-2">
                                    <div className="flex flex-col gap-1">
                                      <label className="text-xs font-semibold text-gray-500 uppercase">Modelo</label>
                                      <select
                                        value={selectedModel}
                                        onChange={(e) =>
                                          setSectionImageModels(prev => ({
                                            ...prev,
                                            [sectionKey]: e.target.value
                                          }))
                                        }
                                        className="rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-100"
                                      >
                                        {availableModels.map((model) => (
                                          <option key={model.id} value={model.id}>
                                            {model.name}
                                          </option>
                                        ))}
                                      </select>
                                    </div>
                                    <div className="flex flex-col gap-1">
                                      <label className="text-xs font-semibold text-gray-500 uppercase">Prompt</label>
                                      <input
                                        type="text"
                                        placeholder="Prompt para imagem..."
                                        value={promptValue}
                                        onChange={(e) =>
                                          setSectionImagePrompts(prev => ({
                                            ...prev,
                                            [sectionKey]: e.target.value
                                          }))
                                        }
                                        className="rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-100"
                                      />
                                    </div>
                                  </div>

                                  <div className="flex justify-end">
                                    <button
                                      onClick={() => handleGenerateSectionImage(
                                        editingChapterIdx!,
                                        editingSectionIdx,
                                        promptValue,
                                        selectedStyles,
                                        selectedModel
                                      )}
                                      disabled={isGeneratingImage}
                                      className="px-4 py-2 bg-slate-700 text-white rounded-lg text-sm hover:bg-slate-800 flex items-center gap-2 disabled:opacity-50"
                                    >
                                      {isGeneratingImage ? <Loader2 className="w-4 h-4 animate-spin" /> : <Wand2 className="w-4 h-4" />}
                                      Gerar imagem
                                    </button>
                                  </div>
                                </div>
                              )}
                              savedImages={currentSectionImages.map((img, i) => ({
                                key: `${img.path}-${i}`,
                                src: img.path,
                                label: img.caption || `Imagem ${i + 1}`,
                                actions: (
                                  <div className="flex items-center gap-2">
                                    <button
                                      title="Inserir no texto"
                                      onClick={() => insertIntoChapterContent(`\n![${img.caption || 'image'}](${img.path})\n`)}
                                      className="p-2 bg-white rounded-full text-gray-900 border"
                                    >
                                      <FileDown className="w-4 h-4" />
                                    </button>
                                    <button
                                      className="p-2 bg-red-500 rounded-full text-white"
                                      onClick={() => {
                                        const updated = [...config.chapters]
                                        updated[editingChapterIdx].sections![editingSectionIdx].images.splice(i, 1)
                                        setConfig({ ...config, chapters: updated })
                                      }}
                                    >
                                      <Trash2 className="w-4 h-4" />
                                    </button>
                                  </div>
                                )
                              }))}
                            />
                          )
                        })()}
                      </div>
                    )}

                    {/* === CODE TAB === */}
                    {sectionTab === 'code' && (
                      <div className="space-y-4">
                        <div className="flex justify-between items-center">
                          <div>
                            <h4 className="font-semibold text-gray-900 dark:text-white">Blocos de Código e Diagramas</h4>
                            <p className="text-xs text-gray-500">Você também pode inserir blocos mermaid para diagram.</p>
                          </div>
                          <div className="flex gap-2">
                            <button
                              className="px-3 py-1.5 bg-gray-100 dark:bg-gray-700 rounded-lg text-sm hover:bg-gray-200 text-gray-700 flex items-center gap-1"
                              onClick={handleExtractSectionCodeBlocks}
                              title="Buscar blocos de código no texto"
                            >
                              <RefreshCw className="w-3 h-3" /> Extrair do Texto
                            </button>
                            <button
                              className="px-3 py-1.5 bg-gray-100 dark:bg-gray-700 rounded-lg text-sm hover:bg-gray-200 text-gray-700"
                              onClick={() => {
                                const newBlock = { language: 'python', title: 'Novo Snippet', content: '' }
                                const updated = [...config.chapters]
                                updated[editingChapterIdx].sections![editingSectionIdx].code_blocks =
                                  [...(updated[editingChapterIdx].sections![editingSectionIdx].code_blocks || []), newBlock]
                                setConfig({ ...config, chapters: updated })
                              }}
                            >
                              + Adicionar
                            </button>
                          </div>
                        </div>

                        {(config.chapters[editingChapterIdx].sections![editingSectionIdx].code_blocks || []).map((block, i) => (
                          <div key={i} className="border rounded-lg p-3 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                            <div className="flex items-center gap-2 mb-2">
                              <input
                                value={block.title}
                                onChange={e => {
                                  const updated = [...config.chapters]
                                  updated[editingChapterIdx].sections![editingSectionIdx].code_blocks[i].title = e.target.value
                                  setConfig({ ...config, chapters: updated })
                                }}
                                className="font-mono text-sm bg-transparent border-none focus:ring-0 p-0 font-bold"
                              />
                              <span className="text-gray-400">|</span>
                              <select
                                value={block.language}
                                onChange={e => {
                                  const updated = [...config.chapters]
                                  updated[editingChapterIdx].sections![editingSectionIdx].code_blocks[i].language = e.target.value
                                  setConfig({ ...config, chapters: updated })
                                }}
                                className="text-xs bg-transparent border-none"
                              >
                                <option value="python">Python</option>
                                <option value="javascript">JS</option>
                                <option value="typescript">TS</option>
                                <option value="bash">Bash</option>
                              </select>

                              <div className="ml-auto flex gap-1">
                                <button
                                  onClick={() => insertIntoChapterContent(`\n\`\`\`${block.language}\n${block.content}\n\`\`\`\n`)}
                                  className="p-1 hover:bg-slate-100 rounded" title="Inserir"
                                >
                                  <FileDown className="w-3 h-3 text-slate-600" />
                                </button>
                                <button
                                  onClick={() => {
                                    const updated = [...config.chapters]
                                    updated[editingChapterIdx].sections![editingSectionIdx].code_blocks.splice(i, 1)
                                    setConfig({ ...config, chapters: updated })
                                  }}
                                  className="p-1 hover:bg-red-100 rounded"
                                >
                                  <Trash2 className="w-3 h-3 text-red-500" />
                                </button>
                              </div>
                            </div>
                            <textarea
                              value={block.content}
                              onChange={e => {
                                const updated = [...config.chapters]
                                updated[editingChapterIdx].sections![editingSectionIdx].code_blocks[i].content = e.target.value
                                setConfig({ ...config, chapters: updated })
                              }}
                              className="w-full bg-transparent resize-y h-16 focus:outline-none"
                            />
                          </div>
                        ))}
                      </div>
                    )}

                    {/* === SETTINGS TAB === */}
                    {sectionTab === 'settings' && (
                      <div className="space-y-6">
                        <div className="grid grid-cols-3 gap-4">
                          <div className="p-4 bg-gray-50 dark:bg-gray-700/30 rounded-lg text-center">
                            <div className="text-2xl font-bold text-gray-900 dark:text-white">
                              {config.chapters[editingChapterIdx].sections![editingSectionIdx].content.split(/\s+/).filter(Boolean).length}
                            </div>
                            <div className="text-xs text-gray-500 uppercase">Palavras</div>
                          </div>
                          <div className="p-4 bg-gray-50 dark:bg-gray-700/30 rounded-lg text-center">
                            <div className="text-2xl font-bold text-gray-900 dark:text-white">
                              {config.chapters[editingChapterIdx].sections![editingSectionIdx].images?.length || 0}
                            </div>
                            <div className="text-xs text-gray-500 uppercase">Imagens</div>
                          </div>
                          <div className="p-4 bg-gray-50 dark:bg-gray-700/30 rounded-lg text-center">
                            <div className="text-2xl font-bold text-gray-900 dark:text-white">
                              {config.chapters[editingChapterIdx].sections![editingSectionIdx].code_blocks?.length || 0}
                            </div>
                            <div className="text-xs text-gray-500 uppercase">Blocos de Código</div>
                          </div>
                        </div>
                        <div className="border-t border-gray-200 dark:border-gray-700 pt-4 space-y-4">
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                            Tamanho mínimo do texto (palavras)
                          </label>
                          <input
                            type="number"
                            min={0}
                            step={50}
                            placeholder="Ex.: 400 (deixe vazio para usar padrão do livro)"
                            value={config.chapters[editingChapterIdx].sections![editingSectionIdx].min_text_length ?? ''}
                            onChange={(e) => {
                              const v = e.target.value
                              const num = v === '' ? undefined : Math.max(0, parseInt(v, 10) || 0)
                              const updated = [...config.chapters]
                              updated[editingChapterIdx].sections![editingSectionIdx].min_text_length = num
                              setConfig({ ...config, chapters: updated })
                            }}
                            className="w-full max-w-xs px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white"
                          />
                          <p className="text-xs text-gray-500 dark:text-gray-400">
                            Na geração com IA, o texto da seção terá pelo menos essa quantidade de palavras.
                          </p>
                          <label className="flex items-center gap-2 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={config.chapters[editingChapterIdx].sections![editingSectionIdx].has_source_code ?? false}
                              onChange={(e) => {
                                const updated = [...config.chapters]
                                updated[editingChapterIdx].sections![editingSectionIdx].has_source_code = e.target.checked
                                setConfig({ ...config, chapters: updated })
                              }}
                              className="rounded border-gray-300 dark:border-gray-600 text-slate-600 focus:ring-slate-500"
                            />
                            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                              Esta seção deve ter código fonte
                            </span>
                          </label>
                          <p className="text-xs text-gray-500 dark:text-gray-400">
                            Se marcado, os textos gerados para esta seção incluirão exemplos de código (blocos de código).
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                </>
              ) : (
                /* Fallback if no section selected but chapter selected */
                <div className="flex-1 flex flex-col items-center justify-center text-gray-400">
                  <BookOpen className="w-12 h-12 mb-4 opacity-50" />
                  <p>Selecione um capítulo para editar suas seções</p>
                </div>
              )}
            </div>
          </div>
        )
      }

      {/* Step 4b: Fallback for Chapter List Level (when only looking at list of chapters before editing) */}
      {
        step === 'content' && editingChapterIdx === null && (
          <div className="space-y-6">
            <div className="bg-gradient-to-r from-slate-600 to-slate-700 rounded-xl p-6 text-white">
              <div className="flex items-center gap-3 mb-2">
                <Edit3 className="w-8 h-8" />
                <h2 className="text-2xl font-bold">✏️ Editor de Conteúdo (Seções)</h2>
              </div>
              <p className="opacity-90">
                Selecione um capítulo para editar suas seções, imagens e códigos.
              </p>
            </div>

            <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-4">
              <div className="flex border-b border-gray-200 dark:border-gray-700 mb-4">
                {[
                  { id: 'chapters', label: '📚 Capítulos' },
                  { id: 'prologue', label: '✨ Prólogo' },
                  { id: 'acknowledgments', label: '🙏 Agradecimentos' }
                ].map(tab => (
                  <button
                    key={tab.id}
                    onClick={() => setChapterOverviewTab(tab.id as any)}
                    className={cn(
                      'px-4 py-2 text-sm font-medium border-b-2 transition-colors',
                      chapterOverviewTab === tab.id
                        ? 'border-slate-500 text-slate-600 dark:text-slate-300'
                        : 'border-transparent text-gray-500 hover:text-gray-700'
                    )}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>

              <div className="flex items-center justify-end gap-2 mb-4">
                <button
                  onClick={() => handleCompileEpub()}
                  disabled={isCompilingEpub}
                  className="px-4 py-2 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg text-sm flex items-center gap-2 disabled:opacity-50"
                >
                  {isCompilingEpub ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileDown className="w-4 h-4" />}
                  Baixar EPUB
                </button>
                <button
                  onClick={() => handleCompileEpub('amazon_kdp')}
                  disabled={isCompilingEpub}
                  className="px-4 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm flex items-center gap-2 disabled:opacity-50"
                  title="Formato preparado para Amazon KDP (livro físico/e-book)"
                >
                  {isCompilingEpub ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileDown className="w-4 h-4" />}
                  EPUB Amazon KDP
                </button>
              </div>

              {chapterOverviewTab === 'chapters' && (
                <>
                  <h3 className="font-semibold text-gray-900 dark:text-white mb-4">
                    Selecione um Capítulo
                  </h3>
                  <div className="space-y-2">
                    {config.chapters.map((chapter, idx) => (
                      <button
                        key={chapter.id}
                        onClick={() => {
                          setEditingChapterIdx(idx)
                          setEditingSectionIdx(0)
                          setChapterContent(chapter.content || '')
                        }}
                        className="w-full text-left p-4 rounded-lg border border-gray-200 dark:border-gray-700 hover:border-slate-500 transition-all flex items-center justify-between group"
                      >
                        <div className="flex items-center gap-3">
                          <span className="w-8 h-8 rounded-full bg-slate-100 text-slate-700 flex items-center justify-center font-bold text-sm">
                            {idx + 1}
                          </span>
                          <div>
                            <div className="font-medium text-gray-900 dark:text-white">{chapter.title}</div>
                            <div className="text-xs text-gray-500">{chapter.sections?.length || 0} seções</div>
                          </div>
                        </div>
                        <Edit3 className="w-4 h-4 text-gray-400 group-hover:text-slate-500" />
                      </button>
                    ))}
                  </div>
                </>
              )}

              {chapterOverviewTab === 'prologue' && (
                <div className="space-y-3">
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    Escreva um prólogo envolvente para abrir o livro. Você pode gerar com IA e ajustar manualmente. As alterações são atribuídas ao livro ao clicar em Salvar.
                  </p>
                  <textarea
                    value={config.prologue}
                    onChange={(e) => setConfig({ ...config, prologue: e.target.value })}
                    placeholder="Texto do prólogo..."
                    rows={20}
                    className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-y min-h-[22rem]"
                  />
                  <div className="flex flex-wrap gap-3">
                    <button
                      onClick={handleGeneratePrologue}
                      disabled={isGeneratingPrologue}
                      className="px-4 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 text-white rounded-lg text-sm flex items-center gap-2"
                    >
                      {isGeneratingPrologue ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                      Gerar prólogo com IA
                    </button>
                    <button
                      onClick={handleSaveFrontMatter}
                      disabled={isSavingFrontMatter}
                      className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm flex items-center gap-2 hover:bg-green-700"
                    >
                      {isSavingFrontMatter ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                      {isSavingFrontMatter ? 'Salvando...' : 'Salvar Prólogo'}
                    </button>
                  </div>
                </div>
              )}

              {chapterOverviewTab === 'acknowledgments' && (
                <div className="space-y-3">
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    Registre seus agradecimentos finais para incluir no EPUB. As alterações são atribuídas ao livro ao clicar em Salvar.
                  </p>
                  <textarea
                    value={config.acknowledgments}
                    onChange={(e) => setConfig({ ...config, acknowledgments: e.target.value })}
                    placeholder="Texto de agradecimentos..."
                    rows={20}
                    className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-y min-h-[22rem]"
                  />
                  <button
                    onClick={handleSaveFrontMatter}
                    disabled={isSavingFrontMatter}
                    className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm flex items-center gap-2 hover:bg-green-700"
                  >
                    {isSavingFrontMatter ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                    {isSavingFrontMatter ? 'Salvando...' : 'Salvar Agradecimentos'}
                  </button>
                </div>
              )}
            </div>

            {/* Navigation */}
            <div className="flex gap-4">
              <button
                onClick={() => setStep('design')}
                className="flex-1 py-3 border rounded-lg hover:bg-gray-50"
              >
                ← Voltar para Design
              </button>
              <button
                onClick={() => setStep('confirm')}
                className="flex-1 py-3 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2"
              >
                <CheckCircle className="w-5 h-5" />
                Revisar e Compilar
              </button>
            </div>
          </div>
        )
      }

      {/* Step 5: Confirm */}
      {
        step === 'confirm' && (
          <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6 space-y-6">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
              📕 Compilar Livro Completo
            </h2>

            <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
              <p className="text-sm text-blue-800 dark:text-blue-300">
                Aqui você configura os elementos finais e gera o EPUB completo do livro.
              </p>
            </div>

            {/* Prologue and Acknowledgments */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Prólogo
                </label>
                <textarea
                  value={config.prologue}
                  onChange={(e) => setConfig({ ...config, prologue: e.target.value })}
                  placeholder="Texto introdutório do livro..."
                  rows={5}
                  className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Agradecimentos
                </label>
                <textarea
                  value={config.acknowledgments}
                  onChange={(e) => setConfig({ ...config, acknowledgments: e.target.value })}
                  placeholder="Dedicatórias e agradecimentos..."
                  rows={5}
                  className="w-full px-4 py-3 border rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                />
              </div>
            </div>

            {/* Summary */}
            <div className="space-y-4 border-t pt-6">
              <h3 className="font-medium text-gray-900 dark:text-white">Resumo do Livro</h3>
              <div className="grid grid-cols-2 gap-4 text-sm">
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Título</span>
                  <span className="font-medium text-gray-900 dark:text-white">{config.title}</span>
                </div>
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Autor</span>
                  <span className="font-medium text-gray-900 dark:text-white">{config.authorName || '-'}</span>
                </div>
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Estilo</span>
                  <span className="font-medium text-gray-900 dark:text-white">
                    {BOOK_STYLES.find(s => s.id === config.style)?.name}
                  </span>
                </div>
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Estilo do Livro</span>
                  <span className="font-medium text-gray-900 dark:text-white">
                    {(config.bookStyle && config.bookStyle.length > 0)
                      ? config.bookStyle
                          .map((id) => BOOK_GENRES.find((g) => g.id === id)?.name || id)
                          .join(', ')
                      : '-'}
                  </span>
                </div>
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Diretrizes do Estilo</span>
                  <span className="font-medium text-gray-900 dark:text-white">
                    {config.bookStylePrompt || '-'}
                  </span>
                </div>
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Capítulos</span>
                  <span className="font-medium text-gray-900 dark:text-white">{config.chapters.length}</span>
                </div>
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Capa Frontal</span>
                  <span className={cn(
                    'font-medium',
                    coverConfig.frontImagePath ? 'text-slate-600' : 'text-gray-400'
                  )}>
                    {coverConfig.frontImagePath ? '✓ Gerada' : 'Não gerada'}
                  </span>
                </div>
                <div className="flex justify-between py-2 border-b">
                  <span className="text-gray-500">Capa Traseira</span>
                  <span className={cn(
                    'font-medium',
                    coverConfig.backImagePath ? 'text-slate-600' : 'text-gray-400'
                  )}>
                    {coverConfig.backImagePath ? '✓ Gerada' : 'Não gerada'}
                  </span>
                </div>
              </div>
            </div>

            {/* Actions */}
            <div className="space-y-4">
              <button
                type="button"
                onClick={handleSavePlanningProgress}
                disabled={isSavingPlanning}
                className="w-full py-2.5 border border-slate-300 dark:border-slate-600 rounded-lg text-sm font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800/50 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {isSavingPlanning ? <Loader2 className="w-4 h-4 animate-spin" /> : <BookMarked className="w-4 h-4" />}
                Salvar planejamento e continuar aqui
              </button>
              <button
                type="button"
                onClick={handleSavePlanningAndExit}
                disabled={isSavingPlanning}
                className="w-full py-2.5 border border-slate-300 dark:border-slate-600 rounded-lg text-sm font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800/50 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {isSavingPlanning ? <Loader2 className="w-4 h-4 animate-spin" /> : <BookMarked className="w-4 h-4" />}
                Salvar planejamento e ir à biblioteca
              </button>
              {/* Create Book Button */}
              <button
                onClick={handleCreateBook}
                disabled={isCreating || !config.title.trim() || !config.topic.trim()}
                className="w-full py-3 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2 disabled:opacity-50"
              >
                {isCreating ? (
                  <>
                    <Loader2 className="w-5 h-5 animate-spin" />
                    Criando Livro...
                  </>
                ) : (
                  <>
                    <BookOpen className="w-5 h-5" />
                    🚀 Criar Livro com IA
                  </>
                )}
              </button>

              {/* EPUB Compilation - Only show after book is created */}
              <div className="flex flex-wrap gap-4">
                <button
                  onClick={() => setStep('content')}
                  className="flex-1 min-w-[140px] py-3 border rounded-lg hover:bg-gray-50"
                >
                  ← Voltar para Edição
                </button>
                <button
                  onClick={() => handleCompileEpub()}
                  disabled={isCompilingEpub}
                  className="flex-1 min-w-[140px] py-3 bg-gradient-to-r from-slate-600 to-slate-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2"
                >
                  {isCompilingEpub ? (
                    <>
                      <Loader2 className="w-5 h-5 animate-spin" />
                      Compilando EPUB...
                    </>
                  ) : (
                    <>
                      <FileDown className="w-5 h-5" />
                      📚 Baixar EPUB
                    </>
                  )}
                </button>
                <button
                  onClick={() => handleCompileEpub('amazon_kdp')}
                  disabled={isCompilingEpub}
                  className="flex-1 min-w-[140px] py-3 bg-amber-600 hover:bg-amber-700 text-white rounded-lg font-semibold flex items-center justify-center gap-2"
                  title="Formato preparado para Amazon KDP (livro físico/e-book)"
                >
                  {isCompilingEpub ? (
                    <>
                      <Loader2 className="w-5 h-5 animate-spin" />
                      Compilando...
                    </>
                  ) : (
                    <>
                      <FileDown className="w-5 h-5" />
                      📚 EPUB Amazon KDP
                    </>
                  )}
                </button>
              </div>
            </div>

            {/* EPUB Download Success */}
            {epubDownloadUrl && (
              <div className="p-4 bg-slate-50 dark:bg-slate-900/20 border border-slate-200 dark:border-slate-800 rounded-lg">
                <p className="text-slate-700 dark:text-slate-300 flex items-center gap-2">
                  <CheckCircle className="w-5 h-5" />
                  EPUB gerado com sucesso! O download deve iniciar automaticamente.
                </p>
                <a
                  href={epubDownloadUrl}
                  download={`${config.title.replace(/\s+/g, '_')}.epub`}
                  className="text-sm text-slate-600 hover:underline mt-2 inline-block"
                >
                  Clique aqui se o download não iniciar
                </a>
              </div>
            )}

            {/* Error Message */}
            {error && (
              <div className="mt-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-400">
                <div className="flex items-start gap-3">
                  <div className="flex-shrink-0">
                    ⚠️
                  </div>
                  <div className="flex-1">
                    <p className="font-medium">{error}</p>
                    {error.includes('API Key não configurada') && (
                      <div className="mt-3 flex gap-2">
                        <button
                          onClick={() => navigate('/system')}
                          className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors text-sm font-medium"
                        >
                          🔑 Configurar API Keys
                        </button>
                        <button
                          onClick={() => setError(null)}
                          className="px-4 py-2 border border-red-300 dark:border-red-700 rounded-lg hover:bg-red-100 dark:hover:bg-red-900/40 transition-colors text-sm"
                        >
                          Fechar
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        )
      }
      {/* PROMPT GENERATION MODAL */}
      {showPromptModal && promptTarget && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-white dark:bg-gray-800 rounded-xl shadow-2xl max-w-2xl w-full flex flex-col max-h-[90vh]">
            <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
              <h3 className="text-lg font-bold text-gray-900 dark:text-white flex items-center gap-2">
                <Wand2 className="w-5 h-5 text-purple-600" />
                Gerador de Conteúdo com IA
              </h3>
              <button onClick={() => setShowPromptModal(false)} className="text-gray-400 hover:text-gray-600">
                ✕
              </button>
            </div>

            <div className="p-6 overflow-y-auto flex-1 space-y-4">
              <div className="bg-purple-50 dark:bg-purple-900/20 p-4 rounded-lg text-sm text-purple-800 dark:text-purple-300 border border-purple-100 dark:border-purple-800">
                <p>
                  <strong>Contexto:</strong> Capítulo {promptTarget.chapterIdx + 1}, Seção {promptTarget.sectionIdx + 1}.
                  <br />
                  O prompt abaixo foi construído automaticamente usando o <strong>Objetivo do Capítulo</strong> e seus <strong>Estilos de Autor</strong> escolhidos.
                  Você pode editá-lo antes de gerar.
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Prompt para a IA
                </label>
                <textarea
                  value={generatedPrompt}
                  onChange={(e) => setGeneratedPrompt(e.target.value)}
                  rows={8}
                  className="w-full p-4 border rounded-lg font-mono text-sm bg-gray-50 dark:bg-gray-900 dark:border-gray-700"
                />
              </div>
            </div>

            <div className="p-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-3 bg-gray-50 dark:bg-gray-800 rounded-b-xl">
              <button
                onClick={() => setShowPromptModal(false)}
                className="px-4 py-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
              >
                Cancelar
              </button>
              <button
                onClick={async () => {
                  await handleGenerateSectionContent(promptTarget.chapterIdx, promptTarget.sectionIdx, 'generate', generatedPrompt)
                  setShowPromptModal(false)
                }}
                disabled={isGeneratingContent || !generatedPrompt.trim()}
                className="px-6 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 text-white rounded-lg font-semibold flex items-center gap-2 hover:from-purple-700 hover:to-indigo-700 disabled:opacity-50 shadow-lg"
              >
                {isGeneratingContent ? <Loader2 className="w-5 h-5 animate-spin" /> : <Sparkles className="w-5 h-5" />}
                Gerar Texto Agora
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

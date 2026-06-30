/**
 * Book Library Page
 * 
 * Dedicated page for viewing and editing created books
 */

import { useState, useEffect, useRef } from 'react'
import { Book, Edit, Trash2, Eye, Loader2, RefreshCw, Calendar, User, Download, Upload, FileText, X, Languages, ChevronDown, ChevronRight, FileInput, Sparkles, Wrench, Search } from 'lucide-react'
import { api, endpoints } from '@/lib/api'
import { getStoredGeminiApiKey, fetchGoogleApiKeyIfMissing } from '@/lib/apiKeys'
import { useNavigate } from 'react-router-dom'
import { useBookCreateFromText } from '@/contexts/BookCreateFromTextContext'
import { LogViewer, LogEntry } from '@/components/LogViewer'
import { buildFileUrl } from '@/lib/files'
import { API_BASE_URL } from '@/lib/api'
import { bookLibraryEditPath } from '@/lib/bookRoutes'

interface BookData {
    id: string
    job_id?: string
    title: string
    subtitle?: string
    author?: string
    category?: string
    language?: string
    description?: string
    chapters: any[]
    total_chapters?: number
    created_at: number
    updated_at?: number
    /** ex.: planning_draft, planning_saved, completed */
    status?: string
    is_legacy?: boolean
    cover_path?: string
    /** Legado: capa armazenada com outro nome no banco */
    capa?: string
    cover_image?: string
}

/** URL da capa: sempre usa /books/{id}/cover que possui fallback completo (banco binário, file path, job state). */
function getBookCoverUrl(book: BookData): string {
    const id = book.id || book.job_id
    if (id) {
        const base = (API_BASE_URL || '').replace(/\/$/, '')
        return `${base}/books/${encodeURIComponent(id)}/cover`
    }
    const path = book.cover_path || book.capa || book.cover_image
    if (path && typeof path === 'string') return buildFileUrl(path)
    return ''
}

/** Capítulos com seções e subseções em painéis recolhíveis (subseções escondidas por padrão). */
function ChaptersSectionsSubsections({ chapters }: { chapters?: any[] }) {
    const [expandedChapterIdx, setExpandedChapterIdx] = useState<number | null>(null)
    const [expandedSectionKey, setExpandedSectionKey] = useState<string | null>(null)

    const list = chapters ?? []
    if (list.length === 0) return null

    return (
        <div>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
                Capítulos ({list.length})
            </h3>
            <div className="space-y-2">
                {list.map((chapter: any, chIdx: number) => {
                    const sections = chapter.sections ?? []
                    const isChapterExpanded = expandedChapterIdx === chIdx
                    return (
                        <div
                            key={chIdx}
                            className="rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700/50 overflow-hidden"
                        >
                            <button
                                type="button"
                                onClick={() => setExpandedChapterIdx((prev) => (prev === chIdx ? null : chIdx))}
                                className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-gray-100 dark:hover:bg-gray-700/70"
                            >
                                {isChapterExpanded ? (
                                    <ChevronDown className="w-4 h-4 shrink-0 text-gray-500" />
                                ) : (
                                    <ChevronRight className="w-4 h-4 shrink-0 text-gray-500" />
                                )}
                                <h4 className="font-medium text-gray-900 dark:text-white flex-1">
                                    Capítulo {chIdx + 1}: {chapter.title || chapter.chapter_title || 'Sem título'}
                                </h4>
                                {sections.length > 0 && (
                                    <span className="text-xs text-gray-500 dark:text-gray-400">
                                        {sections.length} seção(ões)
                                    </span>
                                )}
                            </button>
                            {chapter.description && (
                                <p className="text-sm text-gray-600 dark:text-gray-400 px-4 pb-2 pl-12">
                                    {chapter.description}
                                </p>
                            )}
                            {isChapterExpanded && sections.length > 0 && (
                                <div className="border-t border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800/50 px-4 pb-3 pl-10 space-y-2">
                                    {sections.map((section: any, secIdx: number) => {
                                        const secKey = `${chIdx}-${secIdx}`
                                        const subsections = section.subsections ?? []
                                        const isSectionExpanded = expandedSectionKey === secKey
                                        return (
                                            <div
                                                key={secIdx}
                                                className="rounded border border-gray-200 dark:border-gray-600 overflow-hidden"
                                            >
                                                <button
                                                    type="button"
                                                    onClick={() => setExpandedSectionKey((prev) => (prev === secKey ? null : secKey))}
                                                    className="w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-700/50"
                                                >
                                                    {isSectionExpanded ? (
                                                        <ChevronDown className="w-3.5 h-3.5 shrink-0 text-gray-500" />
                                                    ) : (
                                                        <ChevronRight className="w-3.5 h-3.5 shrink-0 text-gray-500" />
                                                    )}
                                                    <span className="font-medium text-gray-800 dark:text-gray-200 flex-1">
                                                        {section.title || `Seção ${secIdx + 1}`}
                                                    </span>
                                                    {subsections.length > 0 && (
                                                        <span className="text-xs text-gray-500">
                                                            Subseções ({subsections.length})
                                                        </span>
                                                    )}
                                                </button>
                                                {isSectionExpanded && subsections.length > 0 && (
                                                    <div className="border-t border-gray-100 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 px-3 py-2 pl-8 space-y-1">
                                                        {subsections.map((sub: any, subIdx: number) => (
                                                            <div
                                                                key={subIdx}
                                                                className="text-xs text-gray-600 dark:text-gray-400 py-1 border-b border-gray-100 dark:border-gray-700/50 last:border-0"
                                                            >
                                                                <span className="text-gray-500 dark:text-gray-500">{subIdx + 1}.</span>{' '}
                                                                {(sub.objective || sub.title || sub.content?.slice(0, 100) || '—').trim()}
                                                            </div>
                                                        ))}
                                                    </div>
                                                )}
                                            </div>
                                        )
                                    })}
                                </div>
                            )}
                        </div>
                    )
                })}
            </div>
        </div>
    )
}

export default function BookLibrary() {
    const navigate = useNavigate()
    const [books, setBooks] = useState<BookData[]>([])
    const [loading, setLoading] = useState(true)
    const [deletingAll, setDeletingAll] = useState(false)
    const [deletingId, setDeletingId] = useState<string | null>(null)
    const [exportingId, setExportingId] = useState<string | null>(null)
    const [generatingAllId, setGeneratingAllId] = useState<string | null>(null)
    const [repairingId, setRepairingId] = useState<string | null>(null)
    const [importing, setImporting] = useState(false)
    const fileInputRef = useRef<HTMLInputElement>(null)
    const [selectedBook, setSelectedBook] = useState<BookData | null>(null)
    const [selectedBookLoading, setSelectedBookLoading] = useState(false)
    const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')
    const [logs, setLogs] = useState<LogEntry[]>([])
    const [loadError, setLoadError] = useState<string | null>(null)
    const [copyLanguageBook, setCopyLanguageBook] = useState<BookData | null>(null)
    const [copyingId, setCopyingId] = useState<string | null>(null)
    const [translatingMismatchedId, setTranslatingMismatchedId] = useState<string | null>(null)
    const [perplexityEnrichSectionsId, setPerplexityEnrichSectionsId] = useState<string | null>(null)
    const [perplexityEnrichSubsectionsId, setPerplexityEnrichSubsectionsId] = useState<string | null>(null)
    const [perplexityWriteSectionsId, setPerplexityWriteSectionsId] = useState<string | null>(null)
    const [perplexityWriteSubsectionsId, setPerplexityWriteSubsectionsId] = useState<string | null>(null)
    const [showCreateFromText, setShowCreateFromText] = useState(false)
    const [createFromTextInput, setCreateFromTextInput] = useState('')
    const [isCreatingFromText, setIsCreatingFromText] = useState(false)
    const { setActive: setCreateFromTextActive } = useBookCreateFromText()

    const pushLog = (message: string, level: LogEntry['level'] = 'info') => {
        setLogs((prev) => ([
            ...prev,
            {
                timestamp: new Date().toISOString(),
                message,
                level
            }
        ]))
    }

    const closeBookModal = () => {
        setSelectedBook(null)
        setSelectedBookLoading(false)
    }

    const loadBooks = async () => {
        setLoading(true)
        setLoadError(null)
        try {
            const response = await api.get('/books', { params: { include_legacy: false, summary: true } })
            const payload = response.data
            const rawBooks = Array.isArray(payload) ? payload : (payload?.books || []) || []
            const filteredBooks = (rawBooks as BookData[]).filter((book) => !book.is_legacy)
            setBooks(filteredBooks)
        } catch (error) {
            const err = error as { response?: { status?: number; data?: { detail?: string } }; message?: string }
            const message = err?.response?.data?.detail ?? err?.message ?? 'Não foi possível carregar a biblioteca. Verifique se o servidor está rodando.'
            setLoadError(message)
            setBooks([])
            pushLog(`❌ Erro ao carregar livros: ${message}`, 'error')
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        loadBooks()
    }, [])

    const formatDate = (timestamp: number) => {
        return new Date(timestamp * 1000).toLocaleDateString('pt-BR', {
            day: '2-digit',
            month: 'short',
            year: 'numeric'
        })
    }

    const handleViewBook = async (book: BookData) => {
        const id = book.id || book.job_id
        if (!id) return
        setSelectedBook(book)
        setSelectedBookLoading(true)
        try {
            const res = await api.get<BookData>(`/books/${encodeURIComponent(id)}`)
            setSelectedBook(res.data)
        } catch (error) {
            const err = error as { response?: { data?: { detail?: string } }; message?: string }
            const message = err?.response?.data?.detail ?? err?.message ?? 'Não foi possível carregar o livro.'
            pushLog(`❌ ${message}`, 'error')
        } finally {
            setSelectedBookLoading(false)
        }
    }

    const handleEditBook = (book: BookData) => {
        const id = book.id || book.job_id
        if (!id) return
        navigate(bookLibraryEditPath(id, book.status))
    }

    const handleDeleteBook = async (book: BookData, bookId?: string) => {
        const id = bookId ?? book.id ?? book.job_id
        if (!id) return
        if (!window.confirm('Excluir este livro? A ação não pode ser desfeita.')) return
        setDeletingId(id)
        try {
            await api.delete(`/books/${encodeURIComponent(id)}`)
            setBooks((prev) => prev.filter((b) => (b.id || b.job_id) !== id && (b.title || '') !== (book.title || '')))
            if (selectedBook && (selectedBook.id === id || selectedBook.job_id === id || selectedBook.title === book.title)) {
                closeBookModal()
            }
            pushLog(`✅ Livro removido.`, 'success')
        } catch (error) {
            const err = error as { response?: { status?: number; data?: { detail?: string } } }
            const is404 = err?.response?.status === 404
            const title = (book.title || '').trim()
            if (is404 && title) {
                try {
                    await api.delete(`/books/${encodeURIComponent(title)}`)
                    setBooks((prev) => prev.filter((b) => (b.title || '').trim() !== title))
                    if (selectedBook?.title === book.title) closeBookModal()
                    pushLog(`✅ Livro removido.`, 'success')
                    return
                } catch (retryErr) {
                    console.error('Retry delete by title failed:', retryErr)
                }
            }
            console.error('Error deleting book:', error)
            const message = err?.response?.data?.detail || (error instanceof Error ? error.message : 'Falha ao excluir livro')
            pushLog(`❌ Erro ao excluir livro: ${message}`, 'error')
            alert(`Não foi possível excluir o livro: ${message}`)
        } finally {
            setDeletingId(null)
        }
    }

    const handleDeleteAllBooks = async () => {
        if (deletingAll) return
        if (!window.confirm('TEM CERTEZA? Isso apagará TODOS os livros da biblioteca. Esta ação é irreversível.')) {
            pushLog('Exclusão total cancelada pelo usuário.', 'warning')
            return
        }
        setDeletingAll(true)
        pushLog('🗑️ Iniciando exclusão de todos os livros...', 'info')
        try {
            const response = await api.delete('/books')
            const deletedCount = response?.data?.deleted
            pushLog(`✅ Exclusão concluída. Livros removidos: ${deletedCount ?? 'desconhecido'}.`, 'success')
            closeBookModal()
            await loadBooks()
            pushLog('📚 Lista atualizada após exclusão.', 'info')
        } catch (error) {
            console.error('Error deleting all books:', error)
            alert('Erro ao apagar biblioteca')
            const message = error instanceof Error ? error.message : 'Falha ao apagar biblioteca'
            pushLog(`❌ Erro ao apagar biblioteca: ${message}`, 'error')
            if (error instanceof Error && error.stack) {
                pushLog(error.stack, 'error')
            }
        } finally {
            setDeletingAll(false)
            pushLog('🧹 Processo de exclusão finalizado.', 'debug')
        }
    }

    const handleGenerateAllSectionTexts = async (bookId: string) => {
        if (!bookId) return
        setGeneratingAllId(bookId)
        try {
            const apiKey = getStoredGeminiApiKey()
            const res = await api.post(`/books/${bookId}/generate-all-section-texts`, { api_key: apiKey || undefined })
            const count = res.data?.generated_count ?? 0
            pushLog(res.data?.message || `Texto gerado para ${count} seção(ões) do livro.`, 'success')
            await loadBooks()
        } catch (err) {
            const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Erro ao gerar textos'
            pushLog(`❌ ${msg}`, 'error')
        } finally {
            setGeneratingAllId(null)
        }
    }

    const handlePerplexityEnrichSources = async (bookId: string, targets: 'sections' | 'subsections') => {
        if (!bookId) return
        const label = targets === 'sections' ? 'TODAS as SEÇÕES (sem subseções)' : 'TODAS as SUBSEÇÕES (sem repetir busca nas seções)'
        if (
            !window.confirm(
                `Buscar fontes (Perplexity) para ${label}? As referências vão para a base do livro; o texto não é alterado — só citações [n] ao fim de cada parágrafo; a lista completa aparece no final do EPUB. Pode levar vários minutos. Configure a chave em Configurações.`
            )
        ) {
            return
        }
        if (targets === 'sections') setPerplexityEnrichSectionsId(bookId)
        else setPerplexityEnrichSubsectionsId(bookId)
        try {
            const res = await api.post(`/books/${bookId}/perplexity/enrich-all-sources`, { targets })
            const jid = res.data?.job_id as string | undefined
            if (res.data?.status === 'queued' && jid) {
                pushLog(
                    res.data?.message ||
                        'Fontes Perplexity enfileiradas. O livro atualiza quando o job concluir; acompanhe no Histórico.',
                    'success'
                )
                window.open(`/jobs/${jid}`, '_blank', 'noopener,noreferrer')
            } else {
                pushLog(res.data?.message || 'Perplexity: solicitado.', 'success')
            }
        } catch (err) {
            const msg =
                (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
                (err instanceof Error ? err.message : 'Erro Perplexity')
            pushLog(`❌ ${msg}`, 'error')
            alert(msg)
        } finally {
            if (targets === 'sections') setPerplexityEnrichSectionsId(null)
            else setPerplexityEnrichSubsectionsId(null)
        }
    }

    const handlePerplexityWriteSectionTexts = async (bookId: string) => {
        if (!bookId) return
        const onlyEmpty = window.confirm(
            'OK = gerar Perplexity só nas SEÇÕES vazias.\nCancelar = reescrever TODAS as seções (subseções não mudam).'
        )
        if (
            !window.confirm(
                onlyEmpty
                    ? 'Confirmar: preencher apenas seções sem texto?'
                    : 'Confirmar: sobrescrever texto de todas as seções?'
            )
        ) {
            return
        }
        setPerplexityWriteSectionsId(bookId)
        try {
            const res = await api.post(`/books/${bookId}/perplexity/write-section-texts`, { only_empty: onlyEmpty })
            pushLog(res.data?.message || 'Perplexity: seções concluídas.', 'success')
            const errors = res.data?.errors as string[] | undefined
            if (Array.isArray(errors) && errors.length) {
                pushLog(`⚠️ Alguns itens falharam: ${errors.slice(0, 5).join(' | ')}`, 'warning')
            }
            await loadBooks()
        } catch (err) {
            const msg =
                (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
                (err instanceof Error ? err.message : 'Erro Perplexity')
            pushLog(`❌ ${msg}`, 'error')
            alert(msg)
        } finally {
            setPerplexityWriteSectionsId(null)
        }
    }

    const handlePerplexityWriteSubsectionTexts = async (bookId: string) => {
        if (!bookId) return
        const onlyEmpty = window.confirm(
            'OK = gerar Perplexity só nas SUBSEÇÕES vazias.\nCancelar = reescrever TODAS as subseções (seções não mudam).'
        )
        if (
            !window.confirm(
                onlyEmpty
                    ? 'Confirmar: preencher apenas subseções sem texto?'
                    : 'Confirmar: sobrescrever texto de todas as subseções?'
            )
        ) {
            return
        }
        setPerplexityWriteSubsectionsId(bookId)
        try {
            const res = await api.post(`/books/${bookId}/perplexity/write-subsection-texts`, { only_empty: onlyEmpty })
            pushLog(res.data?.message || 'Perplexity: subseções concluídas.', 'success')
            const errors = res.data?.errors as string[] | undefined
            if (Array.isArray(errors) && errors.length) {
                pushLog(`⚠️ Alguns itens falharam: ${errors.slice(0, 5).join(' | ')}`, 'warning')
            }
            await loadBooks()
        } catch (err) {
            const msg =
                (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
                (err instanceof Error ? err.message : 'Erro Perplexity')
            pushLog(`❌ ${msg}`, 'error')
            alert(msg)
        } finally {
            setPerplexityWriteSubsectionsId(null)
        }
    }

    const handleRepairBook = async (bookId: string) => {
        if (!bookId) return
        const apiKey = getStoredGeminiApiKey() || undefined

        setRepairingId(bookId)
        try {
            const res = await api.post<{ job_id?: string }>(`/books/${bookId}/repair`, { api_key: apiKey })
            const jobId = res.data?.job_id
            if (jobId) {
                pushLog('🛠️ Reparo do livro enfileirado. Acompanhe no Histórico.', 'success')
                window.open(`/jobs/${jobId}`, '_blank', 'noopener,noreferrer')
            } else {
                pushLog('Reparo enfileirado, mas sem job_id retornado.', 'warning')
            }
            await loadBooks()
        } catch (err) {
            const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || (err instanceof Error ? err.message : 'Erro ao reparar livro')
            pushLog(`❌ ${msg}`, 'error')
            alert(msg)
        } finally {
            setRepairingId(null)
        }
    }

    const handleExportBook = async (bookId: string) => {
        if (!bookId) return
        setExportingId(bookId)
        try {
            const res = await endpoints.books.exportFull(bookId)
            const blob = res.data as Blob
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = `export_livro_${(books.find(b => (b.id || b.job_id) === bookId)?.title || 'livro').slice(0, 30).replace(/[^\w\s-]/g, '')}.zip`
            document.body.appendChild(a)
            a.click()
            document.body.removeChild(a)
            URL.revokeObjectURL(url)
            pushLog('✅ Livro exportado com sucesso.', 'success')
        } catch (err) {
            console.error('Error exporting book:', err)
            pushLog('❌ Erro ao exportar livro.', 'error')
        } finally {
            setExportingId(null)
        }
    }

    const handleRestoreBook = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0]
        if (!file) return
        setImporting(true)
        try {
            const res = await endpoints.books.importFull(file)
            const newId = (res.data as { id?: string })?.id
            pushLog(`✅ Livro importado com sucesso. Abrindo...`, 'success')
            await loadBooks()
            if (newId) navigate(`/book/${newId}`)
        } catch (err) {
            console.error('Error importing book:', err)
            const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Erro ao importar. Use um ZIP exportado desta biblioteca.'
            pushLog(`❌ ${msg}`, 'error')
        } finally {
            setImporting(false)
            e.target.value = ''
        }
    }

    const TARGET_LANGUAGES = [
        'English',
        'Español',
        'Português (Brasil)',
        'Français',
        'Deutsch',
        'Italiano',
        '日本語',
        '中文',
    ]

    const handleTranslateMismatched = async (bookId: string) => {
        if (!bookId) return
        const apiKey = getStoredGeminiApiKey() || undefined
        setTranslatingMismatchedId(bookId)
        try {
            const res = await endpoints.books.translateMismatched(bookId, { api_key: apiKey })
            const jobId = (res.data as { job_id?: string })?.job_id
            if (jobId) {
                pushLog('Tradução das seções em outro idioma enfileirada. Acompanhe no Histórico.', 'success')
                window.open(`/jobs/${jobId}`, '_blank', 'noopener,noreferrer')
            }
        } catch (err) {
            const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || (err instanceof Error ? err.message : 'Erro ao traduzir seções')
            pushLog(`❌ ${msg}`, 'error')
            alert(msg)
        } finally {
            setTranslatingMismatchedId(null)
        }
    }

    const handleCopyInLanguage = async (book: BookData, targetLanguage: string) => {
        const bookId = book.id || book.job_id || ''
        if (!bookId) return
        setCopyingId(bookId)
        setCopyLanguageBook(null)
        try {
            const apiKey = getStoredGeminiApiKey()
            const res = await api.post<{ new_book_id: string; translate_job_id: string; message: string }>(
                `/books/${bookId}/copy-in-language`,
                { target_language: targetLanguage, api_key: apiKey || undefined }
            )
            const data = res.data
            pushLog(`✅ Cópia criada (${data.new_book_id}). ${data.message}`, 'success')
            await loadBooks()
            if (data.new_book_id) {
                navigate(`/book/${data.new_book_id}`)
            }
        } catch (err) {
            const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Erro ao criar cópia em outro idioma'
            pushLog(`❌ ${msg}`, 'error')
            alert(msg)
        } finally {
            setCopyingId(null)
        }
    }

    const handleCreateBookFromText = async () => {
        const text = createFromTextInput.trim()
        if (!text) {
            pushLog('Digite ou cole o texto para criar o livro.', 'warning')
            return
        }
        let apiKey = getStoredGeminiApiKey().trim()
        if (!apiKey) {
            apiKey = (await fetchGoogleApiKeyIfMissing()).trim()
        }
        if (!apiKey) {
            pushLog(
                'Nenhuma API Key Google encontrada (navegador ou servidor). Salve em Configurações → API ou em localStorage (gemini_api_key).',
                'error'
            )
            alert('Configure uma API Key Google em Configurações (API) ou aguarde o carregamento da chave do servidor.')
            return
        }
        setIsCreatingFromText(true)
        pushLog('📤 Enviando texto para criação do livro (título, capítulos, seções e subseções)...', 'info')
        try {
            const res = await api.post<{ book_id: string; pipeline_job_id: string; message: string }>('/book/create_from_text', {
                source_text: text,
                api_key: apiKey,
                language: 'Português (Brasil)',
            })
            const { book_id, pipeline_job_id } = res.data
            setCreateFromTextActive(pipeline_job_id, book_id)
            setShowCreateFromText(false)
            setCreateFromTextInput('')
            pushLog(`✅ ${res.data?.message ?? 'Livro em criação.'} Acompanhe a barra de progresso no topo.`, 'success')
            await loadBooks()
        } catch (err) {
            const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || (err instanceof Error ? err.message : 'Erro ao criar livro')
            pushLog(`❌ ${msg}`, 'error')
            alert(msg)
        } finally {
            setIsCreatingFromText(false)
        }
    }

    return (
        <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 dark:from-gray-900 dark:to-gray-800 p-6">
            <div className="w-full">
                {/* Header */}
                <div className="mb-8">
                    <div className="flex items-center justify-between">
                        <div>
                            <h1 className="text-3xl font-bold text-gray-900 dark:text-white flex items-center gap-3">
                                <Book className="h-8 w-8 text-purple-600" />
                                Biblioteca de Livros
                            </h1>
                            <p className="text-gray-600 dark:text-gray-400 mt-2">
                                Gerencie e visualize seus livros criados
                            </p>
                        </div>
                        <div className="flex items-center gap-3">
                            <button
                                onClick={loadBooks}
                                disabled={loading}
                                className="px-4 py-2 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                            >
                                <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                                Atualizar
                            </button>
                            <button
                                onClick={handleDeleteAllBooks}
                                disabled={deletingAll || loading}
                                className="px-4 py-2 bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-200 rounded-lg hover:bg-red-200 dark:hover:bg-red-900/60 flex items-center gap-2 disabled:opacity-50"
                            >
                                {deletingAll ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                                Apagar Todos
                            </button>
                            <input
                                ref={fileInputRef}
                                type="file"
                                accept=".zip"
                                className="hidden"
                                onChange={handleRestoreBook}
                            />
                            <button
                                onClick={() => fileInputRef.current?.click()}
                                disabled={importing || loading}
                                className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 flex items-center gap-2 disabled:opacity-50"
                                title="Selecione um ZIP exportado (book.json + imagens) para importar o livro de volta."
                            >
                                {importing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
                                Importar livro (ZIP)
                            </button>
                            <button
                                onClick={() => navigate('/book')}
                                className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 flex items-center gap-2"
                            >
                                <Book className="h-4 w-4" />
                                Criar Novo Livro
                            </button>
                            <button
                                type="button"
                                onClick={() => setShowCreateFromText((v) => !v)}
                                className="px-4 py-2 bg-amber-500 text-white rounded-lg hover:bg-amber-600 flex items-center gap-2"
                                title="Cole um texto longo; a IA gera título, capítulos, seções e subseções."
                            >
                                <FileInput className="h-4 w-4" />
                                <Sparkles className="h-4 w-4" />
                                Criar a partir de texto
                            </button>
                        </div>
                    </div>
                </div>

                {/* Criar livro a partir de texto (campo estilo ChatGPT) */}
                {showCreateFromText && (
                    <div className="mb-6 rounded-xl border border-amber-200 dark:border-amber-800 bg-amber-50/50 dark:bg-amber-900/20 p-4">
                        <div className="flex items-center justify-between gap-2 mb-2">
                            <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
                                <FileInput className="h-5 w-5 text-amber-600" />
                                Criar livro a partir de texto
                            </h2>
                            <button
                                type="button"
                                onClick={() => { setShowCreateFromText(false); setCreateFromTextInput('') }}
                                className="p-1.5 rounded-lg text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700"
                                aria-label="Fechar"
                            >
                                <X className="h-5 w-5" />
                            </button>
                        </div>
                        <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
                            Cole ou digite o texto do livro (ou um rascunho). A IA irá gerar o título, capítulos, seções e subseções. A barra de progresso aparecerá no topo da tela e continuará visível mesmo se você sair desta página.
                        </p>
                        <textarea
                            value={createFromTextInput}
                            onChange={(e) => setCreateFromTextInput(e.target.value)}
                            placeholder="Cole aqui o texto completo ou o rascunho do livro..."
                            className="w-full min-h-[200px] px-4 py-3 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-white placeholder-gray-400 resize-y font-sans text-sm"
                            disabled={isCreatingFromText}
                        />
                        <div className="mt-3 flex items-center gap-3">
                            <button
                                type="button"
                                onClick={handleCreateBookFromText}
                                disabled={isCreatingFromText || !createFromTextInput.trim()}
                                className="px-4 py-2 bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:opacity-50 flex items-center gap-2 font-medium"
                            >
                                {isCreatingFromText ? (
                                    <>
                                        <Loader2 className="h-4 w-4 animate-spin" />
                                        Enviando...
                                    </>
                                ) : (
                                    <>
                                        <Sparkles className="h-4 w-4" />
                                        Gerar livro
                                    </>
                                )}
                            </button>
                            <span className="text-xs text-gray-500 dark:text-gray-400">
                                Título, capítulos, seções e subseções serão gerados automaticamente.
                            </span>
                        </div>
                    </div>
                )}

                <div className="mb-8">
                    <LogViewer
                        logs={logs}
                        title="Logs da Biblioteca"
                        initiallyExpanded={false}
                        maxHeight="280px"
                    />
                </div>

                {/* Loading State */}
                {loading && (
                    <div className="flex items-center justify-center py-20">
                        <Loader2 className="h-8 w-8 animate-spin text-purple-600" />
                    </div>
                )}

                {/* Error State */}
                {!loading && loadError && (
                    <div className="rounded-xl border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-6 text-center">
                        <p className="text-red-700 dark:text-red-300 mb-4">{loadError}</p>
                        <button
                            type="button"
                            onClick={() => loadBooks()}
                            className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
                        >
                            Tentar novamente
                        </button>
                    </div>
                )}

                {/* Empty State */}
                {!loading && !loadError && books.length === 0 && (
                    <div className="text-center py-20">
                        <Book className="h-16 w-16 text-gray-400 mx-auto mb-4" />
                        <h3 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
                            Nenhum livro encontrado
                        </h3>
                        <p className="text-gray-600 dark:text-gray-400 mb-6">
                            Comece criando seu primeiro livro
                        </p>
                        <button
                            onClick={() => navigate('/book')}
                            className="px-6 py-3 bg-purple-600 text-white rounded-lg hover:bg-purple-700 flex items-center justify-center gap-2 mx-auto"
                        >
                            <Book className="h-5 w-5" />
                            Criar Livro
                        </button>
                    </div>
                )}

                {/* Books Grid */}
                {!loading && books.length > 0 && (
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-6">
                        {books.map((book) => (
                            <div
                                key={book.id || book.job_id}
                                className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden hover:shadow-lg transition-shadow"
                            >
                                {/* Book Cover */}
                                <div className="h-72 bg-gray-200 dark:bg-gray-700 flex items-center justify-center overflow-hidden shrink-0">
                                    {(() => {
                                        const coverUrl = getBookCoverUrl(book)
                                        return coverUrl ? (
                                            <>
                                                <img
                                                    src={coverUrl}
                                                    alt={`Capa do livro: ${book.title || 'Livro'}`}
                                                    role="img"
                                                    className="w-full h-full object-cover"
                                                    loading="lazy"
                                                    decoding="async"
                                                    onError={(e) => {
                                                        const target = e.currentTarget
                                                        const filePath = book.cover_path || book.capa || book.cover_image
                                                        const fileUrl = filePath ? buildFileUrl(filePath) : ''
                                                        if (fileUrl && target.src !== fileUrl) {
                                                            target.src = fileUrl
                                                        } else {
                                                            target.style.display = 'none'
                                                            if (target.nextElementSibling) target.nextElementSibling.classList.remove('hidden')
                                                        }
                                                    }}
                                                />
                                                <div className="hidden flex items-center justify-center w-full h-full">
                                                    <Book className="h-20 w-20 text-gray-400 dark:text-gray-500" />
                                                </div>
                                            </>
                                        ) : (
                                            <Book className="h-20 w-20 text-gray-400 dark:text-gray-500" />
                                        )
                                    })()}
                                </div>

                                {/* Book Info */}
                                <div className="p-6">
                                    <h3 className="text-lg font-bold text-gray-900 dark:text-white mb-2 line-clamp-2">
                                        {book.title || 'Sem título'}
                                    </h3>

                                    {book.subtitle && (
                                        <p className="text-sm text-gray-600 dark:text-gray-400 mb-3 line-clamp-1">
                                            {book.subtitle}
                                        </p>
                                    )}

                                    <div className="space-y-2 mb-4">
                                        {book.author && (
                                            <div className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
                                                <User className="h-4 w-4" />
                                                {book.author}
                                            </div>
                                        )}
                                        <div className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
                                            <Calendar className="h-4 w-4" />
                                            {formatDate(book.updated_at || book.created_at)}
                                        </div>
                                        <div className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
                                            <Book className="h-4 w-4" />
                                            {book.total_chapters || book.chapters?.length || 0} capítulos
                                        </div>
                                    </div>

                                    {book.category && (
                                        <span className="inline-block px-2 py-1 bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 text-xs rounded-full mb-4">
                                            {book.category}
                                        </span>
                                    )}

                                    {/* Actions: primeira linha principais, segunda linha secundárias com ícone + texto */}
                                    <div className="space-y-2">
                                        <div className="flex flex-wrap gap-2">
                                            <button
                                                onClick={() => handleViewBook(book)}
                                                className="inline-flex items-center gap-1.5 px-3 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 text-sm"
                                            >
                                                <Eye className="h-4 w-4 shrink-0" />
                                                Ver
                                            </button>
                                            <button
                                                onClick={() => handleEditBook(book)}
                                                className="inline-flex items-center gap-1.5 px-3 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 text-sm"
                                            >
                                                <Edit className="h-4 w-4 shrink-0" />
                                                Editar
                                            </button>
                                            <button
                                                onClick={() => handleRepairBook(book.id || book.job_id || '')}
                                                disabled={repairingId === (book.id || book.job_id)}
                                                className="inline-flex items-center gap-1.5 px-3 py-2 bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-200 rounded-lg hover:bg-slate-200 dark:hover:bg-slate-600 disabled:opacity-50 text-sm"
                                                title="Repara o livro preenchendo o que estiver faltando (estrutura/textos) no Mongo via job"
                                            >
                                                {repairingId === (book.id || book.job_id) ? (
                                                    <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                                ) : (
                                                    <Wrench className="h-4 w-4 shrink-0" />
                                                )}
                                                Corrigir
                                            </button>
                                        </div>
                                        <div className="flex flex-wrap gap-1.5">
                                            <button
                                                onClick={() => handleGenerateAllSectionTexts(book.id || book.job_id || '')}
                                                disabled={generatingAllId === (book.id || book.job_id)}
                                                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 rounded-lg hover:bg-amber-200 dark:hover:bg-amber-900/50 disabled:opacity-50 text-xs"
                                                title="Criar todos os textos das seções (IA)"
                                            >
                                                {generatingAllId === (book.id || book.job_id) ? (
                                                    <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                                                ) : (
                                                    <FileText className="h-3.5 w-3.5 shrink-0" />
                                                )}
                                                Textos
                                            </button>
                                            <button
                                                onClick={() => handleExportBook(book.id || book.job_id || '')}
                                                disabled={exportingId === (book.id || book.job_id)}
                                                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-200 dark:hover:bg-blue-900/50 disabled:opacity-50 text-xs"
                                                title="Exportar livro completo em ZIP"
                                            >
                                                {exportingId === (book.id || book.job_id) ? (
                                                    <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                                                ) : (
                                                    <Download className="h-3.5 w-3.5 shrink-0" />
                                                )}
                                                Exportar
                                            </button>
                                            <button
                                                onClick={() => setCopyLanguageBook(book)}
                                                disabled={copyingId === (book.id || book.job_id)}
                                                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 bg-teal-100 dark:bg-teal-900/30 text-teal-700 dark:text-teal-400 rounded-lg hover:bg-teal-200 dark:hover:bg-teal-900/50 disabled:opacity-50 text-xs"
                                                title="Cópia em outro idioma"
                                            >
                                                {copyingId === (book.id || book.job_id) ? (
                                                    <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                                                ) : (
                                                    <Languages className="h-3.5 w-3.5 shrink-0" />
                                                )}
                                                Cópia idioma
                                            </button>
                                            <button
                                                onClick={() => handleTranslateMismatched(book.id || book.job_id || '')}
                                                disabled={!(book.id || book.job_id) || translatingMismatchedId === (book.id || book.job_id)}
                                                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 rounded-lg hover:bg-amber-200 dark:hover:bg-amber-900/50 disabled:opacity-50 text-xs"
                                                title="Traduzir seções em outro idioma"
                                            >
                                                {translatingMismatchedId === (book.id || book.job_id) ? (
                                                    <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                                                ) : (
                                                    <Languages className="h-3.5 w-3.5 shrink-0" />
                                                )}
                                                Traduzir
                                            </button>
                                            <button
                                                onClick={() => handleDeleteBook(book)}
                                                disabled={deletingId === (book.id || book.job_id)}
                                                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 rounded-lg hover:bg-red-200 dark:hover:bg-red-900/50 disabled:opacity-50 disabled:cursor-not-allowed text-xs"
                                                title="Excluir livro"
                                            >
                                                {deletingId === (book.id || book.job_id) ? (
                                                    <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                                                ) : (
                                                    <Trash2 className="h-3.5 w-3.5 shrink-0" />
                                                )}
                                                Excluir
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                )}

                {/* Book Detail Modal */}
                {selectedBook && (
                    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-6">
                        <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl max-w-4xl w-full max-h-[90vh] overflow-y-auto">
                            <div className="p-6 border-b border-gray-200 dark:border-gray-700">
                                <div className="flex items-start gap-4">
                                    {getBookCoverUrl(selectedBook) && (
                                        <img
                                            src={getBookCoverUrl(selectedBook)}
                                            alt={`Capa do livro: ${selectedBook.title || 'Livro'}`}
                                            role="img"
                                            className="w-48 h-72 object-cover rounded-lg border border-gray-200 dark:border-gray-600 shrink-0"
                                            loading="lazy"
                                            decoding="async"
                                            onError={(e) => {
                                                const target = e.currentTarget
                                                const filePath = selectedBook.cover_path || selectedBook.capa || selectedBook.cover_image
                                                const fileUrl = filePath ? buildFileUrl(filePath) : ''
                                                if (fileUrl && target.src !== fileUrl) {
                                                    target.src = fileUrl
                                                } else {
                                                    target.style.display = 'none'
                                                }
                                            }}
                                        />
                                    )}
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center justify-between gap-2">
                                            <h2 className="text-2xl font-bold text-gray-900 dark:text-white">
                                                {selectedBook.title}
                                            </h2>
                                            <button
                                                onClick={closeBookModal}
                                                className="p-1.5 rounded-lg text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 shrink-0"
                                                aria-label="Fechar"
                                            >
                                                <X className="h-5 w-5" />
                                            </button>
                                        </div>
                                        {selectedBook.subtitle && (
                                            <p className="text-gray-600 dark:text-gray-400 mt-2">
                                                {selectedBook.subtitle}
                                            </p>
                                        )}
                                    </div>
                                </div>
                            </div>

                            <div className="p-6 space-y-6">
                                {/* Metadata */}
                                <div className="grid grid-cols-2 gap-4">
                                    <div>
                                        <label className="text-sm font-medium text-gray-600 dark:text-gray-400">Autor</label>
                                        <p className="text-gray-900 dark:text-white">{selectedBook.author || 'N/A'}</p>
                                    </div>
                                    <div>
                                        <label className="text-sm font-medium text-gray-600 dark:text-gray-400">Categoria</label>
                                        <p className="text-gray-900 dark:text-white">{selectedBook.category || 'N/A'}</p>
                                    </div>
                                    <div>
                                        <label className="text-sm font-medium text-gray-600 dark:text-gray-400">Idioma</label>
                                        <p className="text-gray-900 dark:text-white">{selectedBook.language || 'N/A'}</p>
                                    </div>
                                    <div>
                                        <label className="text-sm font-medium text-gray-600 dark:text-gray-400">Status</label>
                                        <p className="text-gray-900 dark:text-white">{selectedBook.status || 'N/A'}</p>
                                    </div>
                                </div>

                                {/* Description */}
                                {selectedBook.description && (
                                    <div>
                                        <label className="text-sm font-medium text-gray-600 dark:text-gray-400 mb-2 block">Descrição</label>
                                        <p className="text-gray-700 dark:text-gray-300">{selectedBook.description}</p>
                                    </div>
                                )}

                                {/* Chapters com seções e subseções em painéis recolhíveis */}
                                {selectedBookLoading ? (
                                    <div className="flex flex-col items-center justify-center py-16 gap-3 text-gray-500 dark:text-gray-400">
                                        <Loader2 className="h-8 w-8 animate-spin text-purple-600" />
                                        <p className="text-sm">Carregando estrutura do livro…</p>
                                    </div>
                                ) : (
                                    <ChaptersSectionsSubsections chapters={selectedBook.chapters} />
                                )}

                                {/* Actions: grid para não sobrepor, todos com ícone + texto */}
                                <div className="pt-4 border-t border-gray-200 dark:border-gray-700 space-y-3">
                                    <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                                        <button
                                            onClick={() => handleEditBook(selectedBook)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 text-sm"
                                        >
                                            <Edit className="h-4 w-4 shrink-0" />
                                            Editar
                                        </button>
                                        <button
                                            onClick={() => handleRepairBook(selectedBook.id || selectedBook.job_id || '')}
                                            disabled={repairingId === (selectedBook.id || selectedBook.job_id)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-slate-100 dark:bg-slate-700 text-slate-800 dark:text-slate-200 rounded-lg hover:bg-slate-200 dark:hover:bg-slate-600 disabled:opacity-50 text-sm"
                                            title="Repara estrutura e textos faltantes"
                                        >
                                            {repairingId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Wrench className="h-4 w-4 shrink-0" />
                                            )}
                                            Corrigir
                                        </button>
                                        <button
                                            onClick={() => handleExportBook(selectedBook.id || selectedBook.job_id || '')}
                                            disabled={exportingId === (selectedBook.id || selectedBook.job_id)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-200 dark:hover:bg-blue-900/50 disabled:opacity-50 text-sm"
                                            title="Exportar livro em ZIP"
                                        >
                                            {exportingId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Download className="h-4 w-4 shrink-0" />
                                            )}
                                            Exportar
                                        </button>
                                        <button
                                            onClick={() => handleGenerateAllSectionTexts(selectedBook.id || selectedBook.job_id || '')}
                                            disabled={generatingAllId === (selectedBook.id || selectedBook.job_id)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-amber-600 text-white rounded-lg hover:bg-amber-700 disabled:opacity-50 text-sm"
                                            title="Gerar textos das seções com IA"
                                        >
                                            {generatingAllId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <FileText className="h-4 w-4 shrink-0" />
                                            )}
                                            Textos
                                        </button>
                                        <button
                                            onClick={() =>
                                                void handlePerplexityEnrichSources(
                                                    selectedBook.id || selectedBook.job_id || '',
                                                    'sections'
                                                )
                                            }
                                            disabled={
                                                !(selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSubsectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSubsectionsId === (selectedBook.id || selectedBook.job_id)
                                            }
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-sky-600 text-white rounded-lg hover:bg-sky-700 disabled:opacity-50 text-sm"
                                            title="Fontes Perplexity em todas as seções (não nas subseções)"
                                        >
                                            {perplexityEnrichSectionsId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Search className="h-4 w-4 shrink-0" />
                                            )}
                                            Fontes — seções
                                        </button>
                                        <button
                                            onClick={() =>
                                                void handlePerplexityEnrichSources(
                                                    selectedBook.id || selectedBook.job_id || '',
                                                    'subsections'
                                                )
                                            }
                                            disabled={
                                                !(selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSubsectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSubsectionsId === (selectedBook.id || selectedBook.job_id)
                                            }
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-cyan-600 text-white rounded-lg hover:bg-cyan-700 disabled:opacity-50 text-sm"
                                            title="Fontes Perplexity em todas as subseções"
                                        >
                                            {perplexityEnrichSubsectionsId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Search className="h-4 w-4 shrink-0" />
                                            )}
                                            Fontes — subseções
                                        </button>
                                        <button
                                            onClick={() =>
                                                void handlePerplexityWriteSectionTexts(selectedBook.id || selectedBook.job_id || '')
                                            }
                                            disabled={
                                                !(selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSubsectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSubsectionsId === (selectedBook.id || selectedBook.job_id)
                                            }
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 border border-sky-500 dark:border-sky-600 text-sky-800 dark:text-sky-200 rounded-lg hover:bg-sky-50 dark:hover:bg-sky-950/40 disabled:opacity-50 text-sm"
                                            title="Texto Perplexity só nas seções"
                                        >
                                            {perplexityWriteSectionsId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Sparkles className="h-4 w-4 shrink-0" />
                                            )}
                                            Perplexity seções
                                        </button>
                                        <button
                                            onClick={() =>
                                                void handlePerplexityWriteSubsectionTexts(selectedBook.id || selectedBook.job_id || '')
                                            }
                                            disabled={
                                                !(selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityWriteSubsectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSectionsId === (selectedBook.id || selectedBook.job_id) ||
                                                perplexityEnrichSubsectionsId === (selectedBook.id || selectedBook.job_id)
                                            }
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 border border-cyan-600 dark:border-cyan-500 text-cyan-900 dark:text-cyan-100 rounded-lg hover:bg-cyan-50 dark:hover:bg-cyan-950/40 disabled:opacity-50 text-sm"
                                            title="Texto Perplexity só nas subseções"
                                        >
                                            {perplexityWriteSubsectionsId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Sparkles className="h-4 w-4 shrink-0" />
                                            )}
                                            Perplexity subseções
                                        </button>
                                        <button
                                            onClick={() => setCopyLanguageBook(selectedBook)}
                                            disabled={copyingId === (selectedBook.id || selectedBook.job_id)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 disabled:opacity-50 text-sm"
                                            title="Cópia em outro idioma"
                                        >
                                            {copyingId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Languages className="h-4 w-4 shrink-0" />
                                            )}
                                            Cópia idioma
                                        </button>
                                        <button
                                            onClick={() => handleTranslateMismatched(selectedBook.id || selectedBook.job_id || '')}
                                            disabled={!(selectedBook.id || selectedBook.job_id) || translatingMismatchedId === (selectedBook.id || selectedBook.job_id)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 rounded-lg hover:bg-amber-200 dark:hover:bg-amber-900/50 disabled:opacity-50 text-sm"
                                            title="Traduzir seções em outro idioma"
                                        >
                                            {translatingMismatchedId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Languages className="h-4 w-4 shrink-0" />
                                            )}
                                            Traduzir
                                        </button>
                                        <button
                                            onClick={() => handleDeleteBook(selectedBook)}
                                            disabled={deletingId === (selectedBook.id || selectedBook.job_id)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 rounded-lg hover:bg-red-200 dark:hover:bg-red-900/50 disabled:opacity-50 text-sm"
                                            title="Excluir livro"
                                        >
                                            {deletingId === (selectedBook.id || selectedBook.job_id) ? (
                                                <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                                            ) : (
                                                <Trash2 className="h-4 w-4 shrink-0" />
                                            )}
                                            Excluir
                                        </button>
                                        <button
                                            onClick={() => setSelectedBook(null)}
                                            className="inline-flex items-center justify-center gap-2 px-4 py-2 bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-600 text-sm col-span-2 sm:col-span-1"
                                        >
                                            <X className="h-4 w-4 shrink-0" />
                                            Fechar
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                )}

                {/* Copy in another language – language picker modal */}
                {copyLanguageBook && (
                    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[60] p-6">
                        <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl max-w-md w-full p-6">
                            <div className="flex items-center justify-between mb-4">
                                <h3 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
                                    <Languages className="h-5 w-5 text-teal-600" />
                                    Cópia em outro idioma
                                </h3>
                                <button
                                    onClick={() => setCopyLanguageBook(null)}
                                    className="p-1.5 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
                                    aria-label="Fechar"
                                >
                                    <X className="h-5 w-5" />
                                </button>
                            </div>
                            <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                                Criar uma cópia de &quot;{copyLanguageBook.title || 'Livro'}&quot; e traduzir para o idioma escolhido. A tradução rodará em segundo plano.
                            </p>
                            <div className="grid grid-cols-2 gap-2">
                                {TARGET_LANGUAGES.map((lang) => (
                                    <button
                                        key={lang}
                                        onClick={() => handleCopyInLanguage(copyLanguageBook, lang)}
                                        disabled={copyingId === (copyLanguageBook.id || copyLanguageBook.job_id)}
                                        className="px-4 py-3 rounded-lg border border-gray-200 dark:border-gray-600 hover:bg-teal-50 dark:hover:bg-teal-900/20 hover:border-teal-300 dark:hover:border-teal-700 text-left text-sm font-medium text-gray-800 dark:text-gray-200 disabled:opacity-50"
                                    >
                                        {lang}
                                    </button>
                                ))}
                            </div>
                            <p className="text-xs text-gray-500 dark:text-gray-500 mt-3">
                                É necessária uma API Key (Gemini) para tradução. Configure em Configurações se necessário.
                            </p>
                        </div>
                    </div>
                )}
            </div>
        </div>
    )
}

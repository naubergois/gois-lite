import { useState } from 'react'
import { FileDown, Loader2, Save, Settings, Languages, Sparkles } from 'lucide-react'
import { AuthorStyleSelector } from '@/components/AuthorStyleSelector'
import { LogViewer, LogEntry } from '@/components/LogViewer'
import { MarkdownField } from '@/components/MarkdownField'
import { cn } from '@/lib/utils'
import { getStoredGeminiApiKey } from '@/lib/apiKeys'
import { api, endpoints } from '@/lib/api'
import { scheduleHeavyBookWork } from '@/lib/deferUi'
import type { BookPlan, BookPromptItem } from './bookWorkspaceUtils'

type BookGenre = {
  id: string
  name: string
}

type BookMetadataTabProps = {
  draftPlan: BookPlan | null
  setDraftPlan: React.Dispatch<React.SetStateAction<BookPlan | null>>
  savePlan: (nextPlan?: BookPlan | null) => Promise<void> | void
  saving: boolean
  metadataAuthorStyles: string[]
  BOOK_GENRES: BookGenre[]
  handleReplanBookStyle: () => Promise<void> | void
  isGeneratingChapters: boolean
  plan: BookPlan | null
  logs: LogEntry[]
  /** Book/job id for translate API. If provided, shows "Traduzir" button. */
  bookId?: string | null
  /** Called when translate job is enqueued (job_id) so parent can show progress bar. options.fromScratch: when true, parent clears translated-unit icons. */
  onTranslateStarted?: (jobId: string, options?: { fromScratch?: boolean }) => void
  /** Called after successful translate so parent can refetch. */
  onTranslateComplete?: () => void | Promise<void>
  /** Retorna a API key para chamadas de IA (ex.: gerar descrição/palavras-chave). */
  getApiKey?: () => string | undefined
}

function buildMetadataTxt(draftPlan: BookPlan | null, bookGenres: { id: string; name: string }[]): string {
  const title = draftPlan?.title ?? ''
  const subtitle = draftPlan?.subtitle ?? ''
  const description = draftPlan?.description ?? ''
  const keywords = draftPlan?.keywords ?? ''
  const synopsis = (draftPlan?.draft ?? '').slice(0, 1500)
  const synopsisCount = synopsis.length
  const genreNames = (draftPlan?.book_style ?? [])
    .map((id) => bookGenres.find((g) => g.id === id)?.name)
    .filter(Boolean) as string[]
  const categoriesList = bookGenres.map((g) => g.name).join('\n')
  const subtemas = draftPlan?.objective ?? ''
  const author = draftPlan?.author ?? ''
  const authorRecognition = draftPlan?.author_inspiration ?? draftPlan?.author ?? ''
  const language = draftPlan?.language ?? 'Português'
  const year = new Date().getFullYear()

  return [
    '=== METADADOS DO LIVRO (exportação para editoras/distribuição) ===',
    '',
    'Título do livro',
    'O título deve ser impactante e memorável',
    title || '[Preencher]',
    '',
    'Subtítulo',
    'Ofereça mais detalhes sobre o tema central do seu livro',
    subtitle || '[Preencher]',
    '',
    'Descrição do livro',
    'Texto de apresentação do livro para editoras e distribuição',
    description || '[Preencher]',
    '',
    'Sinopse',
    'Aqui é onde você conta a história por trás da história. Uma sinopse cativante é fundamental para capturar a atenção e a imaginação dos leitores',
    `${synopsisCount} / 1500`,
    synopsis || '[Preencher]',
    '',
    'Insira o tema central do seu livro',
    'Selecione de 2 até 5 itens',
    genreNames.length ? genreNames.join(', ') : '[Selecionar categorias]',
    '',
    'Lista de categorias disponíveis:',
    categoriesList,
    '',
    'Liste os subtemas abordados',
    'Selecione de 2 até 5 itens',
    subtemas || '[Preencher]',
    '',
    'Nome do autor',
    '(Para múltiplos autores, use " ; ")',
    author || '[Preencher]',
    '',
    'Como você deseja ser reconhecido?',
    authorRecognition || '[Preencher]',
    '',
    'Número da edição',
    'Ex: Edição nº01',
    '[Preencher]',
    '',
    'Ano de publicação',
    String(year),
    '',
    'Quantidade de páginas de degustação',
    'Nº de páginas que o leitor poderá ler no nosso site, antes de comprar. No caso de vendas por livrarias parceiras (Google Books, Apple etc.), os próprios canais definem um percentual (10%-20%) do livro como degustação gratuita.',
    '5',
    '',
    'Idioma do livro',
    language,
    '',
    'Esta obra é uma tradução?',
    'Não',
    '',
    'Palavras-chave',
    '(Para múltiplas palavras-chave, separe-as com uma vírgula " , ")',
    'Escreva aqui algumas palavras, separadas por vírgulas, que descrevam o seu livro.',
    keywords || '[Preencher]',
    '',
    'ISBN (opcional)',
    'O International Standard Book Number, sendo chamado inicialmente de Standard Book Numbering, é um sistema internacional de identificação de livros e softwares que utiliza números para classificá-los por título, autor, país, editora e edição.',
    'Não tem ISBN? Clique aqui.',
    '[Preencher]',
  ].join('\n')
}

export function BookMetadataTab({
  draftPlan,
  setDraftPlan,
  savePlan,
  saving,
  metadataAuthorStyles,
  BOOK_GENRES,
  handleReplanBookStyle,
  isGeneratingChapters,
  plan,
  logs,
  bookId,
  onTranslateStarted,
  onTranslateComplete,
  getApiKey,
}: BookMetadataTabProps) {
  const [isTranslating, setIsTranslating] = useState(false)
  const [translateError, setTranslateError] = useState<string | null>(null)
  const [isTranslatingMismatched, setIsTranslatingMismatched] = useState(false)
  const [translateMismatchedError, setTranslateMismatchedError] = useState<string | null>(null)
  const [translateFromScratch, setTranslateFromScratch] = useState(true)
  const [isGeneratingMetadata, setIsGeneratingMetadata] = useState(false)
  const [generateMetadataError, setGenerateMetadataError] = useState<string | null>(null)
  // Form para criar novo prompt do livro (persistido em banco)
  const [newPromptName, setNewPromptName] = useState('')
  const [newPromptText, setNewPromptText] = useState('')
  const [newPromptStyleIds, setNewPromptStyleIds] = useState<string[]>([])

  const handleCreateBookPrompt = () => {
    const name = newPromptName?.trim() || 'Novo prompt'
    if (!draftPlan) return
    const item: BookPromptItem = {
      id: `prompt-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      name,
      prompt_text: newPromptText?.trim() || '',
      style_ids: [...newPromptStyleIds],
    }
    setDraftPlan((prev) =>
      prev
        ? { ...prev, book_prompts: [...(prev.book_prompts || []), item] }
        : prev
    )
    setNewPromptName('')
    setNewPromptText('')
    setNewPromptStyleIds([])
    void savePlan()
  }

  const handleRemoveBookPrompt = (id: string) => {
    setDraftPlan((prev) =>
      prev
        ? { ...prev, book_prompts: (prev.book_prompts || []).filter((p) => p.id !== id) }
        : prev
    )
    void savePlan()
  }

  const handleGenerateDescriptionAndKeywords = () => {
    if (!bookId || !draftPlan) return
    const apiKey = getApiKey?.() ?? getStoredGeminiApiKey()
    if (!apiKey) {
      setGenerateMetadataError('Configure uma API Key em Configurações para gerar com IA.')
      return
    }
    setGenerateMetadataError(null)
    setIsGeneratingMetadata(true)
    scheduleHeavyBookWork(async () => {
      try {
        const res = await endpoints.books.generateMetadata(bookId, { api_key: apiKey })
        const jobId = res.data?.job_id
        if (jobId) {
          // Job enfileirado: polling até completar e então atualizar plano
          const t = setInterval(async () => {
            try {
              const statusRes = await api.get(`/status/${jobId}`).catch(() => null)
              const status = statusRes?.data?.status
              if (status === 'completed') {
                clearInterval(t)
                setIsGeneratingMetadata(false)
                const desc = statusRes?.data?.final_state?.description ?? ''
                const kw = statusRes?.data?.final_state?.keywords ?? ''
                if ((desc || kw) && draftPlan) {
                  const nextPlan = { ...draftPlan, description: desc, keywords: kw }
                  setDraftPlan(nextPlan)
                  void savePlan(nextPlan)
                }
              } else if (status === 'failed') {
                clearInterval(t)
                setIsGeneratingMetadata(false)
                setGenerateMetadataError(statusRes?.data?.error ?? 'Geração falhou.')
              }
            } catch {
              /* ignore */
            }
          }, 2500)
        } else {
          setIsGeneratingMetadata(false)
          setGenerateMetadataError('Resposta do servidor sem job_id.')
        }
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : 'Erro ao gerar descrição e palavras-chave.'
        setGenerateMetadataError(msg)
        setIsGeneratingMetadata(false)
      }
    })
  }

  const handleTranslate = () => {
    if (!bookId || !draftPlan?.language) return
    const apiKey = getStoredGeminiApiKey() || undefined
    setIsTranslating(true)
    setTranslateError(null)
    const lang = draftPlan.language
    window.setTimeout(() => {
      api
        .post<{ job_id?: string }>(`/books/${bookId}/translate`, {
          target_language: lang,
          api_key: apiKey,
        }, { timeout: 30000 })
        .then((res) => {
          const data = res.data as { job_id?: string; translate_job_id?: string } | undefined
          const jobId = data?.job_id ?? data?.translate_job_id
          if (jobId) {
            onTranslateStarted?.(jobId, { fromScratch: translateFromScratch })
            window.open(`/jobs/${jobId}`, '_blank', 'noopener,noreferrer')
          }
          void onTranslateComplete?.()
        })
        .catch((e: unknown) => {
          const msg =
            (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
            (e instanceof Error ? e.message : 'Erro ao traduzir')
          setTranslateError(String(msg))
        })
        .finally(() => setIsTranslating(false))
    }, 50)
  }

  const handleTranslateMismatched = () => {
    if (!bookId) return
    const apiKey = getStoredGeminiApiKey() || undefined
    setIsTranslatingMismatched(true)
    setTranslateMismatchedError(null)
    window.setTimeout(() => {
      endpoints.books
        .translateMismatched(bookId, { api_key: apiKey })
        .then((res) => {
          const data = res.data
          const jobId = data?.job_id
          if (jobId) {
            onTranslateStarted?.(jobId)
            window.open(`/jobs/${jobId}`, '_blank', 'noopener,noreferrer')
          }
          void onTranslateComplete?.()
        })
        .catch((e: unknown) => {
          const msg =
            (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
            (e instanceof Error ? e.message : 'Erro ao traduzir seções')
          setTranslateMismatchedError(String(msg))
        })
        .finally(() => setIsTranslatingMismatched(false))
    }, 50)
  }

  return (
    <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-6">
      <div className="flex items-center gap-2 text-lg font-semibold">
        <Settings className="w-5 h-5" />
        Metadados do Livro
      </div>

      <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-900/30">
        <div className="flex items-center justify-between gap-2 mb-2">
          <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">
            Rascunho do Livro (contexto para geracao de capitulos e secoes)
          </span>
          {draftPlan && (
            <button
              type="button"
              onClick={() => savePlan()}
              disabled={saving}
              className="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-60 flex items-center gap-1"
            >
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              Salvar rascunho
            </button>
          )}
        </div>
        {draftPlan ? (
          <MarkdownField
            value={draftPlan.draft ?? ''}
            onChange={(v) => setDraftPlan((prev) => (prev ? { ...prev, draft: v } : prev))}
            placeholder="Cole ou escreva o rascunho do livro. Sera usado como contexto em todas as geracoes."
            rows={6}
            showPreview={false}
            className="text-sm font-mono"
          />
        ) : (
          <p className="text-sm text-gray-500 dark:text-gray-400">Carregando plano do livro...</p>
        )}
      </div>

      <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-900/30">
        <label className="block text-sm font-semibold text-slate-700 dark:text-slate-200 mb-1">
          Prompt aplicado a todas as seções e subseções
        </label>
        <p className="text-xs text-slate-500 dark:text-slate-400 mb-2">
          Instruções adicionais que a IA usará ao gerar ou reescrever o texto de cada seção e subseção do livro (ex.: tom, nível técnico, evitar jargões).
        </p>
        {draftPlan ? (
          <textarea
            value={draftPlan.global_section_prompt ?? ''}
            onChange={(e) => setDraftPlan((prev) => (prev ? { ...prev, global_section_prompt: e.target.value } : prev))}
            onBlur={() => savePlan()}
            placeholder="Ex.: Manter linguagem acessível; evitar termos em inglês sem tradução; incluir um exemplo prático por seção."
            rows={3}
            className="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-md text-sm bg-white dark:bg-slate-700 text-slate-900 dark:text-white resize-y"
          />
        ) : (
          <p className="text-sm text-gray-500 dark:text-gray-400">Carregando plano do livro...</p>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="space-y-2">
          <label className="text-sm font-medium">Titulo</label>
          <input
            type="text"
            value={draftPlan?.title || ''}
            onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, title: e.target.value } : prev)}
            className="w-full px-3 py-2 border rounded-lg text-sm"
            placeholder="Titulo do livro"
          />
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">Subtitulo</label>
          <input
            type="text"
            value={draftPlan?.subtitle || ''}
            onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, subtitle: e.target.value } : prev)}
            className="w-full px-3 py-2 border rounded-lg text-sm"
            placeholder="Subtitulo do livro"
          />
        </div>

        <div className="space-y-2 md:col-span-2">
          <label className="text-sm font-medium">Descricao do livro</label>
          <textarea
            value={draftPlan?.description || ''}
            onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, description: e.target.value } : prev)}
            className="w-full px-3 py-2 border rounded-lg text-sm"
            placeholder="Texto de apresentacao do livro para editoras e distribuicao (incluido na exportacao de metadados)"
            rows={3}
          />
        </div>

        <div className="space-y-2 md:col-span-2">
          <label className="text-sm font-medium">Palavras-chave</label>
          <input
            type="text"
            value={draftPlan?.keywords || ''}
            onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, keywords: e.target.value } : prev)}
            className="w-full px-3 py-2 border rounded-lg text-sm"
            placeholder="Ex: ficcao, romance, Brasil (separadas por virgula)"
          />
        </div>

        {bookId && (
          <div className="md:col-span-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => handleGenerateDescriptionAndKeywords()}
              disabled={isGeneratingMetadata}
              className="px-4 py-2 bg-violet-600 text-white rounded-lg text-sm flex items-center gap-2 hover:bg-violet-700 disabled:opacity-60"
            >
              {isGeneratingMetadata ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              Gerar descricao e palavras-chave com IA
            </button>
            {generateMetadataError && (
              <span className="text-sm text-red-600 dark:text-red-400">{generateMetadataError}</span>
            )}
          </div>
        )}

        <div className="space-y-2">
          <label className="text-sm font-medium">Autor</label>
          <input
            type="text"
            value={draftPlan?.author || ''}
            onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, author: e.target.value } : prev)}
            className="w-full px-3 py-2 border rounded-lg text-sm"
            placeholder="Nome do autor"
          />
        </div>

        <div className="space-y-2">
          {draftPlan && (
            <AuthorStyleSelector
              selectedStyles={metadataAuthorStyles}
              onChange={(styles) =>
                setDraftPlan((prev) =>
                  prev
                    ? {
                        ...prev,
                        author_styles: styles,
                        author_inspiration: styles.join(', '),
                      }
                    : prev
                )
              }
              label="Inspiracao de Escritores"
              description="Os capitulos gerados por IA seguirao o estilo destes escritores."
            />
          )}
        </div>

        <div className="space-y-2 md:col-span-2">
          <label className="text-sm font-medium">Estilo do Livro (Genero)</label>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            {BOOK_GENRES.map((genre) => (
              <button
                key={genre.id}
                onClick={() =>
                  setDraftPlan((prev) =>
                    prev
                      ? {
                          ...prev,
                          book_style: prev.book_style?.includes(genre.id)
                            ? prev.book_style.filter((id: string) => id !== genre.id)
                            : [...(prev.book_style || []), genre.id]
                        }
                      : prev
                  )
                }
                className={cn(
                  'px-3 py-2 rounded-lg border text-sm text-left',
                  (draftPlan?.book_style || []).includes(genre.id)
                    ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/20'
                    : 'border-gray-200 dark:border-gray-600 hover:border-gray-300'
                )}
              >
                {genre.name}
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-2 md:col-span-2">
          <label className="text-sm font-medium">Diretrizes do Estilo</label>
          <textarea
            value={draftPlan?.book_style_prompt || ''}
            onChange={(e) =>
              setDraftPlan((prev) =>
                prev ? { ...prev, book_style_prompt: e.target.value } : prev
              )
            }
            className="w-full px-3 py-2 border rounded-lg text-sm"
            placeholder="Ex: tom leve, humor sarcastico, capitulos curtos..."
            rows={4}
          />
          <div className="flex gap-2">
            <button
              onClick={handleReplanBookStyle}
              className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm"
              disabled={isGeneratingChapters}
            >
              {isGeneratingChapters ? 'Replanejando...' : 'Replanejar Capitulos'}
            </button>
            <button
              onClick={() =>
                setDraftPlan((prev) =>
                  prev ? { ...prev, book_style: undefined, book_style_prompt: '' } : prev
                )
              }
              className="px-4 py-2 border rounded-lg text-sm"
            >
              Limpar
            </button>
          </div>
        </div>

        {/* Prompts do livro (persistidos em banco) + combo multi-estilo */}
        <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50/50 p-4 dark:border-slate-700 dark:bg-slate-900/20">
          <div className="text-sm font-semibold text-slate-700 dark:text-slate-200">
            Prompts do Livro
          </div>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Crie prompts reutilizáveis e associe estilos. Eles são salvos no livro (banco).
          </p>
          {draftPlan && (
            <>
              <AuthorStyleSelector
                selectedStyles={newPromptStyleIds}
                onChange={setNewPromptStyleIds}
                label="Estilos do prompt"
                description="Selecione um ou mais estilos para este prompt (multi-estilo)."
                className="mt-2"
              />
              <div className="grid gap-2 sm:grid-cols-1">
                <div>
                  <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Nome do prompt</label>
                  <input
                    type="text"
                    value={newPromptName}
                    onChange={(e) => setNewPromptName(e.target.value)}
                    placeholder="Ex: Capa principal, Resumo do capítulo..."
                    className="w-full px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg text-sm bg-white dark:bg-gray-800"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Texto do prompt (opcional)</label>
                  <textarea
                    value={newPromptText}
                    onChange={(e) => setNewPromptText(e.target.value)}
                    placeholder="Instruções ou texto base do prompt..."
                    className="w-full px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg text-sm bg-white dark:bg-gray-800"
                    rows={2}
                  />
                </div>
              </div>
              <button
                type="button"
                onClick={handleCreateBookPrompt}
                className="mt-2 px-4 py-2 bg-cyan-600 text-white rounded-lg text-sm font-medium hover:bg-cyan-700"
              >
                Criar prompt do livro
              </button>
            </>
          )}
          {(draftPlan?.book_prompts?.length ?? 0) > 0 && (
            <div className="mt-4 space-y-2">
              <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Prompts salvos</span>
              <ul className="space-y-2">
                {(draftPlan?.book_prompts ?? []).map((p) => (
                  <li
                    key={p.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 dark:border-slate-600 bg-white dark:bg-gray-800 p-2"
                  >
                    <div className="min-w-0 flex-1">
                      <span className="font-medium text-sm text-slate-800 dark:text-slate-200">{p.name}</span>
                      {p.prompt_text && (
                        <p className="text-xs text-slate-500 dark:text-slate-400 truncate max-w-md" title={p.prompt_text}>
                          {p.prompt_text}
                        </p>
                      )}
                      {p.style_ids.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {p.style_ids.map((sid) => (
                            <span
                              key={sid}
                              className="inline-flex items-center rounded-full bg-cyan-100 dark:bg-cyan-900/40 text-cyan-800 dark:text-cyan-300 px-2 py-0.5 text-xs"
                            >
                              {sid}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => handleRemoveBookPrompt(p.id)}
                      className="text-slate-400 hover:text-red-600 dark:hover:text-red-400 text-sm px-2"
                      title="Remover prompt"
                    >
                      Remover
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">Publico-alvo</label>
          <input
            type="text"
            value={draftPlan?.target_audience || ''}
            onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, target_audience: e.target.value } : prev)}
            className="w-full px-3 py-2 border rounded-lg text-sm"
            placeholder="Ex: Publico geral, estudantes, profissionais"
          />
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium">Idioma do livro</label>
          <select
            value={draftPlan?.language || 'Português (Brasil)'}
            onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, language: e.target.value } : prev)}
            className="w-full px-3 py-2 border rounded-lg text-sm bg-white dark:bg-gray-800"
          >
            <option value="Português (Brasil)">Português (Brasil)</option>
            <option value="English">English</option>
            <option value="Español">Español</option>
            <option value="Français">Français</option>
            <option value="Deutsch">Deutsch</option>
            <option value="Italiano">Italiano</option>
            <option value="日本語">日本語</option>
          </select>
          <p className="text-xs text-gray-500">Todos os capítulos, seções e conteúdos serão gerados neste idioma.</p>
          {bookId && (
            <div className="mt-2 flex flex-col gap-2">
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
                  onClick={handleTranslate}
                  disabled={isTranslating || isTranslatingMismatched || !draftPlan?.language}
                  className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-blue-200 bg-blue-50 text-blue-700 text-sm font-medium hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-900/30 dark:text-blue-200 dark:hover:bg-blue-900/50 disabled:opacity-50 w-fit"
                >
                  {isTranslating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Languages className="w-4 h-4" />}
                  Traduzir todo o conteúdo para o idioma selecionado
                </button>
                <button
                  type="button"
                  onClick={handleTranslateMismatched}
                  className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-amber-200 bg-amber-50 text-amber-800 text-sm font-medium hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200 dark:hover:bg-amber-900/50 disabled:opacity-50 w-fit"
                >
                  {isTranslatingMismatched ? <Loader2 className="w-4 h-4 animate-spin" /> : <Languages className="w-4 h-4" />}
                  Traduzir apenas seções em outro idioma
                </button>
              </div>
              {translateError && <p className="text-xs text-red-500 mt-1">{translateError}</p>}
              {translateMismatchedError && <p className="text-xs text-red-500 mt-1">{translateMismatchedError}</p>}
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">A barra de progresso da tradução fica fixa no topo da área de conteúdo ao iniciar; role para cima se não estiver vendo.</p>
            </div>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <label className="text-sm font-medium">Prologo</label>
        <textarea
          value={draftPlan?.prologue || ''}
          onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, prologue: e.target.value } : prev)}
          className="w-full px-3 py-2 border rounded-lg text-sm"
          placeholder="Texto do prologo (opcional)"
          rows={4}
        />
      </div>

      <div className="space-y-2">
        <label className="text-sm font-medium">Agradecimentos</label>
        <textarea
          value={draftPlan?.acknowledgments || ''}
          onChange={(e) => setDraftPlan((prev) => prev ? { ...prev, acknowledgments: e.target.value } : prev)}
          className="w-full px-3 py-2 border rounded-lg text-sm"
          placeholder="Texto de agradecimentos (opcional)"
          rows={4}
        />
      </div>

      <div className="flex flex-wrap gap-3 pt-4 border-t">
        <button
          onClick={() => savePlan()}
          className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm flex items-center gap-2"
          disabled={saving}
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Salvar Metadados
        </button>
        <button
          onClick={() => setDraftPlan(plan)}
          className="px-4 py-2 border rounded-lg text-sm"
        >
          Descartar Alteracoes
        </button>
        <button
          type="button"
          onClick={() => {
            const txt = buildMetadataTxt(draftPlan, BOOK_GENRES)
            const blob = new Blob([txt], { type: 'text/plain;charset=utf-8' })
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = `metadados-livro-${(draftPlan?.title || 'livro').replace(/[^a-z0-9\u00C0-\u024F]/gi, '-').slice(0, 40)}.txt`
            a.click()
            URL.revokeObjectURL(url)
          }}
          className="px-4 py-2 border border-slate-300 dark:border-slate-600 rounded-lg text-sm flex items-center gap-2 hover:bg-slate-50 dark:hover:bg-slate-800"
        >
          <FileDown className="w-4 h-4" />
          Exportar metadados (TXT)
        </button>
      </div>

      {logs.length > 0 && (
        <LogViewer
          logs={logs}
          maxHeight="240px"
          title="Logs de Execucao (Metadados)"
          autoScroll={true}
        />
      )}
    </div>
  )
}

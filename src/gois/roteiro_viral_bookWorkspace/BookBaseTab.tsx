import { useState } from 'react'
import { Loader2, Plus, Trash2, Pencil, Check, X, Search } from 'lucide-react'
import type { BookPlan, BookFact, BookBibliographyEntry } from './bookWorkspaceUtils'
import { api } from '@/lib/api'

type BaseType = 'facts' | 'bibliography'

interface BookBaseTabProps {
  type: BaseType
  draftPlan: BookPlan | null
  onUpdatePlan: (updater: (prev: BookPlan) => BookPlan) => void
  onSave: (planToSave?: BookPlan) => void
  jobId: string | undefined
  getApiKey: () => string | undefined
}

export function BookBaseTab({ type, draftPlan, onUpdatePlan, onSave, jobId, getApiKey }: BookBaseTabProps) {
  const [inputText, setInputText] = useState('')
  const [isExtracting, setIsExtracting] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [editEntry, setEditEntry] = useState<BookBibliographyEntry | null>(null)
  const [searchingSummary, setSearchingSummary] = useState(false)
  const [searchingAllSummaries, setSearchingAllSummaries] = useState(false)
  const [searchAllProgress, setSearchAllProgress] = useState<string | null>(null)

  const isFacts = type === 'facts'
  const title = isFacts ? 'Base de fatos' : 'Base de bibliografia'
  const placeholder = isFacts
    ? 'Cole ou digite um texto. O agente extrairá fatos e afirmativas verificáveis para inserir na lista abaixo.'
    : 'Cole ou digite um texto. O agente extrairá referências bibliográficas (livros, artigos, citações) para inserir na lista abaixo.'

  const items = isFacts
    ? (draftPlan?.facts_base ?? [])
    : (draftPlan?.bibliography_base ?? [])

  const handleExtract = async () => {
    if (!jobId || !inputText.trim()) return
    setIsExtracting(true)
    try {
      if (isFacts) {
        const res = await api.post<{ status: string; facts: Array<{ id: string; text: string; source?: string }> }>(
          '/book/extract_facts_from_text',
          { job_id: jobId, text: inputText.trim(), api_key: getApiKey() }
        )
        const newFacts = (res.data?.facts ?? []).map((f) => ({ ...f, created_at: Date.now() / 1000 }))
        if (newFacts.length) {
          const updated = { ...draftPlan!, facts_base: [...(draftPlan!.facts_base ?? []), ...newFacts] }
          onUpdatePlan(() => updated)
          setInputText('')
          onSave(updated)
        }
      } else {
        const res = await api.post<{ status: string; entries: Array<BookBibliographyEntry> }>(
          '/book/extract_bibliography_from_text',
          { job_id: jobId, text: inputText.trim(), api_key: getApiKey() }
        )
        const newEntries = (res.data?.entries ?? []).map((e) => ({ ...e, created_at: Date.now() / 1000 }))
        if (newEntries.length) {
          const updated = { ...draftPlan!, bibliography_base: [...(draftPlan!.bibliography_base ?? []), ...newEntries] }
          onUpdatePlan(() => updated)
          setInputText('')
          onSave(updated)
        }
      }
    } catch (e) {
      console.error('Extract error:', e)
    } finally {
      setIsExtracting(false)
    }
  }

  const handleDelete = (id: string) => {
    if (!draftPlan) return
    const updated = isFacts
      ? { ...draftPlan, facts_base: (draftPlan.facts_base ?? []).filter((f) => f.id !== id) }
      : { ...draftPlan, bibliography_base: (draftPlan.bibliography_base ?? []).filter((b) => b.id !== id) }
    onUpdatePlan(() => updated)
    onSave(updated)
    if (editingId === id) { setEditingId(null); setEditEntry(null) }
  }

  const startEdit = (id: string, text: string, entry?: BookBibliographyEntry) => {
    setEditingId(id)
    setEditValue(text)
    setEditEntry(entry ?? null)
  }

  const saveEdit = () => {
    if (editingId == null || !draftPlan) return
    if (isFacts) {
      const value = editValue.trim()
      const updated = { ...draftPlan, facts_base: (draftPlan.facts_base ?? []).map((f) => (f.id === editingId ? { ...f, text: value || f.text } : f)) }
      onUpdatePlan(() => updated)
      onSave(updated)
    } else if (editEntry) {
      const updated = {
        ...draftPlan,
        bibliography_base: (draftPlan.bibliography_base ?? []).map((b) =>
          b.id === editingId ? { ...editEntry, id: b.id, created_at: b.created_at } : b
        ),
      }
      onUpdatePlan(() => updated)
      onSave(updated)
      setEditEntry(null)
    }
    setEditingId(null)
    setEditValue('')
  }

  const closeBibModal = () => {
    setEditEntry(null)
    setEditingId(null)
    setEditValue('')
  }

  const handleSearchSummary = async () => {
    if (!jobId || !editEntry) return
    setSearchingSummary(true)
    try {
      const res = await api.post<{ status: string; summary: string }>('/book/search_reference_summary', {
        job_id: jobId,
        entry: {
          author: editEntry.author,
          title: editEntry.title,
          year: editEntry.year,
          url: editEntry.url,
          text: editEntry.text,
        },
        api_key: getApiKey(),
      })
      const summary = res.data?.summary?.trim()
      if (summary) setEditEntry((e) => (e ? { ...e, summary } : e))
    } catch (e) {
      console.error('Search summary error:', e)
    } finally {
      setSearchingSummary(false)
    }
  }

  const handleSearchAllSummaries = async () => {
    if (!jobId || !draftPlan?.bibliography_base?.length) return
    const list = draftPlan.bibliography_base
    setSearchingAllSummaries(true)
    setSearchAllProgress(`0/${list.length}`)
    try {
      let updated = [...list]
      for (let i = 0; i < list.length; i++) {
        // Libera o thread para pintar progresso / evitar sensação de tela travada em sequências longas
        await new Promise((r) => setTimeout(r, 0))
        setSearchAllProgress(`${i + 1}/${list.length}`)
        const entry = list[i] as BookBibliographyEntry
        try {
          const res = await api.post<{ status: string; summary: string }>('/book/search_reference_summary', {
            job_id: jobId,
            entry: {
              author: entry.author,
              title: entry.title,
              year: entry.year,
              url: entry.url,
              text: entry.text,
            },
            api_key: getApiKey(),
          })
          const summary = res.data?.summary?.trim()
          if (summary) updated = updated.map((e, idx) => (idx === i ? { ...e, summary } : e)) as BookBibliographyEntry[]
        } catch (e) {
          console.error(`Search summary error for entry ${i + 1}:`, e)
        }
      }
      const nextPlan = { ...draftPlan, bibliography_base: updated }
      onUpdatePlan(() => nextPlan)
      onSave(nextPlan)
    } finally {
      setSearchingAllSummaries(false)
      setSearchAllProgress(null)
    }
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">{title}</h2>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {isFacts
          ? 'Use o campo abaixo para que um agente extraia fatos do texto e os insira na base. Os itens podem ser editados ou removidos.'
          : 'Use o campo abaixo para que um agente extraia referências bibliográficas do texto e as insira na base. Os itens podem ser editados ou removidos.'}
      </p>

      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-lg p-4">
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Texto para extração</label>
        <textarea
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          placeholder={placeholder}
          rows={4}
          className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white resize-y"
        />
        <div className="mt-2">
          <button
            type="button"
            onClick={handleExtract}
            disabled={isExtracting || !inputText.trim() || !jobId}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-amber-600 text-white text-sm font-medium hover:bg-amber-700 disabled:opacity-50"
          >
            {isExtracting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            Inserir com agente
          </button>
        </div>
      </div>

      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-lg p-4">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {isFacts ? 'Fatos' : 'Referências'} ({items.length})
          </h3>
          {!isFacts && items.length > 0 && (
            <button
              type="button"
              onClick={handleSearchAllSummaries}
              disabled={searchingAllSummaries || !jobId}
              className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sky-600 text-white text-sm font-medium hover:bg-sky-700 disabled:opacity-50"
            >
              {searchingAllSummaries ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {searchAllProgress ?? '...'}
                </>
              ) : (
                <>
                  <Search className="w-4 h-4" />
                  Buscar resumo de todas (LLM)
                </>
              )}
            </button>
          )}
        </div>
        {items.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Nenhum item ainda. Use o campo acima e clique em &quot;Inserir com agente&quot;.
          </p>
        ) : (
          <ul className="space-y-2">
            {items.map((item) => {
              const it = item as BookFact & BookBibliographyEntry
              const isEditing = editingId === it.id
              const bib = it as BookBibliographyEntry
              const displayLine = isFacts
                ? it.text
                : [bib.author, bib.title, bib.year].filter(Boolean).join('. ') || bib.text
              return (
                <li
                  key={it.id}
                  className="flex items-start gap-2 p-2 rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700/50"
                >
                  {isEditing && isFacts ? (
                    <>
                      <input
                        type="text"
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        className="flex-1 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700"
                        autoFocus
                      />
                      <button type="button" onClick={saveEdit} className="p-1 text-emerald-600 hover:bg-emerald-100 dark:hover:bg-emerald-900/30 rounded" title="Salvar">
                        <Check className="w-4 h-4" />
                      </button>
                      <button type="button" onClick={() => { setEditingId(null); setEditValue(''); }} className="p-1 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-600 rounded" title="Cancelar">
                        <X className="w-4 h-4" />
                      </button>
                    </>
                  ) : (
                    <>
                      <div className="flex-1 min-w-0 flex flex-col gap-0.5">
                        <span className="text-sm text-gray-800 dark:text-gray-200">{displayLine}</span>
                        {!isFacts && (() => {
                          const used = (bib.used_in_chapters ?? []).length > 0
                          const chNums = (bib.used_in_chapters ?? []).map((c) => c + 1).sort((a, b) => a - b)
                          return (
                            <span
                              className={`text-xs font-medium shrink-0 ${
                                used
                                  ? 'text-emerald-700 dark:text-emerald-400'
                                  : 'text-gray-500 dark:text-gray-400'
                              }`}
                              title={used ? `Citada nos capítulos: ${chNums.join(', ')}` : 'Ainda não citada no texto'}
                            >
                              {used ? `Usada no texto${chNums.length ? ` (Cap. ${chNums.join(', ')})` : ''}` : 'Não usada no texto'}
                            </span>
                          )
                        })()}
                      </div>
                      <button type="button" onClick={() => startEdit(it.id, it.text, isFacts ? undefined : bib)} className="p-1 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-600 rounded" title="Editar">
                        <Pencil className="w-4 h-4" />
                      </button>
                      <button type="button" onClick={() => handleDelete(it.id)} className="p-1 text-red-600 hover:bg-red-100 dark:hover:bg-red-900/30 rounded" title="Excluir">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {/* Modal de edição da referência (bibliografia) */}
      {!isFacts && editEntry && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50" onClick={closeBibModal}>
          <div
            className="bg-white dark:bg-gray-800 rounded-xl shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto border border-gray-200 dark:border-gray-600"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="p-4 border-b border-gray-200 dark:border-gray-600 flex items-center justify-between sticky top-0 bg-white dark:bg-gray-800 z-10">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Editar referência</h3>
              <button type="button" onClick={closeBibModal} className="p-2 text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4 space-y-3 text-sm">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <input value={editEntry.author ?? ''} onChange={(e) => setEditEntry({ ...editEntry, author: e.target.value })} placeholder="Autor" className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white" />
                <input value={editEntry.title ?? ''} onChange={(e) => setEditEntry({ ...editEntry, title: e.target.value })} placeholder="Título" className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white" />
                <input value={editEntry.year ?? ''} onChange={(e) => setEditEntry({ ...editEntry, year: e.target.value })} placeholder="Ano" className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white w-24" />
                <input value={editEntry.publisher ?? ''} onChange={(e) => setEditEntry({ ...editEntry, publisher: e.target.value })} placeholder="Editora" className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white" />
                <select value={editEntry.entry_type ?? ''} onChange={(e) => setEditEntry({ ...editEntry, entry_type: e.target.value || undefined })} className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white">
                  <option value="">Tipo</option>
                  <option value="book">Livro</option>
                  <option value="article">Artigo</option>
                  <option value="web">Web</option>
                  <option value="other">Outro</option>
                </select>
              </div>
              <input value={editEntry.text ?? ''} onChange={(e) => setEditEntry({ ...editEntry, text: e.target.value })} placeholder="Descrição / texto" className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white" />
              <input value={editEntry.citation ?? ''} onChange={(e) => setEditEntry({ ...editEntry, citation: e.target.value })} placeholder="Citação curta" className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white" />
              <input value={editEntry.url ?? ''} onChange={(e) => setEditEntry({ ...editEntry, url: e.target.value })} placeholder="URL" type="url" className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white" />
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Resumo (usado pelo agente ao escrever seções)</label>
                <div className="flex gap-2">
                  <textarea
                    value={editEntry.summary ?? ''}
                    onChange={(e) => setEditEntry({ ...editEntry, summary: e.target.value })}
                    placeholder="Busque na internet ou digite um resumo da referência."
                    rows={4}
                    className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white resize-y min-h-[100px]"
                  />
                </div>
                <button
                  type="button"
                  onClick={handleSearchSummary}
                  disabled={searchingSummary || !jobId}
                  className="mt-2 inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-sky-600 text-white text-sm font-medium hover:bg-sky-700 disabled:opacity-50"
                >
                  {searchingSummary ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                  Buscar resumo na internet
                </button>
              </div>
            </div>
            <div className="p-4 border-t border-gray-200 dark:border-gray-600 flex justify-end gap-2">
              <button type="button" onClick={closeBibModal} className="px-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700">
                Cancelar
              </button>
              <button type="button" onClick={saveEdit} className="px-4 py-2 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 inline-flex items-center gap-2">
                <Check className="w-4 h-4" /> Salvar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

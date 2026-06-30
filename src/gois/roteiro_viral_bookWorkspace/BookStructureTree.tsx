import { useState } from 'react'
import { BookChapter, BookPlan, chapterHasChart, getChapterKey, sectionHasChart, subsectionHasChart } from './bookWorkspaceUtils'
import { BarChart2, ChevronDown, ChevronRight, Image, Languages } from 'lucide-react'

export interface BookStructureTreeProps {
  /** Plano do livro (title + structure/chapters) */
  draftPlan: BookPlan | null
  /** Altura mínima do container do grafo (ex: 400) */
  minHeight?: number
  /** Classe CSS adicional no container */
  className?: string
  /** Chaves de unidades traduzidas (meta, ch_0, sec_0_0, sub_0_0_0) para exibir ícone de tradução */
  translatedUnitKeys?: string[]
}

function safeLabel(s: string | undefined, fallback: string): string {
  if (s === undefined || s === null) return fallback
  const t = String(s).trim()
  return t || fallback
}

export function BookStructureTree({ draftPlan, minHeight = 420, className = '', translatedUnitKeys = [] }: BookStructureTreeProps) {
  const [expandedSubsections, setExpandedSubsections] = useState<Record<string, boolean>>({})
  const planKey = getChapterKey(draftPlan ?? undefined)
  const chapters = (draftPlan?.[planKey] as BookChapter[] | undefined) || []
  const bookTitle = safeLabel(draftPlan?.title, 'Livro')
  const hasTranslated = (key: string) => translatedUnitKeys.includes(key)

  const toggleSubsections = (chIdx: number, secIdx: number) => {
    const key = `${chIdx}-${secIdx}`
    setExpandedSubsections((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  if (!draftPlan) {
    return (
      <div className={`rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/50 p-6 ${className}`} style={{ minHeight }}>
        <p className="text-sm text-gray-500 dark:text-gray-400">Carregando plano do livro...</p>
      </div>
    )
  }

  if (chapters.length === 0) {
    return (
      <div className={`rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/50 p-6 ${className}`} style={{ minHeight }}>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Nenhum capítulo ainda. Gere capítulos com IA na aba Capítulos para ver a árvore aqui.
        </p>
      </div>
    )
  }

  return (
    <div
      className={`rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 overflow-auto ${className}`}
      style={{ minHeight }}
    >
      <div className="p-4">
        {/* Root: Livro */}
        <div className="flex items-start gap-0">
          <div className="flex flex-col items-center">
            <div className="w-8 h-4 border-l-2 border-t-2 border-gray-300 dark:border-gray-500 rounded-tl-md" />
            <div className="w-px flex-1 min-h-[8px] bg-gray-300 dark:bg-gray-500" />
          </div>
          <div className="flex-1 pb-4">
            <div className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-slate-100 dark:bg-slate-700 border border-slate-200 dark:border-slate-600 shadow-sm">
              <span className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Livro</span>
              <span className="text-sm font-semibold text-gray-900 dark:text-white">{bookTitle}</span>
            </div>
          </div>
        </div>

        {/* Chapters and sections */}
        {chapters.map((chapter, chIdx) => {
          const sections = chapter.sections || []
          const chapterTitle = safeLabel(chapter.title, `Capítulo ${chIdx + 1}`)
          const isLastChapter = chIdx === chapters.length - 1
          const hasSections = sections.length > 0
          const chapterHasImage = !!chapter.cover_path?.trim()
          const chapterHasCharts = chapterHasChart(chapter)

          return (
            <div key={chIdx} className="flex items-start gap-0">
              {/* Vertical line from book to this branch */}
              <div className="flex flex-col items-center w-8 shrink-0">
                {!isLastChapter ? (
                  <>
                    <div className="w-px h-3 bg-gray-300 dark:bg-gray-500" />
                    <div className="w-4 h-px bg-gray-300 dark:bg-gray-500" />
                    <div className="w-px flex-1 min-h-[12px] bg-gray-300 dark:bg-gray-500" />
                  </>
                ) : (
                  <>
                    <div className="w-px h-3 bg-gray-300 dark:bg-gray-500" />
                    <div className="w-4 h-px bg-gray-300 dark:bg-gray-500" />
                    {hasSections ? (
                      <div className="w-px flex-1 min-h-[12px] bg-gray-300 dark:bg-gray-500" />
                    ) : (
                      <div className="w-px h-4 bg-gray-300 dark:bg-gray-500" />
                    )}
                  </>
                )}
              </div>

              <div className="flex-1 min-w-0 pb-2">
                {/* Chapter node */}
                <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-700 shadow-sm mb-1">
                  <span className="text-xs font-medium text-blue-600 dark:text-blue-300">Cap. {chIdx + 1}</span>
                  <span className="text-sm font-medium text-gray-900 dark:text-white truncate" title={chapterTitle}>
                    {chapterTitle}
                  </span>
                  {chapterHasImage && (
                    <span title="Possui imagem/capa">
                      <Image className="w-3.5 h-3.5 shrink-0 text-blue-500 dark:text-blue-400" />
                    </span>
                  )}
                  {chapterHasCharts && (
                    <span title="Contém gráfico">
                      <BarChart2 className="w-3.5 h-3.5 shrink-0 text-blue-500 dark:text-blue-400" />
                    </span>
                  )}
                  {hasTranslated(`ch_${chIdx}`) && (
                    <span title="Traduzido">
                      <Languages className="w-3.5 h-3.5 shrink-0 text-indigo-500 dark:text-indigo-400" />
                    </span>
                  )}
                </div>

                {/* Sections and subsections */}
                {sections.map((section, secIdx) => {
                  const sectionTitle = safeLabel(section.title, `Seção ${secIdx + 1}`)
                  const subsections = section.subsections || []
                  const hasSubsections = subsections.length > 0
                  const isLastSection = secIdx === sections.length - 1
                  const isLastInBranch = isLastSection && !hasSubsections
                  const sectionHasImage = !!(section.image_path?.trim() || (section.images && section.images.length > 0))
                  const sectionHasCharts = sectionHasChart(section)

                  return (
                    <div key={secIdx} className="flex items-start gap-0 ml-4">
                      <div className="flex flex-col items-center w-6 shrink-0">
                        <div className="w-px h-2 bg-gray-300 dark:bg-gray-500" />
                        <div className="w-3 h-px bg-gray-300 dark:bg-gray-500" />
                        {!isLastInBranch ? (
                          <div className="w-px flex-1 min-h-[6px] bg-gray-300 dark:bg-gray-500" />
                        ) : (
                          <div className="w-px h-2 bg-gray-300 dark:bg-gray-500" />
                        )}
                      </div>
                      <div className="flex-1 py-1 min-w-0">
                        <div className="inline-flex items-center gap-2 px-2.5 py-1.5 rounded-md bg-gray-100 dark:bg-gray-700/80 border border-gray-200 dark:border-gray-600">
                          <span className="text-xs text-gray-500 dark:text-gray-400">{chIdx + 1}.{secIdx + 1}</span>
                          <span className="text-xs text-gray-800 dark:text-gray-200 truncate max-w-[280px]" title={sectionTitle}>
                            {sectionTitle}
                          </span>
                          {sectionHasImage && (
                            <span title="Possui imagem">
                              <Image className="w-3 h-3 shrink-0 text-gray-500 dark:text-gray-400" />
                            </span>
                          )}
                          {sectionHasCharts && (
                            <span title="Contém gráfico">
                              <BarChart2 className="w-3 h-3 shrink-0 text-gray-500 dark:text-gray-400" />
                            </span>
                          )}
                          {hasTranslated(`sec_${chIdx}_${secIdx}`) && (
                            <span title="Traduzido">
                              <Languages className="w-3 h-3 shrink-0 text-indigo-500 dark:text-indigo-400" />
                            </span>
                          )}
                        </div>
                        {/* Subsections: painel recolhido por padrão */}
                        {hasSubsections && (
                          <div className="ml-3 mt-1 rounded border border-gray-200 dark:border-gray-600 bg-gray-50/80 dark:bg-gray-800/50 overflow-hidden">
                            <button
                              type="button"
                              onClick={() => toggleSubsections(chIdx, secIdx)}
                              className="w-full flex items-center gap-1.5 px-2 py-1.5 text-left text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700/50"
                            >
                              {expandedSubsections[`${chIdx}-${secIdx}`] ? (
                                <ChevronDown className="w-3.5 h-3.5 shrink-0" />
                              ) : (
                                <ChevronRight className="w-3.5 h-3.5 shrink-0" />
                              )}
                              <span>Subseções ({subsections.length})</span>
                            </button>
                            {expandedSubsections[`${chIdx}-${secIdx}`] && (
                              <div className="border-t border-gray-200 dark:border-gray-600 pl-2 pr-2 pb-1.5 pt-0.5 space-y-0.5">
                                {subsections.map((sub, subIdx) => {
                                  const subLabel = safeLabel(sub.objective, `Subseção ${subIdx + 1}`)
                                  const isLastSub = subIdx === subsections.length - 1
                                  const subHasImage = !!(sub.images && sub.images.length > 0)
                                  const subHasCharts = subsectionHasChart(sub)
                                  return (
                                    <div key={subIdx} className="flex items-start gap-0">
                                      <div className="flex flex-col items-center w-4 shrink-0">
                                        <div className="w-px h-1.5 bg-gray-300 dark:bg-gray-500" />
                                        <div className="w-2 h-px bg-gray-300 dark:bg-gray-500" />
                                        {!isLastSub ? (
                                          <div className="w-px flex-1 min-h-[4px] bg-gray-300 dark:bg-gray-500" />
                                        ) : (
                                          <div className="w-px h-1.5 bg-gray-300 dark:bg-gray-500" />
                                        )}
                                      </div>
                                      <div className="flex-1 py-0.5 min-w-0">
                                        <div className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-gray-50 dark:bg-gray-800/80 border border-gray-200 dark:border-gray-600">
                                          <span className="text-[10px] text-gray-400 dark:text-gray-500">{chIdx + 1}.{secIdx + 1}.{subIdx + 1}</span>
                                          <span className="text-[11px] text-gray-600 dark:text-gray-300 truncate max-w-[240px]" title={subLabel}>
                                            {subLabel}
                                          </span>
                                          {subHasImage && (
                                            <span title="Possui imagem">
                                              <Image className="w-3 h-3 shrink-0 text-gray-500 dark:text-gray-400" />
                                            </span>
                                          )}
                                          {subHasCharts && (
                                            <span title="Contém gráfico">
                                              <BarChart2 className="w-3 h-3 shrink-0 text-gray-500 dark:text-gray-400" />
                                            </span>
                                          )}
                                          {hasTranslated(`sub_${chIdx}_${secIdx}_${subIdx}`) && (
                                            <span title="Traduzido">
                                              <Languages className="w-3 h-3 shrink-0 text-indigo-500 dark:text-indigo-400" />
                                            </span>
                                          )}
                                        </div>
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

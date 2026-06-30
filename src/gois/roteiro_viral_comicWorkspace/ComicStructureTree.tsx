import { useState } from 'react'
import { ChevronDown, ChevronRight, Image } from 'lucide-react'
import {
  ComicSagaPlan,
  pageHasImage,
  pagePanels,
  panelHasImage,
  safeComicLabel,
  sagaStories,
  storyHasContent,
  storyPages,
} from './comicWorkspaceUtils'

export interface ComicStructureTreeProps {
  sagaPlan: ComicSagaPlan | null
  minHeight?: number
  className?: string
}

export function ComicStructureTree({ sagaPlan, minHeight = 420, className = '' }: ComicStructureTreeProps) {
  const [expandedPanels, setExpandedPanels] = useState<Record<string, boolean>>({})
  const stories = sagaStories(sagaPlan)
  const sagaTitle = safeComicLabel(sagaPlan?.name, 'Saga')

  const togglePanels = (storyIdx: number, pageIdx: number) => {
    const key = `${storyIdx}-${pageIdx}`
    setExpandedPanels((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  if (!sagaPlan) {
    return (
      <div
        className={`rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/50 p-6 ${className}`}
        style={{ minHeight }}
      >
        <p className="text-sm text-gray-500 dark:text-gray-400">Carregando plano da saga...</p>
      </div>
    )
  }

  if (stories.length === 0) {
    return (
      <div
        className={`rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/50 p-6 ${className}`}
        style={{ minHeight }}
      >
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Nenhuma história ainda. Crie histórias na saga para ver a árvore aqui.
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
        <div className="flex items-start gap-0">
          <div className="flex flex-col items-center">
            <div className="w-8 h-4 border-l-2 border-t-2 border-gray-300 dark:border-gray-500 rounded-tl-md" />
            <div className="w-px flex-1 min-h-[8px] bg-gray-300 dark:bg-gray-500" />
          </div>
          <div className="flex-1 pb-4">
            <div className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-violet-100 dark:bg-violet-900/30 border border-violet-200 dark:border-violet-700 shadow-sm">
              <span className="text-xs font-semibold text-violet-600 dark:text-violet-300 uppercase tracking-wide">Saga</span>
              <span className="text-sm font-semibold text-gray-900 dark:text-white">{sagaTitle}</span>
            </div>
          </div>
        </div>

        {stories.map((story, stIdx) => {
          const pages = storyPages(story)
          const storyTitle = safeComicLabel(story.title, `História ${stIdx + 1}`)
          const isLastStory = stIdx === stories.length - 1
          const hasPages = pages.length > 0

          return (
            <div key={stIdx} className="flex items-start gap-0">
              <div className="flex flex-col items-center w-8 shrink-0">
                {!isLastStory ? (
                  <>
                    <div className="w-px h-3 bg-gray-300 dark:bg-gray-500" />
                    <div className="w-4 h-px bg-gray-300 dark:bg-gray-500" />
                    <div className="w-px flex-1 min-h-[12px] bg-gray-300 dark:bg-gray-500" />
                  </>
                ) : (
                  <>
                    <div className="w-px h-3 bg-gray-300 dark:bg-gray-500" />
                    <div className="w-4 h-px bg-gray-300 dark:bg-gray-500" />
                    {hasPages ? (
                      <div className="w-px flex-1 min-h-[12px] bg-gray-300 dark:bg-gray-500" />
                    ) : (
                      <div className="w-px h-4 bg-gray-300 dark:bg-gray-500" />
                    )}
                  </>
                )}
              </div>

              <div className="flex-1 min-w-0 pb-2">
                <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-700 shadow-sm mb-1">
                  <span className="text-xs font-medium text-blue-600 dark:text-blue-300">Hist. {stIdx + 1}</span>
                  <span className="text-sm font-medium text-gray-900 dark:text-white truncate" title={storyTitle}>
                    {storyTitle}
                  </span>
                  {storyHasContent(story) && (
                    <span className="text-[10px] text-blue-500 dark:text-blue-300">conteúdo</span>
                  )}
                </div>

                {pages.map((page, pgIdx) => {
                  const panels = pagePanels(page)
                  const pageTitle = safeComicLabel(page.title, `Página ${page.page_number ?? pgIdx + 1}`)
                  const hasPanels = panels.length > 0
                  const isLastPage = pgIdx === pages.length - 1
                  const isLastInBranch = isLastPage && !hasPanels
                  const pageImage = pageHasImage(page)

                  return (
                    <div key={pgIdx} className="flex items-start gap-0 ml-4">
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
                          <span className="text-xs text-gray-500 dark:text-gray-400">Pág. {page.page_number ?? pgIdx + 1}</span>
                          <span className="text-xs text-gray-800 dark:text-gray-200 truncate max-w-[280px]" title={pageTitle}>
                            {pageTitle}
                          </span>
                          {pageImage && (
                            <span title="Possui imagem">
                              <Image className="w-3 h-3 shrink-0 text-gray-500 dark:text-gray-400" />
                            </span>
                          )}
                        </div>

                        {hasPanels && (
                          <div className="ml-3 mt-1 rounded border border-gray-200 dark:border-gray-600 bg-gray-50/80 dark:bg-gray-800/50 overflow-hidden">
                            <button
                              type="button"
                              onClick={() => togglePanels(stIdx, pgIdx)}
                              className="w-full flex items-center gap-1.5 px-2 py-1.5 text-left text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700/50"
                            >
                              {expandedPanels[`${stIdx}-${pgIdx}`] ? (
                                <ChevronDown className="w-3.5 h-3.5 shrink-0" />
                              ) : (
                                <ChevronRight className="w-3.5 h-3.5 shrink-0" />
                              )}
                              <span>Painéis ({panels.length})</span>
                            </button>
                            {expandedPanels[`${stIdx}-${pgIdx}`] && (
                              <div className="border-t border-gray-200 dark:border-gray-600 pl-2 pr-2 pb-1.5 pt-0.5 space-y-0.5">
                                {panels.map((panel, pnIdx) => {
                                  const panelNum = panel.panel_number ?? pnIdx + 1
                                  const visual = (panel.visual_description || panel.visualDescription || '').trim()
                                  const panelLabel = visual.slice(0, 48) || `Painel ${panelNum}`
                                  const isLastPanel = pnIdx === panels.length - 1
                                  return (
                                    <div key={pnIdx} className="flex items-start gap-0">
                                      <div className="flex flex-col items-center w-4 shrink-0">
                                        <div className="w-px h-1.5 bg-gray-300 dark:bg-gray-500" />
                                        <div className="w-2 h-px bg-gray-300 dark:bg-gray-500" />
                                        {!isLastPanel ? (
                                          <div className="w-px flex-1 min-h-[4px] bg-gray-300 dark:bg-gray-500" />
                                        ) : (
                                          <div className="w-px h-1.5 bg-gray-300 dark:bg-gray-500" />
                                        )}
                                      </div>
                                      <div className="flex-1 py-0.5 min-w-0">
                                        <div className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
                                          <span className="text-[10px] text-amber-600 dark:text-amber-400">#{panelNum}</span>
                                          <span className="text-[11px] text-gray-600 dark:text-gray-300 truncate max-w-[240px]" title={panelLabel}>
                                            {panelLabel}
                                          </span>
                                          {panelHasImage(panel) && (
                                            <span title="Possui imagem">
                                              <Image className="w-3 h-3 shrink-0 text-amber-500 dark:text-amber-400" />
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

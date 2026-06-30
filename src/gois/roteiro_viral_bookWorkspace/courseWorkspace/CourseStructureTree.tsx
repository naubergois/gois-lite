import { useCallback, useEffect, useState } from 'react'
import {
  CourseLesson,
  CourseModule,
  CoursePlan,
  getModuleList,
  lessonHasContent,
  lessonHasExercises,
  lessonHasImages,
  lessonHasSlides,
  lessonHasText,
  moduleLessonItems,
  removeLessonSlide,
  updateLessonInPlan,
} from './courseWorkspaceUtils'
import { LessonContentPanel } from './LessonContentPanel'
import { ChevronDown, ChevronRight, FileText, HelpCircle, Image, Presentation } from 'lucide-react'

export interface CourseStructureTreeProps {
  draftPlan: CoursePlan | null
  courseId?: string
  onPlanChange?: (plan: CoursePlan) => void
  onSaveLesson?: (moduleIndex: number, lessonIndex: number, lesson: CourseLesson) => void | Promise<void>
  savingLesson?: boolean
  minHeight?: number
  className?: string
}

function safeLabel(s: string | undefined, fallback: string): string {
  if (s === undefined || s === null) return fallback
  const t = String(s).trim()
  return t || fallback
}

function LessonBadges({ lesson }: { lesson: CourseLesson }) {
  return (
    <span className="inline-flex items-center gap-1 ml-2">
      {lessonHasText(lesson) && (
        <span title="Texto" className="text-blue-500"><FileText className="w-3 h-3" /></span>
      )}
      {lessonHasSlides(lesson) && (
        <span title="Slides" className="text-violet-500"><Presentation className="w-3 h-3" /></span>
      )}
      {lessonHasExercises(lesson) && (
        <span title="Exercícios" className="text-amber-500"><HelpCircle className="w-3 h-3" /></span>
      )}
      {lessonHasImages(lesson) && (
        <span title="Imagens" className="text-emerald-500"><Image className="w-3 h-3" /></span>
      )}
    </span>
  )
}

type LessonSelection = { moduleIndex: number; lessonIndex: number }

export function CourseStructureTree({
  draftPlan,
  courseId,
  onPlanChange,
  onSaveLesson,
  savingLesson = false,
  minHeight = 420,
  className = '',
}: CourseStructureTreeProps) {
  const [expandedModules, setExpandedModules] = useState<Record<number, boolean>>({})
  const [selected, setSelected] = useState<LessonSelection | null>(null)
  const [localPlan, setLocalPlan] = useState<CoursePlan | null>(draftPlan)

  useEffect(() => {
    setLocalPlan(draftPlan)
  }, [draftPlan])

  const modules = getModuleList(localPlan)
  const courseTitle = safeLabel(localPlan?.course_title || localPlan?.title, 'Curso')

  const toggleModule = (idx: number) => {
    setExpandedModules((prev) => ({ ...prev, [idx]: !prev[idx] }))
  }

  const selectedLesson = (() => {
    if (!selected || !localPlan) return null
    const mod = modules[selected.moduleIndex]
    if (!mod) return null
    const lessons = moduleLessonItems(mod)
    return lessons[selected.lessonIndex] || null
  })()

  const patchLessonContent = useCallback(
    (content: string) => {
      if (!localPlan || !selected) return
      const next = updateLessonInPlan(localPlan, selected.moduleIndex, selected.lessonIndex, { content })
      setLocalPlan(next)
      onPlanChange?.(next)
    },
    [localPlan, onPlanChange, selected],
  )

  const handleSaveLesson = useCallback(async () => {
    if (!selected || !selectedLesson || !onSaveLesson) return
    await onSaveLesson(selected.moduleIndex, selected.lessonIndex, selectedLesson)
  }, [onSaveLesson, selected, selectedLesson])

  const handleDeleteSlide = useCallback(
    async (slideIndex: number) => {
      if (!localPlan || !selected || !selectedLesson || !onSaveLesson) return
      if (!window.confirm(`Remover o slide ${slideIndex} desta aula?`)) return
      const nextLesson = removeLessonSlide(selectedLesson, slideIndex)
      const nextPlan = updateLessonInPlan(localPlan, selected.moduleIndex, selected.lessonIndex, {
        generated_images: nextLesson.generated_images,
        gamma_slide_images: nextLesson.gamma_slide_images,
        gamma_code_slide_images: nextLesson.gamma_code_slide_images,
        images: nextLesson.images,
      })
      setLocalPlan(nextPlan)
      onPlanChange?.(nextPlan)
      const lessons = moduleLessonItems(nextPlan.modules?.[selected.moduleIndex] || {})
      await onSaveLesson(selected.moduleIndex, selected.lessonIndex, lessons[selected.lessonIndex] || nextLesson)
    },
    [localPlan, onPlanChange, onSaveLesson, selected, selectedLesson],
  )

  if (!localPlan) {
    return (
      <div className={`rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/50 p-6 ${className}`} style={{ minHeight }}>
        <p className="text-sm text-gray-500 dark:text-gray-400">Carregando plano do curso...</p>
      </div>
    )
  }

  if (modules.length === 0) {
    return (
      <div className={`rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/50 p-6 ${className}`} style={{ minHeight }}>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Nenhum módulo ainda. Gere módulos com IA para ver a árvore aqui.
        </p>
      </div>
    )
  }

  return (
    <div className={`space-y-4 ${className}`}>
      <div
        className="rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 overflow-auto"
        style={{ minHeight }}
      >
        <div className="p-4">
          <div className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-indigo-100 dark:bg-indigo-900/40 border border-indigo-200 dark:border-indigo-700 shadow-sm mb-4">
            <span className="text-xs font-semibold text-indigo-600 dark:text-indigo-300 uppercase tracking-wide">Curso</span>
            <span className="text-sm font-semibold text-gray-900 dark:text-white">{courseTitle}</span>
            {courseId && (
              <span className="text-xs text-gray-400 font-mono ml-2">{courseId}</span>
            )}
          </div>

          {modules.map((mod: CourseModule, modIdx) => {
            const lessons = moduleLessonItems(mod)
            const modTitle = safeLabel(mod.title, `Módulo ${modIdx + 1}`)
            const expanded = expandedModules[modIdx] !== false

            return (
              <div key={modIdx} className="mb-3 border-l-2 border-indigo-200 dark:border-indigo-700 pl-3 ml-1">
                <button
                  type="button"
                  onClick={() => toggleModule(modIdx)}
                  className="flex items-center gap-2 w-full text-left py-1.5 hover:bg-gray-50 dark:hover:bg-gray-700/50 rounded px-1"
                >
                  {expanded ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />}
                  <span className="text-xs font-medium text-indigo-500 dark:text-indigo-400">Módulo {modIdx + 1}</span>
                  <span className="text-sm font-medium text-gray-900 dark:text-white">{modTitle}</span>
                  <span className="text-xs text-gray-400 ml-auto">{lessons.length} aula(s)</span>
                </button>

                {expanded && (
                  <ul className="mt-1 ml-6 space-y-1">
                    {lessons.map((lesson, lesIdx) => {
                      const lesTitle = safeLabel(lesson.title, `Aula ${lesIdx + 1}`)
                      const filled = lessonHasContent(lesson)
                      const isSelected =
                        selected?.moduleIndex === modIdx && selected?.lessonIndex === lesIdx
                      return (
                        <li key={lesIdx}>
                          <button
                            type="button"
                            onClick={() => setSelected({ moduleIndex: modIdx, lessonIndex: lesIdx })}
                            className={`flex items-center gap-2 text-sm py-1 px-2 rounded w-full text-left ${
                              isSelected
                                ? 'bg-indigo-100 dark:bg-indigo-900/40 text-indigo-900 dark:text-indigo-100'
                                : filled
                                  ? 'text-gray-800 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700/50'
                                  : 'text-gray-400 dark:text-gray-500 italic hover:bg-gray-50 dark:hover:bg-gray-700/50'
                            }`}
                          >
                            <span className="text-xs text-gray-400 w-8 shrink-0">{modIdx + 1}.{lesIdx + 1}</span>
                            <span className="flex-1 truncate">{lesTitle}</span>
                            <LessonBadges lesson={lesson} />
                          </button>
                        </li>
                      )
                    })}
                    {lessons.length === 0 && (
                      <li className="text-xs text-gray-400 italic py-1">Sem aulas planeadas</li>
                    )}
                  </ul>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {selected && selectedLesson && (
        <div>
          <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
            Aula {selected.moduleIndex + 1}.{selected.lessonIndex + 1} — {safeLabel(selectedLesson.title, 'Sem título')}
          </h3>
          <LessonContentPanel
            lesson={selectedLesson}
            lessonLabel={`Aula ${selected.moduleIndex + 1}.${selected.lessonIndex + 1}`}
            content={selectedLesson.content || selectedLesson.detail || ''}
            onContentChange={patchLessonContent}
            onSave={onSaveLesson ? handleSaveLesson : undefined}
            onDeleteSlide={onSaveLesson ? handleDeleteSlide : undefined}
            saving={savingLesson}
          />
        </div>
      )}
    </div>
  )
}

export default CourseStructureTree

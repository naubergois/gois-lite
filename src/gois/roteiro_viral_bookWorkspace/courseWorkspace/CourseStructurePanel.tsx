import { useCallback, useMemo, useState } from 'react'
import { useJob } from '@/hooks/useJobs'
import { api, endpoints } from '@/lib/api'
import { normalizePlan } from '../bookWorkspaceUtils'
import { CourseStructureTree } from './CourseStructureTree'
import {
  CourseLesson,
  CoursePlan,
  isCoursePlan,
  planToCoursePlan,
  updateLessonInPlan,
} from './courseWorkspaceUtils'

export interface CourseStructurePanelProps {
  courseId?: string
  draftPlan?: CoursePlan | null
  minHeight?: number
  className?: string
  onPlanChange?: (plan: CoursePlan) => void
}

/** Painel de estrutura do curso — use em `/course?tab=structure` ou embed no chat. */
export function CourseStructurePanel({
  courseId,
  draftPlan: draftPlanProp,
  minHeight = 480,
  className = '',
  onPlanChange,
}: CourseStructurePanelProps) {
  const { job, refetch } = useJob(courseId)
  const [savingLesson, setSavingLesson] = useState(false)
  const { plan, planKey } = useMemo(() => normalizePlan(job?.final_state || {}), [job])
  const draftPlan = useMemo(() => {
    if (draftPlanProp) return draftPlanProp
    if (isCoursePlan(plan, planKey)) return planToCoursePlan(plan)
    return null
  }, [draftPlanProp, plan, planKey])

  const handleSaveLesson = useCallback(
    async (moduleIndex: number, lessonIndex: number, lesson: CourseLesson) => {
      const cid = (courseId || '').trim()
      if (!cid || !draftPlan) return
      setSavingLesson(true)
      try {
        const body = {
          title: lesson.title,
          content: lesson.content || '',
          detail: lesson.detail || lesson.content || '',
          generated_images: lesson.generated_images || [],
          gamma_slide_images: lesson.gamma_slide_images || [],
          gamma_code_slide_images: lesson.gamma_code_slide_images || [],
          images: lesson.images || [],
        }
        await api.put(`/courses/${cid}/modules/${moduleIndex}/lessons/${lessonIndex}`, body)
        const nextPlan = updateLessonInPlan(draftPlan, moduleIndex, lessonIndex, {
          content: body.content,
          detail: body.detail,
        })
        onPlanChange?.(nextPlan)
        await endpoints.jobs.update(cid, {
          final_state: {
            ...(job?.final_state || {}),
            course_plan: nextPlan,
            modules: nextPlan.modules,
          },
        })
        await refetch?.(true)
      } finally {
        setSavingLesson(false)
      }
    },
    [courseId, draftPlan, job?.final_state, onPlanChange, refetch],
  )

  return (
    <div className={`space-y-4 ${className}`}>
      <div>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Estrutura do curso</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Clique numa aula para editar o HTML. Imagens usam tags{' '}
          <code className="text-xs bg-gray-100 dark:bg-gray-800 px-1 rounded">[IMAGE:n]</code>
          {' '}no editor.
        </p>
      </div>
      <CourseStructureTree
        draftPlan={draftPlan}
        courseId={courseId}
        onPlanChange={onPlanChange}
        onSaveLesson={courseId ? handleSaveLesson : undefined}
        savingLesson={savingLesson}
        minHeight={minHeight}
      />
    </div>
  )
}

export default CourseStructurePanel

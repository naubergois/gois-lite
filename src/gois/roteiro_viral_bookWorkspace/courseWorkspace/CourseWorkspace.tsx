import { Suspense, useCallback, useMemo, useState, type ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import { CourseStructurePanel } from './CourseStructurePanel'
import { CourseStructurePanel } from './CourseStructurePanel'
import { planToCoursePlan } from './courseWorkspaceUtils'
import { WorkspaceTabs, type CourseWorkspaceTab } from './WorkspaceTabs'

export interface CourseWorkspaceProps {
  courseId?: string
  topic?: string
  initialTab?: CourseWorkspaceTab
  plan?: unknown
  onTabChange?: (tab: CourseWorkspaceTab) => void
}

function TabPlaceholder({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-8 text-center text-gray-600">
      <p className="font-medium text-gray-800">{title}</p>
      <p className="mt-2 text-sm">{hint}</p>
    </div>
  )
}

function tabFromQuery(raw: string | undefined): CourseWorkspaceTab {
  const id = (raw || 'wizard').trim().toLowerCase() as CourseWorkspaceTab
  const allowed = new Set([
    'wizard', 'modules', 'lessons', 'structure', 'code', 'slides',
    'exercises', 'images', 'gamma', 'heygen', 'export', 'library',
  ])
  return allowed.has(id) ? id : 'wizard'
}

export function CourseWorkspace({
  courseId = '',
  topic = '',
  initialTab,
  plan,
  onTabChange,
}: CourseWorkspaceProps) {
  const [activeTab, setActiveTab] = useState<CourseWorkspaceTab>(
    tabFromQuery(initialTab),
  )

  const coursePlan = useMemo(() => planToCoursePlan(plan), [plan])

  const switchTab = useCallback(
    (tab: CourseWorkspaceTab) => {
      setActiveTab(tab)
      onTabChange?.(tab)
    },
    [onTabChange],
  )

  let panel: ReactNode
  switch (activeTab) {
    case 'wizard':
      panel = (
        <TabPlaceholder
          title="Wizard — novo curso"
          hint={
            topic
              ? `Tema: ${topic}. Configure módulos, público e dificuldade.`
              : 'Defina tema, público-alvo, número de módulos e dificuldade.'
          }
        />
      )
      break
    case 'modules':
      panel = (
        <TabPlaceholder
          title="Módulos"
          hint="Planeie e edite módulos do curso. Use «Gerar estrutura» no chat ou pipeline RV."
        />
      )
      break
    case 'lessons':
      panel = courseId || coursePlan ? (
        <CourseStructurePanel
          courseId={courseId || undefined}
          draftPlan={coursePlan}
          minHeight={520}
        />
      ) : (
        <TabPlaceholder
          title="Aulas"
          hint="Expanda módulos em aulas e edite conteúdo HTML por aula (tags [IMAGE:n] para imagens)."
        />
      )
      break
    case 'structure':
      panel = courseId || coursePlan ? (
        <CourseStructurePanel courseId={courseId || undefined} draftPlan={coursePlan} />
      ) : (
        <TabPlaceholder
          title="Estrutura do curso"
          hint={
            courseId
              ? `Carregue o curso ${courseId} para ver a árvore módulos → aulas.`
              : 'Crie ou carregue um curso para ver a árvore.'
          }
        />
      )
      break
    case 'code':
      panel = (
        <TabPlaceholder
          title="Code Studio"
          hint="Slides de código, snippets e imagens didáticas por aula."
        />
      )
      break
    case 'slides':
      panel = (
        <TabPlaceholder
          title="Slides visuais"
          hint="Prompts de slide, estilos visuais e geração de imagens por aula."
        />
      )
      break
    case 'exercises':
      panel = (
        <TabPlaceholder
          title="Exercícios"
          hint="Questões, gabarito e banco de exercícios por aula."
        />
      )
      break
    case 'images':
      panel = (
        <TabPlaceholder
          title="Imagens"
          hint="Ilustrações e assets visuais associados às aulas."
        />
      )
      break
    case 'gamma':
      panel = (
        <TabPlaceholder
          title="Gamma"
          hint="Apresentações editáveis exportadas para Gamma."
        />
      )
      break
    case 'heygen':
      panel = (
        <TabPlaceholder
          title="HeyGen"
          hint="Vídeos avatar por aula via HeyGen."
        />
      )
      break
    case 'export':
      panel = (
        <TabPlaceholder
          title="Exportar"
          hint="Portal HTML, PDF e pacotes de publicação do curso."
        />
      )
      break
    case 'library':
      panel = (
        <TabPlaceholder
          title="Biblioteca de cursos"
          hint="Abra /courses para listar, pesquisar e retomar cursos existentes."
        />
      )
      break
    default:
      panel = null
  }

  return (
    <div className="flex flex-col gap-4">
      <WorkspaceTabs activeTab={activeTab} setActiveTab={switchTab} />
      <Suspense
        fallback={
          <div className="flex items-center gap-2 text-gray-500">
            <Loader2 className="w-4 h-4 animate-spin" />
            A carregar…
          </div>
        }
      >
        {panel}
      </Suspense>
    </div>
  )
}

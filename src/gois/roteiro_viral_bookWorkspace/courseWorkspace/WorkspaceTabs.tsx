import {
  BookOpen,
  Code,
  Download,
  GitBranch,
  HelpCircle,
  Image as ImageIcon,
  Layers,
  Library,
  Palette,
  Sparkles,
  Video,
  Wand2,
} from 'lucide-react'
import { cn } from '@/lib/utils'

export type CourseWorkspaceTab =
  | 'wizard'
  | 'modules'
  | 'lessons'
  | 'structure'
  | 'code'
  | 'slides'
  | 'exercises'
  | 'images'
  | 'gamma'
  | 'heygen'
  | 'export'
  | 'library'

interface WorkspaceTabsProps {
  activeTab: CourseWorkspaceTab
  setActiveTab: (tab: CourseWorkspaceTab) => void
}

const TABS: Array<{ id: CourseWorkspaceTab; label: string; icon: typeof Wand2 }> = [
  { id: 'wizard', label: 'Wizard', icon: Wand2 },
  { id: 'modules', label: 'Módulos', icon: Layers },
  { id: 'lessons', label: 'Aulas', icon: BookOpen },
  { id: 'structure', label: 'Estrutura', icon: GitBranch },
  { id: 'code', label: 'Code Studio', icon: Code },
  { id: 'slides', label: 'Slides', icon: ImageIcon },
  { id: 'exercises', label: 'Exercícios', icon: HelpCircle },
  { id: 'images', label: 'Imagens', icon: Palette },
  { id: 'gamma', label: 'Gamma', icon: Sparkles },
  { id: 'heygen', label: 'HeyGen', icon: Video },
  { id: 'export', label: 'Exportar', icon: Download },
  { id: 'library', label: 'Biblioteca', icon: Library },
]

export function WorkspaceTabs({ activeTab, setActiveTab }: WorkspaceTabsProps) {
  return (
    <div className="flex gap-2 flex-wrap">
      {TABS.map((tab) => {
        const Icon = tab.icon
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={cn(
              'px-4 py-2 rounded-lg border text-sm flex items-center gap-2',
              activeTab === tab.id
                ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                : 'text-gray-600 border-gray-200 hover:bg-gray-50',
            )}
          >
            <Icon className="w-4 h-4" />
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}

export { TABS as COURSE_WORKSPACE_TABS }

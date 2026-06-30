import { BookMarked, Image as ImageIcon, LayoutList, Pencil, Settings, GitBranch, Database, BookOpen, ListTree } from 'lucide-react'
import { cn } from '@/lib/utils'

type WorkspaceTab = 'chapters' | 'structure' | 'design' | 'assembly' | 'section' | 'subsections' | 'metadata' | 'facts' | 'bibliography'

interface WorkspaceTabsProps {
  activeTab: WorkspaceTab
  setActiveTab: (tab: WorkspaceTab) => void
}

const TABS: Array<{ id: WorkspaceTab; label: string; icon: any }> = [
  { id: 'chapters', label: 'Capitulos', icon: LayoutList },
  { id: 'subsections', label: 'Subsecoes', icon: ListTree },
  { id: 'structure', label: 'Estrutura', icon: GitBranch },
  { id: 'facts', label: 'Base de fatos', icon: Database },
  { id: 'bibliography', label: 'Base de bibliografia', icon: BookOpen },
  { id: 'design', label: 'Design', icon: ImageIcon },
  { id: 'assembly', label: 'Assembly', icon: BookMarked },
  { id: 'section', label: 'Editor de Secao', icon: Pencil },
  { id: 'metadata', label: 'Metadados', icon: Settings },
]

export function WorkspaceTabs({ activeTab, setActiveTab }: WorkspaceTabsProps) {
  return (
    <div className="flex gap-2 flex-wrap">
      {TABS.map((tab) => {
        const Icon = tab.icon
        return (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={cn(
              'px-4 py-2 rounded-lg border text-sm flex items-center gap-2',
              activeTab === tab.id
                ? 'bg-blue-50 text-blue-700 border-blue-200'
                : 'text-gray-600 border-gray-200 hover:bg-gray-50'
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

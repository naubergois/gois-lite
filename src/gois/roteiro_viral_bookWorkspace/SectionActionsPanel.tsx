import { Settings } from 'lucide-react'

type SectionActionsPanelProps = {
  selectedSectionIdx: number
  currentSectionsLength: number
  currentSectionTitle?: string
  onMove: (from: number, to: number) => void
  onInsertAt: (index: number) => void
  onClear: (index: number) => void
  onDelete: (index: number) => void
}

export function SectionActionsPanel({
  selectedSectionIdx,
  currentSectionsLength,
  currentSectionTitle,
  onMove,
  onInsertAt,
  onClear,
  onDelete,
}: SectionActionsPanelProps) {
  const canDelete = currentSectionsLength > 1

  return (
    <div className="bg-white border rounded-lg p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Settings className="w-4 h-4" />
        Acoes da Secao
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => onMove(selectedSectionIdx, selectedSectionIdx - 1)}
          className="px-3 py-2 border rounded-md text-sm"
          disabled={selectedSectionIdx === 0}
        >
          Mover para cima
        </button>
        <button
          onClick={() => onMove(selectedSectionIdx, selectedSectionIdx + 1)}
          className="px-3 py-2 border rounded-md text-sm"
          disabled={selectedSectionIdx >= currentSectionsLength - 1}
        >
          Mover para baixo
        </button>
        <button
          onClick={() => onInsertAt(selectedSectionIdx)}
          className="px-3 py-2 border rounded-md text-sm"
        >
          Inserir antes
        </button>
        <button
          onClick={() => onInsertAt(selectedSectionIdx + 1)}
          className="px-3 py-2 border rounded-md text-sm"
        >
          Inserir depois
        </button>
        <button
          onClick={() => onClear(selectedSectionIdx)}
          className="px-3 py-2 border rounded-md text-sm"
        >
          Limpar conteudo
        </button>
        <button
          onClick={() => {
            if (!canDelete) return
            const title = currentSectionTitle || `Secao ${selectedSectionIdx + 1}`
            if (window.confirm(`Excluir a secao "${title}"?`)) {
              onDelete(selectedSectionIdx)
            }
          }}
          disabled={!canDelete}
          className="px-3 py-2 border rounded-md text-sm text-red-500 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Excluir Secao
        </button>
      </div>
    </div>
  )
}

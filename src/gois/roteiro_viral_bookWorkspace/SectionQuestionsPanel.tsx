import { HelpCircle, Loader2, Trash2, Wand2 } from 'lucide-react'

import QuestionStyleSelector, { DEFAULT_QUESTION_CONFIG, type QuestionConfig } from '@/components/QuestionStyleSelector'
import type { BookSection } from './bookWorkspaceUtils'

type SectionQuestionsPanelProps = {
  currentSection?: BookSection
  selectedSectionIdx: number
  isGeneratingQuestions: boolean
  onGenerateQuestions: () => void
  onUpdateSectionAtIndex: (index: number, patch: Partial<BookSection>) => void
  onSavePlan: () => Promise<void> | void
  onAppendQuestionsToContent: () => void
}

export function SectionQuestionsPanel({
  currentSection,
  selectedSectionIdx,
  isGeneratingQuestions,
  onGenerateQuestions,
  onUpdateSectionAtIndex,
  onSavePlan,
  onAppendQuestionsToContent,
}: SectionQuestionsPanelProps) {
  return (
    <div className="bg-white dark:bg-gray-800 border rounded-lg p-6 space-y-4">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <HelpCircle className="w-4 h-4" />
        Questoes de Estudo
      </div>

      <QuestionStyleSelector
        config={{
          boardId: currentSection?.question_board || DEFAULT_QUESTION_CONFIG.boardId,
          questionType: currentSection?.question_type || DEFAULT_QUESTION_CONFIG.questionType,
          difficulty: currentSection?.question_difficulty || DEFAULT_QUESTION_CONFIG.difficulty,
          numQuestions: currentSection?.num_questions || DEFAULT_QUESTION_CONFIG.numQuestions,
          includeAnswers: currentSection?.question_include_answers !== false,
          includeExplanation: currentSection?.question_include_explanation !== false,
        }}
        onChange={(qCfg: QuestionConfig) => {
          onUpdateSectionAtIndex(selectedSectionIdx, {
            question_board: qCfg.boardId,
            question_type: qCfg.questionType,
            question_difficulty: qCfg.difficulty,
            num_questions: qCfg.numQuestions,
            question_include_answers: qCfg.includeAnswers,
            question_include_explanation: qCfg.includeExplanation,
          })
        }}
      />

      <div className="flex items-center gap-2">
        <button
          onClick={onGenerateQuestions}
          disabled={isGeneratingQuestions || !(currentSection?.content?.trim())}
          className="px-3 py-2 bg-emerald-600 text-white rounded-md text-sm flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
          title={!(currentSection?.content?.trim()) ? 'Preencha o conteudo da secao antes de gerar questoes' : `Gerar ${currentSection?.num_questions || 5} questoes`}
        >
          {isGeneratingQuestions ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Gerando questoes...
            </>
          ) : (
            <>
              <Wand2 className="w-4 h-4" />
              Gerar Questoes
            </>
          )}
        </button>
        {currentSection?.questions && (
          <button
            onClick={() => {
              onUpdateSectionAtIndex(selectedSectionIdx, { questions: '' })
              void onSavePlan()
            }}
            className="px-3 py-2 border rounded-md text-sm text-red-500 flex items-center gap-2 hover:bg-red-50"
          >
            <Trash2 className="w-4 h-4" />
            Limpar
          </button>
        )}
      </div>

      {currentSection?.questions && (
        <div className="space-y-2">
          <label className="text-xs font-medium text-gray-600">Questoes Geradas</label>
          <textarea
            value={currentSection.questions}
            onChange={(e) => {
              onUpdateSectionAtIndex(selectedSectionIdx, { questions: e.target.value })
            }}
            rows={12}
            className="w-full px-3 py-2 border rounded-md text-sm font-mono"
            placeholder="As questoes geradas aparecerao aqui..."
          />
          <button
            onClick={() => {
              onAppendQuestionsToContent()
              void onSavePlan()
            }}
            className="text-xs text-emerald-600 hover:underline"
          >
            Inserir no texto da secao
          </button>
        </div>
      )}
    </div>
  )
}

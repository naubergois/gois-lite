import { useCallback, useMemo, useRef } from 'react'
import { Image as ImageIcon, Loader2, Save, Trash2 } from 'lucide-react'
import { buildFileUrl } from '@/lib/files'
import {
  insertImageTagAt,
  isLikelyHtmlContent,
  lessonImageEntries,
  lessonPreviewHtml,
  lessonSlideEntries,
  type CourseLesson,
} from './courseWorkspaceUtils'

export interface LessonContentPanelProps {
  lesson: CourseLesson
  lessonLabel: string
  content: string
  onContentChange: (next: string) => void
  onSave?: () => void | Promise<void>
  saving?: boolean
  onDeleteSlide?: (slideIndex: number) => void | Promise<void>
}

export function LessonContentPanel({
  lesson,
  lessonLabel,
  content,
  onContentChange,
  onSave,
  saving = false,
  onDeleteSlide,
}: LessonContentPanelProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const images = useMemo(() => lessonImageEntries(lesson), [lesson])
  const slides = useMemo(() => lessonSlideEntries(lesson), [lesson])

  const previewHtml = useMemo(
    () => lessonPreviewHtml(content, images, lessonLabel, buildFileUrl),
    [content, images, lessonLabel],
  )

  const insertTag = useCallback(
    (imageNum: number) => {
      const el = textareaRef.current
      const cursor = el && typeof el.selectionStart === 'number' ? el.selectionStart : null
      onContentChange(insertImageTagAt(content, imageNum, cursor))
    },
    [content, onContentChange],
  )

  const insertTagForPath = useCallback(
    (path: string) => {
      const idx = images.findIndex((img) => (img.path || '').trim() === path.trim())
      insertTag(idx >= 0 ? idx + 1 : 1)
    },
    [images, insertTag],
  )

  const isHtml = isLikelyHtmlContent(content)

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/40 p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-gray-600 dark:text-gray-400">
          Conteúdo em <strong>HTML</strong> — tags{' '}
          <code className="text-[11px] bg-gray-200/80 dark:bg-gray-700 px-1 rounded">[IMAGE:1]</code>,{' '}
          <code className="text-[11px] bg-gray-200/80 dark:bg-gray-700 px-1 rounded">[IMAGE:2]</code>
          {' '}para posicionar imagens
        </p>
        {onSave && (
          <button
            type="button"
            onClick={() => void onSave()}
            disabled={saving}
            className="px-2 py-1 text-xs font-medium rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-60 flex items-center gap-1"
          >
            {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
            Salvar
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 min-h-[220px]">
        <div className="flex flex-col gap-1 min-h-[200px]">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
            HTML {isHtml ? '' : '(legado — converta para HTML)'}
          </span>
          <textarea
            ref={textareaRef}
            value={content}
            onChange={(e) => onContentChange(e.target.value)}
            rows={14}
            className="flex-1 w-full font-mono text-sm px-3 py-2 border rounded-lg dark:bg-gray-900 dark:border-gray-600 resize-y min-h-[200px]"
            placeholder="<h3>Título</h3><p>Texto da aula…</p>"
            spellCheck={false}
          />
        </div>
        <div className="flex flex-col gap-1 min-h-[200px]">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
            Preview
          </span>
          <div
            className="flex-1 min-h-[200px] max-h-[420px] overflow-y-auto p-4 border rounded-lg bg-white dark:bg-gray-900 prose dark:prose-invert max-w-none text-sm lesson-html-preview"
            dangerouslySetInnerHTML={{
              __html: previewHtml.trim() || '<p><em>Sem conteúdo</em></p>',
            }}
          />
        </div>
      </div>

      {images.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-gray-200 dark:border-gray-700">
          <span className="text-xs text-gray-500 flex items-center gap-1">
            <ImageIcon className="w-3 h-3" />
            Inserir no texto ({images.length}):
          </span>
          {images.map((img, idx) => (
            <button
              key={`${img.path}-${idx}`}
              type="button"
              onClick={() => insertTag(idx + 1)}
              className="px-2 py-0.5 text-xs rounded border border-indigo-200 dark:border-indigo-700 text-indigo-700 dark:text-indigo-300 hover:bg-indigo-50 dark:hover:bg-indigo-900/30"
              title={img.caption || img.path}
            >
              [IMAGE:{idx + 1}]
            </button>
          ))}
        </div>
      )}

      {slides.length > 0 && (
        <div className="pt-2 border-t border-gray-200 dark:border-gray-700 space-y-2">
          <span className="text-xs text-gray-500">Slides da aula ({slides.length})</span>
          <div className="flex gap-3 overflow-x-auto pb-1">
            {slides.map((slide, idx) => (
              <div
                key={`${slide.path}-${idx}`}
                className="flex-shrink-0 w-40 rounded-lg border border-gray-200 dark:border-gray-600 overflow-hidden bg-white dark:bg-gray-900"
              >
                <img
                  src={buildFileUrl(slide.path)}
                  alt={slide.caption || `Slide ${idx + 1}`}
                  className="w-full h-24 object-contain bg-gray-100 dark:bg-gray-800"
                  loading="lazy"
                />
                <div className="p-2 space-y-1">
                  <p className="text-[10px] text-gray-500 truncate" title={slide.caption || slide.path}>
                    {slide.caption || `Slide ${idx + 1}`}
                  </p>
                  <div className="flex flex-wrap gap-1">
                    <button
                      type="button"
                      onClick={() => insertTagForPath(slide.path || '')}
                      className="px-1.5 py-0.5 text-[10px] rounded border border-indigo-200 dark:border-indigo-700 text-indigo-700 dark:text-indigo-300"
                    >
                      No HTML
                    </button>
                    {onDeleteSlide && (
                      <button
                        type="button"
                        onClick={() => void onDeleteSlide(idx + 1)}
                        className="px-1.5 py-0.5 text-[10px] rounded border border-red-200 dark:border-red-800 text-red-700 dark:text-red-300 inline-flex items-center gap-0.5"
                        title="Remover slide"
                      >
                        <Trash2 className="w-3 h-3" />
                        Apagar
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default LessonContentPanel

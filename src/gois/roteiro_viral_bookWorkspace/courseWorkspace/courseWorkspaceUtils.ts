/**
 * Course hierarchy — mirrors book structure:
 * CoursePlan → CourseModule → CourseLesson (text, slides, exercises, images)
 */

import type { CodeBlock, SectionImage, SectionSlidePromptItem } from '../bookWorkspaceUtils'

export type { SectionImage, CodeBlock, SectionSlidePromptItem }

export interface CourseLesson {
  lesson_number?: number
  section_number?: number
  title?: string
  objective?: string
  content_directive?: string
  content?: string
  detail?: string
  slide_prompts?: SectionSlidePromptItem[]
  planned_prompts?: (string | SectionSlidePromptItem)[]
  slides_text?: (string | Record<string, unknown>)[]
  code_slides_text?: Record<string, unknown>[]
  images?: SectionImage[]
  generated_images?: string[]
  gamma_slide_images?: string[]
  gamma_code_slide_images?: string[]
  image_path?: string
  questions?: string
  num_questions?: number
  question_type?: string
  question_difficulty?: string
  question_include_answers?: boolean
  question_include_explanation?: boolean
  code_blocks?: CodeBlock[]
  video_url?: string
  lesson_audio_path?: string
  slide_audio_paths?: string[]
  heygen_script?: string
  source_code?: string
  code_snippet?: string
  content_format?: string
  html_content?: string

export interface CourseModule {
  module_number?: number
  title?: string
  objective?: string
  purpose?: string
  description?: string
  introduction?: string
  cover_path?: string
  lessons?: CourseLesson[] | number
  lessons_list?: string[]
  lessons_details?: CourseLesson[]
  sections?: CourseLesson[]
}

export interface CoursePlan {
  course_title?: string
  title?: string
  course_description?: string
  description?: string
  course_objectives?: string[]
  objective?: string
  draft?: string
  target_audience?: string
  language?: string
  difficulty?: string
  cover_path?: string
  modules?: CourseModule[]
  num_modules?: number
  num_lessons_per_module?: number
  default_min_text_length?: number
  course_slide_styles?: string[]
  global_lesson_prompt?: string
}

export function isCoursePlan(plan: unknown, planKey?: string): boolean {
  if (planKey === 'course_plan') return true
  if (!plan || typeof plan !== 'object') return false
  const p = plan as CoursePlan
  const modules = p.modules
  if (!Array.isArray(modules) || modules.length === 0) return false
  const bookPlan = p as CoursePlan & { chapters?: unknown[]; structure?: unknown[] }
  const hasBookTree = !!(bookPlan.chapters?.length || bookPlan.structure?.length)
  return !hasBookTree || !!p.course_title
}

export function getModuleList(plan?: CoursePlan | null): CourseModule[] {
  return (plan?.modules as CourseModule[] | undefined) || []
}

export function moduleLessonItems(mod: CourseModule): CourseLesson[] {
  const raw = mod.lessons
  if (Array.isArray(raw) && raw.length > 0 && typeof raw[0] === 'object') {
    return raw as CourseLesson[]
  }
  const sections = mod.sections || []
  const details = mod.lessons_details || []
  const titles = mod.lessons_list || []
  const count = Math.max(sections.length, details.length, titles.length)
  const out: CourseLesson[] = []
  for (let i = 0; i < count; i++) {
    const sec = sections[i] || {}
    const det = details[i] || {}
    out.push({
      ...det,
      ...sec,
      lesson_number: sec.lesson_number ?? sec.section_number ?? i + 1,
      title: det.title || sec.title || titles[i] || `Aula ${i + 1}`,
      content: det.content || sec.content || det.detail || sec.detail || '',
    })
  }
  return out
}

export function lessonHasSlides(lesson: CourseLesson): boolean {
  return !!(
    (lesson.slide_prompts && lesson.slide_prompts.length > 0) ||
    (lesson.planned_prompts && lesson.planned_prompts.length > 0) ||
    (lesson.slides_text && lesson.slides_text.length > 0)
  )
}

export function lessonHasImages(lesson: CourseLesson): boolean {
  return !!(
    (lesson.images && lesson.images.length > 0) ||
    (lesson.generated_images && lesson.generated_images.length > 0) ||
    lesson.image_path?.trim()
  )
}

export function lessonHasExercises(lesson: CourseLesson): boolean {
  return !!(lesson.questions?.trim() || (lesson.num_questions && lesson.num_questions > 0))
}

export function lessonHasText(lesson: CourseLesson): boolean {
  return !!(lesson.content?.trim() || lesson.detail?.trim())
}

export function lessonHasContent(lesson: CourseLesson): boolean {
  return lessonHasText(lesson) || lessonHasSlides(lesson) || lessonHasImages(lesson) || lessonHasExercises(lesson)
}

export function moduleHasContent(mod: CourseModule): boolean {
  if (mod.introduction?.trim()) return true
  return moduleLessonItems(mod).some(lessonHasContent)
}

export function planToCoursePlan(plan: unknown): CoursePlan | null {
  if (!plan || typeof plan !== 'object') return null
  return plan as CoursePlan
}

/** Imagens da aula (galeria) na ordem usada por [IMAGE:1], [IMAGE:2], … */
export function lessonImageEntries(lesson: CourseLesson): SectionImage[] {
  const out: SectionImage[] = []
  const seen = new Set<string>()

  const add = (raw: string | SectionImage | undefined, caption?: string) => {
    if (!raw) return
    const path = typeof raw === 'string' ? raw.trim() : (raw.path || '').trim()
    if (!path || seen.has(path)) return
    seen.add(path)
    if (typeof raw === 'object') {
      out.push(raw)
    } else {
      out.push({ path, caption: caption || '' })
    }
  }

  for (const img of lesson.images || []) {
    add(img as string | SectionImage)
  }
  for (let i = 0; i < (lesson.generated_images || []).length; i++) {
    const path = lesson.generated_images![i]
    add(path, `Slide ${i + 1}`)
  }
  add(lesson.image_path, 'Imagem')
  return out
}

/** Slides visuais da aula (gerados + Gamma), na ordem do painel de slides. */
export function lessonSlideEntries(lesson: CourseLesson): SectionImage[] {
  const out: SectionImage[] = []
  const seen = new Set<string>()

  const add = (raw: string | undefined, caption: string) => {
    const path = (raw || '').trim()
    if (!path || seen.has(path)) return
    seen.add(path)
    out.push({ path, caption })
  }

  for (let i = 0; i < (lesson.generated_images || []).length; i++) {
    add(lesson.generated_images![i], `Slide ${i + 1}`)
  }
  for (let i = 0; i < (lesson.gamma_slide_images || []).length; i++) {
    add(lesson.gamma_slide_images![i], `Gamma ${i + 1}`)
  }
  for (let i = 0; i < (lesson.gamma_code_slide_images || []).length; i++) {
    add(lesson.gamma_code_slide_images![i], `Código ${i + 1}`)
  }
  return out
}

/** Remove slide pelo índice 1-based na lista combinada de slides. */
export function removeLessonSlide(lesson: CourseLesson, slideIndex: number): CourseLesson {
  const slides = lessonSlideEntries(lesson)
  const idx = slideIndex - 1
  if (idx < 0 || idx >= slides.length) return lesson
  const path = (slides[idx].path || '').trim()
  if (!path) return lesson
  const strip = (items?: string[]) =>
    (items || []).filter((p) => String(p).trim() !== path)
  return {
    ...lesson,
    generated_images: strip(lesson.generated_images),
    gamma_slide_images: strip(lesson.gamma_slide_images),
    gamma_code_slide_images: strip(lesson.gamma_code_slide_images),
    images: (lesson.images || []).filter((img) => {
      const p = typeof img === 'string' ? img : (img.path || '')
      return String(p).trim() !== path
    }),
  }
}

const IMAGE_TAG_RE = /\[IMAGE(?:_TRANSPARENT|_WITHOUT_TEXT|_PROMPT)?:\s*([^\]]+)\]/gi

/** Substitui tags [IMAGE:n] por markdown ![legenda](url) para preview. */
export function applyImageMarkersForPreview(
  content: string,
  images: SectionImage[],
  sectionLabel: string,
  buildUrl: (path: string) => string,
): string {
  if (!content || !images.length || !content.includes('[IMAGE:')) return content || ''
  let nextOrder = 0
  const replacements = images.map((img, idx) => {
    const baseCaption = img.caption || `Imagem ${idx + 1}`
    const caption =
      baseCaption.toLowerCase().startsWith('seção') ||
      baseCaption.toLowerCase().startsWith('secao') ||
      baseCaption.toLowerCase().startsWith('aula')
        ? baseCaption
        : `${sectionLabel} - Figura ${idx + 1}: ${baseCaption}`
    const safeCaption = caption.replace(/]/g, '\\]')
    return `![${safeCaption}](${buildUrl(img.path)})`
  })
  return content.replace(IMAGE_TAG_RE, (_, inner: string) => {
    const trimmed = (inner || '').trim()
    if (/^\d+$/.test(trimmed)) {
      const n = parseInt(trimmed, 10)
      if (n >= 1 && n <= replacements.length) return replacements[n - 1]
    }
    const repl = replacements[nextOrder % replacements.length]
    nextOrder += 1
    return repl
  })
}

export function isLikelyHtmlContent(content: string): boolean {
  const raw = (content || '').trim()
  if (!raw) return true
  return /<\/?(p|h[1-6]|div|ul|ol|li|section|article|br|table|blockquote)\b/i.test(raw) ||
    (raw.startsWith('<') && raw.includes('>'))
}

/** Substitui tags [IMAGE:n] por HTML <figure><img> para preview. */
export function applyImageMarkersForHtmlPreview(
  content: string,
  images: SectionImage[],
  sectionLabel: string,
  buildUrl: (path: string) => string,
): string {
  if (!content || !images.length || !content.includes('[IMAGE:')) return content || ''
  let nextOrder = 0
  const replacements = images.map((img, idx) => {
    const baseCaption = img.caption || `Imagem ${idx + 1}`
    const caption =
      baseCaption.toLowerCase().startsWith('seção') ||
      baseCaption.toLowerCase().startsWith('secao') ||
      baseCaption.toLowerCase().startsWith('aula')
        ? baseCaption
        : `${sectionLabel} - Figura ${idx + 1}: ${baseCaption}`
    const safeCaption = caption.replace(/"/g, '&quot;')
    const safeSrc = buildUrl(img.path).replace(/"/g, '&quot;')
    return `<figure class="lesson-image"><img src="${safeSrc}" alt="${safeCaption}" /><figcaption>${safeCaption}</figcaption></figure>`
  })
  return content.replace(IMAGE_TAG_RE, (_, inner: string) => {
    const trimmed = (inner || '').trim()
    if (/^\d+$/.test(trimmed)) {
      const n = parseInt(trimmed, 10)
      if (n >= 1 && n <= replacements.length) return replacements[n - 1]
    }
    const repl = replacements[nextOrder % replacements.length]
    nextOrder += 1
    return repl
  })
}

export function lessonPreviewHtml(
  content: string,
  images: SectionImage[],
  sectionLabel: string,
  buildUrl: (path: string) => string,
): string {
  const body = isLikelyHtmlContent(content)
    ? content
    : applyImageMarkersForPreview(content, images, sectionLabel, buildUrl)
  if (isLikelyHtmlContent(content)) {
    return applyImageMarkersForHtmlPreview(body, images, sectionLabel, buildUrl)
  }
  return body
}

export function insertImageTagAt(content: string, imageNum: number, cursor?: number | null): string {
  const tag = `[IMAGE:${imageNum}]`
  const snippet = `\n\n${tag}\n\n`
  const text = content || ''
  if (cursor == null || cursor < 0 || cursor > text.length) return text + snippet
  return text.slice(0, cursor) + snippet + text.slice(cursor)
}

export function updateLessonInPlan(
  plan: CoursePlan,
  moduleIndex: number,
  lessonIndex: number,
  patch: Partial<CourseLesson>,
): CoursePlan {
  const modules = [...(plan.modules || [])]
  if (moduleIndex < 0 || moduleIndex >= modules.length) return plan
  const mod = { ...modules[moduleIndex] }
  const lessons = moduleLessonItems(mod).map((lesson, i) =>
    i === lessonIndex ? { ...lesson, ...patch } : lesson,
  )
  mod.lessons = lessons
  mod.lessons_details = lessons.map((l) => ({ ...l }))
  mod.sections = lessons.map((l) => ({ ...l }))
  modules[moduleIndex] = mod
  return { ...plan, modules }
}

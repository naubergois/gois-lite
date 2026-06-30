/** Canonical comic hierarchy — mirrors bookWorkspaceUtils (Saga → Histórias → Páginas → Painéis). */

export interface ComicPanel {
  panel_number?: number
  visual_description?: string
  visualDescription?: string
  dialogue?: string
  dialogue_balloons?: string
  narration?: string
  characters?: string[]
  image_url?: string
  imageUrl?: string
  visual_styles?: string[]
}

export interface ComicPage {
  page_id?: string
  page_number?: number
  title?: string
  page_goal?: string
  composed_image_url?: string
  panels?: ComicPanel[]
}

export interface ComicStory {
  story_id?: string
  saga_id?: string
  order?: number
  title?: string
  summary?: string
  objective?: string
  max_pages?: number
  cover_path?: string
  pages?: ComicPage[]
}

export interface ComicSagaPlan {
  saga_id?: string
  universe_id?: string
  name?: string
  description?: string
  theme?: string
  tone?: string
  genre?: string
  scope?: string
  language?: string
  stories?: ComicStory[]
  /** Legacy alias */
  historias?: ComicStory[]
}

export function getStoriesKey(plan: ComicSagaPlan | null | undefined): 'stories' | 'historias' {
  if (!plan) return 'stories'
  if (Array.isArray(plan.stories) && plan.stories.length > 0) return 'stories'
  if (Array.isArray(plan.historias) && plan.historias.length > 0) return 'historias'
  return 'stories'
}

export function sagaStories(plan: ComicSagaPlan | null | undefined): ComicStory[] {
  if (!plan) return []
  const key = getStoriesKey(plan)
  return (plan[key] as ComicStory[] | undefined) || []
}

export function storyPages(story: ComicStory | null | undefined): ComicPage[] {
  return story?.pages || []
}

export function pagePanels(page: ComicPage | null | undefined): ComicPanel[] {
  return page?.panels || []
}

export function panelVisual(panel: ComicPanel): string {
  return (panel.visual_description || panel.visualDescription || '').trim()
}

export function panelDialogue(panel: ComicPanel): string {
  return (panel.dialogue || panel.dialogue_balloons || '').trim()
}

export function panelHasImage(panel: ComicPanel): boolean {
  return !!(panel.image_url?.trim() || panel.imageUrl?.trim())
}

export function panelHasContent(panel: ComicPanel): boolean {
  return !!(panelVisual(panel) || panelDialogue(panel) || (panel.narration || '').trim() || panelHasImage(panel))
}

export function pageHasImage(page: ComicPage): boolean {
  return !!page.composed_image_url?.trim() || pagePanels(page).some(panelHasImage)
}

export function pageHasContent(page: ComicPage): boolean {
  return !!(page.page_goal?.trim() || pageHasImage(page) || pagePanels(page).some(panelHasContent))
}

export function storyHasContent(story: ComicStory): boolean {
  return !!(
    story.summary?.trim() ||
    story.objective?.trim() ||
    story.cover_path?.trim() ||
    storyPages(story).some(pageHasContent)
  )
}

export function safeComicLabel(s: string | undefined, fallback: string): string {
  if (s === undefined || s === null) return fallback
  const t = String(s).trim()
  return t || fallback
}

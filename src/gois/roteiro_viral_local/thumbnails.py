"""Local thumbnail prompt + generation (ported from roteiro viral image_agent)."""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import local_output_root
from .gemini_text import DEFAULT_TEXT_MODEL, generate_text
from .jobs import STORE

DEFAULT_PERSONA = (
    "A high-quality 3D animation style portrait in the aesthetic of Disney Pixar, featuring a stout and "
    "slightly chubby light-skinned man. He has dark, wavy, and messy hair with natural texture and moderate "
    "length. He has a well-groomed, full black beard and mustache. He is wearing stylish black-rimmed glasses. "
    "His face is friendly with a warm, genuine smile. 8k resolution, vibrant colors, cinematic masterpiece."
)


def get_neuro_boost_prompt() -> str:
    return """
    IRRESISTIBLE-CLICK PACK (YouTube mobile feed) — maximize scroll-stopping salience; the image MUST still match the video premise (no misleading bait):
    - Pattern interrupt: one unexpected but truthful visual hook (prop, giant number, split comparison, stark contrast) that avoids generic “stock reaction face” sameness.
    - Single dominant story: one primary focal point (face OR hero object OR scene). Decide in under 0.5s; remove competing subjects.
    - Curiosity gap: composition or expression implies an unfinished sentence (“what happens next?”) only answerable by watching.
    - Contrast & separation: rim light, vignette, or color separation so the subject pops from the background; avoid flat muddy mid-tones.
    - Text discipline: if text exists, 3–5 words max, ultra-bold, heavy outline/stroke, huge at small preview size — never tiny paragraphs.
    - Emotion readable at 120px width: eyes, brows, mouth exaggerated; gaze or gesture points toward the headline or key object.
    - One accent device max (arrow, circle, sticker) for clarity — not a cluttered collage unless the topic demands it.
    - Platform: 16:9, safe margins for UI chrome; assume phone screen in bright ambient light.
    - High saliency: extreme light-vs-dark or saturated accent vs desaturated surround to guide the eye in a fraction of a second.
    """


def _thumbnail_style_names_and_prompts(styles: Any) -> tuple[str, str]:
    style_names: list[str] = []
    prompt_chunks: list[str] = []
    if isinstance(styles, list):
        for style in styles:
            if isinstance(style, dict):
                name = (style.get("name") or "").strip()
                pr = (style.get("prompt") or style.get("prompt_modifier") or "").strip()
                if name:
                    style_names.append(name)
                if pr:
                    prompt_chunks.append(f"{name + ': ' if name else ''}{pr}")
            else:
                s = str(style).strip()
                if s:
                    style_names.append(s)
    elif styles:
        style_names.append(str(styles).strip())
    return " + ".join(style_names).strip(), " | ".join(prompt_chunks).strip()


def _thumbnail_format_imagen_params(thumbnail_format: Optional[dict[str, Any]]) -> tuple[str, bool, str]:
    if not thumbnail_format or not isinstance(thumbnail_format, dict):
        return "16:9", True, ""

    aspect_raw = (thumbnail_format.get("aspect") or "16:9").strip().replace(" ", "")
    name = (thumbnail_format.get("name") or thumbnail_format.get("id") or "thumbnail").strip()
    w = thumbnail_format.get("width")
    h = thumbnail_format.get("height")

    ar = aspect_raw
    if ar in ("1.91:1", "1,91:1"):
        ar = "16:9"
    supported = ("16:9", "1:1", "9:16", "4:3", "3:4", "3:2", "2:3", "4:5", "5:4", "21:9")
    if ar not in supported:
        ar = "16:9"

    dim_hint = f" Target pixel dimensions ~{w}×{h}." if w and h else ""
    platform_block = (
        f"PLATFORM / FORMAT: {name} — aspect ratio {aspect_raw} ({ar} for generation).{dim_hint} "
        "Compose for this frame: safe margins, hero subject readable at small preview size."
    )
    return ar, ar == "16:9", platform_block


def design_thumbnail_prompt(
    *,
    context: str,
    styles: Any,
    text_overlay: str,
    neuro_boost: bool,
    api_key: Optional[str],
    character_persona: Optional[str] = None,
    maintain_persona: bool = True,
    easter_eggs: bool | str = False,
    thumbnail_format: Optional[dict[str, Any]] = None,
) -> str:
    style_str, style_prompt_bundle = _thumbnail_style_names_and_prompts(styles)
    aspect_ratio, _do_crop, platform_block = _thumbnail_format_imagen_params(thumbnail_format)
    format_constraints = ""
    if platform_block:
        format_constraints = f"""
    OUTPUT TEMPLATE / PLATFORM (MANDATORY — design for this exact frame, not a generic 16:9 still):
    {platform_block}
    - Aspect ratio to design for: {aspect_ratio}. Tall (9:16) = vertical story framing with strong top-to-bottom flow; square (1:1) = centered hero; wide (16:9) = cinematic horizontal emphasis.
    """
    else:
        format_constraints = """
    OUTPUT TEMPLATE: Default wide thumbnail (16:9). Compose horizontally with clear hero and text zones.
    """

    neuro_text = get_neuro_boost_prompt().strip() if neuro_boost else ""
    char_style_guide = ""
    if maintain_persona:
        char_style_guide = (
            f"CRITICAL: The character MUST be rendered as a STYLIZED ILLUSTRATION or CARTOON, "
            f"but the specific art style of the character MUST HARMONIZE with the requested background style ({style_str}). "
            "For example: if the style is 'Cyberpunk', make the character a Cyberpunk Anime/Comic character. "
            "If the style is 'Van Gogh', make the character a Painted Caricature."
        )

    extra_instructions = ""
    if easter_eggs:
        extra_instructions += (
            "8. EASTER EGGS: Include subtle hidden details related to the topic in the background.\n"
        )

    click_optimization = (
        f"CLICK-OPTIMIZATION (MANDATORY — apply every line to the final scene you describe):\n{neuro_text}\n"
        if neuro_text
        else ""
    )
    style_prompts = (
        f"- DETAILED STYLE PROMPTS (MANDATORY): {style_prompt_bundle}" if style_prompt_bundle else ""
    )
    text_section = (
        f'TEXT TO BE RENDERED IN THE IMAGE: "{text_overlay}"'
        if text_overlay
        else "NO TEXT OVERLAY: Do not include any text in the image."
    )
    neuro_boost_text = (
        "ENABLED — extreme contrast, saturated accents, dopamine colors (neon, scarlet, gold), thumb-stopping composition."
        if neuro_boost
        else "Standard coloring."
    )
    rule_3 = (
        "The character MUST match the CHARACTER PERSONA and be in the DRAWING/ILLUSTRATIVE style requested."
        if maintain_persona
        else "Adapt the character style to the requested art style."
    )
    rule_6 = (
        "Mixed Media Look: Explicitly describe the character as a high-quality stylized drawing integrated into the scene."
        if maintain_persona
        else ""
    )
    rule_8 = (
        "8. Apply the full CLICK-OPTIMIZATION block above — the result must feel like a deliberate poster moment engineered to earn the click, not a generic still frame."
        if neuro_text
        else ""
    )

    prompt = f"""
    You are a professional YouTube Thumbnail Designer and Visual Artist.

    TASK: Design a highly descriptive prompt for an AI image generator (like Imagen) to create a VIRAL, IRRESISTIBLE-TO-CLICK thumbnail (optimized for mobile feed CTR while staying honest to the topic).
    {format_constraints}
    INPUT CONTEXT:
    {context[:1500]}

    CHARACTER PERSONA: {character_persona or 'A charismatic content creator'}

    {click_optimization}

    CRITICAL STYLE INSTRUCTIONS:
    - {char_style_guide}
    - ARTISTIC STYLES to Incorporate (names): {style_str or "general high-impact thumbnail"}
    - INSTRUCTION: You MUST blend ALL the requested style names above into a cohesive visual identity. Do not ignore any selected style.
    {style_prompts}
    - NEURO-BOOST: {neuro_boost_text}

    {text_section}

    RULES:
    1. DO NOT include act labels or technical metadata.
    2. Focus on VIVID VISUAL DESCRIPTION: lighting, camera angle, character expression (extreme readable emotion), and dramatic background.
    3. {rule_3}
    4. Composition: Rule of thirds or intentional off-center tension; one hero focal point; high impact, minimal competing elements.
    5. TEXT PLACEMENT: If text is provided, describe massive bold lettering with thick outline/stroke and drop shadow; readable when the image is tiny.
    6. {rule_6}
    7. Language: Always output the final designed prompt in ENGLISH.
    {rule_8}

    {extra_instructions}

    OUTPUT: Pure text, the optimized image prompt only. Do not output explanations.
    """
    try:
        return generate_text(prompt, api_key=api_key, model=DEFAULT_TEXT_MODEL)
    except Exception:
        return f"Viral thumbnail for {context[:100]}, dramatic lighting, high quality, {style_str}"


def _resolve_physical_description(
    *,
    user_photo_path: str,
    character_persona_override: str,
    api_key: Optional[str],
    maintain_persona: bool,
) -> str:
    if character_persona_override.strip():
        return character_persona_override.strip()

    persona = DEFAULT_PERSONA
    try:
        from ..visual_memory import resolve_for_thumbnail

        vm = resolve_for_thumbnail()
        if vm.get("character_persona_override"):
            return str(vm["character_persona_override"]).strip()
    except Exception:
        pass

    path = (user_photo_path or "").strip()
    if path and Path(path).is_file() and maintain_persona:
        try:
            from ..face_analysis import _open_image_path

            pil_image = _open_image_path(Path(path))
            analysis_prompt = (
                "Describe the person in this image in detail for an image generator. Focus on facial features, "
                f"hair color, eye shape, and any glasses. NOTE: The person is {persona}, so ensure the description "
                "complements this. English output."
            )
            desc = generate_text(
                analysis_prompt,
                api_key=api_key,
                model=DEFAULT_TEXT_MODEL,
                images=[pil_image],
            )
            return f"{persona}. Specific features from photo: {desc.strip()}"
        except Exception:
            pass
    return persona


def generate_viral_thumbnail_prompt(
    *,
    script_summary: str,
    user_photo_path: str = "",
    styles: Any = None,
    text_overlay: str = "",
    easter_eggs: bool | str = "",
    neuro_boost: bool = True,
    api_key: Optional[str] = None,
    maintain_persona: bool = True,
    pre_defined_prompt: Optional[str] = None,
    include_mascot: bool = False,
    character_persona_override: str = "",
    thumbnail_format: Optional[dict[str, Any]] = None,
) -> str:
    styles = styles or [{"name": "Hyper-Realistic", "prompt": "high detail, 8k"}]
    user_physical_description = _resolve_physical_description(
        user_photo_path=user_photo_path,
        character_persona_override=character_persona_override,
        api_key=api_key,
        maintain_persona=maintain_persona,
    )

    clean_summary = re.sub(r"Ato\s+\d+\s*\(.*?\):", "", script_summary or "")
    clean_summary = re.sub(r"Ato\s+\d+:", "", clean_summary).strip()
    aspect_ratio, _do_crop, platform_block = _thumbnail_format_imagen_params(thumbnail_format)

    if pre_defined_prompt and pre_defined_prompt.strip():
        designed_visual_prompt = pre_defined_prompt.strip()
        _, style_bundle = _thumbnail_style_names_and_prompts(styles)
        if style_bundle:
            designed_visual_prompt = (
                f"{designed_visual_prompt}\n\nSTYLE CONSTRAINTS (mandatory): {style_bundle}"
            )
    else:
        designed_visual_prompt = design_thumbnail_prompt(
            context=clean_summary,
            styles=styles,
            text_overlay=text_overlay,
            neuro_boost=neuro_boost,
            api_key=api_key,
            character_persona=user_physical_description,
            maintain_persona=maintain_persona,
            easter_eggs=bool(easter_eggs),
            thumbnail_format=thumbnail_format,
        )

    if text_overlay:
        lines = [
            line
            for line in designed_visual_prompt.splitlines()
            if "TEXT TO BE RENDERED" not in line.upper()
        ]
        designed_visual_prompt = "\n".join(lines).strip()
        designed_visual_prompt = (
            f'{designed_visual_prompt}\n\nTEXT TO BE RENDERED IN THE IMAGE: "{text_overlay}"'
        )

    if include_mascot:
        designed_visual_prompt = (
            f"{designed_visual_prompt}\n\n"
            "COMPOSITION: Place the main character in the bottom-right corner. "
            "Ensure the face is clear and recognizable, and leave ample space for the headline text."
        )

    if maintain_persona:
        prompt_persona_section = f"""
        PHYSICAL CHARACTERISTICS AND STYLE (ESSENTIAL):
        - CHARACTER TYPE: The central character MUST be a high-quality stylized DRAWING or 3D Render (Pixar style) integrated into the scene.
        - CHARACTER LIKENESS: {user_physical_description}.
        """
    else:
        style_label, style_prompts = _thumbnail_style_names_and_prompts(styles)
        detail = f"- Visual style details: {style_prompts}\n        " if style_prompts else ""
        prompt_persona_section = f"""
        PHYSICAL CHARACTERISTICS:
        - Character based on: {user_physical_description}.
        - Style: Adapt character completely to: {style_label or "requested art direction"}.
        {detail}"""

    platform_lines = f"- {platform_block}\n    " if platform_block else ""
    mixed = (
        "Mixed Media Aesthetic: High-quality illustration character on a detailed cinematic background."
        if maintain_persona
        else ""
    )
    neuro_extra = (
        "; irresistible-click finish: exaggerate subject–background separation and thumbnail-scale legibility"
        if neuro_boost
        else ""
    )
    easter = f"- Include hidden easter eggs: {easter_eggs}\n    " if easter_eggs else ""

    return f"""
    {designed_visual_prompt}

    {prompt_persona_section}

    ADDITIONAL TECHNICAL SPECS:
    - {mixed}
    - High-impact facial lighting specifically highlighting the features{neuro_extra}.
    {easter}- Resolution: 8k, ultra-detailed, sharp focus.
    {platform_lines}- Output aspect ratio: {aspect_ratio} (match framing to this ratio).
    - NO TEXT LABELS like 'Act 1' or 'Ato 1'.
    """.strip()


def handle_thumbnail_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Prompt vazio.")

    styles = payload.get("styles") if isinstance(payload.get("styles"), list) else []
    styles_payload = []
    for style in styles:
        if isinstance(style, dict):
            styles_payload.append(style)
        elif style:
            styles_payload.append({"name": str(style), "prompt": ""})

    summary_blocks = [str(payload.get("script_summary") or ""), prompt]
    combined_summary = "\n\n".join([b.strip() for b in summary_blocks if b and b.strip()])

    final_prompt = generate_viral_thumbnail_prompt(
        script_summary=combined_summary,
        user_photo_path=str(payload.get("user_photo_path") or ""),
        styles=styles_payload or [{"name": "Hyper-Realistic", "prompt": "high detail, 8k"}],
        text_overlay=str(payload.get("text_overlay") or ""),
        easter_eggs=payload.get("easter_eggs") or "",
        neuro_boost=bool(payload.get("neuro_boost", True)),
        api_key=payload.get("api_key"),
        maintain_persona=True,
        include_mascot=bool(payload.get("include_mascot")),
        character_persona_override=str(payload.get("character_persona_override") or ""),
        thumbnail_format=payload.get("thumbnail_format")
        if isinstance(payload.get("thumbnail_format"), dict)
        else None,
        pre_defined_prompt=None,
    )
    return {"status": "success", "final_prompt": final_prompt}


def _run_thumbnail_job(job_id: str, job: dict[str, Any]) -> None:
    payload = job.get("request_payload") or {}
    api_key = payload.get("api_key") or job.get("api_key")
    if not api_key:
        STORE.log(job_id, "❌ API Key não encontrada.")
        STORE.set_status(job_id, "failed", error="api_key missing")
        return

    STORE.log(job_id, "🖼️ Job de Thumbnail iniciado (local)...")
    final_prompt = generate_viral_thumbnail_prompt(
        script_summary=str(payload.get("script_summary") or ""),
        user_photo_path=str(payload.get("user_photo_path") or ""),
        styles=payload.get("styles") or [{"name": "Hyper-Realistic", "prompt": "high detail, 8k"}],
        text_overlay=str(payload.get("text_overlay") or ""),
        easter_eggs=payload.get("easter_eggs", True),
        neuro_boost=bool(payload.get("neuro_boost", True)),
        api_key=api_key,
        maintain_persona=bool(payload.get("maintain_persona", True)),
        pre_defined_prompt=str(payload.get("pre_defined_prompt") or "") or None,
        include_mascot=bool(payload.get("include_mascot")),
        character_persona_override=str(payload.get("character_persona_override") or ""),
        thumbnail_format=payload.get("thumbnail_format")
        if isinstance(payload.get("thumbnail_format"), dict)
        else None,
    )

    tf = payload.get("thumbnail_format")
    aspect_ratio = "16:9"
    if isinstance(tf, dict):
        aspect_ratio, _, _ = _thumbnail_format_imagen_params(tf)

    out_dir = local_output_root() / "thumbnails"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{job_id}_{uuid.uuid4().hex[:8]}.png"

    from ..roteiro_imagen import DEFAULT_IMAGEN_MODEL, generate_imagen_to_path

    STORE.log(job_id, "🚀 Gerando imagem com Imagen (local)...")
    result = generate_imagen_to_path(
        prompt=final_prompt,
        output_path=out_path,
        api_key=api_key,
        model_name=DEFAULT_IMAGEN_MODEL,
        aspect_ratio=aspect_ratio,
    )
    path = str(result.get("output_path") or result.get("image_path") or "")
    if not path:
        STORE.log(job_id, "❌ Falha na geração de imagem.")
        STORE.set_status(job_id, "failed", error="image generation failed")
        return

    text_overlay = str(payload.get("text_overlay") or "")
    state = {
        "thumbnail_path": path,
        "generation_prompt": final_prompt,
        "thumbnails": [
            {"path": path, "title": text_overlay, "created_at": time.time()},
        ],
    }
    STORE.update(job_id, final_state=state)
    STORE.log(job_id, f"✅ Thumbnail gerada: {path}")
    STORE.set_status(job_id, "completed")


def handle_generate_thumbnail(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("api_key necessária (GEMINI_API_KEY / GOOGLE_API_KEY)")

    parent_job_id = str(payload.get("job_id") or "").strip()
    topic = f"Thumb: {payload.get('text_overlay', 'Sem Título')}"[:50]
    job_id = STORE.create(
        job_type=str(payload.get("job_type") or "thumbnail_generation"),
        topic=topic,
        request_payload=payload,
        parent_job_id=parent_job_id,
        api_key=api_key,
    )
    STORE.run_async(job_id, _run_thumbnail_job)
    return {"status": "started", "job_id": job_id}


def handle_job_status(job_id: str) -> dict[str, Any]:
    job = STORE.get(job_id)
    if not job:
        raise KeyError(f"Job {job_id!r} não encontrado")
    return job

import json
from pydantic import BaseModel, Field
from typing import List

class ViralTitle(BaseModel):
    title: str = Field(description="The actual title text")
    score: int = Field(description="Predicted virality score (0-10)")
    trigger: str = Field(description="Psychological trigger used (e.g. Curiosity Gap, Negativity Bias, FOMO)")
    explanation: str = Field(description="Why this works based on neuroscience")

class ViralMetadataResult(BaseModel):
    titles: List[ViralTitle] = Field(description="5 highly viral title variations")
    description_hook: str = Field(description="First 2 lines of description (the hook)")
    description_body: str = Field(description="SEO optimized description body")
    tags: List[str] = Field(description="15-20 high volume search tags")
    hashtags: List[str] = Field(description="3-5 niche hashtags for discovery")
    thumbnail_text_ideas: List[str] = Field(description="Short, punchy text for thumbnails (max 3 words)")


def generate_viral_metadata_node(state, model):
    """
    Generates viral metadata (Titles, Description, Tags) based on neuroscience principles.
    Supports both LangChain ChatGoogleGenerativeAI and raw google.generativeai.GenerativeModel.
    """
    try:
        script = state.get("final_script") or state.get("draft_script") or state.get("content", "")
        topic = state.get("topic", "")
        
        if not script and not topic:
            return {"error": "No script or topic provided for metadata generation."}

        # prompt text
        prompt_text = f"""
            You are a YouTube/TikTok Growth Hacker & Neuro-Linguistic Programming Expert.
            
            TASK: Create a Viral Metadata Study for the following content.
            
            CONTENT CONTEXT:
            Topic: {topic}
            Script Snippet: {script[:2000]}
            
            ---
            
            LANGUAGE: All generated text (titles, descriptions, explanations, tags, thumb text) MUST be in Brazilian Portuguese (Português do Brasil).
            
            REQUIREMENTS:
            
            1. **TITLES (Create 5 Variations):**
               - Use patterns like "Eu tentei X...", "A verdade sobre Y...", "Pare de fazer Z...".
               - Incorporate Neuro-Triggers in Portuguese: Viés de Negatividade, Curiosity Gap, Autoridade, Urgência.
               - Titles must be under 60 characters if possible.
               
            2. **DESCRIPTION (In Portuguese):**
               - **Hook:** First 2 lines most capture attention immediately (above the fold).
               - **Body:** SEO-optimized with keywords naturally integrated.
               
            3. **TAGS & HASHTAGS (In Portuguese):**
               - Mix of broad (high volume) and specific (high intent) tags.
               
            4. **THUMBNAIL TEXT (In Portuguese):**
               - Max 3 words. Complement the title, don't repeat it.
            
            OUTPUT FORMAT (JSON ONLY):
            {{
                "titles": [
                    {{
                        "title": "...", 
                        "score": 9,
                        "trigger": "Curiosidade",
                        "explanation": "Explicação em português..."
                    }}
                ],
                "description_hook": "Hook em português...",
                "description_body": "Corpo em português...",
                "tags": ["tag1", "tag2"],
                "hashtags": ["#hash1"],
                "thumbnail_text_ideas": ["TEXTO 1", "TEXTO 2"]
            }}
            
            Ensure the output is valid JSON. Do not include markdown code blocks like ```json.
        """
        
        # Check model type to use correct method
        is_langchain = hasattr(model, "invoke")
        
        response_text = ""
        
        if is_langchain:
            # It's a LangChain model
            from langchain.schema import HumanMessage
            res = model.invoke([HumanMessage(content=prompt_text)])
            response_text = res.content
        else:
            # It's a raw GenAI model
            res = model.generate_content(prompt_text)
            response_text = res.text
            
        # Clean and Parse
        json_str = response_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(json_str)
        
        return {
            "viral_metadata": data,
            "logs": ["✅ Estudo de Metadados Virais gerado com sucesso!"]
        }

    except Exception as e:
        return {"error": str(e), "logs": [f"❌ Erro ao gerar metadados: {e}"]}

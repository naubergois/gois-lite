from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArticleSection:
    title: str
    objective: str
    required_items: tuple[str, ...]


_DEFAULT_SECTIONS: tuple[ArticleSection, ...] = (
    ArticleSection(
        title="Título",
        objective="Definir claramente o tema, recorte e principal contribuição do artigo.",
        required_items=(
            "Tema principal em até 15 palavras.",
            "Recorte (contexto, população ou domínio).",
            "Se possível, indicar método/abordagem no próprio título.",
        ),
    ),
    ArticleSection(
        title="Resumo",
        objective="Apresentar uma visão completa e curta do artigo para leitura rápida.",
        required_items=(
            "Contexto e problema em 1-2 frases.",
            "Objetivo geral do estudo.",
            "Método principal utilizado.",
            "Resultado(s) mais relevante(s).",
            "Conclusão e impacto prático/científico.",
        ),
    ),
    ArticleSection(
        title="Palavras-chave",
        objective="Facilitar indexação, busca e descoberta do artigo.",
        required_items=(
            "Entre 3 e 6 termos centrais.",
            "Incluir método, domínio e conceito principal.",
            "Evitar termos genéricos demais.",
        ),
    ),
    ArticleSection(
        title="Introdução",
        objective="Contextualizar o problema, justificar relevância e apresentar os objetivos.",
        required_items=(
            "Contexto e motivação do problema.",
            "Lacuna no estado da arte.",
            "Pergunta(s) de pesquisa ou hipótese(s).",
            "Objetivo geral e objetivos específicos.",
            "Resumo das contribuições do artigo.",
        ),
    ),
    ArticleSection(
        title="Referencial Teórico / Trabalhos Relacionados",
        objective="Posicionar o artigo frente ao conhecimento existente.",
        required_items=(
            "Principais conceitos e definições.",
            "Comparação entre abordagens anteriores.",
            "Limitações dos trabalhos relacionados.",
            "Como o presente artigo se diferencia.",
        ),
    ),
    ArticleSection(
        title="Metodologia",
        objective="Descrever com precisão como o estudo foi conduzido para permitir reprodução.",
        required_items=(
            "Tipo de estudo e desenho metodológico.",
            "Fontes de dados, amostra e critérios.",
            "Procedimentos, ferramentas e etapas.",
            "Métricas/indicadores de avaliação.",
            "Limitações metodológicas.",
        ),
    ),
    ArticleSection(
        title="Resultados",
        objective="Apresentar os achados de forma objetiva e verificável.",
        required_items=(
            "Resultados principais em texto, tabela e/ou figura.",
            "Valores, tendências e comparações relevantes.",
            "Resultados positivos e negativos.",
            "Dados suficientes para sustentar as conclusões.",
        ),
    ),
    ArticleSection(
        title="Discussão",
        objective="Interpretar os resultados e conectá-los à literatura.",
        required_items=(
            "Interpretação dos principais achados.",
            "Relação com trabalhos relacionados.",
            "Implicações teóricas e/ou práticas.",
            "Ameaças à validade e limitações.",
        ),
    ),
    ArticleSection(
        title="Conclusão",
        objective="Fechar o artigo retomando objetivos, contribuições e próximos passos.",
        required_items=(
            "Síntese objetiva do que foi alcançado.",
            "Resposta à pergunta de pesquisa.",
            "Contribuições centrais do trabalho.",
            "Sugestões de trabalhos futuros.",
        ),
    ),
    ArticleSection(
        title="Referências",
        objective="Documentar corretamente todas as fontes utilizadas.",
        required_items=(
            "Todas as citações do texto devem aparecer na lista final.",
            "Padronização em um único estilo (ABNT, APA, IEEE etc.).",
            "Conferir autor, ano, título, periódico/evento e DOI quando houver.",
        ),
    ),
)


def _normalize_custom_section(name: str) -> str:
    return " ".join((name or "").strip().split())


def sections_from_names(names: list[str]) -> list[ArticleSection]:
    out: list[ArticleSection] = []
    for raw in names:
        name = _normalize_custom_section(raw)
        if not name:
            continue
        out.append(
            ArticleSection(
                title=name,
                objective=(
                    "Explicar a função desta seção no artigo e como ela contribui "
                    "para os objetivos gerais do trabalho."
                ),
                required_items=(
                    "Contexto específico da seção.",
                    "Informações essenciais para sustentar a narrativa do artigo.",
                    "Evidências, exemplos ou dados aplicáveis.",
                    "Transição clara para a próxima seção.",
                ),
            )
        )
    return out


def render_article_template_markdown(
    title: str,
    sections: list[ArticleSection] | tuple[ArticleSection, ...] | None = None,
) -> str:
    chosen = list(sections or _DEFAULT_SECTIONS)
    safe_title = (title or "Título do Artigo").strip() or "Título do Artigo"
    parts: list[str] = []

    parts.append(f"# {safe_title}")
    parts.append("")
    parts.append(
        "> Template de escrita de artigo com objetivo e checklist por seção."
    )
    parts.append("")
    parts.append("## Visão Geral")
    parts.append("")
    parts.append("- Tema:")
    parts.append("- Público-alvo:")
    parts.append("- Problema central:")
    parts.append("- Pergunta de pesquisa:")
    parts.append("- Contribuição principal:")
    parts.append("")

    for index, section in enumerate(chosen, start=1):
        parts.append(f"## {index}. {section.title}")
        parts.append("")
        parts.append("### Objetivo")
        parts.append(section.objective)
        parts.append("")
        parts.append("### O que deve ter")
        for item in section.required_items:
            parts.append(f"- {item}")
        parts.append("")
        parts.append("### Rascunho")
        parts.append("")
        parts.append("Escreva aqui o conteúdo desta seção.")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def write_article_template_file(
    output_path: str | Path,
    *,
    title: str,
    sections: list[ArticleSection] | tuple[ArticleSection, ...] | None = None,
    overwrite: bool = False,
) -> Path:
    target = Path(output_path).expanduser()
    if target.exists() and not overwrite:
        raise FileExistsError(f"arquivo já existe: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = render_article_template_markdown(title=title, sections=sections)
    target.write_text(content, encoding="utf-8")
    return target


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gois-article-md",
        description=(
            "Gera um template de artigo em Markdown com objetivo e checklist "
            "para cada seção."
        ),
    )
    parser.add_argument(
        "--title",
        default="Título do Artigo",
        help="título do artigo",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="arquivo de saída .md (se omitido, imprime no stdout)",
    )
    parser.add_argument(
        "--sections",
        default="",
        help=(
            "lista customizada de seções separadas por vírgula. "
            "Exemplo: Introdução,Fundamentação,Método"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="sobrescreve arquivo de saída existente",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    custom_names = [piece.strip() for piece in str(args.sections or "").split(",") if piece.strip()]
    sections = sections_from_names(custom_names) if custom_names else list(_DEFAULT_SECTIONS)
    markdown = render_article_template_markdown(args.title, sections)

    if args.output is None:
        print(markdown, end="")
        return 0

    try:
        out = write_article_template_file(
            args.output,
            title=args.title,
            sections=sections,
            overwrite=bool(args.force),
        )
    except FileExistsError as exc:
        parser.error(str(exc))
        return 2

    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

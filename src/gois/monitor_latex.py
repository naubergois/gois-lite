"""LaTeX article workspaces and compile handlers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .accounts import UserRecord


def _suggest_section_objective(title: str) -> str:
    lower = str(title or "").strip().lower()
    if not lower:
        return "Desenvolver o conteúdo central desta seção de forma objetiva."
    if "introdu" in lower or "context" in lower:
        return "Contextualizar o problema e motivar a pesquisa."
    if "metod" in lower or "métod" in lower:
        return "Descrever o método proposto e suas etapas principais."
    if "result" in lower or "exper" in lower or "avali" in lower:
        return "Apresentar e analisar os resultados obtidos."
    if "conclus" in lower or "considera" in lower:
        return "Sintetizar os achados e implicações do estudo."
    return "Desenvolver o conteúdo central desta seção de forma objetiva."


def _normalize_section_objectives(rows: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("section") or "").strip()
        objective = str(row.get("objective") or "").strip()
        if not title and not objective:
            continue
        out.append({"title": title, "objective": objective})
    return out


def _render_objectives_markdown(
    *,
    article_title: str,
    article_objective: str,
    section_objectives: list[dict[str, str]],
    workspace_name: str,
    article_id: str,
) -> str:
    title = article_title.strip() or "Artigo"
    article_obj = article_objective.strip() or "Não definido."
    lines = [
        f"# Objetivos — {title}",
        "",
        f"- Gerado em: {datetime.now().isoformat(timespec='seconds')}",
        f"- Workspace: {workspace_name or '-'}",
        f"- Artigo: `{article_id}`",
        "",
        "## Objetivo geral do artigo",
        "",
        article_obj,
        "",
        "## Objetivos por seção",
        "",
    ]
    if not section_objectives:
        lines.extend(["_Nenhuma seção detectada._", ""])
        return "\n".join(lines)
    for idx, row in enumerate(section_objectives, start=1):
        section_title = str(row.get("title") or "").strip() or f"Seção {idx}"
        objective = (
            str(row.get("objective") or "").strip()
            or _suggest_section_objective(section_title)
        )
        lines.extend(
            [
                f"### {idx}. {section_title}",
                "",
                objective,
                "",
            ]
        )
    return "\n".join(lines)


class MonitorLatexMixin:
    def _latex_auth(self, user: Optional[UserRecord]) -> Optional[dict]:
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}
        return None

    def handle_latex_workspaces_list(self, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import list_workspaces

        return {"ok": True, "workspaces": list_workspaces()}

    def handle_latex_workspace_register(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import register_workspace

        return register_workspace(
            name=str(payload.get("name") or ""),
            path=str(payload.get("path") or ""),
        )

    def handle_latex_workspace_remove(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import remove_workspace

        return remove_workspace(str(payload.get("workspace_id") or ""))

    def handle_latex_articles_list(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import list_articles

        return list_articles(str(query.get("workspace_id") or ""))

    def handle_latex_files_list(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import list_workspace_files

        return list_workspace_files(
            str(query.get("workspace_id") or ""),
            str(query.get("path") or query.get("rel_path") or ""),
        )

    def handle_latex_tex_files_list(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import list_workspace_tex_files

        return list_workspace_tex_files(str(query.get("workspace_id") or ""))

    def handle_latex_bib_files_list(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import list_workspace_bib_files

        return list_workspace_bib_files(str(query.get("workspace_id") or ""))

    def handle_latex_file_delete(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import delete_workspace_file

        return delete_workspace_file(
            str(payload.get("workspace_id") or ""),
            str(payload.get("path") or payload.get("rel_path") or ""),
        )

    def handle_latex_file_upload(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import upload_workspace_files

        files = payload.get("files") or payload.get("items") or []
        return upload_workspace_files(
            str(payload.get("workspace_id") or ""),
            files if isinstance(files, list) else [],
        )

    def handle_latex_file_get(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import get_workspace_file

        workspace_id = str(query.get("workspace_id") or "").strip()
        rel_path = str(query.get("path") or query.get("file_path") or "").strip()
        if not workspace_id or not rel_path:
            return {"ok": False, "error": "workspace_id and path are required"}
        return get_workspace_file(workspace_id, rel_path)

    def handle_latex_file_save(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import save_workspace_file

        workspace_id = str(payload.get("workspace_id") or "").strip()
        rel_path = str(payload.get("path") or payload.get("file_path") or "").strip()
        if not workspace_id or not rel_path:
            return {"ok": False, "error": "workspace_id and path are required"}
        if "content" not in payload:
            return {"ok": False, "error": "content is required"}
        return save_workspace_file(
            workspace_id,
            rel_path,
            str(payload.get("content") or ""),
            backup=bool(payload.get("backup", True)),
        )

    def handle_latex_compile(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_articles import compile_article

        chat_cfg = None
        oc = getattr(self.cfg, "openclaw_chat", None)
        if oc is not None and getattr(oc, "enabled", True):
            chat_cfg = oc

        return compile_article(
            str(payload.get("workspace_id") or ""),
            str(payload.get("article_id") or ""),
            repair_on_failure=bool(payload.get("repair_on_failure", True)),
            model_id=str(payload.get("model_id") or "").strip() or None,
            chat_cfg=chat_cfg,
        )

    def handle_latex_tex_get(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(query.get("workspace_id") or "").strip()
        article_id = str(query.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .latex_articles import _resolve_article_tex
        from .latex_tex_edit import analyze_tex, extract_objectives, merge_objectives

        _root, tex, err = _resolve_article_tex(workspace_id, article_id)
        if tex is None:
            return {"ok": False, "error": err or "artigo não encontrado"}
        try:
            content = tex.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        pdf = tex.with_suffix(".pdf")
        has_pdf = pdf.is_file()
        response: dict = {
            "ok": True,
            "workspace_id": workspace_id,
            "article_id": article_id,
            "path": str(tex),
            "content": content,
            "length": len(content),
            "analysis": analyze_tex(content),
            "has_pdf": has_pdf,
        }
        # The database is the durable store for objectives: fall back to it so
        # saved objectives survive even if the % qclaw-objectives comment line
        # was stripped from the .tex (LLM rewrites, template apply, page-fit…).
        db_objectives = None
        try:
            from .latex_objectives_db import get_objectives

            db_objectives = get_objectives(workspace_id, article_id)
        except Exception:
            db_objectives = None
        response.update(
            merge_objectives(extract_objectives(content), db_objectives)
        )
        if has_pdf:
            from .latex_tex_edit import _page_count_for_tex

            pages, _ = _page_count_for_tex(tex)
            if pages is not None:
                response["page_count"] = pages
        return response

    def handle_latex_tex_chat(
        self,
        payload: dict,
        user: Optional[UserRecord],
        on_token: Optional["Callable[[str], None]"] = None,
        on_status: Optional["Callable[[str], None]"] = None,
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .latex_tex_chat import latex_tex_chat

        chat_cfg = None
        oc = getattr(self.cfg, "openclaw_chat", None)
        if oc is not None and getattr(oc, "enabled", True):
            chat_cfg = oc
        max_iterations = None
        raw_steps = payload.get("max_iterations", payload.get("max_steps"))
        if raw_steps is not None:
            try:
                max_iterations = max(1, min(50, int(raw_steps)))
            except (TypeError, ValueError):
                max_iterations = None

        team_id = str(payload.get("team_id") or "").strip()
        team_info = None
        if team_id:
            try:
                actor = getattr(self, "_accounts_actor")(user) if hasattr(self, "_accounts_actor") else user
                if actor is not None and hasattr(self, "accounts"):
                    try:
                        team_rec = self.accounts.get_team(team_id, actor.id)
                    except ValueError:
                        team_rec = None
                        if getattr(actor, "is_admin", False):
                            team_rec = next(
                                (t for t in self.accounts.list_all_teams() if t.id == team_id),
                                None,
                            )
                    if team_rec is not None:
                        team_info = team_rec.to_public()
                        try:
                            team_info["__kanban"] = self.accounts.read_kanban(team_rec.id, actor.id)
                        except Exception:
                            team_info["__kanban"] = None
                        try:
                            from .team_normas_ops import list_normas
                            nr = list_normas(team_rec.id)
                            team_info["__normas"] = list(nr.get("normas") or []) if nr.get("ok") else []
                        except Exception:
                            team_info["__normas"] = []
            except Exception:
                pass

        return latex_tex_chat(
            message=str(payload.get("message") or ""),
            content=str(payload.get("content") if payload.get("content") is not None else ""),
            history=payload.get("history"),
            chat_cfg=chat_cfg,
            model_id=str(payload.get("model_id") or "").strip() or None,
            workspace_id=workspace_id,
            article_id=article_id,
            team_id=team_id,
            team_info=team_info,
            cursor_offset=int(payload.get("cursor_offset") or 0),
            save_after=bool(payload.get("save_after")),
            backup=bool(payload.get("backup", True)),
            max_iterations=max_iterations,
            on_token=on_token,
            on_status=on_status,
        )

    def handle_latex_objectives_suggest(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}

        from .latex_articles import _resolve_article_tex
        from .latex_tex_chat import suggest_latex_objectives

        content = payload.get("content")
        if content is None:
            _root, tex, err = _resolve_article_tex(workspace_id, article_id)
            if tex is None:
                return {"ok": False, "error": err or "artigo não encontrado"}
            try:
                content = tex.read_text(encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "error": str(exc)}

        chat_cfg = None
        oc = getattr(self.cfg, "openclaw_chat", None)
        if oc is not None and getattr(oc, "enabled", True):
            chat_cfg = oc
        return suggest_latex_objectives(
            content=str(content),
            chat_cfg=chat_cfg,
            model_id=str(payload.get("model_id") or "").strip() or None,
            workspace_id=workspace_id,
            article_id=article_id,
        )

    def handle_latex_objectives_get(
        self, query: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(query.get("workspace_id") or "").strip()
        article_id = str(query.get("article_id") or "").strip()
        from .latex_objectives_db import get_objectives, list_objectives

        if not workspace_id and not article_id:
            return list_objectives()
        if not article_id:
            return list_objectives(workspace_id=workspace_id)
        if not workspace_id:
            return {"ok": False, "error": "workspace_id is required"}
        result = get_objectives(workspace_id, article_id)
        # Fall back to the objectives embedded in the .tex when the DB is empty.
        if result.get("ok") and not result.get("found"):
            try:
                from .latex_articles import _resolve_article_tex
                from .latex_tex_edit import extract_objectives

                _root, tex, _err = _resolve_article_tex(workspace_id, article_id)
                if tex is not None:
                    content = tex.read_text(encoding="utf-8")
                    objectives = extract_objectives(content)
                    result["article_objective"] = objectives.get(
                        "article_objective", ""
                    )
                    sections = objectives.get("section_objectives", []) or []
                    result["section_objectives"] = sections
                    result["section_count"] = len(sections)
                    result["source"] = "tex"
            except Exception:
                pass
        return result

    def handle_latex_objectives_save(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .latex_objectives_db import save_objectives

        path = ""
        try:
            from .latex_articles import _resolve_article_tex

            _root, tex, _err = _resolve_article_tex(workspace_id, article_id)
            if tex is not None:
                path = str(tex)
        except Exception:
            path = ""

        result = save_objectives(
            workspace_id=workspace_id,
            article_id=article_id,
            article_objective=payload.get("article_objective"),
            section_objectives=payload.get("section_objectives"),
            path=path,
        )
        if not result.get("ok"):
            return result

        # Optionally mirror the objectives back into the .tex source so the
        # editor and database stay in sync.
        if bool(payload.get("write_tex", True)):
            try:
                from .latex_articles import _resolve_article_tex
                from .latex_safe_write import safe_write_tex
                from .latex_tex_edit import apply_objectives

                _root, tex, err = _resolve_article_tex(workspace_id, article_id)
                if tex is not None:
                    content = tex.read_text(encoding="utf-8")
                    updated = apply_objectives(
                        content,
                        article_objective=result.get("article_objective"),
                        section_objectives=result.get("section_objectives"),
                    )
                    if updated != content:
                        write_res = safe_write_tex(
                            tex,
                            updated,
                            backup=bool(payload.get("backup", True)),
                            label="latex_objectives_save",
                        )
                        result["tex_written"] = bool(write_res.get("ok"))
                        if write_res.get("backup_path"):
                            result["backup_path"] = write_res["backup_path"]
                    else:
                        result["tex_written"] = False
                else:
                    result["tex_written"] = False
                    result["tex_error"] = err or "artigo não encontrado"
            except Exception as exc:
                result["tex_written"] = False
                result["tex_error"] = f"{type(exc).__name__}: {exc}"
        return result

    def handle_latex_tex_apply_template(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        template_id = str(payload.get("template_id") or payload.get("id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        if not template_id:
            return {"ok": False, "error": "template_id is required"}
        from .latex_tex_template_apply import latex_tex_template_apply

        chat_cfg = None
        oc = getattr(self.cfg, "openclaw_chat", None)
        if oc is not None and getattr(oc, "enabled", True):
            chat_cfg = oc
        return latex_tex_template_apply(
            template_id=template_id,
            workspace_id=workspace_id,
            article_id=article_id,
            chat_cfg=chat_cfg,
            model_id=str(payload.get("model_id") or "").strip() or None,
            save_after=bool(payload.get("save_after", True)),
            backup=bool(payload.get("backup", True)),
            dry_run=bool(payload.get("dry_run")),
        )

    def handle_latex_tex_restore_template(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .latex_tex_template_apply import latex_tex_restore_template

        return latex_tex_restore_template(
            workspace_id=workspace_id,
            article_id=article_id,
            backup_path=str(payload.get("backup_path") or "").strip(),
        )

    def handle_latex_improve_ops(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_tex_improve import list_improve_operations

        return list_improve_operations()

    def handle_latex_tex_improve(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .latex_articles import _resolve_article_tex
        from .latex_tex_improve import run_latex_improve

        content = payload.get("content")
        if content is None:
            _root, tex, err = _resolve_article_tex(workspace_id, article_id)
            if tex is None:
                return {"ok": False, "error": err or "artigo não encontrado"}
            try:
                content = tex.read_text(encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
        result = run_latex_improve(
            operation=str(payload.get("operation") or ""),
            content=str(content),
            scope=str(payload.get("scope") or "selection"),
            selection_start=int(payload.get("selection_start") or 0),
            selection_end=int(payload.get("selection_end") or 0),
            cursor_offset=int(payload.get("cursor_offset") or 0),
            custom_instruction=str(payload.get("custom_instruction") or ""),
            workspace_id=workspace_id,
            article_id=article_id,
        )
        if not result.get("ok"):
            return result
        if result.get("changed") and result.get("content") is not None and bool(payload.get("save_after")):
            from .latex_tex_edit import analyze_tex

            save_payload = {
                "workspace_id": workspace_id,
                "article_id": article_id,
                "content": result["content"],
                "backup": bool(payload.get("backup", True)),
            }
            saved = self.handle_latex_tex_save(save_payload, user)
            result["saved"] = saved.get("ok", False)
            if saved.get("ok"):
                result["path"] = saved.get("path")
                result["backup_path"] = saved.get("backup_path")
            else:
                result["save_error"] = saved.get("error")
            result["analysis"] = analyze_tex(str(result["content"]))
        return result

    def handle_latex_tex_save(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        if "content" not in payload:
            return {"ok": False, "error": "content is required"}
        content = str(payload.get("content") or "")
        backup = bool(payload.get("backup", True))
        compile_after = bool(payload.get("compile_after"))
        repair_on_failure = bool(payload.get("repair_on_failure", True))
        model_id = str(payload.get("model_id") or "").strip() or None
        chat_cfg = None
        oc = getattr(self.cfg, "openclaw_chat", None)
        if oc is not None and getattr(oc, "enabled", True):
            chat_cfg = oc
        from .latex_articles import _resolve_article_tex, compile_article

        if "article_objective" in payload or "section_objectives" in payload:
            from .latex_tex_edit import apply_objectives

            content = apply_objectives(
                content,
                article_objective=payload.get("article_objective"),
                section_objectives=payload.get("section_objectives"),
            )

        _root, tex, err = _resolve_article_tex(workspace_id, article_id)
        if tex is None:
            return {"ok": False, "error": err or "artigo não encontrado"}
        response: dict = {
            "ok": True,
            "workspace_id": workspace_id,
            "article_id": article_id,
            "path": str(tex),
        }
        # Centralized, data-loss-safe write: blocks accidental truncation of a
        # substantial .tex with empty/near-empty content, backs up, writes
        # atomically, and verifies. Prevents the "disappearing LaTeX" wipe.
        from .latex_safe_write import safe_write_tex

        force = bool(payload.get("force") or payload.get("allow_truncate"))
        write_res = safe_write_tex(
            tex, content, backup=backup, force=force, label="latex_tex_save"
        )
        if not write_res.get("ok"):
            out = {"ok": False, "error": write_res.get("error", "falha ao salvar")}
            for k in ("blocked_truncate", "existing_bytes", "new_bytes", "guard_backup_path"):
                if k in write_res:
                    out[k] = write_res[k]
            return out
        if write_res.get("backup_path"):
            response["backup_path"] = write_res["backup_path"]
        response["bytes_written"] = write_res.get("bytes_written", len(content.encode("utf-8")))
        # Persist every article objective to the database on save so they
        # survive even if the % qclaw-objectives comment is stripped later.
        try:
            from .latex_objectives_db import get_objectives, save_objectives
            from .latex_tex_edit import extract_objectives, merge_objectives

            objectives = extract_objectives(content)
            db_objectives = None
            try:
                db_objectives = get_objectives(workspace_id, article_id)
            except Exception:
                db_objectives = None
            merged = merge_objectives(objectives, db_objectives)
            stored = save_objectives(
                workspace_id=workspace_id,
                article_id=article_id,
                article_objective=merged.get("article_objective"),
                section_objectives=merged.get("section_objectives"),
                path=str(tex),
            )
            response["objectives_saved"] = bool(stored.get("ok"))
            if stored.get("ok"):
                response["article_objective"] = stored.get("article_objective", "")
                response["section_objectives"] = stored.get("section_objectives", [])
                response["section_count"] = stored.get("section_count", 0)
            else:
                response["objectives_error"] = stored.get("error")
        except Exception as exc:  # pragma: no cover - persistence is best effort
            response["objectives_saved"] = False
            response["objectives_error"] = f"{type(exc).__name__}: {exc}"
        if compile_after:
            compiled = compile_article(
                workspace_id, article_id,
                repair_on_failure=repair_on_failure,
                chat_cfg=chat_cfg,
                model_id=model_id,
            )
            response["compile"] = compiled
            if not compiled.get("ok"):
                response["ok"] = False
                response["error"] = compiled.get("error", "compile failed")
            elif tex.with_suffix(".pdf").is_file():
                from .latex_tex_edit import _page_count_for_tex

                pages, _ = _page_count_for_tex(tex)
                if pages is not None:
                    response["page_count"] = pages
        return response

    def handle_latex_pdf_path(
        self, query: dict, user: Optional[UserRecord]
    ) -> tuple[Optional[Path], Optional[str]]:
        denied = self._latex_auth(user)
        if denied:
            return None, str(denied.get("error") or "not authenticated")
        from .latex_articles import resolve_pdf_file

        return resolve_pdf_file(
            str(query.get("workspace_id") or ""),
            str(query.get("article_id") or ""),
        )

    def handle_latex_figure_path(
        self, query: dict, user: Optional[UserRecord]
    ) -> tuple[Optional[Path], Optional[str]]:
        denied = self._latex_auth(user)
        if denied:
            return None, str(denied.get("error") or "not authenticated")
        from .article_images import resolve_figure_image_file

        figure_raw = query.get("figure") or query.get("figure_index")
        try:
            figure_index = int(figure_raw)
        except (TypeError, ValueError):
            return None, "figure is required"
        return resolve_figure_image_file(
            str(query.get("workspace_id") or ""),
            str(query.get("article_id") or ""),
            figure_index,
        )

    def handle_latex_quality_list(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .article_quality import list_quality_report

        ws = str(query.get("workspace_id") or "").strip() or None
        return list_quality_report(workspace_id=ws)

    def handle_latex_quality_detail(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .article_quality import score_article

        wid = str(query.get("workspace_id") or "")
        aid = str(query.get("article_id") or "")
        result = score_article(wid, aid)
        if not result.get("ok"):
            return result
        from .latex_articles import list_workspaces

        ws_name = wid
        for ws in list_workspaces():
            if ws.get("id") == wid:
                ws_name = str(ws.get("name") or wid)
                break
        result["workspace_name"] = ws_name
        return result

    def handle_latex_templates_list(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_templates_api import list_templates_for_ui

        return list_templates_for_ui(
            query=str(query.get("query") or query.get("q") or ""),
            tag=str(query.get("tag") or ""),
        )

    def handle_latex_template_upload(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_templates_api import upload_template_from_base64

        return upload_template_from_base64(
            data_base64=str(payload.get("data_base64") or ""),
            name=str(payload.get("name") or ""),
            filename=str(payload.get("filename") or payload.get("file_name") or ""),
        )

    def handle_latex_template_delete(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_templates_api import delete_template_for_ui

        return delete_template_for_ui(
            str(payload.get("template_id") or payload.get("id") or "")
        )

    def handle_latex_template_apply(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_templates_api import apply_template_to_workspace

        return apply_template_to_workspace(
            template_id=str(payload.get("template_id") or payload.get("id") or ""),
            workspace_id=str(payload.get("workspace_id") or ""),
            article_id=str(payload.get("article_id") or ""),
            dry_run=bool(payload.get("dry_run")),
        )

    def handle_latex_template_get(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_templates_api import get_template_detail

        return get_template_detail(str(query.get("template_id") or query.get("id") or ""))

    def handle_latex_template_preview(
        self, query: dict, user: Optional[UserRecord]
    ) -> tuple[Optional[Path], Optional[str]]:
        denied = self._latex_auth(user)
        if denied:
            return None, str(denied.get("error") or "not authenticated")
        from .latex_templates_api import resolve_template_preview_file

        return resolve_template_preview_file(str(query.get("template_id") or query.get("id") or ""))

    def handle_latex_editor(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .chat_latex_editor import dispatch_latex_editor_action, build_latex_editor_widget

        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        body = dict(payload.get("body") or payload)

        if not workspace_id or not article_id or not action:
            return {
                "ok": False,
                "error": "workspace_id, article_id, and action are required",
            }

        try:
            # Cache widget build for 10s to avoid redundant workspace
            # resolution and figure sync during rapid chat interactions.
            import time

            cache = getattr(self, "_widget_cache", None)
            if cache is None:
                cache = {}
                self._widget_cache = cache
            cache_key = (workspace_id, article_id)
            cached = cache.get(cache_key)
            now = time.monotonic()
            if cached and (now - cached["ts"]) < 10.0 and cached["widget"].get("status") != "error":
                widget = dict(cached["widget"])
            else:
                widget = build_latex_editor_widget(
                    workspace_id=workspace_id,
                    article_id=article_id,
                )
                cache[cache_key] = {"widget": widget, "ts": now}
            if widget.get("status") == "error":
                return {"ok": False, "error": widget.get("error")}

            # Load team context (team_info + kanban) if team_id is provided
            team_id = str(body.get("team_id") or "").strip()
            if team_id and user and hasattr(self, "accounts"):
                try:
                    team_rec = self.accounts.get_team(team_id, user.id)
                    if team_rec is not None:
                        team_info = team_rec.to_public()
                        try:
                            team_info["__kanban"] = self.accounts.read_kanban(team_rec.id, user.id)
                        except Exception:
                            team_info["__kanban"] = None
                        try:
                            from .team_normas_ops import list_normas
                            nr = list_normas(team_rec.id)
                            team_info["__normas"] = nr.get("items") or []
                        except Exception:
                            team_info["__normas"] = []
                        body["__team_info"] = team_info
                except Exception:
                    pass

            result = dispatch_latex_editor_action(widget, action, body)
            return {"ok": True, **result}
        except Exception as e:
            return {
                "ok": False,
                "error": f"editor action failed: {str(e)}",
            }

    def handle_latex_improvements_list(
        self, query: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        wid = str(query.get("workspace_id") or "").strip()
        aid = str(query.get("article_id") or "").strip()
        if not wid or not aid:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .article_improvements_store import get_improvement_points

        return get_improvement_points(wid, aid)

    def handle_latex_improvements_history(
        self, query: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        wid = str(query.get("workspace_id") or "").strip()
        aid = str(query.get("article_id") or "").strip()
        if not wid or not aid:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        mode = str(query.get("mode") or "history").strip()
        if mode == "score":
            from .article_improvements_store import score_history

            return score_history(wid, aid)
        from .article_improvements_store import list_evaluations

        return list_evaluations(
            wid,
            aid,
            limit=int(query.get("limit") or 20),
            source=str(query.get("source") or "").strip() or None,
        )

    def handle_latex_improvements_resolve(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        wid = str(payload.get("workspace_id") or "").strip()
        aid = str(payload.get("article_id") or "").strip()
        record_id = str(payload.get("record_id") or "").strip()
        if not wid or not aid or not record_id:
            return {
                "ok": False,
                "error": "workspace_id, article_id, and record_id are required",
            }
        issue_idx = payload.get("issue_index")
        rec_idx = payload.get("rec_index")
        if issue_idx is not None:
            from .article_improvements_store import mark_resolved

            return mark_resolved(wid, aid, record_id, int(issue_idx))
        if rec_idx is not None:
            from .article_improvements_store import mark_recommendation_done

            return mark_recommendation_done(wid, aid, record_id, int(rec_idx))
        return {"ok": False, "error": "issue_index or rec_index is required"}

    def handle_latex_improvements_evaluate(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        wid = str(payload.get("workspace_id") or "").strip()
        aid = str(payload.get("article_id") or "").strip()
        if not wid or not aid:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .article_quality import score_article
        from .article_improvements_store import save_evaluation, get_improvement_points

        scored = score_article(wid, aid)
        if not scored.get("ok"):
            return scored
        save_result = save_evaluation(wid, aid, "heuristic", scored)
        improvements = get_improvement_points(wid, aid)
        return {
            "ok": True,
            "evaluation": scored,
            "record_id": save_result.get("record_id"),
            "improvements": improvements,
        }

    def _latex_actor_id(self, user: Optional[UserRecord]) -> str:
        if user is None:
            return ""
        return str(getattr(user, "login", None) or getattr(user, "id", None) or "")

    def handle_latex_tex_version_save(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_tex_versions import dispatch_latex_tex_version_save

        body = dict(payload or {})
        if user and not body.get("created_by"):
            body["created_by"] = self._latex_actor_id(user)
        return dispatch_latex_tex_version_save(body)

    def handle_latex_tex_version_list(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_tex_versions import dispatch_latex_tex_version_list

        return dispatch_latex_tex_version_list(dict(query or {}))

    def handle_latex_tex_version_get(self, query: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_tex_versions import dispatch_latex_tex_version_get

        return dispatch_latex_tex_version_get(dict(query or {}))

    def handle_latex_tex_version_restore(self, payload: dict, user: Optional[UserRecord]) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        from .latex_tex_versions import dispatch_latex_tex_version_restore

        return dispatch_latex_tex_version_restore(dict(payload or {}))

    def handle_latex_objectives_md(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._latex_auth(user)
        if denied:
            return denied
        workspace_id = str(payload.get("workspace_id") or "").strip()
        article_id = str(payload.get("article_id") or "").strip()
        if not workspace_id or not article_id:
            return {"ok": False, "error": "workspace_id and article_id are required"}
        from .latex_articles import _resolve_article_tex, list_workspaces
        from .latex_tex_edit import analyze_tex, extract_objectives

        root, tex, err = _resolve_article_tex(workspace_id, article_id)
        if tex is None or root is None:
            return {"ok": False, "error": err or "artigo não encontrado"}
        try:
            content = tex.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        objectives = extract_objectives(content)
        analysis = analyze_tex(content)

        article_objective = str(
            payload.get("article_objective")
            or objectives.get("article_objective")
            or ""
        ).strip()
        section_rows = _normalize_section_objectives(payload.get("section_objectives"))
        if not section_rows:
            section_rows = _normalize_section_objectives(
                objectives.get("section_objectives")
            )
        section_map: dict[str, str] = {}
        for row in section_rows:
            title = str(row.get("title") or "").strip()
            if title:
                section_map[title] = str(row.get("objective") or "").strip()
        merged_sections: list[dict[str, str]] = []
        for row in analysis.get("sections") or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            merged_sections.append(
                {
                    "title": title,
                    "objective": section_map.get(title) or _suggest_section_objective(title),
                }
            )
        if not merged_sections:
            merged_sections = section_rows

        workspace_name = workspace_id
        for ws in list_workspaces():
            if str(ws.get("id") or "") == workspace_id:
                workspace_name = str(ws.get("name") or workspace_id)
                break
        markdown = _render_objectives_markdown(
            article_title=tex.stem,
            article_objective=article_objective,
            section_objectives=merged_sections,
            workspace_name=workspace_name,
            article_id=article_id,
        )
        md_path = tex.with_name(f"{tex.stem}-objetivos.md")
        try:
            md_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"falha ao salvar markdown: {exc}"}

        created_cards: list[dict[str, Any]] = []
        failed_cards: list[dict[str, Any]] = []
        create_cards = bool(payload.get("create_cards", True))
        team_id = str(payload.get("team_id") or "").strip()
        column = str(payload.get("column") or "todo").strip().lower() or "todo"
        if create_cards:
            if not team_id:
                failed_cards.append(
                    {
                        "scope": "cards",
                        "error": "team_id ausente; cards não foram criados",
                    }
                )
            else:
                card_payloads: list[tuple[str, str]] = []
                if article_objective:
                    card_payloads.append(
                        (
                            f"[Artigo] Objetivo geral — {tex.stem}",
                            article_objective,
                        )
                    )
                for row in merged_sections:
                    section_title = str(row.get("title") or "").strip()
                    if not section_title:
                        continue
                    section_objective = str(row.get("objective") or "").strip()
                    if not section_objective:
                        continue
                    card_payloads.append(
                        (
                            f"[Seção] {section_title}",
                            section_objective,
                        )
                    )
                rel_md = md_path.relative_to(root).as_posix()
                for title, desc in card_payloads:
                    created = self.handle_chat_kanban_create_card(
                        {
                            "team_id": team_id,
                            "title": title,
                            "description": f"{desc}\n\nFonte: `{rel_md}`",
                            "column": column,
                        },
                        user,
                    )
                    if created.get("ok"):
                        created_cards.append(
                            {
                                "task_id": created.get("task_id"),
                                "title": title,
                                "workdir": created.get("workdir"),
                            }
                        )
                    else:
                        failed_cards.append(
                            {
                                "title": title,
                                "error": created.get("error") or "falha ao criar card",
                            }
                        )

        return {
            "ok": True,
            "workspace_id": workspace_id,
            "article_id": article_id,
            "markdown_path": str(md_path),
            "markdown_rel_path": md_path.relative_to(root).as_posix(),
            "article_objective": article_objective,
            "section_objectives": merged_sections,
            "cards_created": created_cards,
            "cards_failed": failed_cards,
            "markdown": markdown,
        }

"""Team WhatsApp, email, contacts, and group-message handlers."""

from __future__ import annotations

import base64
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from .accounts import UserRecord


class MonitorTeamCommsMixin:
    def handle_team_whatsapp_numbers_get(
        self, team_id: str, user: Optional[UserRecord]
    ) -> dict:
        """List WhatsApp numbers registered for a team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "team_id": team.id,
            "team_name": team.name,
            "whatsapp_numbers": list(team.whatsapp_numbers),
        }

    def handle_team_whatsapp_numbers_set(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Replace the WhatsApp numbers list for a team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        numbers = payload.get("numbers") or payload.get("whatsapp_numbers") or []
        if not isinstance(numbers, list):
            return {"ok": False, "error": "numbers deve ser uma lista"}
        try:
            team = self.accounts.set_team_whatsapp_numbers(
                team_id, actor.id, [str(n) for n in numbers]
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "team_id": team.id,
            "team_name": team.name,
            "whatsapp_numbers": list(team.whatsapp_numbers),
        }

    def handle_team_whatsapp_number_add(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Add a single WhatsApp number to the team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        number = str(payload.get("number") or "").strip()
        if not number:
            return {"ok": False, "error": "number is required"}
        try:
            team = self.accounts.add_team_whatsapp_number(
                team_id, actor.id, number
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "team_id": team.id,
            "team_name": team.name,
            "whatsapp_numbers": list(team.whatsapp_numbers),
        }

    def handle_team_whatsapp_number_remove(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Remove a WhatsApp number from the team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        number = str(payload.get("number") or "").strip()
        if not number:
            return {"ok": False, "error": "number is required"}
        try:
            team = self.accounts.remove_team_whatsapp_number(
                team_id, actor.id, number
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "team_id": team.id,
            "team_name": team.name,
            "whatsapp_numbers": list(team.whatsapp_numbers),
        }

    def handle_team_notify_emails_get(
        self, team_id: str, user: Optional[UserRecord]
    ) -> dict:
        """Get notify_emails for a team (independent of WhatsApp group)."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # Merge top-level notify_emails with any set inside whatsapp_group
        emails = list(team.notify_emails)
        wg_emails = list((team.whatsapp_group or {}).get("notify_emails") or [])
        for e in wg_emails:
            if e not in emails:
                emails.append(e)
        return {
            "ok": True,
            "team_id": team.id,
            "team_name": team.name,
            "notify_emails": emails,
        }

    def handle_team_notify_emails_set(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Set/replace notify_emails on a team (independent of WhatsApp group)."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        emails = payload.get("emails") or payload.get("notify_emails") or []
        if not isinstance(emails, list):
            return {"ok": False, "error": "emails deve ser uma lista de strings"}
        clean_emails = []
        for e in emails:
            e_str = str(e).strip()
            if e_str and "@" in e_str:
                clean_emails.append(e_str)
            elif e_str:
                return {"ok": False, "error": f"Email inválido: {e_str}"}
        try:
            team = self.accounts.update_team(
                team_id, actor.id, {"notify_emails": clean_emails}
            )
            return {
                "ok": True,
                "team_id": team.id,
                "team_name": team.name,
                "notify_emails": list(team.notify_emails),
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def handle_team_contacts_get(self, team_id: str, user: Optional[UserRecord]) -> dict:
        """List all contacts (members) of a team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "team_id": team.id,
            "team_name": team.name,
            "contacts": team.contacts,
            "count": len(team.contacts),
        }

    def handle_team_contacts_upsert(self, team_id: str, payload: dict, user: Optional[UserRecord]) -> dict:
        """Add or update a contact in the team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        contact = payload.get("contact")
        if not isinstance(contact, dict):
            return {"ok": False, "error": "contact deve ser um objeto"}
        if (
            not str(contact.get("name") or "").strip()
            and not str(contact.get("email") or "").strip()
            and not str(contact.get("whatsapp") or contact.get("phone") or "").strip()
        ):
            return {"ok": False, "error": "contact precisa ter ao menos name, email ou WhatsApp"}
        add_to_allowlist = payload.get("add_to_allowlist", False)
        if isinstance(add_to_allowlist, str):
            add_to_allowlist = add_to_allowlist.strip().lower() in ("1", "true", "yes")
        try:
            team = self.accounts.update_team(
                team_id,
                actor.id,
                {
                    "upsert_contact": contact,
                    "add_to_allowlist": bool(add_to_allowlist),
                },
            )
            return {"ok": True, "team_id": team.id, "contacts": team.contacts}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def handle_team_contacts_remove(self, team_id: str, payload: dict, user: Optional[UserRecord]) -> dict:
        """Remove a contact from the team by id or email."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        uid = str(payload.get("id") or payload.get("email") or "").strip()
        if not uid:
            return {"ok": False, "error": "id ou email do contato é obrigatório"}
        try:
            team = self.accounts.update_team(team_id, actor.id, {"remove_contact": uid})
            return {"ok": True, "team_id": team.id, "contacts": team.contacts}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def handle_team_whatsapp_send(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Send a WhatsApp message to a specific number within the team's scope."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        from .whatsapp_team_guard import team_whatsapp_send

        message = str(payload.get("message") or payload.get("text") or "").strip()
        recipient = str(payload.get("recipient") or payload.get("to") or "").strip()
        wait = bool(payload.get("wait", False))
        if isinstance(payload.get("wait"), str):
            wait = payload["wait"].strip().lower() in ("1", "true", "yes")
        if not recipient:
            return {"ok": False, "error": "recipient is required"}
        return team_whatsapp_send(
            store=self.accounts,
            team_id=team_id,
            user_id=actor.id,
            recipient=recipient,
            message=message,
            wait=wait,
        )

    def handle_team_whatsapp_broadcast(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Send a WhatsApp message to ALL numbers registered for the team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        from .whatsapp_team_guard import team_whatsapp_broadcast

        message = str(payload.get("message") or payload.get("text") or "").strip()
        wait = bool(payload.get("wait", False))
        if isinstance(payload.get("wait"), str):
            wait = payload["wait"].strip().lower() in ("1", "true", "yes")
        return team_whatsapp_broadcast(
            store=self.accounts,
            team_id=team_id,
            user_id=actor.id,
            message=message,
            wait=wait,
        )

    def handle_team_whatsapp_send_to_group(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Send a WhatsApp message/file to the team's linked group (JID @g.us)."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        from .whatsapp_team_guard import team_whatsapp_send_to_group

        message = str(payload.get("message") or payload.get("text") or "").strip()
        file_path = str(payload.get("file_path") or "").strip()
        caption = str(payload.get("caption") or "").strip()
        wait = bool(payload.get("wait", False))
        if isinstance(payload.get("wait"), str):
            wait = payload["wait"].strip().lower() in ("1", "true", "yes")
        return team_whatsapp_send_to_group(
            store=self.accounts,
            team_id=team_id,
            user_id=actor.id,
            message=message,
            file_path=file_path or None,
            caption=caption or None,
            wait=wait,
        )

    def handle_team_group_messages_get(
        self, team_id: str, query: dict, user: Optional[UserRecord]
    ) -> dict:
        """Return paginated WhatsApp group messages for a team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        limit = min(int(query.get("limit") or 50), 200)
        offset = max(int(query.get("offset") or 0), 0)
        search = (query.get("search") or "").strip() or None
        since_str = (query.get("since") or "").strip()
        since = float(since_str) if since_str else None

        # Try fetching by team_id first
        result = self.group_message_store.list_messages(
            team_id,
            limit=limit,
            offset=offset,
            since=since,
            search=search,
        )

        # If no messages found and team has a whatsapp_group configured,
        # also search by group_jid (messages stored before linking)
        if result.get("total", 0) == 0 and team.whatsapp_group:
            group_jid = str(
                team.whatsapp_group.get("group_jid") or ""
            ).strip()
            if group_jid:
                result = self.group_message_store.list_messages(
                    group_jid,
                    limit=limit,
                    offset=offset,
                    since=since,
                    search=search,
                )

        return result

    def handle_team_email_messages_get(
        self, team_id: str, query: dict, user: Optional[UserRecord]
    ) -> dict:
        """Return paginated email messages stored for a team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        limit = min(int(query.get("limit") or 50), 200)
        offset = max(int(query.get("offset") or 0), 0)
        search = (query.get("search") or "").strip() or None
        since_str = (query.get("since") or "").strip()
        since = float(since_str) if since_str else None
        linked_email = (query.get("linked_email") or "").strip() or None

        return self.team_email_message_store.list_messages(
            team_id,
            limit=limit,
            offset=offset,
            since=since,
            search=search,
            linked_email=linked_email,
        )

    def handle_team_email_sync(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Sync Gmail messages for emails linked to the team."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        from .team_email_sync import sync_team_emails

        days = int(payload.get("days") or 90)
        limit = int(payload.get("limit") or 150)
        emails = list(team.linked_emails)
        override = payload.get("linked_emails") or payload.get("emails")
        if isinstance(override, list) and override:
            emails = [str(e).strip().lower() for e in override if str(e).strip()]

        return sync_team_emails(
            self.team_email_message_store,
            team_id=team.id,
            linked_emails=emails,
            days=days,
            limit=limit,
        )

    def handle_team_email_send(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        """Send an email from team SMTP settings with optional attachments."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        to_raw = payload.get("to") or payload.get("to_emails") or []
        if isinstance(to_raw, str):
            to_raw = [x.strip() for x in to_raw.replace(";", ",").split(",")]
        if not isinstance(to_raw, list):
            return {"ok": False, "error": "to deve ser lista ou string"}
        to_emails = [str(x).strip() for x in to_raw if str(x).strip()]
        if not to_emails:
            return {"ok": False, "error": "destinatário obrigatório"}

        cc_raw = payload.get("cc") or []
        if isinstance(cc_raw, str):
            cc_raw = [x.strip() for x in cc_raw.replace(";", ",").split(",")]
        if not isinstance(cc_raw, list):
            return {"ok": False, "error": "cc deve ser lista ou string"}
        cc_emails = [str(x).strip() for x in cc_raw if str(x).strip()]

        bcc_raw = payload.get("bcc") or []
        if isinstance(bcc_raw, str):
            bcc_raw = [x.strip() for x in bcc_raw.replace(";", ",").split(",")]
        if not isinstance(bcc_raw, list):
            return {"ok": False, "error": "bcc deve ser lista ou string"}
        bcc_emails = [str(x).strip() for x in bcc_raw if str(x).strip()]

        subject = str(payload.get("subject") or "").strip()
        if not subject:
            return {"ok": False, "error": "assunto obrigatório"}
        html_body = str(payload.get("html_body") or payload.get("body_html") or "").strip()
        text_body = str(payload.get("text_body") or payload.get("body_text") or "").strip()
        if not html_body and not text_body:
            return {"ok": False, "error": "conteúdo do email obrigatório"}

        wg = team.whatsapp_group if isinstance(team.whatsapp_group, dict) else {}
        smtp_host = str(payload.get("smtp_host") or wg.get("smtp_host") or "smtp.gmail.com").strip()
        smtp_port = int(payload.get("smtp_port") or wg.get("smtp_port") or 587)
        smtp_user = str(payload.get("smtp_user") or wg.get("smtp_user") or "").strip()
        from_address = str(payload.get("from") or payload.get("from_address") or wg.get("smtp_from") or smtp_user).strip()
        smtp_password_env = str(payload.get("smtp_password_env") or wg.get("smtp_password_env") or "TEAM_SMTP_PASSWORD").strip()
        if not from_address:
            return {"ok": False, "error": "remetente não configurado"}
        smtp_password = os.environ.get(smtp_password_env, "").strip()
        if not smtp_password:
            return {
                "ok": False,
                "error": f"senha SMTP ausente (defina {smtp_password_env})",
            }

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_address
        msg["To"] = ", ".join(to_emails)
        if cc_emails:
            msg["Cc"] = ", ".join(cc_emails)
        if text_body and html_body:
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
        elif html_body:
            msg.set_content(html_body, subtype="html")
        else:
            msg.set_content(text_body)

        attachments = payload.get("attachments") or []
        if not isinstance(attachments, list):
            return {"ok": False, "error": "attachments deve ser uma lista"}
        attached = 0
        for idx, att in enumerate(attachments):
            if not isinstance(att, dict):
                return {"ok": False, "error": f"anexo {idx + 1} inválido"}
            name = str(att.get("name") or att.get("filename") or "").strip()
            data_b64 = str(att.get("data_base64") or att.get("content_base64") or "").strip()
            mime_type = str(att.get("mime_type") or "application/octet-stream").strip()
            if not name or not data_b64:
                return {"ok": False, "error": f"anexo {idx + 1} sem nome ou conteúdo"}
            try:
                raw = base64.b64decode(data_b64, validate=True)
            except Exception:
                return {"ok": False, "error": f"anexo {idx + 1} com base64 inválido"}
            maintype, subtype = "application", "octet-stream"
            if "/" in mime_type:
                maintype, subtype = mime_type.split("/", 1)
            msg.add_attachment(raw, maintype=maintype, subtype=subtype, filename=name)
            attached += 1

        recipients = to_emails + cc_emails + bcc_emails
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(smtp_user or from_address, smtp_password)
                server.send_message(msg, from_addr=from_address, to_addrs=recipients)
        except smtplib.SMTPException as e:
            return {"ok": False, "error": f"SMTP: {type(e).__name__}: {e}"}
        except OSError as e:
            return {"ok": False, "error": f"rede: {type(e).__name__}: {e}"}

        return {
            "ok": True,
            "team_id": team.id,
            "sent_to": len(recipients),
            "attachments": attached,
            "subject": subject,
        }

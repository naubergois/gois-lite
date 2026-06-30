"""Catálogo extenso de papéis (presets) para Hermes — TI, pesquisa e YouTube."""

from __future__ import annotations

import re
from typing import Any, Iterable

PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

CAT_IT = "operacoes-ti"
CAT_RESEARCH = "pesquisa-cientifica"
CAT_YOUTUBE = "youtube"
CAT_INFOPRODUTOS = "infoprodutos"


def _preset(role_id: str, label: str, category: str, scope: str) -> dict[str, str]:
    """Build one TEAM_ROLE_PRESETS entry. *scope* is the role description (after 'papel de')."""
    return {
        "id": role_id,
        "label": label,
        "category": category,
        "prompt": f"Crie um papel de {scope}.",
    }


def _slug(*parts: str) -> str:
    raw = "-".join(p.strip().lower() for p in parts if p and str(p).strip())
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    if not slug or not slug[0].isalnum():
        slug = f"role-{slug}" if slug else "role"
    return slug[:64]


def _dedupe_presets(presets: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for p in presets:
        pid = str(p.get("id") or "").strip()
        if not pid or pid in seen:
            continue
        if not PRESET_ID_RE.match(pid):
            raise ValueError(f"preset id inválido: {pid!r}")
        seen.add(pid)
        out.append(p)
    return out


def build_it_operations_presets() -> list[dict[str, str]]:
    """~120 papéis de operações de TI."""
    areas: list[tuple[str, str, list[tuple[str, str, str]]]] = [
        (
            "helpdesk",
            "Service desk",
            [
                ("l1", "Analista L1", "triagem, senhas, acessos básicos e base de conhecimento"),
                ("l2", "Analista L2", "diagnóstico intermediário, software e escalação"),
                ("l3", "Analista L3", "incidentes complexos, integrações e fornecedores"),
                ("coord", "Coordenador", "filas, SLA, métricas CSAT e coaching do time"),
                ("kb", "Gestor de KB", "artigos, macros e melhoria contínua da base"),
                ("vip", "Suporte VIP", "executivos e usuários críticos com SLA premium"),
                ("multilingue", "Suporte multilíngue", "atendimento global e tradução de tickets"),
                ("ferramentas", "Especialista ITSM", "ServiceNow/Jira, workflows e automação"),
            ],
        ),
        (
            "noc",
            "NOC",
            [
                ("analista", "Analista NOC", "monitoramento 24x7, alertas e escalação"),
                ("turno", "Operador de turno", "plantões, runbooks e comunicação de incidentes"),
                ("rede", "NOC rede", "links, BGP, latência e provedores"),
                ("apps", "NOC aplicações", "synthetic checks, APM e SLO"),
                ("coord", "Coordenador NOC", "escalas, post-mortems leves e métricas"),
                ("automacao", "Automação NOC", "runbooks automatizados e auto-remediação"),
                ("bridge", "Bridge manager", "war rooms e coordenação multi-time"),
                ("relatorios", "Relatórios NOC", "disponibilidade, MTTR e tendências"),
            ],
        ),
        (
            "soc",
            "SOC",
            [
                ("l1", "Analista SOC L1", "triagem de alertas SIEM e enriquecimento"),
                ("l2", "Analista SOC L2", "caça a ameaças e contenção inicial"),
                ("l3", "Analista SOC L3", "forense leve, malware e resposta avançada"),
                ("threat", "Threat hunter", "hipóteses, IOCs e detecções customizadas"),
                ("ir", "Resposta a incidentes", "playbooks IR, evidências e comunicação"),
                ("coord", "Coordenador SOC", "turnos, métricas e melhoria de detecção"),
                ("siem", "Engenheiro SIEM", "regras, parsers e integrações de log"),
                ("osint", "OSINT", "inteligência de ameaças e superfície externa"),
            ],
        ),
        (
            "sysadmin",
            "Sysadmin",
            [
                ("linux", "Linux", "servidores Linux, hardening e patching"),
                ("windows", "Windows Server", "AD adjunto, GPO e serviços Microsoft"),
                ("unix", "Unix/AIX", "sistemas legados e alta criticidade"),
                ("virtualizacao", "Virtualização", "VMware/Hyper-V, clusters e capacidade"),
                ("patch", "Gestão de patches", "janelas, compliance e exceções"),
                ("backup", "Backup ops", "jobs, restore tests e retenção"),
                ("automation", "Automação sysadmin", "Ansible/Puppet e scripts idempotentes"),
                ("sênior", "Sysadmin sênior", "arquitetura on-prem e mentoria"),
            ],
        ),
        (
            "rede",
            "Redes",
            [
                ("lan", "LAN/WLAN", "switches, Wi-Fi corporativo e VLANs"),
                ("wan", "WAN/MPLS", "links, SD-WAN e otimização"),
                ("firewall", "Firewall", "políticas, NAT e segmentação"),
                ("dns-dhcp", "DNS/DHCP", "zones internas, IPAM e resolução"),
                ("loadbalancer", "Load balancer", "F5/NGINX, health checks e SSL"),
                ("voip", "Telefonia/UC", "SIP, Teams telephony e QoS"),
                ("arquiteto", "Arquiteto de rede", "desenhos, capacidade e padrões"),
                ("cabos", "Infra física", "cabos, patch panels e documentação as-built"),
            ],
        ),
        (
            "storage",
            "Storage",
            [
                ("san", "SAN", "FC/iSCSI, zoning e performance"),
                ("nas", "NAS", "compartilhamentos, quotas e permissões"),
                ("objeto", "Object storage", "S3-compatible, lifecycle e políticas"),
                ("backup", "Backup storage", "dedupe, tape e air-gap"),
                ("dr", "Disaster recovery", "réplicas, RPO/RTO e testes de DR"),
                ("capacidade", "Capacidade", "forecast, tiering e custos"),
                ("cloud-storage", "Storage híbrido", "integração on-prem + nuvem"),
                ("sênior", "Storage sênior", "arquitetura e troubleshooting profundo"),
            ],
        ),
        (
            "dba-ops",
            "Operações de banco",
            [
                ("oracle", "DBA Oracle", "RAC, patching e tuning"),
                ("sqlserver", "DBA SQL Server", "Always On, jobs e segurança"),
                ("postgres", "DBA PostgreSQL", "réplicas, vacuum e extensões"),
                ("mysql", "DBA MySQL/MariaDB", "cluster, backups e slow query"),
                ("nosql", "DBA NoSQL", "Mongo/Redis operacional e sharding"),
                ("cloud-db", "DBA cloud", "RDS/Aurora/Cloud SQL gerenciado"),
                ("migração", "Migrações", "upgrade de versão e cutover"),
                ("observabilidade", "Observabilidade DB", "métricas, locks e planos"),
            ],
        ),
        (
            "cloud",
            "Cloud ops",
            [
                ("aws", "AWS ops", "EC2, IAM, custos e Well-Architected"),
                ("azure", "Azure ops", "subscriptions, RBAC e políticas"),
                ("gcp", "GCP ops", "projetos, VPC e billing"),
                ("multi", "Multi-cloud", "padrões, landing zones e governança"),
                ("finops", "FinOps", "tags, budgets e rightsizing"),
                ("rede-cloud", "Rede cloud", "VPC/VNet, peering e firewalls"),
                ("serverless", "Serverless ops", "Lambda/Functions, quotas e cold start"),
                ("sênior", "Cloud sênior", "arquitetura operacional e exceções"),
            ],
        ),
        (
            "kubernetes",
            "Kubernetes / plataforma",
            [
                ("admin", "Admin K8s", "clusters, upgrades e node pools"),
                ("sre-plat", "SRE plataforma", "SLO, capacity e incidentes de plataforma"),
                ("gitops", "GitOps", "Argo/Flux, manifests e promoção"),
                ("service-mesh", "Service mesh", "Istio/Linkerd, mTLS e traffic"),
                ("registry", "Registry", "Harbor/ECR, scanning e retenção"),
                ("secrets", "Secrets/PKI", "cert-manager, Vault e rotação"),
                ("cost", "Custo K8s", "requests/limits, VPA e waste"),
                ("devportal", "Developer portal", "Backstage, templates e golden paths"),
            ],
        ),
        (
            "monitoring",
            "Monitoramento",
            [
                ("prometheus", "Prometheus/Grafana", "métricas, dashboards e alertas"),
                ("apm", "APM", "traces, spans e diagnóstico de latência"),
                ("logs", "Logs", "ELK/OpenSearch, parsing e retenção"),
                ("synthetic", "Synthetic", "probes, journeys e disponibilidade"),
                ("oncall", "On-call", "escalas PagerDuty/Opsgenie e runbooks"),
                ("slo", "SLO/SLI", "error budgets e relatórios executivos"),
                ("cmdb", "CMDB", "inventário, relacionamentos e descoberta"),
                ("observability", "Observability lead", "estratégia OTel e padronização"),
            ],
        ),
        (
            "iam",
            "IAM / identidade",
            [
                ("ad", "Active Directory", "OU, GPO, trusts e hygiene"),
                ("sso", "SSO/SAML", "IdP, apps e provisioning"),
                ("pam", "PAM", "vault de senhas, sessões privilegiadas"),
                ("mfa", "MFA", "FIDO, políticas e exceções"),
                ("iga", "IGA", "certificação de acessos e workflows"),
                ("azure-ad", "Entra ID", "conditional access e B2B"),
                ("okta", "Okta/IdP SaaS", "grupos, policies e lifecycle"),
                ("auditoria", "Auditoria IAM", "SoD, logs e relatórios de compliance"),
            ],
        ),
        (
            "endpoint",
            "Endpoint / MDM",
            [
                ("intune", "Intune/MDM", "políticas, apps e compliance devices"),
                ("jamf", "macOS/Jamf", "ABM, perfis e patching Apple"),
                ("sccm", "SCCM/ConfigMgr", "imagens, collections e deploy"),
                ("vdi", "VDI", "Horizon/Citrix, pools e performance"),
                ("mobile", "Mobile", "iOS/Android corporativo e MAM"),
                ("dlp", "DLP endpoint", "políticas de vazamento e exceções"),
                ("imaging", "Imaging", "golden images, drivers e bare-metal"),
                ("kiosk", "Kiosk/loja", "dispositivos fixos e hardening"),
            ],
        ),
        (
            "datacenter",
            "Datacenter",
            [
                ("facilities", "Facilities", "energia, refrigeração e capacidade"),
                ("racks", "Racks/cabling", "patch, etiquetas e as-built"),
                ("hardware", "Hardware break-fix", "RMA, spare parts e inventário"),
                ("smart-hands", "Smart hands", "coordenação com colocation"),
                ("cage", "Segurança física", "badges, câmeras e visitas"),
                ("capacity-dc", "Capacidade DC", "U, kW e expansão"),
                ("migração-dc", "Migração DC", "racks, janelas e rollback"),
                ("sustentabilidade", "Green IT", "PUE, eficiência e descarte e-waste"),
            ],
        ),
        (
            "itil",
            "ITIL / processos",
            [
                ("incident", "Gestor de incidentes", "major incidents e comunicação"),
                ("problem", "Problem manager", "RCA, known errors e workarounds"),
                ("change", "Change manager", "CAB, risco e calendário"),
                ("release", "Release manager", "pacotes, janelas e rollback"),
                ("config", "Configuration manager", "CMDB e baselines"),
                ("catalog", "Catálogo de serviços", "ofertas, SLAs e preços"),
                ("continuidade", "BCP/continuidade", "BIA, planos e exercícios"),
                ("portfolio", "Portfolio IT", "demandas, priorização e roadmap"),
            ],
        ),
        (
            "governance",
            "Governança e compliance TI",
            [
                ("iso27001", "ISO 27001", "controles, evidências e auditorias"),
                ("soc2", "SOC 2", "trust principles e readiness"),
                ("lgpd", "Privacidade/LGPD", "dados pessoais em sistemas internos"),
                ("licenciamento", "Licenciamento", "SAM, contratos e true-up"),
                ("vendor", "Vendor manager", "fornecedores, SLAs e renovações"),
                ("procurement", "Compras TI", "RFP, PO e ativos"),
                ("training", "Capacitação TI", "trilhas, certificações e onboarding"),
                ("architecture-review", "Comitê arquitetura", "padrões, exceções e riscos"),
            ],
        ),
    ]
    out: list[dict[str, str]] = []
    for area_id, area_name, roles in areas:
        for suffix, role_label, scope_tail in roles:
            pid = _slug("it", area_id, suffix)
            label = f"{area_name} — {role_label}"
            scope = f"{label}: {scope_tail}"
            out.append(_preset(pid, label, CAT_IT, scope))
    return out


def build_scientific_research_presets() -> list[dict[str, str]]:
    """~120 papéis de pesquisa científica."""
    fields: list[tuple[str, str]] = [
        ("biologia", "Biologia"),
        ("quimica", "Química"),
        ("fisica", "Física"),
        ("matematica", "Matemática"),
        ("computacao", "Ciência da computação"),
        ("engenharia", "Engenharia"),
        ("medicina", "Medicina"),
        ("enfermagem", "Enfermagem"),
        ("farmacia", "Farmácia"),
        ("psicologia", "Psicologia"),
        ("neurociencia", "Neurociência"),
        ("economia", "Economia"),
        ("administracao", "Administração"),
        ("direito", "Direito"),
        ("historia", "História"),
        ("sociologia", "Sociologia"),
        ("antropologia", "Antropologia"),
        ("geografia", "Geografia"),
        ("astronomia", "Astronomia"),
        ("geologia", "Geologia"),
        ("ecologia", "Ecologia"),
        ("bioquimica", "Bioquímica"),
        ("genetica", "Genética"),
        ("microbiologia", "Microbiologia"),
        ("educacao", "Educação"),
        ("comunicacao", "Comunicação"),
        ("artes", "Artes"),
        ("filosofia", "Filosofia"),
        ("estatistica", "Estatística"),
        ("ciencia-dados", "Ciência de dados acadêmica"),
    ]
    roles: list[tuple[str, str, str]] = [
        ("pi", "Pesquisador principal (PI)", "linha de pesquisa, grants e publicações"),
        ("posdoc", "Pós-doutorando", "projetos, papers e colaboração interlab"),
        ("doutorado", "Doutorando", "tese, experimentos e revisão bibliográfica"),
        ("mestrado", "Mestrando", "dissertação, métodos e cronograma"),
        ("ic", "Iniciação científica", "bancada, protocolos e relatórios"),
        ("lab", "Técnico de laboratório", "equipamentos, EPIs e preparo de amostras"),
        ("stats", "Estatístico de pesquisa", "desenho experimental, power e análise"),
        ("gestor", "Gestor de projetos de pesquisa", "editais, orçamento e compliance"),
    ]
    out: list[dict[str, str]] = []
    for field_id, field_name in fields:
        for suffix, role_label, scope_tail in roles:
            pid = _slug("pesquisa", field_id, suffix)
            label = f"{field_name} — {role_label}"
            scope = f"{label} em {field_name.lower()}: {scope_tail}"
            out.append(_preset(pid, label, CAT_RESEARCH, scope))
    return out


def build_infoproduct_presets() -> list[dict[str, str]]:
    """~160 papéis para criação de cursos online e infoprodutos digitais."""
    niches: list[tuple[str, str]] = [
        ("tecnologia", "Tecnologia"),
        ("programacao", "Programação"),
        ("data", "Dados e IA"),
        ("negocios", "Negócios"),
        ("marketing", "Marketing digital"),
        ("financas", "Finanças"),
        ("vendas", "Vendas"),
        ("lideranca", "Liderança"),
        ("produtividade", "Produtividade"),
        ("dev-pessoal", "Desenvolvimento pessoal"),
        ("saude", "Saúde e bem-estar"),
        ("fitness", "Fitness"),
        ("idiomas", "Idiomas"),
        ("design", "Design e criatividade"),
        ("fotografia", "Fotografia e vídeo"),
        ("gastronomia", "Gastronomia"),
        ("juridico", "Direito"),
        ("rh", "RH e carreira"),
        ("educacao", "Educação infantil"),
        ("espiritualidade", "Espiritualidade"),
    ]
    functions: list[tuple[str, str, str]] = [
        ("coord", "Coordenador de produto", "briefing, cronograma, handoffs e entrega final"),
        (
            "designer-instrucional",
            "Designer instrucional",
            "módulos, objetivos de aprendizagem, sequência pedagógica e avaliações",
        ),
        ("redator", "Redator de aulas", "roteiro, texto das aulas, exercícios e materiais complementares"),
        ("roteirista", "Roteirista de vídeo", "hooks, narrativa por aula e storyboard para gravação"),
        (
            "produtor-video",
            "Produtor de vídeo",
            "vídeos avatar HeyGen, revisão de takes e entrega por aula",
        ),
        (
            "designer-slides",
            "Designer de slides",
            "apresentações Gamma, imagens de slide e identidade visual do curso",
        ),
        (
            "publicador",
            "Publicador de plataforma",
            "Hotmart, KDP, HTML offline, pacotes ZIP e checklist de publicação",
        ),
        (
            "copy-vendas",
            "Copywriter de vendas",
            "página de vendas, promessa, bônus, urgência e sequência de e-mails",
        ),
    ]
    out: list[dict[str, str]] = []
    for niche_id, niche_name in niches:
        for suffix, func_label, scope_tail in functions:
            pid = _slug("info", niche_id, suffix)
            label = f"Infoproduto {niche_name} — {func_label}"
            scope = f"{label}: {scope_tail} no nicho {niche_name.lower()}"
            out.append(_preset(pid, label, CAT_INFOPRODUTOS, scope))
    return out


def build_youtube_presets() -> list[dict[str, str]]:
    """~120 papéis do ecossistema YouTube / creator."""
    niches: list[tuple[str, str]] = [
        ("gaming", "Gaming"),
        ("tech", "Tecnologia"),
        ("educacao", "Educação"),
        ("culinaria", "Culinária"),
        ("fitness", "Fitness"),
        ("beleza", "Beleza e moda"),
        ("viagem", "Viagem"),
        ("musica", "Música"),
        ("humor", "Humor e entretenimento"),
        ("financas", "Finanças pessoais"),
        ("noticias", "Notícias e comentário"),
        ("ciencia", "Divulgação científica"),
        ("diy", "DIY e maker"),
        ("familia", "Família e lifestyle"),
        ("asmr", "ASMR"),
        ("esportes", "Esportes"),
        ("automoveis", "Automóveis"),
        ("pets", "Pets"),
        ("anime", "Anime e cultura pop"),
        ("livros", "Literatura e resenhas"),
        ("podcast-video", "Podcast em vídeo"),
        ("shorts", "Shorts verticais"),
        ("live", "Live streaming"),
        ("documentario", "Documentário"),
        ("reacao", "Reação e review"),
        ("infantil", "Conteúdo infantil (responsável)"),
        ("sustentabilidade", "Sustentabilidade"),
        ("politica", "Análise política (imparcial)"),
        ("saude", "Saúde e bem-estar"),
        ("fotografia", "Fotografia e vídeo"),
    ]
    functions: list[tuple[str, str, str]] = [
        ("criador", "Criador(a) apresentador(a)", "gravar, narrar, engajar e manter calendário"),
        ("roteiro", "Roteirista", "hooks, estrutura, pesquisa e revisão"),
        ("editor", "Editor de vídeo", "corte, ritmo, color e entrega"),
        ("thumb", "Designer de thumbnails", "CTR, A/B e identidade visual"),
        ("seo", "SEO YouTube", "títulos, tags, descrições e tendências"),
        ("community", "Community manager", "comentários, membros e moderação"),
        ("produtor", "Produtor executivo", "cronograma, equipe e orçamento"),
        ("analytics", "Analista de métricas", "retenção, RPM e experimentos"),
    ]
    out: list[dict[str, str]] = []
    for niche_id, niche_name in niches:
        for suffix, func_label, scope_tail in functions:
            pid = _slug("yt", niche_id, suffix)
            label = f"YouTube {niche_name} — {func_label}"
            scope = f"{label}: {scope_tail} no nicho {niche_name.lower()}"
            out.append(_preset(pid, label, CAT_YOUTUBE, scope))
    return out


def build_extended_role_presets() -> list[dict[str, str]]:
    """Todos os papéis estendidos (TI + pesquisa + YouTube), sem duplicatas."""
    combined = (
        build_it_operations_presets()
        + build_scientific_research_presets()
        + build_youtube_presets()
        + build_infoproduct_presets()
    )
    return _dedupe_presets(combined)


EXTENDED_ROLE_PRESETS: list[dict[str, str]] = build_extended_role_presets()

CAT_LABELS: dict[str, str] = {
    CAT_IT: "Operações TI",
    CAT_RESEARCH: "Pesquisa científica",
    CAT_YOUTUBE: "YouTube",
    CAT_INFOPRODUTOS: "Cursos e infoprodutos",
}


def role_preset_group_key(preset_id: str) -> str:
    """Stable group id for area / field / niche (all roles except the last suffix)."""
    parts = [p for p in str(preset_id or "").split("-") if p]
    if len(parts) < 3:
        return str(preset_id or "")
    head = parts[0]
    if head in ("it", "yt", "info"):
        return "-".join(parts[:2])
    if head == "pesquisa":
        return "-".join(parts[:-1])
    return str(preset_id or "")


def _group_label_from_role(label: str) -> str:
    text = str(label or "").strip()
    if " — " in text:
        return text.split(" — ", 1)[0].strip()
    return text


def group_extended_role_presets(
    presets: Iterable[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Cluster extended presets into area/field/niche groups for swarm templates."""
    rows = list(presets or EXTENDED_ROLE_PRESETS)
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        pid = str(row.get("id") or "").strip()
        if not pid:
            continue
        gkey = role_preset_group_key(pid)
        if not gkey or gkey == pid:
            continue
        grouped.setdefault(gkey, []).append(row)

    out: list[dict[str, Any]] = []
    for gkey in sorted(grouped):
        members = grouped[gkey]
        sample = members[0]
        category = str(sample.get("category") or "outros")
        out.append(
            {
                "id": gkey,
                "name": _group_label_from_role(str(sample.get("label") or gkey)),
                "category": category,
                "category_label": CAT_LABELS.get(category, category),
                "roles": members,
            }
        )
    return out

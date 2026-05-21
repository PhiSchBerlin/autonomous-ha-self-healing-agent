"""
Streamlit Dashboard — HA Self-Healing & Security AI Agent.

Zeigt in Echtzeit:
  - Agent-Status und Gesundheitsscores
  - Aktive Findings nach Schweregrad
  - Ausstehende Genehmigungsanfragen
  - Reparaturhistorie
  - Knowledge-Base-Statistiken
  - Prometheus-Metriken (Textansicht)

Starten: streamlit run dashboard/streamlit_app.py
Konfiguration via Umgebungsvariablen:
  AGENT_API_URL   — Standard: http://localhost:8765
  DASHBOARD_REFRESH_SECONDS — Standard: 30
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

API_URL = os.getenv("AGENT_API_URL", "http://localhost:8765")
# Externe URL für Browser-Links (API-Docs, etc.) — kann von localhost abweichen
EXTERNAL_API_URL = os.getenv("AGENT_EXTERNAL_API_URL", API_URL.replace("localhost", "192.168.178.48").replace("127.0.0.1", "192.168.178.48"))
REFRESH_SECONDS = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "30"))

st.set_page_config(
    page_title="HA Self-Healing Agent",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# API-Hilfsfunktionen
# ---------------------------------------------------------------------------


@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_json(path: str) -> dict[str, Any] | None:
    """Holt JSON-Daten vom Agent-Backend. Gibt None bei Fehler zurück."""
    try:
        resp = httpx.get(f"{API_URL}{path}", timeout=5.0)
        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]
    except Exception:
        return None


def post_json(
    path: str,
    payload: dict[str, Any],
    timeout: float = 180.0,
) -> dict[str, Any] | None:
    """Sendet einen POST-Request an das Backend."""
    try:
        resp = httpx.post(f"{API_URL}{path}", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]
    except Exception as exc:
        st.error(f"API-Fehler: {exc}")
        return None


def fetch_text(path: str) -> str | None:
    """Holt Text-Daten (z.B. Prometheus-Metriken) vom Backend."""
    try:
        resp = httpx.get(f"{API_URL}{path}", timeout=5.0)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🏠 HA Self-Healing Agent")
    st.caption(f"API: `{API_URL}`")
    st.markdown(f"[📖 API-Dokumentation]({EXTERNAL_API_URL}/docs)")

    page = st.radio(
        "Navigation",
        [
            "📊 Übersicht",
            "🔍 Findings",
            "🔧 Genehmigungen",
            "📜 Reparaturhistorie",
            "🧠 Knowledge Base",
            "📈 Metriken",
        ],
        label_visibility="collapsed",
    )

    st.divider()

    if st.button("🔄 Jetzt aktualisieren", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"Auto-Refresh alle {REFRESH_SECONDS}s")

    # Manuellen Audit auslösen
    st.divider()
    st.subheader("Aktionen")
    if st.button("▶️ Full Audit starten", use_container_width=True):
        result = post_json("/api/v1/agents/full-audit", {}, timeout=10.0)
        if result and "job_id" in result:
            st.success(f"Full Audit gestartet (Job: `{result['job_id']}`)")
            st.session_state["audit_job_id"] = result["job_id"]
        else:
            st.error("Audit konnte nicht gestartet werden")

    # Audit-Status anzeigen falls Job läuft
    job_id = st.session_state.get("audit_job_id")
    if job_id:
        job_status = fetch_json(f"/api/v1/agents/full-audit/{job_id}")
        if job_status:
            status_val = job_status.get("status", "unbekannt")
            if status_val == "completed":
                st.success("✅ Audit abgeschlossen")
                if st.button("Ergebnis ausblenden"):
                    del st.session_state["audit_job_id"]
            elif status_val == "failed":
                st.error(f"❌ Audit fehlgeschlagen: {job_status.get('error', '-')}")
                del st.session_state["audit_job_id"]
            else:
                st.info(f"⏳ Audit läuft... Status: `{status_val}`")

    # Letztes persistiertes Audit anzeigen
    latest_audit = fetch_json("/api/v1/agents/full-audit/latest")
    if isinstance(latest_audit, dict) and not latest_audit.get("detail"):
        saved_at = latest_audit.get("saved_at", "?")
        st.caption(f"Letztes Audit: {saved_at[:16].replace('T', ' ')}")


# ---------------------------------------------------------------------------
# Seite: Übersicht
# ---------------------------------------------------------------------------

def _severity_color(severity: str) -> str:
    return {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🔵",
        "info": "⚪",
    }.get(severity.lower(), "⚫")


def page_overview() -> None:
    st.header("📊 Agent-Übersicht")

    health = fetch_json("/api/v1/agents/health")
    obs_health = fetch_json("/observability/health")
    ks = fetch_json("/api/v1/knowledge/stats")

    # KPI-Zeile
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if health:
            status = health.get("status", "unknown")
            color = "🟢" if status == "ok" else "🔴"
            st.metric("Agent-Status", f"{color} {status.upper()}")
            mode = health.get("mode", "-")
            st.caption(f"Modus: **{mode}**")
        else:
            st.metric("Agent-Status", "🔴 NICHT ERREICHBAR")

    with col2:
        if obs_health:
            score = obs_health.get("health_score", 1.0)
            color = "🟢" if score >= 0.8 else ("🟡" if score >= 0.5 else "🔴")
            st.metric("HA Health-Score", f"{color} {score:.1%}")
        else:
            st.metric("HA Health-Score", "–")

    with col3:
        if obs_health:
            unavail = obs_health.get("unavailable_entities", [])
            st.metric("Unavailable Entities", len(unavail))
        else:
            st.metric("Unavailable Entities", "–")

    with col4:
        if ks:
            total = ks.get("total_entries", 0)
            st.metric("Knowledge-Einträge", total)
        else:
            st.metric("Knowledge-Einträge", "–")

    st.divider()

    # Ausstehende Genehmigungen
    repair_history = fetch_json("/api/v1/repair/history")
    repairs = repair_history.get("commits", []) if isinstance(repair_history, dict) else []
    if repairs:
        pending = [r for r in repairs if isinstance(r, dict) and r.get("status") == "proposed"]
        if pending:
            st.warning(f"⚠️ **{len(pending)} ausstehende Genehmigungsanfrage(n)**")
            for p in pending[:3]:
                st.write(f"- **{p.get('title', 'Unbekannt')}** — Risiko: `{p.get('risk_level', '-')}`")
            if len(pending) > 3:
                st.caption(f"… und {len(pending) - 3} weitere")

    # Knowledge-Kollektion Übersicht
    if ks:
        st.subheader("Knowledge Base")
        cols = st.columns(3)
        for i, (key, val) in enumerate(ks.items()):
            if key != "total_entries":
                with cols[i % 3]:
                    st.metric(key.replace("ha_", "").replace("_", " ").title(), val)

    # HA-Erreichbarkeit
    if obs_health:
        ha_ok = obs_health.get("ha_reachable", True)
        regs = obs_health.get("regression_count", 0)
        if not ha_ok:
            st.error("❌ Home Assistant ist nicht erreichbar!")
        elif regs > 0:
            st.warning(f"⚠️ {regs} Regression(en) nach letzter Reparatur erkannt")
        else:
            st.success("✅ Home Assistant läuft normal, keine Regressionen")


# ---------------------------------------------------------------------------
# Seite: Findings
# ---------------------------------------------------------------------------

def page_findings() -> None:
    st.header("🔍 Aktive Findings")

    # Security-Scan-Ergebnisse
    st.subheader("Security-Scan")
    with st.expander("Neuen Security-Scan starten"):
        config_path = st.text_input("HA-Config-Pfad", value="/config")
        if st.button("Scan starten"):
            result = post_json("/api/v1/security/scan", {"ha_config_path": config_path})
            if result:
                st.success(f"Scan abgeschlossen — {result.get('security_issues_count', 0)} LLM-Issues, {result.get('static_issues_count', 0)} statische Issues")
                st.cache_data.clear()

    # Zusammenfassung und Filterung
    summary = fetch_json("/api/v1/security/issues/summary")
    if isinstance(summary, dict) and summary.get("total", 0) > 0:
        total = summary["total"]
        cols = st.columns(5)
        cols[0].metric("🔴 Critical", summary.get("critical", 0))
        cols[1].metric("🟠 High", summary.get("high", 0))
        cols[2].metric("🟡 Medium", summary.get("medium", 0))
        cols[3].metric("🟢 Low", summary.get("low", 0))
        cols[4].metric("⚪ Info", summary.get("info", 0))

        sev_filter = st.selectbox(
            "Filtern nach Schweregrad",
            ["Alle", "critical", "high", "medium", "low", "info"],
            key="sec_sev_filter",
        )
        limit = st.slider("Maximale Anzahl anzeigen", 10, 500, 100, key="sec_limit")

        params = f"?limit={limit}"
        if sev_filter != "Alle":
            params += f"&severity={sev_filter}"
        security = fetch_json(f"/api/v1/security/issues{params}")
    else:
        security = fetch_json("/api/v1/security/issues?limit=100")

    if isinstance(security, list):
        if not security:
            st.info("Keine Security-Issues (noch kein Scan gelaufen oder alle gefiltert)")
        else:
            st.caption(f"{len(security)} Issue(s) angezeigt (persistent gespeichert)")
            for issue in security:
                sev = issue.get("severity", "info")
                icon = _severity_color(sev)
                with st.expander(f"{icon} [{sev.upper()}] {issue.get('title', 'Unbekannt')}"):
                    st.write(f"**Beschreibung:** {issue.get('description', '-')}")
                    st.write(f"**Datei:** `{issue.get('file_path', '-')}`")
                    st.write(f"**Zeile:** {issue.get('line_number', '-')}")
                    st.write(f"**Risiko-Score:** {issue.get('risk_score', '-')}")
    else:
        st.warning("Security-Endpunkt nicht erreichbar — bitte Addon neu starten")

    st.divider()

    # Log-Analyse-Findings
    st.subheader("Log-Analyse")
    with st.expander("Log-Analyse starten"):
        log_input = st.text_area(
            "Log-Zeilen (eine pro Zeile)",
            placeholder="2024-01-01 12:00:00 ERROR ...",
            height=150,
        )
        if st.button("Analyse starten") and log_input.strip():
            lines = [{"raw": line} for line in log_input.strip().splitlines()]
            result = post_json("/api/v1/agents/log-analysis", {"raw_logs": lines})
            if result:
                st.json(result)


# ---------------------------------------------------------------------------
# Seite: Genehmigungen
# ---------------------------------------------------------------------------

def page_approvals() -> None:
    st.header("🔧 Ausstehende Genehmigungsanfragen")

    repair_data = fetch_json("/api/v1/repair/history")
    repairs = repair_data.get("commits", []) if isinstance(repair_data, dict) else []

    if repair_data is None:
        st.error("Backend nicht erreichbar")
        return

    pending = [r for r in repairs if isinstance(r, dict) and r.get("status") == "proposed"]

    if not pending:
        st.success("✅ Keine ausstehenden Genehmigungsanfragen")
        return

    for repair in pending:
        repair_id = repair.get("id", "")
        title = repair.get("title", "Unbekannt")
        risk = repair.get("risk_level", "unknown")
        desc = repair.get("description", "-")
        rationale = repair.get("rationale", "-")

        icon = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(risk, "⚫")
        with st.expander(f"{icon} **{title}** — Risiko: `{risk}`", expanded=True):
            st.write(f"**Beschreibung:** {desc}")
            st.write(f"**Begründung:** {rationale}")

            changes = repair.get("changes", [])
            if changes:
                st.write(f"**Betroffene Dateien:** {len(changes)}")
                for ch in changes:
                    st.code(ch.get("diff", ""), language="diff")

            col_approve, col_reject = st.columns(2)
            with col_approve:
                if st.button(f"✅ Genehmigen", key=f"approve_{repair_id}"):
                    result = post_json(f"/api/v1/repair/{repair_id}/approve", {})
                    if result:
                        st.success("Genehmigt!")
                        st.cache_data.clear()
                        st.rerun()

            with col_reject:
                reject_reason = st.text_input("Ablehnungsgrund", key=f"reason_{repair_id}")
                if st.button(f"❌ Ablehnen", key=f"reject_{repair_id}"):
                    result = post_json(
                        f"/api/v1/repair/{repair_id}/reject",
                        {"reason": reject_reason},
                    )
                    if result:
                        st.warning("Abgelehnt")
                        st.cache_data.clear()
                        st.rerun()


# ---------------------------------------------------------------------------
# Seite: Reparaturhistorie
# ---------------------------------------------------------------------------

def page_repair_history() -> None:
    st.header("📜 Reparaturhistorie")

    repair_data = fetch_json("/api/v1/repair/history")
    repairs = repair_data.get("commits", []) if isinstance(repair_data, dict) else []

    if repair_data is None:
        st.error("Backend nicht erreichbar")
        return

    if not repairs:
        st.info("Noch keine Reparaturen durchgeführt")
        return

    # Statusfilter
    all_statuses = sorted({r.get("status", "unknown") for r in repairs if isinstance(r, dict)})
    selected = st.multiselect("Filter nach Status", all_statuses, default=all_statuses)
    filtered = [r for r in repairs if isinstance(r, dict) and r.get("status") in selected]

    st.caption(f"{len(filtered)} Einträge angezeigt (gesamt: {len(repairs)})")

    status_icons = {
        "applied": "✅",
        "proposed": "📋",
        "validated": "🔬",
        "approved": "👍",
        "rejected": "❌",
        "rolled_back": "↩️",
        "failed": "💥",
        "simulated": "🧪",
    }

    for repair in filtered:
        status = repair.get("status", "unknown")
        icon = status_icons.get(status, "❓")
        title = repair.get("title", "Unbekannt")
        risk = repair.get("risk_level", "-")
        created = repair.get("created_at", "-")

        with st.expander(f"{icon} **{title}** — `{status}` | Risiko: {risk}"):
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Erstellt:** {created}")
                st.write(f"**Risiko:** {risk}")
            with col2:
                st.write(f"**Beschreibung:** {repair.get('description', '-')}")
                st.write(f"**Git-Hash:** `{repair.get('git_commit_hash', '-')}`")

            if repair.get("validation_result"):
                st.json(repair["validation_result"])


# ---------------------------------------------------------------------------
# Seite: Knowledge Base
# ---------------------------------------------------------------------------

def page_knowledge() -> None:
    st.header("🧠 Knowledge Base")

    stats = fetch_json("/api/v1/knowledge/stats")
    if stats:
        cols = st.columns(len(stats))
        for i, (key, val) in enumerate(stats.items()):
            with cols[i]:
                label = key.replace("ha_", "").replace("_", " ").title()
                st.metric(label, val)

    st.divider()

    # Semantische Suche
    st.subheader("Semantische Suche")
    query = st.text_input("Suchbegriff", placeholder="MQTT Verbindungsfehler...")
    n_results = st.slider("Anzahl Ergebnisse", 1, 10, 5)

    if st.button("Suchen") and query.strip():
        result = post_json(
            "/api/v1/knowledge/search",
            {"query": query, "n_results": n_results},
        )
        if result and "results" in result:
            results = result["results"]
            if not results:
                st.info("Keine Ergebnisse gefunden")
            for entry in results:
                dist = entry.get("distance", 0)
                similarity = max(0.0, 1.0 - dist)
                with st.expander(
                    f"🔍 Ähnlichkeit: {similarity:.1%} — `{entry.get('id', '?')[:12]}...`"
                ):
                    st.text(entry.get("text", ""))
                    meta = entry.get("metadata", {})
                    if meta:
                        st.json(meta)

    st.divider()

    # Wissen hinzufügen
    st.subheader("Wissen hinzufügen")
    with st.expander("Neuen Eintrag erstellen"):
        topic = st.text_input("Thema")
        content = st.text_area("Inhalt")
        tags_raw = st.text_input("Tags (kommagetrennt)")
        source = st.text_input("Quelle", value="user")

        if st.button("Speichern") and topic and content:
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            result = post_json(
                "/api/v1/knowledge/store",
                {"topic": topic, "content": content, "tags": tags, "source": source},
            )
            if result:
                st.success(f"Gespeichert mit ID: `{result.get('id', '?')}`")
                st.cache_data.clear()

    st.divider()

    # Wissenseinträge auflisten und bearbeiten
    st.subheader("Einträge verwalten")
    coll_choice = st.selectbox(
        "Kollektion",
        ["ha_knowledge", "ha_findings", "ha_repairs"],
        key="kb_collection",
    )
    kb_list = fetch_json(f"/api/v1/knowledge/list?collection={coll_choice}&limit=200")
    if kb_list:
        entries = kb_list.get("entries", [])
        total = kb_list.get("total", 0)
        st.caption(f"{total} Einträge in `{coll_choice}`")

        for entry in entries:
            eid = entry.get("id", "?")
            meta = entry.get("metadata", {})
            label = meta.get("topic") or entry.get("text", "")[:60]
            with st.expander(f"📄 {label}  `{eid[:8]}...`"):
                st.text_area("Text", value=entry.get("text", ""), key=f"txt_{eid}", disabled=True)
                st.json(meta)

                if coll_choice == "ha_knowledge":
                    with st.form(key=f"edit_{eid}"):
                        new_topic = st.text_input("Thema", value=meta.get("topic", ""))
                        new_content = st.text_area("Inhalt", value=entry.get("text", "").split("\n", 1)[-1])
                        new_tags = st.text_input("Tags", value=meta.get("tags", ""))
                        submitted = st.form_submit_button("Speichern")
                        if submitted:
                            r = post_json(
                                f"/api/v1/knowledge/update/{eid}",
                                {"topic": new_topic, "content": new_content, "tags": new_tags, "source": meta.get("source", "user")},
                            )
                            if r:
                                st.success("Gespeichert")
                                st.cache_data.clear()

                if st.button("🗑 Löschen", key=f"del_{eid}"):
                    try:
                        import httpx as _httpx
                        resp = _httpx.delete(f"{API_URL}/api/v1/knowledge/delete/{coll_choice}/{eid}", timeout=10.0)
                        if resp.status_code == 200:
                            st.warning("Gelöscht")
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error(f"Fehler: {resp.status_code}")
                    except Exception as _e:
                        st.error(f"Fehler: {_e}")
    elif kb_list is None:
        st.info("Keine Daten verfügbar")

    st.divider()

    # Export / Import
    st.subheader("Export / Import")
    col_exp, col_imp = st.columns(2)

    with col_exp:
        # Export direkt als Download-Button — kein Zwischenspeichern im Browser-State
        try:
            import httpx as _httpx
            _resp = _httpx.get(f"{API_URL}/api/v1/knowledge/export", timeout=30.0)
            st.download_button(
                label="📥 Export herunterladen (JSON)",
                data=_resp.content,
                file_name="knowledge_export.json",
                mime="application/json",
            )
        except Exception as _e:
            st.error(f"Export nicht verfügbar: {_e}")

    with col_imp:
        uploaded = st.file_uploader("📤 JSON importieren", type=["json"], key="kb_import")
        overwrite = st.checkbox("Bestehende Einträge überschreiben", key="kb_overwrite")
        if uploaded and st.button("Import starten"):
            import json as _json
            try:
                data = _json.loads(uploaded.read())
                # Export-Format: {"ha_knowledge": [...], ...}
                if isinstance(data, dict) and any(k.startswith("ha_") for k in data):
                    total_imp = 0
                    for cname, centries in data.items():
                        if isinstance(centries, list) and centries:
                            r = post_json(
                                "/api/v1/knowledge/import",
                                {"entries": centries, "collection": cname, "overwrite": overwrite},
                            )
                            if r:
                                total_imp += r.get("imported", 0)
                    st.success(f"{total_imp} Einträge importiert")
                else:
                    st.error("Unbekanntes Format — erwartet wird eine Export-Datei dieses Systems")
            except Exception as e:
                st.error(f"Import fehlgeschlagen: {e}")
            st.cache_data.clear()


# ---------------------------------------------------------------------------
# Seite: Metriken
# ---------------------------------------------------------------------------

def page_metrics() -> None:
    st.header("📈 Prometheus-Metriken")

    metrics_text = fetch_text("/metrics")

    if metrics_text is None:
        st.error("Metriken-Endpunkt nicht erreichbar")
        return

    if "prometheus_client not installed" in metrics_text:
        st.warning("prometheus_client ist nicht installiert. Installation: `pip install prometheus-client`")
        return

    # Metriken nach Gruppen aufteilen
    lines = metrics_text.splitlines()
    ha_metrics = [l for l in lines if l.startswith("ha_") or l.startswith("# HELP ha_") or l.startswith("# TYPE ha_")]
    other_metrics = [l for l in lines if l not in ha_metrics and l.strip()]

    tab1, tab2 = st.tabs(["HA Agent Metriken", "System-Metriken"])

    with tab1:
        if ha_metrics:
            st.code("\n".join(ha_metrics), language="text")
        else:
            st.info("Noch keine HA-Metriken vorhanden (Agent noch nicht gelaufen?)")

    with tab2:
        if other_metrics:
            st.code("\n".join(other_metrics[:100]), language="text")
            if len(other_metrics) > 100:
                st.caption(f"… {len(other_metrics) - 100} weitere Zeilen ausgeblendet")
        else:
            st.info("Keine System-Metriken")

    st.divider()
    st.caption(f"Letzte Aktualisierung: {datetime.now().strftime('%H:%M:%S')}")
    st.caption(f"Metriken-Endpunkt: `{API_URL}/metrics`")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_PAGES = {
    "📊 Übersicht": page_overview,
    "🔍 Findings": page_findings,
    "🔧 Genehmigungen": page_approvals,
    "📜 Reparaturhistorie": page_repair_history,
    "🧠 Knowledge Base": page_knowledge,
    "📈 Metriken": page_metrics,
}

_PAGES[page]()

# Auto-Refresh
time.sleep(0.1)
st.empty()

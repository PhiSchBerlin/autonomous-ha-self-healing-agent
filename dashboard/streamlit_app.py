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

from datetime import datetime
from typing import Any

import streamlit as st

from dashboard.api_client import (
    API_URL,
    EXTERNAL_API_URL,
    REFRESH_SECONDS,
    fetch_sandbox_status,
    get_json,
    get_text,
    poll_repair_job,
    post_json,
    set_sandbox_status,
)

st.set_page_config(
    page_title="HA Self-Healing Agent",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Gecachte API-Hilfsfunktionen (Cache-Wrapper um api_client)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_json(path: str) -> dict[str, Any] | None:
    return get_json(path)  # type: ignore[return-value]


def fetch_text(path: str) -> str | None:
    return get_text(path)


def _poll_repair_job(job_id: str) -> dict[str, Any] | None:
    return poll_repair_job(job_id)


def _show_active_repair_job() -> None:
    """Zeigt den aktuellen Status eines laufenden Repair-Jobs (nicht-blockierend)."""
    job_id = st.session_state.get("active_repair_job_id")
    if not job_id:
        return
    job = _poll_repair_job(job_id)
    if job is None:
        st.warning("Repair-Job läuft — Status temporär nicht abrufbar")
        return
    total = job.get("total", 1)
    completed = job.get("completed", 0)
    status_txt = job.get("status", "running")
    st.progress(completed / max(total, 1), text=f"Repair-Fortschritt: {completed}/{total} Issues verarbeitet")
    if status_txt == "aborted":
        error_msg = job.get("error", "Unbekannter Abbruchgrund")
        st.error(f"⛔ Repair-Job abgebrochen: {error_msg}")
        st.session_state.pop("active_repair_job_id", None)
        st.cache_data.clear()
    elif status_txt == "done":
        successful = job.get("successful", 0)
        st.success(f"Reparatur abgeschlossen: {successful}/{total} erfolgreich")
        for r in job.get("results", []):
            if r.get("success"):
                st.info(f"✅ {r.get('issue_title', '')} — {r.get('repairs_proposed', 0)} Reparatur(en) vorgeschlagen")
            else:
                errs = "; ".join(r.get("errors", []) or [])
                st.warning(f"⚠️ {r.get('issue_title', '')} — {errs or 'Kein Patch generierbar'}")
        st.session_state.pop("active_repair_job_id", None)
        st.cache_data.clear()
    else:
        st.caption(f"Job läuft … wird alle {REFRESH_SECONDS}s aktualisiert")


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

    # Sandbox-Toggle
    st.subheader("Sandbox-Modus")
    if "sandbox_enabled" not in st.session_state:
        st.session_state["sandbox_enabled"] = fetch_sandbox_status()

    def _toggle_sandbox() -> None:
        new_val = not st.session_state["sandbox_enabled"]
        result = set_sandbox_status(new_val)
        st.session_state["sandbox_enabled"] = result

    sandbox_on = st.session_state["sandbox_enabled"]
    label = "🔒 Sandbox AN — nur simulieren" if sandbox_on else "⚡ Sandbox AUS — live schreiben"
    st.toggle(
        label,
        value=sandbox_on,
        key="_sandbox_toggle",
        on_change=_toggle_sandbox,
        help=(
            "Sandbox AN: Reparaturen werden nur simuliert, nie auf Disk geschrieben.\n"
            "Sandbox AUS: Validierte Reparaturen werden direkt in die HA-Konfiguration geschrieben."
        ),
    )
    if not sandbox_on:
        st.warning("Sandbox ist deaktiviert — Reparaturen werden live angewendet!")

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

    # Zusammenfassung
    summary = fetch_json("/api/v1/security/issues/summary")
    has_issues = isinstance(summary, dict) and summary.get("total", 0) > 0

    if has_issues:
        cols = st.columns(5)
        cols[0].metric("🔴 Critical", summary.get("critical", 0))
        cols[1].metric("🟠 High", summary.get("high", 0))
        cols[2].metric("🟡 Medium", summary.get("medium", 0))
        cols[3].metric("🟢 Low", summary.get("low", 0))
        cols[4].metric("⚪ Info", summary.get("info", 0))

    # Severity-Filter als Checkboxen
    st.markdown("**Schweregrad-Filter:**")
    fc = st.columns(5)
    show_critical = fc[0].checkbox("🔴 Critical", value=True, key="f_crit")
    show_high     = fc[1].checkbox("🟠 High",     value=True, key="f_high")
    show_medium   = fc[2].checkbox("🟡 Medium",   value=True, key="f_med")
    show_low      = fc[3].checkbox("🟢 Low",      value=False, key="f_low")
    show_info     = fc[4].checkbox("⚪ Info",      value=False, key="f_info")

    active_severities = {
        sev for sev, active in [
            ("critical", show_critical),
            ("high", show_high),
            ("medium", show_medium),
            ("low", show_low),
            ("info", show_info),
        ] if active
    }

    limit = st.slider("Maximale Anzahl laden", 50, 2000, 500, key="sec_limit")
    security = fetch_json(f"/api/v1/security/issues?limit={limit}")

    if not isinstance(security, list):
        st.warning("Security-Endpunkt nicht erreichbar — bitte Addon neu starten")
        return

    # Client-seitige Filterung nach aktiven Schweregraden
    filtered = [i for i in security if i.get("severity", "info").lower() in active_severities]

    if not filtered:
        st.info("Keine Issues für die gewählten Filter (oder noch kein Scan gelaufen)")
        return

    # Auswahlsteuerung — Schlüssel ist die Issue-ID (UUID), Fallback auf title+line
    if "selected_issue_ids" not in st.session_state:
        st.session_state["selected_issue_ids"] = set()

    def _issue_key(issue: dict) -> str:
        return issue.get("id") or (issue.get("title", "") + str(issue.get("line_number", "")))

    col_sel, col_info = st.columns([1, 3])
    with col_sel:
        if st.button("Alle auswählen", key="sel_all"):
            st.session_state["selected_issue_ids"] = {_issue_key(i) for i in filtered}
            st.rerun()
        if st.button("Auswahl aufheben", key="sel_none"):
            st.session_state["selected_issue_ids"] = set()
            st.rerun()
    with col_info:
        n_sel = len(st.session_state["selected_issue_ids"])
        st.caption(f"{len(filtered)} Issues angezeigt · {n_sel} ausgewählt zur Reparatur")

    _show_active_repair_job()

    if n_sel > 0:
        if st.button(f"🔧 {n_sel} ausgewählte Issues reparieren lassen", type="primary"):
            selected_keys = st.session_state["selected_issue_ids"]
            issues_to_repair = [i for i in filtered if _issue_key(i) in selected_keys]
            result = post_json("/api/v1/repair/issues", {
                "issues": issues_to_repair,
                "ha_config_path": "/config",
            }, timeout=30.0)
            if result and result.get("job_id"):
                st.session_state["active_repair_job_id"] = result["job_id"]
                st.session_state["selected_issue_ids"] = set()
                st.rerun()
            elif result:
                st.warning("Job gestartet, aber keine Job-ID erhalten")
            else:
                st.error("Repair-Endpunkt nicht erreichbar")

    # Issue-Liste mit Checkbox je Eintrag
    for issue in filtered:
        sev = issue.get("severity", "info")
        icon = _severity_color(sev)
        title = issue.get("title", "Unbekannt")
        ikey = _issue_key(issue)

        col_chk, col_exp = st.columns([0.05, 0.95])
        is_checked = ikey in st.session_state["selected_issue_ids"]
        new_val = col_chk.checkbox("Auswählen", value=is_checked, key=f"chk_{ikey}", label_visibility="collapsed")
        if new_val != is_checked:
            if new_val:
                st.session_state["selected_issue_ids"].add(ikey)
            else:
                st.session_state["selected_issue_ids"].discard(ikey)
            st.rerun()

        with col_exp:
            with st.expander(f"{icon} [{sev.upper()}] {title}"):
                st.write(f"**Beschreibung:** {issue.get('description', '-')}")
                st.write(f"**Datei:** `{issue.get('file_path', '-')}`")
                st.write(f"**Zeile:** {issue.get('line_number', '-')}")
                st.write(f"**Risiko-Score:** {issue.get('risk_score', '-')}")
                if issue.get("remediation"):
                    st.write(f"**Empfohlene Massnahme:** {issue['remediation']}")
                if issue.get("cve_ids"):
                    st.write(f"**CVEs:** {', '.join(issue['cve_ids'])}")

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

_STATUS_ICONS = {
    "applied": "✅",
    "proposed": "📋",
    "validated": "🔬",
    "approved": "👍",
    "rejected": "❌",
    "rolled_back": "↩️",
    "failed": "💥",
    "simulated": "🧪",
}

_RISK_ICONS = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}


def page_approvals() -> None:
    st.header("🔧 Genehmigungen")

    # ── Bereich 1: Security-Issues auswählen & Reparatur starten ────────────
    st.subheader("1 — Security-Issues zur Reparatur auswählen")

    summary = fetch_json("/api/v1/security/issues/summary")
    has_issues = isinstance(summary, dict) and summary.get("total", 0) > 0

    if has_issues:
        cols = st.columns(5)
        cols[0].metric("🔴 Critical", summary.get("critical", 0))
        cols[1].metric("🟠 High", summary.get("high", 0))
        cols[2].metric("🟡 Medium", summary.get("medium", 0))
        cols[3].metric("🟢 Low", summary.get("low", 0))
        cols[4].metric("⚪ Info", summary.get("info", 0))
    else:
        st.info("Noch kein Security-Scan gelaufen. Bitte auf der Findings-Seite einen Scan starten.")

    # Filterzeile
    fc = st.columns([1, 1, 1, 1, 1, 2])
    show_critical = fc[0].checkbox("🔴 Critical", value=True, key="ap_crit")
    show_high     = fc[1].checkbox("🟠 High",     value=True, key="ap_high")
    show_medium   = fc[2].checkbox("🟡 Medium",   value=True, key="ap_med")
    show_low      = fc[3].checkbox("🟢 Low",      value=False, key="ap_low")
    show_info     = fc[4].checkbox("⚪ Info",      value=False, key="ap_info")
    limit_ap = fc[5].slider("Max. laden", 50, 2000, 500, key="ap_limit")

    active_severities = {
        sev for sev, active in [
            ("critical", show_critical), ("high", show_high),
            ("medium", show_medium), ("low", show_low), ("info", show_info),
        ] if active
    }

    security = fetch_json(f"/api/v1/security/issues?limit={limit_ap}")
    if not isinstance(security, list):
        st.warning("Security-Endpunkt nicht erreichbar")
    else:
        filtered_sec = [i for i in security if i.get("severity", "info").lower() in active_severities]

        if not filtered_sec:
            st.info("Keine Issues für die gewählten Filter")
        else:
            if "ap_selected_ids" not in st.session_state:
                st.session_state["ap_selected_ids"] = set()

            def _ikey(issue: dict) -> str:
                return issue.get("id") or (issue.get("title", "") + str(issue.get("line_number", "")))

            sel_col, info_col = st.columns([1, 3])
            with sel_col:
                if st.button("Alle auswählen", key="ap_sel_all"):
                    st.session_state["ap_selected_ids"] = {_ikey(i) for i in filtered_sec}
                    st.rerun()
                if st.button("Auswahl aufheben", key="ap_sel_none"):
                    st.session_state["ap_selected_ids"] = set()
                    st.rerun()
            with info_col:
                n_ap_sel = len(st.session_state["ap_selected_ids"])
                st.caption(f"{len(filtered_sec)} Issues angezeigt · {n_ap_sel} ausgewählt")

            _show_active_repair_job()

            if n_ap_sel > 0:
                if st.button(f"🔧 {n_ap_sel} Issues reparieren lassen", type="primary", key="ap_repair_btn"):
                    selected_keys = st.session_state["ap_selected_ids"]
                    issues_to_repair = [i for i in filtered_sec if _ikey(i) in selected_keys]
                    result = post_json("/api/v1/repair/issues", {
                        "issues": issues_to_repair,
                        "ha_config_path": "/config",
                    }, timeout=30.0)
                    if result and result.get("job_id"):
                        st.session_state["active_repair_job_id"] = result["job_id"]
                        st.session_state["ap_selected_ids"] = set()
                        st.rerun()
                    elif result:
                        st.warning("Job gestartet, aber keine Job-ID erhalten")
                    else:
                        st.error("Repair-Endpunkt nicht erreichbar")

            for issue in filtered_sec:
                sev = issue.get("severity", "info")
                icon = _severity_color(sev)
                title = issue.get("title", "Unbekannt")
                ikey = _ikey(issue)

                chk_col, exp_col = st.columns([0.05, 0.95])
                is_checked = ikey in st.session_state.get("ap_selected_ids", set())
                new_val = chk_col.checkbox("Auswählen", value=is_checked, key=f"ap_chk_{ikey}", label_visibility="collapsed")
                if new_val != is_checked:
                    if new_val:
                        st.session_state["ap_selected_ids"].add(ikey)
                    else:
                        st.session_state["ap_selected_ids"].discard(ikey)
                    st.rerun()

                with exp_col:
                    with st.expander(f"{icon} [{sev.upper()}] {title}"):
                        st.write(f"**Datei:** `{issue.get('file_path', '-')}`  |  **Zeile:** {issue.get('line_number', '-')}  |  **Risiko:** {issue.get('risk_score', '-')}")
                        st.write(f"**Beschreibung:** {issue.get('description', '-')}")
                        if issue.get("remediation"):
                            st.write(f"**Empfohlene Massnahme:** {issue['remediation']}")
                        if issue.get("cve_ids"):
                            st.write(f"**CVEs:** {', '.join(issue['cve_ids'])}")

    st.divider()

    # ── Bereich 2: Ausstehende RepairActions genehmigen/ablehnen ────────────
    st.subheader("2 — Ausstehende Reparaturen genehmigen")

    pending = fetch_json("/api/v1/repair/pending")
    if not isinstance(pending, list):
        st.warning("Repair-Endpunkt nicht erreichbar")
        return

    if not pending:
        st.success("✅ Keine ausstehenden Reparatur-Genehmigungen")
        return

    col_count, col_discard = st.columns([3, 1])
    with col_count:
        st.caption(f"{len(pending)} ausstehende Reparatur(en)")
    with col_discard:
        if st.button("🗑️ Alle verwerfen", key="discard_all_pending", type="secondary"):
            import httpx as _httpx
            try:
                resp = _httpx.delete(f"{API_URL}/api/v1/repair/pending/all", timeout=10.0)
                if resp.status_code == 200:
                    st.success(f"{resp.json().get('discarded', 0)} Einträge verworfen")
                    st.cache_data.clear()
                    st.rerun()
            except Exception as exc:
                st.error(f"Fehler: {exc}")

    for action in pending:
        action_id = action.get("id", "")
        title = action.get("title", "Unbekannt")
        risk = action.get("risk_level", "unknown")
        sev = action.get("finding_severity", "")
        status_val = action.get("status", "proposed")
        proposed_count = action.get("repairs_proposed", 0)

        risk_icon = _RISK_ICONS.get(risk, "⚫")
        sev_icon = _severity_color(sev) if sev else ""
        status_icon = _STATUS_ICONS.get(status_val, "❓")

        with st.expander(
            f"{status_icon} {risk_icon} **{title}** — Status: `{status_val}` | {proposed_count} Reparatur(en) vorgeschlagen",
            expanded=True,
        ):
            col_l, col_r = st.columns(2)
            with col_l:
                st.write(f"**Erstellt:** {action.get('created_at', '-')[:19].replace('T', ' ')}")
                st.write(f"**Schweregrad:** {sev_icon} {sev.upper() if sev else '-'}")
                st.write(f"**Betroffene Datei:** `{action.get('finding_file', '-')}`")
                if action.get("finding_line"):
                    st.write(f"**Zeile:** {action['finding_line']}")
                confidence = action.get("confidence")
                if confidence is not None:
                    st.progress(confidence, text=f"Confidence: {confidence:.0%}")
                risk = action.get("risk_level")
                if risk:
                    risk_icon = _RISK_ICONS.get(risk, "⚫")
                    st.write(f"**Risiko:** {risk_icon} `{risk}`")
                if action.get("estimated_impact"):
                    st.write(f"**Erwartete Auswirkung:** {action['estimated_impact']}")
            with col_r:
                st.write(f"**Beschreibung:** {action.get('description', '-')}")
                if action.get("rationale"):
                    st.write(f"**Begründung des Agents:** {action['rationale']}")
                if action.get("remediation_hint"):
                    st.write(f"**Hinweis:** {action['remediation_hint']}")
                if action.get("cve_ids"):
                    st.write(f"**CVEs:** {', '.join(action['cve_ids'])}")
                errs = action.get("errors", [])
                if errs:
                    st.write(f"**Fehler:** {'; '.join(errs)}")

            alternatives = action.get("alternatives", [])
            if alternatives:
                st.divider()
                st.write("**💡 Verworfene Alternativen**")
                for alt in alternatives:
                    st.markdown(f"- {alt}")

            changes = action.get("changes", [])
            if changes:
                st.divider()
                st.write(f"**📝 Codeänderungen ({len(changes)} Datei(en))**")
                for ch in changes:
                    st.caption(f"`{ch.get('file_path', '')}` — {ch.get('change_type', 'modify')}")
                    if ch.get("diff"):
                        st.code(ch["diff"], language="diff")

            if action.get("audit_trail"):
                st.caption(f"Audit-Trail: {len(action['audit_trail'])} Einträge")

            st.markdown("---")
            approve_col, reject_col = st.columns(2)
            with approve_col:
                if st.button("✅ Genehmigen", key=f"ap_approve_{action_id}", type="primary"):
                    res = post_json(f"/api/v1/repair/{action_id}/approve", {"approved": True})
                    if res:
                        st.success("Genehmigt!")
                        st.cache_data.clear()
                        st.rerun()
            with reject_col:
                reject_reason = st.text_input("Ablehnungsgrund (optional)", key=f"ap_reason_{action_id}")
                if st.button("❌ Ablehnen", key=f"ap_reject_{action_id}"):
                    res = post_json(f"/api/v1/repair/{action_id}/approve", {"approved": False, "reason": reject_reason})
                    if res:
                        st.warning("Abgelehnt und gespeichert")
                        st.cache_data.clear()
                        st.rerun()


# ---------------------------------------------------------------------------
# Seite: Reparaturhistorie
# ---------------------------------------------------------------------------

def page_repair_history() -> None:
    st.header("📜 Reparaturhistorie")

    repair_data = fetch_json("/api/v1/repair/history")
    if repair_data is None:
        st.error("Backend nicht erreichbar")
        return

    actions = repair_data.get("commits", [])
    git_commits = repair_data.get("git_commits", [])
    backup_tags = repair_data.get("backup_tags", [])

    # ── Zusammenfassung ──────────────────────────────────────────────────────
    if actions:
        status_counts: dict[str, int] = {}
        for a in actions:
            s = a.get("status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1
        m_cols = st.columns(len(status_counts) or 1)
        for idx, (s, cnt) in enumerate(sorted(status_counts.items())):
            icon = _STATUS_ICONS.get(s, "❓")
            m_cols[idx].metric(f"{icon} {s.capitalize()}", cnt)
        st.divider()
    else:
        st.info("Noch keine abgeschlossenen Reparaturen vorhanden.")

    # ── Filter ───────────────────────────────────────────────────────────────
    all_statuses = sorted({a.get("status", "unknown") for a in actions})
    all_severities = sorted({a.get("finding_severity", "") for a in actions if a.get("finding_severity")})

    f_col1, f_col2 = st.columns(2)
    with f_col1:
        sel_status = st.multiselect("Status", all_statuses, default=all_statuses, key="hist_status")
    with f_col2:
        sel_sev = st.multiselect("Schweregrad des Findings", all_severities, default=all_severities, key="hist_sev") if all_severities else []

    filtered_actions = [
        a for a in actions
        if a.get("status") in sel_status
        and (not sel_sev or a.get("finding_severity", "") in sel_sev)
    ]

    st.caption(f"{len(filtered_actions)} Einträge angezeigt (gesamt: {len(actions)})")

    # ── Aktions-Liste ─────────────────────────────────────────────────────────
    for action in filtered_actions:
        status_val = action.get("status", "unknown")
        sev = action.get("finding_severity", "")
        title = action.get("title", "Unbekannt")
        status_icon = _STATUS_ICONS.get(status_val, "❓")
        sev_icon = _severity_color(sev) if sev else ""
        created = action.get("created_at", "-")[:19].replace("T", " ")
        updated = action.get("updated_at", "-")[:19].replace("T", " ")

        confidence = action.get("confidence")
        conf_label = f"{confidence:.0%}" if confidence is not None else "—"
        with st.expander(f"{status_icon} **{title}** — `{status_val}` | {sev_icon} {sev.upper() if sev else ''}  |  {created}  |  Confidence: {conf_label}"):
            c1, c2 = st.columns(2)
            with c1:
                st.write(f"**Status:** {status_icon} `{status_val}`")
                st.write(f"**Erstellt:** {created}")
                st.write(f"**Zuletzt geändert:** {updated}")
                st.write(f"**Betroffene Datei:** `{action.get('finding_file', '-')}`")
                if action.get("finding_line"):
                    st.write(f"**Zeile:** {action['finding_line']}")
                st.write(f"**Reparaturen vorgeschlagen:** {action.get('repairs_proposed', 0)}")
                st.write(f"**Reparaturen angewendet:** {action.get('repairs_applied', 0)}")
                if action.get("duration_seconds") is not None:
                    st.write(f"**Dauer:** {action['duration_seconds']:.1f}s")
                if confidence is not None:
                    st.progress(confidence, text=f"Confidence: {conf_label}")
                risk = action.get("risk_level")
                if risk:
                    risk_icon = _RISK_ICONS.get(risk, "⚫")
                    st.write(f"**Risiko:** {risk_icon} `{risk}`")
                if action.get("estimated_impact"):
                    st.write(f"**Erwartete Auswirkung:** {action['estimated_impact']}")
            with c2:
                st.write(f"**Beschreibung:** {action.get('description', '-')}")
                if action.get("rationale"):
                    st.write(f"**Begründung des Agents:** {action['rationale']}")
                if action.get("remediation_hint"):
                    st.write(f"**Empfohlene Massnahme:** {action['remediation_hint']}")
                if action.get("cve_ids"):
                    st.write(f"**CVEs:** {', '.join(action['cve_ids'])}")
                if action.get("approval_decision") is not None:
                    decision_txt = "✅ Genehmigt" if action["approval_decision"] else "❌ Abgelehnt"
                    st.write(f"**Entscheidung:** {decision_txt}")
                if action.get("approval_reason"):
                    st.write(f"**Begründung:** {action['approval_reason']}")
                if action.get("approved_at"):
                    st.write(f"**Entschieden am:** {action['approved_at'][:19].replace('T', ' ')}")

            # Alternativen
            alternatives = action.get("alternatives", [])
            if alternatives:
                st.divider()
                st.write("**💡 Vom Agent verworfene Alternativen**")
                for alt in alternatives:
                    st.markdown(f"- {alt}")

            # Sandbox-Validierungsergebnis
            val = action.get("validation_result") or {}
            sandbox_report = val.get("sandbox_report") or {}
            if sandbox_report:
                sb_passed = sandbox_report.get("passed", True)
                sb_issues = sandbox_report.get("issues", [])
                sb_icon = "✅" if sb_passed else "❌"
                st.divider()
                st.write(f"**{sb_icon} Sandbox-Validierung — {'bestanden' if sb_passed else f'{len(sb_issues)} Problem(e)'}**")
                if sb_issues:
                    for issue in sb_issues:
                        st.warning(issue)
                else:
                    st.success("YAML-Lint, Python-Syntax, Jinja2-Templates: alles in Ordnung")
                llm_sanity_verdict = sandbox_report.get("llm_verdict")
                if llm_sanity_verdict:
                    conf = sandbox_report.get("llm_confidence", 0.0)
                    st.write(f"**LLM-Sanity-Check:** {llm_sanity_verdict} (Confidence: {conf:.0%})")

            # Dry-Run-Hinweis
            app_result = action.get("application_result") or {}
            if app_result.get("dry_run"):
                st.info(f"Simulation (dry_run): Änderungen wurden NICHT geschrieben. Betroffene Dateien: {', '.join(app_result.get('would_apply', []))}")
            elif app_result.get("blocked"):
                st.warning(f"Blockiert durch Security-Policy: {app_result.get('message', '')}")

            # LLM-Patch-Review
            llm_review = val.get("llm_review") or {}
            if llm_review:
                st.divider()
                st.write("**🔍 LLM-Patch-Review**")
                approved = llm_review.get("approved")
                if approved is not None:
                    st.write(f"**Bewertet:** {'✅ Gut' if approved else '⚠️ Bedenken'}")
                if llm_review.get("concerns"):
                    st.write("**Bedenken:**")
                    for concern in llm_review["concerns"]:
                        st.markdown(f"- {concern}")
                if llm_review.get("suggestions"):
                    st.write("**Verbesserungsvorschläge:**")
                    for s in llm_review["suggestions"]:
                        st.markdown(f"- {s}")

            # Änderungs-Diffs
            changes = action.get("changes", [])
            if changes:
                st.divider()
                st.write(f"**📝 Codeänderungen ({len(changes)} Datei(en))**")
                for ch in changes:
                    st.caption(f"`{ch.get('file_path', '')}` — {ch.get('change_type', 'modify')}")
                    if ch.get("diff"):
                        st.code(ch["diff"], language="diff")

            errs = action.get("errors", [])
            if errs:
                st.error(f"Fehler: {'; '.join(errs)}")

            if action.get("audit_trail"):
                st.divider()
                st.write(f"**Audit-Trail ({len(action['audit_trail'])} Einträge)**")
                for entry in action["audit_trail"]:
                    st.code(str(entry))

            # Rollback-Option für angewendete Reparaturen
            if status_val == "applied" and action.get("git_commit_hash"):
                st.markdown("---")
                if st.button(f"↩️ Rollback zu diesem Commit", key=f"hist_rb_{action.get('id', '')}"):
                    res = post_json("/api/v1/repair/rollback", {
                        "ha_config_path": action.get("ha_config_path", "/config"),
                        "commit_hash": action["git_commit_hash"],
                    })
                    if res and res.get("success"):
                        st.success("Rollback erfolgreich")
                        st.cache_data.clear()
                    else:
                        st.error("Rollback fehlgeschlagen")

    # ── Git-History (sekundär) ────────────────────────────────────────────────
    if git_commits:
        st.divider()
        with st.expander(f"Git-Commit-History ({len(git_commits)} Commits)"):
            for commit in git_commits:
                st.markdown(f"**`{commit.get('short_hash', '')}`** — {commit.get('message', '')} *(by {commit.get('author', '')} am {commit.get('timestamp', '')[:10]})*")
                if commit.get("files_changed"):
                    st.caption(f"Geänderte Dateien: {', '.join(commit['files_changed'][:5])}")

    # ── Backup-Tags ───────────────────────────────────────────────────────────
    if backup_tags:
        st.divider()
        with st.expander(f"Backup-Tags ({len(backup_tags)})"):
            for tag in backup_tags:
                col_t, col_rb = st.columns([3, 1])
                col_t.write(f"🏷️ `{tag.get('name')}` — Commit `{tag.get('commit')}` am {tag.get('timestamp', '')[:10]}")
                if col_rb.button("↩️ Rollback", key=f"tag_rb_{tag.get('name')}"):
                    res = post_json("/api/v1/repair/rollback", {
                        "ha_config_path": "/config",
                        "tag_name": tag.get("name"),
                    })
                    if res and res.get("success"):
                        st.success(f"Rollback zu Tag `{tag.get('name')}` erfolgreich")
                    else:
                        st.error("Rollback fehlgeschlagen")


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
    if kb_list is None:
        st.warning("Knowledge-API nicht erreichbar")
    else:
        entries = kb_list.get("entries", [])
        total = kb_list.get("total", 0)
        st.caption(f"{total} Einträge in `{coll_choice}`")
        if total == 0:
            st.info("Keine Einträge in dieser Kollektion")

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

    st.divider()

    # Export / Import
    st.subheader("Export / Import")
    col_exp, col_imp = st.columns(2)

    with col_exp:
        # Export: Erst auf Knopfdruck laden, dann Download-Button zeigen
        # (Direktes Laden beim Render überläuft Streamlits localStorage-Quota)
        if st.button("📦 Export vorbereiten"):
            try:
                import httpx as _httpx
                _resp = _httpx.get(f"{API_URL}/api/v1/knowledge/export", timeout=60.0)
                _resp.raise_for_status()
                st.session_state["kb_export_bytes"] = _resp.content
                st.session_state["kb_export_ready"] = True
            except Exception as _e:
                st.error(f"Export nicht verfügbar: {_e}")
                st.session_state["kb_export_ready"] = False

        if st.session_state.get("kb_export_ready"):
            st.download_button(
                label="📥 Export herunterladen (JSON)",
                data=st.session_state["kb_export_bytes"],
                file_name="knowledge_export.json",
                mime="application/json",
                key="kb_dl_btn",
            )
            st.caption("Bereit zum Herunterladen")

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
    ha_metrics = [line for line in lines if line.startswith("ha_") or line.startswith("# HELP ha_") or line.startswith("# TYPE ha_")]
    other_metrics = [line for line in lines if line not in ha_metrics and line.strip()]

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

# Auto-Refresh: schneller wenn ein Repair-Job läuft, aber mit Mindest-Pause
if st.session_state.get("active_repair_job_id"):
    import time
    time.sleep(REFRESH_SECONDS)
    st.rerun()

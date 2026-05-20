"""Demo run — triggers a real news event and prints every step in detail.

Shows:
  - The triggering news
  - For each agent: its output + LLM call(s) + tool call(s) + auditor verdict
  - HITL pause + plan preview
  - Operator approval + execution
  - Final state (holdings, plan, journal, traces)

Run:  python demo_run.py
"""
from __future__ import annotations
import json
import os
import sys
import time

# Honor whatever .env says. Don't force mock.
# To force mock: USE_MOCK_LLM=1 python demo_run.py

from app import db, rag, config, graph, traces, llm
from app.seed import seed_all


CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def header(title, color=CYAN):
    print(f"\n{color}{BOLD}{'=' * 80}{RESET}")
    print(f"{color}{BOLD}{title}{RESET}")
    print(f"{color}{BOLD}{'=' * 80}{RESET}")


def section(title, color=YELLOW):
    print(f"\n{color}{BOLD}▶ {title}{RESET}")


def kv(label, value, indent=2):
    print(f"{' ' * indent}{DIM}{label}:{RESET} {value}")


def trim(s, max_len=300):
    s = str(s)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def print_agent_section(agent_name, journal_entry, audit_entry, traces_for_step):
    """Pretty-print one agent's full activity."""
    print(f"\n{MAGENTA}{BOLD}┌─ Agent: {agent_name}{RESET}")
    kv("duration", f"{journal_entry['duration_ms']} ms")

    output = journal_entry["output"]
    # Agent-specific highlights
    if agent_name == "news_triage":
        kv("decision", f"{BOLD}{output.get('decision')}{RESET}")
        kv("materiality_score", output.get("materiality_score"))
        kv("primary_dimension", output.get("primary_dimension"))
        kv("impacted_tickers", output.get("impacted_tickers"))
        kv("reasoning", trim(output.get("reasoning", ""), 200))
    elif agent_name == "fundamental_analyst":
        kv("recommended_action", f"{BOLD}{output.get('recommended_action')}{RESET}")
        kv("thesis_strength", output.get("thesis_strength"))
        kv("thesis_intact", output.get("thesis_intact"))
        kv("durability", output.get("business_quality", {}).get("durability"))
        kv("margin_trend", output.get("business_quality", {}).get("margin_trend"))
        kv("valuation", output.get("valuation"))
        kv("sources_count", len(output.get("sources_referenced", [])))
        kv("narrative", trim(output.get("reasoning_narrative", ""), 250))
    elif agent_name == "plan_builder":
        kv("status", f"{BOLD}{output.get('status')}{RESET}")
        kv("plan_id", output.get("plan_id"))
        kv("precedent_summary", trim(output.get("precedent_summary", ""), 150))
        kv("pre_check_results", output.get("pre_check_results"))
    elif agent_name == "risk_officer":
        kv("verdict", f"{BOLD}{output.get('verdict')}{RESET}")
        kv("recommended_routing", output.get("recommended_routing"))
        kv("policy_checks_count", len(output.get("policy_checks", [])))
        sim = output.get("simulate_order_result", {})
        kv("simulate_order_feasible", sim.get("feasible"))
        kv("violations", sim.get("policy_violations") or "none")
        kv("narrative", trim(output.get("reasoning_narrative", ""), 200))
    elif agent_name == "execution":
        kv("status", f"{BOLD}{output.get('status')}{RESET}")
        kv("ticker", output.get("ticker"))
        kv("quantity", output.get("quantity"))
        kv("fill_price", output.get("fill_price"))
        kv("notional_usd", output.get("notional_usd"))

    # Audit
    if audit_entry:
        a = json.loads(audit_entry["note_json"])
        sev = a.get("overall_severity", "info")
        sev_color = {"info": GREEN, "low": GREEN, "medium": YELLOW, "high": RED}.get(sev, "")
        print(f"\n  {sev_color}{BOLD}Auditor:{RESET} severity={sev_color}{sev}{RESET} "
              f"compliant={a.get('compliant')} findings={len(a.get('findings', []))}")
        for f in a.get("findings", []):
            print(f"    • {f['severity']}: {f['deviation']} ({f['policy_section']})")

    # Trace events
    llm_calls = [t for t in traces_for_step if t["event_type"] == "llm_call"]
    tool_calls = [t for t in traces_for_step if t["event_type"] == "tool_call"]

    if tool_calls:
        print(f"\n  {GREEN}Tool calls ({len(tool_calls)}){RESET}")
        for t in tool_calls:
            ed = t["event_data"]
            args_str = ", ".join(ed.get("args", []))
            status = ed.get("status", "?")
            status_color = GREEN if status == "ok" else RED
            print(f"    {status_color}●{RESET} {ed.get('tool')}({trim(args_str, 60)}) "
                  f"{DIM}[{t['duration_ms']}ms]{RESET}")

    if llm_calls:
        print(f"\n  {MAGENTA}LLM calls ({len(llm_calls)}){RESET}")
        for t in llm_calls:
            ed = t["event_data"]
            mode = ed.get("mode", "?")
            mode_color = YELLOW if mode == "mock" else GREEN
            purpose = ed.get("purpose", "?")
            tokens = ed.get("tokens") or {}
            tok_str = ""
            if tokens.get("total"):
                tok_str = f"tokens={tokens.get('total')} ({tokens.get('prompt')}+{tokens.get('completion')})"
            print(f"    {mode_color}●{RESET} {ed.get('model')} [{mode}] "
                  f"purpose={purpose} {tok_str} {DIM}[{t['duration_ms']}ms]{RESET}")
            print(f"      {DIM}system →{RESET} {trim(ed.get('system_preview', ''), 150)}")
            print(f"      {DIM}user   →{RESET} {trim(ed.get('user_preview', ''), 200)}")
            print(f"      {DIM}resp   →{RESET} {trim(ed.get('response_preview', ''), 200)}")

    print(f"{MAGENTA}└─{RESET}")


def main():
    header("HORIZON CAPITAL — DEMO RUN", CYAN)
    print(f"{DIM}LLM mode:{RESET} {'MOCK' if llm.is_mock() else 'LIVE'}")
    print(f"{DIM}LangSmith:{RESET} {'ENABLED' if traces.langsmith_enabled() else 'DISABLED'}")
    print(f"{DIM}FIRM_DB:{RESET} {config.FIRM_DB}")
    print(f"{DIM}VECTOR_DB:{RESET} {config.VECTOR_DB}")

    # Reset DBs for a clean run
    for p in (config.FIRM_DB, config.VECTOR_DB):
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    section("STEP 0 — Init DBs and seed RAG corpora")
    db.init_db()
    rag.init_db()
    traces.init_db()
    seed_all()
    for c in ("policy", "news", "filings", "past_plans"):
        kv(c, f"{rag.count(c)} chunks")

    # ---- TRIGGER ----
    header("STEP 1 — TRIGGER NEWS EVENT", CYAN)
    news = {
        "id": "demo_news_" + str(int(time.time())),
        "headline": "Microsoft CFO Amy Hood to step down next year, succession plan underway",
        "body": (
            "Microsoft Corp said on Friday that long-tenured CFO Amy Hood will retire next year. "
            "The company has begun a formal succession process. Hood, who has served as CFO since 2013, "
            "was widely credited with the disciplined messaging around the AI capex cycle that has "
            "weighed on operating margins. Shares were down 2% in after-hours trading."
        ),
        "tickers": ["MSFT"],
        "source": "Reuters",
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    print(f"  {BOLD}Headline:{RESET} {news['headline']}")
    print(f"  {DIM}Body:{RESET} {trim(news['body'], 200)}")
    print(f"  {DIM}Tickers:{RESET} {news['tickers']}")

    section("STEP 2 — RUN GRAPH (sync, runs until HITL pause or terminal)")
    t0 = time.time()
    result = graph.start_news_run(news)
    duration = time.time() - t0
    print(f"  {BOLD}Run ID:{RESET} {result.run_id}")
    print(f"  {BOLD}Status:{RESET} {result.status}")
    print(f"  {BOLD}Final:{RESET} {result.state.get('final_status')}")
    print(f"  {BOLD}Wall time:{RESET} {duration:.2f}s")

    # ---- DUMP TRACES ----
    header("STEP 3 — ALL AGENT ACTIVITY (every LLM call, every tool call)", CYAN)
    journal = db.list_journal_for_run(result.run_id)
    audits = db.audits_for_run(result.run_id)
    ab = {a["about_journal_id"]: a for a in audits}
    all_traces = traces.list_for_run(result.run_id)
    tba: dict[str, list] = {}
    for t in all_traces:
        tba.setdefault(t["agent"] or "_global", []).append(t)

    for j in journal:
        j = dict(j)
        j["output"] = json.loads(j["output_json"])
        agent_traces = [t for t in tba.get(j["agent"], [])
                        if t.get("journal_id") == j["journal_id"]
                        or t.get("journal_id") is None]
        auditor_traces = [t for t in tba.get("auditor", [])
                          if t.get("journal_id") == j["journal_id"]]
        print_agent_section(j["agent"], j, ab.get(j["journal_id"]),
                            agent_traces + auditor_traces)

    # ---- HITL ----
    if result.status == "awaiting_hitl":
        header("STEP 4 — HITL PAUSE", YELLOW)
        queue = db.list_hitl_pending()
        if not queue:
            print(f"  {RED}No HITL items found - bug?{RESET}")
            return
        item = queue[0]
        plan_row = db.get_plan(item["plan_id"])
        plan = json.loads(plan_row["plan_json"])

        print(f"  {BOLD}Plan {plan['id']} for {plan['ticker']} awaits operator decision.{RESET}\n")
        print(f"  {BOLD}Thesis narrative:{RESET}")
        print(f"  {trim(plan['thesis']['narrative'], 400)}\n")
        print(f"  {BOLD}Entry:{RESET} {plan['entry']['side']} "
              f"{plan['entry']['target_size_pct_nav']:.1%} NAV "
              f"@ limit {plan['entry']['entry_price_or_trigger']['value']:.2f}")
        print(f"  {BOLD}Horizon:{RESET} {plan['thesis']['expected_holding_horizon']}")
        print(f"  {BOLD}Monitoring:{RESET} {plan['monitoring']['interval']} "
              f"({len(plan['monitoring']['checks'])} checks)")
        for chk in plan["monitoring"]["checks"]:
            thr = (chk.get('threshold') or chk.get('threshold_pct')
                   or chk.get('threshold_days'))
            print(f"    • {chk['name']}: {chk['type']} thr={thr} "
                  f"on_breach={chk['on_breach']}")

        section("STEP 5 — OPERATOR APPROVES")
        result2 = graph.resume_after_hitl(result.run_id, item["plan_id"],
                                           "approve", operator="demo_operator",
                                           note="approved in demo")
        print(f"  Status: {result2.status}")
        print(f"  Final:  {result2.state.get('final_status')}")

        # Print execution step's traces
        section("STEP 6 — EXECUTION (post-HITL)")
        journal2 = db.list_journal_for_run(result.run_id)
        audits2 = db.audits_for_run(result.run_id)
        ab2 = {a["about_journal_id"]: a for a in audits2}
        all_traces2 = traces.list_for_run(result.run_id)
        tba2: dict[str, list] = {}
        for t in all_traces2:
            tba2.setdefault(t["agent"] or "_global", []).append(t)
        # Only print the new (execution) step
        existing_ids = {j["journal_id"] for j in journal}
        for j in journal2:
            if j["journal_id"] in existing_ids:
                continue
            j = dict(j)
            j["output"] = json.loads(j["output_json"])
            agent_traces = [t for t in tba2.get(j["agent"], [])
                            if t.get("journal_id") == j["journal_id"]
                            or t.get("journal_id") is None]
            auditor_traces = [t for t in tba2.get("auditor", [])
                              if t.get("journal_id") == j["journal_id"]]
            print_agent_section(j["agent"], j, ab2.get(j["journal_id"]),
                                agent_traces + auditor_traces)

    # ---- FINAL STATE ----
    header("STEP 7 — FINAL STATE", GREEN)
    holdings = db.list_holdings()
    print(f"  {BOLD}Holdings ({len(holdings)}):{RESET}")
    for h in holdings:
        print(f"    {h['ticker']:6s} qty={h['quantity']:>5d} "
              f"@ ${h['cost_basis']:.2f} sector={h['sector']}")

    plans = db.list_plans()
    print(f"\n  {BOLD}Plans ({len(plans)}):{RESET}")
    for p in plans:
        print(f"    {p['plan_id']} {p['ticker']:6s} status={p['status']}")

    # Totals
    print(f"\n  {BOLD}Tracing summary:{RESET}")
    all_traces_final = traces.list_for_run(result.run_id)
    llm_calls = [t for t in all_traces_final if t["event_type"] == "llm_call"]
    tool_calls = [t for t in all_traces_final if t["event_type"] == "tool_call"]
    print(f"    LLM calls:     {len(llm_calls)}")
    print(f"    Tool calls:    {len(tool_calls)}")
    print(f"    Total traces:  {len(all_traces_final)}")
    print(f"    Journal rows:  {len(db.list_journal_for_run(result.run_id))}")
    print(f"    Audit rows:    {len(db.audits_for_run(result.run_id))}")

    # If using live LLM, print actual token usage
    total_tokens = sum((t['event_data'].get('tokens') or {}).get('total', 0)
                        for t in llm_calls)
    if total_tokens:
        print(f"    Total tokens:  {total_tokens}")

    header("DEMO COMPLETE", GREEN)
    print(f"  View this run in UI:  http://127.0.0.1:8000/run/{result.run_id}")
    print(f"  Plan detail:          http://127.0.0.1:8000/plan/{plans[0]['plan_id'] if plans else 'NONE'}")


if __name__ == "__main__":
    main()

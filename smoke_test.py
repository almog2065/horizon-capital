"""Smoke test - runs the full news event flow end-to-end without FastAPI.

Run:  python smoke_test.py

Demonstrates:
  - DB + RAG init
  - Seeding all corpora
  - News Triage -> Fundamental -> Plan Builder -> Risk Officer
  - Auditor side-channel
  - HITL queue enqueue
  - HITL resume -> execution
  - Final state
"""
from __future__ import annotations
import json
import os
import sys
import time

# Force mock mode for smoke test
os.environ["USE_MOCK_LLM"] = "1"

from app import db, rag, config, graph, traces
from app.seed import seed_all


def hr(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main():
    print("Horizon Capital — smoke test")
    print(f"FIRM_DB:   {config.FIRM_DB}")
    print(f"VECTOR_DB: {config.VECTOR_DB}")
    print(f"DOSSIERS:  {config.DOSSIERS_DIR}")

    # Clean previous state for a fresh test
    if config.FIRM_DB.exists():
        config.FIRM_DB.unlink()
    if config.VECTOR_DB.exists():
        config.VECTOR_DB.unlink()

    hr("STEP 1: init + seed")
    db.init_db()
    rag.init_db()
    traces.init_db()
    seed_all()
    for c in ("policy", "news", "filings", "past_plans"):
        print(f"  {c}: {rag.count(c)} chunks")

    hr("STEP 2: RAG search smoke test")
    hits = rag.search("policy", "max position sizing", top_k=2)
    print(f"  policy search 'max position sizing' returned {len(hits)} hits")
    for h in hits:
        print(f"    score={h['score']:.3f} - {h['text'][:80]}...")

    hr("STEP 3: trigger news event (MSFT CFO transition - high materiality)")
    news = {
        "id": "smoke_news_1",
        "headline": "Microsoft CFO Amy Hood to step down next year, succession plan underway",
        "body": "Microsoft Corp said on Friday that long-tenured CFO Amy Hood will retire. The company has begun a formal succession process. Shares were down 2% in after-hours trading.",
        "tickers": ["MSFT"],
        "source": "Reuters",
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    result = graph.start_news_run(news)
    print(f"  Run ID: {result.run_id}")
    print(f"  Status: {result.status}")
    print(f"  Final:  {result.state.get('final_status')}")

    hr("STEP 4: journal review")
    journal = db.list_journal_for_run(result.run_id)
    print(f"  {len(journal)} journal entries:")
    for j in journal:
        out = json.loads(j["output_json"])
        summary = ""
        if j["agent"] == "news_triage":
            summary = f"decision={out.get('decision')} score={out.get('materiality_score', 0):.2f}"
        elif j["agent"] == "fundamental_analyst":
            summary = f"action={out.get('recommended_action')} strength={out.get('thesis_strength')}"
        elif j["agent"] == "plan_builder":
            summary = f"status={out.get('status')} plan_id={out.get('plan_id')}"
        elif j["agent"] == "risk_officer":
            summary = f"verdict={out.get('verdict')} routing={out.get('recommended_routing')}"
        print(f"    [{j['journal_id']}] {j['agent']}: {summary}")

    hr("STEP 5: audit findings")
    audits = db.audits_for_run(result.run_id)
    print(f"  {len(audits)} audit notes:")
    for a in audits:
        note = json.loads(a["note_json"])
        print(f"    journal_id={a['about_journal_id']} severity={a['severity']} "
              f"compliant={bool(a['compliant'])} findings={len(note.get('findings', []))}")

    hr("STEP 6: HITL queue")
    queue = db.list_hitl_pending()
    print(f"  Pending HITL items: {len(queue)}")
    if not queue:
        print("  No HITL pending - flow stopped before HITL. Inspecting state...")
        print(json.dumps(result.state, indent=2)[:800])
        return

    item = queue[0]
    plan_row = db.get_plan(item["plan_id"])
    plan = json.loads(plan_row["plan_json"])
    print(f"  Item {item['item_id']}: plan {plan['id']} for {plan['ticker']}")
    print(f"  Plan status: {plan['status']}")
    print(f"  Thesis: {plan['thesis']['narrative'][:200]}")

    hr("STEP 7: operator APPROVES")
    result2 = graph.resume_after_hitl(item["run_id"], item["plan_id"],
                                       "approve", operator="smoke_tester",
                                       note="approved in smoke test")
    print(f"  Status: {result2.status}")
    print(f"  Final:  {result2.state.get('final_status')}")
    print(f"  Fill:   {json.dumps(result2.state.get('fill', {}), indent=2)}")

    hr("STEP 8: final state")
    holdings = db.list_holdings()
    print(f"  Holdings: {len(holdings)}")
    for h in holdings:
        print(f"    {h['ticker']} qty={h['quantity']} cost=${h['cost_basis']:.2f} sector={h['sector']}")

    plans = db.list_plans()
    print(f"  Plans: {len(plans)}")
    for p in plans:
        print(f"    {p['plan_id']} {p['ticker']} {p['status']}")

    hr("STEP 9: trigger off-universe news (should be ignored)")
    news2 = {
        "id": "smoke_news_2",
        "headline": "GameStop board approves bitcoin treasury allocation",
        "body": "GameStop board approved bitcoin allocation...",
        "tickers": ["GME"],
        "source": "Reuters",
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    result3 = graph.start_news_run(news2)
    print(f"  Run: {result3.run_id} final: {result3.state.get('final_status')}")

    hr("STEP 10: trace inspection")
    # Pick the first run that produced traces
    runs = db.list_runs()
    for r in runs:
        ts = traces.list_for_run(r["run_id"])
        if ts:
            llm_calls = [t for t in ts if t["event_type"] == "llm_call"]
            tool_calls = [t for t in ts if t["event_type"] == "tool_call"]
            print(f"  Run {r['run_id']}: {len(ts)} traces ({len(llm_calls)} LLM, {len(tool_calls)} tool)")
            # Per-agent breakdown
            from collections import Counter
            by_agent = Counter(t["agent"] or "_global" for t in ts)
            for agent, count in by_agent.most_common():
                print(f"    {agent}: {count} events")
            # Show a sample tool call
            if tool_calls:
                tc = tool_calls[0]
                print(f"  Sample tool call: {tc['event_data'].get('tool')} ({tc['duration_ms']} ms)")
            # Show a sample llm call
            if llm_calls:
                lc = llm_calls[0]
                ed = lc['event_data']
                print(f"  Sample LLM call: mode={ed.get('mode')} model={ed.get('model')} purpose={ed.get('purpose')}")
            break

    hr("OK — smoke test passed")
    print("Summary:")
    print(f"  Runs:    {len(db.list_runs())}")
    print(f"  Plans:   {len(db.list_plans())}")
    print(f"  Holdings: {len(db.list_holdings())}")
    print(f"  Journal entries: {sum(1 for r in db.list_runs() for _ in db.list_journal_for_run(r['run_id']))}")
    print(f"  Audit notes:     {sum(1 for r in db.list_runs() for _ in db.audits_for_run(r['run_id']))}")
    print(f"  Trace events:    {sum(len(traces.list_for_run(r['run_id'])) for r in db.list_runs())}")


if __name__ == "__main__":
    main()

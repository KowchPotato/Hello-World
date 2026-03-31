"""
Decision Engine — LLM-powered portfolio rebalancing via Ollama + LangGraph.

Consumes yield data from scanners, current portfolio state, and risk metrics.
Outputs a structured JSON action plan for executors.
"""

import json
from typing import Any

import ollama
import structlog
from langgraph.graph import END, StateGraph

from agent.utils import get_db, utc_now_iso

log = structlog.get_logger("ruthless.decision_engine")


# ---------------------------------------------------------------------------
# State schema for LangGraph
# ---------------------------------------------------------------------------
class AgentState(dict):
    """Typed dict flowing through the LangGraph nodes."""
    pass


# ---------------------------------------------------------------------------
# Prompt Templates
# ---------------------------------------------------------------------------
REBALANCE_PROMPT = """You are RuthlessCompounder, an autonomous DeFi yield optimization agent.

CURRENT PORTFOLIO:
{portfolio_json}

AVAILABLE YIELDS:
{yields_json}

RISK METRICS:
{risk_json}

TX BUDGET:
- Used this year: {tx_used}
- Remaining: {tx_remaining}
- Projected annual: {tx_projected}

ALLOCATION TARGETS:
- Solana yield: {solana_pct}%
- EVM yield: {evm_pct}%
- Trading sleeve: {sleeve_pct}%

RULES:
1. Minimize transactions — batch where possible.
2. Only move capital if the new position's APY exceeds the current by ≥2% (net of fees).
3. Never exceed leverage limits from config.
4. If yields are similar, prefer staying put (reduce churn).
5. If risk metrics are elevated, bias toward stablecoins / de-leveraging.
6. Trading sleeve: only open positions with clear trend signals.

Output ONLY valid JSON with this schema:
{{
  "reasoning": "string — brief explanation",
  "actions": [
    {{
      "type": "deposit|withdraw|rebalance|harvest|open_perp|close_perp|emergency_exit|no_action",
      "chain": "solana|base|arbitrum",
      "protocol": "string",
      "pool": "string",
      "amount_usd": number,
      "params": {{}}
    }}
  ],
  "estimated_tx_count": number,
  "confidence": number  // 0-100
}}
"""


# ---------------------------------------------------------------------------
# LangGraph Nodes
# ---------------------------------------------------------------------------
def gather_context(state: AgentState) -> AgentState:
    """Assemble all data into the prompt context."""
    config = state["config"]
    alloc = config.get("allocation", {})

    state["prompt"] = REBALANCE_PROMPT.format(
        portfolio_json=json.dumps(state.get("portfolio", {}), indent=2),
        yields_json=json.dumps(state.get("yields", {}), indent=2),
        risk_json=json.dumps(state.get("risk_metrics", {}), indent=2),
        tx_used=state.get("tx_used", 0),
        tx_remaining=state.get("tx_remaining", 0),
        tx_projected=state.get("tx_projected", 0),
        solana_pct=alloc.get("solana_yield_pct", 45),
        evm_pct=alloc.get("evm_yield_pct", 40),
        sleeve_pct=alloc.get("trading_sleeve_pct", 15),
    )
    return state


def call_llm(state: AgentState) -> AgentState:
    """Send prompt to Ollama and parse JSON response."""
    config = state["config"]
    llm_cfg = config.get("llm", {})
    model = llm_cfg.get("model", "qwen2.5:72b-instruct-q4_K_M")
    system = llm_cfg.get("system_prompt", "You are a DeFi agent.")

    log.info("llm_calling", model=model)

    try:
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": state["prompt"]},
            ],
            options={
                "temperature": llm_cfg.get("temperature", 0.1),
                "num_predict": llm_cfg.get("max_tokens", 4096),
            },
        )
        raw_text = response["message"]["content"]
        state["llm_raw"] = raw_text
        log.info("llm_response_received", length=len(raw_text))

    except Exception as e:
        log.error("llm_call_failed", error=str(e))
        state["llm_raw"] = ""
        state["decision"] = {"reasoning": f"LLM error: {e}", "actions": [], "estimated_tx_count": 0, "confidence": 0}
        return state

    # Parse JSON from response (handle markdown code fences)
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        # Try extracting JSON between braces
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                decision = json.loads(text[start:end])
            except json.JSONDecodeError:
                log.error("llm_json_parse_failed", raw=text[:500])
                decision = {"reasoning": "Failed to parse LLM output", "actions": [], "estimated_tx_count": 0, "confidence": 0}
        else:
            decision = {"reasoning": "No JSON in LLM output", "actions": [], "estimated_tx_count": 0, "confidence": 0}

    state["decision"] = decision
    return state


def validate_decision(state: AgentState) -> AgentState:
    """Sanity-check the LLM's decision before execution."""
    decision = state.get("decision", {})
    config = state["config"]

    # Reject if confidence too low
    if decision.get("confidence", 0) < 30:
        log.warning("decision_low_confidence", confidence=decision.get("confidence"))
        decision["actions"] = []
        decision["reasoning"] += " [BLOCKED: low confidence]"

    # Cap leverage in any perp actions
    max_lev = config.get("trading_sleeve", {}).get("max_leverage", 5.0)
    for action in decision.get("actions", []):
        params = action.get("params", {})
        if params.get("leverage", 1) > max_lev:
            log.warning("decision_leverage_capped", requested=params["leverage"], max=max_lev)
            params["leverage"] = max_lev

    state["decision"] = decision
    return state


def persist_decision(state: AgentState) -> AgentState:
    """Save the LLM prompt + decision to the database."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO llm_decisions (prompt, response, actions) VALUES (?, ?, ?)",
            (
                state.get("prompt", ""),
                state.get("llm_raw", ""),
                json.dumps(state.get("decision", {}).get("actions", [])),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("persist_decision_failed", error=str(e))
    return state


# ---------------------------------------------------------------------------
# Build the LangGraph
# ---------------------------------------------------------------------------
def build_decision_graph() -> StateGraph:
    """Construct and compile the decision-making graph."""
    graph = StateGraph(AgentState)

    graph.add_node("gather", gather_context)
    graph.add_node("llm", call_llm)
    graph.add_node("validate", validate_decision)
    graph.add_node("persist", persist_decision)

    graph.set_entry_point("gather")
    graph.add_edge("gather", "llm")
    graph.add_edge("llm", "validate")
    graph.add_edge("validate", "persist")
    graph.add_edge("persist", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def make_decision(
    config: dict[str, Any],
    portfolio: dict,
    yields: dict,
    risk_metrics: dict,
    tx_budget_status: dict,
) -> dict:
    """
    Run the full decision pipeline and return the action plan.

    Returns:
        dict with keys: reasoning, actions, estimated_tx_count, confidence
    """
    graph = build_decision_graph()

    initial_state: AgentState = AgentState(
        config=config,
        portfolio=portfolio,
        yields=yields,
        risk_metrics=risk_metrics,
        tx_used=tx_budget_status.get("used_this_year", 0),
        tx_remaining=tx_budget_status.get("remaining", 0),
        tx_projected=tx_budget_status.get("projected_annual", 0),
    )

    result = await asyncio.to_thread(graph.invoke, initial_state)
    return result.get("decision", {})


# We need asyncio for the async wrapper
import asyncio  # noqa: E402

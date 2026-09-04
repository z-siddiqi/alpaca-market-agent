# ruff: noqa: E501

import argparse
import asyncio
import html
import json
import os
import subprocess
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud.firestore_v1 import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]

# `decisions` is heterogeneous: real records written by DecisionStore.put(), plus
# lean markers for scheduled ticks that never produced one. Markers carry only
# tickId/tradingDate/evaluatedAt/status/source/createdAt and are NOT valid
# DecisionRecords — market_agent.storage.DecisionStore.get() skips them too.
MISSING_STATUS = "missing"

# The scheduled tick grid, mirroring market_agent.tick.tick_id()'s %Y-%m-%d-%H%M
# output and its 5-minute flooring. Duplicated rather than imported: the web
# image installs only google-cloud-firestore (see deploy/web/Dockerfile).
FIRST_TICK = time(9, 40)
LAST_TICK = time(15, 55)
TICK_INTERVAL_MINUTES = 5


def text(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def percent(value: Any) -> str:
    # Stored as a fraction (tick.build_account_state divides by starting equity),
    # so scale here rather than just appending a sign.
    try:
        return f"{float(value):+.2%}"
    except (TypeError, ValueError):
        return "—"


def signed(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return '<span class="muted">—</span>'
    tone = "gain" if amount > 0 else "loss" if amount < 0 else "flat"
    return f'<span class="{tone}">{amount:+,.2f}</span>'


def et_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(ET).strftime("%H:%M:%S")
    except ValueError:
        return text(value)


def list_items(values: list[Any]) -> str:
    if not values:
        return '<span class="muted">None</span>'
    return "<ul>" + "".join(f"<li>{text(value)}</li>" for value in values) + "</ul>"


def markdown(value: str) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{' '.join(paragraph)}</p>")
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            output.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
            bullets.clear()

    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_bullets()
        elif line.startswith("## "):
            flush_paragraph()
            flush_bullets()
            output.append(f"<h3>{text(line[3:])}</h3>")
        elif line.startswith("- "):
            flush_paragraph()
            bullets.append(text(line[2:]))
        else:
            flush_bullets()
            paragraph.append(text(line))
    flush_paragraph()
    flush_bullets()
    return "".join(output)


def pretty(value: Any, limit: int | None = None) -> str:
    encoder = json.JSONEncoder(indent=2, default=str, ensure_ascii=False, sort_keys=True)
    if limit is None:
        return html.escape(encoder.encode(value))
    # Stop encoding once past the limit; tool results reach megabytes and all but
    # the first `limit` characters are discarded anyway.
    chunks: list[str] = []
    size = 0
    for chunk in encoder.iterencode(value):
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            truncated = "".join(chunks)[:limit]
            return html.escape(
                truncated + "\n… truncated in HTML; full value remains in Firestore"
            )
    return html.escape("".join(chunks))


def render_tool_calls(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return '<p class="muted">No tools called.</p>'
    rendered: list[str] = []
    for call in calls:
        blocked = " · blocked" if call.get("blocked") else ""
        rendered.append(
            f"""
            <details class="tool">
              <summary>{text(call.get("name"))}{blocked}</summary>
              <h5>Arguments</h5><pre>{pretty(call.get("arguments"), 5_000)}</pre>
              <h5>Result</h5><pre>{pretty(call.get("result"), 20_000)}</pre>
            </details>
            """
        )
    return "".join(rendered)


def is_missing(record: dict[str, Any]) -> bool:
    return record.get("status") == MISSING_STATUS


def render_summary(
    *,
    when: str,
    action: str,
    action_class: str,
    confidence: str,
    auction: str,
    pnl: str,
    headline: str,
    tools: str,
) -> str:
    # Seven cells, matching the .tick>summary grid template in the stylesheet.
    return f"""      <summary>
        <time>{when}</time>
        <span class="{f"action {action_class}".strip()}">{action}</span>
        <span>{confidence}</span>
        <span>{auction}</span>
        <span class="pnl">{pnl}</span>
        <span class="headline">{headline}</span>
        <span class="tools">{tools}</span>
      </summary>"""


def render_missing_tick(record: dict[str, Any]) -> str:
    summary = render_summary(
        when=et_time(record.get("evaluatedAt")),
        action="—",
        action_class="",
        confidence="—",
        auction="—",
        pnl="—",
        headline="No recorded evaluation",
        tools="0 tools",
    )
    return f"""
    <details class="tick placeholder">
{summary}
      <div class="tick-body">
        <section class="wide">
          <h4>No recorded evaluation</h4>
          <p class="muted">No durable agent decision was recorded for this scheduled tick. Market,
          account, model, and execution details are unavailable.</p>
        </section>
      </div>
    </details>
    """


def observe_positions(
    decisions: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Derive trades by diffing consecutive position snapshots.

    Takes recorded decisions only; a marker tick carries no snapshot, and reading
    its absence as an empty position list would emit a phantom SELL at every gap.
    """
    events: dict[str, list[str]] = {}
    trades: dict[str, dict[str, Any]] = {}
    held: set[str] = set()

    for record in decisions:
        tick_id = str(record.get("tickId") or "")
        context = record.get("context") or {}
        evaluated_at = context.get("evaluatedAt")
        current = {
            str(position.get("symbol")): position
            for position in context.get("positions") or []
            if position.get("symbol")
        }

        for symbol in current.keys() - held:
            position = current[symbol]
            events.setdefault(tick_id, []).append(f"BUY {symbol}")
            trades[symbol] = {
                "symbol": symbol,
                "firstSeen": evaluated_at,
                "lastSeen": evaluated_at,
                "flatBy": None,
                "quantity": position.get("quantity"),
                "averageEntryPrice": position.get("averageEntryPrice"),
                "minimumUnrealizedPnl": position.get("unrealizedPnl"),
                "maximumUnrealizedPnl": position.get("unrealizedPnl"),
            }

        for symbol, position in current.items():
            # Every held symbol was inserted above, on this tick or an earlier one.
            trade = trades[symbol]
            trade["lastSeen"] = evaluated_at
            unrealized = position.get("unrealizedPnl")
            if unrealized is not None:
                low = trade["minimumUnrealizedPnl"]
                high = trade["maximumUnrealizedPnl"]
                trade["minimumUnrealizedPnl"] = unrealized if low is None else min(low, unrealized)
                trade["maximumUnrealizedPnl"] = unrealized if high is None else max(high, unrealized)

        for symbol in held - current.keys():
            events.setdefault(tick_id, []).append(f"SELL {symbol}")
            trades[symbol]["flatBy"] = evaluated_at

        held = set(current)

    return events, list(trades.values())


def render_trades(trades: list[dict[str, Any]]) -> str:
    if not trades:
        return '<p class="muted">No trades.</p>'
    rows = "".join(
        f"""
        <tr>
          <td>{text(trade.get("symbol"))}</td>
          <td>{et_time(trade.get("firstSeen"))}</td>
          <td>{text(trade.get("quantity"))}</td>
          <td>{money(trade.get("averageEntryPrice"))}</td>
          <td>{money(trade.get("minimumUnrealizedPnl"))} / {money(trade.get("maximumUnrealizedPnl"))}</td>
          <td>{et_time(trade.get("flatBy")) if trade.get("flatBy") else "Still open"}</td>
        </tr>
        """
        for trade in trades
    )
    return f"""
    <table class="trades-table">
      <thead><tr><th>Contract</th><th>First seen</th><th>Quantity</th><th>Average entry</th><th>Unrealized low / high</th><th>Flat by</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """


def render_broker_record(fills: dict[str, Any] | None) -> str:
    rows = (fills or {}).get("fills") or []
    if not rows:
        return '<p class="muted">No broker fills on this session.</p>'
    body = "".join(
        f"""
        <tr>
          <td>{et_time(fill.get("filledAt"))}</td>
          <td>{text(fill.get("side"))}</td>
          <td>{text(fill.get("symbol"))}</td>
          <td>{text(fill.get("quantity"))}</td>
          <td>{money(fill.get("price"))}</td>
          <td>{signed(fill.get("cashDelta"))}</td>
        </tr>
        """
        for fill in rows
    )
    recorded = fills.get("recordedDailyPnl")
    tie = (
        '<span class="gain">matches</span>'
        if fills.get("matchesStored")
        else '<span class="loss">does not match</span>'
        if recorded is not None
        else '<span class="muted">nothing stored to compare</span>'
    )
    return f"""
    <table class="trades-table">
      <thead><tr><th>Filled</th><th>Side</th><th>Contract</th><th>Quantity</th><th>Price</th><th>Cash</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
    <dl class="tieout">
      <dt>From fills</dt><dd>{signed(fills.get("realizedPnl"))}</dd>
      <dt>Fees on {fills.get("contractLegs", 0)} contracts</dt><dd>{money(fills.get("estimatedFees"))}</dd>
      <dt>Net</dt><dd>{signed(fills.get("netPnl"))}</dd>
      <dt>Stored session P&amp;L</dt><dd>{signed(recorded)} · {tie}</dd>
    </dl>
    <p class="muted note">Fills come straight from Alpaca. Where a tick has no stored
    evaluation, this is what the broker says happened.</p>
    """


def render_tick(record: dict[str, Any], position_events: list[str]) -> str:
    if is_missing(record):
        return render_missing_tick(record)
    context = record.get("context") or {}
    decision = record.get("decision") or {}
    account = context.get("account") or {}
    market = context.get("market") or {}
    action = str(decision.get("action") or "unknown")
    confidence = float(decision.get("confidence") or 0)
    reasons = decision.get("holdReasons") or []
    headline = (
        position_events[0]
        if position_events
        else reasons[0]
        if reasons
        else decision.get("thesis") or "No summary"
    )
    calls = record.get("toolCalls") or []
    summary = render_summary(
        when=et_time(decision.get("evaluatedAt") or context.get("evaluatedAt")),
        action=text(position_events[0].split(" ", 1)[0] if position_events else action),
        action_class="trade" if action != "hold" or position_events else "hold",
        confidence=f"{confidence:.0%}",
        auction=text(decision.get("auctionState")),
        pnl=money(account.get("dailyEquityPnl")),
        headline=text(headline),
        tools=f"{len(calls)} tools",
    )

    return f"""
    <details class="tick">
{summary}
      <div class="tick-body">
        <section>
          <h4>Decision</h4>
          <p>{text(decision.get("thesis"))}</p>
          {f"<h5>Trade</h5>{list_items(position_events)}" if position_events else ""}
          <dl>
            <dt>Action</dt><dd>{text(action)}</dd>
            <dt>Confidence</dt><dd>{confidence:.0%}</dd>
            <dt>Auction</dt><dd>{text(decision.get("auctionState"))}</dd>
            <dt>Contract</dt><dd>{text(decision.get("optionSymbol"))}</dd>
            <dt>SPY entry / invalidation / target</dt>
            <dd>{text(decision.get("entryPrice"))} / {text(decision.get("invalidationPrice"))} / {text(decision.get("targetPrice"))}</dd>
          </dl>
          <h5>Hold reasons</h5>{list_items(reasons)}
          <h5>Evidence</h5>{list_items(decision.get("evidence") or [])}
          <h5>Policy checks</h5>{list_items(decision.get("policyChecks") or [])}
        </section>
        <section>
          <h4>Snapshot</h4>
          <dl>
            <dt>Equity</dt><dd>{money(account.get("equity"))}</dd>
            <dt>Daily P&amp;L</dt><dd>{money(account.get("dailyEquityPnl"))} ({percent(account.get("dailyEquityPnlPercent"))})</dd>
            <dt>Buying power</dt><dd>{money(account.get("optionsBuyingPower"))}</dd>
            <dt>SPY</dt><dd>{money(market.get("latestPrice"))}</dd>
            <dt>Entry window</dt><dd>{text((context.get("entryWindow") or {}).get("state"))}</dd>
            <dt>Positions / orders</dt><dd>{len(context.get("positions") or [])} / {len(context.get("workingOrders") or [])}</dd>
          </dl>
          <h5>Entry blockers</h5>{list_items(context.get("entryBlockers") or [])}
          <h5>Exit reasons</h5>{list_items(context.get("exitReasons") or [])}
        </section>
        <section class="wide">
          <h4>MCP activity</h4>
          {render_tool_calls(calls)}
        </section>
        <details class="raw wide">
          <summary>Raw model decision</summary><pre>{pretty(decision)}</pre>
        </details>
      </div>
    </details>
    """


def render_page(
    day: date,
    decisions: list[dict[str, Any]],
    narrative: dict[str, Any] | None,
    fills: dict[str, Any] | None,
) -> str:
    decisions = sorted(decisions, key=lambda row: str(row.get("tickId")))
    recorded = [record for record in decisions if not is_missing(record)]
    accounts = [(record.get("context") or {}).get("account") or {} for record in recorded]
    first_account = accounts[0] if accounts else {}
    last_account = accounts[-1] if accounts else {}
    position_events, trades = observe_positions(recorded)
    calls = sum(len(row.get("toolCalls") or []) for row in decisions)
    narrative_html = (
        markdown(str(narrative.get("markdown") or ""))
        if narrative
        else '<p class="muted">No narrative stored for this session.</p>'
    )
    levels = pretty((narrative or {}).get("levels"))
    ticks = "".join(
        render_tick(record, position_events.get(str(record.get("tickId") or ""), []))
        for record in decisions
    )
    trades_html = render_trades(trades)
    broker_html = render_broker_record(fills)
    generated_at = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Augur audit · {day.isoformat()}</title>
  <style>
    :root {{ color-scheme:light; --background:#f7f7f4; --text:#1d1e1b; --muted:#666a62; --line:#d7d9d2; --accent:#256342; --panel:#efefeb; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--background); color:var(--text); font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    a {{ color:inherit; }}
    .page {{ width:min(960px,calc(100% - 40px)); margin:0 auto; }}
    .site-header {{ display:flex; align-items:center; justify-content:space-between; min-height:60px; border-bottom:1px solid var(--line); }}
    .site-header a {{ text-decoration:none; }}
    .brand {{ font-weight:650; }}
    .nav-link {{ color:var(--muted); font-size:14px; }}
    .nav-link:hover {{ color:var(--text); }}
    .audit-heading {{ display:flex; justify-content:space-between; align-items:end; gap:32px; padding:64px 0 28px; }}
    h1,h2,h3,h4,h5,p {{ margin-top:0; }}
    h1 {{ margin:0 0 8px; font-size:clamp(34px,5vw,48px); font-weight:650; letter-spacing:-.035em; line-height:1.05; }}
    h2 {{ margin:48px 0 16px; font-size:25px; font-weight:650; letter-spacing:-.02em; }}
    h3 {{ margin-top:24px; color:var(--accent); font-size:15px; }}
    h4 {{ font-size:13px; text-transform:uppercase; letter-spacing:.08em; }}
    h5 {{ margin:20px 0 6px; color:var(--muted); }}
    .muted {{ color:var(--muted); }}
    .generated {{ flex:0 0 auto; color:var(--muted); font-size:13px; text-align:right; }}
    .stats {{ display:grid; grid-template-columns:repeat(5,1fr); border-top:1px solid var(--line); margin:0 0 56px; }}
    .stat {{ padding:18px 16px 18px 0; border-bottom:1px solid var(--line); }}
    .stat:not(:first-child) {{ padding-left:16px; border-left:1px solid var(--line); }}
    .stat b {{ display:block; font:600 18px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace; }}
    .stat span {{ color:var(--muted); font-size:12px; }}
    .narrative {{ display:grid; grid-template-columns:minmax(0,2fr) minmax(240px,1fr); gap:40px; padding:24px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); }}
    pre {{ overflow:auto; padding:14px; background:var(--panel); border:0; color:#454941; font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:12px 10px; border-top:1px solid var(--line); text-align:left; }}
    tbody tr:last-child td {{ border-bottom:1px solid var(--line); }}
    th {{ color:var(--muted); font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }}
    td {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
    .note {{ margin-top:8px; font-size:11px; }}
    .gain {{ color:var(--accent); }}
    .loss {{ color:#a4423a; }}
    .flat {{ color:var(--muted); }}
    .tieout {{ display:grid; grid-template-columns:260px 1fr; margin:20px 0 0; }}
    .tieout dt,.tieout dd {{ padding:8px 0; border-bottom:1px solid var(--line); }}
    .tieout dt {{ color:var(--muted); }}
    .tieout dd {{ margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
    ul {{ margin:6px 0; padding-left:20px; }}
    .tick {{ border-top:1px solid var(--line); }}
    .tick:last-child {{ border-bottom:1px solid var(--line); }}
    .tick>summary {{ display:grid; grid-template-columns:76px 96px 48px 120px 90px minmax(240px,1fr) 70px; gap:12px; padding:12px 4px; cursor:pointer; align-items:center; list-style:none; }}
    .tick>summary::-webkit-details-marker {{ display:none; }}
    .tick>summary:hover .headline,.tick>summary:hover time {{ color:var(--accent); }}
    .tick[open]>summary {{ background:var(--panel); }}
    .tick time,.action,.pnl,.tools {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
    .action {{ color:var(--muted); text-transform:uppercase; }}
    .action.trade {{ color:var(--accent); font-weight:600; }}
    .pnl,.tools {{ text-align:right; }}
    .headline {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:var(--muted); }}
    .placeholder>summary {{ opacity:.58; }}
    .tick-body {{ display:grid; grid-template-columns:1fr 1fr; gap:36px; padding:28px 20px; background:var(--panel); border-top:1px solid var(--line); }}
    .wide {{ grid-column:1/-1; }}
    dl {{ display:grid; grid-template-columns:190px 1fr; margin:0; }}
    dt,dd {{ padding:6px 0; border-bottom:1px solid var(--line); }}
    dt {{ color:var(--muted); }}
    dd {{ margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
    details.tool {{ margin:8px 0; padding:10px 0; border-top:1px solid var(--line); }}
    details.tool:last-child {{ border-bottom:1px solid var(--line); }}
    details.tool>summary,.raw>summary {{ cursor:pointer; color:var(--accent); }}
    summary:focus-visible,a:focus-visible {{ outline:2px solid var(--accent); outline-offset:3px; }}
    footer {{ display:flex; justify-content:space-between; gap:20px; margin-top:56px; padding:28px 0 44px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; }}
    @media(max-width:800px) {{
      .page {{ width:min(100% - 28px,960px); }}
      .audit-heading {{ display:block; padding-top:48px; }}
      .generated {{ margin-top:20px; text-align:left; }}
      .stats {{ grid-template-columns:1fr 1fr; }}
      .stat:not(:first-child) {{ padding-left:0; border-left:0; }}
      .stat:nth-child(even) {{ padding-left:16px; border-left:1px solid var(--line); }}
      .narrative,.tick-body {{ grid-template-columns:1fr; }}
      .wide {{ grid-column:auto; }}
      .tick>summary {{ grid-template-columns:64px 76px 44px 1fr; }}
      .tick>summary .pnl,.tick>summary .headline,.tick>summary .tools {{ display:none; }}
      dl {{ grid-template-columns:1fr; }}
      dd {{ padding-top:0; }}
      .trades-table {{ display:block; overflow-x:auto; white-space:nowrap; }}
      footer {{ display:block; }}
    }}
  </style>
</head>
<body><div class="page">
  <header class="site-header">
    <a class="brand" href="index.html">Augur Market Agent</a>
    <a class="nav-link" href="index.html#audits">Daily audits</a>
  </header>
  <main>
  <section class="audit-heading"><div><h1>{day.strftime("%d %B %Y")}</h1><div class="muted">SPY options · daily decision audit</div></div><div class="generated">Generated {generated_at}</div></section>
  <div class="stats">
    <div class="stat"><b>{len(decisions)}</b><span>Ticks</span></div>
    <div class="stat"><b>{len(trades)}</b><span>Trades</span></div>
    <div class="stat"><b>{calls}</b><span>MCP calls</span></div>
    <div class="stat"><b>{money(first_account.get("sessionStartingEquity"))}</b><span>Starting equity</span></div>
    <div class="stat"><b>{money(last_account.get("dailyEquityPnl"))}</b><span>Closing P&amp;L</span></div>
  </div>
  <h2>Daily narrative</h2>
  <section class="narrative"><div>{narrative_html}</div><div><h4>Selected levels</h4><pre>{levels}</pre></div></section>
  <h2>Trades</h2>
  <section>{trades_html}</section>
  <h2>Broker record</h2>
  <section>{broker_html}</section>
  <h2>Tick timeline</h2>
  <section>{ticks or '<p class="muted">No completed ticks stored for this session.</p>'}</section>
  <footer><span>Scheduled ticks with no stored evaluation are shown as placeholder rows.</span><span>Paper trading only · Not investment advice</span></footer>
  </main>
</div></body></html>"""


def is_scheduled_tick(tick_id: str, day: date) -> bool:
    prefix = f"{day.isoformat()}-"
    if not tick_id.startswith(prefix):
        return False
    try:
        scheduled = datetime.strptime(tick_id.removeprefix(prefix), "%H%M").time()
    except ValueError:
        return False
    minutes = scheduled.hour * 60 + scheduled.minute
    first = FIRST_TICK.hour * 60 + FIRST_TICK.minute
    last = LAST_TICK.hour * 60 + LAST_TICK.minute
    return first <= minutes <= last and minutes % TICK_INTERVAL_MINUTES == 0


def starting_equity(decisions: dict[date, list[dict[str, Any]]]) -> float:
    for day in sorted(decisions):
        for record in sorted(decisions[day], key=lambda row: str(row.get("tickId"))):
            if is_missing(record):
                continue
            account = (record.get("context") or {}).get("account") or {}
            if account.get("sessionStartingEquity") is not None:
                return float(account["sessionStartingEquity"])
    return 0.0


def equity_curve(fills: dict[date, dict[str, Any]], opening: float) -> list[tuple[date, float]]:
    equity = opening
    curve: list[tuple[date, float]] = []
    for day in sorted(fills):
        equity += float(fills[day].get("netPnl") or 0)
        curve.append((day, equity))
    return curve


def render_equity_curve(curve: list[tuple[date, float]], opening: float) -> str:
    values = [opening, *(equity for _, equity in curve)]
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    width, height, pad = 960.0, 132.0, 14.0
    step = width / (len(values) - 1)

    def y_at(value: float) -> float:
        return height - pad - (value - low) / span * (height - 2 * pad)

    points = " ".join(f"{i * step:.1f},{y_at(v):.1f}" for i, v in enumerate(values))
    return f"""
        <svg class="equity" viewBox="0 0 {width:.0f} {height:.0f}" role="img"
             aria-label="Account equity across {len(curve)} sessions">
          <line class="equity-base" x1="0" y1="{y_at(opening):.1f}"
                x2="{width:.0f}" y2="{y_at(opening):.1f}"/>
          <polyline class="equity-line" points="{points}"/>
        </svg>
    """


def render_performance(fills: dict[date, dict[str, Any]], opening: float) -> str:
    curve = equity_curve(fills, opening)
    if not curve or not opening:
        return '<p class="empty">No settled sessions yet.</p>'
    closing = curve[-1][1]
    total = closing - opening
    traded = {day: float(row.get("netPnl") or 0) for day, row in fills.items() if row.get("fills")}
    best = max(traded.items(), key=lambda item: item[1], default=None)
    worst = min(traded.items(), key=lambda item: item[1], default=None)
    winners = sum(1 for value in traded.values() if value > 0)
    return f"""
        <dl class="controls">
          <div><dt>Starting equity</dt><dd>{money(opening)}</dd></div>
          <div><dt>Current equity</dt><dd>{money(closing)}</dd></div>
          <div><dt>Cumulative P&amp;L</dt><dd>{signed(total)} ({signed(total / opening * 100)}%)</dd></div>
          <div><dt>Sessions traded</dt><dd>{len(traded)} of {len(curve)}</dd></div>
          <div><dt>Best session</dt><dd>{signed(best[1]) if best else "—"}</dd></div>
          <div><dt>Worst session</dt><dd>{signed(worst[1]) if worst else "—"}</dd></div>
        </dl>
        {render_equity_curve(curve, opening)}
        <p class="muted note">Taken from Alpaca fills, after contract fees.
        {winners} of {len(traded)} traded sessions closed positive.</p>
    """


def render_index(
    destination: Path,
    fills: dict[date, dict[str, Any]],
    opening: float,
    policy: dict[str, Any],
) -> None:
    audits = sorted(destination.glob("????-??-??.html"), reverse=True)
    rows: list[str] = []
    for audit in audits:
        audit_date = date.fromisoformat(audit.stem)
        session = fills.get(audit_date) or {}
        pnl = signed(session["netPnl"]) if session.get("fills") else '<span class="muted">flat</span>'
        rows.append(
            f"""
            <a class="audit-row" href="{audit.name}">
              <time datetime="{audit.stem}">{audit_date.strftime("%d %B %Y")}</time>
              <span>SPY options · daily decision audit</span>
              <span class="row-pnl">{pnl}</span>
              <span aria-hidden="true">Open ↗</span>
            </a>
            """
        )
    if not rows:
        rows.append('<p class="empty">No daily audits published yet.</p>')

    def pct(key: str, fallback: float, sign: str = "") -> str:
        value = policy.get(key)
        return f"{sign}{float(fallback if value is None else value) * 100:.0f}%"

    rendered = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    for placeholder, value in {
        "{{MAX_CONTRACTS}}": str(int(policy.get("maxContracts") or 20)),
        "{{DAILY_LOSS}}": pct("dailyLossFraction", 0.10),
        "{{PREMIUM_STOP}}": pct("premiumStopFraction", 0.35, "−"),
        "{{BREAKEVEN}}": pct("breakevenTriggerFraction", 0.20, "+"),
        "{{PROFIT_TARGET}}": pct("profitTargetFraction", 0.50, "+"),
        "{{LATEST_AUDIT}}": audits[0].name if audits else "#audits",
        "{{AUDIT_COUNT}}": str(len(audits)),
        "<!-- PERFORMANCE -->": render_performance(fills, opening),
        "<!-- AUDIT_LINKS -->": "".join(rows),
    }.items():
        rendered = rendered.replace(placeholder, value)
    (destination / "index.html").write_text(rendered, encoding="utf-8")


async def load_policy(client: AsyncClient) -> dict[str, Any]:
    snapshot = await client.collection("policy").document("current").get()
    return snapshot.to_dict() or {} if snapshot.exists else {}


async def load(
    client: AsyncClient, day: date | None
) -> tuple[
    dict[date, list[dict[str, Any]]],
    dict[date, dict[str, Any]],
    dict[date, dict[str, Any]],
]:
    """Read decisions and narratives for one day, or for every stored day."""
    decisions: dict[date, list[dict[str, Any]]] = {}
    narratives: dict[date, dict[str, Any]] = {}
    fills: dict[date, dict[str, Any]] = {}

    query = client.collection("decisions")
    if day is not None:
        query = query.where(filter=FieldFilter("tradingDate", "==", day.isoformat()))
    async for snapshot in query.stream():
        record = snapshot.to_dict() or {}
        try:
            trading_date = date.fromisoformat(str(record.get("tradingDate")))
        except ValueError:
            continue
        decisions.setdefault(trading_date, []).append(record)

    if day is not None:
        snapshot = await client.collection("narratives").document(day.isoformat()).get()
        if snapshot.exists:
            narratives[day] = snapshot.to_dict()
    else:
        async for snapshot in client.collection("narratives").stream():
            try:
                narratives[date.fromisoformat(snapshot.id)] = snapshot.to_dict()
            except ValueError:
                continue

    if day is not None:
        snapshot = await client.collection("fills").document(day.isoformat()).get()
        if snapshot.exists:
            fills[day] = snapshot.to_dict()
    else:
        async for snapshot in client.collection("fills").stream():
            try:
                fills[date.fromisoformat(snapshot.id)] = snapshot.to_dict()
            except ValueError:
                continue

    return decisions, narratives, fills


def render_day(
    day: date,
    destination: Path,
    decisions: list[dict[str, Any]],
    narrative: dict[str, Any] | None,
    fills: dict[str, Any] | None,
) -> None:
    scheduled = [
        record for record in decisions if is_scheduled_tick(str(record.get("tickId") or ""), day)
    ]
    destination.write_text(render_page(day, scheduled, narrative, fills), encoding="utf-8")
    print(f"wrote {destination} ({len(scheduled)} ticks)")


def publish(project: str | None, channel: str | None) -> None:
    if not project:
        raise ValueError("GCP_PROJECT_ID is required to publish")
    command = ["firebase"]
    if channel:
        command.extend(["hosting:channel:deploy", channel, "--expires", "7d"])
    else:
        command.extend(["deploy", "--only", "hosting"])
    command.extend(["--project", project, "--non-interactive"])
    subprocess.run(command, check=True, cwd=ROOT)


async def run(arguments: argparse.Namespace) -> None:
    project = os.getenv("GCP_PROJECT_ID")
    destination = arguments.output_dir
    destination.mkdir(parents=True, exist_ok=True)

    day = None if arguments.all else arguments.date or datetime.now(ET).date()
    client = AsyncClient(project=project)
    try:
        decisions, narratives, fills = await load(client, day)
        policy = await load_policy(client)
    finally:
        client.close()

    if day is None:
        for existing in destination.glob("????-??-??.html"):
            existing.unlink()
        days = sorted(decisions.keys() | narratives.keys() | fills.keys())
    else:
        days = [day]

    for current in days:
        render_day(
            current,
            destination / f"{current.isoformat()}.html",
            decisions.get(current, []),
            narratives.get(current),
            fills.get(current),
        )

    render_index(destination, fills, starting_equity(decisions), policy)

    if arguments.publish:
        publish(project, arguments.channel)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Firestore trading audits as HTML")
    parser.add_argument("date", nargs="?", type=date.fromisoformat)
    parser.add_argument("--all", action="store_true", help="render every stored trading day")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "web" / "dist")
    parser.add_argument("--publish", action="store_true", help="deploy with Firebase Hosting")
    parser.add_argument(
        "--channel",
        default=os.getenv("FIREBASE_HOSTING_CHANNEL"),
        help="publish to a Firebase preview channel instead of live",
    )
    arguments = parser.parse_args()
    if arguments.all and arguments.date:
        parser.error("--all cannot be combined with a date")
    return arguments


if __name__ == "__main__":
    asyncio.run(run(parse_args()))

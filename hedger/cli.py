"""Interactive terminal dashboard and CLI for Kalshi-Polymarket Hedger.

Displays:
- Real-time portfolio delta gauge ([-100 ... 0 ... +100])
- Venue exposure comparison (USD on Kalshi vs USDC on Polymarket)
- Active hedges and execution audit log
- Real-time PnL and fee breakdown (CFTC regulatory fees vs Polygon gas)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

from hedger.client import NaxosCrossVenueClient
from hedger.math_hedger import (
    calculate_cftc_quadratic_fee,
    calculate_polygon_gas_cost_usd,
    marginal_cftc_fee_rate,
)
from hedger.models import (
    DeltaRiskProfile,
    FeeScheduleConfig,
    HedgeStrategy,
    LegExecution,
    Venue,
)
from hedger.rebalancer import HedgingRebalancer


# Terminal color constants
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BG_BLUE = "\033[44m"
BG_DARK = "\033[40m"


def clear_screen() -> None:
    """Clear terminal screen if in interactive mode."""
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def render_delta_gauge(net_delta: float, threshold: float = 25.0, width: int = 30) -> str:
    """Render an ASCII delta exposure gauge centered at zero."""
    max_range = max(threshold * 2.5, 100.0)
    clamped_delta = max(-max_range, min(max_range, net_delta))
    
    # Normalized position from 0 to width
    ratio = (clamped_delta + max_range) / (2.0 * max_range)
    cursor_pos = int(ratio * (width - 1))
    center_pos = int(width / 2)

    bar = ["-"] * width
    bar[center_pos] = "|"

    if abs(net_delta) < threshold:
        gauge_color = GREEN
        status_tag = f"{GREEN}[NEUTRAL]{RESET}"
    else:
        gauge_color = RED if abs(net_delta) >= threshold * 1.5 else YELLOW
        status_tag = f"{RED}[DRIFT ALERT]{RESET}"

    if cursor_pos < center_pos:
        for i in range(cursor_pos, center_pos):
            bar[i] = "="
    elif cursor_pos > center_pos:
        for i in range(center_pos + 1, cursor_pos + 1):
            bar[i] = "="
    bar[cursor_pos] = "*"

    gauge_str = "".join(bar)
    return f"{gauge_color}[{gauge_str}]{RESET}  Δ: {BOLD}{net_delta:+.1f}{RESET}  {status_tag}"


def render_dashboard(
    rebalancer: HedgingRebalancer,
    profile: DeltaRiskProfile,
    latest_exec: Optional[LegExecution] = None,
) -> None:
    """Render full interactive terminal dashboard."""
    clear_screen()
    pos = rebalancer.cross_position
    k_pos = pos.kalshi_pos
    pm_pos = pos.polymarket_pos
    summary = rebalancer.get_portfolio_summary()

    header = (
        f"{BOLD}{CYAN}═══════════════════════════════════════════════════════════════════════════════{RESET}\n"
        f"  {BOLD}{WHITE}KALSHI ⟷ POLYMARKET DELTA HEDGER{RESET}  {DIM}|  NaxosData Cross-Venue Engine{RESET}\n"
        f"  {DIM}Pair: {WHITE}{rebalancer.pair.pair_id}{DIM}  |  Title: {rebalancer.pair.title}{RESET}\n"
        f"{BOLD}{CYAN}═══════════════════════════════════════════════════════════════════════════════{RESET}"
    )
    print(header)

    # 1. Delta Risk Gauge
    print(f"\n{BOLD}1. PORTFOLIO DELTA NEUTRALITY{RESET}")
    print(f"   Delta Neutral Band:  ±{rebalancer.delta_threshold:.1f} contracts")
    print(f"   Gauge:  {render_delta_gauge(profile.net_delta, rebalancer.delta_threshold)}")
    print(f"   Gross Contracts:     {BOLD}{profile.gross_delta:.1f}{RESET}  |  Notional Delta: {BOLD}${profile.notional_delta_usd:.2f}{RESET}")
    print(f"   Cross-Venue Basis:   {BOLD}{profile.basis:+.3f}{RESET} (Kalshi mid - PM mid)")
    print(f"   Status Action:       {BOLD}{profile.recommended_action}{RESET}")

    # 2. Venue Positions & Exposure
    print(f"\n{BOLD}2. VENUE EXPOSURE BREAKDOWN{RESET}")
    print(f"   {'Metric':<24} {'KALSHI (CFTC Regulated)':<25} {'POLYMARKET (Polygon CLOB)':<25}")
    print(f"   {'-'*74}")
    k_dir = "LONG YES" if k_pos.contracts >= 0 else "SHORT YES"
    pm_dir = "LONG YES" if pm_pos.contracts >= 0 else "LONG NO / SHORT YES"
    print(f"   {'Position Direction':<24} {BOLD}{k_dir:<25}{RESET} {BOLD}{pm_dir:<25}{RESET}")
    print(f"   {'Contracts Held':<24} {abs(k_pos.contracts):<25.1f} {abs(pm_pos.contracts):<25.1f}")
    print(f"   {'Mark / Mid Price':<24} ${k_pos.current_price:<24.3f} ${pm_pos.current_price:<24.3f}")
    print(f"   {'Venue Notional Value':<24} ${k_pos.notional_value:<24.2f} ${pm_pos.notional_value:<24.2f}")
    
    k_pnl_str = f"{GREEN if k_pos.unrealized_pnl >= 0 else RED}${k_pos.unrealized_pnl:+.2f}{RESET}"
    pm_pnl_str = f"{GREEN if pm_pos.unrealized_pnl >= 0 else RED}${pm_pos.unrealized_pnl:+.2f}{RESET}"
    print(f"   {'Unrealized PnL':<24} {k_pnl_str:<34} {pm_pnl_str:<34}")
    print(f"   {'Cash Balance':<24} ${rebalancer.kalshi_usd_balance:<24.2f} ${rebalancer.polymarket_usdc_balance:<24.2f}")

    # 3. Fees & Net Performance
    total_unrealized = summary["total_unrealized_pnl"]
    total_fees = summary["total_fees_paid"]
    net_pnl = summary["net_pnl_after_fees"]
    net_pnl_col = GREEN if net_pnl >= 0 else RED

    print(f"\n{BOLD}3. RISK-ADJUSTED PnL & TRANSACTION COSTS{RESET}")
    print(f"   Total Unrealized PnL:  ${total_unrealized:+.2f}")
    print(f"   CFTC Fees Paid:        ${rebalancer.total_cftc_fees_paid:.4f}  (Kalshi quadratic schedule)")
    print(f"   Polygon Gas Paid:      ${rebalancer.total_gas_fees_paid:.4f}  (USDC/POL relayer)")
    print(f"   Total Fee Drag:        ${total_fees:.4f}")
    print(f"   {BOLD}Net Portfolio PnL:     {net_pnl_col}${net_pnl:+.4f}{RESET}")

    # 4. Recent Executions
    print(f"\n{BOLD}4. RECENT HEDGE EXECUTIONS ({len(rebalancer.execution_history)} total){RESET}")
    if not rebalancer.execution_history:
        print(f"   {DIM}No hedge executions yet. Portfolio currently within delta tolerance.{RESET}")
    else:
        print(f"   {'Time':<10} {'Venue':<12} {'Side':<6} {'Outcome':<8} {'Qty':<8} {'Fill Price':<12} {'Fee/Gas':<10}")
        print(f"   {'-'*68}")
        for ex in rebalancer.execution_history[-5:]:
            t_str = time.strftime("%H:%M:%S", time.localtime(ex.timestamp))
            fee_str = f"${ex.fee_paid:.3f}" if ex.venue == Venue.KALSHI else f"${ex.gas_paid_usd:.4f}"
            print(
                f"   {t_str:<10} {ex.venue.value.capitalize():<12} {ex.side.value.upper():<6} "
                f"{ex.outcome.value.upper():<8} {ex.filled_contracts:<8.1f} ${ex.fill_price:<11.3f} {fee_str:<10}"
            )

    print(f"\n{DIM}[Press Ctrl+C to stop monitoring]{RESET}")


def cmd_monitor(args: argparse.Namespace) -> None:
    """Execute continuous monitoring loop."""
    client = NaxosCrossVenueClient(
        base_url=args.api_url,
        api_key=args.api_key,
        offline=args.offline,
    )
    pair = client.get_market_pair(args.pair)
    if not pair:
        pairs = client.get_pairs()
        pair = pairs[0] if pairs else None
        if not pair:
            print(f"{RED}Error: Market pair '{args.pair}' not found.{RESET}")
            sys.exit(1)

    print(f"{CYAN}Starting Kalshi-Polymarket Hedger on {pair.pair_id}...{RESET}")
    rebalancer = HedgingRebalancer(
        client=client,
        pair=pair,
        delta_threshold=args.threshold,
        initial_kalshi_contracts=args.kalshi_contracts,
        initial_polymarket_contracts=args.polymarket_contracts,
        paper_trading=True,
    )

    def on_tick(profile: DeltaRiskProfile, execution: Optional[LegExecution]) -> None:
        render_dashboard(rebalancer, profile, execution)

    rebalancer.run_continuous(
        interval_seconds=args.interval,
        max_iterations=args.max_iterations,
        callback=on_tick,
    )


def cmd_check(args: argparse.Namespace) -> None:
    """One-shot delta check and recommendation."""
    client = NaxosCrossVenueClient(
        base_url=args.api_url,
        api_key=args.api_key,
        offline=args.offline,
    )
    pair = client.get_market_pair(args.pair) or client.get_pairs()[0]
    rebalancer = HedgingRebalancer(
        client=client,
        pair=pair,
        delta_threshold=args.threshold,
        initial_kalshi_contracts=args.kalshi_contracts,
        initial_polymarket_contracts=args.polymarket_contracts,
        paper_trading=True,
    )

    profile, _ = rebalancer.step()
    render_dashboard(rebalancer, profile)


def cmd_pairs(args: argparse.Namespace) -> None:
    """List available cross-venue pairs."""
    client = NaxosCrossVenueClient(
        base_url=args.api_url,
        api_key=args.api_key,
        offline=args.offline,
    )
    pairs = client.get_pairs()
    print(f"\n{BOLD}{CYAN}Synchronized Cross-Venue Prediction Pairs (NaxosData){RESET}")
    print(f"{'-'*78}")
    print(f"{'Pair ID':<22} {'Correlation':<12} {'Kalshi Ticker':<18} {'Polymarket Slug'}")
    print(f"{'-'*78}")
    for p in pairs:
        print(f"{BOLD}{p.pair_id:<22}{RESET} {p.correlation:<12.3f} {p.kalshi_market_id:<18} {p.polymarket_slug or p.polymarket_market_id[:16]}")
    print()


def cmd_calc(args: argparse.Namespace) -> None:
    """Interactive fee and gas budget calculator."""
    contracts = args.contracts
    price = args.price
    cftc_fee = calculate_cftc_quadratic_fee(contracts, price)
    marginal_fee = marginal_cftc_fee_rate(price)
    gas_usd = calculate_polygon_gas_cost_usd(gas_price_gwei=args.gas_price, pol_price_usd=args.pol_price)
    notional = contracts * price

    print(f"\n{BOLD}{CYAN}Cross-Venue Fee & Gas Budget Calculator{RESET}")
    print(f"{'-'*50}")
    print(f"Contracts Traded:     {contracts:.1f}")
    print(f"Execution Price:      ${price:.3f}")
    print(f"Total Notional Value: ${notional:.2f}")
    print(f"{'-'*50}")
    print(f"CFTC Quadratic Fee (Kalshi):  ${cftc_fee:.4f}  (Marginal: ${marginal_fee:.4f}/contract)")
    print(f"Polygon Gas Cost (Polymarket): ${gas_usd:.6f}  (at {args.gas_price:.0f} Gwei, POL=${args.pol_price:.2f})")
    gas_drag_pct = (gas_usd / max(notional, 0.01)) * 100.0
    print(f"Polygon Gas Drag:              {gas_drag_pct:.3f}% of notional")
    print()


def build_parser() -> argparse.ArgumentParser:
    """Build command line argument parser with global and subcommand options."""
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("--api-url", default="https://api.naxosdata.com", help="NaxosData API base URL")
    common_parent.add_argument("--api-key", default=os.getenv("NAXOS_API_KEY"), help="NaxosData API Key")
    common_parent.add_argument("--dry-run", "--offline", dest="offline", action="store_true", default=True, help="Run in offline simulation mode")

    parser = argparse.ArgumentParser(
        prog="kalshi-hedger",
        description="Cross-venue delta hedging engine for Kalshi and Polymarket prediction markets.",
        parents=[common_parent],
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand to run")

    # monitor
    p_mon = subparsers.add_parser("monitor", parents=[common_parent], help="Continuous monitoring and rebalancing loop")
    p_mon.add_argument("--pair", default="FED-MAY-2026-CUT", help="Pair ID to monitor")
    p_mon.add_argument("--threshold", type=float, default=25.0, help="Delta threshold to trigger hedge")
    p_mon.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    p_mon.add_argument("--kalshi-contracts", type=float, default=100.0, help="Initial Kalshi position contracts")
    p_mon.add_argument("--polymarket-contracts", type=float, default=0.0, help="Initial Polymarket position contracts")
    p_mon.add_argument("--max-iterations", type=int, default=None, help="Stop after N iterations (for testing)")

    # check
    p_chk = subparsers.add_parser("check", parents=[common_parent], help="One-shot delta check and recommendation")
    p_chk.add_argument("--pair", default="FED-MAY-2026-CUT", help="Pair ID to inspect")
    p_chk.add_argument("--threshold", type=float, default=25.0, help="Delta threshold")
    p_chk.add_argument("--kalshi-contracts", type=float, default=100.0, help="Initial Kalshi position contracts")
    p_chk.add_argument("--polymarket-contracts", type=float, default=0.0, help="Initial Polymarket position contracts")

    # pairs
    subparsers.add_parser("pairs", parents=[common_parent], help="List available synchronized pairs")

    # calc
    p_calc = subparsers.add_parser("calc", parents=[common_parent], help="CFTC fee and gas budget calculator")
    p_calc.add_argument("--contracts", type=float, default=100.0, help="Contract quantity")
    p_calc.add_argument("--price", type=float, default=0.50, help="Contract price (0.01 to 0.99)")
    p_calc.add_argument("--gas-price", type=float, default=30.0, help="Gas price in Gwei")
    p_calc.add_argument("--pol-price", type=float, default=0.50, help="POL price in USD")

    return parser


def main(argv: Optional[list] = None) -> None:
    """CLI Entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # Default to check if no command specified
        args.command = "check"
        args.pair = "FED-MAY-2026-CUT"
        args.threshold = 25.0
        args.kalshi_contracts = 100.0
        args.polymarket_contracts = 0.0

    if args.command == "monitor":
        cmd_monitor(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "pairs":
        cmd_pairs(args)
    elif args.command == "calc":
        cmd_calc(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

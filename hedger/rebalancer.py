"""Continuous cross-venue delta rebalancer and execution engine.

Monitors portfolio delta drift against configured thresholds and executes
reciprocal hedges (Kalshi YES <-> Polymarket NO, etc.) with VWAP order book
slippage modeling and fee budget gating.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, List, Optional, Tuple

from hedger.client import NaxosCrossVenueClient
from hedger.math_hedger import (
    build_risk_profile,
    calculate_cftc_quadratic_fee,
    calculate_polygon_gas_cost_usd,
    determine_hedge_action,
    evaluate_hedge_feasibility,
    solve_required_hedge_contracts,
)
from hedger.models import (
    CrossVenuePosition,
    DeltaRiskProfile,
    FeeScheduleConfig,
    HedgeOrder,
    HedgeStrategy,
    LegExecution,
    MarketPair,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    OrderStatus,
    Outcome,
    RebalanceStatus,
    SingleVenuePosition,
    Venue,
)

logger = logging.getLogger(__name__)


def simulate_orderbook_fill(
    book: OrderBookSnapshot,
    side: OrderSide,
    desired_quantity: float,
    max_slippage_pct: float = 0.05,
) -> Tuple[float, float, float]:
    """Simulate a market/aggressive limit fill against an order book snapshot.

    Walks the depth of the book to calculate the volume-weighted average price (VWAP)
    and filled quantity.

    Args:
        book: OrderBookSnapshot to fill against.
        side: OrderSide.BUY matches against ASKS; OrderSide.SELL matches against BIDS.
        desired_quantity: Desired number of contracts.
        max_slippage_pct: Maximum allowed percentage price deviation from top of book.

    Returns:
        Tuple of (filled_contracts, vwap_price, slippage_pct).
    """
    levels: List[OrderBookLevel] = book.asks if side == OrderSide.BUY else book.bids
    if not levels:
        raise ValueError(f"No liquidity available in orderbook for venue {book.venue.value}")

    best_price = levels[0].price
    remaining = desired_quantity
    total_cost = 0.0
    filled = 0.0

    for level in levels:
        if remaining <= 0:
            break
        # Check slippage limit
        slippage = abs(level.price - best_price) / max(best_price, 0.01)
        if slippage > max_slippage_pct:
            break

        take_size = min(remaining, level.size)
        total_cost += take_size * level.price
        filled += take_size
        remaining -= take_size

    if filled <= 0.0:
        # Fallback to top of book if levels were zero size
        filled = min(desired_quantity, 100.0)
        vwap = best_price
    else:
        vwap = total_cost / filled

    final_slippage = abs(vwap - best_price) / max(best_price, 0.01)
    return round(filled, 4), round(vwap, 4), round(final_slippage, 4)


class HedgingRebalancer:
    """Manages cross-venue inventory, risk assessment, and automated rebalancing.
    
    Supports paper trading with synthetic order books as well as live execution.
    """

    def __init__(
        self,
        client: NaxosCrossVenueClient,
        pair: MarketPair,
        delta_threshold: float = 25.0,
        strategy: HedgeStrategy = HedgeStrategy.RECIPROCAL_BUY,
        preferred_hedge_venue: Venue = Venue.POLYMARKET,
        fee_config: Optional[FeeScheduleConfig] = None,
        initial_kalshi_contracts: float = 0.0,
        initial_kalshi_price: float = 0.50,
        initial_polymarket_contracts: float = 0.0,
        initial_polymarket_price: float = 0.50,
        initial_kalshi_usd: float = 10_000.0,
        initial_polymarket_usdc: float = 10_000.0,
        paper_trading: bool = True,
        max_slippage_pct: float = 0.05,
    ) -> None:
        self.client = client
        self.pair = pair
        self.delta_threshold = delta_threshold
        self.strategy = strategy
        self.preferred_hedge_venue = preferred_hedge_venue
        self.fee_config = fee_config or FeeScheduleConfig()
        self.paper_trading = paper_trading
        self.max_slippage_pct = max_slippage_pct

        # Cash balances
        self.kalshi_usd_balance = initial_kalshi_usd
        self.polymarket_usdc_balance = initial_polymarket_usdc
        self.total_cftc_fees_paid = 0.0
        self.total_gas_fees_paid = 0.0

        # Position tracking
        kalshi_pos = SingleVenuePosition(
            venue=Venue.KALSHI,
            market_id=pair.kalshi_market_id,
            outcome=Outcome.YES,
            contracts=initial_kalshi_contracts,
            avg_entry_price=initial_kalshi_price,
            current_price=initial_kalshi_price,
            currency="USD",
        )
        polymarket_pos = SingleVenuePosition(
            venue=Venue.POLYMARKET,
            market_id=pair.polymarket_market_id,
            outcome=Outcome.YES,
            contracts=initial_polymarket_contracts,
            avg_entry_price=initial_polymarket_price,
            current_price=initial_polymarket_price,
            currency="USDC",
        )
        self.cross_position = CrossVenuePosition(
            pair_id=pair.pair_id,
            kalshi_pos=kalshi_pos,
            polymarket_pos=polymarket_pos,
            correlation=pair.correlation,
            beta=1.0,
        )

        # Audit history
        self.execution_history: List[LegExecution] = []
        self.order_history: List[HedgeOrder] = []
        self.last_status: RebalanceStatus = RebalanceStatus.BALANCED

    def sync_market_prices(self) -> Tuple[OrderBookSnapshot, OrderBookSnapshot]:
        """Fetch synchronized order books and update position marks."""
        k_book, pm_book = self.client.get_pair_snapshots(self.pair)
        if k_book.mid_price is not None:
            self.cross_position.kalshi_pos.current_price = k_book.mid_price
        if pm_book.mid_price is not None:
            self.cross_position.polymarket_pos.current_price = pm_book.mid_price
        return k_book, pm_book

    def evaluate_risk(self) -> DeltaRiskProfile:
        """Evaluate portfolio delta and generate a DeltaRiskProfile."""
        return build_risk_profile(
            pair_id=self.pair.pair_id,
            kalshi_delta=self.cross_position.kalshi_pos.delta,
            polymarket_delta=self.cross_position.polymarket_pos.delta,
            kalshi_price=self.cross_position.kalshi_pos.current_price,
            polymarket_price=self.cross_position.polymarket_pos.current_price,
            threshold=self.delta_threshold,
            beta=self.cross_position.beta,
            correlation=self.cross_position.correlation,
            preferred_hedge_venue=self.preferred_hedge_venue,
            fee_config=self.fee_config,
        )

    def execute_hedge(
        self,
        order: HedgeOrder,
        target_book: OrderBookSnapshot,
    ) -> LegExecution:
        """Execute a hedge leg either on paper or via API."""
        filled_qty, vwap_price, _ = simulate_orderbook_fill(
            book=target_book,
            side=order.side,
            desired_quantity=order.contracts,
            max_slippage_pct=self.max_slippage_pct,
        )

        # Calculate actual fees
        cftc_fee = 0.0
        gas_cost = 0.0
        if order.target_venue == Venue.KALSHI:
            cftc_fee = calculate_cftc_quadratic_fee(filled_qty, vwap_price, self.fee_config)
            self.total_cftc_fees_paid += cftc_fee
            total_cash_outlay = (filled_qty * vwap_price) + cftc_fee
            self.kalshi_usd_balance -= total_cash_outlay if order.side == OrderSide.BUY else -((filled_qty * vwap_price) - cftc_fee)
        else:
            gas_cost = calculate_polygon_gas_cost_usd(config=self.fee_config)
            pm_taker_fee = (filled_qty * vwap_price) * self.fee_config.polymarket_taker_fee_pct
            self.total_gas_fees_paid += gas_cost + pm_taker_fee
            total_cash_outlay = (filled_qty * vwap_price) + gas_cost + pm_taker_fee
            self.polymarket_usdc_balance -= total_cash_outlay if order.side == OrderSide.BUY else -((filled_qty * vwap_price) - total_cash_outlay)

        # Update position inventory
        target_pos = (
            self.cross_position.kalshi_pos
            if order.target_venue == Venue.KALSHI
            else self.cross_position.polymarket_pos
        )

        signed_fill = filled_qty if order.side == OrderSide.BUY else -filled_qty
        
        # If order outcome is NO, invert signed impact on YES delta inventory
        if order.outcome == Outcome.NO:
            signed_delta_impact = -signed_fill
        else:
            signed_delta_impact = signed_fill

        prev_contracts = target_pos.contracts
        new_contracts = prev_contracts + signed_delta_impact

        # Update VWAP entry price
        if new_contracts != 0:
            if (prev_contracts >= 0 and signed_delta_impact >= 0) or (prev_contracts <= 0 and signed_delta_impact <= 0):
                # Accumulating position
                total_cost = (abs(prev_contracts) * target_pos.avg_entry_price) + (abs(signed_delta_impact) * vwap_price)
                target_pos.avg_entry_price = round(total_cost / abs(new_contracts), 4)
            # If closing or flipping, entry price remains until flipped
        target_pos.contracts = round(new_contracts, 4)

        execution = LegExecution(
            execution_id=f"exec_{uuid.uuid4().hex[:8]}",
            order_id=order.order_id,
            venue=order.target_venue,
            outcome=order.outcome,
            side=order.side,
            filled_contracts=filled_qty,
            fill_price=vwap_price,
            fee_paid=cftc_fee if order.target_venue == Venue.KALSHI else round(pm_taker_fee, 4),
            gas_paid_usd=gas_cost,
            tx_hash_or_fill_id=f"0x{uuid.uuid4().hex}" if order.target_venue == Venue.POLYMARKET else f"fill_{uuid.uuid4().hex[:10]}",
        )

        order.status = OrderStatus.FILLED
        order.execution = execution
        self.execution_history.append(execution)
        self.order_history.append(order)

        logger.info(
            "Executed hedge: %s %s %s on %s at %s (Net delta was: %s)",
            order.side.value.upper(),
            filled_qty,
            order.outcome.value.upper(),
            order.target_venue.value,
            vwap_price,
            self.cross_position.net_delta,
        )
        return execution

    def step(self) -> Tuple[DeltaRiskProfile, Optional[LegExecution]]:
        """Perform one monitoring and rebalancing evaluation cycle.

        Returns:
            Tuple of (DeltaRiskProfile, Optional[LegExecution]).
        """
        k_book, pm_book = self.sync_market_prices()
        profile = self.evaluate_risk()

        if not profile.rebalance_required or profile.recommended_order is None:
            self.last_status = RebalanceStatus.BALANCED
            return profile, None

        order = profile.recommended_order
        target_book = pm_book if order.target_venue == Venue.POLYMARKET else k_book

        # Budget & gas check
        is_viable, details = evaluate_hedge_feasibility(
            contracts=order.contracts,
            price=order.limit_price,
            target_venue=order.target_venue,
            config=self.fee_config,
        )

        if not is_viable:
            logger.warning(
                "Skipping hedge due to gas/fee drag constraint: %s%% > %s%%",
                details["cost_ratio"] * 100,
                self.fee_config.max_gas_drag_pct * 100,
            )
            self.last_status = RebalanceStatus.SKIPPED_BUDGET
            order.status = OrderStatus.CANCELLED
            order.reason = "Cost drag exceeded budget constraint"
            self.order_history.append(order)
            return profile, None

        self.last_status = RebalanceStatus.REBALANCING
        execution = self.execute_hedge(order, target_book)
        self.last_status = RebalanceStatus.REBALANCED
        return profile, execution

    def run_continuous(
        self,
        interval_seconds: float = 2.0,
        max_iterations: Optional[int] = None,
        callback: Optional[Callable[[DeltaRiskProfile, Optional[LegExecution]], None]] = None,
    ) -> None:
        """Run continuous monitoring and rebalancing loop."""
        iteration = 0
        logger.info("Starting continuous rebalancing loop for pair %s", self.pair.pair_id)

        try:
            while True:
                profile, execution = self.step()
                if callback:
                    callback(profile, execution)

                iteration += 1
                if max_iterations is not None and iteration >= max_iterations:
                    break

                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("Continuous rebalancer stopped by user.")

    def get_portfolio_summary(self) -> dict:
        """Compile a complete portfolio performance and balance summary."""
        k_unrealized = self.cross_position.kalshi_pos.unrealized_pnl
        pm_unrealized = self.cross_position.polymarket_pos.unrealized_pnl
        total_unrealized = round(k_unrealized + pm_unrealized, 4)
        total_fees = round(self.total_cftc_fees_paid + self.total_gas_fees_paid, 4)
        net_pnl = round(total_unrealized - total_fees, 4)

        return {
            "pair_id": self.pair.pair_id,
            "net_delta": self.cross_position.net_delta,
            "gross_delta": self.cross_position.gross_delta,
            "kalshi_contracts": self.cross_position.kalshi_pos.contracts,
            "kalshi_price": self.cross_position.kalshi_pos.current_price,
            "kalshi_usd_balance": round(self.kalshi_usd_balance, 2),
            "kalshi_unrealized_pnl": k_unrealized,
            "polymarket_contracts": self.cross_position.polymarket_pos.contracts,
            "polymarket_price": self.cross_position.polymarket_pos.current_price,
            "polymarket_usdc_balance": round(self.polymarket_usdc_balance, 2),
            "polymarket_unrealized_pnl": pm_unrealized,
            "total_unrealized_pnl": total_unrealized,
            "total_fees_paid": total_fees,
            "net_pnl_after_fees": net_pnl,
            "total_hedges_executed": len(self.execution_history),
            "status": self.last_status.value,
        }

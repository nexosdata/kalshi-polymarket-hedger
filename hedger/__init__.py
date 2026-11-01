"""Kalshi-Polymarket Hedger: Production Cross-Venue Prediction Market Delta Hedging Engine.

Hedging and risk management between CFTC-regulated Kalshi (USD) and Polygon CLOB Polymarket (USDC).
Powered by NaxosData institutional market data feeds.
"""

from __future__ import annotations

from hedger.client import NaxosCrossVenueClient
from hedger.math_hedger import (
    calculate_basis_risk,
    calculate_cftc_quadratic_fee,
    calculate_polygon_gas_cost_usd,
    determine_hedge_action,
    evaluate_hedge_feasibility,
    marginal_cftc_fee_rate,
    reciprocal_outcome,
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
from hedger.rebalancer import HedgingRebalancer

__version__ = "1.0.0"
__all__ = [
    "CrossVenuePosition",
    "DeltaRiskProfile",
    "FeeScheduleConfig",
    "HedgeOrder",
    "HedgeStrategy",
    "HedgingRebalancer",
    "LegExecution",
    "MarketPair",
    "NaxosCrossVenueClient",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "OrderSide",
    "OrderStatus",
    "Outcome",
    "RebalanceStatus",
    "SingleVenuePosition",
    "Venue",
    "calculate_basis_risk",
    "calculate_cftc_quadratic_fee",
    "calculate_polygon_gas_cost_usd",
    "determine_hedge_action",
    "evaluate_hedge_feasibility",
    "marginal_cftc_fee_rate",
    "reciprocal_outcome",
    "solve_required_hedge_contracts",
]

"""Cross-venue prediction market hedging data models.

Defines Pydantic models for cross-venue positions, hedge orders,
delta risk profiles, order books, and execution logs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, computed_field, ConfigDict


class Venue(str, Enum):
    """Prediction market trading venues."""
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Outcome(str, Enum):
    """Binary contract outcomes."""
    YES = "yes"
    NO = "no"


class OrderSide(str, Enum):
    """Trading order sides."""
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """Order lifecycle states."""
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class HedgeStrategy(str, Enum):
    """Hedging execution strategy."""
    RECIPROCAL_BUY = "reciprocal_buy"  # Long Kalshi YES -> Long Polymarket NO
    DIRECT_SELL = "direct_sell"        # Long Kalshi YES -> Short/Sell Polymarket YES


class RebalanceStatus(str, Enum):
    """Status of portfolio delta rebalancing."""
    BALANCED = "balanced"
    DRIFT_DETECTED = "drift_detected"
    REBALANCING = "rebalancing"
    REBALANCED = "rebalanced"
    SKIPPED_BUDGET = "skipped_budget"


class OrderBookLevel(BaseModel):
    """A single price level in an order book."""
    model_config = ConfigDict(frozen=True)

    price: float = Field(ge=0.0, le=1.0, description="Normalized price in dollars (0.00 to 1.00)")
    size: float = Field(ge=0.0, description="Number of contracts available at this price")


class OrderBookSnapshot(BaseModel):
    """Full snapshot of an order book for a venue and contract."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    venue: Venue
    market_id: str
    outcome: Outcome = Outcome.YES
    bids: List[OrderBookLevel] = Field(default_factory=list, description="Bids sorted descending by price")
    asks: List[OrderBookLevel] = Field(default_factory=list, description="Asks sorted ascending by price")
    timestamp: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2.0, 4)
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return round(self.best_ask - self.best_bid, 4)
        return None


class MarketPair(BaseModel):
    """A pair of fungible or correlated binary contracts on Kalshi and Polymarket."""
    pair_id: str = Field(description="Unique pair identifier, e.g., 'FED-MAY-2026-CUT'")
    title: str = Field(description="Readable market title")
    kalshi_market_id: str = Field(description="Kalshi ticker / market ID")
    polymarket_market_id: str = Field(description="Polymarket condition ID / token ID")
    expiry_timestamp: Optional[float] = None
    correlation: float = Field(default=1.0, ge=-1.0, le=1.0, description="Statistical outcome correlation")
    polymarket_slug: Optional[str] = None


class SingleVenuePosition(BaseModel):
    """Position state on a single venue."""
    venue: Venue
    market_id: str
    outcome: Outcome = Outcome.YES
    contracts: float = Field(default=0.0, description="Signed contract quantity (+ long, - short)")
    avg_entry_price: float = Field(default=0.0, ge=0.0, le=1.0, description="Volume-weighted average entry price")
    current_price: float = Field(default=0.0, ge=0.0, le=1.0, description="Latest mark or mid price")
    currency: str = Field(default="USD", description="'USD' for Kalshi, 'USDC' for Polymarket")

    @computed_field
    @property
    def notional_value(self) -> float:
        """Current market value of the position in venue currency."""
        return round(abs(self.contracts) * self.current_price, 4)

    @computed_field
    @property
    def cost_basis(self) -> float:
        """Initial cost basis of open contracts."""
        return round(abs(self.contracts) * self.avg_entry_price, 4)

    @computed_field
    @property
    def unrealized_pnl(self) -> float:
        """Unrealized profit/loss in venue currency."""
        if self.contracts == 0:
            return 0.0
        pnl = (self.current_price - self.avg_entry_price) * self.contracts
        return round(pnl, 4)

    @computed_field
    @property
    def delta(self) -> float:
        """Delta exposure relative to binary event probability.
        
        Long YES has delta +1 per contract; Long NO has delta -1 per contract.
        Short YES has delta -1; Short NO has delta +1.
        """
        multiplier = 1.0 if self.outcome == Outcome.YES else -1.0
        return round(self.contracts * multiplier, 4)


class LegExecution(BaseModel):
    """Execution receipt for an executed hedge leg."""
    execution_id: str
    order_id: str
    venue: Venue
    outcome: Outcome
    side: OrderSide
    filled_contracts: float = Field(gt=0.0)
    fill_price: float = Field(ge=0.0, le=1.0)
    fee_paid: float = Field(default=0.0, ge=0.0, description="Exchange / CFTC fee paid in venue currency")
    gas_paid_usd: float = Field(default=0.0, ge=0.0, description="Polygon gas fee paid in USD")
    timestamp: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    tx_hash_or_fill_id: Optional[str] = None


class HedgeOrder(BaseModel):
    """A proposed or executed hedge order."""
    order_id: str
    pair_id: str
    target_venue: Venue
    outcome: Outcome
    side: OrderSide
    contracts: float = Field(gt=0.0)
    limit_price: float = Field(ge=0.0, le=1.0)
    estimated_cftc_fee: float = Field(default=0.0, ge=0.0)
    estimated_gas_cost: float = Field(default=0.0, ge=0.0)
    estimated_total_cost: float = Field(default=0.0, ge=0.0)
    reason: str = Field(default="")
    status: OrderStatus = OrderStatus.PENDING
    execution: Optional[LegExecution] = None
    created_at: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


class DeltaRiskProfile(BaseModel):
    """Statistical risk assessment and delta neutrality breakdown."""
    pair_id: str
    gross_delta: float = Field(description="Gross sum of absolute deltas across venues")
    net_delta: float = Field(description="Net delta exposure: Kalshi delta + beta * Polymarket delta")
    notional_delta_usd: float = Field(description="Dollar delta equivalent at prevailing prices")
    basis: float = Field(description="Kalshi price - Polymarket price")
    basis_risk_var: float = Field(description="Statistical variance of the cross-venue basis")
    drift_magnitude: float = Field(description="Absolute deviation from neutrality |net_delta|")
    threshold: float = Field(description="Rebalance trigger threshold")
    rebalance_required: bool = Field(description="True if drift exceeds threshold")
    recommended_action: Optional[str] = None
    recommended_order: Optional[HedgeOrder] = None
    timestamp: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


class CrossVenuePosition(BaseModel):
    """Cross-venue paired position tracking Kalshi and Polymarket exposures."""
    pair_id: str
    kalshi_pos: SingleVenuePosition
    polymarket_pos: SingleVenuePosition
    correlation: float = Field(default=1.0, ge=-1.0, le=1.0)
    beta: float = Field(default=1.0, ge=0.0, description="Hedge ratio: rho * (sigma_K / sigma_PM)")

    @computed_field
    @property
    def net_delta(self) -> float:
        """Net portfolio delta: Kalshi delta + beta * Polymarket delta."""
        return round(self.kalshi_pos.delta + (self.beta * self.polymarket_pos.delta), 4)

    @computed_field
    @property
    def gross_delta(self) -> float:
        """Gross contract exposure."""
        return round(abs(self.kalshi_pos.delta) + abs(self.polymarket_pos.delta), 4)

    @computed_field
    @property
    def basis_spread(self) -> float:
        """Price difference: Kalshi mid - Polymarket mid."""
        return round(self.kalshi_pos.current_price - self.polymarket_pos.current_price, 4)

    @computed_field
    @property
    def total_unrealized_pnl(self) -> float:
        """Total unrealized PnL (USD + USDC assuming 1:1 parity)."""
        return round(self.kalshi_pos.unrealized_pnl + self.polymarket_pos.unrealized_pnl, 4)

    @computed_field
    @property
    def venue_exposures(self) -> Dict[str, float]:
        """Breakdown of currency exposure on each venue."""
        return {
            "kalshi_usd": self.kalshi_pos.notional_value,
            "polymarket_usdc": self.polymarket_pos.notional_value,
            "total_notional": round(self.kalshi_pos.notional_value + self.polymarket_pos.notional_value, 4),
        }


class FeeScheduleConfig(BaseModel):
    """Configuration for CFTC quadratic fees and Polygon gas budgets."""
    cftc_fee_coefficient: float = Field(default=0.07, description="Quadratic fee factor c in c * N * P * (1-P)")
    cftc_min_fee: float = Field(default=0.01, description="Minimum CFTC fee per transaction in USD")
    cftc_max_fee_per_contract: float = Field(default=0.07, description="Cap on CFTC fee per contract")
    gas_price_gwei: float = Field(default=30.0, description="Polygon gas price in Gwei")
    gas_limit_per_trade: int = Field(default=120_000, description="Gas units required for Polymarket transaction")
    pol_price_usd: float = Field(default=0.50, description="Price of POL/MATIC token in USD")
    polymarket_taker_fee_pct: float = Field(default=0.0, description="Polymarket taker fee percentage (0.0 to 1.0)")
    max_gas_drag_pct: float = Field(default=0.03, description="Maximum acceptable gas fee drag as % of notional (3%)")

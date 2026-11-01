"""Mathematical models for cross-venue prediction market delta hedging.

Implements:
1. Statistical delta exposure solver and hedge sizing.
2. Reciprocal outcome mapper (Kalshi YES -> Polymarket NO, Kalshi NO -> Polymarket YES).
3. Cross-venue basis calculation and statistical tracking error variance.
4. CFTC quadratic fee schedule (Kalshi regulatory fee model).
5. Polygon gas budget optimization and minimum viable batch size solver.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional, Tuple

from hedger.models import (
    DeltaRiskProfile,
    FeeScheduleConfig,
    HedgeOrder,
    HedgeStrategy,
    OrderSide,
    OrderStatus,
    Outcome,
    Venue,
)


class BasisRiskMetrics(NamedTuple):
    """Statistical basis risk and tracking error metrics."""
    basis_spread: float          # Kalshi price - Polymarket price
    dollar_basis_exposure: float # Spread * matched contract size
    basis_variance: float        # Variance of cross-venue basis
    tracking_error_std: float    # Volatility / standard deviation of basis
    hedge_ratio_beta: float      # Calibrated beta = rho * (sigma_K / sigma_PM)


def reciprocal_outcome(outcome: Outcome) -> Outcome:
    """Return the reciprocal binary outcome.
    
    In binary prediction markets:
    - Long Kalshi YES is delta-hedged by Long Polymarket NO.
    - Long Kalshi NO is delta-hedged by Long Polymarket YES.
    """
    return Outcome.NO if outcome == Outcome.YES else Outcome.YES


def determine_hedge_action(
    current_net_delta: float,
    target_venue: Venue = Venue.POLYMARKET,
    strategy: HedgeStrategy = HedgeStrategy.RECIPROCAL_BUY,
) -> Tuple[Outcome, OrderSide]:
    """Determine outcome and order side to neutralize net delta drift.

    Args:
        current_net_delta: Current portfolio net delta.
                           Positive means portfolio is long-biased (+delta).
                           Negative means portfolio is short-biased (-delta).
        target_venue: The venue on which the hedge leg will be placed.
        strategy: Hedging strategy (RECIPROCAL_BUY or DIRECT_SELL).

    Returns:
        Tuple of (Outcome, OrderSide) to execute on target_venue.
    """
    if current_net_delta > 0:
        # Portfolio is LONG (+delta). We need -delta to neutralize.
        if strategy == HedgeStrategy.RECIPROCAL_BUY:
            # Buying NO generates -1 delta
            return Outcome.NO, OrderSide.BUY
        else:
            # DIRECT_SELL: Selling/shorting YES generates -1 delta
            return Outcome.YES, OrderSide.SELL
    else:
        # Portfolio is SHORT (-delta). We need +delta to neutralize.
        if strategy == HedgeStrategy.RECIPROCAL_BUY:
            # Buying YES generates +1 delta
            return Outcome.YES, OrderSide.BUY
        else:
            # DIRECT_SELL: Selling/shorting NO generates +1 delta
            return Outcome.NO, OrderSide.SELL


def solve_required_hedge_contracts(
    current_net_delta: float,
    beta: float = 1.0,
    target_venue: Venue = Venue.POLYMARKET,
) -> float:
    """Solve for the required hedge contract quantity to achieve delta neutrality.

    Delta Neutrality Condition:
        Delta_Kalshi + beta * Delta_Polymarket = 0

    When hedging on Polymarket with hedge ratio beta:
        Contracts_PM = |current_net_delta| / beta
    When hedging on Kalshi:
        Contracts_K = |current_net_delta|

    Args:
        current_net_delta: Net portfolio delta.
        beta: Cross-venue hedge ratio (default 1.0).
        target_venue: Venue where the hedge order will be submitted.

    Returns:
        Exact contract quantity required (float >= 0).
    """
    if abs(current_net_delta) < 1e-9:
        return 0.0

    safe_beta = max(beta, 1e-4)
    if target_venue == Venue.POLYMARKET:
        required = abs(current_net_delta) / safe_beta
    else:
        required = abs(current_net_delta)

    return round(required, 4)


def calculate_basis_risk(
    kalshi_price: float,
    polymarket_price: float,
    sigma_kalshi: float = 0.05,
    sigma_polymarket: float = 0.05,
    correlation: float = 0.98,
    matched_contracts: float = 100.0,
) -> BasisRiskMetrics:
    """Compute cross-venue basis spread, variance, and tracking error.

    Formulation:
        Basis B = P_Kalshi - P_Polymarket
        Hedge Ratio beta = rho * (sigma_K / sigma_PM)
        Basis Variance Var(B) = sigma_K^2 + beta^2 * sigma_PM^2 - 2 * beta * rho * sigma_K * sigma_PM
        Tracking Error = sqrt(Var(B))

    Args:
        kalshi_price: Kalshi contract price in [0, 1].
        polymarket_price: Polymarket contract price in [0, 1].
        sigma_kalshi: Estimated volatility of Kalshi contract price.
        sigma_polymarket: Estimated volatility of Polymarket contract price.
        correlation: Correlation rho between Kalshi and Polymarket price moves.
        matched_contracts: Size of hedged paired contracts.

    Returns:
        BasisRiskMetrics tuple with calculated metrics.
    """
    spread = kalshi_price - polymarket_price
    safe_sigma_pm = max(sigma_polymarket, 1e-4)
    beta = correlation * (sigma_kalshi / safe_sigma_pm)
    
    # Quadratic basis variance
    var_basis = (
        (sigma_kalshi ** 2)
        + ((beta ** 2) * (sigma_polymarket ** 2))
        - (2.0 * beta * correlation * sigma_kalshi * sigma_polymarket)
    )
    # Ensure non-negative due to float rounding
    var_basis = max(var_basis, 0.0)
    tracking_error = math.sqrt(var_basis)
    dollar_basis = spread * matched_contracts

    return BasisRiskMetrics(
        basis_spread=round(spread, 4),
        dollar_basis_exposure=round(dollar_basis, 4),
        basis_variance=round(var_basis, 6),
        tracking_error_std=round(tracking_error, 6),
        hedge_ratio_beta=round(beta, 4),
    )


def calculate_cftc_quadratic_fee(
    contracts: float,
    price: float,
    config: Optional[FeeScheduleConfig] = None,
) -> float:
    """Calculate the Kalshi regulatory fee under the CFTC quadratic fee schedule.

    The CFTC quadratic fee model prices contract risk symmetrically around 50 cents,
    charging maximum fees when outcome uncertainty is highest (P = 0.50):
        Fee = ceil( 100 * c * N * P * (1 - P) ) / 100.0

    Subject to:
        - Minimum fee per fill (e.g. $0.01)
        - Maximum fee cap per contract (e.g. $0.07)

    Args:
        contracts: Number of contracts traded.
        price: Contract price in [0.0, 1.0].
        config: FeeScheduleConfig with c, min_fee, and max_fee_per_contract.

    Returns:
        Total fee in USD rounded to the next whole cent (ceiling).
    """
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0

    cfg = config or FeeScheduleConfig()
    c = cfg.cftc_fee_coefficient
    
    # Quadratic formula: c * N * P * (1 - P)
    raw_fee = c * contracts * price * (1.0 - price)
    
    # Enforce maximum cap per contract
    max_cap = contracts * cfg.cftc_max_fee_per_contract
    fee_capped = min(raw_fee, max_cap)
    
    # Enforce minimum fee per order
    fee_with_min = max(fee_capped, cfg.cftc_min_fee)
    
    # Round up to the nearest cent as per CFTC exchange clearing convention,
    # rounding to 6 decimal places first to eliminate floating point epsilon errors.
    cents = round(fee_with_min * 100.0, 6)
    return round(math.ceil(cents) / 100.0, 4)


def marginal_cftc_fee_rate(price: float, coefficient: float = 0.07) -> float:
    """Calculate the marginal CFTC fee per single contract at price P.
    
    Formula: c * P * (1 - P)
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return round(coefficient * price * (1.0 - price), 6)


def calculate_polygon_gas_cost_usd(
    gas_units: Optional[int] = None,
    gas_price_gwei: Optional[float] = None,
    pol_price_usd: Optional[float] = None,
    config: Optional[FeeScheduleConfig] = None,
) -> float:
    """Calculate Polygon transaction gas cost in USD.

    Formula:
        Gas Cost (POL) = Gas Units * (Gas Price Gwei * 10^-9)
        Gas Cost (USD) = Gas Cost (POL) * POL Price (USD)

    Args:
        gas_units: Number of gas units (e.g. 120,000).
        gas_price_gwei: Gas price in Gwei (e.g. 30 Gwei).
        pol_price_usd: Price of 1 POL token in USD.
        config: FeeScheduleConfig defaults.

    Returns:
        Gas cost in USD rounded to 6 decimal places.
    """
    cfg = config or FeeScheduleConfig()
    units = gas_units if gas_units is not None else cfg.gas_limit_per_trade
    gwei = gas_price_gwei if gas_price_gwei is not None else cfg.gas_price_gwei
    pol_price = pol_price_usd if pol_price_usd is not None else cfg.pol_price_usd

    pol_spent = units * (gwei * 1e-9)
    usd_spent = pol_spent * pol_price
    return round(usd_spent, 6)


def calculate_min_viable_hedge_size(
    price: float,
    config: Optional[FeeScheduleConfig] = None,
) -> float:
    """Determine minimum viable hedge size to prevent excessive gas drag.

    Constraint:
        Gas Cost (USD) / Notional Value <= max_gas_drag_pct
        Notional Value = N * P
        N_min = Gas Cost / (max_gas_drag_pct * P)

    Args:
        price: Prevailing contract price on Polymarket (0.01 - 0.99).
        config: FeeScheduleConfig with gas and drag constraints.

    Returns:
        Minimum contract quantity N_min.
    """
    if price <= 0.0:
        return 1.0

    cfg = config or FeeScheduleConfig()
    gas_usd = calculate_polygon_gas_cost_usd(config=cfg)
    target_notional = gas_usd / cfg.max_gas_drag_pct
    min_contracts = target_notional / price
    return max(1.0, round(min_contracts, 2))


def evaluate_hedge_feasibility(
    contracts: float,
    price: float,
    target_venue: Venue,
    config: Optional[FeeScheduleConfig] = None,
) -> Tuple[bool, dict]:
    """Evaluate whether an intended hedge passes fee and gas budget criteria.

    Args:
        contracts: Intended hedge quantity.
        price: Execution price estimate.
        target_venue: Venue where order will be placed.
        config: Fee schedule configuration.

    Returns:
        Tuple of (is_viable: bool, details: dict).
    """
    cfg = config or FeeScheduleConfig()
    notional = contracts * price
    cftc_fee = 0.0
    gas_cost = 0.0
    pm_fee = 0.0

    if target_venue == Venue.KALSHI:
        cftc_fee = calculate_cftc_quadratic_fee(contracts, price, cfg)
        total_cost = cftc_fee
    else:
        gas_cost = calculate_polygon_gas_cost_usd(config=cfg)
        pm_fee = notional * cfg.polymarket_taker_fee_pct
        total_cost = gas_cost + pm_fee

    cost_ratio = (total_cost / notional) if notional > 0 else float("inf")
    is_viable = cost_ratio <= cfg.max_gas_drag_pct

    details = {
        "notional_usd": round(notional, 4),
        "cftc_fee": cftc_fee,
        "gas_cost_usd": gas_cost,
        "polymarket_taker_fee": round(pm_fee, 4),
        "total_cost": round(total_cost, 4),
        "cost_ratio": round(cost_ratio, 4),
        "max_cost_ratio": cfg.max_gas_drag_pct,
        "is_viable": is_viable,
    }
    return is_viable, details


def build_risk_profile(
    pair_id: str,
    kalshi_delta: float,
    polymarket_delta: float,
    kalshi_price: float,
    polymarket_price: float,
    threshold: float = 25.0,
    beta: float = 1.0,
    correlation: float = 0.98,
    preferred_hedge_venue: Venue = Venue.POLYMARKET,
    fee_config: Optional[FeeScheduleConfig] = None,
) -> DeltaRiskProfile:
    """Analyze current exposure and construct a full DeltaRiskProfile.

    Args:
        pair_id: Identifier for the pair.
        kalshi_delta: Delta on Kalshi.
        polymarket_delta: Delta on Polymarket.
        kalshi_price: Latest Kalshi price.
        polymarket_price: Latest Polymarket price.
        threshold: Rebalance trigger threshold in contracts.
        beta: Hedge ratio.
        correlation: Price correlation between markets.
        preferred_hedge_venue: Venue to use for rebalancing.
        fee_config: Fee schedule configuration.

    Returns:
        DeltaRiskProfile with neutrality metrics and recommended order if needed.
    """
    net_delta = round(kalshi_delta + (beta * polymarket_delta), 4)
    gross_delta = round(abs(kalshi_delta) + abs(polymarket_delta), 4)
    drift = abs(net_delta)
    rebalance_required = drift >= threshold

    basis_metrics = calculate_basis_risk(
        kalshi_price=kalshi_price,
        polymarket_price=polymarket_price,
        correlation=correlation,
        matched_contracts=min(abs(kalshi_delta), abs(polymarket_delta)),
    )

    notional_delta = round(drift * ((kalshi_price + polymarket_price) / 2.0), 4)

    recommended_order: Optional[HedgeOrder] = None
    recommended_action: Optional[str] = None

    if rebalance_required:
        outcome, side = determine_hedge_action(
            current_net_delta=net_delta,
            target_venue=preferred_hedge_venue,
            strategy=HedgeStrategy.RECIPROCAL_BUY,
        )
        required_contracts = solve_required_hedge_contracts(
            current_net_delta=net_delta,
            beta=beta,
            target_venue=preferred_hedge_venue,
        )

        hedge_price = polymarket_price if preferred_hedge_venue == Venue.POLYMARKET else kalshi_price
        # Adjust limit price based on side
        limit_price = min(0.99, max(0.01, round(hedge_price + (0.01 if side == OrderSide.BUY else -0.01), 2)))

        cftc_fee = calculate_cftc_quadratic_fee(required_contracts, limit_price, fee_config) if preferred_hedge_venue == Venue.KALSHI else 0.0
        gas_cost = calculate_polygon_gas_cost_usd(config=fee_config) if preferred_hedge_venue == Venue.POLYMARKET else 0.0

        recommended_order = HedgeOrder(
            order_id=f"hedge_{int(basis_metrics.basis_variance * 1e6)}_{int(drift)}",
            pair_id=pair_id,
            target_venue=preferred_hedge_venue,
            outcome=outcome,
            side=side,
            contracts=required_contracts,
            limit_price=limit_price,
            estimated_cftc_fee=cftc_fee,
            estimated_gas_cost=gas_cost,
            estimated_total_cost=round(cftc_fee + gas_cost, 4),
            reason=f"Delta drift {drift:.2f} >= threshold {threshold:.2f}",
            status=OrderStatus.PENDING,
        )
        recommended_action = f"{side.value.upper()} {required_contracts:.0f} {outcome.value.upper()} on {preferred_hedge_venue.value.capitalize()}"
    else:
        recommended_action = "Portfolio delta within neutral band"

    return DeltaRiskProfile(
        pair_id=pair_id,
        gross_delta=gross_delta,
        net_delta=net_delta,
        notional_delta_usd=notional_delta,
        basis=basis_metrics.basis_spread,
        basis_risk_var=basis_metrics.basis_variance,
        drift_magnitude=drift,
        threshold=threshold,
        rebalance_required=rebalance_required,
        recommended_action=recommended_action,
        recommended_order=recommended_order,
    )

"""Unit tests for mathematical models and delta solvers."""

import unittest
from hedger.math_hedger import (
    build_risk_profile,
    calculate_basis_risk,
    calculate_cftc_quadratic_fee,
    calculate_min_viable_hedge_size,
    calculate_polygon_gas_cost_usd,
    determine_hedge_action,
    evaluate_hedge_feasibility,
    marginal_cftc_fee_rate,
    reciprocal_outcome,
    solve_required_hedge_contracts,
)
from hedger.models import (
    FeeScheduleConfig,
    HedgeStrategy,
    OrderSide,
    Outcome,
    Venue,
)


class TestMathHedger(unittest.TestCase):
    """Test quantitative delta hedging functions."""

    def test_reciprocal_outcome(self):
        self.assertEqual(reciprocal_outcome(Outcome.YES), Outcome.NO)
        self.assertEqual(reciprocal_outcome(Outcome.NO), Outcome.YES)

    def test_determine_hedge_action(self):
        # Current delta > 0 (long bias) -> Need negative delta
        # Strategy RECIPROCAL_BUY on Polymarket -> BUY NO
        outcome, side = determine_hedge_action(100.0, Venue.POLYMARKET, HedgeStrategy.RECIPROCAL_BUY)
        self.assertEqual(outcome, Outcome.NO)
        self.assertEqual(side, OrderSide.BUY)

        # Strategy DIRECT_SELL on Polymarket -> SELL YES
        outcome, side = determine_hedge_action(100.0, Venue.POLYMARKET, HedgeStrategy.DIRECT_SELL)
        self.assertEqual(outcome, Outcome.YES)
        self.assertEqual(side, OrderSide.SELL)

        # Current delta < 0 (short bias) -> Need positive delta
        # Strategy RECIPROCAL_BUY on Polymarket -> BUY YES
        outcome, side = determine_hedge_action(-100.0, Venue.POLYMARKET, HedgeStrategy.RECIPROCAL_BUY)
        self.assertEqual(outcome, Outcome.YES)
        self.assertEqual(side, OrderSide.BUY)

        # Strategy DIRECT_SELL on Polymarket -> SELL NO
        outcome, side = determine_hedge_action(-100.0, Venue.POLYMARKET, HedgeStrategy.DIRECT_SELL)
        self.assertEqual(outcome, Outcome.NO)
        self.assertEqual(side, OrderSide.SELL)

    def test_solve_required_hedge_contracts(self):
        # Zero delta -> 0 required
        self.assertEqual(solve_required_hedge_contracts(0.0), 0.0)

        # Delta = +50, beta = 1.0 -> 50.0 contracts
        self.assertEqual(solve_required_hedge_contracts(50.0, beta=1.0), 50.0)

        # Delta = +50, beta = 0.5 -> 50 / 0.5 = 100 contracts on Polymarket
        self.assertEqual(solve_required_hedge_contracts(50.0, beta=0.5, target_venue=Venue.POLYMARKET), 100.0)

        # Delta = -75, beta = 1.5 -> 75 / 1.5 = 50 contracts on Polymarket
        self.assertEqual(solve_required_hedge_contracts(-75.0, beta=1.5, target_venue=Venue.POLYMARKET), 50.0)

        # Target venue Kalshi: beta not applied
        self.assertEqual(solve_required_hedge_contracts(50.0, beta=0.5, target_venue=Venue.KALSHI), 50.0)

    def test_calculate_basis_risk(self):
        # Perfect correlation, equal volatilities
        metrics = calculate_basis_risk(
            kalshi_price=0.62,
            polymarket_price=0.60,
            sigma_kalshi=0.04,
            sigma_polymarket=0.04,
            correlation=1.0,
            matched_contracts=100.0,
        )
        self.assertAlmostEqual(metrics.basis_spread, 0.02)
        self.assertAlmostEqual(metrics.dollar_basis_exposure, 2.0)
        self.assertAlmostEqual(metrics.hedge_ratio_beta, 1.0)
        self.assertAlmostEqual(metrics.basis_variance, 0.0, places=5)
        self.assertAlmostEqual(metrics.tracking_error_std, 0.0, places=4)

        # Imperfect correlation
        metrics2 = calculate_basis_risk(
            kalshi_price=0.50,
            polymarket_price=0.48,
            sigma_kalshi=0.05,
            sigma_polymarket=0.05,
            correlation=0.95,
            matched_contracts=100.0,
        )
        self.assertGreater(metrics2.basis_variance, 0.0)
        self.assertGreater(metrics2.tracking_error_std, 0.0)

    def test_cftc_quadratic_fee(self):
        cfg = FeeScheduleConfig(cftc_fee_coefficient=0.07, cftc_min_fee=0.01)

        # At P=0.50, fee = 0.07 * 100 * 0.5 * 0.5 = 1.75
        fee_50 = calculate_cftc_quadratic_fee(100, 0.50, cfg)
        self.assertEqual(fee_50, 1.75)

        # At P=0.10, fee = 0.07 * 100 * 0.1 * 0.9 = 0.63
        fee_10 = calculate_cftc_quadratic_fee(100, 0.10, cfg)
        self.assertEqual(fee_10, 0.63)

        # Symmetry: P=0.90 should equal P=0.10: 0.07 * 100 * 0.9 * 0.1 = 0.63
        fee_90 = calculate_cftc_quadratic_fee(100, 0.90, cfg)
        self.assertEqual(fee_90, 0.63)

        # Extreme prices (0.00 or 1.00) -> 0 fee
        self.assertEqual(calculate_cftc_quadratic_fee(100, 0.0, cfg), 0.0)
        self.assertEqual(calculate_cftc_quadratic_fee(100, 1.0, cfg), 0.0)

        # Minimum fee enforcement on small order
        fee_small = calculate_cftc_quadratic_fee(1, 0.50, cfg)
        # raw: 0.07 * 1 * 0.25 = 0.0175 -> ceil to 0.02
        self.assertGreaterEqual(fee_small, cfg.cftc_min_fee)

    def test_marginal_cftc_fee_rate(self):
        rate = marginal_cftc_fee_rate(0.50, 0.07)
        self.assertEqual(rate, 0.0175)

    def test_polygon_gas_cost(self):
        # 120,000 gas * 30 gwei * 10^-9 = 0.0036 POL * $0.50 = $0.0018
        gas_usd = calculate_polygon_gas_cost_usd(
            gas_units=120_000,
            gas_price_gwei=30.0,
            pol_price_usd=0.50,
        )
        self.assertEqual(gas_usd, 0.0018)

    def test_min_viable_hedge_size_and_feasibility(self):
        cfg = FeeScheduleConfig(
            gas_price_gwei=30.0,
            gas_limit_per_trade=120_000,
            pol_price_usd=0.50,
            max_gas_drag_pct=0.03,  # 3%
        )
        # Gas cost = $0.0018
        # Target notional = $0.0018 / 0.03 = $0.06
        # At P=0.60, min contracts = 0.06 / 0.60 = 0.1 -> clamped to 1.0
        min_size = calculate_min_viable_hedge_size(0.60, cfg)
        self.assertGreaterEqual(min_size, 1.0)

        # For 50 contracts at $0.60 ($30 notional), gas cost $0.0018 is 0.006% << 3% -> viable
        is_viable, details = evaluate_hedge_feasibility(50.0, 0.60, Venue.POLYMARKET, cfg)
        self.assertTrue(is_viable)
        self.assertLess(details["cost_ratio"], cfg.max_gas_drag_pct)

    def test_build_risk_profile(self):
        # Drift = 10 < threshold 25 -> no rebalance required
        prof1 = build_risk_profile(
            pair_id="FED-MAY-CUT",
            kalshi_delta=10.0,
            polymarket_delta=0.0,
            kalshi_price=0.62,
            polymarket_price=0.61,
            threshold=25.0,
        )
        self.assertFalse(prof1.rebalance_required)
        self.assertIsNone(prof1.recommended_order)

        # Drift = 60 >= threshold 25 -> rebalance required
        prof2 = build_risk_profile(
            pair_id="FED-MAY-CUT",
            kalshi_delta=60.0,
            polymarket_delta=0.0,
            kalshi_price=0.62,
            polymarket_price=0.61,
            threshold=25.0,
            preferred_hedge_venue=Venue.POLYMARKET,
        )
        self.assertTrue(prof2.rebalance_required)
        self.assertIsNotNone(prof2.recommended_order)
        self.assertEqual(prof2.recommended_order.contracts, 60.0)
        self.assertEqual(prof2.recommended_order.target_venue, Venue.POLYMARKET)
        self.assertEqual(prof2.recommended_order.outcome, Outcome.NO)
        self.assertEqual(prof2.recommended_order.side, OrderSide.BUY)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the rebalancer and execution engine."""

import unittest
from hedger.client import NaxosCrossVenueClient
from hedger.models import (
    FeeScheduleConfig,
    HedgeOrder,
    HedgeStrategy,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    Outcome,
    RebalanceStatus,
    Venue,
)
from hedger.rebalancer import HedgingRebalancer, simulate_orderbook_fill


class TestRebalancer(unittest.TestCase):
    """Test rebalancing loop, drift thresholds, and fill simulations."""

    def setUp(self):
        self.client = NaxosCrossVenueClient(offline=True, seed=777)
        self.pair = self.client.get_pairs()[0]

    def test_simulate_orderbook_fill(self):
        bids = [
            OrderBookLevel(price=0.60, size=50),
            OrderBookLevel(price=0.59, size=100),
        ]
        asks = [
            OrderBookLevel(price=0.62, size=50),
            OrderBookLevel(price=0.63, size=100),
        ]
        book = OrderBookSnapshot(
            venue=Venue.POLYMARKET,
            market_id="PM-01",
            bids=bids,
            asks=asks,
        )

        # Buy 50 contracts: should fill entirely at best ask 0.62
        filled, vwap, slippage = simulate_orderbook_fill(book, OrderSide.BUY, 50.0)
        self.assertEqual(filled, 50.0)
        self.assertEqual(vwap, 0.62)
        self.assertEqual(slippage, 0.0)

        # Buy 100 contracts: fills 50 at 0.62 and 50 at 0.63 -> vwap = 0.625
        filled2, vwap2, slippage2 = simulate_orderbook_fill(book, OrderSide.BUY, 100.0)
        self.assertEqual(filled2, 100.0)
        self.assertEqual(vwap2, 0.625)
        self.assertGreater(slippage2, 0.0)

    def test_rebalancer_drift_trigger(self):
        # Initial Kalshi delta +80, Polymarket 0, threshold 25
        rebalancer = HedgingRebalancer(
            client=self.client,
            pair=self.pair,
            delta_threshold=25.0,
            initial_kalshi_contracts=80.0,
            initial_kalshi_price=0.60,
            initial_polymarket_contracts=0.0,
            initial_polymarket_price=0.60,
        )
        self.assertEqual(rebalancer.cross_position.net_delta, 80.0)

        # Step 1: drift 80 >= 25 -> executes hedge on Polymarket (reciprocal buy NO)
        profile, execution = rebalancer.step()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.venue, Venue.POLYMARKET)
        self.assertEqual(execution.outcome, Outcome.NO)
        self.assertEqual(execution.side, OrderSide.BUY)
        self.assertEqual(execution.filled_contracts, 80.0)

        # After execution, net delta drops to ~0
        self.assertAlmostEqual(rebalancer.cross_position.net_delta, 0.0, places=1)
        self.assertEqual(rebalancer.last_status, RebalanceStatus.REBALANCED)

        # Step 2: delta is now balanced -> no order executed
        profile2, execution2 = rebalancer.step()
        self.assertIsNone(execution2)
        self.assertEqual(rebalancer.last_status, RebalanceStatus.BALANCED)

    def test_rebalancer_negative_delta_trigger(self):
        # Kalshi has -60 contracts (short YES), threshold 20
        rebalancer = HedgingRebalancer(
            client=self.client,
            pair=self.pair,
            delta_threshold=20.0,
            initial_kalshi_contracts=-60.0,
            initial_kalshi_price=0.55,
        )
        self.assertEqual(rebalancer.cross_position.net_delta, -60.0)

        # Need +delta -> reciprocal buy YES on Polymarket
        profile, execution = rebalancer.step()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.outcome, Outcome.YES)
        self.assertEqual(execution.side, OrderSide.BUY)
        self.assertEqual(execution.filled_contracts, 60.0)
        self.assertAlmostEqual(rebalancer.cross_position.net_delta, 0.0, places=1)

    def test_rebalancer_fee_deductions_and_pnl(self):
        rebalancer = HedgingRebalancer(
            client=self.client,
            pair=self.pair,
            delta_threshold=20.0,
            initial_kalshi_contracts=50.0,
            initial_kalshi_price=0.50,
            initial_polymarket_usdc=5000.0,
        )
        profile, execution = rebalancer.step()
        self.assertIsNotNone(execution)
        self.assertGreater(rebalancer.total_gas_fees_paid, 0.0)
        self.assertLess(rebalancer.polymarket_usdc_balance, 5000.0)

        summary = rebalancer.get_portfolio_summary()
        self.assertEqual(summary["total_hedges_executed"], 1)
        self.assertIn("net_pnl_after_fees", summary)

    def test_continuous_rebalancer_max_iterations(self):
        rebalancer = HedgingRebalancer(
            client=self.client,
            pair=self.pair,
            delta_threshold=30.0,
            initial_kalshi_contracts=10.0,  # below threshold
        )
        ticks = []
        rebalancer.run_continuous(
            interval_seconds=0.01,
            max_iterations=3,
            callback=lambda prof, ex: ticks.append(prof),
        )
        self.assertEqual(len(ticks), 3)


if __name__ == "__main__":
    unittest.main()

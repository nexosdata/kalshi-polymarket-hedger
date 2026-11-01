"""Unit tests for data models."""

import unittest
from hedger.models import (
    CrossVenuePosition,
    DeltaRiskProfile,
    HedgeOrder,
    LegExecution,
    MarketPair,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    OrderStatus,
    Outcome,
    SingleVenuePosition,
    Venue,
)


class TestModels(unittest.TestCase):
    """Test data models and computed properties."""

    def test_single_venue_position_delta(self):
        # Long YES: delta = +1 per contract
        pos1 = SingleVenuePosition(
            venue=Venue.KALSHI,
            market_id="KX-TEST",
            outcome=Outcome.YES,
            contracts=100.0,
            avg_entry_price=0.60,
            current_price=0.65,
        )
        self.assertEqual(pos1.delta, 100.0)
        self.assertAlmostEqual(pos1.notional_value, 65.0)
        self.assertAlmostEqual(pos1.cost_basis, 60.0)
        self.assertAlmostEqual(pos1.unrealized_pnl, 5.0)

        # Long NO: delta = -1 per contract
        pos2 = SingleVenuePosition(
            venue=Venue.POLYMARKET,
            market_id="PM-TEST",
            outcome=Outcome.NO,
            contracts=100.0,
            avg_entry_price=0.40,
            current_price=0.35,
        )
        self.assertEqual(pos2.delta, -100.0)
        self.assertAlmostEqual(pos2.unrealized_pnl, -5.0)

        # Short YES: delta = -1 per contract
        pos3 = SingleVenuePosition(
            venue=Venue.KALSHI,
            market_id="KX-TEST",
            outcome=Outcome.YES,
            contracts=-50.0,
            avg_entry_price=0.50,
            current_price=0.40,
        )
        self.assertEqual(pos3.delta, -50.0)
        self.assertAlmostEqual(pos3.unrealized_pnl, 5.0)  # Sold at 0.50, now 0.40

    def test_cross_venue_position_net_delta(self):
        # Long 100 Kalshi YES (+100 delta), Long 100 Polymarket NO (-100 delta) -> Net Delta 0
        k_pos = SingleVenuePosition(
            venue=Venue.KALSHI,
            market_id="KX-TEST",
            outcome=Outcome.YES,
            contracts=100.0,
            avg_entry_price=0.62,
            current_price=0.62,
        )
        pm_pos = SingleVenuePosition(
            venue=Venue.POLYMARKET,
            market_id="PM-TEST",
            outcome=Outcome.NO,
            contracts=100.0,
            avg_entry_price=0.38,
            current_price=0.38,
        )
        cross = CrossVenuePosition(
            pair_id="TEST-PAIR",
            kalshi_pos=k_pos,
            polymarket_pos=pm_pos,
            correlation=1.0,
            beta=1.0,
        )
        self.assertEqual(cross.net_delta, 0.0)
        self.assertEqual(cross.gross_delta, 200.0)
        self.assertEqual(cross.venue_exposures["kalshi_usd"], 62.0)
        self.assertEqual(cross.venue_exposures["polymarket_usdc"], 38.0)

    def test_cross_venue_position_with_beta(self):
        # Long 100 Kalshi YES (+100 delta), Short 80 Polymarket YES (-80 delta), beta=1.25
        # Net delta: 100 + 1.25 * (-80) = 100 - 100 = 0.0
        k_pos = SingleVenuePosition(
            venue=Venue.KALSHI,
            market_id="KX-TEST",
            outcome=Outcome.YES,
            contracts=100.0,
            avg_entry_price=0.50,
            current_price=0.50,
        )
        pm_pos = SingleVenuePosition(
            venue=Venue.POLYMARKET,
            market_id="PM-TEST",
            outcome=Outcome.YES,
            contracts=-80.0,
            avg_entry_price=0.50,
            current_price=0.50,
        )
        cross = CrossVenuePosition(
            pair_id="TEST-BETA",
            kalshi_pos=k_pos,
            polymarket_pos=pm_pos,
            beta=1.25,
        )
        self.assertAlmostEqual(cross.net_delta, 0.0)

    def test_orderbook_snapshot_properties(self):
        bids = [OrderBookLevel(price=0.60, size=100), OrderBookLevel(price=0.59, size=200)]
        asks = [OrderBookLevel(price=0.62, size=150), OrderBookLevel(price=0.63, size=250)]
        book = OrderBookSnapshot(
            venue=Venue.KALSHI,
            market_id="KX-FED",
            bids=bids,
            asks=asks,
        )
        self.assertEqual(book.best_bid, 0.60)
        self.assertEqual(book.best_ask, 0.62)
        self.assertEqual(book.mid_price, 0.61)
        self.assertEqual(book.spread, 0.02)

    def test_hedge_order_and_execution_serialization(self):
        order = HedgeOrder(
            order_id="ord-001",
            pair_id="FED-MAY-2026-CUT",
            target_venue=Venue.POLYMARKET,
            outcome=Outcome.NO,
            side=OrderSide.BUY,
            contracts=100.0,
            limit_price=0.38,
            estimated_gas_cost=0.002,
            reason="Delta drift test",
        )
        self.assertEqual(order.status, OrderStatus.PENDING)

        execution = LegExecution(
            execution_id="exec-001",
            order_id="ord-001",
            venue=Venue.POLYMARKET,
            outcome=Outcome.NO,
            side=OrderSide.BUY,
            filled_contracts=100.0,
            fill_price=0.38,
            fee_paid=0.0,
            gas_paid_usd=0.0018,
            tx_hash_or_fill_id="0xabc123",
        )
        order.execution = execution
        order.status = OrderStatus.FILLED
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.execution.tx_hash_or_fill_id, "0xabc123")


if __name__ == "__main__":
    unittest.main()

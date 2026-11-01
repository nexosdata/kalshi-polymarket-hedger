"""Unit tests for NaxosData feed client and mock generator."""

import unittest
from hedger.client import MockOrderBookGenerator, NaxosCrossVenueClient
from hedger.models import MarketPair, Outcome, Venue


class TestClient(unittest.TestCase):
    """Test client requests and mock book generator."""

    def test_mock_orderbook_generator(self):
        gen = MockOrderBookGenerator(seed=42)
        snapshot = gen.generate_snapshot(
            venue=Venue.KALSHI,
            market_id="KXFED-26MAY-C",
            outcome=Outcome.YES,
            depth_levels=5,
        )
        self.assertEqual(snapshot.venue, Venue.KALSHI)
        self.assertEqual(len(snapshot.bids), 5)
        self.assertEqual(len(snapshot.asks), 5)
        self.assertGreater(snapshot.best_ask, snapshot.best_bid)
        self.assertGreater(snapshot.spread, 0.0)

        # Check descending bids and ascending asks
        bids = [b.price for b in snapshot.bids]
        asks = [a.price for a in snapshot.asks]
        self.assertEqual(bids, sorted(bids, reverse=True))
        self.assertEqual(asks, sorted(asks))

    def test_client_offline_benchmark_pairs(self):
        client = NaxosCrossVenueClient(offline=True, seed=10)
        pairs = client.get_pairs()
        self.assertGreaterEqual(len(pairs), 3)

        # Lookup by pair_id
        found = client.get_market_pair("FED-MAY-2026-CUT")
        self.assertIsNotNone(found)
        self.assertEqual(found.kalshi_market_id, "KXFED-26MAY-C")

        # Unknown pair returns None
        not_found = client.get_market_pair("NONEXISTENT-PAIR")
        self.assertIsNone(not_found)

    def test_client_pair_snapshots(self):
        client = NaxosCrossVenueClient(offline=True, seed=100)
        pair = client.get_pairs()[0]
        k_book, pm_book = client.get_pair_snapshots(pair)

        self.assertEqual(k_book.venue, Venue.KALSHI)
        self.assertEqual(pm_book.venue, Venue.POLYMARKET)
        self.assertIsNotNone(k_book.mid_price)
        self.assertIsNotNone(pm_book.mid_price)
        self.assertGreater(k_book.mid_price, 0.0)
        self.assertGreater(pm_book.mid_price, 0.0)

    def test_client_context_manager(self):
        with NaxosCrossVenueClient(offline=True) as client:
            pairs = client.get_pairs()
            self.assertGreater(len(pairs), 0)

    def test_client_live_api_mocked_response(self):
        """Test API JSON parsing when live API returns real format."""
        import httpx

        def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/api/v1/pairs" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "pairs": [
                            {
                                "pair_id": "TEST-LIVE-PAIR",
                                "title": "Test Live Market Pair",
                                "kalshi_market_id": "KX-LIVE",
                                "polymarket_market_id": "0xlive123",
                                "correlation": 0.99,
                            }
                        ]
                    },
                )
            elif "/api/v1/orderbooks" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "venue": "kalshi",
                        "market_id": "KX-LIVE",
                        "bids": [[0.55, 200.0], [0.54, 400.0]],
                        "asks": [[0.57, 150.0], [0.58, 300.0]],
                        "timestamp": 1779926400.0,
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        client = NaxosCrossVenueClient(
            base_url="https://mock.naxosdata.com",
            offline=False,
            fallback_to_mock=False,
        )
        client._http = httpx.Client(transport=transport)

        pairs = client.get_pairs()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].pair_id, "TEST-LIVE-PAIR")

        book = client.get_orderbook(Venue.KALSHI, "KX-LIVE")
        self.assertEqual(book.best_bid, 0.55)
        self.assertEqual(book.best_ask, 0.57)
        self.assertEqual(book.mid_price, 0.56)
        self.assertEqual(book.spread, 0.02)
        client.close()

    def test_client_api_error_fallback(self):
        """Test graceful fallback to mock generator when API throws network error."""
        import httpx

        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Network connection refused", request=request)

        transport = httpx.MockTransport(failing_handler)
        client = NaxosCrossVenueClient(
            base_url="https://offline.naxosdata.com",
            offline=False,
            fallback_to_mock=True,
        )
        client._http = httpx.Client(transport=transport)

        # Should fall back to mock without raising
        pairs = client.get_pairs()
        self.assertGreaterEqual(len(pairs), 3)

        book = client.get_orderbook(Venue.KALSHI, "KXFED-26MAY-C")
        self.assertIsNotNone(book.best_bid)
        self.assertIsNotNone(book.best_ask)
        client.close()


if __name__ == "__main__":
    unittest.main()

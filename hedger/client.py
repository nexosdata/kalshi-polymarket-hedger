"""Cross-venue synchronized market feed client for NaxosData API.

Connects to institutional prediction market endpoints:
- GET /api/v1/pairs
- GET /api/v1/orderbooks/{venue}/{marketId}/snapshot

Includes a high-fidelity offline mock generator for paper trading, backtesting,
and simulated order flow with realistic liquidity, spreads, and random walk drift.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Dict, List, Optional, Tuple
import httpx

from hedger.models import (
    MarketPair,
    OrderBookLevel,
    OrderBookSnapshot,
    Outcome,
    Venue,
)

logger = logging.getLogger(__name__)


# Standard curated cross-venue benchmark pairs
DEFAULT_BENCHMARK_PAIRS: List[MarketPair] = [
    MarketPair(
        pair_id="FED-MAY-2026-CUT",
        title="Fed Interest Rate Cut in May 2026",
        kalshi_market_id="KXFED-26MAY-C",
        polymarket_market_id="0x712a8f93e1b046a2977b3df63071b78292f7d981",
        polymarket_slug="will-fed-cut-rates-in-may-2026",
        correlation=0.992,
        expiry_timestamp=1779926400.0,
    ),
    MarketPair(
        pair_id="CPI-APR-2026-CORE",
        title="US Core CPI YoY >= 2.8% for April 2026",
        kalshi_market_id="KXCPI-26APR-28",
        polymarket_market_id="0x981c2d4310a08e6720bf2f28b753c129e1fa0b55",
        polymarket_slug="us-core-cpi-yoy-above-28-april-2026",
        correlation=0.985,
        expiry_timestamp=1778716800.0,
    ),
    MarketPair(
        pair_id="BTC-100K-Q2-2026",
        title="Bitcoin above $100k by end of Q2 2026",
        kalshi_market_id="KXBTC-26JUN-100",
        polymarket_market_id="0x546e10bd7184a3e74a81b2c4d6189efbc9842a10",
        polymarket_slug="bitcoin-above-100k-by-end-of-q2-2026",
        correlation=0.978,
        expiry_timestamp=1782864000.0,
    ),
]


class MockOrderBookGenerator:
    """High-fidelity simulated order book generator.
    
    Generates realistic bid-ask order books with realistic spreads, depth,
    and slight random walk price movements for paper trading.
    """

    def __init__(self, seed: Optional[int] = None) -> None:
        self._rng = random.Random(seed)
        # Store persistent mid prices per (venue, market_id)
        self._mid_prices: Dict[Tuple[str, str], float] = {
            (Venue.KALSHI.value, "KXFED-26MAY-C"): 0.62,
            (Venue.POLYMARKET.value, "0x712a8f93e1b046a2977b3df63071b78292f7d981"): 0.615,
            (Venue.KALSHI.value, "KXCPI-26APR-28"): 0.44,
            (Venue.POLYMARKET.value, "0x981c2d4310a08e6720bf2f28b753c129e1fa0b55"): 0.445,
            (Venue.KALSHI.value, "KXBTC-26JUN-100"): 0.73,
            (Venue.POLYMARKET.value, "0x546e10bd7184a3e74a81b2c4d6189efbc9842a10"): 0.725,
        }

    def generate_snapshot(
        self,
        venue: Venue,
        market_id: str,
        outcome: Outcome = Outcome.YES,
        depth_levels: int = 5,
        drift: bool = True,
    ) -> OrderBookSnapshot:
        """Generate a realistic synthetic order book snapshot."""
        key = (venue.value, market_id)
        current_mid = self._mid_prices.get(key, 0.50)

        if drift:
            # Random walk drift bounded in [0.08, 0.92]
            step = self._rng.uniform(-0.008, 0.008)
            current_mid = min(0.92, max(0.08, current_mid + step))
            self._mid_prices[key] = current_mid

        # Venue-specific ticks and half-spreads
        if venue == Venue.KALSHI:
            # Kalshi ticks at 1 cent ($0.01)
            half_spread = 0.01
            tick_size = 0.01
        else:
            # Polymarket ticks at 0.1 or 0.5 cents ($0.001 - $0.005)
            half_spread = 0.005
            tick_size = 0.005

        best_bid = round(max(0.01, current_mid - half_spread), 3)
        best_ask = round(min(0.99, current_mid + half_spread), 3)
        if best_bid >= best_ask:
            best_ask = round(best_bid + tick_size, 3)

        bids: List[OrderBookLevel] = []
        asks: List[OrderBookLevel] = []

        # Generate bids descending
        for level in range(depth_levels):
            level_price = round(best_bid - (level * tick_size), 3)
            if level_price <= 0.001:
                break
            # Larger liquidity further out from the mid
            base_size = self._rng.randint(80, 500) * (level + 1)
            bids.append(OrderBookLevel(price=level_price, size=float(base_size)))

        # Generate asks ascending
        for level in range(depth_levels):
            level_price = round(best_ask + (level * tick_size), 3)
            if level_price >= 0.999:
                break
            base_size = self._rng.randint(80, 500) * (level + 1)
            asks.append(OrderBookLevel(price=level_price, size=float(base_size)))

        return OrderBookSnapshot(
            venue=venue,
            market_id=market_id,
            outcome=outcome,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )


class NaxosCrossVenueClient:
    """Client for fetching cross-venue prediction market orderbooks and pairs.

    Connects to NaxosData API:
        GET /api/v1/pairs
        GET /api/v1/orderbooks/{venue}/{marketId}/snapshot

    Features:
        - Transparent fallback to realistic offline simulation if offline=True or offline network.
        - Synchronized dual-venue snapshot fetcher.
    """

    def __init__(
        self,
        base_url: str = "https://api.naxosdata.com",
        api_key: Optional[str] = None,
        timeout_seconds: float = 8.0,
        offline: bool = False,
        fallback_to_mock: bool = True,
        seed: Optional[int] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.offline = offline
        self.fallback_to_mock = fallback_to_mock
        self._mock = MockOrderBookGenerator(seed=seed)
        self._http = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "User-Agent": "NaxosData-Kalshi-Polymarket-Hedger/1.0.0",
                "Accept": "application/json",
                **({"X-API-Key": api_key} if api_key else {}),
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "NaxosCrossVenueClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def get_pairs(self) -> List[MarketPair]:
        """Fetch active cross-venue prediction pairs."""
        if self.offline:
            return list(DEFAULT_BENCHMARK_PAIRS)

        url = f"{self.base_url}/api/v1/pairs"
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
            data = resp.json()
            raw_pairs = data.get("pairs", data) if isinstance(data, dict) else data
            return [MarketPair.model_validate(p) for p in raw_pairs]
        except Exception as e:
            if self.fallback_to_mock:
                logger.warning("NaxosData API /api/v1/pairs unavailable (%s). Using benchmark pairs.", e)
                return list(DEFAULT_BENCHMARK_PAIRS)
            raise

    def get_market_pair(self, pair_id: str) -> Optional[MarketPair]:
        """Find a market pair by its unique pair ID."""
        pairs = self.get_pairs()
        for p in pairs:
            if p.pair_id.lower() == pair_id.lower():
                return p
        return None

    def get_orderbook(
        self,
        venue: Venue,
        market_id: str,
        outcome: Outcome = Outcome.YES,
    ) -> OrderBookSnapshot:
        """Fetch current orderbook snapshot for a venue contract."""
        if self.offline:
            return self._mock.generate_snapshot(venue, market_id, outcome)

        url = f"{self.base_url}/api/v1/orderbooks/{venue.value}/{market_id}/snapshot"
        try:
            resp = self._http.get(url, params={"outcome": outcome.value})
            resp.raise_for_status()
            data = resp.json()
            
            # Map API response format to OrderBookSnapshot
            raw_bids = data.get("bids", [])
            raw_asks = data.get("asks", [])
            
            bids = [
                OrderBookLevel(
                    price=float(b[0] if isinstance(b, (list, tuple)) else b["price"]),
                    size=float(b[1] if isinstance(b, (list, tuple)) else b["size"]),
                )
                for b in raw_bids
            ]
            asks = [
                OrderBookLevel(
                    price=float(a[0] if isinstance(a, (list, tuple)) else a["price"]),
                    size=float(a[1] if isinstance(a, (list, tuple)) else a["size"]),
                )
                for a in raw_asks
            ]
            return OrderBookSnapshot(
                venue=venue,
                market_id=market_id,
                outcome=outcome,
                bids=bids,
                asks=asks,
                timestamp=float(data.get("timestamp", time.time())),
            )
        except Exception as e:
            if self.fallback_to_mock:
                logger.warning("NaxosData API orderbook snapshot unavailable (%s). Using mock book.", e)
                return self._mock.generate_snapshot(venue, market_id, outcome)
            raise

    def get_pair_snapshots(
        self,
        pair: MarketPair,
    ) -> Tuple[OrderBookSnapshot, OrderBookSnapshot]:
        """Fetch synchronized orderbooks for both venues in a pair simultaneously.

        Returns:
            Tuple of (Kalshi snapshot, Polymarket snapshot).
        """
        kalshi_book = self.get_orderbook(Venue.KALSHI, pair.kalshi_market_id, Outcome.YES)
        polymarket_book = self.get_orderbook(Venue.POLYMARKET, pair.polymarket_market_id, Outcome.YES)
        return kalshi_book, polymarket_book

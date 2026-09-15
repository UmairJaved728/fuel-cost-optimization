"""Comprehensive tests for the fuel optimizer.

The optimizer is pure business logic that depends on NO Django / DB / network.
All test data is constructed in-place.
"""

import random
from decimal import Decimal

import pytest

from routes.services.fuel_optimizer import NoFeasiblePlan, optimize


def _make_station(pos: float, price: float, station_id: int = 1, name: str = "S") -> dict:
    return {
        "route_position_miles": pos,
        "price_per_gallon": Decimal(str(price)),
        "station": {
            "id": station_id,
            "name": name,
            "address": f"{name} Rd",
            "city": "Test",
            "state": "OK",
            "latitude": 36.0,
            "longitude": -96.0,
        },
    }


# ----------------------------------------------------------------------- #
# 1. Short route (shorter than starting fuel range): no purchase.
# ----------------------------------------------------------------------- #
class TestShortRoute:
    def test_no_stations_short_route(self):
        result = optimize(
            stations=[],
            destination_position_miles=300,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert result.total_fuel_purchased_gallons == 0
        assert result.total_fuel_cost == 0
        assert len(result.stops) == 0

    def test_short_route_with_present_station(self):
        result = optimize(
            stations=[_make_station(50, 3.0)],
            destination_position_miles=200,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert len(result.stops) == 0
        assert result.total_fuel_purchased_gallons == 0


# ----------------------------------------------------------------------- #
# 2. Destination reachable with current fuel: zero purchase.
# ----------------------------------------------------------------------- #
class TestDestinationReachable:
    def test_station_present_but_not_needed(self):
        result = optimize(
            stations=[_make_station(200, 4.0)],
            destination_position_miles=400,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        # Arrive at 200 with 30 gal; the destination needs only 20 more -> buy 0.
        assert len(result.stops) == 0
        assert result.total_fuel_purchased_gallons == 0


# ----------------------------------------------------------------------- #
# 3. Cheaper future station: buy only enough to reach it.
# ----------------------------------------------------------------------- #
class TestCheaperFutureStation:
    def test_buy_only_enough_to_reach_cheaper(self):
        stations = [
            _make_station(100, 3.5, station_id=1),
            _make_station(300, 3.0, station_id=2),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=500,
            starting_fuel_gallons=20,
            tank_capacity_gallons=50,
            mpg=10,
        )
        # At 100: fuel=10. cheaper at 300 needs 20 gal total -> buy 10 gal @3.5.
        # At 300: fuel=0. buy 20 gal @3.0 for the destination.
        assert len(result.stops) == 2
        assert result.stops[0].fuel_purchased_gallons == Decimal("10")
        assert result.stops[0].retail_price_per_gallon == Decimal("3.5")
        assert result.total_fuel_cost == Decimal("95")

    def test_less_than_full_tank_bought_at_expensive_station(self):
        """The expensive station must NOT be topped-off to the tank limit."""
        stations = [
            _make_station(100, 3.5, station_id=1),
            _make_station(300, 3.0, station_id=2),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=500,
            starting_fuel_gallons=20,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert result.stops[0].fuel_purchased_gallons < Decimal("30")  # not a full tank


# ----------------------------------------------------------------------- #
# 4. No cheaper future station: fill tank.
# ----------------------------------------------------------------------- #
class TestNoCheaperFutureStation:
    def test_fill_tank_when_no_cheaper_reachable(self):
        stations = [
            _make_station(100, 3.5, station_id=1),
            _make_station(300, 4.0, station_id=2),
            _make_station(600, 3.8, station_id=3),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=900,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        # At 100: fuel=40, no cheaper reachable, dest out of range -> fill (10 gal @3.5).
        assert result.stops[0].fuel_after_stop_gallons == Decimal("50")
        assert result.total_fuel_cost == Decimal("149")


# ----------------------------------------------------------------------- #
# 5. Cheaper station beyond the maximum range: ignore it.
# ----------------------------------------------------------------------- #
class TestCheaperBeyondMaxRange:
    def test_far_cheaper_station_ignored(self):
        stations = [
            _make_station(100, 4.0, station_id=1),
            _make_station(500, 4.5, station_id=2),
            _make_station(700, 3.0, station_id=3),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=1000,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        # At 100: the 3.0 station is 600 miles ahead (> 500) so it is NOT used to
        # decide a purchase; the tank is filled instead.
        assert result.stops[0].fuel_after_stop_gallons == Decimal("50")
        assert result.total_fuel_cost == Decimal("175")


# ----------------------------------------------------------------------- #
# 6. Exact 500 mile reach: allowed.
# ----------------------------------------------------------------------- #
class TestExactMaxRange:
    def test_station_at_exact_max_range(self):
        stations = [_make_station(500, 4.0, station_id=1)]
        result = optimize(
            stations=stations,
            destination_position_miles=1000,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert len(result.stops) == 1
        assert result.stops[0].fuel_before_stop_gallons == Decimal("0")
        assert result.total_fuel_cost == Decimal("200")


# ----------------------------------------------------------------------- #
# 7. Current fuel already sufficient: purchase zero.
# ----------------------------------------------------------------------- #
class TestCurrentFuelSufficient:
    def test_arrive_at_station_with_dest_in_range(self):
        stations = [_make_station(50, 4.0, station_id=1)]
        result = optimize(
            stations=stations,
            destination_position_miles=200,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert len(result.stops) == 0
        assert result.total_fuel_cost == 0


# ----------------------------------------------------------------------- #
# 8. Destination reachable after a partial purchase: buy only required amount.
# ----------------------------------------------------------------------- #
class TestPartialPurchaseToReachDest:
    def test_buy_only_enough_to_reach_dest(self):
        stations = [_make_station(400, 4.0, station_id=1)]
        result = optimize(
            stations=stations,
            destination_position_miles=600,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert len(result.stops) == 1
        assert result.stops[0].fuel_purchased_gallons == Decimal("10")
        assert result.stops[0].estimated_cost == Decimal("40")


# ----------------------------------------------------------------------- #
# 9. Multiple stops: correctly manage fuel state.
# ----------------------------------------------------------------------- #
class TestMultipleStops:
    def test_multi_stop_fuel_state(self):
        stations = [
            _make_station(200, 5.0, station_id=1),
            _make_station(400, 3.0, station_id=2),
            _make_station(600, 4.0, station_id=3),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=1000,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert len(result.stops) == 2
        assert result.stops[0].fuel_purchased_gallons == Decimal("40")
        assert result.stops[1].fuel_purchased_gallons == Decimal("10")
        assert result.total_fuel_purchased_gallons == Decimal("50")
        assert result.total_fuel_cost == Decimal("160")


# ----------------------------------------------------------------------- #
# 10. Impossible route: controlled error.
# ----------------------------------------------------------------------- #
class TestImpossibleRoute:
    def test_no_stations_long_route(self):
        with pytest.raises(NoFeasiblePlan):
            optimize(
                stations=[],
                destination_position_miles=800,
                starting_fuel_gallons=50,
                tank_capacity_gallons=50,
                mpg=10,
            )

    def test_gap_larger_than_range(self):
        stations = [_make_station(200, 4.0, station_id=1)]
        with pytest.raises(NoFeasiblePlan):
            optimize(
                stations=stations,
                destination_position_miles=1000,
                starting_fuel_gallons=50,
                tank_capacity_gallons=50,
                mpg=10,
            )


# ----------------------------------------------------------------------- #
# 11. Zero-distance route: no purchase.
# ----------------------------------------------------------------------- #
class TestZeroDistance:
    def test_zero_distance_no_purchase(self):
        result = optimize(
            stations=[],
            destination_position_miles=0,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert result.total_fuel_purchased_gallons == 0
        assert len(result.stops) == 0


# ----------------------------------------------------------------------- #
# 12. Decimal money calculations: exact, no floating point money errors.
# ----------------------------------------------------------------------- #
class TestDecimalMoney:
    def test_exact_decimal_cost(self):
        stations = [_make_station(100, 3.14159, station_id=1)]
        result = optimize(
            stations=stations,
            destination_position_miles=600,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert result.total_fuel_cost == Decimal("10") * Decimal("3.14159")
        assert isinstance(result.total_fuel_cost, Decimal)

    def test_cost_never_uses_float(self):
        stations = [_make_station(100, 2.907, station_id=1)]
        result = optimize(
            stations=stations,
            destination_position_miles=600,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        assert result.stops[0].estimated_cost == Decimal("10") * Decimal("2.907")


# ----------------------------------------------------------------------- #
# 13. Future stations with varying prices: choose correct reachable cheaper.
# ----------------------------------------------------------------------- #
class TestVaryingPrices:
    def test_choose_reachable_cheaper_over_closer_expensive(self):
        stations = [
            _make_station(100, 5.0, station_id=1),
            _make_station(300, 4.0, station_id=2),
            _make_station(500, 3.0, station_id=3),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=1000,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        # Only the last (cheapest) station is used to fill the destination leg.
        assert len(result.stops) == 1
        assert result.stops[0].retail_price_per_gallon == Decimal("3.0")
        assert result.total_fuel_cost == Decimal("150")


# ----------------------------------------------------------------------- #
# 14. Station order: only consider downstream stations.
# ----------------------------------------------------------------------- #
class TestStationOrder:
    def test_station_behind_is_not_returned_to(self):
        """The optimizer must never move backward along the route."""
        stations = [
            _make_station(300, 3.0, station_id=1),
            _make_station(100, 4.0, station_id=2),
            _make_station(200, 3.5, station_id=3),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=500,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        # All stations behind position 300 are ignored; dest is in range.
        assert len(result.stops) == 0

    def test_future_scan_only_ahead(self):
        stations = [
            _make_station(100, 4.0, station_id=1),
            _make_station(600, 3.0, station_id=2),
        ]
        result = optimize(
            stations=stations,
            destination_position_miles=700,
            starting_fuel_gallons=50,
            tank_capacity_gallons=50,
            mpg=10,
        )
        # Station 2 at 600 is 500 miles ahead of 100 - exactly reachable.
        # At 100: fuel=40, cheaper at 600 (dist=500) -> needed=50, purchase=10 gal.
        assert result.stops[0].fuel_purchased_gallons == Decimal("10")


# ----------------------------------------------------------------------- #
# Brute-force correctness validator
# ----------------------------------------------------------------------- #
def _brute_force_minimum(
    stations: list[dict],
    destination_position_miles: float,
    starting_fuel_gallons: float,
    tank_capacity_gallons: float,
    mpg: float,
) -> float:
    """Exhaustive dynamic program over a deci-gallon purchase grid.

    The grid resolution is 0.1 gallons.  All test scenarios here use integer
    mile positions with ``mpg = 10``, so every required fuel amount lies
    exactly on the grid and the discretization introduces no error.

    This is a deliberately independent re-derivation of the optimum: it
    enumerates every possible purchase amount at every station rather than
    applying the greedy rule, so a divergence between the two implementations
    would indicate a bug.
    """
    scale = 10  # deci-gallons
    cap = int(round(float(tank_capacity_gallons) * scale))
    start = int(round(float(starting_fuel_gallons) * scale))
    m = int(mpg)
    n_stations = len(stations)
    positions = [0.0] + [float(s["route_position_miles"]) for s in stations] + [
        float(destination_position_miles)
    ]
    n = len(positions)
    price_map = {i: float(stations[i - 1]["price_per_gallon"]) for i in range(1, n - 1)}

    INF = float("inf")
    cost = [[INF] * (cap + 1) for _ in range(n)]
    cost[0][start] = 0.0

    for i in range(n - 1):
        distance = positions[i + 1] - positions[i]
        needed_deci = int(round(distance * scale / m))
        for f in range(cap + 1):
            if cost[i][f] >= INF:
                continue
            if f < needed_deci:
                continue
            arrival = f - needed_deci
            if i + 1 == n - 1:
                if arrival <= cap and cost[i + 1][min(arrival, cap)] > cost[i][f]:
                    cost[i + 1][min(arrival, cap)] = cost[i][f]
                continue
            # Purchase anything from 0 up to the free tank space.
            for purchase in range(cap - arrival + 1):
                new_fuel = arrival + purchase
                spend = (purchase / scale) * price_map[i + 1]
                candidate = cost[i][f] + spend
                if candidate < cost[i + 1][new_fuel]:
                    cost[i + 1][new_fuel] = candidate

    return min(cost[n - 1])


# ----------------------------------------------------------------------- #
# Brute-force comparison tests
# ----------------------------------------------------------------------- #
class TestBruteForceCorrectness:
    def _compare(self, stations, dest, start=10, cap=10):
        greedy = optimize(
            [dict(s) for s in stations],
            dest,
            start,
            cap,
            10,
        )
        bf = _brute_force_minimum([dict(s) for s in stations], dest, start, cap, 10)
        assert float(greedy.total_fuel_cost) == pytest.approx(bf, abs=0.02), (
            f"greedy={greedy.total_fuel_cost} brute_force={bf} stations={stations} dest={dest}"
        )

    def test_short_route(self):
        self._compare([], 50)

    def test_single_station(self):
        self._compare(
            [{"route_position_miles": 20.0, "price_per_gallon": Decimal("4.0")}],
            100,
        )

    def test_three_stations_varied(self):
        stations = [
            {"route_position_miles": 20.0, "price_per_gallon": Decimal("4.0")},
            {"route_position_miles": 40.0, "price_per_gallon": Decimal("3.0")},
            {"route_position_miles": 70.0, "price_per_gallon": Decimal("4.5")},
        ]
        self._compare(stations, 100)

    def test_same_prices(self):
        stations = [
            {"route_position_miles": 20.0, "price_per_gallon": Decimal("3.5")},
            {"route_position_miles": 40.0, "price_per_gallon": Decimal("3.5")},
            {"route_position_miles": 60.0, "price_per_gallon": Decimal("3.5")},
        ]
        self._compare(stations, 100)

    def test_declining_prices(self):
        stations = [
            {"route_position_miles": 20.0, "price_per_gallon": Decimal("5.0")},
            {"route_position_miles": 40.0, "price_per_gallon": Decimal("4.0")},
            {"route_position_miles": 60.0, "price_per_gallon": Decimal("3.0")},
        ]
        self._compare(stations, 100)

    def test_increasing_prices(self):
        stations = [
            {"route_position_miles": 20.0, "price_per_gallon": Decimal("3.0")},
            {"route_position_miles": 40.0, "price_per_gallon": Decimal("4.0")},
            {"route_position_miles": 60.0, "price_per_gallon": Decimal("5.0")},
        ]
        self._compare(stations, 100)

    def test_randomized_deterministic(self):
        rng = random.Random(42)
        for _ in range(20):
            n = rng.randint(2, 5)
            positions = sorted(rng.sample(range(10, 95), n))
            prices = [round(rng.uniform(2.5, 5.0), 2) for _ in range(n)]
            stations = [
                {"route_position_miles": float(p), "price_per_gallon": Decimal(str(pr))}
                for p, pr in zip(positions, prices)
            ]
            dest = positions[-1] + rng.randint(10, 30)
            self._compare(stations, dest)
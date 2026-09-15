"""Exact fixed-route minimum-cost refueling optimizer.

This module is deliberately pure Python business logic:

* NO Django imports
* NO database access
* NO HTTP calls
* NO environment access

It solves the following problem: Given a FIXED driving route (a set of fuel
stations whose position along the route is known, plus a final destination
position), which stations should be visited and how much fuel should be bought
at each, so that the total purchase cost is the mathematical minimum?

Classic greedy argument used here:

At station S with price P, let Q be the price of the first future station that
(a) lies ahead of S, (b) is strictly cheaper (Q < P), and (c) is reachable
within the vehicle's maximum range.

  * Any gallon purchased at S that could instead be purchased at the cheaper
    station Q costs more (P per gallon) and is therefore wasteful.  So we
    purchase ONLY enough fuel to reach Q.
  * If no cheaper reachable station exists, there is no cheaper purchasing
    opportunity within the vehicle's current reachable range, so filling the
    tank at S cannot be improved upon.
  * If the destination is reachable with the fuel already in the tank, buying
    further fuel at S is unnecessary.

This greedy produces the minimum possible purchase cost for the fixed route
and the given candidate station set under the model assumptions.  It does NOT
claim global optimality of the road route itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class NoFeasiblePlan(Exception):
    """Raised when the vehicle cannot complete the fixed route with fuel."""


@dataclass
class FuelStop:
    stop_number: int
    station: dict[str, Any]
    route_position_miles: Decimal
    distance_from_previous_stop_miles: Decimal
    retail_price_per_gallon: Decimal
    fuel_before_stop_gallons: Decimal
    fuel_purchased_gallons: Decimal
    fuel_after_stop_gallons: Decimal
    estimated_cost: Decimal


@dataclass
class OptimizationResult:
    stops: list[FuelStop] = field(default_factory=list)
    total_fuel_purchased_gallons: Decimal = Decimal("0")
    total_fuel_cost: Decimal = Decimal("0")
    ending_fuel_gallons: Decimal = Decimal("0")
    destination_position_miles: Decimal = Decimal("0")


def optimize(
    stations: list[dict[str, Any]],
    destination_position_miles: float | Decimal,
    starting_fuel_gallons: float | Decimal,
    tank_capacity_gallons: float | Decimal,
    mpg: int,
) -> OptimizationResult:
    """Compute the minimum-cost fuel purchase plan for a fixed route.

    Parameters
    ----------
    stations
        Candidate stations sorted by increasing ``route_position_miles``.
        Each station is a dict containing at least ``route_position_miles``
        (float) and ``price_per_gallon`` (Decimal).
    destination_position_miles
        Total driving distance from route start to the destination in miles.
    starting_fuel_gallons
        Fuel already in the tank when the trip begins (not charged).
    tank_capacity_gallons
        Maximum fuel the tank can hold.
    mpg
        Fixed vehicle fuel economy (miles per gallon).

    Raises
    ------
    NoFeasiblePlan
        If the destination cannot be reached using the available stations,
        the starting fuel, and the maximum vehicle range.
    """
    tank = Decimal(str(tank_capacity_gallons))
    fuel = Decimal(str(starting_fuel_gallons))
    mpg_d = Decimal(str(mpg))
    max_range_miles = tank * mpg_d
    destination = Decimal(str(destination_position_miles))
    current_position = Decimal("0")

    total_purchased = Decimal("0")
    total_cost = Decimal("0")
    stops: list[FuelStop] = []

    candidate_positions = [
        item for s in stations if (item := _station_for_plan(s))["position"] < destination
    ]

    if destination <= Decimal("0"):
        return OptimizationResult(
            stops=[],
            total_fuel_purchased_gallons=Decimal("0"),
            total_fuel_cost=Decimal("0"),
            ending_fuel_gallons=fuel,
            destination_position_miles=destination,
        )

    for index, item in enumerate(candidate_positions):
        position = item["position"]
        if position <= current_position:
            # The caller guarantees nondecreasing positions; this guard ensures
            # the optimizer never moves backward along the route.
            continue

        distance = position - current_position
        required = distance / mpg_d

        if fuel < required:
            # The current station is out of reach with the fuel on board.
            raise NoFeasiblePlan(
                "Vehicle cannot reach the station "
                f"at {position:.1f} miles with the fuel available."
            )

        fuel -= required
        price = item["price"]

        # ------------------------- purchase decision -----------------------
        dest_needed = (destination - position) / mpg_d

        if fuel >= dest_needed:
            # Destination is already reachable: nothing to buy.
            purchase = Decimal("0")
        else:
            cheaper = _first_cheaper_reachable(
                candidate_positions, index, position, price, max_range_miles
            )
            if cheaper is not None:
                needed = (cheaper["position"] - position) / mpg_d
                purchase = max(Decimal("0"), needed - fuel)
                # Clamp so we never exceed the tank (needed <= max_range
                # guarantees this, but keep the invariant explicit).
                purchase = min(purchase, tank - fuel)
            else:
                if dest_needed <= tank:
                    # Dest is within maximum range: buy only what is required.
                    purchase = max(Decimal("0"), dest_needed - fuel)
                    purchase = min(purchase, tank - fuel)
                else:
                    # No cheaper reachable station and the destination is out of
                    # range: fill the tank as much as possible.
                    purchase = tank - fuel

        purchase = max(purchase, Decimal("0"))
        purchase = min(purchase, tank - fuel)

        # -------------------------------------------------------------------
        fuel += purchase

        if purchase > Decimal("0"):
            cost = purchase * price
            total_purchased += purchase
            total_cost += cost
            stop_number = len(stops) + 1
            previous_stop_position = stops[-1].route_position_miles if stops else Decimal("0")
            stops.append(
                FuelStop(
                    stop_number=stop_number,
                    station=item["station"],
                    route_position_miles=position,
                    distance_from_previous_stop_miles=position - previous_stop_position,
                    retail_price_per_gallon=price,
                    fuel_before_stop_gallons=fuel - purchase,
                    fuel_purchased_gallons=purchase,
                    fuel_after_stop_gallons=fuel,
                    estimated_cost=cost,
                )
            )

        current_position = position

    final_needed = (destination - current_position) / mpg_d
    if fuel < final_needed:
        raise NoFeasiblePlan(
            "Vehicle cannot reach the destination with the fuel available "
            "after the last reachable station."
        )
    ending_fuel = fuel - final_needed

    result = OptimizationResult(
        stops=stops,
        total_fuel_purchased_gallons=total_purchased,
        total_fuel_cost=total_cost,
        ending_fuel_gallons=ending_fuel,
        destination_position_miles=destination,
    )
    _validate_invariants(result, tank, mpg_d)
    return result


def _station_for_plan(station: dict[str, Any]) -> dict[str, Any]:
    return {
        "position": Decimal(str(station["route_position_miles"])),
        "price": Decimal(station["price_per_gallon"]),
        "station": station.get("station", station),
    }


def _first_cheaper_reachable(
    items: list[dict[str, Any]],
    current_index: int,
    current_position: Decimal,
    current_price: Decimal,
    max_range_miles: Decimal,
) -> dict[str, Any] | None:
    """First future station that is strictly cheaper and in range."""
    for item in items[current_index + 1:]:
        if item["position"] <= current_position:
            continue
        if item["position"] - current_position <= max_range_miles and item["price"] < current_price:
            return item
    return None


def _validate_invariants(result: OptimizationResult, tank: Decimal, mpg_d: Decimal) -> None:
    """Internal consistency checks required by the specification.

    If any of these fail the implementation has a bug; do NOT return a fake
    successful result.
    """
    if result.total_fuel_cost < 0:
        raise AssertionError("negative total fuel cost")
    if result.total_fuel_purchased_gallons < 0:
        raise AssertionError("negative total purchased fuel")

    running = result.total_fuel_purchased_gallons
    running_cost = Decimal("0")
    prior_position = Decimal("-1")
    for stop in result.stops:
        if stop.fuel_purchased_gallons < 0:
            raise AssertionError("negative purchase at a stop")
        if stop.fuel_before_stop_gallons < 0:
            raise AssertionError("negative fuel before a stop")
        if stop.fuel_after_stop_gallons > tank:
            raise AssertionError("fuel exceeded tank capacity")
        if stop.fuel_before_stop_gallons + stop.fuel_purchased_gallons != stop.fuel_after_stop_gallons:
            raise AssertionError("fuel bookkeeping mismatch at a stop")
        if stop.route_position_miles <= prior_position:
            raise AssertionError("fuel stops are not forward ordered")
        prior_position = stop.route_position_miles
        running -= stop.fuel_purchased_gallons
        running_cost += stop.estimated_cost

    if running != 0:
        raise AssertionError("total purchased fuel does not match individual purchases")
    if running_cost != result.total_fuel_cost:
        raise AssertionError("total cost does not match individual stop costs")
    if result.ending_fuel_gallons < 0:
        raise AssertionError("ending fuel is negative")
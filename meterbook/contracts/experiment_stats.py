"""Pure weighted scenario statistics and growth crossover geometry.

Everything here is deterministic and free of side effects so that the same
frozen experiment always produces the same answer, regardless of the order in
which candidates or scenarios were submitted.  Monetary inputs are integer
minor units (cents) so the ledger's exact-amount and ROUND_HALF_EVEN rounding
convention stays the single source of truth for money.
"""
from decimal import Decimal, ROUND_HALF_EVEN
from meterbook.values import reject, encoded, exact, cents as to_cents


def _round_cents(value):
    """Outcomes are already integer minor units; interpolated or averaged
    values round half-even to a whole cent without rescaling."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _decimal_weight(weight):
    if isinstance(weight, bool) or not isinstance(weight, (str, int, Decimal)):
        reject('experiment_weight', 'Weights must be exact strings or integers')
    parsed = Decimal(weight)
    if not parsed.is_finite() or parsed <= 0 or parsed > Decimal('1e12') or parsed.as_tuple().exponent < -12:
        reject('experiment_weight', 'Weight must be positive and within scale')
    return parsed


def normalize_weights(weights):
    """Return positive Decimal weights keyed by scenario id."""
    if not isinstance(weights, dict) or not weights:
        reject('experiment_weights', 'Scenario weights must be a nonempty mapping')
    return {key: _decimal_weight(value) for key, value in weights.items()}


@exact
def expected_value(outcomes, weights):
    """Weighted mean of integer-cent outcomes, rounded to whole cents."""
    total_weight = sum(weights.values())
    total = sum(Decimal(outcomes[key]) * weights[key] for key in outcomes)
    return _round_cents(total / total_weight)


def _ordered(outcomes):
    """Deterministic ordering: cost ascending then scenario id, independent of
    the order callers happened to store rows."""
    return sorted(outcomes, key=lambda key: (outcomes[key], key))


@exact
def weighted_quantile(outcomes, weights, level):
    """Inverse-CDF weighted quantile with linear interpolation.

    A discrete mass straddling the quantile boundary is split into the exact
    portion lying on each side instead of snapping the whole atom.
    """
    if not isinstance(level, (str, int, Decimal)):
        reject('experiment_quantile', 'Quantile level must be an exact decimal')
    level = Decimal(level)
    if not level.is_finite() or not Decimal(0) <= level <= Decimal(1):
        reject('experiment_quantile', 'Quantile level must lie between zero and one')
    ordered = _ordered(outcomes)
    if level == 0:
        return outcomes[ordered[0]]
    if level == 1:
        return outcomes[ordered[-1]]
    total_weight = sum(weights.values())
    target = level * total_weight
    cumulative = Decimal(0)
    previous_cost = None
    for key in ordered:
        cost = Decimal(outcomes[key])
        mass = weights[key]
        if cumulative + mass >= target:
            if previous_cost is None or target <= cumulative:
                return int(cost)
            # Split this atom at the exact quantile position and interpolate.
            fraction = (target - cumulative) / mass
            interpolated = previous_cost + (cost - previous_cost) * fraction
            return _round_cents(interpolated)
        previous_cost = cost
        cumulative += mass
    return outcomes[ordered[-1]]


@exact
def weighted_cvar(outcomes, weights, tail_ratio):
    """Conditional value at risk over the worst ``tail_ratio`` fraction of mass.

    Costs are accumulated from the worst (highest) downward until the target
    tail mass is reached.  When a discrete atom crosses the tail boundary only
    the exact tail portion of its weight is counted, so CVaR is continuous in
    ``tail_ratio`` rather than jumping between scenarios.
    """
    if not isinstance(tail_ratio, (str, int, Decimal)):
        reject('experiment_tail', 'Tail ratio must be an exact decimal')
    tail_ratio = Decimal(tail_ratio)
    if not tail_ratio.is_finite() or not Decimal(0) < tail_ratio <= Decimal(1):
        reject('experiment_tail', 'Tail ratio must lie above zero and at most one')
    total_weight = sum(weights.values())
    target = tail_ratio * total_weight
    ordered = sorted(outcomes, key=lambda key: (-outcomes[key], key))
    accumulated_mass = Decimal(0)
    accumulated_cost_mass = Decimal(0)
    for key in ordered:
        cost = Decimal(outcomes[key])
        mass = weights[key]
        if accumulated_mass + mass <= target:
            accumulated_mass += mass
            accumulated_cost_mass += cost * mass
            if accumulated_mass == target:
                break
        else:
            partial = target - accumulated_mass
            accumulated_mass += partial
            accumulated_cost_mass += cost * partial
            break
    return _round_cents(accumulated_cost_mass / target)


@exact
def exceedance_probability(outcomes, weights, budget_cents):
    """Probability mass whose cost is strictly over the budget, as an exact
    decimal string between 0 and 1."""
    if type(budget_cents) is not int:
        reject('experiment_budget', 'Budget must be integer minor units')
    total_weight = sum(weights.values())
    over = sum(weights[key] for key in outcomes if outcomes[key] > budget_cents)
    ratio = over / total_weight
    return encoded(ratio.quantize(Decimal('1e-12'), rounding=ROUND_HALF_EVEN))


def worst_case(outcomes):
    key = max(outcomes, key=lambda identifier: (outcomes[identifier], identifier))
    return {'scenario_id': key, 'cost_cents': outcomes[key]}


def summarize(outcomes, weights, quantiles, tail_ratio):
    """Full risk profile for one quote over the shared scenario set."""
    if set(outcomes) != set(weights):
        reject('experiment_outcomes', 'Outcomes and weights must cover the same scenarios')
    if not isinstance(quantiles, list) or not 1 <= len(quantiles) <= 20:
        reject('experiment_quantiles', 'Require one to twenty quantile levels')
    levels = []
    seen = set()
    for level in quantiles:
        if not isinstance(level, (str, int, Decimal)):
            reject('experiment_quantile', 'Quantile level must be an exact decimal')
        parsed = Decimal(level)
        if not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(1):
            reject('experiment_quantile', 'Quantile level must lie between zero and one')
        text = encoded(parsed)
        if text in seen:
            reject('experiment_quantile', 'Quantile levels must be distinct')
        seen.add(text)
        levels.append(text)
    if not isinstance(tail_ratio, (str, int, Decimal)):
        reject('experiment_tail', 'Tail ratio must be an exact decimal')
    tail = Decimal(tail_ratio)
    if not tail.is_finite() or not Decimal(0) < tail <= Decimal(1):
        reject('experiment_tail', 'Tail ratio must lie above zero and at most one')
    return {
        'expected_cents': expected_value(outcomes, weights),
        'quantiles': [{'level': level, 'cost_cents': weighted_quantile(outcomes, weights, level)} for level in levels],
        'worst_case': worst_case(outcomes),
        'cvar': {'tail_ratio': encoded(tail), 'cost_cents': weighted_cvar(outcomes, weights, tail)},
        'minimum_cents': min(outcomes.values()),
    }


def decide(quote_reports, budget_cents, max_exceedance, tail_cap_cents):
    """Feasibility gate first (probability then tail cap), expectation second.

    Every candidate keeps an explicit inclusion/rejection reason, including the
    case where none is feasible.
    """
    if type(budget_cents) is not int or type(tail_cap_cents) is not int:
        reject('experiment_budget', 'Money limits must be integer minor units')
    cap = Decimal(max_exceedance)
    if not cap.is_finite() or not Decimal(0) <= cap <= Decimal(1):
        reject('experiment_limit', 'Exceedance limit must lie between zero and one')
    evaluated = []
    for quote_id in sorted(quote_reports):
        report = quote_reports[quote_id]
        probability = Decimal(report['exceedance_probability'])
        cvar = report['cvar']['cost_cents']
        reasons = []
        if probability > cap:
            reasons.append({'rule': 'exceedance_probability', 'limit': encoded(cap), 'actual': report['exceedance_probability']})
        if cvar > tail_cap_cents:
            reasons.append({'rule': 'tail_charge_cap', 'limit_cents': tail_cap_cents, 'actual_cents': cvar})
        evaluated.append({'quote_id': quote_id, 'feasible': not reasons, 'reasons': reasons,
                          'expected_cents': report['expected_cents']})
    feasible = [row for row in evaluated if row['feasible']]
    if not feasible:
        return {'feasible': False, 'selected': None, 'candidates': evaluated}
    selected = min(feasible, key=lambda row: (row['expected_cents'], row['quote_id']))
    return {'feasible': True, 'selected': selected['quote_id'], 'candidates': evaluated}


# --- growth crossover geometry -------------------------------------------------


def _raw_charge(rate, quantity):
    """Exact tiered charge before the cent rounding, honouring included
    allowance, minimum and maximum charges."""
    included = Decimal(rate['included'])
    remaining = max(Decimal(0), quantity - included)
    total = Decimal(0)
    boundary = Decimal(0)
    for tier in rate['tiers']:
        up_to = None if tier['up_to'] is None else Decimal(tier['up_to'])
        width = remaining if up_to is None else max(Decimal(0), up_to - boundary)
        used = min(remaining, width)
        total += used * Decimal(tier['unit_price'])
        remaining -= used
        if up_to is not None:
            boundary = up_to
        if remaining == 0:
            break
    if quantity:
        total = max(total, Decimal(rate['minimum_charge']))
        if rate['maximum_charge'] is not None:
            total = min(total, Decimal(rate['maximum_charge']))
    return total


def _rate_breakpoints(rate):
    """All quantity values where the charge function can change slope or
    intercept: tier boundaries plus exact minimum/maximum envelope crossings."""
    included = Decimal(rate['included'])
    points = {Decimal(0)}
    for tier in rate['tiers']:
        if tier['up_to'] is not None:
            points.add(included + Decimal(tier['up_to']))
    # Rebuild the uncapped (pre-envelope) affine segments.
    running = Decimal(0)
    affine = []
    segment_lower = included
    for tier in rate['tiers']:
        price = Decimal(tier['unit_price'])
        segment_upper = None if tier['up_to'] is None else included + Decimal(tier['up_to'])
        # charge(q) = price * (q - segment_lower) + running
        affine.append((segment_lower, segment_upper, price, running - price * segment_lower))
        if segment_upper is not None:
            running += price * (segment_upper - segment_lower)
            segment_lower = segment_upper
    minimum = Decimal(rate['minimum_charge'])
    maximum = None if rate['maximum_charge'] is None else Decimal(rate['maximum_charge'])
    for segment_lower, segment_upper, slope, intercept in affine:
        if slope <= 0:
            continue
        for envelope in (minimum, maximum):
            if envelope is None:
                continue
            crossing = (envelope - intercept) / slope
            if crossing > segment_lower and (segment_upper is None or crossing < segment_upper):
                points.add(crossing)
    return sorted(points)


def _rate_segments(rate):
    """Affine segments (q_lower, q_upper_or_None, slope, intercept) of the
    exact charge as a function of monthly quantity."""
    points = _rate_breakpoints(rate)
    segments = []
    for index, start in enumerate(points):
        end = points[index + 1] if index + 1 < len(points) else None
        if end is not None and end <= start:
            continue
        if end is None:
            q1, q2 = start + 1, start + 2
        else:
            q1 = start + (end - start) / 3
            q2 = start + 2 * (end - start) / 3
        c1 = _raw_charge(rate, q1)
        c2 = _raw_charge(rate, q2)
        slope = (c2 - c1) / (q2 - q1)
        intercept = c1 - slope * q1
        segments.append((start, end, slope, intercept))
    return segments


def _active_tier(rate, quantity):
    """Index of the tier billing the marginal unit at this quantity."""
    included = Decimal(rate['included'])
    remaining = max(Decimal(0), quantity - included)
    if remaining == 0:
        return 0
    for index, tier in enumerate(rate['tiers']):
        up_to = None if tier['up_to'] is None else Decimal(tier['up_to'])
        if up_to is None or remaining <= up_to:
            return index
    return len(rate['tiers']) - 1


def _map_segments(segments, base, growth_start, growth_end):
    """Map quantity-space affine segments onto growth-multiplier space."""
    if base == 0:
        # A zero baseline stays zero at every positive multiplier, so the
        # meter never bills (minimum charge requires positive usage).
        return [(growth_start, growth_end, Decimal(0), Decimal(0))]
    mapped = []
    for lower, upper, slope, intercept in segments:
        g_lower = max(growth_start, lower / base)
        g_upper = growth_end if upper is None else min(growth_end, upper / base)
        if g_upper <= g_lower:
            continue
        mapped.append((g_lower, g_upper, slope * base, intercept))
    return mapped


@exact
def crossover_analysis(candidates, bases, growth_start, growth_end):
    """Intervals of the shared growth multiplier on which each quote wins.

    Candidate cost is piecewise linear in the multiplier; every tier boundary,
    minimum/maximum envelope crossing, overage threshold, fixed fee and the
    period floor are kept as explicit breakpoints, so every flip interval and
    the active billing tiers are reported rather than assuming one crossing.
    """
    growth_start = Decimal(growth_start)
    growth_end = Decimal(growth_end)
    if not growth_start.is_finite() or not growth_end.is_finite() or growth_start <= 0 or growth_end < growth_start:
        reject('experiment_growth', 'Growth range must be positive and ordered')
    if not isinstance(candidates, dict) or not candidates:
        reject('experiment_candidates', 'Crossover requires at least one candidate')
    quote_ids = sorted(candidates)
    prepared = {}
    cuts = {growth_start, growth_end}
    for quote_id in quote_ids:
        candidate = candidates[quote_id]
        per_meter = []
        for meter in sorted(candidate['meters']):
            base = bases[meter]
            rate = candidate['meters'][meter]['rate']
            mapped = _map_segments(_rate_segments(rate), base, growth_start, growth_end)
            per_meter.append((meter, mapped))
            for g_lower, g_upper, _, _ in mapped:
                cuts.add(g_lower)
                cuts.add(g_upper)
            overage = candidate['meters'][meter].get('overage')
            if overage is not None and base > 0:
                threshold = Decimal(overage['allowance']) / base
                if growth_start < threshold < growth_end:
                    cuts.add(threshold)
        prepared[quote_id] = per_meter

    def unfloored_affine(quote_id, growth):
        """Slope/intercept of the candidate cost before the period floor on
        the cell containing ``growth`` (everything is affine inside a cell)."""
        candidate = candidates[quote_id]
        slope = Decimal(0)
        intercept = Decimal(candidate['fixed_fee_cents']) / Decimal(100)
        for meter, segments in prepared[quote_id]:
            # Right-continuous selection: at a breakpoint the next segment's
            # affine form applies, which also resolves the final growth cut.
            match = next(segment for segment in reversed(segments) if segment[0] <= growth)
            slope += match[2]
            intercept += match[3]
            base = bases[meter]
            overage = candidate['meters'][meter].get('overage')
            if overage is not None and base > 0:
                threshold = Decimal(overage['allowance']) / base
                unit = Decimal(overage['unit_price'])
                if growth >= threshold:
                    slope += unit * base
                    intercept -= unit * Decimal(overage['allowance'])
        return slope, intercept

    # The period floor binds where the unfloored cost crosses it; expose that
    # crossing as an explicit cell boundary per candidate.
    ordered_cuts = sorted(cuts)
    floor_roots = set()
    for quote_id in quote_ids:
        floor = Decimal(candidates[quote_id]['period_minimum_cents']) / Decimal(100)
        for index, start in enumerate(ordered_cuts):
            end = ordered_cuts[index + 1] if index + 1 < len(ordered_cuts) else None
            if end is not None and end <= start:
                continue
            slope, intercept = unfloored_affine(quote_id, start)
            if slope == 0:
                continue
            root = (floor - intercept) / slope
            if root > start and (end is None or root < end):
                floor_roots.add(root)
    cuts |= floor_roots
    ordered_cuts = sorted(cuts)

    def cost_and_tiers(quote_id, growth):
        candidate = candidates[quote_id]
        slope, intercept = unfloored_affine(quote_id, growth)
        value = slope * growth + intercept
        floor = Decimal(candidate['period_minimum_cents']) / Decimal(100)
        value = max(value, floor)
        active_tiers = {}
        for meter in sorted(candidate['meters']):
            quantity = bases[meter] * growth
            active_tiers[meter] = _active_tier(candidate['meters'][meter]['rate'], quantity)
        return value, active_tiers

    boundaries = [growth_start]
    for index in range(len(ordered_cuts) - 1):
        start, end = ordered_cuts[index], ordered_cuts[index + 1]
        if end <= start:
            continue
        for left_position, left_id in enumerate(quote_ids):
            left_slope, left_intercept = unfloored_affine(left_id, start)
            left_floor = Decimal(candidates[left_id]['period_minimum_cents']) / Decimal(100)
            for right_id in quote_ids[left_position + 1:]:
                right_slope, right_intercept = unfloored_affine(right_id, start)
                right_floor = Decimal(candidates[right_id]['period_minimum_cents']) / Decimal(100)
                # Floors are constant inside the cell once their own crossings
                # are cell boundaries, so each cost stays affine here.
                def floored(slope, intercept, floor):
                    probe = slope * start + intercept
                    return (slope, intercept) if probe >= floor else (Decimal(0), floor)
                ls, lb = floored(left_slope, left_intercept, left_floor)
                rs, rb = floored(right_slope, right_intercept, right_floor)
                difference_slope = ls - rs
                if difference_slope == 0:
                    continue
                difference_at_start = (ls * start + lb) - (rs * start + rb)
                root = start - difference_at_start / difference_slope
                if start < root < end:
                    boundaries.append(root)
    boundaries = sorted(set(boundaries))
    boundaries.append(growth_end)

    intervals = []
    for index in range(len(boundaries) - 1):
        start, end = boundaries[index], boundaries[index + 1]
        if end <= start:
            continue
        sample = (start + end) / 2
        ranking = []
        for quote_id in quote_ids:
            cost, active_tiers = cost_and_tiers(quote_id, sample)
            ranking.append((cost, quote_id, active_tiers))
        best_cost = min(row[0] for row in ranking)
        winners = sorted(row[1] for row in ranking if row[0] == best_cost)
        tiers = next(row[2] for row in ranking if row[1] == winners[0])
        intervals.append({
            'growth_from': encoded(start),
            'growth_to': encoded(end),
            'winners': winners,
            'tied': len(winners) > 1,
            'active_tiers': {meter: tiers[meter] for meter in sorted(tiers)},
            'winner_cost_cents': to_cents(best_cost),
        })
    merged = []
    for interval in intervals:
        if merged and merged[-1]['winners'] == interval['winners'] and merged[-1]['growth_to'] == interval['growth_from']:
            merged[-1]['growth_to'] = interval['growth_to']
            merged[-1]['active_tiers'] = interval['active_tiers']
            merged[-1]['winner_cost_cents'] = interval['winner_cost_cents']
        else:
            merged.append(dict(interval))
    return {'growth_from': encoded(growth_start), 'growth_to': encoded(growth_end), 'intervals': merged}

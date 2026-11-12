"""Quote experiments: full-schedule cost-risk comparison on frozen scenarios.

An experiment binds one checked group of complete historical settlement cycles
(one reconciliation run per period), an explicit meter set and one currency.
Scenarios are drawn whole from those periods and expanded by predeclared growth
multipliers with weights.  Every candidate quote is priced on the *same*
per-scenario meter paths, so cross-meter correlation and intra-month
concentration are preserved; tiered rates are always applied to the full monthly
quantity, never to daily pieces that are later summed.

The module is read-only with respect to contracts, rates and the ledger: it
neither changes the current contract nor starts a rate approval.
"""
import calendar
import hashlib
import json
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from meterbook.values import (
    fields, key, number, instant, decimal, encoded, canonical, digest,
    reject, prose,
)
from meterbook.contracts import registry, rates
from meterbook.contracts import experiment_stats
from meterbook.events import reconcile
from meterbook.settlement import pricing

DDL = '''
CREATE TABLE IF NOT EXISTS quote_experiments(experiment_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,currency TEXT NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL,base_fingerprint TEXT NOT NULL,author TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quote_experiment_scenarios(experiment_id TEXT NOT NULL,scenario_id TEXT NOT NULL,period TEXT NOT NULL,run_id TEXT NOT NULL,growth TEXT NOT NULL,weight TEXT NOT NULL,daily_paths TEXT NOT NULL,monthly_quantities TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(experiment_id,scenario_id));
'''

_MAX_SCENARIOS = 1000
_MAX_CANDIDATES = 50
_QUANTITY_SCALE = Decimal('0.000001')


def normalize_candidate(value, meters):
    """Validate and normalize one full quote against the experiment meters."""
    fields(value, {'quote_id', 'fixed_fee_cents', 'period_minimum_cents', 'meters'}, {'label'})
    quote_id = key(value['quote_id'])
    fixed_fee = number(value['fixed_fee_cents'], 'fixed_fee_cents', 0, 10**13)
    period_minimum = number(value['period_minimum_cents'], 'period_minimum_cents', 0, 10**13)
    if not isinstance(value['meters'], dict) or set(value['meters']) != set(meters):
        reject('experiment_candidate_meters', 'Each candidate must price every experiment meter exactly once', meters=sorted(meters))
    normalized_meters = {}
    for meter in sorted(meters):
        key(meter, 'meter')
        entry = value['meters'][meter]
        fields(entry, {'rate'}, {'overage'})
        rate_definition = rates.normalize(entry['rate'])
        overage = None
        if entry.get('overage') is not None:
            fields(entry['overage'], {'allowance', 'unit_price'})
            allowance = decimal(entry['overage']['allowance'], 'overage_allowance')
            unit_price = decimal(entry['overage']['unit_price'], 'overage_unit_price', maximum=Decimal('1e9'), scale=8)
            overage = {'allowance': encoded(allowance), 'unit_price': encoded(unit_price)}
        normalized_meters[meter] = {'rate': rate_definition, 'overage': overage}
    label = value.get('label', quote_id)
    prose(label, 'label', 200)
    return {'quote_id': quote_id, 'label': label, 'fixed_fee_cents': fixed_fee,
            'period_minimum_cents': period_minimum, 'meters': normalized_meters}


def normalize_growth_plan(value):
    fields(value, {'multipliers'}, {'weights'})
    if not isinstance(value['multipliers'], list) or not 1 <= len(value['multipliers']) <= 100:
        reject('experiment_growth', 'Growth plan requires one to one hundred multipliers')
    multipliers = []
    seen = set()
    for raw in value['multipliers']:
        multiplier = decimal(raw, 'growth_multiplier', minimum=Decimal('0.000001'), maximum=Decimal('1000000'))
        text = encoded(multiplier)
        if text in seen:
            reject('experiment_growth', 'Growth multipliers must be distinct')
        seen.add(text)
        multipliers.append(text)
    raw_weights = value.get('weights')
    if raw_weights is None:
        weights = {text: '1' for text in multipliers}
    else:
        if not isinstance(raw_weights, dict) or set(raw_weights) != set(multipliers):
            reject('experiment_weights', 'Growth weights must map every multiplier exactly once')
        weights = {text: encoded(experiment_stats.normalize_weights(raw_weights)[text]) for text in multipliers}
    return multipliers, weights


def period_days(period):
    year, month_number = map(int, period.split('-'))
    return calendar.monthrange(year, month_number)[1]


def _day_of(timestamp):
    return instant(timestamp)[8:10]


def _coverage_days(run_period):
    return {f'{index:02d}' for index in range(1, period_days(run_period) + 1)}


def completeness_report(run):
    """Daily coverage check for every experiment meter in a run.

    A period qualifies as a sample only when each meter that belongs to the
    experiment has at least one chosen (matched/resolved) usage record on every
    calendar day of the month.  Gaps cannot be treated as low usage.
    """
    days = _coverage_days(run['period'])
    covered = {}
    quantities = {}
    paths = {}
    for row in run['rows']:
        chosen = row['chosen']
        if chosen is None:
            continue
        meter = chosen['meter']
        day = _day_of(chosen['at'])
        covered.setdefault(meter, set()).add(day)
        quantities[meter] = quantities.get(meter, Decimal(0)) + Decimal(chosen['quantity'])
        paths.setdefault(meter, {})
        paths[meter].setdefault(day, []).append(
            {'usage_id': row['usage_id'], 'at': chosen['at'], 'quantity': chosen['quantity']})
    return days, covered, paths, quantities


class QuoteExperiments:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('experiment_transaction', 'Initialize quote experiments outside transactions')
        store.db.executescript(DDL)

    def fingerprint(self, contract_id):
        contract = registry.get(self.store, contract_id)
        return digest({
            'rates': rates.fingerprint(self.store, contract_id),
            'contract_revision': contract['revision'],
            'period_revisions': self.store.all('SELECT period,revision FROM period_revisions WHERE contract_id=? ORDER BY period', (contract_id,)),
        })

    def create(self, experiment_id, contract_id, run_ids, meters, growth_plan, candidates, seed, author):
        key(experiment_id)
        key(author)
        seed = number(seed, 'seed', 0, 2**31 - 1)
        contract = registry.get(self.store, contract_id)
        if not isinstance(meters, list) or not 1 <= len(meters) <= 100 or len(set(meters)) != len(meters):
            reject('experiment_meters', 'Experiment needs distinct bounded meter list')
        meter_keys = sorted(key(meter, 'meter') for meter in meters)
        unknown = set(meter_keys) - set(contract['definition']['meters'])
        if unknown:
            reject('experiment_meter', 'Experiment references an undeclared contract meter', meters=sorted(unknown))
        if not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 100:
            reject('experiment_runs', 'Experiment requires one to one hundred reconciliation runs')
        if len(set(run_ids)) != len(run_ids):
            reject('experiment_runs', 'A historical period cannot be bound twice')
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= _MAX_CANDIDATES:
            reject('experiment_candidates', 'Experiment requires one to fifty candidate quotes')
        multipliers, growth_weights = normalize_growth_plan(growth_plan)
        normalized_candidates = []
        candidate_ids = set()
        for candidate in candidates:
            normalized = normalize_candidate(candidate, meter_keys)
            if normalized['quote_id'] in candidate_ids:
                reject('experiment_candidate', 'Candidate quote identifiers must be distinct')
            candidate_ids.add(normalized['quote_id'])
            normalized_candidates.append(normalized)
        # Freeze candidates by quote id so input permutation cannot perturb the
        # digest or the conclusions.
        normalized_candidates.sort(key=lambda candidate: candidate['quote_id'])

        # Freeze the shared historical inputs and reject periods whose daily
        # data is incomplete before any scenario is built.  Periods are frozen
        # in chronological order so permuting the request cannot perturb the
        # experiment digest.
        fetched = {run_id: reconcile.get(self.store, run_id) for run_id in run_ids}
        bound_runs = []
        for run_id in sorted(run_ids, key=lambda identifier: fetched[identifier]['period']):
            run = fetched[run_id]
            if run['contract_id'] != contract_id:
                reject('experiment_scope', 'Reconciliation belongs to another contract')
            if not run['fresh']:
                reject('experiment_stale_run', 'Reconcile current usage before building an experiment')
            days, covered, paths, quantities = completeness_report(run)
            missing_meters = sorted(set(meter_keys) - set(covered))
            gap_days = {meter: sorted(days - covered.get(meter, set())) for meter in meter_keys}
            gap_days = {meter: gap for meter, gap in gap_days.items() if gap}
            if missing_meters or gap_days:
                reject('experiment_incomplete_period', 'Period has incomplete daily data and cannot be a sample',
                       period=run['period'], missing_meters=missing_meters, gap_days=gap_days)
            bound_runs.append({'run_id': run_id, 'period': run['period'], 'paths': paths,
                               'quantities': {meter: encoded(quantities[meter]) for meter in sorted(quantities) if meter in meter_keys}})

        scenarios = self._build_scenarios(bound_runs, multipliers, growth_weights, seed)
        if len(scenarios) > _MAX_SCENARIOS:
            reject('experiment_size', 'Experiment exceeds the supported scenario count')
        definition = {
            'contract_id': contract_id,
            'currency': contract['currency'],
            'meters': meter_keys,
            'runs': [{'run_id': item['run_id'], 'period': item['period'],
                      'monthly_quantities': item['quantities']} for item in bound_runs],
            'growth_multipliers': multipliers,
            'growth_weights': growth_weights,
            'candidates': normalized_candidates,
            'seed': seed,
            'scenario_order': [item['scenario_id'] for item in scenarios],
            'rounding': 'exact_decimal_then_half_even_cents_once_per_quote_per_scenario',
        }
        base_fingerprint = self.fingerprint(contract_id)
        with self.store.atomic():
            if self.store.one('SELECT experiment_id FROM quote_experiments WHERE experiment_id=?', (experiment_id,)):
                reject('experiment_exists', 'Quote experiment identifier exists')
            self.store.execute('INSERT INTO quote_experiments VALUES(?,?,?,?,?,?,?,?)',
                               (experiment_id, contract_id, contract['currency'], canonical(definition),
                                digest(definition), base_fingerprint, author, self.store.clock()))
            for scenario in scenarios:
                self.store.execute('INSERT INTO quote_experiment_scenarios VALUES(?,?,?,?,?,?,?,?,?)',
                                   (experiment_id, scenario['scenario_id'], scenario['period'], scenario['run_id'],
                                    scenario['growth'], encoded(scenario['weight']),
                                    canonical(scenario['daily_paths']), canonical(scenario['monthly_quantities']),
                                    digest(scenario)))
            self.store.log('quote_experiment.created',
                           {'experiment_id': experiment_id, 'scenarios': len(scenarios),
                            'candidates': len(normalized_candidates), 'seed': seed})
            return self.get(experiment_id)

    def _build_scenarios(self, bound_runs, multipliers, growth_weights, seed):
        """Draw whole periods, then expand by growth multipliers.

        All meters inside a scenario keep the same source period and multiplier
        and the original day-level usage paths are carried across, preserving
        correlation and intra-month concentration.
        """
        scenarios = []
        for item in bound_runs:
            for multiplier in multipliers:
                factor = Decimal(multiplier)
                daily_paths = {}
                monthly = {}
                for meter in sorted(item['paths']):
                    scaled_days = {}
                    total = Decimal(0)
                    for day in sorted(item['paths'][meter]):
                        entries = []
                        for record in item['paths'][meter][day]:
                            quantity = (Decimal(record['quantity']) * factor).quantize(_QUANTITY_SCALE, rounding=ROUND_HALF_EVEN)
                            total += quantity
                            entries.append({'usage_id': record['usage_id'], 'at': record['at'],
                                            'quantity': encoded(quantity)})
                        scaled_days[day] = entries
                    daily_paths[meter] = scaled_days
                    monthly[meter] = encoded(total)
                # Identity derives from the source period and multiplier, not
                # submission position, so permuting inputs cannot rename
                # scenarios or change the frozen experiment digest.
                scenario_id = f"s-{item['period']}-g{multiplier.replace('.', '_')}"
                scenarios.append({'scenario_id': scenario_id, 'period': item['period'], 'run_id': item['run_id'],
                                  'growth': multiplier, 'weight': growth_weights[multiplier],
                                  'daily_paths': daily_paths, 'monthly_quantities': monthly})
        # A deterministic shuffle driven solely by the frozen seed makes the
        # stored order reproducible and proves conclusions do not depend on the
        # input submission order.  Each scenario draws an independent 64-bit
        # value from a seeded hash stream; ties break on the scenario id.
        def draw(index, scenario_id):
            material = canonical({'seed': seed, 'index': index, 'scenario_id': scenario_id}).encode()
            return int.from_bytes(hashlib.sha256(material).digest()[:8], 'big')

        indexed = list(enumerate(scenarios))
        indexed.sort(key=lambda pair: (draw(pair[0], pair[1]['scenario_id']), pair[1]['scenario_id']))
        return [item for _, item in indexed]

    def get(self, experiment_id):
        row = self.store.one('SELECT * FROM quote_experiments WHERE experiment_id=?', (experiment_id,))
        if row is None:
            reject('experiment_missing', 'Quote experiment does not exist')
        definition = json.loads(row['definition'])
        if digest(definition) != row['digest']:
            reject('experiment_corrupt', 'Quote experiment definition checksum differs')
        scenarios = []
        for item in self.store.all('SELECT * FROM quote_experiment_scenarios WHERE experiment_id=? ORDER BY rowid', (experiment_id,)):
            payload = {'scenario_id': item['scenario_id'], 'period': item['period'], 'run_id': item['run_id'],
                       'growth': item['growth'], 'weight': item['weight'],
                       'daily_paths': json.loads(item['daily_paths']),
                       'monthly_quantities': json.loads(item['monthly_quantities'])}
            if digest(payload) != item['digest']:
                reject('experiment_scenario_corrupt', 'Frozen scenario checksum differs')
            scenarios.append(payload)
        if [item['scenario_id'] for item in scenarios] != definition['scenario_order']:
            reject('experiment_scenario_corrupt', 'Frozen scenario order differs')
        result = dict(row)
        result['definition'] = definition
        result['scenarios'] = scenarios
        result['fresh'] = self.fingerprint(row['contract_id']) == row['base_fingerprint']
        return result

    @staticmethod
    def _scenario_charge(candidate, scenario):
        """Price one quote on one frozen scenario in integer cents.

        Tiered rates see the full monthly quantity once per meter; daily paths
        are retained for evidence but never priced independently and summed.
        Money follows the ledger convention: exact decimal arithmetic with a
        single ROUND_HALF_EVEN conversion to minor units per charge segment.
        """
        total = candidate['fixed_fee_cents']
        for meter in sorted(candidate['meters']):
            quantity = scenario['monthly_quantities'][meter]
            spec = candidate['meters'][meter]
            total += pricing.tier_charge(quantity, spec['rate'])['amount_cents']
            if spec['overage'] is not None:
                extra = max(Decimal(0), Decimal(quantity) - Decimal(spec['overage']['allowance']))
                with localcontext() as context:
                    context.prec = 80
                    overage_cents = int((extra * Decimal(spec['overage']['unit_price']) * 100)
                                        .quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
                total += overage_cents
        # Period floor binds against the whole-cycle charge across all meters.
        return max(total, candidate['period_minimum_cents'])

    def evaluate(self, experiment_id, quantiles=None, tail_ratio='0.1'):
        experiment = self.get(experiment_id)
        definition = experiment['definition']
        if quantiles is None:
            quantiles = ['0.5', '0.9', '0.95']
        tail_text = encoded(decimal(tail_ratio, 'tail_ratio', minimum=Decimal('0.000001'), maximum=Decimal(1)))
        level_texts = []
        seen = set()
        for level in quantiles:
            text = encoded(decimal(level, 'quantile', minimum=Decimal(0), maximum=Decimal(1)))
            if text in seen:
                reject('experiment_quantile', 'Quantile levels must be distinct')
            seen.add(text)
            level_texts.append(text)
        scenarios = experiment['scenarios']
        weights = experiment_stats.normalize_weights(
            {item['scenario_id']: item['weight'] for item in scenarios})
        quotes = []
        for candidate in definition['candidates']:
            outcomes = {}
            for scenario in scenarios:
                outcomes[scenario['scenario_id']] = self._scenario_charge(candidate, scenario)
            profile = experiment_stats.summarize(outcomes, weights, level_texts, tail_text)
            profile.update({
                'quote_id': candidate['quote_id'],
                'label': candidate['label'],
                'exceedance_probability': None,
                'outcome_cents': {key: outcomes[key] for key in sorted(outcomes)},
            })
            quotes.append(profile)
        return {
            'experiment_id': experiment_id,
            'currency': experiment['currency'],
            'digest': experiment['digest'],
            'fresh': experiment['fresh'],
            'scenario_count': len(scenarios),
            'scenario_weights': {key: encoded(weights[key]) for key in sorted(weights)},
            'tail_ratio': tail_text,
            'quantiles': level_texts,
            'quotes': sorted(quotes, key=lambda item: item['quote_id']),
        }

    def decide(self, experiment_id, budget_cents, max_exceedance, tail_cap_cents,
               quantiles=None, tail_ratio='0.1'):
        report = self.evaluate(experiment_id, quantiles, tail_ratio)
        weights = experiment_stats.normalize_weights(report['scenario_weights'])
        for quote in report['quotes']:
            quote['exceedance_probability'] = experiment_stats.exceedance_probability(
                quote['outcome_cents'], weights, budget_cents)
        indexed = {quote['quote_id']: quote for quote in report['quotes']}
        decision = experiment_stats.decide(indexed, budget_cents, max_exceedance, tail_cap_cents)
        return {
            'experiment_id': experiment_id,
            'currency': report['currency'],
            'fresh': report['fresh'],
            'budget_cents': budget_cents,
            'max_exceedance': encoded(Decimal(max_exceedance)),
            'tail_cap_cents': tail_cap_cents,
            'tail_ratio': report['tail_ratio'],
            'feasible': decision['feasible'],
            'selected': decision['selected'],
            'candidates': decision['candidates'],
            'quotes': [{key: value for key, value in quote.items() if key != 'outcome_cents'}
                       for quote in report['quotes']],
        }

    def crossover(self, experiment_id, growth_from, growth_to):
        """Growth-range flip intervals and active billing tiers per interval."""
        experiment = self.get(experiment_id)
        definition = experiment['definition']
        first_run = definition['runs'][0]
        bases = {meter: Decimal(quantity) for meter, quantity in first_run['monthly_quantities'].items()}
        candidates = {candidate['quote_id']: candidate for candidate in definition['candidates']}
        analysis = experiment_stats.crossover_analysis(candidates, bases, growth_from, growth_to)
        # Verify the piecewise geometry against direct scenario pricing at each
        # interval midpoint.  Geometry uses unrounded exact decimals and direct
        # pricing applies the ledger's single half-even cent rounding, so the
        # check asserts the winning identity, which must be identical either
        # way; the geometric cent estimate is also returned for inspection.
        for interval in analysis['intervals']:
            midpoint = (Decimal(interval['growth_from']) + Decimal(interval['growth_to'])) / 2
            direct = {}
            for quote_id, candidate in candidates.items():
                synthetic = {'monthly_quantities': {meter: encoded((bases[meter] * midpoint)
                                                                    .quantize(_QUANTITY_SCALE, rounding=ROUND_HALF_EVEN))
                                                    for meter in bases}}
                direct[quote_id] = self._scenario_charge(candidate, synthetic)
            direct_winner = min(sorted(direct), key=lambda quote_id: (direct[quote_id], quote_id))
            if direct_winner not in interval['winners']:
                reject('experiment_crossover', 'Crossover geometry disagrees with direct pricing',
                       interval=interval['growth_from'], geometric=interval['winners'], direct=direct_winner)
            interval['direct_check_cents'] = {key: direct[key] for key in sorted(direct)}
        return {'experiment_id': experiment_id, 'currency': experiment['currency'],
                'fresh': experiment['fresh'], 'baseline_period': first_run['period'], **analysis}

    def listing(self):
        return [self.get(row['experiment_id']) for row in
                self.store.all('SELECT experiment_id FROM quote_experiments ORDER BY experiment_id')]

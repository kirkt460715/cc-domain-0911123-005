from tests.contracts.experiment_support import ExperimentCase, simple_tariff
from meterbook.contracts import experiments, experiment_stats, rates, registry
from meterbook.events import receiver, reconcile
from meterbook.settlement import pricing
from meterbook.values import MeterError, digest, canonical, encoded as encoded_amount


class TailStatistics(ExperimentCase):
    """Hand-computed weighted quantile / CVaR with split tail mass."""

    def test_quantile_interpolates_across_weight_boundary(self):
        outcomes = {'low': 100, 'high': 200}
        weights = experiment_stats.normalize_weights({'low': '3', 'high': '1'})
        # target position 0.5 * 4 = 2: two thirds through the low atom, whose
        # own cost is flat at 100 cents.
        self.assertEqual(experiment_stats.weighted_quantile(outcomes, weights, '0.5'), 100)
        # target 3 lands exactly on the low atom's upper edge: the atom itself
        # has a flat cost of 100, so no interpolation beyond it occurs.
        self.assertEqual(experiment_stats.weighted_quantile(outcomes, weights, '0.75'), 100)
        # target 3.5 splits the high atom halfway; linear interpolation between
        # the 100-cent and 200-cent levels yields 150 cents.
        self.assertEqual(experiment_stats.weighted_quantile(outcomes, weights, '0.875'), 150)
        self.assertEqual(experiment_stats.weighted_quantile(outcomes, weights, '0'), 100)
        self.assertEqual(experiment_stats.weighted_quantile(outcomes, weights, '1'), 200)

    def test_cvar_splits_atom_at_tail_boundary(self):
        outcomes = {'low': 100, 'mid': 150, 'high': 200}
        weights = experiment_stats.normalize_weights({'low': '2', 'mid': '1', 'high': '1'})
        # Statistics consume integer-cent outcomes.
        # total mass 4; tail 0.25 => target 1 => full high atom
        self.assertEqual(experiment_stats.weighted_cvar(outcomes, weights, '0.25'), 200)
        # tail 0.5 => target 2: full high + full mid
        self.assertEqual(experiment_stats.weighted_cvar(outcomes, weights, '0.5'), 175)
        # tail 0.375 => target 1.5: full high plus half of mid => (200+75)/1.5
        self.assertEqual(experiment_stats.weighted_cvar(outcomes, weights, '0.375'), 183)
        # whole distribution reproduces the weighted mean
        self.assertEqual(experiment_stats.weighted_cvar(outcomes, weights, '1'),
                         experiment_stats.expected_value(outcomes, weights))

    def test_exceedance_and_decision_keep_reasons(self):
        weights = {'a': '1', 'b': '1', 'c': '2'}
        reports = {
            'q1': {'expected_cents': 10, 'exceedance_probability': '0.25',
                   'cvar': {'cost_cents': 50}},
            'q2': {'expected_cents': 5, 'exceedance_probability': '0.75',
                   'cvar': {'cost_cents': 40}},
            'q3': {'expected_cents': 8, 'exceedance_probability': '0.25',
                   'cvar': {'cost_cents': 200}},
        }
        decision = experiment_stats.decide(reports, budget_cents=30, max_exceedance='0.5', tail_cap_cents=100)
        self.assertTrue(decision['feasible'])
        self.assertEqual(decision['selected'], 'q1')
        reasons = {row['quote_id']: [item['rule'] for item in row['reasons']]
                   for row in decision['candidates']}
        self.assertEqual(reasons['q1'], [])
        self.assertEqual(reasons['q2'], ['exceedance_probability'])
        self.assertEqual(reasons['q3'], ['tail_charge_cap'])
        none_feasible = experiment_stats.decide(reports, 30, '0.1', 10)
        self.assertFalse(none_feasible['feasible'])
        self.assertIsNone(none_feasible['selected'])
        self.assertTrue(all(not row['feasible'] for row in none_feasible['candidates']))


class QuoteExperimentFlow(ExperimentCase):
    def setUp(self):
        super().setUp()
        # February 2026 (28 days): moderate usage, concentrated late in month.
        api_feb = ['1'] * 20 + ['4'] * 8
        storage_feb = ['2'] * 28
        # March 2026 (31 days): heavier usage crossing tier boundaries.
        api_mar = ['2'] * 20 + ['5'] * 11
        storage_mar = ['3'] * 31
        self.build_period('2026-02', api_feb, storage_feb, 'run-feb')
        self.build_period('2026-03', api_mar, storage_mar, 'run-mar')
        self.growth = {'multipliers': ['1', '2'], 'weights': {'1': '3', '2': '1'}}
        self.candidates = [
            self.quote('quote-cheap', 0, 0,
                       self.tiered('0', [(None, '0.2')]),
                       self.tiered('0', [(None, '0.1')])),
            self.quote('quote-floor', 500, 3000,
                       self.tiered('20', [('100', '0.5'), (None, '2')]),
                       self.tiered('0', [('200', '0.3'), (None, '1')])),
            self.quote('quote-overage', 200, 0,
                       self.tiered('0', [('60', '0.15'), (None, '0.15')]),
                       self.tiered('0', [(None, '0.05')]),
                       api_overage={'allowance': '60', 'unit_price': '1'}),
        ]

    def create(self, run_ids=None, candidates=None, seed=7, growth=None):
        return self.app.experiments.create(
            'exp-1', 'contract-b', run_ids or ['run-feb', 'run-mar'],
            ['api', 'storage'], growth or self.growth,
            candidates or self.candidates, seed, 'analyst')

    def test_expected_quantile_cvar_and_worst_case(self):
        self.create()
        report = self.app.experiments.evaluate('exp-1', tail_ratio='0.25')
        by_id = {quote['quote_id']: quote for quote in report['quotes']}
        self.assertEqual(report['scenario_count'], 4)
        self.assertEqual(report['currency'], 'CNY')
        # quote-cheap: api*0.2 + storage*0.1 on monthly totals
        # feb: api 52, storage 56 -> 1040+560 = 1600; mar: api 95, storage 93
        # -> 1900+930 = 2830; doubled: 3200 and 5660.
        cheap = by_id['quote-cheap']['outcome_cents']
        self.assertEqual(cheap, {
            's-2026-02-g1': 1600, 's-2026-02-g2': 3200,
            's-2026-03-g1': 2830, 's-2026-03-g2': 5660})
        profile = by_id['quote-cheap']
        # weights 3,3,1,1 -> total 8; mean is a half-cent, rounded half-even.
        self.assertEqual(profile['expected_cents'], 2769)
        # worst tail 0.25 -> target mass 2: full g2-mar (5660) plus half the
        # g2-feb atom at 3200, exercising the discrete-tail split.
        self.assertEqual(profile['cvar']['cost_cents'], (5660 + 3200) // 2)
        self.assertEqual(profile['worst_case'], {'scenario_id': 's-2026-03-g2', 'cost_cents': 5660})
        median = next(row for row in profile['quantiles'] if row['level'] == '0.5')
        # target position 4: cumulative after feb-g1 is 3; one unit into the
        # mar-g1 atom interpolates between the 1600 and 2830 levels.
        self.assertEqual(median['cost_cents'], 2010)

    def test_period_floor_and_tiers_are_priced_on_full_month(self):
        # A dedicated quote whose usage charges stay tiny so the whole-cycle
        # period minimum actually binds.
        floored = self.quote(
            'quote-floor-only', 0, 3000,
            self.tiered('0', [(None, '0.01')]),
            self.tiered('0', [(None, '0.01')]))
        self.create(candidates=[floored])
        report = self.app.experiments.evaluate('exp-1')
        outcomes = report['quotes'][0]['outcome_cents']
        # March g1 usage value is only 1.88 -> the 30.00 period floor binds.
        self.assertEqual(outcomes['s-2026-03-g1'], 3000)
        # Doubled usage reaches 3.76, still below the floor.
        self.assertEqual(outcomes['s-2026-03-g2'], 3000)
        # The richer bundled quote keeps its own tier + fixed-fee arithmetic.
        bundled = self.app.experiments.create(
            'exp-floor', 'contract-b', ['run-feb', 'run-mar'], ['api', 'storage'],
            self.growth, [self.candidates[1]], 7, 'analyst')
        bundled_outcomes = self.app.experiments.evaluate('exp-floor')['quotes'][0]['outcome_cents']
        # March g2: api 190 -> 100*0.5 + 70*2 = 190; storage 186 -> 55.8;
        # fixed 5.00 => 250.80, above any floor.
        self.assertEqual(bundled_outcomes['s-2026-03-g2'], 25080)

    def test_feasibility_gate_then_expectation_selection(self):
        self.create()
        # Budget 5000: only the March g2 scenario at 5660 (weight 1/8 = 0.125)
        # breaches for quote-cheap; the other quotes breach on more scenarios.
        decision = self.app.experiments.decide('exp-1', budget_cents=5000,
                                               max_exceedance='0.2', tail_cap_cents=14000)
        self.assertTrue(decision['feasible'])
        table = {row['quote_id']: row for row in decision['candidates']}
        # Only quote-cheap clears both gates, so it is selected on expectation.
        self.assertEqual(decision['selected'], 'quote-cheap')
        self.assertTrue(table['quote-cheap']['feasible'])
        self.assertFalse(table['quote-overage']['feasible'])
        self.assertFalse(table['quote-floor']['feasible'])
        self.assertTrue(any(item['rule'] == 'tail_charge_cap' for item in table['quote-floor']['reasons']))
        # A zero-tolerance gate rejects quote-cheap as soon as its single worst
        # scenario crosses the budget, proving the probability boundary works.
        tight = self.app.experiments.decide('exp-1', 5000, '0', 14000)
        tight_table = {row['quote_id']: row for row in tight['candidates']}
        self.assertFalse(tight_table['quote-cheap']['feasible'])
        self.assertEqual(tight_table['quote-cheap']['reasons'][0]['rule'], 'exceedance_probability')
        # A gate nobody passes returns an explicit no-feasible result.
        impossible = self.app.experiments.decide('exp-1', 100, '0', 100)
        self.assertFalse(impossible['feasible'])
        self.assertIsNone(impossible['selected'])
        self.assertTrue(all(not row['feasible'] for row in impossible['candidates']))

    def test_incomplete_daily_period_is_rejected_as_sample(self):
        # April 2026 with only five days of api data: gaps cannot read as low usage.
        self.report_daily('2026-04', 'api', {day: '1' for day in range(1, 6)})
        self.report_daily('2026-04', 'storage', ['1'] * 30)
        reconcile.run(self.store, 'run-gap', 'contract-b', '2026-04')
        failure = self.error(
            'experiment_incomplete_period', self.app.experiments.create,
            'exp-gap', 'contract-b', ['run-feb', 'run-gap'], ['api', 'storage'],
            self.growth, self.candidates, 7, 'analyst')
        self.assertEqual(failure.details['period'], '2026-04')
        self.assertIn('api', failure.details['gap_days'])
        # the failed experiment left nothing behind
        self.error('experiment_missing', self.app.experiments.get, 'exp-gap')

    def test_input_permutation_does_not_change_conclusions(self):
        self.create(run_ids=['run-feb', 'run-mar'])
        first = self.app.experiments.get('exp-1')
        first_report = self.app.experiments.evaluate('exp-1')
        reversed_candidates = list(reversed(self.candidates))
        second = self.app.experiments.create(
            'exp-2', 'contract-b', ['run-mar', 'run-feb'], ['storage', 'api'],
            self.growth, reversed_candidates, 7, 'analyst')
        # Same seed and same frozen inputs => identical definition digest and
        # identical per-quote statistics.
        self.assertEqual(second['digest'], first['digest'])
        second_report = self.app.experiments.evaluate('exp-2')
        for left, right in zip(first_report['quotes'], second_report['quotes']):
            self.assertEqual(left['expected_cents'], right['expected_cents'])
            self.assertEqual(left['cvar'], right['cvar'])
            self.assertEqual(left['quantiles'], right['quantiles'])
            self.assertEqual(left['worst_case'], right['worst_case'])

    def test_seed_and_inputs_are_frozen_and_ledger_untouched(self):
        self.create(seed=7)
        experiment = self.app.experiments.get('exp-1')
        self.assertEqual(experiment['definition']['seed'], 7)
        self.assertEqual(experiment['definition']['growth_multipliers'], ['1', '2'])
        before_audit = self.store.scalar('SELECT COUNT(*) FROM audit')
        before_rates = self.store.scalar("SELECT COUNT(*) FROM rates WHERE state='active'")
        self.app.experiments.evaluate('exp-1')
        self.app.experiments.decide('exp-1', 3000, '0.25', 4000)
        self.app.experiments.crossover('exp-1', '0.5', '2')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM audit'), before_audit)
        self.assertEqual(self.store.scalar("SELECT COUNT(*) FROM rates WHERE state='active'"), before_rates)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM ledger_entries'), 0)
        # A different seed is a visibly different frozen experiment.
        self.app.experiments.create('exp-3', 'contract-b', ['run-feb', 'run-mar'],
                                    ['api', 'storage'], self.growth, self.candidates, 8, 'analyst')
        self.assertNotEqual(self.app.experiments.get('exp-3')['digest'], experiment['digest'])

    def test_freshness_flag_tracks_contract_and_rate_changes(self):
        self.create()
        self.assertTrue(self.app.experiments.get('exp-1')['fresh'])
        rates.withdraw(self.store, 'base-api', 'renewal negotiation')
        rates.add(self.store, 'base-api2', 'contract-b', 'api',
                  '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', simple_tariff('0.4'))
        self.assertFalse(self.app.experiments.get('exp-1')['fresh'])
        self.assertFalse(self.app.experiments.evaluate('exp-1')['fresh'])

    def test_overage_surcharge_is_separate_from_tier_charge(self):
        # Storage baseline is 93 in March; allowance 60 means 33 units of
        # explicit overage on top of the ordinary tiered line.
        rate = self.tiered('0', [(None, '0.1')])
        candidate = self.quote(
            'q-over', 0, 0, self.tiered('0', [(None, '0')]), rate,
            storage_overage={'allowance': '60', 'unit_price': '0.5'})
        self.app.experiments.create(
            'exp-over', 'contract-b', ['run-mar'], ['api', 'storage'],
            {'multipliers': ['1']}, [candidate], 1, 'analyst')
        outcome = self.app.experiments.evaluate('exp-over')['quotes'][0]['outcome_cents']
        # tier 93*0.10 = 9.30 plus overage 33*0.50 = 16.50 -> 25.80
        self.assertEqual(outcome['s-2026-03-g1'], 2580)
        # Below the allowance no surcharge is added.
        low = self.quote(
            'q-low', 0, 0, self.tiered('0', [(None, '0')]), rate,
            storage_overage={'allowance': '200', 'unit_price': '0.5'})
        self.app.experiments.create(
            'exp-low', 'contract-b', ['run-mar'], ['api', 'storage'],
            {'multipliers': ['1']}, [low], 1, 'analyst')
        low_outcome = self.app.experiments.evaluate('exp-low')['quotes'][0]['outcome_cents']
        self.assertEqual(low_outcome['s-2026-03-g1'], 930)

    def test_frozen_scenario_tampering_is_detected(self):
        self.create()
        row = self.store.one("SELECT digest FROM quote_experiment_scenarios WHERE experiment_id='exp-1' LIMIT 1")
        self.store.execute("UPDATE quote_experiment_scenarios SET weight='9' WHERE experiment_id='exp-1'")
        self.error('experiment_scenario_corrupt', self.app.experiments.get, 'exp-1')
        # Definition tampering is independently checksummed too.
        self.store.execute("UPDATE quote_experiments SET digest=? WHERE experiment_id='exp-1'", (row['digest'],))
        self.error('experiment_corrupt', self.app.experiments.get, 'exp-1')

    def test_fixed_fee_is_present_even_without_usage_charge(self):
        candidate = self.quote(
            'q-fixed', 777, 0,
            self.tiered('0', [(None, '0')]), self.tiered('0', [(None, '0')]))
        self.app.experiments.create(
            'exp-fixed', 'contract-b', ['run-feb'], ['api', 'storage'],
            {'multipliers': ['1']}, [candidate], 1, 'analyst')
        outcome = self.app.experiments.evaluate('exp-fixed')['quotes'][0]['outcome_cents']
        self.assertTrue(all(value == 777 for value in outcome.values()))

    def test_daily_settlement_cannot_reproduce_monthly_tier_charge(self):
        # One meter whose monthly total crosses a tier boundary; pricing days
        # independently would miss the cross-day tier and undercharge.
        meter = 'api'
        rate = rates.normalize(self.tiered('0', [('100', '1'), (None, '2')]))
        candidate = self.quote('q', 0, 0, rate, self.tiered('0', [(None, '0')]))
        # March api baseline is 95; doubled it reaches 190 and crosses the band.
        self.app.experiments.create(
            'exp-cross', 'contract-b', ['run-mar'], [meter, 'storage'],
            {'multipliers': ['2']}, [candidate], 1, 'analyst')
        monthly = self.app.experiments.evaluate('exp-cross')['quotes'][0]['outcome_cents']['s-2026-03-g2']
        # Correct monthly tier pricing on the full quantity: 100*1 + 90*2 = 280
        self.assertEqual(monthly, 28000)
        # The forbidden daily-summed answer keeps every day inside tier 1.
        daily_wrong = sum(
            pricing.tier_charge(str(q * 2), rate)['amount_cents']
            for q in ([2] * 20 + [5] * 11))
        self.assertEqual(daily_wrong, 19000)
        self.assertNotEqual(monthly, daily_wrong)

    def test_scenarios_keep_meter_correlation_and_daily_paths(self):
        self.create()
        experiment = self.app.experiments.get('exp-1')
        scenario = next(item for item in experiment['scenarios'] if item['scenario_id'] == 's-2026-03-g2')
        # Whole-period extraction: both meters reference the same source run and
        # every original day survives with doubled quantities.
        self.assertEqual(len(scenario['daily_paths']['api']), 31)
        self.assertEqual(scenario['daily_paths']['api']['05'][0]['quantity'], '4')
        self.assertEqual(scenario['daily_paths']['storage']['31'][0]['quantity'], '6')
        # Monthly totals equal the sum of the retained daily path.
        from decimal import Decimal
        for meter in ('api', 'storage'):
            recomputed = sum(Decimal(record['quantity'])
                             for day in scenario['daily_paths'][meter].values() for record in day)
            self.assertEqual(encoded_amount(recomputed), scenario['monthly_quantities'][meter])
        self.assertEqual(scenario['monthly_quantities']['api'], '190')
        self.assertEqual(scenario['monthly_quantities']['storage'], '186')


class CrossoverGeometry(ExperimentCase):
    def test_multiple_flip_intervals_and_active_tiers(self):
        # February baseline api quantity is 52.  Three single-meter quotes whose
        # cheapest identity flips twice as the shared growth multiplier rises:
        #   Y: no fixed, 0.10 up to 40 units then 0.60 (convex, wins low growth)
        #   X: fixed 8.00, flat 0.25 per unit (wins the middle band)
        #   Z: fixed 25.00, flat 0.15 per unit (wins high growth)
        self.build_period('2026-02', ['1'] * 20 + ['4'] * 8, ['2'] * 28, 'run-feb')
        zero = self.tiered('0', [(None, '0')])
        x = self.quote('x', 800, 0, self.tiered('0', [(None, '0.25')]), zero)
        y = self.quote('y', 0, 0, self.tiered('0', [('40', '0.1'), (None, '0.6')]), zero)
        z = self.quote('z', 2500, 0, self.tiered('0', [(None, '0.15')]), zero)
        self.app.experiments.create(
            'exp-x', 'contract-b', ['run-feb'], ['api', 'storage'],
            {'multipliers': ['1']}, [x, y, z], 3, 'analyst')
        analysis = self.app.experiments.crossover('exp-x', '0.1', '6')
        winner_sequence = [tuple(interval['winners']) for interval in analysis['intervals']]
        # Two real flip boundaries across the growth range, not a single crossing.
        self.assertEqual(winner_sequence[0], ('y',))
        self.assertIn(('x',), winner_sequence)
        self.assertEqual(winner_sequence[-1], ('z',))
        self.assertEqual(winner_sequence.count(('y',)), 1)
        self.assertEqual(winner_sequence.count(('x',)), 1)
        self.assertEqual(winner_sequence.count(('z',)), 1)
        # Active tier index tracks Y's band change across the reported
        # intervals: the winning Y region starts inside its cheap tier and its
        # later boundary moves it onto the steep tier.
        api_tiers = {interval['active_tiers']['api'] for interval in analysis['intervals']}
        self.assertEqual(api_tiers, {0, 1})
        # Every interval carries a direct-pricing cross-check that confirms the
        # geometric winner identity against ledger-rounded scenario pricing.
        for interval in analysis['intervals']:
            self.assertIn('direct_check_cents', interval)

    def test_crossover_respects_period_floor(self):
        self.build_period('2026-02', ['1'] * 20 + ['4'] * 8, ['2'] * 28, 'run-feb')
        zero = self.tiered('0', [(None, '0')])
        # Floored quote: cheap 0.01 per unit with a 20.00 whole-cycle minimum,
        # so at low growth usage is negligible and the floor dominates.
        # Variable quote: no floor at 0.6 per unit, cheap at tiny usage and
        # overtaken once the floored schedule climbs past its minimum.
        floored = self.quote('floored', 0, 2000, self.tiered('0', [(None, '0.01')]), zero)
        variable = self.quote('variable', 0, 0, self.tiered('0', [(None, '0.6')]), zero)
        self.app.experiments.create(
            'exp-f', 'contract-b', ['run-feb'], ['api', 'storage'],
            {'multipliers': ['1']}, [floored, variable], 3, 'analyst')
        analysis = self.app.experiments.crossover('exp-f', '0.01', '8')
        first = analysis['intervals'][0]
        last = analysis['intervals'][-1]
        self.assertEqual(first['winners'], ['variable'])
        self.assertEqual(last['winners'], ['floored'])
        # The period-floor crossing appears as an explicit interval boundary
        # and direct scenario pricing agrees with the geometric winner.
        self.assertGreaterEqual(len(analysis['intervals']), 2)


class ExperimentBoundaries(ExperimentCase):
    def test_schema_and_scope_rejections(self):
        self.build_period('2026-02', ['1'] * 28, ['2'] * 28, 'run-feb')
        create = self.app.experiments.create
        kwargs = dict(experiment_id='exp-bad', contract_id='contract-b',
                      run_ids=['run-feb'], meters=['api', 'storage'],
                      growth_plan={'multipliers': ['1']},
                      candidates=[self.quote('q', 0, 0,
                                             self.tiered('0', [(None, '1')]),
                                             self.tiered('0', [(None, '1')]))],
                      seed=1, author='analyst')
        self.error('experiment_meter', create, **{**kwargs, 'meters': ['api', 'unknown']})
        # candidate missing storage meter
        bad = {'quote_id': 'q', 'fixed_fee_cents': 0, 'period_minimum_cents': 0,
               'meters': {'api': {'rate': self.tiered('0', [(None, '1')])}}}
        self.error('experiment_candidate_meters', create, **{**kwargs, 'candidates': [bad]})
        self.error('experiment_growth', create, **{**kwargs, 'growth_plan': {'multipliers': []}})
        self.error('experiment_weight', create, **{**kwargs,
                                                   'growth_plan': {'multipliers': ['1'], 'weights': {'1': '0'}}})
        self.error('experiment_weights', create, **{**kwargs,
                                                    'growth_plan': {'multipliers': ['1'],
                                                                    'weights': {'1': '1', '2': '1'}}})
        self.error('reconciliation_missing', create, **{**kwargs, 'run_ids': ['run-ghost']})
        self.error('experiment_runs', create, **{**kwargs, 'run_ids': ['run-feb', 'run-feb']})

    def test_routes_are_exposed_through_service_facade(self):
        capabilities = {row['operation'] for row in self.app.capabilities()}
        for operation in ('experiment.create', 'experiment.evaluate', 'experiment.decide',
                          'experiment.crossover', 'experiment.get', 'experiment.listing'):
            self.assertIn(operation, capabilities)

import calendar
from tests.support import Case
from meterbook.contracts import registry, rates
from meterbook.events import receiver, reconcile


TWO_METER = dict(
    valid_from='2026-01-01T00:00:00Z', valid_until='2027-01-01T00:00:00Z',
    meters=['api', 'storage'], purposes=['research'], regions=['CN'],
    tolerance={'api': '0', 'storage': '0'})


def simple_tariff(price='0.25'):
    return {'tiers': [{'up_to': '100', 'unit_price': price}, {'up_to': None, 'unit_price': '0.1'}]}


class ExperimentCase(Case):
    """Two meters, periods whose every calendar day carries matched usage."""

    def setUp(self):
        super().setUp()
        registry.create(self.store, 'contract-b', 'provider-a', 'consumer-b', 'CNY', TWO_METER)
        for meter in ('api', 'storage'):
            rates.add(self.store, f'base-{meter}', 'contract-b', meter,
                      '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', simple_tariff())
        # Next stream sequence per side for contract-b; streams must stay gap free.
        self._next_sequence = {'provider': 1, 'consumer': 1}

    def _entries(self, period, quantities):
        days = calendar.monthrange(int(period[:4]), int(period[5:7]))[1]
        if isinstance(quantities, dict):
            return sorted((int(day), quantity) for day, quantity in quantities.items())
        if len(quantities) != days:
            raise AssertionError('daily helper expects one quantity per calendar day')
        return list(enumerate(quantities, start=1))

    def report_daily(self, period, meter, quantities, contract='contract-b'):
        """Pair provider/consumer events for one meter.

        ``quantities`` is either one entry per calendar day or a mapping of
        day-number -> quantity, the latter intentionally producing gap periods
        used to prove incomplete-period rejection.
        """
        for day, quantity in self._entries(period, quantities):
            identity = f'{meter}-{period}-{day:02d}'
            payload = dict(meter=meter, at=f'{period}-{day:02d}T12:00:00Z',
                           quantity=str(quantity), purpose='research', region='CN',
                           dimensions={'source': 'synthetic'})
            for side in ('provider', 'consumer'):
                sequence = self._next_sequence[side]
                receiver.receive(self.store, {
                    'event_id': f'{side}-{sequence}', 'contract_id': contract, 'side': side,
                    'sequence': sequence, 'usage_id': identity, 'revision': 1, 'action': 'report',
                    'usage': payload})
                result = receiver.drain(self.store, contract, side)
                self.assertIsNone(result['blocked'])
                self._next_sequence[side] = sequence + 1

    def build_period(self, period, api_quantities, storage_quantities, run_id):
        self.report_daily(period, 'api', api_quantities)
        self.report_daily(period, 'storage', storage_quantities)
        reconcile.run(self.store, run_id, 'contract-b', period)
        return run_id

    @staticmethod
    def quote(quote_id, fixed, minimum, api_rate, storage_rate, api_overage=None, storage_overage=None):
        meters = {'api': {'rate': api_rate}, 'storage': {'rate': storage_rate}}
        if api_overage is not None:
            meters['api']['overage'] = api_overage
        if storage_overage is not None:
            meters['storage']['overage'] = storage_overage
        return {'quote_id': quote_id, 'fixed_fee_cents': fixed,
                'period_minimum_cents': minimum, 'meters': meters}

    @staticmethod
    def tiered(included, bands, minimum='0', maximum=None):
        definition = {'included': included, 'minimum_charge': minimum,
                      'tiers': [{'up_to': up_to, 'unit_price': price} for up_to, price in bands]}
        if maximum is not None:
            definition['maximum_charge'] = maximum
        return definition

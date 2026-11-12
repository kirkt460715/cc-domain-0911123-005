import json
from pathlib import Path

def load():
    return json.loads((Path(__file__).with_name("context.json")).read_text())

def run():
    from decimal import Decimal
    import calendar
    from meterbook.service import Meterbook
    from meterbook.cli import replay
    data = load()
    assert sum(Decimal(r['weight']) for r in data['growth']) == 1
    for period in data['periods']:
        year, month = map(int, period['period'].split('-'))
        assert {r['day'] for r in period['days']} == set(range(1,calendar.monthrange(year,month)[1]+1))
        assert all(Decimal(r['api']) >= 0 and Decimal(r['bytes']) >= 0 for r in period['days'])
    fixture = json.loads((Path(__file__).parents[1] / 'fixtures/settlement.json').read_text())
    with Meterbook() as app:
        result = replay(app, fixture)
        return {'ok': result['ok'], 'records': sum(len(p['days']) for p in data['periods']), 'ledger_steps': len(result['steps'])}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))

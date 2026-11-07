import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))


class ContractTests(unittest.TestCase):
    def test_reject_remote_database_and_live_default_off(self):
        from local_app.config import load_settings
        from local_app.models import AppError
        root = Path(__file__).resolve().parents[1]
        self.assertFalse(load_settings(root).allow_live)
        for uri in ('mongodb://example.com/', 'mongodb://127.0.0.1,example.com/', 'mongodb+srv://localhost/'):
            with self.subTest(uri=uri), self.assertRaises(AppError):
                load_settings(root, {'mongo_uri': uri})

    def test_five_unique_catalog_seats_and_strict_times(self):
        from local_app.models import validate_plan, AppError
        from local_app.catalog import load_catalog
        catalog = load_catalog(Path(__file__).resolve().parents[1] / 'AL-BackEnd/座位信息')
        self.assertGreater(len(catalog), 2000)
        seats = list(catalog)[:6]
        data = dict(seat_ids=seats[:5], day_offset=1, mode='day', begin='09:00', end='12:00', trigger='20:00:00', enabled=True)
        self.assertEqual(len(validate_plan(data, catalog).seat_ids), 5)
        for changes in ({'seat_ids': seats}, {'seat_ids': [seats[0]]*2}, {'begin': '9:00'}, {'end': '08:00'}, {'enabled': 'false'}, {'trigger': '25:00:00'}):
            with self.subTest(changes=changes), self.assertRaises(AppError):
                validate_plan(data | changes, catalog)

    def test_week_uses_target_day_across_midnight(self):
        from local_app.models import validate_plan, resolve_run
        from local_app.catalog import load_catalog
        catalog = load_catalog(Path(__file__).resolve().parents[1] / 'AL-BackEnd/座位信息')
        p = validate_plan(dict(seat_ids=[next(iter(catalog))], mode='week', day_offset=1, begin='09:00', end='12:00', trigger='23:59:59', enabled=True, week_times={'1': ['10:00', '13:00']}), catalog)
        run = resolve_run('a', 2, p, date(2026, 10, 18))
        self.assertEqual((run.target_date, run.begin, run.end), ('2026-10-19', '10:00', '13:00'))
        self.assertIsNone(resolve_run('a', 2, p, date(2026, 10, 19)))

    def test_month_and_year_rollover(self):
        from local_app.models import Plan, resolve_run
        p = Plan(('1',), 2, 'day', '09:00', '12:00', {}, '20:00:00', True)
        self.assertEqual(resolve_run('a', 1, p, date(2026, 12, 31)).target_date, '2027-01-02')


if __name__ == '__main__': unittest.main()

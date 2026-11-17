import importlib, pathlib, sqlite3,sys,unittest
R=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R/'files'))
sys.path.insert(0,str(R/'tests'))
from test_v5 import TestV5,AT,KICK
import oracle_v5 as v

class TestKickoffRepair(unittest.TestCase):
    setUp=TestV5.setUp
    tearDown=TestV5.tearDown
    def test_unmatched_later_matched_refreshes_kickoff(self):
        # First import without the root event metadata. The source forecast has
        # only the calendar day and gets persisted with NULL kickoff_utc.
        self.c.execute('DELETE FROM fb_full_events')
        v.ingest_catalog(self.c,AT,force=True)
        v.refresh_predictions(self.c,AT)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM v5_forecasts WHERE origin='sf_forecasts' AND kickoff_utc IS NULL").fetchone()[0],2)
        self.c.execute('INSERT INTO fb_full_events VALUES (?,?,?,?,?,?,?,?,?)',('1001','1001','football','Germany','Майнц','Вердер',KICK,'line',1))
        v.ingest_catalog(self.c,AT,force=True)
        summary=v.refresh_predictions(self.c,AT)
        self.assertGreaterEqual(summary['verified_events'],1)
        rows=self.c.execute("SELECT kickoff_utc,root_id FROM v5_forecasts WHERE origin='sf_forecasts'").fetchall()
        self.assertEqual(rows,[(KICK,'1001'),(KICK,'1001')])
        st=v.status(self.c)
        self.assertEqual(st['predictions']['linked_forecast_records_missing_kickoff'],0)
        self.assertEqual(st['predictions']['upcoming_root_events_with_forecasts'],1)
        self.assertGreaterEqual(st['predictions']['upcoming_root_events_in_catalog'],1)
    def test_followup_refresh_repairs_old_null_timestamp_on_attached_rows(self):
        v.ingest_catalog(self.c,AT,force=True)
        v.refresh_predictions(self.c,AT)
        self.c.execute("UPDATE v5_forecasts SET kickoff_utc=NULL WHERE origin='sf_forecasts'")
        self.assertEqual(v.status(self.c)['predictions']['linked_forecast_records_missing_kickoff'],2)
        before=v.doctor(self.c)
        self.assertIn('FORECAST_KICKOFF_SYNC',before['problems'])
        v.refresh_predictions(self.c,AT)
        self.assertEqual(v.status(self.c)['predictions']['linked_forecast_records_missing_kickoff'],0)
        self.assertNotIn('FORECAST_KICKOFF_SYNC',v.doctor(self.c)['problems'])
    def test_kickoff_repair_does_not_modify_immutable_forecast_identity(self):
        v.ingest_catalog(self.c,AT,force=True)
        v.refresh_predictions(self.c,AT)
        self.c.execute("UPDATE v5_forecasts SET kickoff_utc=NULL WHERE origin='sf_forecasts'")
        before=self.c.execute("SELECT id,created_at,probability,model,market,selection FROM v5_forecasts WHERE origin='sf_forecasts' ORDER BY id").fetchall()
        v.refresh_predictions(self.c,AT)
        after=self.c.execute("SELECT id,created_at,probability,model,market,selection FROM v5_forecasts WHERE origin='sf_forecasts' ORDER BY id").fetchall()
        self.assertEqual(before,after)

# Don't run inherited TestV5 suite twice in discovery mode.
# Tests above depend only on the shared fixture setUp() and tearDown().

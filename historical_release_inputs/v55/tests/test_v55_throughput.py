import sqlite3,sys,unittest,datetime as dt
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'files'));sys.path.insert(0,str(ROOT/'tests'/'fixtures'))
import oracle_v5 as v
AT='2026-10-24T00:00:00+00:00'
class ThroughputTest(unittest.TestCase):
 def test_48_per_half_hour_has_2304_daily_capacity_in_ideal_conditions(self):
  c=sqlite3.connect(':memory:')
  try:
   v.setup(c)
   for i in range(2400):
    rid=str(i)
    c.execute('''INSERT INTO v5_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (rid,'football','league','Команда А','Команда Б','2026-11-01T13:00:00+00:00',
         'line',1,20,8,0,'ACTIVE_QUOTED','packet',AT,AT))
    c.execute('''INSERT INTO v5_research_queue(root_id,priority,reasons,updated_at) VALUES(?,?,?,?)''',
        (rid,100 if i<70 else 30,'ALL_EVENTS',AT))
   c.commit()
   base=dt.datetime.fromisoformat(AT)
   observed=0
   for x in range(48):
    moment=(base+dt.timedelta(minutes=x*30)).isoformat(timespec='seconds')
    out=v.check_public_evidence(c,moment,max_events=48,loader=lambda _:[],pause=lambda _:None)
    observed+=out['events_attempted']
   self.assertEqual(observed,2304)
   self.assertEqual(c.execute('SELECT COUNT(DISTINCT root_id) FROM v5_research_queue WHERE last_checked IS NOT NULL').fetchone()[0],2304)
   self.assertEqual(out['events_checked_since_utc_midnight'],2304)
  finally:c.close()
if __name__=='__main__':unittest.main()

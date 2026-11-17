#!/usr/bin/env python3
"""ORACLE 5.0: auditable, bookmaker-independent forecast inventory and full market catalogue.

No wagers, no accounts, no fabricated prices or implied meaning for unknown factor IDs.
A source price is evidence of a BOOKMAKER quote, NOT an independent forecast input.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, math, os, re, sqlite3, unicodedata
import urllib.parse, urllib.request, urllib.error, xml.etree.ElementTree as ET, time
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Optional


# Exact bilingual dictionary + NFL abbreviations (bundled in same module so it
# works even when the Docker image copies an explicit list of Python modules).
"""Auditable bilingual alias map for event DISCOVERY only.
Not proof an event exists; matching still requires both sides + date/league timezone
+ unique bookmaker root + same sport. No fuzzy names are auto-linked.
"""
# Officially recognizable club names; localized spellings require event-level
# validation, and the coverage report remains authoritative.
ALIASES = {
 # England Premier League
 'Arsenal FC': ('Арсенал', 'Арсенал Лондон'),
 'Everton FC': ('Эвертон',),
 'Aston Villa FC': ('Астон Вилла',),
 'Manchester City FC': ('Манчестер Сити',),
 'Manchester United FC': ('Манчестер Юнайтед',),
 'Chelsea FC': ('Челси',),
 'Tottenham Hotspur FC': ('Тоттенхэм', 'Тоттенхэм Хотспур'),
 'Liverpool FC': ('Ливерпуль',),
 'Newcastle United FC': ('Ньюкасл', 'Ньюкасл Юнайтед'),
 'West Ham United FC': ('Вест Хэм', 'Вест Хэм Юнайтед'),
 'Brighton & Hove Albion FC': ('Брайтон', 'Брайтон энд Хоув Альбион'),
 'Brentford FC': ('Брентфорд',),
 'Crystal Palace FC': ('Кристал Пэлас',),
 'Wolverhampton Wanderers FC': ('Вулверхэмптон',),
 'Fulham FC': ('Фулхэм',),
 'AFC Bournemouth': ('Борнмут',),
 'Nottingham Forest FC': ('Ноттингем Форест',),
 'Leeds United FC': ('Лидс', 'Лидс Юнайтед'),
 'Sunderland AFC': ('Сандерленд',),
 'Burnley FC': ('Бернли',),
 # English Championship
 'Blackburn Rovers FC': ('Блэкберн', 'Блэкберн Роверс'),
 'Wrexham AFC': ('Рексем', 'Рексхэм'),
 'Cardiff City FC': ('Кардифф', 'Кардифф Сити'),
 'Birmingham City FC': ('Бирмингем', 'Бирмингем Сити'),
 'Charlton Athletic FC': ('Чарльтон', 'Чарльтон Атлетик'),
 'Norwich City FC': ('Норвич', 'Норвич Сити'),
 'Portsmouth FC': ('Портсмут',),
 'Millwall FC': ('Миллуолл',),
 'Preston North End FC': ('Престон', 'Престон Норт Энд'),
 'West Bromwich Albion FC': ('Вест Бромвич', 'Вест Бромвич Альбион'),
 'Queens Park Rangers FC': ('Куинз Парк Рейнджерс', 'КПР'),
 'Swansea City AFC': ('Суонси', 'Суонси Сити'),
 'Sheffield United FC': ('Шеффилд Юнайтед',),
 'Derby County FC': ('Дерби', 'Дерби Каунти'),
 'Stoke City FC': ('Сток Сити', 'Сток'),
 'Bristol City FC': ('Бристоль Сити',),
 'Ipswich Town FC': ('Ипсвич', 'Ипсвич Таун'),
 'Southampton FC': ('Саутгемптон',),
 'Coventry City FC': ('Ковентри', 'Ковентри Сити'),
 'Watford FC': ('Уотфорд',),
 'Leicester City FC': ('Лестер', 'Лестер Сити'),
 'Hull City AFC': ('Халл Сити', 'Халл'),
 'Oxford United FC': ('Оксфорд Юнайтед',),
 'Middlesbrough FC': ('Мидлсбро',),
 # Italy Serie A
 'Cagliari Calcio': ('Кальяри',),
 'Bologna FC 1909': ('Болонья',),
 'Como 1907': ('Комо',),
 'US Sassuolo Calcio': ('Сассуоло',),
 'SS Lazio': ('Лацио',),
 'Parma Calcio 1913': ('Парма',),
 'SSC Napoli': ('Наполи', 'Неаполь'),
 'AS Roma': ('Рома',),
 'Juventus FC': ('Ювентус',),
 'FC Internazionale Milano': ('Интер', 'Интер Милан'),
 'AC Milan': ('Милан',),
 'ACF Fiorentina': ('Фиорентина',),
 'Torino FC': ('Торино',),
 'Atalanta BC': ('Аталанта',),
 'Genoa CFC': ('Дженоа',),
 'Udinese Calcio': ('Удинезе',),
 'Hellas Verona FC': ('Верона', 'Эллас Верона'),
 'US Lecce': ('Лечче',),
 'AC Pisa 1909': ('Пиза',),
 'US Cremonese': ('Кремонезе',),
 # France
 'Racing Club de Lens': ('Ланс',),
 'Paris FC': ('Париж ФК', 'Пари ФК', 'Париж'),
 'AS Monaco FC': ('Монако',),
 'Lille OSC': ('Лилль',),
 'Angers SCO': ('Анже',),
 'FC Lorient': ('Лорьян',),
 'Paris Saint-Germain FC': ('ПСЖ', 'Пари Сен-Жермен'),
 'Olympique de Marseille': ('Марсель',),
 'Olympique Lyonnais': ('Лион',),
 'OGC Nice': ('Ницца',),
 'Stade Rennais FC': ('Ренн',),
 'RC Strasbourg Alsace': ('Страсбур',),
 'Toulouse FC': ('Тулуза',),
 'FC Nantes': ('Нант',),
 'AJ Auxerre': ('Осер',),
 'Stade Brestois 29': ('Брест',),
 # Portugal
 'CF Estrela da Amadora': ('Эштрела Амадора', 'Эштрела', 'Эстрела Амадора'),
 'Casa Pia AC': ('Каза Пия', 'Каза Пиа'),
 'FC Alverca': ('Алверка',),
 'FC Arouca': ('Арока', 'Арука'),
 'Gil Vicente FC': ('Жил Висенте',),
 'FC Porto': ('Порту',),
 'Sport Lisboa e Benfica': ('Бенфика',),
 'Sporting CP': ('Спортинг', 'Спортинг Лиссабон'),
 'SC Braga': ('Брага',),
 'Vitória SC': ('Витория Гимарайнш', 'Витория Гимараэш'),
 # Netherlands
 'FC Utrecht': ('Утрехт',),
 'PEC Zwolle': ('Зволле',),
 'AZ': ('АЗ Алкмар', 'АЗ', 'Алкмар'),
 "FC Twente '65": ('Твенте',),
 'AFC Ajax': ('Аякс',),
 'PSV Eindhoven': ('ПСВ', 'ПСВ Эйндховен'),
 'Feyenoord Rotterdam': ('Фейеноорд',),
 'NEC Nijmegen': ('НЕК Неймеген', 'НЕК'),
 'FC Groningen': ('Гронинген',),
 'Go Ahead Eagles': ('Гоу Эхед Иглс',),
 # Spain
 'Real Madrid CF': ('Реал Мадрид',),
 'FC Barcelona': ('Барселона',),
 'Club Atlético de Madrid': ('Атлетико Мадрид',),
 'Athletic Club': ('Атлетик Бильбао', 'Атлетик'),
 'Real Sociedad de Fútbol': ('Реал Сосьедад',),
 'Villarreal CF': ('Вильярреал',),
 'Real Betis Balompié': ('Бетис',),
 'Sevilla FC': ('Севилья',),
 'Valencia CF': ('Валенсия',),
 'RC Celta de Vigo': ('Сельта',),
 'RCD Espanyol': ('Эспаньол',),
 'Rayo Vallecano de Madrid': ('Райо Вальекано',),
 'CA Osasuna': ('Осасуна',),
 'Deportivo Alavés': ('Алавес',),
 'Getafe CF': ('Хетафе',),
 'Girona FC': ('Жирона',),
 'RCD Mallorca': ('Мальорка',),
 'Real Oviedo': ('Реал Овьедо',),
 # Germany one missing outside bridge
 'SV 07 Elversberg': ('Эльверсберг','Эльферсберг'),
}
# NFL names are explicitly team-ID-specific; abbreviations are never fuzzy matched.
NFL = {
 'ARI': ('Аризона Кардиналс','Аризона','Arizona Cardinals'),
 'ATL': ('Атланта Фэлконс','Атланта Фэлконз','Атланта','Atlanta Falcons'),
 'BAL': ('Балтимор Рэйвенс','Балтимор','Baltimore Ravens'),
 'BUF': ('Баффало Биллс','Баффало','Buffalo Bills'),
 'CAR': ('Каролина Пантерс','Каролина','Carolina Panthers'),
 'CHI': ('Чикаго Беарс','Чикаго','Chicago Bears'),
 'CIN': ('Цинциннати Бенгалс','Цинциннати','Cincinnati Bengals'),
 'CLE': ('Кливленд Браунс','Кливленд','Cleveland Browns'),
 'DAL': ('Даллас Ковбойс','Даллас','Dallas Cowboys'),
 'DEN': ('Денвер Бронкос','Денвер','Denver Broncos'),
 'DET': ('Детройт Лайонс','Детройт','Detroit Lions'),
 'GB': ('Грин-Бэй Пэкерс','Грин Бэй','Green Bay Packers'),
 'HOU': ('Хьюстон Тексанс','Хьюстон','Houston Texans'),
 'IND': ('Индианаполис Колтс','Индианаполис','Indianapolis Colts'),
 'JAX': ('Джексонвилл Джагуарс','Джексонвилл','Jacksonville Jaguars'),
 'KC': ('Канзас-Сити Чифс','Канзас Сити','Kansas City Chiefs'),
 'LAC': ('Лос-Анджелес Чарджерс','ЛА Чарджерс','Los Angeles Chargers'),
 'LAR': ('Лос-Анджелес Рэмс','ЛА Рэмс','Los Angeles Rams'),
 'LV': ('Лас-Вегас Рэйдерс','Лас Вегас','Las Vegas Raiders'),
 'MIA': ('Майами Долфинс','Майами','Miami Dolphins'),
 'MIN': ('Миннесота Вайкингс','Миннесота','Minnesota Vikings'),
 'NE': ('Нью-Ингленд Пэтриотс','Нью-Ингленд','New England Patriots'),
 'NO': ('Нью-Орлеан Сэйнтс','Нью-Орлеан','New Orleans Saints'),
 'NYG': ('Нью-Йорк Джайентс','НЙ Джайентс','New York Giants'),
 'NYJ': ('Нью-Йорк Джетс','НЙ Джетс','New York Jets'),
 'PHI': ('Филадельфия Иглс','Филадельфия','Philadelphia Eagles'),
 'PIT': ('Питтсбург Стилерс','Питтсбург','Pittsburgh Steelers'),
 'SEA': ('Сиэтл Сихокс','Сиэтл','Seattle Seahawks'),
 'SF': ('Сан-Франциско Форти Найнерс','Сан-Франциско 49ерс','Сан-Франциско','San Francisco 49ers'),
 'TB': ('Тампа-Бэй Бакканирс','Тампа-Бэй','Tampa Bay Buccaneers'),
 'TEN': ('Теннесси Тайтанс','Теннесси','Tennessee Titans'),
 'WAS': ('Вашингтон Коммандерс','Вашингтон','Washington Commanders'),
}
ALIASES.update(NFL)
# Expanded verified-identity *candidates*, not automatic fuzzy linking. The actual
# Fonbet counterpart must still match BOTH participants + league-local date + sport
# and be the only possible root on that day. Youth/women/reserve suffixes are NOT stripped.
ALIASES_V52 = {
 'TSG 1899 Hoffenheim': ('Хоффенхайм', 'Хоффенхайм 1899', 'ТСГ Хоффенхайм'),
 'Rayo Vallecano de Madrid': ('Райо Вальекано', 'Райо Вальекано Мадрид'),
 'Elche CF': ('Эльче',),
 'Levante UD': ('Леванте',),
 'Valencia CF': ('Валенсия', 'Валенсия КФ'),
 'Villarreal CF': ('Вильярреал', 'Вильярреал КФ'),
 'Frosinone Calcio': ('Фрозиноне',),
 'Venezia FC': ('Венеция', 'Венеция ФК'),
 'Le Havre AC': ('Гавр', 'Гавр АК'),
 'Olympique de Marseille': ('Марсель', 'Олимпик Марсель'),
 'Olympique Lyonnais': ('Лион', 'Олимпик Лион'),
 'RC Strasbourg Alsace': ('Страсбург', 'Страсбур'),
 'Stade Rennais FC 1901': ('Ренн',),
 'AC Monza': ('Монца',),
 'PEC Zwolle': ('Зволле', 'ПЕК Зволле'),
 'CF Estrela da Amadora': ('Эштрела Амадора', 'Эстрела Амадора', 'Эштрела', 'Эстрела'),
 'Casa Pia AC': ('Каза Пия', 'Каза Пиа', 'Каса Пия'),
 'Fortuna Sittard': ('Фортуна Ситтард', 'Фортуна Ситтард 1968'),
 'SBV Excelsior': ('Эксельсиор', 'Эксельсиор Роттердам'),
 'NEC': ('НЕК', 'НЕК Неймеген', 'Неймеген'),
 'NEC Nijmegen': ('НЕК', 'НЕК Неймеген', 'Неймеген'),
 'CD Nacional': ('Насьонал', 'Насьонал Мадейра'),
 'Sporting Clube de Braga': ('Брага', 'Спортинг Брага'),
 'GD Estoril Praia': ('Эшторил', 'Эшторил Прая', 'Эсторил'),
 'Moreirense FC': ('Морейренсе', 'Морейренси', 'Морейренше'),
 'Rio Ave FC': ('Риу Аве', 'Риу-Аве', 'Рио Аве'),
 'FC Famalicão': ('Фамаликан', 'Фамаликан ФК', 'Фамалисао'),
 'CD Santa Clara': ('Санта-Клара', 'Санта Клара'),
 # NFL common localized variants. Never map generic 'LA' to either Rams/Chargers.
 'TEN': ('Теннесси Тайтенс','Теннесси Тайтэнс', 'Теннесси Тайтанс'),
 'CLE': ('Кливленд Браунс','Кливленд Браунз'),
 'ATL': ('Атланта Фалконс','Атланта Фэлконс','Атланта Фэлконз'),
 'SF': ('Сан-Франциско Форти Найнерс','Сан Франциско 49ерс', 'Сан-Франциско 49ерс', 'Сан-Франциско 49-е'),
 'CAR': ('Каролина Пантерз', 'Каролина Пантерс'),
 'TB': ('Тампа Бэй Бакканирс','Тампа-Бэй Бакканирс','Тампа-Бэй Бакканирз', 'Тампа-Бэй Бакэнирс'),
 'GB': ('Грин Бэй Пэкерс', 'Грин-Бей Пэкерс', 'Грин-Бэй Пэкерс'),
}
for canonical,variants in ALIASES_V52.items():
    ALIASES[canonical] = tuple(dict.fromkeys(tuple(ALIASES.get(canonical, ())) + tuple(variants)))
# In nflverse schedules LA means the Los Angeles Rams; LAC means Chargers.
# Limit this interpretation to IDs explicitly originated by nflverse.
NFLVERSE_LA_RAMS = ('Лос-Анджелес Рэмс', 'ЛА Рэмс', 'Los Angeles Rams')
V51_ALIASES = ALIASES

# Local fixture date is not UTC kickoff date for some leagues, notably NFL.
LEAGUE_TZ = {
 'nfl':'America/New_York','en.1':'Europe/London','en.2':'Europe/London',
 'it.1':'Europe/Rome','es.1':'Europe/Madrid','fr.1':'Europe/Paris',
 'pt.1':'Europe/Lisbon','nl.1':'Europe/Amsterdam','de.1':'Europe/Berlin',
}

UTC = dt.timezone.utc
DB = os.getenv('ORACLE_DB', '/app/state/oracle.sqlite3')
SNAPSHOT_MAX_AGE_MIN = max(5, int(os.getenv('ORACLE_V5_MAX_QUOTE_AGE_MIN', '25')))
KNOWN_FT_1X2 = {'921':'1','922':'X','923':'2'}  # Root-level only.
KNOWN_TOTAL = {'930':'over','931':'under','1727':'over','1728':'under'} # match total only if explicit matching line
KNOWN_DC = {'924':'1X','1571':'12','925':'X2'} # verified root full time double chance
KNOWN_BTTS = {'4241':'YES','4242':'NO'} # verified football root both score
VERIFIED_CODES = set(KNOWN_FT_1X2)|set(KNOWN_TOTAL)|set(KNOWN_DC)|set(KNOWN_BTTS)
CODE_BY_SELECTION = {v:k for k,v in KNOWN_FT_1X2.items()}


def stamp(t=None):
    if isinstance(t,str):t=parse(t)
    return (t or dt.datetime.now(UTC)).astimezone(UTC).isoformat(timespec='seconds')


def parse(s):
    v=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if v.tzinfo is None:raise ValueError('Timestamp requires an explicit timezone: '+str(s)[:80])
    return v.astimezone(UTC)


def has_table(c,t):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone() is not None


def chash(*v):
    return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')).hexdigest()[:32]


def norm(s):
    s=unicodedata.normalize('NFKD',str(s or '').casefold())
    return ''.join(x for x in s if x.isalnum() and not unicodedata.combining(x))

# Cyrillic transliteration is useful for discovery; never sufficient on its own unless
# both independently ordered participants match with a strict margin and one-to-one event.
RU={'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z','и':'i','й':'i',
    'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f',
    'х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    'і':'i','ї':'yi','є':'ye','ґ':'g'}

def translit(s):
    return norm(''.join(RU.get(ch,ch) for ch in str(s or '').casefold()))


def name_sets(s):
    from oracle_fonbet_bridge import KNOWN_ALIASES
    al=tuple(KNOWN_ALIASES.get(s,())) + tuple(V51_ALIASES.get(s,()))
    return {norm(s),translit(s)} | {norm(a) for a in al} | {translit(a) for a in al}


def name_match(src,fb):
    a,b=name_sets(src), {norm(fb),translit(fb)}
    if a & b: return 1.0,'EXPLICIT_ALIAS_OR_EXACT'
    if not norm(src) or not norm(fb):return 0.,''
    # Do NOT resolve games from only one participant or fuzzy names alone.
    return max(SequenceMatcher(None,x,y).ratio() for x in a for y in b if x and y),'FUZZY_DISCOVERY_ONLY'


def sport_norm(s):
    s=norm(s)
    names={'soccer':'football','футбол':'football','americanfootball':'americanfootball','amfootball':'americanfootball',
           'американскийфутбол':'americanfootball','хоккей':'hockey','icehockey':'hockey',
           'баскетбол':'basketball','теннис':'tennis','волейбол':'volleyball',
           'киберспорт':'esports','cs2':'esports'}
    return names.get(s,s)


def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS v5_catalog (
      root_id TEXT PRIMARY KEY, sport TEXT NOT NULL, league TEXT,home TEXT,away TEXT,
      kickoff_utc TEXT,place TEXT,root_exists INTEGER NOT NULL,active_quotes INTEGER NOT NULL,
      market_codes INTEGER NOT NULL,nested_events INTEGER NOT NULL,
      quote_status TEXT NOT NULL,packet_version TEXT,packet_at TEXT,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS v5_catalog_start ON v5_catalog(kickoff_utc,sport);
    CREATE TABLE IF NOT EXISTS v5_forecasts (
      id TEXT PRIMARY KEY,origin TEXT NOT NULL,original_event_id TEXT NOT NULL,
      created_at TEXT NOT NULL,sport TEXT NOT NULL,league TEXT,home TEXT NOT NULL,
      away TEXT NOT NULL,match_date TEXT NOT NULL,kickoff_utc TEXT,
      market TEXT NOT NULL,selection TEXT NOT NULL,probability REAL,
      model TEXT NOT NULL,root_id TEXT,match_rule TEXT NOT NULL,price REAL,
      quote_key TEXT,quote_snapshot TEXT,price_status TEXT NOT NULL,
      evaluated_at TEXT NOT NULL,details_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS v5_forecasts_start ON v5_forecasts(match_date,origin,price_status);
    CREATE INDEX IF NOT EXISTS v5_forecasts_root ON v5_forecasts(root_id);
    CREATE TABLE IF NOT EXISTS v5_price_captures (
      forecast_id TEXT NOT NULL,quote_key TEXT NOT NULL,source_snapshot TEXT NOT NULL,
      decimal_price REAL NOT NULL,selection TEXT NOT NULL,verified_market TEXT NOT NULL,
      stored_at TEXT NOT NULL,PRIMARY KEY(forecast_id,quote_key,source_snapshot));
    CREATE INDEX IF NOT EXISTS v5_capture_snapshot ON v5_price_captures(source_snapshot);
    CREATE TABLE IF NOT EXISTS v5_event_links (
      origin TEXT NOT NULL,original_event_id TEXT NOT NULL,root_id TEXT NOT NULL,
      rule TEXT NOT NULL,matched_at TEXT NOT NULL,source_event_at TEXT,
      PRIMARY KEY(origin,original_event_id));
    CREATE INDEX IF NOT EXISTS v5_event_links_root ON v5_event_links(root_id);
    CREATE TABLE IF NOT EXISTS v5_research_queue (
      root_id TEXT PRIMARY KEY,priority INTEGER NOT NULL,reasons TEXT NOT NULL,
      verified_lineup TEXT NOT NULL DEFAULT 'UNKNOWN',
      verified_availability TEXT NOT NULL DEFAULT 'UNKNOWN',
      verified_player_mood TEXT NOT NULL DEFAULT 'UNKNOWN',
      source_provenance TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
      last_checked TEXT,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v5_dossiers (
      root_id TEXT PRIMARY KEY,asof_utc TEXT NOT NULL,payload TEXT NOT NULL,
      source_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v5_research_sources (
      root_id TEXT NOT NULL,url TEXT NOT NULL,headline TEXT NOT NULL,
      provider TEXT NOT NULL,first_seen TEXT NOT NULL,published_at TEXT,
      verification TEXT NOT NULL,PRIMARY KEY(root_id,url));
    CREATE TABLE IF NOT EXISTS v5_link_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      origin TEXT NOT NULL, original_event_id TEXT NOT NULL,
      previous_root_id TEXT NOT NULL, replacement_root_id TEXT,
      reason TEXT NOT NULL, corrected_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS v5_link_audit_event ON v5_link_audit(origin,original_event_id);
    CREATE TABLE IF NOT EXISTS v5_runs (
      id INTEGER PRIMARY KEY,at TEXT NOT NULL,job TEXT NOT NULL,state TEXT NOT NULL,
      counts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v5_checkpoints (
      job TEXT PRIMARY KEY,packet_version TEXT,completed_at TEXT,details TEXT);
    ''')
    c.commit()


def source_packet(c):
    if not has_table(c,'fb_full_packets'):return None
    row=c.execute('SELECT packet_version,imported_at FROM fb_full_packets ORDER BY imported_at DESC LIMIT 1').fetchone()
    return {'version':row[0],'at':row[1]} if row else None


def ingest_catalog(c,now=None,force=False):
    """Index ALL observed Fonbet roots and factor counts; never copy opaque quote data."""
    at=stamp(now);pk=source_packet(c)
    if not (has_table(c,'fb_full_events') and has_table(c,'fb_full_current')):
        return {'state':'NO_FONBET_TABLES','catalog_events':0}
    if not pk:return {'state':'NO_SOURCE_PACKET','catalog_events':0}
    prev=c.execute("SELECT packet_version FROM v5_checkpoints WHERE job='catalog'").fetchone()
    age=(parse(at)-parse(pk['at'])).total_seconds()/60
    fresh= -2<=age<=SNAPSHOT_MAX_AGE_MIN
    if prev and prev[0]==pk['version'] and not force:
        # A non-changing packet can become stale between scheduler iterations.
        # Never leave yesterday's lines labelled ACTIVE_QUOTED indefinitely.
        if not fresh:
            with c:
                c.execute('''UPDATE v5_catalog SET quote_status='SOURCE_STALE',updated_at=?
                    WHERE quote_status IN ('ACTIVE_QUOTED','NO_ACTIVE_QUOTE','UNRESOLVED_ROOT_METADATA')''',(at,))
        return {'state':'UNCHANGED_PACKET','catalog_events':c.execute('SELECT COUNT(*) FROM v5_catalog').fetchone()[0],
                'packet':pk,'snapshot_fresh':fresh,'snapshot_age_min':round(age,1)}
    # A single scan of full quote table, not one query per candidate event.
    quotes={str(r[0]):(int(r[1]),int(r[2])) for r in c.execute('''
      SELECT root_event_id,COUNT(*),COUNT(DISTINCT factor_id)
      FROM fb_full_current WHERE active=1 AND root_event_id IS NOT NULL
      GROUP BY root_event_id''')}
    nested=Counter()
    for root,n in c.execute("SELECT root_event_id,COUNT(*) FROM fb_full_events WHERE root_event_id<>event_id GROUP BY root_event_id"):
        nested[str(root)]=int(n)
    roots={}
    for row in c.execute('''SELECT event_id,sport,league,home,away,start_utc,place FROM fb_full_events
       WHERE root_event_id=event_id OR level=1'''):
        roots[str(row[0])]=row
    # Factor groups whose parent was not present in event metadata must not vanish.
    all_ids=set(roots)|set(quotes)
    entries=[]
    for rid in sorted(all_ids):
        r=roots.get(rid)
        count,uni=quotes.get(rid,(0,0))
        status=('SOURCE_STALE' if not fresh else 'NO_ACTIVE_QUOTE' if not count else
                'ACTIVE_QUOTED' if r else 'UNRESOLVED_ROOT_METADATA')
        entries.append((rid,str(r[1] or '') if r else '',str(r[2] or '') if r else '',
           str(r[3] or '') if r else '',str(r[4] or '') if r else '',
           str(r[5] or '') if r else '',str(r[6] or '') if r else '',int(bool(r)),
           count,uni,nested.get(rid,0),status,pk['version'],pk['at'],at))
    with c:
        # Snapshot represents current universe.  Keep old roots for historical inspection
        # but mark absent active line explicitly. Never erase odds history.
        c.execute("UPDATE v5_catalog SET active_quotes=0,market_codes=0,quote_status='NOT_IN_LATEST_SNAPSHOT', updated_at=?",
                  (at,))
        c.executemany('''INSERT INTO v5_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(root_id) DO UPDATE SET
         sport=excluded.sport,league=excluded.league,home=excluded.home,away=excluded.away,
         kickoff_utc=excluded.kickoff_utc,place=excluded.place,root_exists=excluded.root_exists,
         active_quotes=excluded.active_quotes,market_codes=excluded.market_codes,
         nested_events=excluded.nested_events,quote_status=excluded.quote_status,
         packet_version=excluded.packet_version,packet_at=excluded.packet_at,updated_at=excluded.updated_at''',entries)
        c.execute('''INSERT INTO v5_checkpoints VALUES(?,?,?,?)
          ON CONFLICT(job) DO UPDATE SET packet_version=excluded.packet_version,
          completed_at=excluded.completed_at,details=excluded.details''',
          ('catalog',pk['version'],at,json.dumps({'roots':len(entries),'active_quote_roots':sum(r[8]>0 for r in entries)})))
    return {'state':'OK','catalog_events':len(entries),'active_quoted_events':sum(r[8]>0 for r in entries),
            'active_quotes':sum(r[8] for r in entries),'packet':pk,'snapshot_age_min':round(age,1)}


def iter_predictions(c,now=None):
    """Capture every historical + upcoming forecast from three known ORACLE registries.
    Do not treat a count of selections as a count of unique events.
    """
    if has_table(c,'v4_sport_preds') and has_table(c,'v4_games'):
        for eid,sport,lg,home,away,kick,market,sel,p,issued in c.execute('''
          SELECT g.event_id,g.sport,g.league,g.home,g.away,g.kickoff_utc,
             p.market,p.selection,p.prob,p.issued_at FROM v4_sport_preds p
             JOIN v4_games g ON g.event_id=p.event_id'''):
            yield {'id':chash('v4_sport_preds',eid,market,sel,issued),
                'origin':'v4_sport_preds','event_id':str(eid),'created_at':issued,
                'sport':sport,'league':lg,'home':home,'away':away,'date':kick[:10],
                'kick':kick,'market':market,'selection':sel,'prob':p,'model':'v4_sports'}
    if has_table(c,'sf_forecasts'):
        for row in c.execute('''SELECT match_id,asof_day,created_at,sport,league,match_date,
             home,away,market,selection,probability,model_version FROM sf_forecasts'''):
            mid,day,at,sport,lg,date,h,a,market,sel,prob,model=row
            yield {'id':chash('sf_forecasts',mid,day,market,sel),
                'origin':'sf_forecasts','event_id':str(mid),'created_at':at,
                'sport':sport,'league':lg,'home':h,'away':a,'date':date,
                'kick':None,'market':market,'selection':sel,'prob':prob,'model':model}
    if has_table(c,'sb_independent_forecasts') and has_table(c,'sb_event_coverage'):
        for row in c.execute('''SELECT f.selection_key,f.model,f.asof_utc,f.p_home,f.p_draw,f.p_away,
            e.sport,e.home,e.away,e.kickoff_utc,e.fonbet_root_event_id
            FROM sb_independent_forecasts f LEFT JOIN sb_event_coverage e
            ON e.selection_key=f.selection_key'''):
            key,model,at,p1,px,p2,sport,h,a,kick,root=row
            for sel,p in (('1',p1),('X',px),('2',p2)):
                yield {'id':chash('sb_independent_forecasts',key,model,sel),
                    'origin':'sb_independent_forecasts','event_id':str(key),'created_at':at,
                    'sport':sport or 'football','league':'','home':h or '', 'away':a or '',
                    'date':kick[:10] if kick else at[:10],'kick':kick,
                    'market':'1X2','selection':sel,'prob':p,'model':model,'known_root':root}


def selected_predictions(c,now=None):
    # Historical forecasts must remain visible for accounting; no fixed LIMIT 15/60/1200.
    return list(iter_predictions(c,now))


def canonical_selection(market,selection):
    # Only map markets whose factor meaning is supported by independent public
    # format examples. UNKNOWN is not the same as odds missing.
    if norm(market)=='1x2':
        return ('1X2',{'1':'1','п1':'1','home':'1','x':'X','ничья':'X','draw':'X',
                      '2':'2','п2':'2','away':'2'}.get(norm(selection)))
    # SportsOpenLab makes explicit TOTAL n.n forecasts.
    m=re.fullmatch(r'(?i)\s*ТОТАЛ\s+([0-9]+(?:[.,][0-9]+)?)\s*',str(market))
    if m:
        line=float(m.group(1).replace(',','.'))
        sel=str(selection).lower().strip()
        if sel.startswith('больше '):side='over'
        elif sel.startswith('меньше '):side='under'
        else:return None
        try:
            sline=float(sel.split()[-1].replace(',','.'))
        except (ValueError,IndexError):return None
        if abs(sline-line)>1e-7:return None
        return ('TOTAL',side,line)
    if norm(market) in ('двойнойшанс','двоиноишанс','doublechance','doublechances'):
        selection_key=norm(selection)
        return ('DOUBLE_CHANCE', {'1x':'1X','12':'12','x2':'X2'}.get(selection_key)) if selection_key in ('1x','12','x2') else None
    if norm(market) in ('обезабьют','bothteamstoscore','btts'):
        selection_key=norm(selection)
        return ('BTTS', 'YES') if selection_key in ('да','yes') else ('BTTS','NO') if selection_key in ('нет','no') else None
    return None


def total_line(point):
    """Decode documented Fonbet total parameter representation ONLY.

    In provider CFX examples a 2.5-goal line is represented by pt='2.5'
    and p=250 (hundredths). MA may expose either one. Never infer a line
    from a bare unparameterized total code or from non-total factor IDs.
    If both are present they MUST agree, otherwise reject the quote.
    """
    try:
        v=json.loads(point or '{}')
        if not isinstance(v,dict):return None
        raw_text=v.get('pt')
        raw_num=v.get('p')
        text_value=None
        numeric_value=None
        if raw_text is not None and str(raw_text).strip():
            text_value=float(str(raw_text).strip().replace(',','.'))
            if not (math.isfinite(text_value) and 0<text_value<2000):return None
        if raw_num is not None and str(raw_num).strip():
            # Numeric p is in hundredths, per published CFX param/paramText.
            # No fractional hundredths or unbounded unexpected parameters.
            param=float(raw_num)
            if not math.isfinite(param) or not param.is_integer():return None
            numeric_value=param/100.0
            if not (0<numeric_value<2000):return None
        if text_value is None and numeric_value is None:return None
        if text_value is not None and numeric_value is not None:
            if abs(text_value-numeric_value)>1e-7:return None
        return round(text_value if text_value is not None else numeric_value,6)
    except (TypeError,ValueError,OverflowError):return None

def local_match_day(p,kick):
    """Fixture source 'match_date' denotes local league calendar day.
    An adjacent UTC date is eligible only when its local day is EXACTLY that day.
    A source-provided precise kickoff still requires <=10min equality.
    """
    if p.get('kick'):
        try:return abs((parse(p['kick'])-parse(kick)).total_seconds())<=600
        except (ValueError,TypeError):return False
    league=str(p.get('league') or '').lower()
    if kick[:10]==p['date']:
        # Still reject a contradiction if known fixture timezone says previous day.
        tzname=LEAGUE_TZ.get(league)
        if not tzname:return True
        try:return parse(kick).astimezone(ZoneInfo(tzname)).date().isoformat()==p['date']
        except (ValueError,ZoneInfoNotFoundError):return True
    tzname=LEAGUE_TZ.get(league)
    if not tzname:return False
    try:
        delta=abs((parse(kick).date()-dt.date.fromisoformat(p['date'])).days)
        return delta==1 and parse(kick).astimezone(ZoneInfo(tzname)).date().isoformat()==p['date']
    except (ValueError,ZoneInfoNotFoundError):return False


def is_fixture_market(root):
    """Exclude markets that impersonate matches with the same two participants.

    Fonbet's season-long 'who finishes higher' can have team HOME/AWAY and a
    future deadline, yet is NOT a head-to-head fixture. Never bind it to 1X2.
    Restrict block-list to explicit, recognized season-comparison phrases;
    unfamiliar league names remain subject to all other safeguards.
    """
    if not root or not root.get('root_exists'):return False
    league=norm(root.get('league') or '')
    return not any(x in league for x in (
        'ктовыше','ктобудетвыше','итогамчемпионата',
        'whofinisheshigher','seasonheadtohead','higherinthestandings'))


def verified_fixture_pair(p,root):
    """Revalidate even previously saved event links; stored does not mean true."""
    if not is_fixture_market(root):return False
    if sport_norm(p.get('sport'))!=sport_norm(root.get('sport')):return False
    if not local_match_day(p,root.get('kickoff_utc') or ''):return False
    # The compact 'LA' is licensed by the original nflverse event identity only.
    h=p['home'];a=p['away']
    if p.get('origin')=='sf_forecasts' and str(p.get('event_id','')).startswith('nflverse|') and str(p.get('league','')).lower()=='nfl':
        if h=='LA':h='LAR'
        if a=='LA':a='LAR'
    return name_match(h,root['home'])[0]==1.0 and name_match(a,root['away'])[0]==1.0


def detect_match(p,events_by_date,root_index,links):
    """Fixture-only, verified home+away, same sport and kickoff/day.
    Existing stored links MUST pass the same verification as new candidates.
    A provider seasonal comparison is not a real match even if both names match.
    """
    key=(p['origin'],p['event_id'])
    kr=str(p.get('known_root') or '')
    if kr in root_index and verified_fixture_pair(p,root_index[kr]):
        return kr,'EXISTING_BRAIN_ID_REVERIFIED',[]
    prior=links.get(key)
    if prior in root_index and verified_fixture_pair(p,root_index[prior]):
        return prior,'EXISTING_LINK_REVERIFIED',[]
    dates=[p['date']]
    try:
        day=dt.date.fromisoformat(p['date'])
        dates += [(day+dt.timedelta(days=x)).isoformat() for x in (-1,1)]
    except ValueError:pass
    candidates=[];near=[];seen=set()
    for date in dates:
        for root in events_by_date.get(date,[]):
            if root['root_id'] in seen:continue
            seen.add(root['root_id'])
            if not is_fixture_market(root):continue
            if sport_norm(p['sport'])!=sport_norm(root['sport']):continue
            if not local_match_day(p,root['kickoff_utc']):continue
            hname=p['home'];aname=p['away']
            if p.get('origin')=='sf_forecasts' and str(p.get('event_id','')).startswith('nflverse|') and str(p.get('league','')).lower()=='nfl':
                if hname=='LA':hname='LAR'
                if aname=='LA':aname='LAR'
            h,_=name_match(hname,root['home']);a,_=name_match(aname,root['away'])
            near.append((min(h,a),root['root_id']))
            if h==1 and a==1:
                candidates.append((root['root_id'],'VERIFIED_PAIR_KICKOFF' if p.get('kick') else 'VERIFIED_PAIR_LOCAL_DAY'))
    if len(candidates)==1:return candidates[0][0],candidates[0][1],near
    if len(candidates)>1:return None,'AMBIGUOUS_EVENT_PAIR',sorted(near,reverse=True)[:5]
    return None,'NO_VERIFIED_EVENT_PAIR',sorted(near,reverse=True)[:3]


def safe_prob(x):
    try:
        x=float(x)
        return x if math.isfinite(x) and 0<=x<=1 else None
    except (ValueError,TypeError):return None


def current_quotes(c,ids):
    if not ids:return {}
    out=defaultdict(dict)
    codes=sorted(VERIFIED_CODES)
    for i in range(0,len(ids),400):
        batch=ids[i:i+400]
        sql=("SELECT root_event_id,factor_id,quote_key,decimal_price,last_snapshot,point "
             "FROM fb_full_current WHERE active=1 AND event_id=root_event_id "
             "AND factor_id IN ("+','.join('?'*len(codes))+") AND root_event_id IN ("+
             ','.join('?'*len(batch))+")")
        for root,code,qkey,price,at,point in c.execute(sql,codes+batch):
            try:
                params=json.loads(point or '{}')
                price=float(price)
                if not math.isfinite(price) or not (1.001<price<=1001):continue
                if str(code) in KNOWN_TOTAL:
                    line=total_line(point)
                    if line is None:continue
                    sel=('TOTAL',KNOWN_TOTAL[str(code)],round(line,6))
                else:
                    if not isinstance(params,dict) or params.get('p') is not None or params.get('pt') is not None:continue
                    if str(code) in KNOWN_FT_1X2:sel=('1X2',KNOWN_FT_1X2[str(code)])
                    elif str(code) in KNOWN_DC:sel=('DOUBLE_CHANCE',KNOWN_DC[str(code)])
                    elif str(code) in KNOWN_BTTS:sel=('BTTS',KNOWN_BTTS[str(code)])
                    else:continue
                old=out[str(root)].get(sel)
                if old is None or str(at)>str(old[2]):out[str(root)][sel]=(price,qkey,at)
            except (ValueError,TypeError,AttributeError):continue
    return out



def verified_quote_semantics(factor_id,point,canonical):
    """Never reuse a historical quote from the wrong selection, line, or period."""
    if canonical is None:return False
    code=str(factor_id)
    if code in KNOWN_TOTAL:
        line=total_line(point)
        return line is not None and canonical==('TOTAL',KNOWN_TOTAL[code],line)
    try:
        p=json.loads(point or '{}')
        if not isinstance(p,dict) or p.get('p') is not None or p.get('pt') is not None:return False
    except (ValueError,TypeError):return False
    if code in KNOWN_FT_1X2:return canonical==('1X2',KNOWN_FT_1X2[code])
    if code in KNOWN_DC:return canonical==('DOUBLE_CHANCE',KNOWN_DC[code])
    if code in KNOWN_BTTS:return canonical==('BTTS',KNOWN_BTTS[code])
    return False


def refresh_predictions(c,now=None):
    """Attach only semantically VERIFIED markets, report gaps for all others.

    Quote might be displayed without a backtest-comparable as-of timestamp;
    price_status expressly distinguishes this. Odds are NOT used for model training.
    """
    at=stamp(now);pk=source_packet(c)
    if not has_table(c,'v5_catalog'):return {'state':'NO_CATALOG'}
    raw=selected_predictions(c,at)
    root_index={r['root_id']:r for r in catalog_events(c)}
    by_date=defaultdict(list)
    for r in root_index.values():
        if r['kickoff_utc'] and r['root_exists']:
            by_date[r['kickoff_utc'][:10]].append(r)
    already={(a,b):z for a,b,z in c.execute('SELECT origin,original_event_id,root_id FROM v5_event_links')}
    if has_table(c,'fb_bridge_links'):
        for eid,rid in c.execute('SELECT oracle_event_id,fonbet_event_id FROM fb_bridge_links'):
            already.setdefault(('v4_sport_preds',str(eid)),str(rid))
    mappings={};suggestions={};ambiguous=0
    for p in raw:
        k=(p['origin'],p['event_id'])
        if k not in mappings:
            rid,method,hints=detect_match(p,by_date,root_index,already)
            mappings[k]=(rid,method)
            if not rid and len(suggestions)<30:suggestions[str(k)]=hints[:2]
            ambiguous+=method=='AMBIGUOUS_EVENT_PAIR'
    roots=list({m[0] for m in mappings.values() if m[0]})
    qcache=current_quotes(c,roots) if has_table(c,'fb_full_current') else {}
    age=(parse(at)-parse(pk['at'])).total_seconds()/60 if pk else None
    fresh=age is not None and -2<=age<=SNAPSHOT_MAX_AGE_MIN
    writes=[];captures=[];newlinks=[];cnt=Counter();future=0;future_by_date=0
    for p in raw:
        root,rule=mappings[(p['origin'],p['event_id'])]
        start=root_index[root]['kickoff_utc'] if root in root_index else p.get('kick')
        event_future=bool(start and start>at)
        future+=event_future
        future_by_date+=bool(p['date']>=at[:10])
        canonical=canonical_selection(p['market'],p['selection'])
        price=qkey=qat=None
        if root is None:status='EVENT_UNMATCHED' if rule!='AMBIGUOUS_EVENT_PAIR' else 'EVENT_AMBIGUOUS'
        elif not event_future:status='HISTORICAL_OR_STARTED'
        elif not fresh:status='SOURCE_STALE'
        elif canonical is None:status='MARKET_NOT_VERIFIED'  # Not equal to no offered odds!
        else:
            q=qcache.get(root,{}).get(canonical)
            if q:
                price,qkey,qat=q
                if parse(qat)>=parse(start):status='AFTER_KICKOFF'
                elif parse(qat)<parse(p['created_at']):status='QUOTE_BEFORE_FORECAST'
                else:status='PRICE_ATTACHED_TIME_VALID'
            else:status='NO_ACTIVE_MATCHING_QUOTE'
        if status=='PRICE_ATTACHED_TIME_VALID':
            captures.append((p['id'],qkey,qat,price,p['selection'],str(canonical),at))
        if status=='HISTORICAL_OR_STARTED' and root is not None and canonical is not None:
            # Revalidate previous captures against the CURRENT verified fixture ID.
            # Stale V5.0/V5.1 mistaken links must never reappear as historic prices.
            oldrows=c.execute('''SELECT pc.decimal_price,pc.quote_key,pc.source_snapshot,q.factor_id,q.point
                FROM v5_price_captures pc JOIN fb_full_current q ON q.quote_key=pc.quote_key
                WHERE pc.forecast_id=? AND q.root_event_id=? AND q.event_id=q.root_event_id
                AND pc.source_snapshot>=? AND pc.source_snapshot<?
                ORDER BY pc.source_snapshot LIMIT 100''',
                (p['id'],root,p['created_at'],start)).fetchall()
            for historic_price,historic_key,historic_time,code,params in oldrows:
                if verified_quote_semantics(code,params,canonical):
                    price,qkey,qat=historic_price,historic_key,historic_time
                    break
        cnt[status]+=1
        # Always preserve human-readable observed price (if found), even when time invalid.
        details={'reason':rule,'forecast_prices_not_model_inputs':True,
            'market_verified':canonical is not None,'market_count_for_root':root_index.get(root,{}).get('market_codes',0),
            'source_packet':pk,'event_time_known':bool(start),'prediction_asof':p['created_at'],
            'candidate_suggestions_unverified':suggestions.get(str((p['origin'],p['event_id'])),[])}
        writes.append((p['id'],p['origin'],p['event_id'],str(p['created_at']),str(p['sport'] or ''),
          str(p['league'] or ''),str(p['home'] or ''),str(p['away'] or ''),str(p['date']),
          start,str(p['market']),str(p['selection']),safe_prob(p['prob']),str(p['model']),
          root,rule,price,qkey,qat,status,at,json.dumps(details,ensure_ascii=False)))
    # Never blindly retain a legacy V5 link. Keep an audit trail of every
    # invalidated/corrected old link, without touching source quotes/history.
    persisted_links={(a,b):z for a,b,z in c.execute('SELECT origin,original_event_id,root_id FROM v5_event_links')}
    repairs=[];revocations=[];link_audit=[];new_count=0
    for (origin,eid),(root,rule) in mappings.items():
        key=(origin,eid);old=persisted_links.get(key)
        if root:
            if old is None:new_count+=1
            if old!=root:
                newlinks.append((origin,eid,root,rule,at,
                  root_index[root]['kickoff_utc'] if root in root_index else None))
                if old is not None:
                    repairs.append(key)
                    link_audit.append((origin,eid,old,root,'STALE_LINK_REPLACED_BY_VERIFIED_FIXTURE',at))
        elif old is not None:
            # A root that does not pass new checks is not a usable match.
            revocations.append((origin,eid,old))
            link_audit.append((origin,eid,old,None,'UNVERIFIED_LEGACY_LINK_QUARANTINED',at))
    captures_before=c.execute('SELECT COUNT(*) FROM v5_price_captures').fetchone()[0]
    with c:
        c.executemany('''INSERT INTO v5_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET root_id=excluded.root_id,
            kickoff_utc=excluded.kickoff_utc,
            match_rule=excluded.match_rule,price=excluded.price,quote_key=excluded.quote_key,
            quote_snapshot=excluded.quote_snapshot,price_status=excluded.price_status,
            evaluated_at=excluded.evaluated_at,details_json=excluded.details_json''',writes)
        c.executemany('''INSERT INTO v5_event_links VALUES(?,?,?,?,?,?)
            ON CONFLICT(origin,original_event_id) DO UPDATE SET
            root_id=excluded.root_id,rule=excluded.rule,
            matched_at=excluded.matched_at,source_event_at=excluded.source_event_at''',newlinks)
        c.executemany('''DELETE FROM v5_event_links
            WHERE origin=? AND original_event_id=? AND root_id=?''',revocations)
        c.executemany('''INSERT INTO v5_link_audit
            (origin,original_event_id,previous_root_id,replacement_root_id,reason,corrected_at)
            VALUES(?,?,?,?,?,?)''',link_audit)
        c.executemany('''INSERT OR IGNORE INTO v5_price_captures VALUES(?,?,?,?,?,?,?)''',captures)
    real_captures=c.execute('SELECT COUNT(*) FROM v5_price_captures').fetchone()[0]-captures_before
    return {'state':'OK','all_forecast_records':len(raw),'future_forecast_records':future_by_date,
        'upcoming_kickoff_records':future,
        'unique_events':len(mappings),'verified_events':sum(bool(x[0]) for x in mappings.values()),
        'new_event_links':new_count,'repaired_event_links':len(repairs),'quarantined_legacy_links':len(revocations),'new_asof_captures':real_captures,'ambiguous_events':ambiguous,
        'price_statuses':dict(cnt),'snapshot_fresh':fresh,
        'unverified_market_semantics_are_not_guessed':True,
        'note':'DATE_ONLY sf forecasts cannot be assumed to have exact prediction/market timing'}


def catalog_events(c):
    if not has_table(c,'v5_catalog'):return
    cols=['root_id','sport','league','home','away','kickoff_utc','place','root_exists','active_quotes',
          'market_codes','nested_events','quote_status','packet_version','packet_at','updated_at']
    for row in c.execute('SELECT '+','.join(cols)+' FROM v5_catalog'):
        yield dict(zip(cols,row))


def prepare_research(c,now=None):
    """Queue ALL upcoming catalogue events. Deep evidence retrieval is staged/rate limited.
    Never pretend 13k independently verified player dossiers exist.
    """
    at=stamp(now);end=stamp(parse(at)+dt.timedelta(days=14))
    links={r[0] for r in c.execute("SELECT DISTINCT root_id FROM v5_event_links")}
    event_rows=c.execute('''SELECT root_id,kickoff_utc,sport,home,away,active_quotes FROM v5_catalog
       WHERE kickoff_utc>? AND kickoff_utc<=? AND root_exists=1''',(at,end)).fetchall()
    records=[]
    for rid,kick,sport,home,away,counts in event_rows:
        high=rid in links
        priority=100 if high else 30 if counts else 10
        # No private claims. Research backlog is explicit and auditable.
        records.append((rid,priority,'FORECAST_EVENT' if high else 'ALL_PUBLIC_FONBET_EVENTS',at))
    with c:
        c.executemany('''INSERT INTO v5_research_queue(root_id,priority,reasons,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(root_id) DO UPDATE SET
            priority=excluded.priority,reasons=excluded.reasons,updated_at=excluded.updated_at''',records)
    return {'state':'OK','future_fonbet_roots_14d':len(event_rows),
            'priority_forecast_events':sum(x[1]==100 for x in records),
            'player_rosters_verified':c.execute("SELECT COUNT(*) FROM v5_research_queue WHERE verified_lineup='VERIFIED'").fetchone()[0],
            'no_private_player_mood_claims':True}


def evidence_feed_url(event):
    """Search recent public coverage of EACH participant; never infer a verified fact.

    Previously both team names were required (implicit AND) plus an English sport
    word. That suppressed virtually every injury/roster/player article about one
    team. OR finds candidates, but candidate headlines are NOT factual evidence.
    """
    terms=[]
    for key in ('home_alias','away_alias','home','away'):
        t=re.sub(r'["\n\r\t]',' ',str(event.get(key) or '')).strip()[:90]
        if len(t)>=3 and t.casefold() not in {x.casefold() for x in terms}:
            terms.append(t)
    if not terms:return None
    # No betting market vocabulary; price information must not feed the model.
    q='('+' OR '.join('"'+t+'"' for t in terms[:4])+') when:3d'
    has_ascii_alias=bool(event.get('home_alias') or event.get('away_alias'))
    cyrillic=any('А'<=ch<='я' or ch=='ё' for t in terms for ch in t)
    locale={'hl':'en-US','gl':'US','ceid':'US:en'} if has_ascii_alias or not cyrillic else {'hl':'ru','gl':'RU','ceid':'RU:ru'}
    return 'https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':q,**locale})


def fetch_rss(url,timeout=9):
    if not url.startswith('https://news.google.com/rss/search?'):
        raise ValueError('Unapproved news source')
    req=urllib.request.Request(url,headers={'User-Agent':'ORACLE-Sports-Research/5.0 (public RSS, no orders)'})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        data=r.read(950001)
        if len(data)>950000:raise ValueError('RSS response too large')
    root=ET.fromstring(data)
    items=[]
    for node in root.findall('.//item')[:12]:
        val=lambda n:(node.findtext(n) or '').strip()
        title,link,published=val('title'),val('link'),val('pubDate')
        if title and link.startswith('https://news.google.com/'):
            items.append({'url':link[:1500],'title':title[:400], 'published':published or None})
    return items


def check_public_evidence(c,now=None,max_events=48,loader=fetch_rss,pause=time.sleep):
    """Incrementally search news by team, with an inspectable provenance trail.

    Forecast-linked events first; all other upcoming matches remain in queue.
    Empty RSS results and failed requests are distinguished from genuine facts.
    """
    at=stamp(now)
    last=(parse(at)-dt.timedelta(hours=48)).isoformat(timespec='seconds')
    if not (has_table(c,'v5_research_queue') and has_table(c,'v5_catalog')):
        return {'state':'NOT_INITIALIZED'}
    max_events=max(1,min(int(max_events),80))
    rows=c.execute("""SELECT x.root_id,e.home,e.away,e.sport,e.kickoff_utc,x.priority
       FROM v5_research_queue x JOIN v5_catalog e ON e.root_id=x.root_id
       WHERE e.kickoff_utc>? AND e.root_exists=1 AND e.active_quotes>0
       AND x.priority>=30 AND (x.last_checked IS NULL OR x.last_checked<?)
       ORDER BY (x.last_checked IS NOT NULL) ASC,x.priority DESC,
       COALESCE(x.last_checked,'') ASC,e.kickoff_utc ASC LIMIT ?""",(at,last,max_events)).fetchall()
    attempted=added=errors=no_participants=returned=empty=events_with_hits=0
    samples=[];errors_by_kind=Counter();sources={'GoogleNewsRSS':0}
    for root,h,a,sport,kick,priority in rows:
        aliases={}
        # Exact known event link lets us search with the forecast's English names
        # as well as provider-localized names. These are SEARCH terms, not facts.
        if has_table(c,'v5_forecasts'):
            row=c.execute("""SELECT home,away FROM v5_forecasts
               WHERE root_id=? AND origin='sf_forecasts' AND home!='' AND away!=''
               ORDER BY created_at DESC LIMIT 1""",(root,)).fetchone()
            if row:aliases={'home_alias':row[0],'away_alias':row[1]}
        url=evidence_feed_url({'home':h,'away':a,'sport':sport,**aliases})
        if not url:no_participants+=1;continue
        attempted+=1
        try:
            articles=loader(url)
            if not isinstance(articles,list):raise ValueError('RSS result must be a list')
            returned+=len(articles)
            empty+=not bool(articles)
            events_with_hits+=bool(articles)
            with c:
                for item in articles[:30]:
                    if not isinstance(item,dict):continue
                    u=item.get('url');title=item.get('title')
                    if not isinstance(u,str) or not u.startswith('https://news.google.com/') or not isinstance(title,str) or not title.strip():
                        continue
                    result=c.execute("""INSERT OR IGNORE INTO v5_research_sources
                      VALUES(?,?,?,?,?,?,?)""",(root,u[:1500],title[:400],
                        'GoogleNewsRSS',at,item.get('published'), 'UNVERIFIED_SEARCH_HIT'))
                    added+=int(result.rowcount>0)
                    sources['GoogleNewsRSS']+=int(result.rowcount>0)
                c.execute('UPDATE v5_research_queue SET last_checked=? WHERE root_id=?',(at,root))
        except Exception as exc:
            errors+=1
            errors_by_kind[type(exc).__name__]+=1
            if len(samples)<5:samples.append({'root_id':root,'error':type(exc).__name__,
                                            'description':str(exc)[:150]})
            with c:c.execute('UPDATE v5_research_queue SET last_checked=? WHERE root_id=?',(at,root))
        if loader is fetch_rss:pause(.3)
    state='PARTIAL' if errors else 'OK'
    out={'state':state,'events_attempted':attempted,'articles_returned':returned,
         'articles_added':added,'events_with_hits':events_with_hits,
         'empty_searches':empty,'http_or_parse_errors':errors,
         'error_kinds':dict(errors_by_kind),'no_teams':no_participants,
         'samples':samples,'all_articles_unverified':True,
         'search_semantics':'individual team OR (not fixture-only AND)',
         'next_check_after_hours':48,
         'note':'News titles are UNVERIFIED search hits, never evidence of injury, lineups or mental state.'}
    out['source_requests_target_per_day']=2304
    out['target_is_attempts_not_verified_articles']=True
    out['events_checked_since_utc_midnight']=c.execute('''SELECT COUNT(*) FROM v5_research_queue
        WHERE last_checked>=? AND last_checked<=?''',(at[:10]+'T00:00:00+00:00',at)).fetchone()[0]
    with c:c.execute('INSERT INTO v5_runs(at,job,state,counts) VALUES(?,?,?,?)',
                     (at,'evidence',state,json.dumps(out,ensure_ascii=False)))
    return out


def form_team(c,team,at,n=8):
    if not team:return {'sample':0,'origin':'NO_TEAM'}
    if not has_table(c,'v4_games'):
        return {'sample':0,'origin':'NO_VERIFIED_RESULTS_TABLE'}
    rows=c.execute('''SELECT home,away,home_score,away_score,kickoff_utc,league
      FROM v4_games WHERE final=1 AND home_score IS NOT NULL AND away_score IS NOT NULL
      AND kickoff_utc<? AND (home=? OR away=?)
      ORDER BY kickoff_utc DESC LIMIT ?''',(at,team,team,n)).fetchall()
    won=draw=loss=gf=ga=0
    for h,a,hs,as_,date,lg in rows:
        score,conceded=(hs,as_) if h==team else (as_,hs)
        won+=score>conceded;draw+=score==conceded;loss+=score<conceded
        gf+=score;ga+=conceded
    return {'sample':len(rows),'wins':won,'draws':draw,'losses':loss,
            'scored':gf,'conceded':ga,'origin':'v4_games_VERIFIED_FINISHED_ONLY',
            'last_kickoff':rows[0][4] if rows else None}


def build_dossiers(c,now=None):
    """All upcoming linked ORACLE events get a research readiness report."""
    at=stamp(now)
    rows=c.execute('''SELECT DISTINCT e.root_id,e.home,e.away,e.sport,e.league,e.kickoff_utc
         FROM v5_catalog e JOIN v5_event_links l ON l.root_id=e.root_id
         WHERE e.kickoff_utc>? ORDER BY e.kickoff_utc''',(at,)).fetchall()
    completed=0
    with c:
        for root,home,away,sport,league,kick in rows:
            articles=[]
            for url,title,provider,t0 in c.execute('''SELECT url,headline,provider,first_seen
              FROM v5_research_sources WHERE root_id=? ORDER BY first_seen DESC LIMIT 6''',(root,)):
                articles.append({'url':url,'title':title,'provider':provider,'observed_at':t0,
                                   'verified_fact':False})
            facts={'match_root_id':root,'sport':sport,'league':league,'teams':[home,away],
                 'kickoff_utc':kick,'observed_at':at,'independent_of_odds':True,
                 'teams_form':{'home':form_team(c,home,at) if sport_norm(sport)=='football' else {'state':'NO_VALIDATED_SPORT_MODEL'},
                               'away':form_team(c,away,at) if sport_norm(sport)=='football' else {'state':'NO_VALIDATED_SPORT_MODEL'}},
                 'player_level':{'squad':'NOT_VERIFIED','injuries':'NOT_VERIFIED',
                      'starting_lineup':'NOT_VERIFIED','individual_stats':'NOT_VERIFIED',
                      'mental_state':'NOT_ESTIMATED_NO_AUTHENTIC_SOURCE'},
                 'context':{'travel':'NOT_VERIFIED','weather':'NOT_VERIFIED',
                    'tactics':'NOT_VERIFIED'},
                 'news_candidates':articles,
                 'readiness':'UNVERIFIED_PLAYER_EVIDENCE',
                 'warning':'No bookmaker prices or unverified news fed into model.'}
            raw=json.dumps(facts,ensure_ascii=False,sort_keys=True)
            c.execute('''INSERT INTO v5_dossiers VALUES(?,?,?,?)
              ON CONFLICT(root_id) DO UPDATE SET asof_utc=excluded.asof_utc,
              payload=excluded.payload,source_hash=excluded.source_hash''',
              (root,at,raw,chash(raw)))
            completed+=1
    return {'state':'OK','upcoming_dossiers':completed,
            'fully_verified_player_dossiers':0,
            'independent_of_bookmaker_prices':True}



def archive_due(c,now=None):
    at=stamp(now)
    row=c.execute("SELECT completed_at FROM v5_checkpoints WHERE job='archive'").fetchone()
    if row and (parse(at)-parse(row[0])).total_seconds()<6*3600:
        return {'state':'OK','not_due':True}
    result=__import__('oracle_v5_settlement').backfill(c,at)
    with c:c.execute("INSERT INTO v5_checkpoints(job,packet_version,completed_at,details) VALUES('archive',NULL,?,?) ON CONFLICT(job) DO UPDATE SET completed_at=excluded.completed_at,details=excluded.details",
             (at,json.dumps(result,ensure_ascii=False)))
    return result


def run(c,now=None,force=False):
    now=stamp(now);setup(c)
    out={}
    for name,work in (('catalog',lambda:ingest_catalog(c,now,force)),
                      ('forecasts',lambda:refresh_predictions(c,now)),
                      ('research_queue',lambda:prepare_research(c,now)),
                      ('dossiers',lambda:build_dossiers(c,now)),
                      ('forward_paper',lambda:__import__('oracle_v5_settlement').prepare(c,now)),
                      ('portfolio',lambda:__import__('oracle_v56_portfolio').sync(c,now)),
                      ('archive',lambda:archive_due(c,now))):
        try:
            out[name]=work()
        except (sqlite3.OperationalError,ValueError,TypeError) as exc:
            out[name]={'state':'ERROR','kind':type(exc).__name__,'message':str(exc)[:180]}
    state='OK' if all(x.get('state') in ('OK','UNCHANGED_PACKET') for x in out.values()) else 'PARTIAL'
    with c:c.execute('INSERT INTO v5_runs(at,job,state,counts) VALUES(?,?,?,?)',
                     (now,'v5_universe',state,json.dumps(out,ensure_ascii=False)[:12000]))
    return {'state':state,'at':now,'components':out,'orders_placed':0}


def catalog_page(c,sport=None,query=None,limit=50,offset=0,all_history=False):
    args=[];where=[]
    if sport:where.append('sport=?');args.append(sport)
    if query:
        where.append('(home LIKE ? OR away LIKE ? OR league LIKE ? OR root_id LIKE ?)')
        args.extend(['%'+query[:70]+'%']*4)
    where.append('root_exists=1')
    if not all_history:
        where.append('kickoff_utc>=?');args.append(stamp())
        where.append('active_quotes>0')
    else:
        where.append('EXISTS(SELECT 1 FROM fb_full_current q WHERE q.root_event_id=v5_catalog.root_id)')
    cond=(' WHERE '+' AND '.join(where)) if where else ''
    count=c.execute('SELECT COUNT(*) FROM v5_catalog'+cond,args).fetchone()[0]
    sql='''SELECT root_id,sport,league,home,away,kickoff_utc,active_quotes,market_codes,
             nested_events,quote_status,packet_at FROM v5_catalog'''+cond+''' ORDER BY kickoff_utc,root_id LIMIT ? OFFSET ?'''
    keys=['root_id','sport','league','home','away','kickoff_utc','active_quotes','market_codes',
          'nested_events','quote_status','packet_at']
    return {'state':'OK','total':count,'limit':limit,'offset':offset,
            'events':[dict(zip(keys,r)) for r in c.execute(sql,args+[limit,offset])]}


def price_page(c,root,limit=100,offset=0):
    # All active factors for upcoming roots, or truly observed PRE-KICKOFF history.

    # Inactive old quotes never masquerade as current; archive values must be
    # actually observed prior to kickoff (not inferred after the event).
    # Historical archive prices cannot be computed from postgame quotes.
    if not has_table(c,'fb_full_current') or not has_table(c,'v5_catalog'):
        return {'state':'NOT_INITIALIZED','items':[]}
    e=c.execute('SELECT root_id,home,away,sport,kickoff_utc,packet_at FROM v5_catalog WHERE root_id=?',(str(root),)).fetchone()
    if not e:return {'state':'UNKNOWN_EVENT','items':[]}
    kickoff=e[4];historical=bool(kickoff and kickoff<=stamp())
    hist_table=has_table(c,'fb_full_history')
    if historical:
        # Any q.last_snapshot before kickoff is a genuine observed price even if
        # the all-time history table has not been provisioned. Otherwise require
        # the history's own active observation timestamp before kickoff.
        eligible=('''q.first_seen<? AND (q.last_snapshot<? OR EXISTS(
                  SELECT 1 FROM fb_full_history h WHERE h.quote_key=q.quote_key
                  AND h.active=1 AND h.changed_at<?))''' if hist_table else
                  'q.first_seen<? AND q.last_snapshot<?')
        args=[str(root),kickoff,kickoff]+([kickoff] if hist_table else [])
        count=c.execute('SELECT COUNT(*) FROM fb_full_current q WHERE q.root_event_id=? AND '+eligible,args).fetchone()[0]
        if hist_table:
            prices=('''(SELECT h.decimal_price FROM fb_full_history h
                WHERE h.quote_key=q.quote_key AND h.active=1 AND h.changed_at<?
                ORDER BY h.changed_at DESC LIMIT 1)''')
            times=('''(SELECT h.changed_at FROM fb_full_history h
                WHERE h.quote_key=q.quote_key AND h.active=1 AND h.changed_at<?
                ORDER BY h.changed_at DESC LIMIT 1)''')
            sql=('''SELECT q.event_id,q.factor_id,q.point,
                 CASE WHEN ('''+times+''') IS NOT NULL THEN '''+prices+''' ELSE q.decimal_price END,
                 COALESCE('''+times+''',q.last_snapshot),q.quote_key
                 FROM fb_full_current q WHERE q.root_event_id=? AND '''+eligible+'''
                 ORDER BY q.event_id,q.factor_id,q.point LIMIT ? OFFSET ?''')
            params=[kickoff,kickoff,kickoff]+args+[limit,offset]
        else:
            sql=('''SELECT q.event_id,q.factor_id,q.point,q.decimal_price,q.last_snapshot,q.quote_key
                 FROM fb_full_current q WHERE q.root_event_id=? AND '''+eligible+'''
                 ORDER BY q.event_id,q.factor_id,q.point LIMIT ? OFFSET ?''')
            params=args+[limit,offset]
        rows=c.execute(sql,params)
    else:
        count=c.execute('SELECT COUNT(*) FROM fb_full_current WHERE root_event_id=? AND active=1',(str(root),)).fetchone()[0]
        rows=c.execute('''SELECT event_id,factor_id,point,decimal_price,last_snapshot,quote_key
          FROM fb_full_current WHERE root_event_id=? AND active=1
          ORDER BY event_id,factor_id,point LIMIT ? OFFSET ?''',(str(root),limit,offset))
    items=[]
    for event,fac,point,price,at,qkey in rows:
        try:params=json.loads(point or '{}')
        except (TypeError,ValueError):params={'raw':point}
        parameterless=(not params or (isinstance(params,dict) and params.get('p') is None and params.get('pt') is None))
        meaning=KNOWN_FT_1X2.get(str(fac)) if str(event)==str(root) and parameterless else None
        dc=KNOWN_DC.get(str(fac)) if str(event)==str(root) and parameterless else None
        btts=KNOWN_BTTS.get(str(fac)) if str(event)==str(root) and parameterless else None
        tline=total_line(point) if str(event)==str(root) and str(fac) in KNOWN_TOTAL else None
        label=('FULL_TIME_RESULT_'+meaning if meaning else
               'DOUBLE_CHANCE_'+dc if dc else
               'BOTH_TEAMS_SCORE_'+btts if btts else
               'MATCH_TOTAL_'+KNOWN_TOTAL[str(fac)]+'_'+str(tline) if tline is not None else
               'UNDECODED_RAW_MARKET')
        items.append({'event_id':event,'factor_id':fac,'market_label':label,
                      'verified':bool(meaning or dc or btts or tline is not None),
                      'params':params,'price':price,'source_snapshot':at,'quote_key':qkey,
                      'quote_mode':'PRE_KICKOFF_ARCHIVE' if historical else 'ACTIVE_LATEST'})
    return {'state':'OK','root_event_id':root,'sport':e[3],'home':e[1],'away':e[2],
        'kickoff_utc':kickoff,'packet_at':e[5],'total_active_factors':count,
        'historical_archive':historical,'limit':limit,'offset':offset,'items':items,
        'notice':('Only source-observed pre-kickoff historical prices; missing records not reconstructed' if historical else
                  'Every currently active numeric factor; unknown markets remain raw')}


def forecast_page(c,status=None,limit=50,offset=0,future_only=True):
    if not has_table(c,'v5_forecasts'):return {'state':'NOT_INITIALIZED','records':[]}
    where=[];a=[]
    if status:where.append('v5_forecasts.price_status=?');a.append(status)
    # Show only fixtures that truly existed in the observed Fonbet universe.
    where.append('v5_forecasts.root_id IS NOT NULL')
    where.append('EXISTS(SELECT 1 FROM fb_full_current q WHERE q.root_event_id=v5_forecasts.root_id)')
    if future_only:
        where.append('v5_forecasts.match_date>=?');a.append(stamp()[:10])
    cond=(' WHERE '+' AND '.join(where)) if where else ''
    n=c.execute('SELECT COUNT(*) FROM v5_forecasts'+cond,a).fetchone()[0]
    names=('id','origin','original_event_id','created_at','sport','league','home','away',
           'match_date','kickoff_utc','market','selection','probability','model',
           'root_id','price','quote_snapshot','price_status','display_price_origin')
    archived=has_table(c,'v55_archive_prices')
    if archived:
        q=('SELECT '+','.join('f.'+x for x in names[:15])+
           ',COALESCE(f.price,a.price),COALESCE(f.quote_snapshot,a.observed_at),f.price_status,'+
           "CASE WHEN f.price IS NOT NULL THEN 'LIVE_OR_CAPTURED' " +
           "WHEN a.price IS NOT NULL THEN 'HISTORICAL_OBSERVED' ELSE 'MISSING' END"+
           ' FROM v5_forecasts f LEFT JOIN v55_archive_prices a ON a.forecast_id=f.id'+cond.replace('v5_forecasts.','f.')+
           ' ORDER BY f.match_date,f.home,f.market,f.selection LIMIT ? OFFSET ?')
    else:
        q=('SELECT '+','.join(names[:18])+",'NO_ARCHIVE' FROM v5_forecasts"+cond+
           ' ORDER BY match_date,home,market,selection LIMIT ? OFFSET ?')
    return {'state':'OK','total':n,'limit':limit,'offset':offset,
            'fonbet_only':True,'records':[dict(zip(names,r)) for r in c.execute(q,a+[limit,offset])]}


def status(c):
    names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cnt=lambda t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] if t in names else 0
    pk=source_packet(c)
    age=round((parse(stamp())-parse(pk['at'])).total_seconds()/60,1) if pk else None
    scores={k:n for k,n in c.execute('SELECT price_status,COUNT(*) FROM v5_forecasts GROUP BY price_status')} if 'v5_forecasts' in names else {}
    future_states={k:n for k,n in c.execute('SELECT price_status,COUNT(*) FROM v5_forecasts WHERE match_date>=? GROUP BY price_status',(stamp()[:10],))} if 'v5_forecasts' in names else {}
    decoded=VERIFIED_CODES
    factor_total=(c.execute('SELECT COUNT(DISTINCT factor_id) FROM fb_full_current WHERE active=1').fetchone()[0]
        if 'fb_full_current' in names else 0)
    factor_verified=(c.execute('SELECT COUNT(DISTINCT factor_id) FROM fb_full_current WHERE active=1 AND factor_id IN ('+','.join('?'*len(decoded))+')',tuple(sorted(decoded))).fetchone()[0]
        if 'fb_full_current' in names else 0)
    origin={k:n for k,n in c.execute('SELECT origin,COUNT(*) FROM v5_forecasts GROUP BY origin')} if 'v5_forecasts' in names else {}
    sports=[{'sport':s,'root_events':n,'active_quotes':q} for s,n,q in c.execute('''
         SELECT sport,COUNT(*),SUM(active_quotes) FROM v5_catalog
         GROUP BY sport ORDER BY SUM(active_quotes) DESC''')] if 'v5_catalog' in names else []
    forecasts_future=c.execute("SELECT COUNT(*) FROM v5_forecasts WHERE match_date>=?",(stamp()[:10],)).fetchone()[0] if 'v5_forecasts' in names else 0
    # Show ACTUAL persisted root/kickoff linkage, not only the transient counts
    # from refresh_predictions() that previously concealed NULL kickoff_utc rows.
    sql_now=stamp()
    upcoming_roots=c.execute('''SELECT COUNT(DISTINCT f.root_id) FROM v5_forecasts f
        JOIN v5_catalog e ON e.root_id=f.root_id
        WHERE f.kickoff_utc>? AND e.root_exists=1 AND e.active_quotes>0''',(sql_now,)).fetchone()[0] if 'v5_forecasts' in names and 'v5_catalog' in names else 0
    linked_missing_kickoff=c.execute('''SELECT COUNT(*) FROM v5_forecasts f
        JOIN v5_catalog e ON e.root_id=f.root_id
        WHERE f.kickoff_utc IS NULL AND e.kickoff_utc IS NOT NULL''').fetchone()[0] if 'v5_forecasts' in names and 'v5_catalog' in names else 0
    quoted_upcoming_roots=c.execute('''SELECT COUNT(*) FROM v5_catalog
        WHERE kickoff_utc>? AND root_exists=1 AND active_quotes>0''',(sql_now,)).fetchone()[0] if 'v5_catalog' in names else 0
    upcoming_by_sport=[{'sport':sp,'events':n} for sp,n in c.execute('''
        SELECT f.sport,COUNT(DISTINCT f.root_id) FROM v5_forecasts f
        JOIN v5_catalog e ON e.root_id=f.root_id
        WHERE f.kickoff_utc>? AND e.root_exists=1 AND e.active_quotes>0
        GROUP BY f.sport ORDER BY 2 DESC''',(sql_now,))] if 'v5_forecasts' in names and 'v5_catalog' in names else []
    latest=c.execute('SELECT at,state,counts FROM v5_runs ORDER BY id DESC LIMIT 1').fetchone() if 'v5_runs' in names else None
    return {'version':'5.6','state':'PAPER_RESEARCH_ONLY','live_bets':0,'last_run':{'at':latest[0], 'state':latest[1]} if latest else None,
        'fonbet':{'latest_packet':pk,'age_minutes':age,'fresh':age is not None and -2<=age<=SNAPSHOT_MAX_AGE_MIN,
                  'all_root_events_recorded':cnt('v5_catalog'),
                  'quoted_roots':c.execute('SELECT COUNT(*) FROM v5_catalog WHERE active_quotes>0').fetchone()[0] if 'v5_catalog' in names else 0,
                  'factor_observations':c.execute('SELECT SUM(active_quotes) FROM v5_catalog').fetchone()[0] if 'v5_catalog' in names else 0,
                  'sports':sports,
                  'factor_codes_current':factor_total,
                  'factor_codes_with_documented_partial_decode':factor_verified,
                  'opaque_factor_codes':max(0,factor_total-factor_verified)},
        'predictions':{'all_records':cnt('v5_forecasts'),'future_records':forecasts_future,
                       'by_source':origin,'price_states':scores,'future_price_states':future_states,
                       'price_captures':cnt('v5_price_captures'),
                       'covered_with_verified_and_time_valid_odds':scores.get('PRICE_ATTACHED_TIME_VALID',0),
                       'upcoming_root_events_in_catalog':quoted_upcoming_roots,
                       'upcoming_root_events_with_forecasts':upcoming_roots,
                       'forecasted_upcoming_sports':upcoming_by_sport,
                       'linked_forecast_records_missing_kickoff':linked_missing_kickoff,
                       'fonbet_matched_visible':c.execute("SELECT COUNT(*) FROM v5_forecasts f WHERE f.root_id IS NOT NULL AND EXISTS(SELECT 1 FROM fb_full_current q WHERE q.root_event_id=f.root_id)").fetchone()[0] if 'v5_forecasts' in names and 'fb_full_current' in names else 0},
        'research':{'queued_upcoming_events':cnt('v5_research_queue'),
                    'found_articles':cnt('v5_research_sources'),
                    'dossiers':cnt('v5_dossiers'),
                    'verified_player_rosters':c.execute("SELECT COUNT(*) FROM v5_research_queue WHERE verified_lineup='VERIFIED'").fetchone()[0] if 'v5_research_queue' in names else 0,
                    'checked_today_utc':c.execute('SELECT COUNT(*) FROM v5_research_queue WHERE last_checked>=?',(stamp()[:10]+'T00:00:00+00:00',)).fetchone()[0] if 'v5_research_queue' in names else 0,
                    'requested_daily_capacity':2304},
        'paper':__import__('oracle_v5_settlement').summary(c) if 'v55_paper' in names else {'state':'NOT_INITIALIZED'},
        'portfolio':__import__('oracle_v56_portfolio').summary(c) if 'v56_portfolio' in names else {'state':'NOT_INITIALIZED'},
        'limitations':['All observable Fonbet factors are catalogued but opaque factor IDs are not interpreted as specific markets.',
                       'Some forecasts have no verifiable event link; no fabricated odds.',
                       'Sports with insufficient historical results receive no reliable independent probability.',
                       'Player emotional state is not inferred without verifiable public evidence.',
                       'No independently proven betting edge or complete worldwide coverage.']}


def triage(c,limit=30,now=None):
    """Read-only evidence for remaining links and markets, never auto-accepts hints."""
    at=stamp(now)
    rows=c.execute("""SELECT origin,original_event_id,price_status,
       sport,league,home,away,match_date,MAX(root_id),COUNT(*)
       FROM v5_forecasts WHERE match_date>=? AND price_status!= 'PRICE_ATTACHED_TIME_VALID'
       GROUP BY origin,original_event_id,price_status
       ORDER BY COUNT(*) DESC,match_date LIMIT ?""",(at[:10],limit)).fetchall()
    events={r['root_id']:r for r in catalog_events(c)}
    by_date=defaultdict(list)
    for r in events.values():
        if r['kickoff_utc'] and r['root_exists']:
            by_date[r['kickoff_utc'][:10]].append(r)
    out=[]
    for origin,eid,state,sport,league,home,away,day,rid,n in rows:
        pred={'origin':origin,'event_id':eid,'sport':sport,'league':league,
              'home':home,'away':away,'date':day,'kick':None}
        method='EXISTING_LINK' if rid else None
        hints=[]
        if not rid:
            result,method,hints=detect_match(pred,by_date,events,{})
        details=[]
        for score,cid in sorted(hints,reverse=True)[:5]:
            ev=events.get(cid)
            if not ev:continue
            details.append({'id':cid,'home':ev['home'],'away':ev['away'],
                'league':ev['league'],'start_utc':ev['kickoff_utc'],
                'place':ev['place'],'similarity_discovery_only':round(score,3)})
        if rid and rid in events:
            root_quotes=c.execute("""SELECT factor_id,point,COUNT(*)
                 FROM fb_full_current WHERE active=1 AND event_id=root_event_id
                 AND root_event_id=? AND factor_id IN ('921','922','923','930','931','1727','1728')
                 GROUP BY factor_id,point ORDER BY factor_id,point LIMIT 25""",(rid,)).fetchall()
        else:root_quotes=[]
        out.append({'origin':origin,'event_id':eid,'status':state,
          'sport':sport,'league':league,'day':day,'home':home,'away':away,
          'fonbet_root_id':rid,'forecast_records':n,'matching_rule':method,'candidates_not_approved':details,
          'root_total_and_1x2_codes':[{'code':x[0],'point':x[1],'quotes':x[2]} for x in root_quotes]})
        if len(out)>=limit:break
    return {'state':'READ_ONLY_DIAGNOSTIC','unresolved_examples':len(out),
            'entries':out,'notes':['Candidate hints are NOT confirmed event links',
            'Root codes do not imply offered total line unless p/pt valid']}


def doctor(c):
    s=status(c);problems=[];checks=[]
    def add(name,ok,reason):
        checks.append({'component':name,'state':'OK' if ok else 'ACTION_REQUIRED','detail':reason})
        if not ok:problems.append(name)
    f=s['fonbet'];p=s['predictions'];r=s['research']
    add('FONBET_FRESHNESS',f['fresh'], 'Source age in minutes: '+str(f['age_minutes']))
    add('FULL_ROOT_EVENT_CATALOGUE',f['all_root_events_recorded']>0,
        'Catalogued root events: '+str(f['all_root_events_recorded']))
    add('FORECAST_INVENTORY',p['all_records']>0,
        'Registry records (not unique matches): '+str(p['all_records']))
    add('FORECAST_KICKOFF_SYNC',p.get('linked_forecast_records_missing_kickoff',0)==0,
        'Linked forecast rows with NULL kickoff despite known Fonbet start: '
        +str(p.get('linked_forecast_records_missing_kickoff',0)))
    source_counts={}
    for t in ('sf_forecasts','v4_sport_preds','sb_independent_forecasts'):
        if has_table(c,t):source_counts[t]=c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
        else:source_counts[t]=None
    for t in ('sf_forecasts','v4_sport_preds'):
        if source_counts[t] is not None:
            add('INVENTORY_'+t,p['by_source'].get(t,0)>=source_counts[t],
                str(p['by_source'].get(t,0))+' indexed / '+str(source_counts[t])+' source records')
    uncertain=p.get('future_price_states',{})
    linked_price=uncertain.get('PRICE_ATTACHED_TIME_VALID',0)
    missing=sum(value for key,value in uncertain.items() if key in (
        'EVENT_UNMATCHED','EVENT_AMBIGUOUS','NO_ACTIVE_MATCHING_QUOTE','MARKET_NOT_VERIFIED','SOURCE_STALE'))
    add('FORECAST_PRICE_COMPLETENESS',missing==0,
        str(linked_price)+' verified time-valid; '+str(missing)+' unmatched/unsupported/stale among today-and-later')
    add('FACTOR_DICTIONARY',f['opaque_factor_codes']==0,
        str(f['opaque_factor_codes'])+' remaining opaque factor IDs (original raw quotes still accessible)')
    paper=s.get('paper',{})
    add('PAPER_ACCOUNTING_READY',paper.get('state')=='PAPER_ONLY',
        'Ten-dollar research-only paper ledger: '+str(paper.get('state')))
    add('PLAYER_INFORMATION',r['verified_player_rosters']>0,
        str(r['verified_player_rosters'])+' verified player rosters; no inventions')
    jobs=[]
    if has_table(c,'v4_jobs'):
        for row in c.execute("SELECT job,last_run,status,substr(details,1,450) FROM v4_jobs WHERE job IN ('v4_v5_full_universe','v4_v5_evidence_search','v4_fonbet_first_party','v4_v5_paper_settlement_6h') ORDER BY job"):
            jobs.append({'job':row[0],'last_run':row[1],'state':row[2],'detail':row[3]})
        job_names={j['job'] for j in jobs}
        add('AUTOMATION_SCHEDULED', 'v4_v5_full_universe' in job_names,
            'Scheduler entry '+('present' if 'v4_v5_full_universe' in job_names else 'not yet observed'))
    else:add('AUTOMATION_SCHEDULED',False,'Missing scheduler table')
    return {'state':'ACTION_REQUIRED' if problems else 'OBSERVED','problems':problems,
            'checks':checks,'scheduler':jobs,'source_forecast_counts':source_counts,
            'scope':'Read-only installed-database audit; server uptime cannot be proven offline',
            'snapshot':s}



def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('once','init','status','doctor','events','odds','forecasts','evidence','triage','settle','paper','portfolio','archive'))
    p.add_argument('--db',default=DB);p.add_argument('--event');p.add_argument('--sport')
    p.add_argument('--query');p.add_argument('--status');p.add_argument('--limit',type=int,default=50)
    p.add_argument('--offset',type=int,default=0);p.add_argument('--history',action='store_true')
    p.add_argument('--batch',type=int,default=48)
    args=p.parse_args()
    if not 1<=args.limit<=200 or not 0<=args.offset<=1000000 or not 1<=args.batch<=80:raise SystemExit('Invalid limit or offset or batch')
    readonly=args.action in ('status','doctor','events','odds','forecasts','triage','paper','portfolio')
    uri=('file:'+args.db+'?mode=ro') if readonly else args.db
    with sqlite3.connect(uri,uri=readonly,timeout=45) as c:
        c.execute('PRAGMA busy_timeout=45000')
        if args.action=='once':ans=run(c)
        elif args.action=='init':setup(c);ans={'state':'OK','version':'5.6'}
        elif args.action=='evidence':setup(c);ans=check_public_evidence(c,max_events=args.batch)
        elif args.action=='status':ans=status(c)
        elif args.action=='doctor':ans=doctor(c)
        elif args.action=='triage':ans=triage(c,args.limit)
        elif args.action=='settle':ans=__import__('oracle_v5_settlement').settle(c)
        elif args.action=='archive':setup(c);ans=__import__('oracle_v5_settlement').backfill(c)
        elif args.action=='paper':ans=__import__('oracle_v5_settlement').paper_page(c,args.limit,args.offset)
        elif args.action=='portfolio':ans=__import__('oracle_v56_portfolio').page(c,args.limit,args.offset)
        elif args.action=='events':ans=catalog_page(c,args.sport,args.query,args.limit,args.offset,args.history)
        elif args.action=='odds':ans=price_page(c,args.event or '',args.limit,args.offset)
        else:ans=forecast_page(c,args.status,args.limit,args.offset,not args.history)
    print(json.dumps(ans,ensure_ascii=False,indent=2,default=str))

if __name__=='__main__':main()

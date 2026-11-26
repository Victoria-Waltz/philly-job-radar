import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import time
from threading import Lock
import urllib.error
import urllib.parse as url
import urllib.request as request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = os.getenv('RADAR_DB', str(ROOT / 'radar.sqlite3'))
BOARDS = [('Greenhouse', 'stripe', 'Stripe'), ('Greenhouse', 'discord', 'Discord'),
          ('Ashby', 'ramp', 'Ramp'), ('Lever', 'palantir', 'Palantir'),
          ('Lever', 'gopuff', 'Gopuff'), ('Greenhouse', 'perpay', 'Perpay'),
          ('Greenhouse', 'healthverity', 'HealthVerity'),
          ('Lever', 'freedompay', 'FreedomPay'), ('Lever', 'veeva', 'Veeva Systems'),
          ('Greenhouse', 'trellahealth', 'Trella Health'), ('Greenhouse', 'fortrobotics', 'FORT Robotics'),
          ('Greenhouse', 'datasystemsanalystsinc', 'Data Systems Analysts')]
GRAD_PRESET = dict(any='Software Engineer, Software Developer, Junior Developer, Application Developer, Technology Associate, Technology Analyst, Tech Delivery Analyst, Technology Consultant, Technology Rotational, IT Project Coordinator, Technical Project Analyst, Business Analyst, Business Systems Analyst',
               exclude='internship, senior, staff engineer, principal', location='Philadelphia, PA', scope='25', work=[], date='any', unknown_date=True, experience=['New Grad','Entry Level','Associate','Unspecified'], employment=['Full-time','Unspecified'], max_years='2', start='Spring/Summer 2027', unknown_start=True, grad='2027-05', unknown_grad=True, sort='Best Match', state='PA')
ROLE_PATTERNS = {
    'Software Engineering': r'\b(?:software|web|application|app|mobile|backend|back.end|frontend|front.end|full.stack|platform|systems|integration|embedded|firmware|api|java|python|javascript|android|ios|react|\.net)\b.*\b(?:engineer(?:ing)?|developer|development|programmer|architect|intern(?:ship)?)\b|\b(?:developer|programmer|software engineer)\b',
    'Data and AI': r'\b(?:data|database|analytics|business intelligence|machine learning|artificial intelligence|ai|ml|reporting)\b.*\b(?:analyst|engineer(?:ing)?|scientist|developer|specialist|architect|consultant|administrator|manager|intern(?:ship)?)\b|\bdata science\b',
    'IT and Cybersecurity': r'\b(?:information technology|it|tech(?:nical)? support|help desk|service desk|cloud|devops|devsecops|cyber(?:security)?|information security|security operations|site reliability|network|system[s]?)\b.*\b(?:engineer|analyst|specialist|administrator|associate|technician|consultant|support|coordinator)\b|\b(?:sysadmin|devops|cybersecurity|penetration tester)\b',
    'Technology Consulting': r'\b(?:tech(?:nology|nical)?|digital|solutions|implementation|it|erp|software|salesforce|sap|servicenow|workday)\b.*\b(?:consultant|consulting|analyst|associate|specialist|rotational|advisory|engineer)\b|\b(?:implementation|solutions|functional)\s+consultant\b',
    'Business Analysis and Project Management': r'\b(?:business|systems|process|product|project|program|delivery|requirements|operations|scrum)\b.*\b(?:analyst|manager|coordinator|associate|specialist|owner|master|management|intern(?:ship)?)\b|\b(?:pmo|business analysis)\b',
    'QA and Testing': r'\b(?:qa|quality assurance|software quality|test automation|software test|sdet|quality engineering)\b|\btest\s+(?:engineer|developer|analyst)\b',
}
ROLE_QUERIES = ('software developer', 'data analyst', 'IT support', 'technology consultant',
                'business analyst', 'quality assurance engineer')
DEFAULT = dict(roles=list(ROLE_PATTERNS), location='Philadelphia, PA', scope='50',
               national_remote=True, state='PA', unknown_date=True, date='any', sort='Best Match')
PAGE_SIZE = 50
REMOTIVE_LOCK = Lock()
REMOTIVE_CACHE = {'retry_after':0, 'jobs':[], 'error':None}
SKILLS = ['Python','Java','JavaScript','SQL','Git','Power BI','automation','project coordination']
HOSTS = {'job-boards.greenhouse.io':'Greenhouse','boards.greenhouse.io':'Greenhouse',
         'jobs.lever.co':'Lever','jobs.ashbyhq.com':'Ashby'}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def db():
    conn = sqlite3.connect(DB, timeout=20)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, data TEXT NOT NULL);
    """)
    if not conn.execute("SELECT 1 FROM kv WHERE key='searches'").fetchone():
        conn.execute('INSERT OR IGNORE INTO kv VALUES (?,?)', ('searches',json.dumps({'My May 2027 search':{'filters':GRAD_PRESET}})))
    conn.commit()
    return conn

def get(conn, key, fallback):
    row = conn.execute('SELECT data FROM kv WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else fallback

def put(conn, key, value):
    conn.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data',(key,json.dumps(value)))

def fetch(endpoint, params=None, headers=None):
    endpoint += '?' + url.urlencode(params) if params else ''
    for attempt in range(2):
        try:
            req = request.Request(endpoint, headers={'User-Agent':'PhillyJobRadar/1.0', **(headers or {})})
            with request.urlopen(req, timeout=9) as response:
                raw = response.read(16_000_001)
                if len(raw)>16_000_000:
                    raise ValueError('Feed exceeds size limit')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if attempt or exc.code not in (429,500,502,503,504):
                raise RuntimeError(f'HTTP {exc.code}') from None
            retry = exc.headers.get('Retry-After','1')
            if not retry.isdigit() or int(retry)>3:
                raise RuntimeError(f'HTTP {exc.code} · Retry later') from None
            time.sleep(int(retry))
        except (urllib.error.URLError, TimeoutError):
            if attempt:
                raise RuntimeError('Connection unavailable') from None

def clean(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', html.unescape(value or '')))).strip()

def canonical(value):
    p = url.urlsplit(value or '')
    if p.scheme != 'https' or not p.hostname:
        return ''
    query = url.parse_qs(p.query)
    return url.urlunsplit(('https',p.hostname,p.path.rstrip('/'),
                           url.urlencode([(k,query[k][0]) for k in ('gh_jid','jobId','job_id','id') if k in query]),''))

def board_url(value):
    p=url.urlsplit(value)
    slug=p.path.strip('/').split('/')[0]
    if p.scheme!='https' or p.netloc not in HOSTS or not re.fullmatch(r'[\w-]{1,80}',slug):
        raise ValueError('Use an HTTPS Greenhouse, Lever or Ashby board URL.')
    return HOSTS[p.netloc],slug,slug.replace('-', ' ').title()

def as_date(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc)
    except (ValueError,AttributeError):
        return None

def normalize(source, company, title, location, description, link, **extra):
    body=clean(description)
    lower=body.lower()
    workplace=extra.pop('workplace','') or ''
    work='Unspecified'
    for label, pattern in [('Hybrid',r'\bhybrid\b'),('On-site',r'\bon[- ]?site\b'),('Remote',r'\bremote\b')]:
        if re.fullmatch(pattern, workplace.lower()) or (not workplace and re.search(pattern, f'{title} {location}'.lower())):
            work=label
            break
    if work=='Unspecified' and not workplace:
        explicit=re.search(r'(?:work arrangement|workplace type|work model)\s*[:–-]\s*(hybrid|remote|on[- ]?site)',lower)
        if explicit:
            work={'hybrid':'Hybrid','remote':'Remote'}.get(explicit[1],'On-site')
    years=[]
    preferred=[]
    for match in re.finditer(r'(\d{1,2})(?:\s*[–-]\s*\d{1,2})?\+?\s+years? (?:of )?(?:\w+ ){0,4}experience\b', lower):
        context=lower[max(0,match.start()-75):match.end()+35]
        if re.search(r'preferred|nice to have|bonus|a plus',context):
            preferred.append(int(match[1]))
        elif re.search(r'requires?|required|minimum|at least|must have|qualifications|requirements',context):
            years.append(int(match[1]))
    level='Unspecified'
    for label,pattern in [('Senior',r'\b(senior|sr\.?|staff|principal|director)\b'),('New Grad',r'\b(?:new|recent|university|college)[ -]+grad(?:uate)?s?\b|\bgraduate\s+(?:program|scheme|engineer|developer|trainee|analyst)\b|\b(?:early career|campus hire|campus recruiting|university hire)\b'),('Entry Level',r'entry.level|\bjunior\b|\bi$'),('Associate',r'\bassociate\b'),('Mid-Level',r'mid.level|\bii\b')]:
        if re.search(pattern,title,re.I):
            level=label
            break
    start=re.search(r'(?:start(?:ing| date)?\s*(?:in|:|of)?\s*|\b)(spring|summer|fall|autumn|winter|january|february|march|april|may|june|july|august|september|october|november|december)\s+(20\d\d)',f'{title} {body}',re.I)
    if start and not (re.search(r'start|cohort',f'{title} {body}'[max(0,start.start()-40):start.end()+30],re.I) or start.start()<len(title)):
        start=None
    if not start:
        start=re.search(r'\bstart(?:ing| date)?\s*(?:in|:|of)?\s*(20\d\d)\b',body,re.I)
    if not extra.get('salary_max'):
        salary=re.search(r'(?:USD\s*)?\$([\d,]+)(?:\s*[kK])?\s*(?:to|[-–—])\s*\$([\d,]+)([kK]?)(?:\s*(?:USD|per year|annually|/yr))?',extra.get('salary') or body)
        if salary:
            high=int(salary[2].replace(',',''))*(1000 if salary[3] else 1)
            if high>=10000 and not re.search(r'(?:CAD|AUD|SGD|HKD|NZD|hour|/hr)',(extra.get('salary') or body)[max(0,salary.start()-6):salary.end()+18],re.I):
                extra['salary_max']=high
                extra.setdefault('salary',salary[0]+' USD / year')
    if not extra.get('employment') or extra['employment']=='Unspecified':
        stated=re.search(r'(?:employment type|job type|schedule)\s*[:–-]\s*(full[- ]time|part[- ]time|temporary|contract|internship)',body,re.I)
        if stated:
            extra['employment']=stated[1]
    grad=re.search(r'graduat\w*[^.!?]{0,100}\b20\d\d\b',body,re.I)
    restriction=re.search(r'[^.!?]{0,65}(?:must (?:currently )?(?:reside|be based|live)|US only|U\.S\. only|United States only|except (?:in |for )?|excluding|not eligible in)[^.!?]{0,135}',body,re.I) if work=='Remote' else None
    office=re.search(r'[^.!?]{0,55}\b\d\s+days?\s+(?:per week\s+)?(?:in(?: the)? office|on.site)[^.!?]{0,45}',body,re.I) if work=='Hybrid' else None
    deadline=re.search(r'(?:application deadline|applications close(?: on)?|closing date)\s*[:–-]?\s*(20\d\d-\d\d-\d\d|[A-Za-z]+ \d{1,2},? 20\d\d)',body,re.I)
    if deadline and not extra.get('expires_at'):
        date=as_date(deadline[1])
        if not date:
            try:
                date=dt.datetime.strptime(deadline[1].replace(',',''),'%B %d %Y')
            except ValueError:
                date=None
        if date:
            extra['expires_at']=date.date().isoformat()
    if level=='Unspecified' and re.search(r'\b(?:open to|designed for|ideal for|targeted at)\s+(?:new|recent)\s+graduates\b',lower):
        level='New Grad'
    posted=extra.pop('posted_at',None)
    return dict(source=source, company=clean(company), title=clean(title), location=clean(location),
        description=body, link=link if canonical(link) else '', work=work, experience=level, required=max(years,default=None),
        preferred=max(preferred,default=None), skills=[s for s in SKILLS if re.search(r'\b'+re.escape(s)+r'\b',body,re.I)],
        start=start[0].strip() if start else '', graduation=grad[0] if grad else '',
        restriction=restriction[0].strip() if restriction else '', office=office[0].strip() if office else '',
        posted_at=posted if as_date(posted) else None, precision=('day' if len(posted)==10 else 'timestamp') if as_date(posted) else None,
        posting_source=source if as_date(posted) else None, **extra)

def requisition(value):
    value=str(value or '').strip()
    # Some boards put instructions such as "See Opening ID" in this field.
    return value if re.search(r'\d',value) and len(value)<=80 else ''


def employer(board):
    provider, slug, name=board
    if provider=='Greenhouse':
        rows=fetch(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs',{'content':'true'})['jobs']
        return [normalize(f'{provider} · {name}',name,r['title'],(r.get('location') or {}).get('name',''),r.get('content'),r['absolute_url'],
            req=requisition(r.get('requisition_id')), updated_at=r.get('updated_at'), employer=True) for r in rows]
    if provider=='Ashby':
        rows=fetch(f'https://api.ashbyhq.com/posting-api/job-board/{slug}',{'includeCompensation':'true'})['jobs']
        return [normalize(f'{provider} · {name}',name,r['title'],r.get('location',''),r.get('descriptionPlain'),r['jobUrl'],
            apply=r.get('applyUrl'), workplace=r.get('workplaceType') or ('Remote' if r.get('isRemote') else ''),
            employment=r.get('employmentType','Unspecified'), posted_at=r.get('publishedAt'),
            salary=(r.get('compensation') or {}).get('compensationTierSummary'), employer=True) for r in rows if r.get('isListed',True)]
    rows=[]
    for offset in range(0,1000,100):
        page=fetch(f'https://api.lever.co/v0/postings/{slug}',{'mode':'json','skip':offset,'limit':100})
        rows.extend(page)
        if len(page)<100:
            break
    return [normalize(f'{provider} · {name}',name,r['text'],r['categories'].get('location',''),r.get('descriptionPlain'),r['hostedUrl'],
        apply=r.get('applyUrl'), workplace=r.get('workplaceType'),employment=r['categories'].get('commitment','Unspecified'),
        industry=r['categories'].get('department',''), employer=True) for r in rows]

def terms(value):
    return [x.strip().lower() for x in str(value or '').replace(';',',').split(',') if x.strip()]

def adzuna(filters):
    found=[]
    keywords=terms(filters.get('any'))[:6] or (list(ROLE_QUERIES) if filters.get('roles') else [''])
    places=[''] if filters.get('scope')=='nationwide' else (locations(filters) or [''])
    for place in places[:3]:
        for keyword in keywords:
            for page in (1,2):
                rows=fetch(f'https://api.adzuna.com/v1/api/jobs/us/search/{page}',
                    dict(app_id=os.environ['ADZUNA_APP_ID'],app_key=os.environ['ADZUNA_APP_KEY'],results_per_page=30,what=keyword,where=place))['results']
                for r in rows:
                    salary=(f"${r['salary_min']:,.0f}–${r['salary_max']:,.0f} USD / year" if r.get('salary_min') and r.get('salary_max') and not r.get('salary_is_predicted') else None)
                    found.append(normalize('Adzuna',r['company']['display_name'],r['title'],r['location']['display_name'],
                        r.get('description'),r['redirect_url'],posted_at=r.get('created'),employment=r.get('contract_time','Unspecified'),
                        lat=r.get('latitude'),lon=r.get('longitude'),salary=salary,salary_max=r.get('salary_max') if salary else None,
                        industry=(r.get('category') or {}).get('label',''),employer=False))
                if len(rows)<30:
                    break
    return found

def remotive(_filters):
    with REMOTIVE_LOCK:
        current=time.monotonic()
        if current < REMOTIVE_CACHE['retry_after']:
            if REMOTIVE_CACHE['error']:
                raise RuntimeError(REMOTIVE_CACHE['error'])
            return REMOTIVE_CACHE['jobs']
        try:
            rows=fetch('https://remotive.com/api/remote-jobs')['jobs']
        except (RuntimeError,ValueError,KeyError) as exc:
            REMOTIVE_CACHE.update(retry_after=time.monotonic()+900,error='Remote listings temporarily unavailable')
            raise RuntimeError(REMOTIVE_CACHE['error']) from exc
        jobs=[]
        checked_at=now()
        for row in rows:
            region=row.get('candidate_required_location') or 'Remote · Location eligibility unspecified'
            item=normalize('Remotive',row['company_name'],row['title'],region,row.get('description'),row['url'],
                           workplace='Remote',employment=row.get('job_type'),posted_at=(row.get('publication_date') or '')[:10],
                           industry=row.get('category',''),salary=row.get('salary') or None,employer=False,
                           checked_at=checked_at)
            if row.get('candidate_required_location'):
                item['restriction']='Eligible location: '+region
            jobs.append(item)
        REMOTIVE_CACHE.update(retry_after=time.monotonic()+21600,jobs=jobs,error=None)
        return jobs

def usajobs(filters):
    headers={'User-Agent':os.environ['USAJOBS_EMAIL'],'Authorization-Key':os.environ['USAJOBS_API_KEY']}
    jobs=[]
    keywords=terms(filters.get('any'))[:6] or ROLE_QUERIES
    places=locations(filters)[:2] if filters.get('scope')!='nationwide' else ['']
    queries=[(keyword,place,False) for place in (places or ['']) for keyword in keywords]
    if filters.get('national_remote') and any(places):
        queries.extend((keyword,'',True) for keyword in keywords)
    for keyword,place,remote in queries:
        params={'Keyword':keyword,'LocationName':place,'ResultsPerPage':50,'HiringPath':'public;graduates'}
        if place and filters.get('scope','').isdigit():
            params['Radius']=filters['scope']
        if remote:
            params['RemoteIndicator']='True'
        response=fetch('https://data.usajobs.gov/api/Search',params,headers)
        for result in response['SearchResult']['SearchResultItems']:
            row=result['MatchedObjectDescriptor']
            details=(row.get('UserArea') or {}).get('Details') or {}
            apply=(row.get('ApplyURI') or [None])[0]
            jobs.append(normalize('USAJOBS',row['OrganizationName'],row['PositionTitle'],row.get('PositionLocationDisplay'),
                        details.get('JobSummary'),row['PositionURI'],posted_at=row.get('PublicationStartDate'),
                        expires_at=row.get('ApplicationCloseDate'),apply=apply,employer=True,
                        workplace='Remote' if remote or details.get('RemoteIndicator') is True else '',
                        req=str(row.get('PositionID') or '')))
    return jobs

def web_job_hits(payload):
    for hit in (payload.get('web') or {}).get('results',[]):
        link=hit.get('url','')
        try:
            parsed=url.urlsplit(link)
        except ValueError:
            continue
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            continue
        title=clean(hit.get('title',''))
        if not title or not re.search(r'\b(?:jobs?|careers?|vacanc(?:y|ies)|hiring|apply|workday|recruiting)\b',
                                       f'{title} {parsed.hostname} {parsed.path} {parsed.query}',re.I):
            continue
        yield dict(url=link,title=title,domain=parsed.hostname)

def discovery(filters):
    boards={}
    web_links={}
    career='new graduate' if filters.get('experience')==['New Grad'] else ''
    places=[] if filters.get('scope')=='nationwide' else locations(filters)[:2]
    location=' OR '.join(f'"{place}"' for place in places)
    work=' OR '.join(label for label in filters.get('work',[]) if label!='Unspecified')
    if filters.get('national_remote') and places:
        location=f'({location} OR "US remote")'
    elif len(places)>1:
        location=f'({location})'
    for keyword in terms(filters.get('any'))[:6] or ROLE_QUERIES:
        query=f'"{keyword[:140]}" {location[:200]} {work} {career} job openings'
        result=fetch('https://api.search.brave.com/res/v1/web/search',{'q':query,'count':15},{'X-Subscription-Token':os.environ['BRAVE_SEARCH_API_KEY']})
        for hit in web_job_hits(result):
            web_links.setdefault(hit['url'],hit)
            try:
                board=board_url(hit['url'])
            except ValueError:
                continue
            boards.setdefault(board,set()).add(canonical(hit['url']))
    output=[]
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(employer,board):links for board,links in list(boards.items())[:8]}
        for future in cf.as_completed(futures):
            try:
                output.extend(r for r in future.result() if canonical(r['link']) in futures[future])
            except (RuntimeError,ValueError,KeyError):
                continue
    verified={canonical(item['link']) for item in output}
    links=[hit for hit in web_links.values() if canonical(hit['url']) not in verified]
    return output,links[:40]

def company_key(value):
    return re.sub(r'\W','',value.lower())

def aliases(job):
    keys=set()
    if job.get('link'):
        keys.add('url:'+(canonical(job['link']) if job.get('employer') else job['link']))
    if job.get('req'):
        keys.add('req:'+company_key(job['company'])+':'+job['req'])
    return keys

def merge(prior, incoming, stamp):
    all_jobs={j['id']:j for j in prior}
    index={key:j['id'] for j in prior for key in aliases(j)|set(j.get('aliases',[]))}
    for incoming_job in incoming:
        keys=aliases(incoming_job)
        if not keys:
            continue
        jid=next((index[k] for k in sorted(keys) if k in index), hashlib.sha256(sorted(keys)[0].encode()).hexdigest()[:24])
        old=all_jobs.get(jid,{})
        preferred=incoming_job if incoming_job.get('employer') or not old else old
        combined={**(old if preferred is incoming_job else incoming_job),**preferred}
        if old.get('posted_at') and (old.get('posting_source','').startswith(('Greenhouse','Ashby','Lever')) or not incoming_job.get('employer') or not incoming_job.get('posted_at')):
            for field in ('posted_at','precision','posting_source'):
                combined[field]=old.get(field)
        elif not combined.get('posted_at'):
            for field in ('posted_at','precision','posting_source'):
                combined[field]=incoming_job.get(field) or old.get(field)
        keys |= set(old.get('aliases',[]))
        combined.update(id=jid,aliases=sorted(keys),first_seen_at=old.get('first_seen_at',stamp),last_checked_at=incoming_job.get('checked_at',stamp),
                        availability='Open',sources=sorted(set(old.get('sources',[])+[incoming_job['source']])))
        all_jobs[jid]=combined
        index.update(dict.fromkeys(keys,jid))
    return all_jobs

def locations(filters):
    return [p.strip().lower() for key in ('location','secondary') for p in re.split(r'[;\n]',filters.get(key,'')) if p.strip()]

PHILLY = {'philadelphia':(39.953,-75.165),'conshohocken':(40.08,-75.30),'king of prussia':(40.10,-75.38),
          'wayne':(40.044,-75.387),'malvern':(40.036,-75.514),'radnor':(40.046,-75.369),'wilmington':(39.74,-75.55),
          'camden':(39.94,-75.12),'cherry hill':(39.934,-75.03),'mount laurel':(39.934,-74.893),'horsham':(40.178,-75.128),
          'fort washington':(40.142,-75.207),'pennsauken':(39.956,-75.06),'media':(39.917,-75.387),'bala cynwyd':(40.005,-75.23),
          'feasterville':(40.146,-75.005),'newtown':(40.229,-74.936),'new york':(40.713,-74.006),'washington':(38.907,-77.037)}
ZIP={'19112':'philadelphia','19103':'philadelphia','19104':'philadelphia','19102':'philadelphia','19053':'feasterville','19406':'king of prussia','19087':'wayne','19428':'conshohocken'}
STATE={'pa':'pennsylvania','nj':'new jersey','de':'delaware','ny':'new york','ca':'california','tx':'texas','ma':'massachusetts','fl':'florida'}

def city(place):
    return ZIP.get(place,place.split(',')[0].strip())

def distance(job,place):
    from math import radians, sin, cos, asin, sqrt
    target=PHILLY.get(city(place))
    origin=(job['lat'],job['lon']) if isinstance(job.get('lat'),(int,float)) and isinstance(job.get('lon'),(int,float)) else PHILLY.get(city(job['location'].lower()))
    if not origin or not target:
        return None
    a,b=map(radians,origin)
    c,d=map(radians,target)
    return 3958.8*2*asin(min(1,sqrt(sin((c-a)/2)**2+cos(a)*cos(c)*sin((d-b)/2)**2)))

def employment(job):
    val=re.sub(r'[^a-z]','',(job.get('employment') or '').lower())
    for label in ('Full-time','Part-time','Internship','Contract','Temporary'):
        if val.startswith(label.lower().replace('-','').replace('ship','')):
            return label
    return 'Internship' if re.search(r'\bintern(ship)?\b',job['title'],re.I) else 'Unspecified'

def matches(job,f,clock=None):
    clock=clock or dt.datetime.now(dt.timezone.utc)
    title=job['title'].lower()
    content=title+' '+job['description'].lower() if f.get('descriptions') else title
    roles=f.get('roles') or []
    if roles and not any(re.search(pattern,title) for role,pattern in ROLE_PATTERNS.items() if role in roles): return False
    if terms(f.get('any')) and not any(x in content for x in terms(f['any'])): return False
    if any(x not in content for x in terms(f.get('must'))) or any(x in content for x in terms(f.get('exclude'))): return False
    for key,value in [('work',job['work']),('experience',job['experience']),('employment',employment(job))]:
        if f.get(key) and value not in f[key]: return False
    place_list=locations(f)
    where=job['location'].lower()
    scope=f.get('scope','25')
    if place_list and scope!='nationwide' and not (job['work']=='Remote' and f.get('national_remote')):
        def near(place):
            town=city(place)
            if town in STATE or town in STATE.values():
                abbreviation=next((k for k,v in STATE.items() if v==town),town)
                return bool(re.search(r'\b'+abbreviation+r'\b',where)) or STATE.get(abbreviation,town) in where
            if re.search(r'\b'+re.escape(town)+r'\b',where):
                parts=place.split(',')
                return len(parts)==1 or parts[1].strip() in where or STATE.get(parts[1].strip(),'\0') in where or ',' not in where
            miles=distance(job,place)
            return miles is not None and scope!='exact' and miles <= (25 if scope=='metro' else int(scope) if scope.isdigit() else 25)
        if not any(near(place) for place in place_list): return False
    state=f.get('state','').strip().lower()
    restriction=job.get('restriction','').lower()
    if state and restriction and job['work']=='Remote':
        state_pattern=r'\b(?:'+re.escape(state)+'|'+re.escape(STATE.get(state,state))+r')\b'
        excluded=re.search(r'(?:except|excluding|not eligible in|cannot reside in|not available in)(.*)',restriction)
        if excluded and re.search(state_pattern,excluded[1]): return False
        required=re.search(r'(?:must (?:currently )?(?:reside|be based|live) in) (.*)',restriction)
        if required and any(re.search(r'\b'+name+r'\b',required[1]) for name in STATE.values()) and not re.search(state_pattern,required[1]): return False
    posted=as_date(job.get('posted_at'))
    age=f.get('date','any')
    if not posted and not f.get('unknown_date'): return False
    if posted and age in ('1h','24h','3d','7d','30d'):
        limit={'1h':1,'24h':24,'3d':72,'7d':168,'30d':720}[age]
        if job['precision']=='day':
            if age in ('1h','24h') or not 0 <= (clock.date()-posted.date()).days < limit//24: return False
        elif not 0 <= (clock-posted).total_seconds() <= limit*3600: return False
    if posted and age=='custom':
        end=f.get('date_end') or '9999-12-31'
        if not (f.get('date_start') or '0001-01-01') <= posted.date().isoformat() <= end: return False
    if f.get('max_years','').isdigit() and (job.get('required') or 0)>int(f['max_years']): return False
    start=f.get('start','').strip().lower()
    actual=job.get('start','').lower()
    if start and not actual and not f.get('unknown_start'): return False
    if start and actual:
        year=re.search(r'20\d\d',start)
        if year and year[0] not in actual: return False
        if 'spring/summer' in start and not re.search(r'spring|summer|march|april|may|june|july|august',actual) and re.search(r'[a-z]{4}',actual.replace('start','')): return False
        if 'spring/summer' not in start and re.search(r'[a-z]',start) and start.split()[0] not in actual:
            seasons={'spring':'march april may','summer':'june july august','fall':'september october november','winter':'december january february'}
            if not any(month in actual for month in seasons.get(start.split()[0],'').split()): return False
    grad=f.get('grad','')
    if grad:
        if not job.get('graduation') and not f.get('unknown_grad'): return False
        year=grad[:4]
        years=re.findall(r'20\d\d',job.get('graduation',''))
        if years and not int(min(years))<=int(year)<=int(max(years)): return False
        months='january february march april may june july august september october november december'.split()
        if len(grad)==7 and len(years)==1 and 1<=int(grad[5:])<=12:
            stated=[m for m in months if re.search(r'\b'+m+r'\b',job.get('graduation','').lower())]
            if len(stated)==1 and months[int(grad[5:])-1] not in stated: return False
    for key, value in [('industry',job.get('industry','')),('source',' '.join(job.get('sources',[]))),('companies',job['company'])]:
        if terms(f.get(key)) and not any(term in value.lower() for term in terms(f[key])): return False
    if any(term in job['company'].lower() for term in terms(f.get('exclude_companies'))): return False
    if f.get('salary','').isdigit():
        high=job.get('salary_max')
        if high is None and not f.get('unknown_salary'): return False
        if high is not None and high<int(f['salary']): return False
    return True

def results(conn, filters):
    tracking=get(conn,'tracking',{})
    jobs=[]
    for (data,) in conn.execute('SELECT data FROM jobs'):
        job=json.loads(data)
        if job['availability']=='Closed' or tracking.get(job['id'],{}).get('hidden') or not matches(job,filters):
            continue
        job['distance']=min((d for place in locations(filters) if (d:=distance(job,place)) is not None),default=None)
        job['employment']=employment(job)
        job['score']=(8 if 'philadelphia' in job['location'].lower() else 0)+(6 if job['experience'] in ('New Grad','Entry Level','Associate') else 0)+(4 if '2027' in job['start'] or '2027' in job['graduation'] else 0)+len(job['skills'])
        jobs.append(job)
    sort=filters.get('sort','Best Match')
    epoch=dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    def key(job):
        posted=as_date(job.get('posted_at')) or epoch
        if sort=='Newest Posted': return (posted,job['first_seen_at'])
        if sort=='Recently Discovered': return (job['first_seen_at'],posted)
        if sort=='Closest': return (-(job['distance'] if job['distance'] is not None else 100000),posted)
        if sort=='Highest Published Salary': return (job.get('salary_max') or 0,posted)
        return (job['score'],posted)
    return sorted(jobs,key=key,reverse=True)

def refresh(filters, custom, previous):
    sources={f'{b[0]} · {b[2]}':(employer,b) for b in BOARDS+[tuple(b) for b in custom]}
    sources['Remotive']=(remotive,filters)
    optional={'USAJOBS':(usajobs,bool(os.getenv('USAJOBS_EMAIL') and os.getenv('USAJOBS_API_KEY'))),
              'Adzuna':(adzuna,bool(os.getenv('ADZUNA_APP_ID') and os.getenv('ADZUNA_APP_KEY'))),
              'Brave Search':(discovery,bool(os.getenv('BRAVE_SEARCH_API_KEY')))}
    status={name:{'state':'awaiting credentials'} for name,(_,ready) in optional.items() if not ready}
    sources.update({name:(fn,filters) for name,(fn,ready) in optional.items() if ready})
    incoming=[]
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        pending={pool.submit(fn,arg):name for name,(fn,arg) in sources.items()}
        for future in cf.as_completed(pending):
            name=pending[future]
            try:
                jobs=future.result()
            except Exception as exc:
                status[name]={'state':'unavailable','last_success':previous.get(name,{}).get('last_success'),
                              'detail':str(exc) if isinstance(exc,RuntimeError) else 'Unexpected feed response'}
            else:
                links=[]
                if name=='Brave Search':
                    jobs,links=jobs
                incoming.extend(jobs)
                status[name]={'state':'searched','last_success':now(),'count':len(jobs)}
                if links:
                    status[name]['web_links']=links
    return status,incoming

def snapshot(conn):
    tracking=get(conn,'tracking',{})
    jobs={j['id']:j for (raw,) in conn.execute('SELECT data FROM jobs') if (j:=json.loads(raw))['id'] in tracking}
    searches=get(conn,'searches',{})
    for item in searches.values():
        item['new']=sum(j['first_seen_at']>(item.get('viewed') or '') for j in results(conn,item['filters']))
    boards=get(conn,'boards',[])
    statuses={f'{provider} · {name}':{'state':'configured'} for provider,_,name in BOARDS+[tuple(b) for b in boards]}
    statuses['Remotive']={'state':'configured'}
    statuses['USAJOBS']={'state':'configured' if os.getenv('USAJOBS_EMAIL') and os.getenv('USAJOBS_API_KEY') else 'awaiting credentials'}
    statuses['Adzuna']={'state':'configured' if os.getenv('ADZUNA_APP_ID') and os.getenv('ADZUNA_APP_KEY') else 'awaiting credentials'}
    statuses['Brave Search']={'state':'configured' if os.getenv('BRAVE_SEARCH_API_KEY') else 'awaiting credentials'}
    statuses.update({name:status for name,status in get(conn,'sources',{}).items() if name in statuses})
    return dict(default=DEFAULT, roles=list(ROLE_PATTERNS), searches=searches, tracking=tracking, tracked_jobs=jobs, sources=statuses, boards=boards)

class Handler(BaseHTTPRequestHandler):
    def reply(self,value,status=200):
        raw=json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path=='/api/state':
            with closing(db()) as conn,conn:
                self.reply(snapshot(conn))
            return
        path={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}.get(url.urlsplit(self.path).path)
        if not path:
            self.send_error(404)
            return
        raw=(ROOT/path[0]).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type',path[1]+'; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        try:
            if self.headers.get('Content-Type')!='application/json':
                raise ValueError('Expected application/json')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<100000:
                raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(size))
            self.dispatch(body)
        except (ValueError,KeyError,TypeError) as exc:
            self.reply({'error':str(exc)},400)

    def dispatch(self,body):
        action=self.path
        if action in ('/api/search','/api/filter'):
            filters=body['filters']
            name=body.get('name')
            if action=='/api/search':
                with closing(db()) as conn,conn:
                    custom=get(conn,'boards',[])
                    previous=get(conn,'sources',{})
                statuses,incoming=refresh(filters,custom,previous)
            with closing(db()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                searches=get(conn,'searches',{})
                if action=='/api/search':
                    stamp=now()
                    prior=[json.loads(raw) for (raw,) in conn.execute('SELECT data FROM jobs')]
                    jobs=merge(prior,incoming,stamp)
                    for job in jobs.values():
                        if job['last_checked_at']!=stamp and job.get('employer') and statuses.get(job['source'],{}).get('state')=='searched' and job['availability']!='Closed':
                            job['availability']='Unconfirmed'
                    conn.executemany('INSERT OR REPLACE INTO jobs VALUES (?,?)',((j,json.dumps(data)) for j,data in jobs.items()))
                    put(conn,'sources',statuses)
                    if name in searches and any(s['state']=='searched' for s in statuses.values()):
                        searches[name]['last_refresh']=stamp
                        put(conn,'searches',searches)
                elif body.get('view') and name in searches:
                    searches[name]['viewed']=now()
                    put(conn,'searches',searches)
                selected=results(conn,filters)
                total=conn.execute('SELECT count(*) FROM jobs').fetchone()[0]
                page_count=max(1,(len(selected)+PAGE_SIZE-1)//PAGE_SIZE)
                page=min(page_count,max(1,int(body.get('page',1))))
                start=(page-1)*PAGE_SIZE
                self.reply(dict(jobs=selected[start:start+PAGE_SIZE],count=len(selected),total=total,
                                page=page,page_count=page_count,**snapshot(conn)))
            return
        with closing(db()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if action=='/api/saved-search':
                searches=get(conn,'searches',{})
                name=body['name'].strip()[:100]
                if not name: raise ValueError('Enter a search name.')
                if body.get('remove'): searches.pop(name,None)
                else: searches[name]={**searches.get(name,{}),'filters':body['filters'],'viewed':now()}
                put(conn,'searches',searches)
            elif action=='/api/track':
                jid=body['id']
                if not conn.execute('SELECT 1 FROM jobs WHERE id=?',(jid,)).fetchone():
                    raise ValueError('Listing unavailable')
                tracking=get(conn,'tracking',{})
                tracking[jid]={**tracking.get(jid,{}),**{k:v for k,v in body.items() if k in ('saved','hidden','status','date','notes')}}
                put(conn,'tracking',tracking)
            elif action=='/api/board':
                board=board_url(body['url'])
                if body.get('name'): board=(*board[:2],body['name'].strip()[:80])
                boards=get(conn,'boards',[])
                boards=[b for b in boards if tuple(b[:2])!=board[:2]]
                if not body.get('remove'): boards.append(board)
                put(conn,'boards',boards)
            else:
                self.send_error(404)
                return
            self.reply(snapshot(conn))

if __name__=='__main__':
    server=ThreadingHTTPServer((os.getenv('RADAR_HOST','127.0.0.1'),int(os.getenv('PORT','8000'))),Handler)
    print(f'Philly Job Radar listening at http://{server.server_address[0]}:{server.server_port}',flush=True)
    server.serve_forever()

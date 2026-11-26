import datetime as dt
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from unittest.mock import patch

import radar


def job(**overrides):
    params = dict(source='Ashby · Example', company='Example', title='Software Engineer I',
                  location='Philadelphia, PA', description='Java. Minimum 2 years of experience.',
                  link='https://jobs.ashbyhq.com/example/first', employer=True,
                  workplace='Hybrid', employment='FullTime', req='101')
    params.update(overrides)
    return radar.normalize(**params)


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.clock = dt.datetime(2026,11,26,18,tzinfo=dt.timezone.utc)
        self.filters = dict(location='Philadelphia, PA', scope='25', work=['Hybrid'],
                            employment=['Full-time'], date='7d', unknown_date=False, max_years='2')

    def test_broad_default_and_new_grad_filter(self):
        for title in ('Software Engineer III', 'Software Engineering Internship', 'Data Analyst', 'IT Support Specialist',
                      'Implementation Consultant', 'Business Systems Analyst', 'QA Engineer'):
            with self.subTest(title=title):
                self.assertTrue(radar.matches(job(title=title),radar.DEFAULT,self.clock))
        self.assertFalse(radar.matches(job(title='Restaurant Server'),radar.DEFAULT,self.clock))
        self.assertTrue(radar.matches(job(title='Software Developer',location='United States',
                                          workplace='Remote'),radar.DEFAULT,self.clock))
        new_grad=dict(radar.DEFAULT,experience=['New Grad'])
        self.assertTrue(radar.matches(job(title='Software Engineer, New Graduate'),new_grad,self.clock))
        self.assertFalse(radar.matches(job(title='Senior Software Engineer'),new_grad,self.clock))

    def test_combined_filters(self):
        self.assertTrue(radar.matches(job(posted_at='2026-11-25'),self.filters,self.clock))
        for variant in (job(),job(posted_at='2026-11-10'),job(posted_at='2026-11-25',workplace='Remote'),
                        job(posted_at='2026-11-25',location='New York, NY'),
                        job(posted_at='2026-11-25',employment='PartTime')):
            self.assertFalse(radar.matches(variant,self.filters,self.clock))
        self.assertTrue(radar.matches(job(),dict(self.filters,unknown_date=True),self.clock))

    def test_dates(self):
        exact=job(posted_at='2026-11-26T17:30:00Z')
        day=job(posted_at='2026-11-26')
        self.assertTrue(radar.matches(exact,dict(self.filters,date='1h'),self.clock))
        self.assertFalse(radar.matches(day,dict(self.filters,date='1h'),self.clock))
        self.assertTrue(radar.matches(day,dict(self.filters,date='custom',date_start='2026-11-26',date_end='2026-11-26'),self.clock))
        self.assertIsNone(job(updated_at='2026-11-26T17:30:00Z')['posted_at'])

    def test_remote_eligibility_and_experience(self):
        restricted=job(workplace='Remote',description='US only, excluding Pennsylvania.')
        self.assertIn('Pennsylvania',restricted['restriction'])
        self.assertFalse(radar.matches(restricted,dict(scope='nationwide',unknown_date=True,state='PA')))
        self.assertTrue(radar.matches(restricted,dict(scope='nationwide',unknown_date=True,state='NJ')))
        self.assertEqual(job(description='Preferred: 3 years of experience.')['preferred'],3)
        self.assertEqual(job(description='This role requires 3 years of experience.')['required'],3)
        self.assertFalse(radar.matches(job(description='Requires Java. Minimum 3 years of experience.'),dict(self.filters,unknown_date=True)))

    def test_duplicate_employer_priority_and_timestamps(self):
        provider=job(source='Adzuna',link='https://example.org/job',employer=False,posted_at='2026-11-24')
        original=job(posted_at='2026-11-25')
        other=job(link='https://jobs.ashbyhq.com/example/second',req='102')
        prior=radar.merge([], [provider], '2026-11-26T14:00:00Z')
        saved=radar.merge(prior.values(),[original,other],'2026-11-26T17:00:00Z')
        self.assertEqual(len(saved),2)
        chosen=saved[next(iter(prior))]
        self.assertEqual(chosen['link'],original['link'])
        self.assertEqual(chosen['posted_at'],'2026-11-25')
        self.assertEqual(chosen['first_seen_at'],'2026-11-26T14:00:00Z')
        changed=radar.merge(saved.values(),[job(posted_at='2026-11-26')],'2026-11-26T18:00:00Z')
        self.assertEqual(changed[chosen['id']]['posted_at'],'2026-11-25')

    def test_placeholder_requisition_is_ignored(self):
        self.assertEqual(radar.requisition('See Opening ID'),'')
        self.assertEqual(radar.requisition('R0041642'),'R0041642')

    def test_partial_and_persistence_through_http(self):
        def publisher(board):
            if board[1]=='broken':
                raise RuntimeError('HTTP 503')
            return [job(),job(req='102',link='https://jobs.ashbyhq.com/example/second')]
        with tempfile.TemporaryDirectory() as tmp, patch.object(radar,'DB',tmp+'/test.sqlite3'), \
             patch.object(radar,'BOARDS',[('Ashby','example','Example'),('Ashby','broken','Broken')]), \
             patch.object(radar,'employer',side_effect=publisher), patch.object(radar,'PAGE_SIZE',1):
            with radar.ThreadingHTTPServer(('127.0.0.1',0),radar.Handler) as server:
                t=threading.Thread(target=server.serve_forever,daemon=True)
                t.start()
                def call(route,body=None):
                    req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/{route}',
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={'Content-Type':'application/json'})
                    with urllib.request.urlopen(req,timeout=10) as response:
                        return json.load(response)
                try:
                    f=dict(self.filters,unknown_date=True)
                    other=dict(f, work=['Remote'],scope='nationwide')
                    call('saved-search',dict(name='Hybrid',filters=f))
                    call('saved-search',dict(name='Remote',filters=other))
                    answer=call('search',dict(filters=f,name='Hybrid'))
                    self.assertEqual(answer['count'],2)
                    self.assertEqual(answer['page_count'],2)
                    second=call('filter',dict(filters=f,page=2))
                    self.assertEqual(second['page'],2)
                    self.assertNotEqual(second['jobs'][0]['id'],answer['jobs'][0]['id'])
                    self.assertEqual(answer['sources']['Ashby · Broken']['state'],'unavailable')
                    jid=answer['jobs'][0]['id']
                    call('track',dict(id=jid,saved=True,status='Applied',date='2026-11-26',notes='Follow up'))
                    self.assertEqual(call('filter',dict(filters=other,name='Remote',view=True))['count'],0)
                    call('search',dict(filters=f,name='Hybrid'))
                    state=call('state')
                    self.assertEqual(state['searches']['Hybrid']['filters'],f)
                    self.assertEqual(state['searches']['Remote']['filters'],other)
                    self.assertEqual(state['tracking'][jid]['status'],'Applied')
                    self.assertTrue(state['tracking'][jid]['saved'])
                    self.assertEqual(state['tracking'][jid]['notes'],'Follow up')
                finally:
                    server.shutdown()
                    t.join()

if __name__=='__main__':
    unittest.main()

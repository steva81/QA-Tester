"""Phase 2 causality remains authoritative for real local browser triage."""
import asyncio

import pytest
from test_full_site import site, isolated_state, browser_case, engine, seed


@pytest.mark.parametrize('genuine', [False, True])
def test_actual_safety_limited_browser_run_does_not_promote_diagnostics(site, genuine):
    from app.models.database import get_db_connection
    from app.qa.triage import triage_run
    from app.routes import api
    pid, rid = seed(site)
    site.links['/'] = []
    preblock = 'console.error("GENUINE_PRE_BLOCK_CUSTOMER_ERROR");' if genuine else ''
    site.script = '<script>' + preblock + 'fetch("/write",{method:"POST"}).catch(error=>console.error({error:{error:String(error)}}));</script>'
    async def check(context, user):
        qa = engine(site, context)
        results = await qa.run()
        api._save_results(results, rid, pid, coverage=qa.snapshot())
        conn = get_db_connection()
        findings_before = [dict(row) for row in conn.execute('SELECT * FROM findings WHERE run_id=?', (rid,))]
        evidence = [dict(row) for row in conn.execute('SELECT * FROM tests WHERE run_id=?', (rid,))]
        triage = triage_run(conn, rid)
        assert site.mutations == 0
        assert sum(row['status'] == 'LIMITED COVERAGE' for row in evidence) == 2
        assert any(row['status'] == 'DIAGNOSTIC' for row in evidence)
        if genuine:
            assert len(triage) == len(findings_before) == 1
            assert triage[0]['evidence_count'] == 2
            eligible = {row['id'] for row in evidence if row['status'] == 'OBSERVATION'}
            assert set(triage[0]['evidence_ids']) == eligible
        else:
            assert triage == findings_before == []
        assert [dict(row) for row in conn.execute('SELECT * FROM tests WHERE run_id=?', (rid,))] == evidence
        assert [dict(row) for row in conn.execute('SELECT * FROM findings WHERE run_id=?', (rid,))] == findings_before
        conn.close()
    asyncio.run(browser_case(site, check))

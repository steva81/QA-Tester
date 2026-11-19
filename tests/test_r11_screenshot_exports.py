"""Screenshot evidence must survive Standard and Full text/Markdown exports."""
from urllib.parse import quote
import pytest
from test_reporting import facts, PRIVATE
from app.qa.reporting import build_report
from app.qa.report_renderers import render_text, render_markdown

@pytest.mark.parametrize('mode', ['standard', 'full'])
@pytest.mark.parametrize('renderer', [render_text, render_markdown])
def test_screenshot_references_and_captions_survive(renderer, mode):
    f = facts(customer=True, record_status='FAIL')
    names = ['desktop evidence [1] #.png', 'mobile_evidence.png']
    f[3][0]['screenshot_path'] = 'C:/private-source/screenshots/' + names[0]
    f[3].append(dict(f[3][0], id=2, viewport='Mobile', screenshot_path='C:/private-source/screenshots/' + names[1]))
    f[2][0]['how_to_explain_sv'] = PRIVATE + '-oral'
    output = renderer(build_report(*f), mode)
    for name in names:
        reference = '../screenshots/' + (quote(name, safe='') if renderer is render_markdown else name)
        assert reference in output
    assert 'Desktop' in output and 'Mobile' in output and 'QA-1' in output
    assert 'Evidence for QA-1' in output
    assert 'C:/private-source' not in output
    assert PRIVATE not in output

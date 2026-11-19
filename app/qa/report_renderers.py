"""Format adapters for canonical blocks; no business rules."""
from html import escape
from pathlib import Path
from app.qa.reporting import presentation


def render_text(report, mode='standard'):
    def plain(b):
        if b['kind'] == 'heading':
            return '\n' + b['text'] + '\n'
        if b['kind'] == 'image':
            return f"Screenshot: ../screenshots/{b['text']}\n{b.get('caption', 'Evidence screenshot')}"
        if b['kind'] == 'table':
            return '\n\n'.join('\n'.join(f'{key}: {value}' for key, value in zip(b['headers'], row)) for row in b['rows'])
        return b['text']
    return '\n'.join(plain(b)
                     for b in presentation(report, mode)) + '\n'


def render_markdown(report, mode='standard'):
    def md(text):
        text = escape(text, quote=False)
        # Intraword underscores are literal in CommonMark; preserve technical
        # identifiers such as GENUINE_JSON_ERROR without altering the evidence.
        for char in ('\\', '*', '[', ']', '`', '|', '#'):
            text = text.replace(char, '\\' + char)
        return text.replace('\n', '  \n')
    def block(b):
        if b['kind'] == 'image':
            from urllib.parse import quote
            caption = md(b.get('caption', 'Evidence screenshot'))
            return f"![{caption}](../screenshots/{quote(b['text'], safe='')})"
        if b['kind'] == 'heading':
            return '#' * (b['level'] + 1) + ' ' + md(b['text'])
        if b['kind'] == 'table':
            rows = [b['headers'], ['---'] * len(b['headers']), *b['rows']]
            return '\n'.join('| ' + ' | '.join(md(cell).replace('\n', ' ') for cell in row) + ' |' for row in rows)
        return md(b['text'])
    return '\n\n'.join(block(b)
                       for b in presentation(report, mode)) + '\n'


def render_html(report, mode='standard', document=True):
    parts = []
    for b in presentation(report, mode):
        if b['kind'] == 'table':
            parts.append('<table style="border-collapse:collapse;width:100%;margin:16px 0;box-shadow:0 1px 3px rgba(0,0,0,0.06);border-radius:6px;overflow:hidden"><thead><tr>' + ''.join('<th style="background:#f1f5f9;color:#12665f;font-weight:600;font-size:13px;text-align:left;padding:10px 12px;border:1px solid #cbd5e1">'+escape(cell)+'</th>' for cell in b['headers']) + '</tr></thead><tbody>' +
                         ''.join('<tr>'+''.join('<td style="padding:8px 12px;font-size:13px;border:1px solid #e2e8f0;overflow-wrap:anywhere">'+escape(cell)+'</td>' for cell in row)+'</tr>' for row in b['rows']) + '</tbody></table>')
        elif b['kind'] == 'image':
            from urllib.parse import quote
            caption = escape(b.get('caption', 'Evidence screenshot'))
            parts.append(
                f'<figure style="margin:20px auto;background:#f8fafc;padding:12px;border:1px solid #cbd5e1;border-radius:8px;max-width:640px;box-shadow:0 2px 4px rgba(0,0,0,0.04)">'
                f'<img style="max-width:100%;max-height:280px;object-fit:contain;border-radius:4px;display:block;margin:0 auto;box-shadow:0 1px 3px rgba(0,0,0,0.1)" alt="Evidence screenshot" src="/screenshots/{quote(b["text"], safe="")}">'
                f'<figcaption style="font-size:12px;color:#475569;margin-top:8px;text-align:center;font-weight:600;font-style:italic">{caption}</figcaption>'
                f'</figure>'
            )
        elif b['kind'] == 'heading':
            tag = f'h{b["level"] + 1}'
            parts.append(f'<{tag}>{escape(b["text"])}</{tag}>')
        else:
            parts.append('<p style="white-space:pre-wrap;margin:6px 0;overflow-wrap:anywhere">' + escape(b['text']) + '</p>')
    body = '\n'.join(parts)
    if not document:
        return body
    return ('<!doctype html><html><head><meta charset="utf-8"><title>QA Test Report</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;'
            'max-width:1040px;margin:32px auto;padding:0 24px 48px;color:#1e293b;background:#f8fafc;line-height:1.6}'
            'article{background:white;padding:36px 44px;border-radius:10px;border:1px solid #e2e8f0;box-shadow:0 4px 6px -1px rgba(0,0,0,0.05),0 2px 4px -2px rgba(0,0,0,0.05)}'
            'h1{color:#12665f;border-bottom:2px solid #12665f;padding-bottom:12px;margin-top:0;font-size:26px}'
            'h2{color:#0b4a45;border-bottom:1px solid #e2e8f0;padding-top:20px;margin-top:24px;font-size:20px}'
            'h3{color:#1e293b;margin-top:16px;font-size:16px}p{margin:6px 0;overflow-wrap:anywhere}'
            '@media(max-width:640px){body{padding:0 12px 24px}article{padding:20px 16px}}'
            '</style>'
            '</head><body><article>' + body + '</article></body></html>')


def render_docx(report, mode='standard'):
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls
    from app.paths import get_screenshots_dir

    doc = Document()
    normal_style = doc.styles['Normal']
    normal_style.font.name = 'Segoe UI'
    normal_style.font.size = Pt(9.5)
    normal_style.font.color.rgb = RGBColor(30, 41, 59)
    normal_style.paragraph_format.space_after = Pt(3)

    # Document margins: 0.75 inches
    for section in doc.sections:
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(0.75)
        section.right_margin = Inches(0.75)

    first_detailed_finding = True
    for b in presentation(report, mode):
        if b['kind'] == 'table':
            table = doc.add_table(rows=1, cols=len(b['headers']))
            table.style = 'Table Grid'
            table.autofit = False

            # Specific, proportional column widths (totaling 7.0 inches printable width)
            first_header = b['headers'][0] if b['headers'] else ''
            col_widths = None
            if first_header == 'Bug ID':
                # Findings Overview: ['Bug ID', 'Finding', 'Severity', 'Risk', 'Priority', 'Status']
                col_widths = [Inches(1.5), Inches(2.5), Inches(0.75), Inches(0.7), Inches(0.9), Inches(0.65)]
            elif first_header == 'Severity / Risk':
                # 4x4 Bug Risk Matrix: ['Severity / Risk', 'High Risk', 'Medium Risk', 'Low Risk', 'Total']
                col_widths = [Inches(1.6), Inches(1.4), Inches(1.5), Inches(1.4), Inches(1.1)]
            elif first_header == 'Coverage Metric':
                # Coverage table: ['Coverage Metric', 'Value', 'Coverage Metric', 'Value']
                col_widths = [Inches(2.1), Inches(1.4), Inches(2.1), Inches(1.4)]
            elif first_header == 'Field':
                # Finding details metadata: ['Field', 'Details', 'Field', 'Details']
                col_widths = [Inches(1.2), Inches(2.3), Inches(1.2), Inches(2.3)]
            elif first_header == 'Target Page URL':
                # Technical Summary: ['Target Page URL', 'Viewport', 'Checks', 'Results Summary']
                col_widths = [Inches(2.6), Inches(1.0), Inches(0.7), Inches(2.7)]
            elif first_header == 'Related Bug IDs':
                col_widths = [Inches(2.0), Inches(1.1), Inches(1.3), Inches(1.2), Inches(1.4)]

            for col_idx, col in enumerate(table.columns):
                if col_widths and col_idx < len(col_widths):
                    col.width = col_widths[col_idx]

            for cell, text in zip(table.rows[0].cells, b['headers']):
                cell.text = text
                shading = parse_xml(r'<w:shd {} w:fill="F1F5F9"/>'.format(nsdecls('w')))
                cell._tc.get_or_add_tcPr().append(shading)
                if cell.paragraphs and cell.paragraphs[0].runs:
                    run = cell.paragraphs[0].runs[0]
                    run.font.bold = True
                    run.font.color.rgb = RGBColor(18, 102, 95)
                    run.font.size = Pt(9)
            trPr = table.rows[0]._tr.get_or_add_trPr()
            trPr.append(parse_xml(r'<w:tblHeader {}/>'.format(nsdecls('w'))))
            trPr.append(parse_xml(r'<w:cantSplit {}/>'.format(nsdecls('w'))))

            for row in b['rows']:
                r_cells = table.add_row().cells
                r_trPr = table.rows[-1]._tr.get_or_add_trPr()
                r_trPr.append(parse_xml(r'<w:cantSplit {}/>'.format(nsdecls('w'))))
                for cell, text in zip(r_cells, row):
                    cell.text = text
                    if cell.paragraphs and cell.paragraphs[0].runs:
                        cell.paragraphs[0].runs[0].font.size = Pt(8.5)

            # Enforce cell widths on every cell in XML
            if col_widths:
                for row in table.rows:
                    for col_idx, cell in enumerate(row.cells):
                        if col_idx < len(col_widths):
                            w = col_widths[col_idx]
                            cell.width = w
                            tcPr = cell._tc.get_or_add_tcPr()
                            tcW = parse_xml(r'<w:tcW {} w:w="{}" w:type="dxa"/>'.format(nsdecls('w'), int(w.inches * 1440)))
                            tcPr.append(tcW)
            p_space = doc.add_paragraph()
            p_space.paragraph_format.space_before = Pt(0)
            p_space.paragraph_format.space_after = Pt(2)
            p_space.paragraph_format.line_spacing = Pt(4)

        elif b['kind'] == 'heading':
            level = b['level']
            text = b['text']

            # Controlled page breaks:
            # - Page break before Coverage (keeps Coverage narrative and table together on Page 2, preventing split)
            # - Page break before Detailed Customer Findings when findings exist (starts customer findings cleanly on Page 3)
            # - Page break before each subsequent detailed customer finding
            if level == 1 and text == 'Coverage':
                doc.add_page_break()
            elif level == 1 and text == 'Detailed Customer Findings':
                if report.get('findings'):
                    doc.add_page_break()
            elif level == 1 and text == 'Technical Appendix — Technical Data':
                doc.add_page_break()
            elif level == 2 and '—' in text and not text.startswith('Technical Summary'):
                if not first_detailed_finding:
                    doc.add_page_break()
                first_detailed_finding = False

            h = doc.add_heading(text, level=level)
            h.paragraph_format.space_before = Pt(8 if level > 1 else 14)
            h.paragraph_format.space_after = Pt(4)
            if h.runs:
                if level == 0:
                    h.runs[0].font.color.rgb = RGBColor(18, 102, 95)
                    h.runs[0].font.size = Pt(20)
                elif level == 1:
                    h.runs[0].font.color.rgb = RGBColor(11, 74, 69)
                    h.runs[0].font.size = Pt(14)
                elif level == 2:
                    h.runs[0].font.color.rgb = RGBColor(30, 41, 59)
                    h.runs[0].font.size = Pt(11)

        elif b['kind'] == 'text':
            doc.add_paragraph(b['text'])

        else:
            path = Path(get_screenshots_dir()) / b['text']
            if not path.is_file():
                doc.add_paragraph(f'[Screenshot not found: {b["text"]}]')
                continue
            try:
                from PIL import Image
                with Image.open(path) as image:
                    width, height = image.size
                    low, high = image.convert('L').getextrema()
                if high - low < 5:
                    doc.add_paragraph('[Visually blank screenshot omitted to save document space]')
                else:
                    max_w = 6.0 if mode == 'standard' else 5.5
                    max_h = 3.6 if mode == 'standard' else 3.2
                    scale = min(max_w / width, max_h / height)
                    p_pic = doc.add_paragraph()
                    p_pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p_pic.paragraph_format.space_before = Pt(6)
                    p_pic.paragraph_format.space_after = Pt(2)
                    p_pic.paragraph_format.keep_with_next = True
                    r_pic = p_pic.add_run()
                    r_pic.add_picture(str(path), width=Inches(width * scale), height=Inches(height * scale))
                    caption = b.get('caption', 'Evidence screenshot')
                    p_cap = doc.add_paragraph(caption if caption.startswith('Figure:') else f"Figure: {caption}")
                    p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p_cap.paragraph_format.space_after = Pt(8)
                    if p_cap.runs:
                        p_cap.runs[0].font.size = Pt(8.5)
                        p_cap.runs[0].font.italic = True
                        p_cap.runs[0].font.color.rgb = RGBColor(100, 116, 139)
            except Exception:
                doc.add_paragraph('[Screenshot image could not be embedded]')
    return doc


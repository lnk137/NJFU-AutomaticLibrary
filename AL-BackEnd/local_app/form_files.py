"""Bounded XLSX/CSV input; raw credentials never enter user-facing errors."""
import csv
import hashlib
from dataclasses import asdict
from datetime import date, datetime, time
import io
import json
import re
from zipfile import BadZipFile, ZipFile
from xml.etree.ElementTree import ParseError, fromstring

from .models import AppError, credentials, validate_plan
from .policy import closing_end

HEADERS = ['姓名或备注','学号','学校密码','预约类型','首选座位','备选座位','开始时间','结束时间','使用日期','每周日期','放号时间']
MAX_FILE_BYTES = 32768
PERSONAL_TITLE = '个人预约表'
PERSONAL_FOOTER = '表单含学校密码，请私发管理员。'
MAX_SHEET_ROWS = 51  # Original template reserves fifty formatted input rows.
# Text-only fingerprint of the original blank template's instruction page (51 x 11).
LEGACY_HELP_SHA256 = 'e02bcb744004b8c11a6b17db1b0cc5c340e92147de4d833996cc93f28ac7b3bb'


def _invalid(message='请使用模板填写并保存为 XLSX 或 CSV'):
    raise AppError('invalid_form', message)


def _read(filename, content):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_FILE_BYTES:
        _invalid('表单大小须在32 KiB以内')
    if filename.lower().endswith('.csv'):
        try:
            try:text = content.decode('utf-8-sig')
            except UnicodeDecodeError:text = content.decode('gb18030')
            return list(csv.reader(io.StringIO(text), strict=True))
        except (UnicodeError, csv.Error):_invalid('CSV内容无法读取，请重新另存为 UTF-8 CSV')
    if not filename.lower().endswith('.xlsx'):_invalid()
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if (len(entries) > 50 or sum(item.file_size for item in entries) > 2 * 1024 * 1024
                    or any(item.file_size > 1024 * 1024 or item.flag_bits & 1
                           or 'externalLinks/' in item.filename or item.filename.endswith('.bin') for item in entries)):
                _invalid('Excel文件过大或包含不支持的外部内容')
            for entry in entries:
                if entry.is_dir():continue
                if not entry.filename.endswith(('.xml','.rels')):_invalid('请仅保留模板表格，不支持嵌入附件')
                if entry.filename.endswith(('.xml','.rels')):
                    xml_text=archive.read(entry).decode('utf-8-sig')
                    xml = xml_text.upper()
                    if '<!DOCTYPE' in xml or '<!ENTITY' in xml:_invalid('Excel文件包含不支持的XML内容')
                    tree=fromstring(xml_text);ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                    if tree.tag==ns+'worksheet':
                        def coordinate(value):
                            match=re.fullmatch(r'([A-Z]{1,3})([1-9]\d{0,9})',value or '')
                            if not match:_invalid('Excel单元格坐标无效')
                            column=0
                            for char in match[1]:column=column*26+ord(char)-64
                            row=int(match[2])
                            if column>len(HEADERS) or row>MAX_SHEET_ROWS:_invalid('表单范围过大，请使用单人模板')
                            return column,row
                        positions=set();actual_column=actual_row=0
                        for row in tree.iter(ns+'row'):
                            if not re.fullmatch(r'[1-9]\d{0,9}',row.get('r','')) or int(row.get('r'))>MAX_SHEET_ROWS:_invalid('表单范围过大，请使用单人模板')
                            for cell in row.findall(ns+'c'):
                                point=coordinate(cell.get('r'))
                                if point in positions or point[1]!=int(row.get('r')):_invalid('Excel单元格坐标重复或不一致')
                                positions.add(point);actual_column=max(actual_column,point[0]);actual_row=max(actual_row,point[1])
                        dimension=tree.find(ns+'dimension')
                        if dimension is not None:
                            parts=dimension.get('ref','').split(':')
                            if len(parts)>2:_invalid('Excel范围无效')
                            bounds=[coordinate(part) for part in parts]
                            if bounds[-1][0]<actual_column or bounds[-1][1]<actual_row:_invalid('Excel范围与实际内容不一致，请重新保存')
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            sheet = workbook.worksheets[0]
            if len(workbook.worksheets)>1:
                if (len(workbook.worksheets)!=2 or sheet['A1'].value==PERSONAL_TITLE
                        or not _legacy_help(workbook.worksheets[1])):
                    _invalid('每份表单只填写一人，请不要附加其他工作表')
            if sheet.max_row is None or sheet.max_column is None:sheet.calculate_dimension(force=True)
            if sheet.max_row > MAX_SHEET_ROWS or sheet.max_column > len(HEADERS):
                _invalid('表单范围过大，请使用单人模板')
            rows = []
            for cells in sheet.iter_rows(max_row=MAX_SHEET_ROWS, max_col=len(HEADERS)):
                if any(cell.data_type == 'f' for cell in cells):_invalid('请填写文字或数值，不支持公式')
                rows.append([cell.value for cell in cells])
            return rows
        finally:workbook.close()
    except AppError:raise
    except ImportError:raise AppError('xlsx_unavailable', 'Excel读取库暂不可用，可以另存为CSV导入', 503) from None
    except (BadZipFile, ParseError, UnicodeError, ValueError, KeyError, IndexError, OSError, TypeError):_invalid('Excel文件无法读取，请使用下载的模板')


def _text(value):
    if value is None:return ''
    if isinstance(value, datetime):return value.date().isoformat() if value.time() == time() else value.isoformat()
    if isinstance(value, date):return value.isoformat()
    if isinstance(value, time):return value.strftime('%H:%M:%S') if value.second else value.strftime('%H:%M')
    return str(value).strip()


def _legacy_help(sheet):
    """Only the known, unchanged instruction page may accompany a legacy form."""
    if sheet.title!='填写说明':return False
    values=[[_text(value) for value in row]
            for row in sheet.iter_rows(max_row=MAX_SHEET_ROWS,max_col=len(HEADERS),values_only=True)]
    digest=hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return digest==LEGACY_HELP_SHA256


def _applicant(rows):
    """Normalize a vertical personal form or a previous single-row form."""
    if rows and rows[0] and _text(rows[0][0]) == PERSONAL_TITLE:
        if len(rows)<14 or [_text(v) for v in rows[2][:3]]!=['项目','填写内容','怎么填']:
            _invalid('个人表单项目不完整，请重新下载模板')
        values=[]
        for index,field in enumerate(HEADERS,3):
            row=rows[index]
            if not row or _text(row[0])!=field or any(_text(v) for v in row[3:]):
                _invalid('个人表单项目有缺失或重复，请保持模板中的项目名称和顺序')
            values.append(row[1] if len(row)>1 else None)
        for index,row in enumerate(rows):
            if 3<=index<=13:continue
            if index==2:allowed=3
            elif index in (0,1):allowed=1
            elif index==15 and row and _text(row[0])==PERSONAL_FOOTER:allowed=1
            else:allowed=0
            if any(_text(v) for v in row[allowed:]):_invalid('每份表单只填写一人，请不要附加其他用户的信息')
        return [(4,values)]
    if not rows or [_text(value) for value in rows[0]] != HEADERS:
        _invalid('表单与模板不一致，请重新下载个人模板')
    records=[(index,row) for index,row in enumerate(rows[1:],2) if any(value not in (None,'') for value in row)]
    if len(records)!=1:_invalid('每份表单只填写一人，请为每位用户分别导入')
    return records


def parse_form(filename, content, catalog, today):
    rows = _applicant(_read(filename, content))
    names = {}
    for seat in catalog.values():names.setdefault(seat.name.casefold(), []).append(seat.id)

    def seat_id(text):
        if text in catalog:return text
        matches = names.get(text.casefold(), [])
        if len(matches) != 1:_invalid('座位不存在或名称不唯一，请填写目录中的完整编号')
        return matches[0]

    output = []
    for line, values in rows:
        cells = [_text(value) for value in values]
        item = {'line': line, 'alias': (cells[0] if cells else '')[:80], 'student_id': (cells[1] if len(cells)>1 else '')[:20]}
        try:
            if len(cells) != len(HEADERS):_invalid('每行须保留模板的全部列')
            alias, student, _, action, preferred, backups, begin, end, day, weekdays, trigger = cells
            password = values[2]
            if not isinstance(values[1], str) or not isinstance(password, str):_invalid('学号和学校密码须按文本填写')
            credentials(student, password)
            if not re.fullmatch(r'\d{5,20}', student) or len(alias)>80:_invalid('请填写5–20位学号及不超过80字的姓名或备注')
            if action not in ('生成定时计划','当天一次预约'):_invalid('预约类型请选择生成定时计划或当天一次预约')
            ids = [seat_id(preferred)] + [seat_id(key) for key in re.split(r'[,，;；\s]+',backups) if key]
            raw = {'seat_ids':ids,'begin':begin,'end':end,'enabled':True,'trigger':trigger or '07:00:00','mode':'day'}
            item.update(password=password, action='scheduled' if action=='生成定时计划' else 'today')
            if item['action']=='today':
                if day != today.isoformat():_invalid('当天一次预约须填写北京时间今天的使用日期')
                if weekdays or trigger:_invalid('当天一次预约请留空每周日期和放号时间')
                plan = validate_plan(raw,catalog,single_date=today)
                item['today'] = {'date':day,'begin':plan.begin,'end':closing_end(plan.end,today.isoweekday()),'seat_ids':ids}
            else:
                if day:_invalid('定时计划请留空使用日期，按每天或每周重复')
                if weekdays:
                    days = re.split(r'[,，;；\s]+',weekdays)
                    if any(key not in '1234567' or len(key)!=1 for key in days) or len(set(days))!=len(days):_invalid('每周日期填写1–7，用逗号分隔且不能重复')
                    raw.update(mode='week',week_times={key:[begin,end] for key in days})
                item['plan'] = asdict(validate_plan(raw,catalog))
        except AppError as exc:item['error'] = str(exc)
        output.append(item)
    return output

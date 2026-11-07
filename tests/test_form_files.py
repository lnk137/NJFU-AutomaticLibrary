import csv
from datetime import date
import io
from pathlib import Path
import sys
import unittest
import subprocess
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Seat

HEADERS = ['姓名或备注','学号','学校密码','预约类型','首选座位','备选座位','开始时间','结束时间','使用日期','每周日期','放号时间']
CATALOG = {'1': Seat('1','A001','2F'), '2': Seat('2','A002','2F')}
ROW = ['同学','99000001','synthetic-password','生成定时计划','A001','A002','09:00','12:00','','1,3','07:00:00']


def csv_file(rows, headers=HEADERS):
    text = io.StringIO();writer = csv.writer(text);writer.writerow(headers);writer.writerows(rows)
    return text.getvalue().encode('utf-8-sig')


def personal_csv(values=ROW):
    rows=[['个人预约表'],['每人填写一份，填好后发给管理员。'],['项目','填写内容','怎么填']]
    rows.extend([[field,value,''] for field,value in zip(HEADERS,values)])
    rows.extend([[],['表单含学校密码，请私发管理员。']])
    text=io.StringIO();csv.writer(text).writerows(rows);return text.getvalue().encode('utf-8-sig')

def xlsx_file(values=ROW, *, formula=False, broken=False, macro=False, sparse=False, overflow=False, relocated=False):
    template=Path(__file__).resolve().parents[1]/'AL-BackEnd/resources/booking-form.xlsx'
    output=io.BytesIO();ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(template) as original,ZipFile(output,'w',ZIP_DEFLATED) as archive:
        for item in original.infolist():
            content=original.read(item)
            if item.filename=='xl/worksheets/sheet1.xml':
                xml=ET.fromstring(content);data=xml.find('{'+ns+'}sheetData')
                for number,value in enumerate(values,4):
                    row=next((r for r in data if r.get('r')==str(number)),None)
                    if row is None:row=ET.SubElement(data,'{'+ns+'}row',r=str(number))
                    cell=next((c for c in row if c.get('r')=='B'+str(number)),None)
                    if cell is None:cell=ET.SubElement(row,'{'+ns+'}c',r='B'+str(number))
                    for child in list(cell):cell.remove(child)
                    cell.set('t','inlineStr')
                    ET.SubElement(ET.SubElement(cell,'{'+ns+'}is'),'{'+ns+'}t').text=value
                row=next(r for r in data if r.get('r')=='4')
                if formula:ET.SubElement(row[0],'{'+ns+'}f').text='1+1'
                if sparse:
                    row.set('r','1000000000')
                    for cell in row:cell.set('r',cell.get('r')[:-1]+'1000000000')
                if overflow:
                    import copy
                    for number in range(17,53):
                        extra=copy.deepcopy(row);extra.set('r',str(number))
                        for cell in extra:cell.set('r',cell.get('r')[:-1]+str(number))
                        data.append(extra)
                    xml.insert(0,ET.Element('{'+ns+'}dimension',ref='A1:C16'))
                content=b'<broken' if broken else ET.tostring(xml)
            if relocated and item.filename=='xl/_rels/workbook.xml.rels':content=content.replace(b'/xl/worksheets/sheet1.xml',b'/evil.xml').replace(b'worksheets/sheet1.xml',b'../evil.xml')
            archive.writestr('evil.xml' if relocated and item.filename=='xl/worksheets/sheet1.xml' else item,content)
        if macro:archive.writestr('xl/vbaProject.bin',b'fake macro')
    return output.getvalue()


def duplicate_applicant_sheet(hidden=False):
    original=io.BytesIO(xlsx_file());output=io.BytesIO()
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with ZipFile(original) as source,ZipFile(output,'w',ZIP_DEFLATED) as target:
        sheet=source.read('xl/worksheets/sheet1.xml').replace(b'99000001',b'99000002')
        for item in source.infolist():
            content=source.read(item)
            if item.filename=='xl/workbook.xml':
                root=ET.fromstring(content);sheets=root.find('{'+ns+'}sheets')
                ET.SubElement(sheets,'{'+ns+'}sheet',{'name':'Other Applicant','sheetId':'2','{'+rel+'}id':'rId99','state':'hidden' if hidden else 'visible'})
                content=ET.tostring(root)
            elif item.filename=='xl/_rels/workbook.xml.rels':
                root=ET.fromstring(content)
                ET.SubElement(root,'{http://schemas.openxmlformats.org/package/2006/relationships}Relationship',{'Id':'rId99','Type':rel+'/worksheet','Target':'/xl/worksheets/extra.xml'})
                content=ET.tostring(root)
            elif item.filename=='[Content_Types].xml':
                root=ET.fromstring(content)
                ET.SubElement(root,'{http://schemas.openxmlformats.org/package/2006/content-types}Override',{'PartName':'/xl/worksheets/extra.xml','ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
                content=ET.tostring(root)
            target.writestr(item,content)
        target.writestr('xl/worksheets/extra.xml',sheet)
    return output.getvalue()


def legacy_xlsx(extra=False):
    original=Path(__file__).parent/'fixtures/form-template-legacy.xlsx';output=io.BytesIO()
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(original) as source,ZipFile(output,'w',ZIP_DEFLATED) as target:
        for item in source.infolist():
            content=source.read(item)
            if item.filename=='xl/worksheets/sheet1.xml':
                root=ET.fromstring(content);data=root.find('{'+ns+'}sheetData')
                for number in ([2,3] if extra else [2]):
                    row=next(r for r in data if r.get('r')==str(number))
                    for child in list(row):row.remove(child)
                    values=ROW.copy();values[1]='99000001' if number==2 else '99000002'
                    for column,value in enumerate(values):
                        cell=ET.SubElement(row,'{'+ns+'}c',r=chr(65+column)+str(number),t='inlineStr')
                        ET.SubElement(ET.SubElement(cell,'{'+ns+'}is'),'{'+ns+'}t').text=value
                content=ET.tostring(root)
            target.writestr(item,content)
    return output.getvalue()


class FormFileTests(unittest.TestCase):
    def parse(self, rows, **changes):
        try:
            from local_app.form_files import parse_form
        except ModuleNotFoundError:
            self.fail('Form reader is not implemented')
        return parse_form(changes.get('filename','requests.csv'),changes.get('content',csv_file(rows)),CATALOG,date(2026,10,23))

    def test_weekly_seat_names_resolve_to_ordered_snapshot(self):
        row = self.parse([ROW])[0]
        self.assertEqual(row['plan']['seat_ids'], ('1','2'))
        self.assertEqual(row['plan']['week_times'], {'1':['09:00','12:00'],'3':['09:00','12:00']})
        self.assertEqual(row['action'],'scheduled');self.assertEqual(row['password'],'synthetic-password')

    def test_blank_weekdays_and_trigger_default_to_daily_at_seven(self):
        data = ROW.copy();data[9] = data[10] = ''
        row = self.parse([data])[0]
        self.assertEqual((row['plan']['mode'],row['plan']['trigger']),('day','07:00:00'))

    def test_today_has_explicit_date_and_effective_friday_time(self):
        row = ROW.copy();row[3]='当天一次预约';row[6:11]=['18:00','22:00','2026-10-23','','']
        result=self.parse([row])[0]
        self.assertEqual(result['today'],{'date':'2026-10-23','begin':'18:00','end':'20:00','seat_ids':['1','2']})
        row[8]='2026-10-24';self.assertIn('error',self.parse([row])[0])

    def test_multi_person_rosters_are_rejected_instead_of_silently_importing_anyone(self):
        third=ROW.copy();third[1]='99000002'
        for rows in ([ROW,third],[ROW,ROW]):
            with self.subTest(rows=len(rows)),self.assertRaisesRegex(AppError,'一人|一位'):self.parse(rows)

    def test_vertical_personal_form_has_the_same_scheduled_and_today_meaning(self):
        values=ROW.copy();values[1]='00123456';values[2]=' synthetic-password '
        parsed=self.parse([],content=personal_csv(values))[0]
        self.assertEqual(parsed['student_id'],'00123456');self.assertEqual(parsed['password'],' synthetic-password ')
        self.assertEqual(parsed['plan']['seat_ids'],('1','2'));self.assertEqual(parsed['action'],'scheduled')
        values[3]='当天一次预约';values[8:11]=['2026-10-23','','']
        self.assertEqual(self.parse([],content=personal_csv(values))[0]['action'],'today')

    def test_personal_form_rejects_extra_applicant_blocks_and_missing_or_duplicate_fields(self):
        original=personal_csv().decode('utf-8-sig')
        for content in (original+'学号,99000002\n',original.replace('学号,99000001','姓名或备注,99000001'),original.replace('学号,99000001,\r\n','')):
            with self.subTest(content=content[:8]),self.assertRaises(AppError):self.parse([],content=content.encode())
    def test_visible_and_hidden_second_applicant_sheets_are_rejected(self):
        for hidden in (False,True):
            with self.subTest(hidden=hidden),self.assertRaisesRegex(AppError,'一人|一位|工作表'):
                self.parse([],filename='requests.xlsx',content=duplicate_applicant_sheet(hidden))
    def test_real_legacy_template_accepts_one_person_and_rejects_two(self):
        row=self.parse([],filename='legacy.xlsx',content=legacy_xlsx())[0]
        self.assertEqual(row['student_id'],'99000001');self.assertNotIn('error',row)
        with self.assertRaisesRegex(AppError,'一人|一位'):
            self.parse([],filename='legacy.xlsx',content=legacy_xlsx(extra=True))

    def test_bad_rows_report_errors_without_echoing_password(self):
        for index,value in ((4,'missing'),(7,'09:30'),(3,'unknown'),(9,'8')):
            data=ROW.copy();data[index]=value
            with self.subTest(index=index):
                row=self.parse([data])[0];self.assertIn('error',row)
                self.assertNotIn('synthetic-password',row['error'])

    def test_empty_oversized_wrong_headers_and_legacy_format_fail_before_authentication(self):
        for options in ({'content':csv_file([])}, {'content':b'x'*32769}, {'content':csv_file([ROW],['wrong'])}, {'filename':'a.xls'}):
            with self.subTest(options=list(options)),self.assertRaises(AppError):self.parse([],**options)

    def test_scheduled_rows_cannot_silently_ignore_a_single_use_date(self):
        row=ROW.copy();row[8]='2026-10-23';self.assertIn('error',self.parse([row])[0])

    def test_password_whitespace_is_preserved_but_student_identity_is_trimmed(self):
        row=ROW.copy();row[1]=' 99000001 ';row[2]=' synthetic-password '
        result=self.parse([row])[0]
        self.assertEqual(result['student_id'],'99000001');self.assertEqual(result['password'],' synthetic-password ')
    def test_downloaded_excel_template_accepts_text_inputs_and_matches_csv_semantics(self):
        row=self.parse([],filename='requests.xlsx',content=xlsx_file())[0]
        self.assertEqual(row['student_id'],'99000001');self.assertEqual(row['plan']['seat_ids'],('1','2'))
    def test_excel_formula_macro_and_malformed_xml_are_rejected_as_file_errors(self):
        for options in ({'formula':True},{'macro':True},{'broken':True}):
            with self.subTest(options=options),self.assertRaises(AppError):
                self.parse([],filename='requests.xlsx',content=xlsx_file(**options))
    def test_underreported_dimension_never_silently_drops_extra_applicants(self):
        with self.assertRaises(AppError):self.parse([],filename='requests.xlsx',content=xlsx_file(overflow=True))
    def test_sparse_billionth_row_is_rejected_without_expanding_empty_rows(self):
        root=Path(__file__).resolve().parents[1]
        code="import sys;from datetime import date;sys.path[:0]=[sys.argv[1],sys.argv[2]];from local_app.form_files import parse_form;from local_app.models import AppError;from test_form_files import CATALOG\ntry:parse_form('a.xlsx',sys.stdin.buffer.read(),CATALOG,date(2026,10,23))\nexcept AppError:sys.exit(0)\nsys.exit(3)"
        for relocated in (False,True):
            with self.subTest(relocated=relocated):
                try:result=subprocess.run([sys.executable,'-B','-c',code,str(root/'AL-BackEnd'),str(root/'tests')],input=xlsx_file(sparse=True,relocated=relocated),capture_output=True,timeout=2)
                except subprocess.TimeoutExpired:self.fail('Sparse Excel expanded unbounded empty rows')
                self.assertEqual(result.returncode,0,result.stderr[:300])


if __name__=='__main__':unittest.main()

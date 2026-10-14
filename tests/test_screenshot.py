import struct
import unittest
from datetime import datetime, timezone
from sushiwait.screenshot import dimensions, extract_tsv, number_region

HEADER='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
def token(text,x,y,w=150,h=40,confidence=95):return f'5\t1\t1\t1\t1\t1\t{x}\t{y}\t{w}\t{h}\t{confidence}\t{text}\n'
class ScreenshotTests(unittest.TestCase):
 def sample(self):
  return HEADER+token('13:10',20,10)+token('321',250,350,400,180)+token('合成门店',200,850)+token('未签到',400,220)+token('现在时刻:13:10:40',200,720)+token('取号',40,1800)+token('2026-09-08',480,1800)+token('12:45:19',750,1800)+token('过号保留',40,1900)+token('2026-09-08',480,1900)+token('12:59:59',750,1900)+token('380',220,2000)
 def parse(self,text):return extract_tsv(text,1200,2400,{'900001':'合成门店'},now=datetime(2026,9,8,6,tzinfo=timezone.utc))
 def test_own_number_and_issue_time_not_phone_or_current_or_retention(self):
  r=self.parse(self.sample());self.assertTrue(r['auto_accept']);self.assertEqual(r['fields']['number'],'321');self.assertEqual(r['fields']['issued_at'],'2026-09-08T04:45:19+00:00');self.assertEqual(r['fields']['store_id'],'900001');self.assertFalse(r['fields']['checked_in']);self.assertNotIn('text',r)
 def test_missing_label_or_low_confidence_requires_review(self):
  r=self.parse(self.sample().replace('取号\n','未知\n'));self.assertFalse(r['auto_accept']);self.assertIsNone(r['fields']['issued_at'])
  r=self.parse(self.sample().replace('\t95\t321','\t30\t321'));self.assertFalse(r['auto_accept'])
 def test_conflicting_issue_dates_not_silently_chosen(self):
  r=self.parse(self.sample()+token('取号',40,1700)+token('2026-09-08',480,1700)+token('12:46:19',750,1700));self.assertFalse(r['auto_accept']);self.assertIsNone(r['fields']['issued_at'])
 def test_png_bounds_and_svg_refused(self):
  raw=b'\x89PNG\r\n\x1a\n'+b'\x00\x00\x00\x0dIHDR'+struct.pack('>II',1200,2400);self.assertEqual(dimensions(raw),(1200,2400,'.png'))
  for raw in [b'<svg>'+b' '*40,b'\x89PNG\r\n\x1a\n'+b'\x00\x00\x00\x0dIHDR'+struct.pack('>II',65535,65535),b'\xff\xd8'+b' '*40]:
   with self.assertRaises(ValueError):dimensions(raw)
 def test_store_partial_name_cannot_autosave(self):
  r=self.parse(self.sample().replace('合成门店','合成门'));self.assertFalse(r['auto_accept']);self.assertIsNone(r['fields']['store_id'])
 def test_reservation_number_is_not_assumed_ordinary(self):
  r=self.parse(self.sample().replace('\t321\n','\t7321\n'));self.assertTrue(r['auto_accept']);self.assertEqual(r['fields']['queue_type'],'reservation')
  r=self.parse(self.sample().replace('\t321\n','\t8321\n'));self.assertFalse(r['auto_accept'])
 def test_white_number_region_isolated_from_header(self):
  try:from PIL import Image,ImageDraw
  except ImportError:self.skipTest('optional screenshot extra not installed')
  image=Image.new('RGB',(600,1200),'white');d=ImageDraw.Draw(image);d.rectangle((30,170,570,430),fill=(220,185,132));d.rectangle((240,190,360,205),fill='white')
  for x in (210,255,300):d.rectangle((x,260,x+22,350),fill='white')
  result=number_region(image);self.assertIsNotNone(result);self.assertGreaterEqual(result[1][1],250);self.assertGreater(result[1][3],80)
  self.assertIsNone(number_region(Image.new('RGB',(600,1200),'white')))

 def test_digit_pass_does_not_remove_large_store_name_and_requires_agreement(self):
  try:from PIL import Image
  except ImportError:self.skipTest('optional screenshot extra not installed')
  import io,tempfile
  from types import SimpleNamespace
  from unittest.mock import patch
  from sushiwait.screenshot import recognize
  image=Image.new('RGB',(1200,2400),'white');buf=io.BytesIO();image.save(buf,format='PNG')
  # Engine coordinates belong to the resized 800x1600 page.
  full=HEADER+token('合成门店',120,560,350,80)+token('取号',30,1200)+token('2026-09-08',300,1200)+token('12:45:19',510,1200)
  line=HEADER+token('032',12,12,150,90,82)+token('I',170,12,20,90,85)
  word=HEADER+token('0321',12,12,180,90,0)
  with tempfile.TemporaryDirectory() as root:
   results=[SimpleNamespace(returncode=0,stdout=v.encode()) for v in (full,line,word)]
   with patch('sushiwait.screenshot.subprocess.run',side_effect=results),patch('sushiwait.screenshot.number_region',return_value=(image.resize((290,114)),(310,414,532,180))):
    result=recognize(buf.getvalue(),{'900001':'合成门店'},directory=root,now=datetime(2026,9,8,6,tzinfo=timezone.utc))
   self.assertTrue(result['auto_accept']);self.assertEqual(result['fields']['number'],'0321');self.assertEqual(result['fields']['store_id'],'900001')
   from pathlib import Path
   self.assertEqual(list(Path(root).iterdir()),[])
   results=[SimpleNamespace(returncode=0,stdout=v.encode()) for v in (full,line,word.replace('0321','0327'))]
   with patch('sushiwait.screenshot.subprocess.run',side_effect=results),patch('sushiwait.screenshot.number_region',return_value=(image.resize((290,114)),(310,414,532,180))):
    result=recognize(buf.getvalue(),{'900001':'合成门店'},directory=root,now=datetime(2026,9,8,6,tzinfo=timezone.utc))
   self.assertFalse(result['auto_accept'])

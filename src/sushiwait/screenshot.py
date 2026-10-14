"""Local screenshot OCR. Pixels and raw OCR text are never sent to a model."""
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
import csv
import hashlib
import io
import os
import re
import struct
import subprocess
import tempfile
import time

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 8_000_000
SHANGHAI = timezone(timedelta(hours=8))


def dimensions(raw):
    if not isinstance(raw, bytes) or not 24 <= len(raw) <= MAX_BYTES:
        raise ValueError('image_size_unsupported')
    if raw.startswith(b'\x89PNG\r\n\x1a\n') and raw[12:16] == b'IHDR':
        width, height = struct.unpack('>II', raw[16:24])
        suffix = '.png'
    elif raw.startswith(b'\xff\xd8'):
        cursor = 2; width = height = None
        for _ in range(4096):
            if cursor+4 > min(len(raw), 524288) or raw[cursor] != 255:
                break
            marker = raw[cursor+1]; cursor += 2
            if marker in (0xd8, 0xd9) or marker == 0x01:
                continue
            size = int.from_bytes(raw[cursor:cursor+2], 'big')
            if size < 2 or cursor+size > len(raw):
                break
            if marker in {0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf}:
                height, width = struct.unpack('>HH', raw[cursor+3:cursor+7]); break
            cursor += size
        suffix = '.jpg'
    else:
        raise ValueError('image_format_unsupported')
    if not width or not height or width > 4096 or height > 8192 or width*height > MAX_PIXELS or min(width,height)<200:
        raise ValueError('image_dimensions_unsupported')
    return width, height, suffix


def normalized(text):
    return re.sub(r'[^\w\u3400-\u9fff]', '', text)


def extract_tsv(text, width, height, names, *, now=None):
    now = now or datetime.now(timezone.utc)
    if len(text.encode()) > 262144 or not isinstance(names, dict) or len(names)>200:
        raise ValueError('ocr_projection_unsupported')
    words=[]
    for row in csv.DictReader(io.StringIO(text), delimiter='\t'):
        if row.get('level')!='5' or not row.get('text','').strip(): continue
        try:
            word={k:int(row[k]) for k in ('left','top','width','height')}
            word.update(text=row['text'].strip(),conf=float(row['conf']))
        except (ValueError,KeyError):continue
        if word['left']<0 or word['top']<0 or word['left']+word['width']>width or word['top']+word['height']>height:continue
        words.append(word)
        if len(words)>5000:raise ValueError('ocr_projection_unsupported')
    lines=[]
    for word in sorted(words,key=lambda w:w['top']+w['height']/2):
        center=word['top']+word['height']/2
        if lines and abs(lines[-1][0]-center)<max(10,height*.008):lines[-1][1].append(word)
        else:lines.append([center,[word]])
    texts=[(center,''.join(w['text'] for w in sorted(group,key=lambda w:w['left']))) for center,group in lines]
    digits=[w for w in words if re.fullmatch(r'\d{1,7}',w['text']) and int(w['text'])>0 and w['height']>=height*.035 and w['top']<height*.48 and .2*width<w['left']+w['width']/2<.8*width]
    digits.sort(key=lambda w:w['height'],reverse=True)
    number=digits[0]['text'] if digits else None
    number_clear=bool(digits and digits[0]['conf']>=60 and (len(digits)==1 or digits[0]['height']>digits[1]['height']*1.4))
    store_lines=[normalized(v) for y,v in texts if .2*height<y<.68*height]
    matched=[str(i) for i,name in names.items() if normalized(name) and any(normalized(name) in v for v in store_lines)]
    store_id=matched[0] if len(matched)==1 else None
    suggestion=None
    if not store_id:
        candidates=[(SequenceMatcher(None,normalized(name),line).ratio(),str(i)) for i,name in names.items() for line in store_lines if line.endswith('店')]
        if candidates:
            score,identity=max(candidates)
            if score>=.75:suggestion={'store_id':identity,'name':names[identity]}
    pattern=re.compile(r'(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s*(\d{1,2})[:：](\d{2})[:：](\d{2})')
    issued=[]
    for y,line in texts:
        if y<height*.55 or not line.startswith('取号') or line.startswith('取号成功'):continue
        for match in pattern.finditer(line):
            try:
                dt=datetime(*map(int,match.groups()),tzinfo=SHANGHAI)
                if dt<=now and dt.year>=2000:issued.append(dt.astimezone(timezone.utc).isoformat())
            except ValueError:pass
    issued=list(dict.fromkeys(issued))
    checked_in=False if any('未签到' in v for y,v in texts if y<height*.35) else True if any('已签到' in v for y,v in texts if y<height*.35) else None
    queue='reservation' if number and 7000<=int(number)<=7999 else 'ordinary'
    queue_clear=bool(number and 1<=int(number)<=7999)
    if any('预约号码' in v for y,v in texts if y<height*.4):
        queue_clear=queue=='reservation';queue='reservation'
    fields={'store_id':store_id,'number':number,'issued_at':issued[0] if len(issued)==1 else None,'checked_in':checked_in,'queue_type':queue}
    missing=[key for key in ('store_id','number','issued_at') if not fields[key]]
    return {'schema_version':1,'fields':fields,'auto_accept':not missing and number_clear and queue_clear,'missing_fields':missing,
            'store_name':names.get(store_id) if store_id else None,'store_suggestion':suggestion,
            'reason':None if not missing and number_clear and queue_clear else 'review_required','engine':'tesseract_local','raw_text_retained':False}


def number_region(image):
    """Separate white digits from the warm ticket card; return None for other layouts."""
    from PIL import Image, ImageOps
    scale=min(1,600/image.width)
    small=image.resize((round(image.width*scale),round(image.height*scale))).convert('RGB')
    w,h=small.size; pixels=small.load();groups=[];start=None
    for y in range(round(h*.08),round(h*.48)):
        warm=sum(1 for x in range(round(w*.1),round(w*.9),3)
                 if (p:=pixels[x,y])[0]-p[1]>8 and p[1]-p[2]>12)
        if warm>w*.12:
            if start is None:start=y
        elif start is not None:groups.append((start,y));start=None
    if start is not None:groups.append((start,round(h*.48)))
    if not groups:return None
    top,bottom=max(groups,key=lambda v:v[1]-v[0])
    if bottom-top<h*.06:return None
    left,right=round(w*.14),round(w*.86)
    mask=Image.new('L',(right-left,bottom-top),0);out=mask.load();bands=[];start=None
    for y in range(top,bottom):
        count=0
        for x in range(left,right):
            r,g,b=pixels[x,y]
            if min(r,g,b)>235 and max(r,g,b)-min(r,g,b)<14:
                out[x-left,y-top]=255;count+=1
        if count>w*.025:
            if start is None:start=y-top
        elif start is not None:bands.append((start,y-top));start=None
    if start is not None:bands.append((start,bottom-top))
    if not bands:return None
    a,b=max(bands,key=lambda v:v[1]-v[0])
    if b-a<h*.035:return None
    band=mask.crop((0,a,right-left,b));box=band.getbbox()
    if box is None:return None
    glyph=ImageOps.invert(band.crop(box));glyph=ImageOps.expand(glyph,border=12,fill=255)
    original=(round((left+box[0])/scale),round((top+a+box[1])/scale),
              round((box[2]-box[0])/scale),round((box[3]-box[1])/scale))
    return glyph,original


def recognize(raw,names,*,now=None,engine='/usr/bin/tesseract',directory=None):
    from PIL import Image
    width,height,suffix=dimensions(raw)
    deadline=time.monotonic()+24
    with tempfile.TemporaryDirectory(prefix='screenshot-',dir=directory) as tmp:
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.size!=(width,height) or opened.format not in {'PNG','JPEG'}:raise ValueError('image_format_unsupported')
            opened.load();image=opened.convert('RGB')
        scale=min(1,800/width);small=image.resize((round(width*scale),round(height*scale)))
        path=Path(tmp)/'text.png';small.save(path);path.chmod(0o600)
        words=Path(tmp)/'stores.txt';words.write_text('\n'.join(names.values()),encoding='utf-8');words.chmod(0o600)
        def run(path,language,mode,extra=()):
            command=[engine,str(path),'stdout','-l',language,'--psm',str(mode),*extra,'tsv']
            if os.name=='posix' and Path('/usr/bin/prlimit').exists():command=['/usr/bin/prlimit','--as=1073741824','--cpu=20','--',*command]
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('ocr_timeout')
            result=subprocess.run(command,capture_output=True,timeout=remaining,env={**os.environ,'OMP_THREAD_LIMIT':'1'},check=False)
            if result.returncode!=0 or len(result.stdout)>262144:raise ValueError('ocr_not_available')
            return list(csv.DictReader(io.StringIO(result.stdout.decode('utf-8',errors='strict')),delimiter='\t'))
        rows=run(path,'chi_sim',11,('--user-words',str(words)))
        for row in rows:
            for key in ('left','top','width','height'):row[key]=str(round(int(row[key])/scale))
        region=number_region(image)
        if region:
            glyph,box=region;path=Path(tmp)/'number.png';glyph.save(path);path.chmod(0o600)
            found=[]
            for mode in (7,8):
                extra=('-c','tessedit_char_whitelist=0123456789') if mode==8 else ()
                parts=[r for r in run(path,'eng',mode,extra) if r.get('level')=='5' and r.get('text','').strip()]
                candidate=''.join(r['text'].strip() for r in parts)
                # A narrow 1 is often read as I/| in line mode. It is trusted only
                # when the independent numeric word pass agrees with all digits.
                candidate=candidate.translate(str.maketrans({'I':'1','l':'1','|':'1','O':'0'}))
                if re.fullmatch(r'\d{1,7}',candidate):found.append((candidate,min(float(r['conf']) for r in parts)))
            if found:
                # Full-page recognition is unreliable for unusually large white digits.
                rows=[r for r in rows if not (r.get('level')=='5' and int(r['height'])>=height*.035
                    and int(r['top'])<box[1]+box[3] and int(r['top'])+int(r['height'])>box[1]
                    and int(r['left'])<box[0]+box[2] and int(r['left'])+int(r['width'])>box[0])]
                agreed=len(found)==2 and found[0][0]==found[1][0] and max(v[1] for v in found)>=60
                rows.append({'level':'5','page_num':'1','block_num':'99','par_num':'1','line_num':'1','word_num':'1','left':str(box[0]),'top':str(box[1]),'width':str(box[2]),'height':str(box[3]),'conf':str(max(v[1] for v in found)) if agreed else '30','text':found[0][0]})
        output=io.StringIO();writer=csv.DictWriter(output,fieldnames=('level','page_num','block_num','par_num','line_num','word_num','left','top','width','height','conf','text'),delimiter='\t');writer.writeheader();writer.writerows(rows)
        value=extract_tsv(output.getvalue(),width,height,names,now=now)
    value['image_sha256']=hashlib.sha256(raw).hexdigest()
    return value

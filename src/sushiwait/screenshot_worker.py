"""Offline, one-image CPU OCR worker. No image/text files or network requests."""
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import sys

MODELS = {
    'det': '4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae',
    'rec': '5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5',
}
TSV_FIELDS = ('level','page_num','block_num','par_num','line_num','word_num','left','top','width','height','conf','text')


def local_models(root):
    paths = {}
    for task, digest in MODELS.items():
        path = Path(root)/(task+'.onnx')
        if not path.is_file() or path.stat().st_size > 24*1024*1024:
            raise ValueError('ocr_models_unavailable')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('ocr_models_invalid')
        paths[task] = str(path)
    return paths


def offline(*args, **kwargs):
    raise OSError('OCR network disabled')


def recognize_pixels(raw, model_dir):
    from PIL import Image
    from .screenshot import dimensions, number_region
    width,height,_ = dimensions(raw)
    paths = local_models(model_dir)
    # Even an unexpected library download must fail locally, never send pixels.
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    from rapidocr import RapidOCR, OCRVersion, ModelType
    import cv2
    cv2.setNumThreads(1)
    with Image.open(io.BytesIO(raw)) as opened:
        if opened.size != (width,height) or opened.format not in {'PNG','JPEG'}:
            raise ValueError('image_format_unsupported')
        opened.load(); image=opened.convert('RGB')
    engine = RapidOCR(params={
        'Global.log_level':'critical', 'Global.use_cls':False,
        'Global.max_side_len':2560,
        'EngineConfig.onnxruntime.intra_op_num_threads':1,
        'EngineConfig.onnxruntime.inter_op_num_threads':1,
        'Det.ocr_version':OCRVersion.PPOCRV5, 'Rec.ocr_version':OCRVersion.PPOCRV5,
        'Det.model_type':ModelType.MOBILE, 'Rec.model_type':ModelType.MOBILE,
        'Det.model_path':paths['det'], 'Rec.model_path':paths['rec'],
        'Det.limit_side_len':1280, 'Det.limit_type':'max', 'Rec.rec_batch_num':4,
    })
    result = engine(image, use_cls=False)
    def project(result, scale=1, offset=0):
        rows=[]
        for box,text,score in zip(result.boxes if result.boxes is not None else [], result.txts or [], result.scores or []):
            left=max(0,round(min(p[0] for p in box)/scale));top=max(0,round(min(p[1] for p in box)/scale+offset))
            right=min(width,round(max(p[0] for p in box)/scale));bottom=min(height,round(max(p[1] for p in box)/scale+offset))
            rows.append(dict(level=5,left=left,top=top,width=right-left,height=bottom-top,conf=100*float(score),text=text))
        return rows
    rows=project(result)
    # Whole tall screenshots shrink the shop heading. A separate central crop
    # preserves Chinese strokes, including on compressed 600px screenshots.
    top,bottom=round(height*.28),round(height*.70)
    crop=image.crop((0,top,width,bottom));scale=1200/width
    crop=crop.resize((1200,round(crop.height*scale)),Image.Resampling.LANCZOS)
    detailed=project(engine(crop,use_cls=False),scale,top)
    central=lambda r: height*.31 < r['top']+r['height']/2 < height*.67
    if any(central(r) for r in detailed):
        rows=[r for r in rows if not central(r)]+[r for r in detailed if central(r)]
    # A second pass on the oversized white ticket number avoids losing its
    # first digit. Disagreement is surfaced for review, not hidden as certainty.
    region=number_region(image)
    if region:
        glyph,(left,top,w,h)=region
        digit_result=engine(glyph.convert('RGB'),use_det=False,use_cls=False)
        found=''.join(digit_result.txts or []).strip()
        if re.fullmatch(r'\d{1,7}',found):
            overlapping=[r for r in rows if r['height']>=height*.035 and r['left']<left+w and r['left']+r['width']>left and r['top']<top+h and r['top']+r['height']>top]
            confidence=min(digit_result.scores or [0])*100
            if any(re.fullmatch(r'\d{1,7}',r['text']) and r['text']!=found for r in overlapping):confidence=min(confidence,30)
            rows=[r for r in rows if r not in overlapping]
            rows.append(dict(level=5,left=left,top=top,width=w,height=h,conf=confidence,text=found))
    out=io.StringIO();writer=csv.DictWriter(out,fieldnames=TSV_FIELDS,delimiter='\t');writer.writeheader();writer.writerows(rows)
    if len(out.getvalue().encode())>262144:raise ValueError('ocr_projection_unsupported')
    return {'tsv':out.getvalue()}


def main():
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
        resource.setrlimit(resource.RLIMIT_CPU,(20,20))
    from .screenshot import MAX_BYTES
    raw=sys.stdin.buffer.read(MAX_BYTES+1)
    # Libraries sometimes print startup banners. Keep the protocol clean.
    with contextlib.redirect_stdout(sys.stderr):value=recognize_pixels(raw,sys.argv[1])
    sys.stdout.write(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception:
        # Never log arbitrary OCR text, pixels, paths or model exception payloads.
        sys.stderr.write('screenshot_worker_failed\n');sys.exit(1)

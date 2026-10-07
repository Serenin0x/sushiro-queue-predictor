"""Actual Linux-container HTTP, stop/recreate and terminal-resume checks, synthetic upstream only."""
import argparse,hashlib,io,json,re,subprocess,tarfile,time,uuid
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.request import ProxyHandler,build_opener

def check():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',required=True)
    parser.add_argument('--docker',default='docker')
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,199}',args.image):
        raise ValueError('invalid_image_reference')
    fixture=Path(__file__).with_name('container_fixture_service.py').resolve()
    prefix='sushiwait-smoke-'+uuid.uuid4().hex[:12]
    volume=prefix+'-state';fixture_image=prefix+':fixture';names=[]
    def run(*arguments):
        result=subprocess.run([args.docker,*arguments],capture_output=True,text=True,timeout=65)
        if result.returncode:raise RuntimeError('container_command_failed: '+result.stderr[-1500:])
        return result.stdout.strip()
    opener=build_opener(ProxyHandler({}));started=time.monotonic()
    def start():
        name=prefix+'-'+str(len(names)+1);names.append(name)
        run('run','-d','--name',name,'--init','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--pids-limit=64','--memory=256m',
            '--mount','source='+volume+',target=/state',
            '--tmpfs','/tmp:rw,noexec,nosuid,size=16m,mode=1777',
            '-p','127.0.0.1::8765','--entrypoint','python',fixture_image,'/fixture.py')
        port=run('port',name,'8765/tcp').split(':')[-1]
        config=json.loads(run('inspect',name))[0]
        assert config['Config']['User']=='10001:10001'
        assert config['HostConfig']['ReadonlyRootfs'] and config['HostConfig']['CapDrop']==['ALL']
        assert all(p['HostIp']=='127.0.0.1' for p in config['HostConfig']['PortBindings']['8765/tcp'])
        return name,'http://127.0.0.1:'+port
    def read(url,path):
        try:
            with opener.open(url+path,timeout=3) as response:return response.status,json.loads(response.read(32769))
        except HTTPError as error:
            with error:return error.code,json.loads(error.read(32769))
    def wait(url,predicate,seconds=20):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                status,value=read(url,'/api/v1/status')
                if status==200 and predicate(value):return value
            except (URLError,TimeoutError,ConnectionError):pass
            time.sleep(.2)
        raise RuntimeError('container_service_wait_expired')
    def inspect_state():
        code="import pathlib,json,hashlib,os; p=pathlib.Path('/state'); print(json.dumps({'counter':json.loads((p/'synthetic-http-count.json').read_text()),'task':json.loads((p/'task.json').read_text()),'hashes':{n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in ['remote.sqlite3','task.json','plans.json']},'modes':{n:oct((p/n).stat().st_mode & 511) for n in ['remote.sqlite3','task.json','plans.json']},'parent_mode':oct(p.stat().st_mode & 511),'uid':os.getuid()}))"
        return json.loads(run('run','--rm','--read-only','--network=none','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--mount','source='+volume+',target=/state,readonly',
            '--entrypoint','python',args.image,'-c',code))
    try:
        context=io.BytesIO()
        with tarfile.open(fileobj=context,mode='w') as archive:
            for name,body in [('Dockerfile',('FROM '+args.image+'\nCOPY fixture.py /fixture.py\n').encode()),
                              ('fixture.py',fixture.read_bytes())]:
                entry=tarfile.TarInfo(name);entry.size=len(body);entry.mode=0o644
                archive.addfile(entry,io.BytesIO(body))
        built=subprocess.run([args.docker,'build','-t',fixture_image,'-'],input=context.getvalue(),
            capture_output=True,timeout=65)
        if built.returncode:raise RuntimeError('fixture_image_build_failed: '+built.stderr.decode()[-1500:])
        run('volume','create',volume)
        first,url=start();one=wait(url,lambda v:v.get('task',{}).get('successful_pairs')==1)
        assert read(url,'/health')[0]==200
        for _ in range(5):
            code,view=read(url,'/api/v1/stores/900001/queue')
            assert code==200 and view['eta_available'] is False and view['network_performed_by_read'] is False
            code,history=read(url,'/api/v1/stores/900001/history')
            assert code==200 and history['retained_points']==1 and not history['complete_history']
            assert history['points'][0]['reported_count_raw']==1 and not history['network_performed_by_read']
        for path,name in [('/monitor','monitor.html'),('/monitor.js','monitor.js'),('/monitor.css','monitor.css')]:
            with opener.open(url+path,timeout=3) as response:
                assert response.status==200 and "connect-src 'self'" in response.headers['content-security-policy']
                assert response.read(16385)==(fixture.parent.parent/'src/sushiwait/web'/name).read_bytes()
        run('stop','--time','45',first);before=inspect_state();assert before['counter']==2
        second,url=start();two=wait(url,lambda v:v.get('service_state')=='completed',seconds=80)
        assert read(url,'/health')[0]==503
        run('stop','--time','45',second);after=inspect_state()
        assert after['counter']==4 and two['task']['successful_pairs']==2
        assert before['task']['deadline_at']==after['task']['deadline_at']
        assert after['task']['config']['max_pairs']==before['task']['config']['max_pairs']==2
        last=after['task']['last_gap']
        assert last['reason']=='restart'
        resume_wait=(datetime.fromisoformat(after['task']['starts']['900001'].replace('Z','+00:00'))
            -datetime.fromisoformat(last['to'].replace('Z','+00:00'))).total_seconds()
        assert resume_wait>=59.99
        third,url=start();terminal=wait(url,lambda v:v.get('service_state')=='completed')
        assert read(url,'/health')[0]==503
        code,view=read(url,'/api/v1/stores/900001/queue')
        assert code==200 and view['fields']['groupqueues']['state']=='saved_history'
        code,history=read(url,'/api/v1/stores/900001/history')
        assert code==200 and history['retained_points']==2 and not history['worker_alive']
        assert all(p['origin']=='saved_history' for p in history['points'])
        run('stop','--time','45',third);final=inspect_state()
        assert after['counter']==final['counter']==4 and after['hashes']==final['hashes']
        assert final['uid']==10001 and final['parent_mode']=='0o700'
        assert set(final['modes'].values())=={'0o600'}
        print(json.dumps({'ok':True,'is_live':False,'upstream_transport':'synthetic',
            'official_http_requests':0,'synthetic_pairs':2,'synthetic_http_attempts':4,
            'recreated_services':3,'reader_added_upstream_requests':0,
            'original_deadline_preserved':True,'original_budget_preserved':True,
            'terminal_resume_unchanged':True,'non_root_uid':10001,'read_only_root':True,
            'resume_wait_seconds':resume_wait,
            'host_bind':'127.0.0.1','elapsed_seconds':round(time.monotonic()-started,3)}))
    finally:
        for name in names:
            subprocess.run([args.docker,'rm','-f',name],capture_output=True,timeout=50)
        subprocess.run([args.docker,'volume','rm',volume],capture_output=True,timeout=20)
        subprocess.run([args.docker,'image','rm',fixture_image],capture_output=True,timeout=20)

if __name__=='__main__':check()

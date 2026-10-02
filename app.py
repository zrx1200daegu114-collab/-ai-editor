import os, json, secrets, shutil, subprocess, tempfile, threading, time, uuid, selectors
from pathlib import Path
from flask import Flask, request, jsonify, send_file, session, redirect, render_template_string
from datetime import timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES + 10 * 1024 * 1024
app.secret_key = os.environ.get('SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
OWNER_PASSWORD = os.environ.get('OWNER_PASSWORD', '')
OWNER_HASH = generate_password_hash(OWNER_PASSWORD) if len(OWNER_PASSWORD) >= 16 else None
LOGIN_FAILURES = []
LOGIN_LOCK = threading.Lock()

LOGIN_HTML = """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>藤原専用ログイン</title>
<style>body{background:#0b0d12;color:white;font-family:sans-serif;max-width:420px;margin:60px auto;padding:20px}input,button{box-sizing:border-box;width:100%;padding:16px;margin-top:16px;border-radius:10px}button{background:#7d5cff;color:white;border:0}p{line-height:1.6}</style>
<h1>藤原専用</h1><p>専用パスワードでログインしてください。</p><p>{{ error }}</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="password" name="password" autocomplete="current-password" placeholder="専用パスワード" required><button>ログイン</button></form></html>"""

def csrf_token():
    if 'csrf' not in session: session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']

@app.before_request
def owner_only():
    if request.path == '/health': return None
    if not OWNER_HASH:
        return '専用パスワードの設定が必要です。管理画面でOWNER_PASSWORDを16文字以上に設定してください。', 503
    if request.path != '/login' and not session.get('owner'):
        if request.path.startswith('/api/'): return jsonify(error='ログインが必要です。ページを開き直してください。'), 401
        return redirect('/login')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        supplied = request.headers.get('X-CSRF-Token') or (request.form.get('csrf', '') if request.path in ('/login','/logout') else '')
        if not secrets.compare_digest(session.get('csrf', 'missing'), supplied):
            return jsonify(error='ページを開き直してから操作してください。'), 403

@app.after_request
def private_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        now = time.monotonic()
        with LOGIN_LOCK:
            LOGIN_FAILURES[:] = [t for t in LOGIN_FAILURES if now-t < 60]
            if len(LOGIN_FAILURES) >= 5:
                return render_template_string(LOGIN_HTML, csrf=csrf_token(), error='1分待ってから再試行してください。'), 429
            if not check_password_hash(OWNER_HASH, request.form.get('password', '')):
                LOGIN_FAILURES.append(now)
                error = 'パスワードが違います。'
            else:
                session.clear()
                session['owner'] = True
                session.permanent = True
                csrf_token()
                return redirect('/')
    return render_template_string(LOGIN_HTML, csrf=csrf_token(), error=error), (401 if error else 200)

@app.post('/logout')
def logout():
    session.clear()
    return redirect('/login')

ROOT = Path(tempfile.mkdtemp(prefix='video-editor-'))
JOBS = {}
LOCK = threading.Lock()
BUSY = threading.Semaphore(1)
TTL = 3600

def run(args, timeout=900):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        raise ValueError('動画を読み込めませんでした。別の動画で試してください。')
    return p.stdout

def run_conversion(args, job, completed, duration, total, timeout=900):
    started = time.monotonic()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(args[:-1] + ['-progress', 'pipe:1', '-nostats', args[-1]],
                                   stdout=subprocess.PIPE, stderr=errors)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                pending = b''
                while True:
                    if time.monotonic() - started > timeout:
                        raise subprocess.TimeoutExpired(args, timeout)
                    if not selector.select(timeout=1):
                        continue
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk: break
                    pending += chunk
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if line.startswith(b'out_time_us='):
                            try:
                                seconds = min(duration, max(0, int(line.split(b'=', 1)[1]) / 1000000))
                                job['percent'] = min(99, round(100 * (completed + seconds) / total))
                            except ValueError: pass
            if process.wait(timeout=5):
                raise ValueError('動画の変換に失敗しました。短い動画で試してください。')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

def probe(path):
    return json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],30))

def clean():
    now = time.time()
    with LOCK:
        for key, job in list(JOBS.items()):
            if job['status'] != 'processing' and now-job['created'] > TTL:
                shutil.rmtree(ROOT/key, ignore_errors=True)
                del JOBS[key]

def render(key, files, aspect):
    job = JOBS[key]
    folder = ROOT/key
    try:
        infos = [probe(f) for f in files]
        duration = 0
        for info in infos:
            videos = [s for s in info['streams'] if s['codec_type']=='video']
            d = float(info.get('format',{}).get('duration',0))
            if not videos or d <= 0 or d > 600:
                raise ValueError('長さを確認できる、10分以内の動画を選んでください。')
            duration += d
        if duration > 600:
            raise ValueError('動画の合計を10分以内にしてください。')
        first = next(s for s in infos[0]['streams'] if s['codec_type']=='video')
        if aspect == 'original':
            rotation = float(first.get('tags',{}).get('rotate',0))
            for side in first.get('side_data_list',[]):
                rotation = float(side.get('rotation',rotation))
            w,h = first['width'],first['height']
            if abs(rotation)%180 == 90: w,h=h,w
            aspect = 'vertical' if h>w else ('square' if h==w else 'horizontal')
        w,h = {'vertical':(720,1280),'horizontal':(1280,720),'square':(720,720)}[aspect]
        completed = 0
        for i,(source,info) in enumerate(zip(files,infos)):
            job['message'] = f'{i+1}/{len(files)}本目を変換中'
            audio = any(s['codec_type']=='audio' for s in info['streams'])
            d = float(info['format']['duration'])
            cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-threads','1','-i',str(source)]
            if not audio: cmd += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            cmd += ['-map','0:v:0','-map','0:a:0' if audio else '1:a:0',
                    '-vf',f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p',
                    '-af','aresample=48000,apad','-t',str(d),'-c:v','libx264','-preset','superfast','-crf','24','-threads','1',
                    '-c:a','aac','-ac','2','-ar','48000','-b:a','128k',str(folder/f'clip{i}.mp4')]
            run_conversion(cmd, job, completed, d, duration)
            completed += d
            source.unlink(missing_ok=True)
        job['message']='MP4を仕上げています'
        listing = folder/'concat.txt'
        listing.write_text(''.join(f"file 'clip{i}.mp4'\n" for i in range(len(files))))
        run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','1','-i',str(listing),'-c','copy','-movflags','+faststart',str(folder/'result.mp4')])
        job.update(status='done',message='動画が完成しました',percent=100)
    except subprocess.TimeoutExpired:
        job.update(status='error',message='処理時間を超えました。短い動画で試してください。')
    except Exception as e:
        job.update(status='error',message=str(e) if isinstance(e,ValueError) else '処理に失敗しました。もう一度試してください。')
    finally:
        for f in folder.iterdir():
            if f.name != 'result.mp4': f.unlink(missing_ok=True)
        BUSY.release()

@app.get('/')
def index():
    return HTML.replace('__CSRF__', csrf_token())

@app.get('/health')
def health(): return {'status':'ok'}

@app.errorhandler(413)
def too_large(e): return jsonify(error='動画の合計サイズを2GB以内にしてください。'),413

@app.post('/api/jobs')
def create():
    clean()
    if not BUSY.acquire(blocking=False):
        return jsonify(error='前に送った動画を処理中です。ページを開き直すと状況を確認できます。'),429
    key=uuid.uuid4().hex
    folder=ROOT/key
    try:
        incoming = request.files.getlist('videos')
        aspect = request.form.get('aspect','original')
        if not 1<=len(incoming)<=5 or aspect not in ('original','vertical','horizontal','square'):
            raise ValueError('動画は1〜5本選んでください。')
        folder.mkdir()
        paths=[]
        total=0
        for i,file in enumerate(incoming):
            path=folder/f'input{i}'
            with path.open('wb') as target:
                while True:
                    chunk = file.stream.read(1024 * 1024)
                    if not chunk: break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise ValueError('動画の合計サイズを2GB以内にしてください。')
                    if shutil.disk_usage(folder).free < len(chunk) + 512 * 1024 * 1024:
                        raise ValueError('サーバーの空き容量が不足しています。少ない本数で試してください。')
                    target.write(chunk)
            paths.append(path)
        if total>MAX_UPLOAD_BYTES or any(p.stat().st_size==0 for p in paths):
            raise ValueError('空の動画は使えません。合計サイズは2GB以内にしてください。')
        token=secrets.token_urlsafe(32)
        JOBS[key]={'token':token,'status':'processing','message':'動画を確認しています','created':time.time(),'percent':0}
        threading.Thread(target=render,args=(key,paths,aspect),daemon=True).start()
        return jsonify(id=key,token=token),202
    except Exception as e:
        BUSY.release()
        shutil.rmtree(folder,ignore_errors=True)
        if isinstance(e, HTTPException): raise
        return jsonify(error=str(e) if isinstance(e,ValueError) else '動画の受信に失敗しました。'),400

@app.get('/api/jobs/current')
def current_job():
    clean()
    with LOCK:
        entries = list(JOBS.items())
    if not entries: return jsonify(job=None)
    processing = [(key, job) for key, job in entries if job['status'] == 'processing']
    key, job = max(processing or entries, key=lambda entry: entry[1]['created'])
    return jsonify(job={'id':key, 'token':job['token'], 'status':job['status']})

def authorized(key):
    clean()
    job=JOBS.get(key)
    token=request.args.get('token','')
    return job if job and secrets.compare_digest(job['token'],token) else None

@app.get('/api/jobs/<key>')
def status(key):
    job=authorized(key)
    if not job: return jsonify(error='動画が見つかりません。保存期限は1時間です。'),404
    return jsonify(status=job['status'],message=job['message'],percent=job.get('percent',0),elapsed=int(time.time()-job['created']))

@app.get('/api/jobs/<key>/video')
def video(key):
    job=authorized(key)
    if not job or job['status']!='done': return jsonify(error='動画が見つかりません。'),404
    return send_file(ROOT/key/'result.mp4',mimetype='video/mp4',as_attachment=request.args.get('download')=='1',download_name='wedding-edited.mp4',conditional=True)

HTML = '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>動画編集アプリ</title>
<style>body{margin:0;background:#0b0d12;color:#fff;font-family:-apple-system,sans-serif}.w{max-width:720px;margin:auto;padding:24px 16px 60px}.card{background:#171b25;border:1px solid #303747;border-radius:18px;padding:18px;margin:16px 0}h1{font-size:28px}.sub,.note{color:#aeb6c7;line-height:1.6}.step{color:#a994ff;font-weight:bold}.upload,button,.save{display:block;border-radius:12px;padding:18px;text-align:center}input[type=file]{position:absolute;width:1px;height:1px;opacity:0}.upload{border:2px dashed #59637b;cursor:pointer}.file{display:flex;gap:8px;align-items:center;background:#10141c;margin-top:8px;padding:10px;border-radius:10px}.file span{flex:1;overflow-wrap:anywhere}.file button{width:auto;padding:8px;margin:0;background:#303747}select,button{width:100%;box-sizing:border-box;font-size:16px;color:white}select{background:#0f131b;border:1px solid #343c4d;padding:14px;border-radius:12px}button,.save{border:0;background:#7d5cff;color:#fff;font-weight:bold;margin-top:16px;text-decoration:none}button:disabled{opacity:.45}.note{font-size:13px}video{width:100%;max-height:520px;margin-top:16px}.hidden{display:none}label{display:block;margin:12px 0}progress{width:100%}</style>
<div class="w"><form method="post" action="/logout"><input type="hidden" name="csrf" value="__CSRF__"><button>ログアウト</button></form><h1>藤原専用・動画編集アプリ</h1><p class="sub">複数の動画を1本のMP4に。結婚式の思い出を、選んだ順番でまとめます。</p>
<div class="card"><div class="step">STEP 1</div><h3>動画を選ぶ</h3><label class="upload" for="files">＋ 動画を選択</label><input id="files" type="file" accept="video/*" multiple><p class="note">1〜5本・合計2GB／10分以内。矢印で順番を変えられます。</p><p id="size" class="note" aria-live="polite"></p><div id="list"></div></div>
<div class="card"><div class="step">STEP 2</div><h3>完成動画の画角</h3><select id="aspect"><option value="original">最初の動画に合わせる</option><option value="vertical">縦 9:16（TikTok・Reels・Shorts）</option><option value="horizontal">横 16:9（YouTube・式の記録）</option><option value="square">正方形 1:1</option></select><p class="note">人物が切れないよう、余白を付けて画角を揃えます。元の音声は残します。出力は720p相当です。</p><button id="go" disabled>動画を結合してMP4を作る</button><p class="note">この版では動画の結合と保存ができます。AIによる見どころ選択・自動字幕・BGM追加はまだ入っていません。</p></div>
<div id="out" class="card hidden" aria-live="polite"><h3 id="message"></h3><progress id="progress"></progress><video id="preview" class="hidden" controls playsinline></video><a id="save" class="save hidden">MP4を保存</a><p id="hint" class="note"></p></div></div>
<script>
let selected=[],busy=false;const $=id=>document.getElementById(id);function draw(){const total=selected.reduce((n,f)=>n+f.size,0);$('size').textContent=selected.length?selected.length+'本・合計 '+(total/(1024*1024)).toFixed(1)+' MB（上限 2GB）':''; $('list').replaceChildren();selected.forEach((f,i)=>{const row=document.createElement('div');row.className='file';const name=document.createElement('span');name.textContent=(i+1)+'．'+f.name;row.append(name);for(const [label,delta] of [['↑',-1],['↓',1]]){const b=document.createElement('button');b.textContent=label;b.setAttribute('aria-label',f.name+'を'+(delta<0?'前':'後')+'へ');b.disabled=busy||i+delta<0||i+delta>=selected.length;b.onclick=()=>{[selected[i],selected[i+delta]]=[selected[i+delta],selected[i]];draw()};row.append(b)}$('list').append(row)});$('go').disabled=busy||!selected.length;$('files').disabled=busy;$('aspect').disabled=busy}
$('files').onchange=()=>{selected=[...$('files').files];draw()};const pause=ms=>new Promise(r=>setTimeout(r,ms));

function showOutput(){
 $('out').classList.remove('hidden');$('preview').classList.add('hidden');
 $('preview').removeAttribute('src');$('save').classList.add('hidden');
 $('progress').classList.remove('hidden');$('progress').removeAttribute('value');
 $('out').scrollIntoView({behavior:'smooth'});
}
async function getCurrent(){
 const response=await fetch('/api/jobs/current');
 if(!response.ok)throw Error(response.status===401?'ログインが必要です。ページを開き直してください。':'処理状況を確認できません。少し待ってから再試行してください。');
 return (await response.json()).job;
}
async function watch(job){
 const base='/api/jobs/'+job.id,query='?token='+encodeURIComponent(job.token);
 for(;;){
  const response=await fetch(base+query);
  if(!response.ok)throw Error(response.status===404?'処理情報が消えました。サーバー再起動などで中断された可能性があります。':'処理状況を取得できません。ページを開き直してください。');
  const status=await response.json();
  $('message').textContent=status.message;
  $('progress').max=100;$('progress').value=status.percent;
  $('hint').textContent='変換 '+status.percent+'%・処理開始から '+Math.floor(status.elapsed/60)+'分 '+status.elapsed%60+'秒。開き直してもこの画面へ戻れます。';
  if(status.status==='error')throw Error(status.message);
  if(status.status==='done'){
   const url=base+'/video'+query;$('preview').src=url;$('preview').classList.remove('hidden');
   $('save').href=url+'&download=1';$('save').download='wedding-edited.mp4';$('save').classList.remove('hidden');
   $('hint').textContent='保存期限は処理開始から約1時間です。「MP4を保存」で保存してください。';return;
  }
  await pause(2000);
 }
}
function sendVideos(body){return new Promise((resolve,reject)=>{
 const xhr=new XMLHttpRequest();xhr.open('POST','/api/jobs');xhr.setRequestHeader('X-CSRF-Token','__CSRF__');
 xhr.timeout=30*60*1000;
 xhr.upload.onprogress=event=>{
  if(event.lengthComputable){const percent=Math.round(event.loaded/event.total*100);$('progress').max=100;$('progress').value=percent;$('message').textContent='動画を送信しています '+percent+'%';}
 };
 xhr.upload.onload=()=>{$('message').textContent='サーバーで動画の受信を確認しています';};
 xhr.onerror=()=>reject(Error('送信中に通信が切れました。ページを開き直して処理状況を確認してください。'));
 xhr.ontimeout=()=>reject(Error('送信に30分以上かかりました。通信環境を確認してください。'));
 xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);if(xhr.status<200||xhr.status>=300)reject(Error(data.error||'送信に失敗しました'));else resolve(data);}catch(e){reject(Error('サーバーから正常な応答がありません。ページを開き直して処理状況を確認してください。'));}};
 xhr.send(body);
});}
async function task(work){
 busy=true;draw();showOutput();
 try{await work();}catch(e){$('message').textContent=e.message;$('hint').textContent='再送信する前に、ページを開き直すと前の処理状況を確認できます。';}
 finally{busy=false;$('progress').classList.add('hidden');draw();}
}
$('go').onclick=()=>task(async()=>{
 $('message').textContent='前の処理を確認しています';
 const existing=await getCurrent();
 if(existing&&existing.status==='processing'){await watch(existing);return;}
 const total=selected.reduce((n,f)=>n+f.size,0);
 if(!selected.length||selected.length>5||total>2*1024*1024*1024)throw Error('1〜5本、合計2GB以内で選んでください。');
 const body=new FormData();selected.forEach(f=>body.append('videos',f));body.append('aspect',$('aspect').value);
 $('message').textContent='動画を送信しています 0%';$('hint').textContent='送信が終わるまでSafariを開いたままお待ちください。';
 try{await watch(await sendVideos(body));}catch(error){
  const active=await getCurrent().catch(()=>null);
  if(active&&active.status==='processing'){await watch(active);return;}throw error;
 }
});
(async()=>{
 busy=true;draw();
 try{const job=await getCurrent();if(job){await task(()=>watch(job));}}
 catch(e){showOutput();$('message').textContent=e.message;}
 finally{busy=false;draw();}
})();
</script></html>'''
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',10000)))

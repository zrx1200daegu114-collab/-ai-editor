import os, json, secrets, shutil, subprocess, tempfile, threading, time, uuid
from pathlib import Path
from flask import Flask, request, jsonify, send_file, session, redirect, render_template_string
from datetime import timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 210 * 1024 * 1024
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
        for i,(source,info) in enumerate(zip(files,infos)):
            job['message'] = f'{i+1}/{len(files)}本目を変換中'
            audio = any(s['codec_type']=='audio' for s in info['streams'])
            d = float(info['format']['duration'])
            cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-threads','1','-i',str(source)]
            if not audio: cmd += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            cmd += ['-map','0:v:0','-map','0:a:0' if audio else '1:a:0',
                    '-vf',f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p',
                    '-af','aresample=48000,apad','-t',str(d),'-c:v','libx264','-preset','veryfast','-crf','24','-threads','1',
                    '-c:a','aac','-ac','2','-ar','48000','-b:a','128k',str(folder/f'clip{i}.mp4')]
            run(cmd)
            source.unlink(missing_ok=True)
        job['message']='MP4を仕上げています'
        listing = folder/'concat.txt'
        listing.write_text(''.join(f"file 'clip{i}.mp4'\n" for i in range(len(files))))
        run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','1','-i',str(listing),'-c','copy','-movflags','+faststart',str(folder/'result.mp4')])
        job.update(status='done',message='動画が完成しました')
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
def too_large(e): return jsonify(error='動画の合計サイズを200MB以内にしてください。'),413

@app.post('/api/jobs')
def create():
    clean()
    incoming = request.files.getlist('videos')
    aspect = request.form.get('aspect','original')
    if not 1<=len(incoming)<=5 or aspect not in ('original','vertical','horizontal','square'):
        return jsonify(error='動画は1〜5本選んでください。'),400
    if not BUSY.acquire(blocking=False):
        return jsonify(error='別の動画を処理中です。完成後にもう一度試してください。'),429
    key=uuid.uuid4().hex
    folder=ROOT/key
    folder.mkdir()
    try:
        paths=[]
        total=0
        for i,file in enumerate(incoming):
            path=folder/f'input{i}'
            file.save(path)
            total += path.stat().st_size
            paths.append(path)
        if total>200*1024*1024 or any(p.stat().st_size==0 for p in paths):
            raise ValueError('空の動画は使えません。合計サイズは200MB以内にしてください。')
        token=secrets.token_urlsafe(32)
        JOBS[key]={'token':token,'status':'processing','message':'動画を確認しています','created':time.time()}
        threading.Thread(target=render,args=(key,paths,aspect),daemon=True).start()
        return jsonify(id=key,token=token),202
    except Exception as e:
        BUSY.release()
        shutil.rmtree(folder,ignore_errors=True)
        return jsonify(error=str(e) if isinstance(e,ValueError) else '動画の受信に失敗しました。'),400

def authorized(key):
    clean()
    job=JOBS.get(key)
    token=request.args.get('token','')
    return job if job and secrets.compare_digest(job['token'],token) else None

@app.get('/api/jobs/<key>')
def status(key):
    job=authorized(key)
    if not job: return jsonify(error='動画が見つかりません。保存期限は1時間です。'),404
    return jsonify(status=job['status'],message=job['message'])

@app.get('/api/jobs/<key>/video')
def video(key):
    job=authorized(key)
    if not job or job['status']!='done': return jsonify(error='動画が見つかりません。'),404
    return send_file(ROOT/key/'result.mp4',mimetype='video/mp4',as_attachment=request.args.get('download')=='1',download_name='wedding-edited.mp4',conditional=True)

HTML = '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>動画編集アプリ</title>
<style>body{margin:0;background:#0b0d12;color:#fff;font-family:-apple-system,sans-serif}.w{max-width:720px;margin:auto;padding:24px 16px 60px}.card{background:#171b25;border:1px solid #303747;border-radius:18px;padding:18px;margin:16px 0}h1{font-size:28px}.sub,.note{color:#aeb6c7;line-height:1.6}.step{color:#a994ff;font-weight:bold}.upload,button,.save{display:block;border-radius:12px;padding:18px;text-align:center}input[type=file]{position:absolute;width:1px;height:1px;opacity:0}.upload{border:2px dashed #59637b;cursor:pointer}.file{display:flex;gap:8px;align-items:center;background:#10141c;margin-top:8px;padding:10px;border-radius:10px}.file span{flex:1;overflow-wrap:anywhere}.file button{width:auto;padding:8px;margin:0;background:#303747}select,button{width:100%;box-sizing:border-box;font-size:16px;color:white}select{background:#0f131b;border:1px solid #343c4d;padding:14px;border-radius:12px}button,.save{border:0;background:#7d5cff;color:#fff;font-weight:bold;margin-top:16px;text-decoration:none}button:disabled{opacity:.45}.note{font-size:13px}video{width:100%;max-height:520px;margin-top:16px}.hidden{display:none}label{display:block;margin:12px 0}progress{width:100%}</style>
<div class="w"><form method="post" action="/logout"><input type="hidden" name="csrf" value="__CSRF__"><button>ログアウト</button></form><h1>藤原専用・動画編集アプリ</h1><p class="sub">複数の動画を1本のMP4に。結婚式の思い出を、選んだ順番でまとめます。</p>
<div class="card"><div class="step">STEP 1</div><h3>動画を選ぶ</h3><label class="upload" for="files">＋ 動画を選択</label><input id="files" type="file" accept="video/*" multiple><p class="note">1〜5本・合計200MB／10分以内。矢印で順番を変えられます。</p><div id="list"></div></div>
<div class="card"><div class="step">STEP 2</div><h3>完成動画の画角</h3><select id="aspect"><option value="original">最初の動画に合わせる</option><option value="vertical">縦 9:16（TikTok・Reels・Shorts）</option><option value="horizontal">横 16:9（YouTube・式の記録）</option><option value="square">正方形 1:1</option></select><p class="note">人物が切れないよう、余白を付けて画角を揃えます。元の音声は残します。出力は720p相当です。</p><button id="go" disabled>動画を結合してMP4を作る</button><p class="note">この版では動画の結合と保存ができます。AIによる見どころ選択・自動字幕・BGM追加はまだ入っていません。</p></div>
<div id="out" class="card hidden" aria-live="polite"><h3 id="message"></h3><progress id="progress"></progress><video id="preview" class="hidden" controls playsinline></video><a id="save" class="save hidden">MP4を保存</a><p id="hint" class="note"></p></div></div>
<script>
let selected=[],busy=false;const $=id=>document.getElementById(id);function draw(){ $('list').replaceChildren();selected.forEach((f,i)=>{const row=document.createElement('div');row.className='file';const name=document.createElement('span');name.textContent=(i+1)+'．'+f.name;row.append(name);for(const [label,delta] of [['↑',-1],['↓',1]]){const b=document.createElement('button');b.textContent=label;b.setAttribute('aria-label',f.name+'を'+(delta<0?'前':'後')+'へ');b.disabled=busy||i+delta<0||i+delta>=selected.length;b.onclick=()=>{[selected[i],selected[i+delta]]=[selected[i+delta],selected[i]];draw()};row.append(b)}$('list').append(row)});$('go').disabled=busy||!selected.length;$('files').disabled=busy;$('aspect').disabled=busy}
$('files').onchange=()=>{selected=[...$('files').files];draw()};const pause=ms=>new Promise(r=>setTimeout(r,ms));
$('go').onclick=async()=>{if(selected.length>5||selected.reduce((n,f)=>n+f.size,0)>200*1024*1024){alert('1〜5本、合計200MB以内で選んでください。');return}busy=true;draw();$('out').classList.remove('hidden');$('preview').classList.add('hidden');$('preview').removeAttribute('src');$('save').classList.add('hidden');$('progress').classList.remove('hidden');$('message').textContent='動画を送信しています';$('hint').textContent='この画面を開いたままお待ちください。動画の長さによって数分かかります。';$('out').scrollIntoView({behavior:'smooth'});try{const body=new FormData();selected.forEach(f=>body.append('videos',f));body.append('aspect',$('aspect').value);const r=await fetch('/api/jobs',{method:'POST',headers:{'X-CSRF-Token':'__CSRF__'},body});const job=await r.json();if(!r.ok)throw Error(job.error||'送信に失敗しました');const base='/api/jobs/'+job.id;const query='?token='+encodeURIComponent(job.token);for(;;){await pause(2000);const response=await fetch(base+query);const status=await response.json();if(!response.ok)throw Error(status.error);$('message').textContent=status.message;if(status.status==='error')throw Error(status.message);if(status.status==='done'){const url=base+'/video'+query;$('preview').src=url;$('preview').classList.remove('hidden');$('save').href=url+'&download=1';$('save').download='wedding-edited.mp4';$('save').classList.remove('hidden');$('hint').textContent='保存期限は1時間です。iPhoneでは「MP4を保存」でダウンロード後、ファイルアプリから共有→「ビデオを保存」で写真に入れられます。';break}}}catch(e){$('message').textContent=e.message;$('hint').textContent='通信が切れた場合は、動画を選び直してもう一度試してください。'}finally{busy=false;$('progress').classList.add('hidden');draw()}};
</script></html>'''
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',10000)))

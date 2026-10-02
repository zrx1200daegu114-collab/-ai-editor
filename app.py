import os, json, secrets, shutil, subprocess, tempfile, threading, time, uuid, selectors, queue, math
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
MAX_ACTIVE_JOBS = 5  # Owner edition; future free plan can accept three jobs.
BUSY = threading.BoundedSemaphore(MAX_ACTIVE_JOBS)
WORK_QUEUE = queue.Queue()
WORKER_LOCK = threading.Lock()
WORKER_STARTED = False

def start_worker():
    global WORKER_STARTED
    with WORKER_LOCK:
        if WORKER_STARTED: return
        threading.Thread(target=queue_worker, daemon=True).start()
        WORKER_STARTED = True

def queue_worker():
    while True:
        key, files, aspect = WORK_QUEUE.get()
        try:
            with LOCK:
                JOBS[key].update(status='processing', message='動画を確認しています', started=time.time())
            render(key, files, aspect)
        finally:
            WORK_QUEUE.task_done()
TTL = 3600
SNS = {'original':'元の画角', 'tiktok':'TikTok', 'instagram':'Instagram Reels', 'shorts':'YouTube Shorts', 'x':'X', 'youtube':'YouTube'}
GENRES = ['お笑い・コメディ','グルメ・料理','Vlog','ゲーム','配信・切り抜き','音楽','美容・ファッション','旅行','ビジネス','商品紹介']
STYLES = {'full':'全編を残す', 'reach':'再生数・伸び重視', 'tempo':'テンポ重視', 'stylish':'おしゃれ重視', 'pro':'プロっぽく'}
TEMPO_SECONDS = dict(zip(GENRES,[12,8,8,12,15,20,8,8,15,10]))

def run(args, timeout=900):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        raise ValueError('動画を読み込めませんでした。別の動画で試してください。')
    return p.stdout

def run_conversion(args, job, completed, duration, total, timeout=7200):
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
                                job['percent'] = min(99, round(100 * (completed + seconds) / total, 2))
                            except ValueError: pass
            if process.wait(timeout=5):
                raise ValueError('動画の変換に失敗しました。短い動画で試してください。')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

def probe(path):
    return json.loads(run(['ffprobe','-v','error','-show_streams','-show_data','-show_format','-of','json',str(path)],30))

def copy_compatible(infos, video_only=False):
    fields = ('codec_type','codec_name','profile','level','width','height','pix_fmt',
              'sample_aspect_ratio','r_frame_rate','field_order',
              'sample_fmt','sample_rate','channels','channel_layout','extradata',
              'color_range','color_space','color_transfer','color_primaries')
    signatures = []
    for info in infos:
        streams = [stream for stream in info['streams'] if stream['codec_type'] in ('video','audio')]
        videos = [stream for stream in streams if stream['codec_type']=='video']
        audios = [stream for stream in streams if stream['codec_type']=='audio']
        if len(videos)!=1 or len(audios)>1: return False
        if videos[0]['codec_name'] not in ('h264','hevc'): return False
        if not video_only and audios and audios[0]['codec_name']!='aac': return False
        if video_only: streams = videos
        if any(not stream.get('extradata') for stream in streams): return False
        # Average FPS is derived from each clip's duration, not codec compatibility.
        # Container time bases are normalized by the remux below.
        signature = []
        for stream in streams:
            rotation = float(stream.get('tags',{}).get('rotate',0))
            for side in stream.get('side_data_list',[]):
                rotation = float(side.get('rotation',rotation))
            color_metadata = [side for side in stream.get('side_data_list',[])
                              if side.get('side_data_type') in ('Mastering display metadata','Content light level metadata','DOVI configuration record')]
            signature.append((tuple(stream.get(field) for field in fields), rotation % 360, color_metadata))
        signatures.append(signature)
    return bool(signatures) and all(signature==signatures[0] for signature in signatures)

def copy_merge(folder, files, infos, job, duration):
    normalize_audio = not copy_compatible(infos)
    job['message']='映像は元画質のまま、音声を揃えて結合しています' if normalize_audio else '元画質のまま高速結合しています'
    completed = 0
    for i,(source,info) in enumerate(zip(files,infos)):
        d=float(info['format']['duration'])
        cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(source)]
        audio = any(s['codec_type']=='audio' for s in info['streams'])
        if normalize_audio and not audio:
            cmd += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
        cmd += ['-map','0:v:0','-map',('0:a:0' if audio else '1:a:0') if normalize_audio else '0:a:0?',
                '-c:v','copy','-video_track_timescale','90000']
        if normalize_audio:
            cmd += ['-c:a','aac','-ac','2','-ar','48000','-b:a','128k','-af','aresample=48000,apad','-t',str(d)]
        else:
            cmd += ['-c:a','copy']
        if next(stream for stream in info['streams'] if stream['codec_type']=='video')['codec_name']=='hevc':
            cmd += ['-tag:v','hvc1']
        cmd += [str(folder/f'fast{i}.mp4')]
        run_conversion(cmd,job,completed,d,duration)
        completed += d
    normalized = [probe(folder/f'fast{i}.mp4') for i in range(len(files))]
    if not copy_compatible(normalized):
        raise ValueError('映像または音声の形式を揃える必要があります')
    listing=folder/'fast-concat.txt'
    listing.write_text(''.join(f"file 'fast{i}.mp4'\n" for i in range(len(files))))
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','1',
         '-i',str(listing),'-map','0:v:0','-map','0:a:0?','-c','copy','-movflags','+faststart']
    if next(stream for stream in infos[0]['streams'] if stream['codec_type']=='video')['codec_name']=='hevc':
        cmd += ['-tag:v','hvc1']
    run(cmd+[str(folder/'result.mp4')])
    result=probe(folder/'result.mp4')
    if abs(float(result['format']['duration'])-duration)>max(1,duration*.02):
        raise ValueError('高速結合の長さを確認できませんでした。')

def aspect_matches(infos, aspect):
    if aspect == 'original': return True
    target = {'vertical':9/16, 'horizontal':16/9, 'square':1}[aspect]
    for info in infos:
        video = next(s for s in info['streams'] if s['codec_type']=='video')
        if video.get('sample_aspect_ratio') not in (None, '1:1'): return False
        rotation = float(video.get('tags',{}).get('rotate',0))
        for side in video.get('side_data_list',[]):
            rotation = float(side.get('rotation',rotation))
        w,h = video['width'],video['height']
        if abs(rotation)%180 == 90: w,h=h,w
        if abs(w/h-target)>0.001: return False
    return True

def clean():
    now = time.time()
    with LOCK:
        for key, job in list(JOBS.items()):
            if job['status'] not in ('processing','queued') and now-job.get('finished',job['created']) > TTL:
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
            if not videos or not math.isfinite(d) or d <= 0 or d > 600:
                raise ValueError('長さを確認できる、10分以内の動画を選んでください。')
            duration += d
        if duration > 600:
            raise ValueError('動画の合計を10分以内にしてください。')
        style = job.get('style','full')
        if style in ('tempo','reach'):
            cap = TEMPO_SECONDS[job['genre']] if style=='tempo' else 15
            for info in infos: info['format']['duration'] = str(min(float(info['format']['duration']),cap))
            duration = sum(float(info['format']['duration']) for info in infos)
        same_aspect = aspect_matches(infos, aspect)
        reason = '画角変更または編集効果のため' if not same_aspect or style!='full' else '映像の形式・解像度・撮影設定が異なるため'
        if same_aspect and style=='full' and copy_compatible(infos, video_only=True):
            try:
                copy_merge(folder, files, infos, job, duration)
                job.update(status='done',message='動画が完成しました（元画質・高速結合）',percent=100)
                return
            except (ValueError, subprocess.TimeoutExpired):
                app.logger.exception('Fast merge failed; falling back to video encoding')
                reason = '高速結合の検証に通らなかったため'
                for temporary in folder.glob('fast*'): temporary.unlink(missing_ok=True)
                (folder/'result.mp4').unlink(missing_ok=True)
                job['percent']=0
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
            job['message'] = f'{i+1}/{len(files)}本目を変換中（{reason}）'
            audio = any(s['codec_type']=='audio' for s in info['streams'])
            d = float(info['format']['duration'])
            cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-threads','1','-filter_threads','1','-i',str(source)]
            if not audio: cmd += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            vf = f'scale={w}:{h}:flags=fast_bilinear:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p'
            if style=='stylish':
                fade = min(.35,d/2)
                vf += f',fade=t=in:st=0:d={fade},fade=t=out:st={d-fade}:d={fade}'
            cmd += ['-map','0:v:0','-map','0:a:0' if audio else '1:a:0',
                    '-vf',vf,
                    '-af','aresample=48000,apad','-t',str(d),'-c:v','libx264','-preset','ultrafast','-crf','20' if style=='pro' else '24','-threads','1',
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
        job.update(status='error',message='サーバーの処理時間上限に達しました。動画の長さだけが原因とは限りません。')
    except Exception as e:
        job.update(status='error',message=str(e) if isinstance(e,ValueError) else '処理に失敗しました。もう一度試してください。')
    finally:
        job['finished'] = time.time()
        try:
            for f in folder.iterdir():
                if f.name != 'result.mp4': f.unlink(missing_ok=True)
        except OSError:
            app.logger.exception('Temporary video cleanup failed')
        finally:
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
        return jsonify(error='制作依頼は同時に5件までです。どれかが完成したら追加できます。'),429
    key=uuid.uuid4().hex
    folder=ROOT/key
    try:
        incoming = request.files.getlist('videos')
        aspect = request.form.get('aspect','original')
        sns = request.form.get('sns','original')
        genre = request.form.get('genre',GENRES[0])
        style = request.form.get('style','full')
        if sns not in SNS or genre not in GENRES or style not in STYLES: raise ValueError('編集設定を選び直してください。')
        if aspect=='auto': aspect = 'vertical' if sns in ('tiktok','instagram','shorts') else 'horizontal' if sns in ('youtube','x') else 'original'
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
        with LOCK:
            JOBS[key]={'token':token,'status':'queued','message':'順番待ち','created':time.time(),'percent':0,
                       'sns':sns,'genre':genre,'style':style,'prompt':request.form.get('prompt','')[:1000],
                       'title':request.form.get('title','').strip()[:80] or incoming[0].filename[:80]}
        start_worker()
        WORK_QUEUE.put((key,paths,aspect))
        return jsonify(id=key,token=token),202
    except Exception as e:
        BUSY.release()
        shutil.rmtree(folder,ignore_errors=True)
        if isinstance(e, HTTPException): raise
        return jsonify(error=str(e) if isinstance(e,ValueError) else '動画の受信に失敗しました。'),400

@app.get('/api/jobs/list')
def list_jobs():
    clean()
    with LOCK:
        entries = sorted(JOBS.items(), key=lambda item:item[1]['created'])
        waiting = 0
        result = []
        for key, job in entries:
            if job['status']=='queued': waiting += 1
            result.append(dict(id=key, token=job['token'], status=job['status'],
                               title=job.get('title','動画編集'), message=job['message'],
                               settings= SNS[job.get('sns','original')]+' ／ '+job.get('genre',GENRES[0])+' ／ '+STYLES[job.get('style','full')],
                               prompt=job.get('prompt',''),
                               percent=job.get('percent',0), queue_position=waiting if job['status']=='queued' else 0,
                               elapsed=int(job.get('finished',time.time())-job['started']) if 'started' in job else 0))
    return jsonify(jobs=list(reversed(result)), max_active=MAX_ACTIVE_JOBS)

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
    return jsonify(status=job['status'],message=job['message'],percent=job.get('percent',0),elapsed=int(job.get('finished',time.time())-job['started']) if 'started' in job else 0)

@app.get('/api/jobs/<key>/video')
def video(key):
    job=authorized(key)
    if not job or job['status']!='done': return jsonify(error='動画が見つかりません。'),404
    return send_file(ROOT/key/'result.mp4',mimetype='video/mp4',as_attachment=request.args.get('download')=='1',download_name='edited-video.mp4',conditional=True)

HTML = '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>動画編集アプリ</title>
<style>body{margin:0;background:#0b0d12;color:#fff;font-family:-apple-system,sans-serif}.w{max-width:720px;margin:auto;padding:24px 16px 60px}.card{background:#171b25;border:1px solid #303747;border-radius:18px;padding:18px;margin:16px 0}h1{font-size:28px}.sub,.note{color:#aeb6c7;line-height:1.6}.step{color:#a994ff;font-weight:bold}.upload,button,.save{display:block;border-radius:12px;padding:18px;text-align:center}input[type=file]{position:absolute;width:1px;height:1px;opacity:0}.upload{border:2px dashed #59637b;cursor:pointer}.file{display:flex;gap:8px;align-items:center;background:#10141c;margin-top:8px;padding:10px;border-radius:10px}.file span{flex:1;overflow-wrap:anywhere}.file button{width:auto;padding:8px;margin:0;background:#303747}select,button{width:100%;box-sizing:border-box;font-size:16px;color:white}select{background:#0f131b;border:1px solid #343c4d;padding:14px;border-radius:12px}button,.save{border:0;background:#7d5cff;color:#fff;font-weight:bold;margin-top:16px;text-decoration:none}button:disabled{opacity:.45}.note{font-size:13px}video{width:100%;max-height:520px;margin-top:16px}.hidden{display:none}label{display:block;margin:12px 0}progress{width:100%}</style>
<div class="w"><form method="post" action="/logout"><input type="hidden" name="csrf" value="__CSRF__"><button>ログアウト</button></form><h1>藤原専用・動画編集アプリ</h1><p class="sub">複数の動画を1本のMP4に。SNS・ジャンル・編集方針を選んで作成できます。</p>
<div class="card"><div class="step">STEP 1</div><h3>動画を選ぶ</h3><label class="upload" for="files">＋ 動画を選択</label><input id="files" type="file" accept="video/*" multiple><label class="upload" for="originalFiles" style="margin-top:12px">＋ 保存済みの動画ファイルを選択</label><input id="originalFiles" type="file" accept=".mov,.mp4,.m4v,.webm,.mkv,.avi" multiple><p class="note">写真からの読み込みが遅い場合は、保存済みの元動画を選べます。選択メニューでは「ファイルを選択」を選んでください。</p><details class="note"><summary>元動画をファイルに保存するには</summary><p>写真アプリで動画を選択 → 共有 →「未編集のオリジナルを書き出す」→「このiPhone内」に保存します。表示されない場合は「ファイルに保存」も使えますが、書き出し時に変換される場合があります。iCloud上の動画はダウンロード時間が必要です。</p></details><p class="note">1〜5本・合計2GB／10分以内。矢印で順番を変えられます。</p><p id="size" class="note" aria-live="polite"></p><div id="list"></div></div>
<div class="card"><div class="step">STEP 2</div><h3>投稿するSNS</h3><select id="sns"><option value="original">指定なし（元の画角）</option><option value="tiktok">TikTok</option><option value="instagram">Instagram Reels</option><option value="shorts">YouTube Shorts</option><option value="x">X</option><option value="youtube">YouTube</option></select></div>
<div class="card"><div class="step">STEP 3</div><h3>ジャンルと編集方針</h3><select id="genre"><option>お笑い・コメディ</option><option>グルメ・料理</option><option>Vlog</option><option>ゲーム</option><option>配信・切り抜き</option><option>音楽</option><option>美容・ファッション</option><option>旅行</option><option>ビジネス</option><option>商品紹介</option></select><select id="style"><option value="full">全編を残す（高速結合優先）</option><option value="reach">再生数・伸び重視</option><option value="tempo">テンポ重視</option><option value="stylish">おしゃれ重視</option><option value="pro">プロっぽく</option><option disabled>AIにおまかせ（準備中）</option></select><p id="policy" class="note"></p><textarea id="prompt" maxlength="1000" placeholder="追加指示をメモ（保存のみ。自動編集にはまだ反映されません）" style="box-sizing:border-box;width:100%;min-height:85px;padding:14px"></textarea><p class="note">現在は固定ルールによる編集です。AIによる見どころ判断・字幕・BGM・再生数の最適化は未対応です。</p></div>
<div class="card"><div class="step">STEP 4</div><label for="title">編集名（任意）</label><input id="title" maxlength="80" placeholder="例：挙式・披露宴・二次会" style="box-sizing:border-box;width:100%;padding:14px;margin-bottom:16px"><h3>完成動画の画角</h3><select id="aspect"><option value="auto">投稿するSNSに合わせる</option><option value="original">元の画角（形式が揃えば高速結合）</option><option value="vertical">縦 9:16（TikTok・Reels・Shorts）</option><option value="horizontal">横 16:9（YouTube・式の記録）</option><option value="square">正方形 1:1</option></select><p class="note">人物が切れないよう、余白を付けて画角を揃えます。元の音声は残します。「全編を残す」で映像形式と指定した画角が揃えば、SNSを選んでも元画質で高速結合します。音声だけが異なる場合は音声を揃えます。画角変更や編集効果が必要な場合は720p相当に変換します。</p><button id="go" disabled>この編集を制作リストに追加</button><p class="note">制作依頼は最大5件。変換中も次の編集を追加できます。変換は受付順に進みます。</p><p class="note">この版では動画の結合と保存ができます。AIによる見どころ選択・自動字幕・BGM追加はまだ入っていません。</p></div>
<div id="out" class="card hidden" aria-live="polite"><h3 id="message"></h3><progress id="progress"></progress><video id="preview" class="hidden" controls playsinline></video><a id="save" class="save hidden">MP4を保存</a><p id="hint" class="note"></p></div><h2>制作リスト</h2><p id="jobsNotice" class="note">読み込み中…</p><div id="jobs"></div></div>
<script>
let selected=[],busy=false;const $=id=>document.getElementById(id);function draw(){const total=selected.reduce((n,f)=>n+f.size,0);$('size').textContent=selected.length?selected.length+'本・合計 '+(total/(1024*1024)).toFixed(1)+' MB（上限 2GB）':''; $('list').replaceChildren();selected.forEach((f,i)=>{const row=document.createElement('div');row.className='file';const name=document.createElement('span');name.textContent=(i+1)+'．'+f.name;row.append(name);for(const [label,delta] of [['↑',-1],['↓',1]]){const b=document.createElement('button');b.textContent=label;b.setAttribute('aria-label',f.name+'を'+(delta<0?'前':'後')+'へ');b.disabled=busy||i+delta<0||i+delta>=selected.length;b.onclick=()=>{[selected[i],selected[i+delta]]=[selected[i+delta],selected[i]];draw()};row.append(b)}$('list').append(row)});$('go').disabled=busy||!selected.length;$('files').disabled=busy;$('originalFiles').disabled=busy;$('aspect').disabled=busy;for(const id of ['sns','genre','style','prompt','title'])$(id).disabled=busy}
function policy(){
 const caps={'お笑い・コメディ':12,'グルメ・料理':8,'Vlog':8,'ゲーム':12,'配信・切り抜き':15,'音楽':20,'美容・ファッション':8,'旅行':8,'ビジネス':15,'商品紹介':10};
 const descriptions={full:'素材を全編残します。映像形式と指定した画角が揃えば元画質で高速結合します。',reach:'各素材の冒頭15秒までを使います。見どころの自動判断は行いません。',tempo:'このジャンルは各素材の冒頭'+caps[$('genre').value]+'秒までを使います。',stylish:'各素材の映像の最初と最後に0.35秒のフェードを付けます。',pro:'720p相当で、通常より画質を優先して変換します。'};
 $('policy').textContent=descriptions[$('style').value];
}
$('genre').onchange=policy;$('style').onchange=policy;policy();
for(const id of ['files','originalFiles'])$(id).onchange=()=>{if(!$(id).files.length)return;selected=[...$(id).files];$(id==='files'?'originalFiles':'files').value='';draw()};const pause=ms=>new Promise(r=>setTimeout(r,ms));

let remainingTimer=null;
function remainingText(seconds){return Math.floor(seconds/60)+'分'+String(seconds%60).padStart(2,'0')+'秒';}
function clearRemaining(){if(remainingTimer!==null){clearInterval(remainingTimer);remainingTimer=null;}}
function showRemaining(prefix,seconds){
 clearRemaining();
 if(seconds===null){$('hint').textContent=prefix+'・残り時間を計算中';return;}
 const target=Date.now()+Math.ceil(seconds)*1000;
 const update=()=>{$('hint').textContent=prefix+'・完了まで約 '+remainingText(Math.max(1,Math.ceil((target-Date.now())/1000)))+'（目安）';};
 update();remainingTimer=setInterval(update,1000);
}
function showOutput(){
 $('out').classList.remove('hidden');$('preview').classList.add('hidden');
 $('preview').removeAttribute('src');$('save').classList.add('hidden');
 $('progress').classList.remove('hidden');$('progress').removeAttribute('value');
 $('out').scrollIntoView({behavior:'smooth'});
}
function updateEstimate(card,job,now){
 // Repeated polls at the same progress must not extend the completion time.
 if(job.percent>=99){card.target=null;return;}
 if(card.lastPercent===undefined||card.lastPercent===null||job.percent<card.lastPercent){
  card.target=null;card.lastPercent=job.percent;card.lastEstimate=0;
 }
 if(job.percent<5||job.elapsed<30)return;
 const advanced=job.percent>card.lastPercent;
 if(card.target===null||card.target===undefined){
  card.target=now+job.elapsed*(100-job.percent)/job.percent*1000;card.lastEstimate=now;card.lastPercent=job.percent;return;
 }
 if(advanced&&now-card.lastEstimate>=15000){
  const measured=now+job.elapsed*(100-job.percent)/job.percent*1000;
  card.target+=.15*(measured-card.target);card.lastEstimate=now;card.lastPercent=job.percent;
 }
}
const cards=new Map();let latestJobs=[];
function updateJobs(jobs){
 latestJobs=jobs;const active=jobs.filter(j=>['queued','processing'].includes(j.status)).length;
 $('jobsNotice').textContent=jobs.length?active+'件を制作中・順番待ち（最大5件）':'まだ制作依頼はありません。';
 for(const job of jobs){
  let card=cards.get(job.id);
  if(!card){
   const el=document.createElement('div');el.className='card';
   const settings=document.createElement('p');settings.className='note';const title=document.createElement('h3'),message=document.createElement('p'),bar=document.createElement('progress'),hint=document.createElement('p'),video=document.createElement('video'),save=document.createElement('a');
   hint.className='note';video.controls=true;video.playsInline=true;video.preload='none';video.className='hidden';save.className='save hidden';save.textContent='MP4を保存';
   el.append(title,settings,message,bar,hint,video,save);card={el,title,settings,message,bar,hint,video,save};cards.set(job.id,card);$('jobs').append(el);
  }
  card.title.textContent=job.title;card.settings.textContent=job.settings+(job.prompt?' ／ メモ：'+job.prompt:'');card.message.textContent=job.status==='queued'?'順番待ち '+job.queue_position+'件目':job.message;
  card.bar.max=100;card.bar.value=job.percent;card.bar.hidden=job.status!=='processing';
  if(job.status==='processing'){
   updateEstimate(card,job,Date.now());
   card.hint.textContent=job.percent>=99?'MP4を仕上げています':Math.floor(job.percent)+'%・残り時間を計算中';
  }else{
   card.target=null;card.lastPercent=null;card.hint.textContent=job.status==='queued'?job.settings+'。先の編集が完成すると自動で始まります。':job.status==='done'?'完成後約1時間保存できます。サーバー再起動時は消えます。':'この編集をもう一度追加してください。';
  }
  if(job.status==='done'&&!card.video.getAttribute('src')){
   const url='/api/jobs/'+job.id+'/video?token='+encodeURIComponent(job.token);
   card.video.src=url;card.video.classList.remove('hidden');card.save.href=url+'&download=1';card.save.download='edited-video.mp4';card.save.classList.remove('hidden');
  }

 }
 for(const [id,card] of cards){if(!jobs.some(j=>j.id===id)){card.el.remove();cards.delete(id);}}
}
setInterval(()=>{for(const card of cards.values()){if(card.target!==null&&card.target!==undefined)card.hint.textContent=(card.target>Date.now()?'完了まで約 '+remainingText(Math.ceil((card.target-Date.now())/1000))+'（目安）':'推定を更新中。処理は継続しています');}},1000);
async function refreshJobs(){
 const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),15000);
 let response;try{response=await fetch('/api/jobs/list',{signal:controller.signal});}finally{clearTimeout(timeout);}
 if(!response.ok)throw Error(response.status===401?'ログインが必要です。ページを開き直してください。':'制作リストを取得できません。');
 updateJobs((await response.json()).jobs);
}
async function pollJobs(){
 try{await refreshJobs();}catch(error){$('jobsNotice').textContent=error.message+' 通信が戻ると再確認します。';}
 setTimeout(pollJobs,2000);
}
function sendVideos(body){return new Promise((resolve,reject)=>{
 const uploadStarted=Date.now();
 const xhr=new XMLHttpRequest();xhr.open('POST','/api/jobs');xhr.setRequestHeader('X-CSRF-Token','__CSRF__');
 xhr.timeout=30*60*1000;
 xhr.upload.onprogress=event=>{
  if(event.lengthComputable){const percent=Math.round(event.loaded/event.total*100);$('progress').max=100;$('progress').value=percent;$('message').textContent='動画を送信しています '+percent+'%';const elapsed=(Date.now()-uploadStarted)/1000;showRemaining('送信中',event.loaded>0&&elapsed>=3?elapsed*(event.total-event.loaded)/event.loaded:null);}
 };
 xhr.upload.onload=()=>{clearRemaining();$('hint').textContent='サーバーの応答を待っています';$('message').textContent='サーバーで動画の受信を確認しています';};
 xhr.onerror=()=>reject(Error('送信中に通信が切れました。ページを開き直して処理状況を確認してください。'));
 xhr.ontimeout=()=>reject(Error('送信に30分以上かかりました。通信環境を確認してください。'));
 xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);if(xhr.status<200||xhr.status>=300)reject(Error(data.error||'送信に失敗しました'));else resolve(data);}catch(e){reject(Error('サーバーから正常な応答がありません。ページを開き直して処理状況を確認してください。'));}};
 xhr.send(body);
});}
async function task(work){
 busy=true;draw();showOutput();
 try{await work();}catch(e){$('message').textContent=e.message;$('hint').textContent='再送信する前に、ページを開き直すと前の処理状況を確認できます。';}
 finally{clearRemaining();busy=false;$('progress').classList.add('hidden');draw();}
}
$('go').onclick=()=>task(async()=>{
 await refreshJobs();
 if(latestJobs.filter(j=>['queued','processing'].includes(j.status)).length>=5)throw Error('制作依頼は同時に5件までです。完成後に追加できます。');
 const total=selected.reduce((n,f)=>n+f.size,0);
 if(!selected.length||selected.length>5||total>2*1024*1024*1024)throw Error('素材は1〜5本、合計2GB以内で選んでください。');
 const body=new FormData();selected.forEach(f=>body.append('videos',f));body.append('aspect',$('aspect').value);body.append('title',$('title').value);for(const id of ['sns','genre','style','prompt'])body.append(id,$(id).value);
 $('message').textContent='動画を送信しています 0%';$('hint').textContent='送信が終わるまでSafariを開いたままお待ちください。';
 await sendVideos(body);selected=[];$('files').value='';$('originalFiles').value='';$('title').value='';
 clearRemaining();$('message').textContent='制作リストに追加しました';$('hint').textContent='次の編集の動画を選んで追加できます。';
 await refreshJobs().catch(()=>{$('jobsNotice').textContent='受付済み。制作リストは通信が戻ると更新します。再送信は不要です。';});
});
draw();pollJobs();
</script></html>'''
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',10000)))

# 動画結合・MP4出力版

app.py と Dockerfile を、既存の GitHub リポジトリ -ai-editor の一番上へ追加してください。既存 index.html はそのままで構いません。

Render の New → Web Service で同じリポジトリを選択し、Language に Docker、Branch に main、Dockerfile Path に ./Dockerfile を指定します。無料プランが選べる場合は Free を選択し、Deploy Web Service を押します。Build Command・Start Command の手入力は不要です。デプロイ成功後、新しい Web Service の URL でアプリを開きます。今までの Static Site の URL は旧版です。

## 実装済み
- MOV／MP4などFFmpegが読み込める動画を1〜5本結合
- 順番変更、画角統一、音声維持、音声のない動画にも対応
- MP4のプレビュー・ダウンロード
- 合計200MB／10分以内、同時処理1件、完成後約1時間の一時保存

AIによる内容分析、字幕、BGM、自動カットは未実装です。「最初の動画に合わせる」は向きに応じて9:16、16:9、1:1のいずれかを選びます。画角が違う素材は黒い余白を付けて収めます。動画はサーバーへ送信され、処理後は元の素材を削除します。

Renderの再起動・再デプロイ時は一時動画が消えます。無料サーバーでは処理に時間がかかる場合があります。個人で試すための実装で、複数人向けのアカウント管理・永続保存はありません。

ローカル起動: Python 3.12、Flask 3.1.2、FFmpegを入れて python app.py。Dockerの場合は docker build -t video-editor . && docker run -p 10000:10000 video-editor。

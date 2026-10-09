# リリースの手順と署名

Worker (Linux の install.sh・固定の更新スクリプト、Windows / macOS の GUI) は、管理者の Ed25519 鍵で署名されたリリースにしか更新しません。CI はリリースを **draft** で作るだけで、署名と公開は管理者が自分の端末で行います。秘密鍵は CI (GitHub) にもリポジトリにも置きません。

仕組みは [architecture.md の「更新物の署名」](architecture.md#更新物の署名) を参照。

## 必要なもの (署名する端末)

- OpenSSL 1.1.1 以上 (`openssl version`)
- [gh](https://cli.github.com/) (このリポジトリにリリースを書ける権限で `gh auth login` 済み)
- このリポジトリの clone (タグを `git fetch --tags` 済み)
- 署名鍵 `~/.config/swim-release/signing-key.pem` (パスフレーズ付き。下の「鍵を作る」)

## リリースの流れ

1. `swim_worker/__init__.py` の `__version__` を上げてコミットし、master に push
2. タグを打って push する (CI がタグと `__version__` の一致を確かめる)

   ```bash
   git tag v1.3.0
   git push origin v1.3.0
   ```

3. CI (`Build Worker Executables`) を待つ。公開鍵の埋め込みの確認 → テスト → 4 OS のビルド → **draft** のリリース (全ファイル + `SHA256SUMS`) の順に進む。公開鍵が未設定・テスト失敗・成果物の欠けがあれば draft はできない

   ```bash
   gh run watch -R Meku-30/swim-worker
   ```

4. 署名して公開する

   ```bash
   git fetch --tags
   scripts/sign-release.sh v1.3.0
   ```

   スクリプトは次を確かめてから署名します。どれかが合わなければ何も上げずに止まります。
   - draft であること (公開済みには署名し直さない)・まだ `SHA256SUMS.sig` が無いこと
   - `SHA256SUMS` の先頭行が `# swim-worker-release v1.3.0` であること
   - 全ファイルのハッシュが一致し、足りないファイル・`SHA256SUMS` に無いファイルが無いこと
   - `install.sh`・`swim-worker-update.sh`・unit がリポジトリのタグの中身と同じであること

   `SHA256SUMS` が表示され、openssl が署名鍵のパスフレーズを聞きます。署名はリリースの install.sh・更新スクリプトに埋め込まれた公開鍵で確かめてから `SHA256SUMS.sig` として上げ、上がったものが同じか確かめてから、公開してよいか聞きます (`y` で公開)。

   - 公開せずに署名だけ上げる: `scripts/sign-release.sh --no-publish v1.3.0`。後で `gh release edit v1.3.0 -R Meku-30/swim-worker --draft=false`
   - 公開すると Linux Worker は次の更新チェック (6 時間ごと) で、GUI は次の案内で更新します。管理者の一時停止・段階配布の設定はそのまま効きます

5. (任意) 公開後に README の「リリースの署名を自分で確かめる」の手順で確かめる

`-` を含むタグ (`v1.3.0-rc1` など) は prerelease になり、自動更新の対象 (最新 stable) になりません。

## 鍵を作る (最初の 1 回)

署名する端末で実行します。パスフレーズは openssl が 2 回聞きます (忘れると署名できません。パスワードマネージャー等に保管)。

```bash
mkdir -p ~/.config/swim-release
chmod 700 ~/.config/swim-release
openssl genpkey -algorithm ed25519 -aes-256-cbc -out ~/.config/swim-release/signing-key.pem
chmod 600 ~/.config/swim-release/signing-key.pem
openssl pkey -in ~/.config/swim-release/signing-key.pem -pubout -out ~/.config/swim-release/signing-key.pub.pem
```

秘密鍵 (`signing-key.pem`) は暗号化したバックアップを別の場所 (オフラインの媒体など) にも置いておくことを推奨します。失くすと下の「鍵を失くしたとき」の手作業が要ります。

## 公開鍵を埋め込む

```bash
scripts/set-release-pubkeys.sh ~/.config/swim-release/signing-key.pub.pem
git diff          # 3 か所 (release_keys.py・install.sh・swim-worker-update.sh) と scripts/release_pubkeys/ を確認
git add scripts/release_pubkeys/ swim_worker/release_keys.py scripts/install.sh scripts/swim-worker-update.sh
git commit -m "リリースの公開鍵を埋め込む"
```

スクリプトは Ed25519 の公開鍵だけを受け付け (秘密鍵を渡すと止まる)、埋め込んだ鍵の指紋 (DER の SHA-256) を表示します。手元の鍵の指紋は次で確かめられます。

```bash
openssl pkey -pubin -in ~/.config/swim-release/signing-key.pub.pem -outform DER | sha256sum
```

埋め込みが元のファイルと一致するかは `python3 scripts/release_pubkeys.py check` (テストでも確かめる)、公開鍵が未設定ならリリース CI が `check --require` で失敗します。

## 鍵を入れ替える (鍵が手元にあるうち)

Worker は今入っている版に埋め込まれた公開鍵でしか検証しないので、2 段階で入れ替えます。

1. 新しい鍵を作り (上の「鍵を作る」を別のファイル名で)、旧・新の 2 本を埋め込む

   ```bash
   scripts/set-release-pubkeys.sh ~/.config/swim-release/signing-key.pub.pem ~/.config/swim-release/new-signing-key.pub.pem
   ```

   この版を **旧鍵** で署名して公開する。全 Worker がこの版に上がるのを待つ (管理画面の版の分布で確認)
2. 新しい鍵 1 本だけを埋め込み、次の版から **新鍵** で署名する (`SWIM_RELEASE_KEY=<新しい秘密鍵> scripts/sign-release.sh <タグ>`)。旧鍵は破棄してよい

## 鍵を失くしたとき・漏れたとき

予備の鍵は持たない運用なので、署名鍵を失くした (パスフレーズを忘れた) ときは、既存の Worker が受け付けるリリースをもう作れません。

1. 新しい鍵を作り、新しい公開鍵 1 本だけを埋め込んだ版を出す (新しい鍵で署名)
2. 既存の Worker は自動更新ではこの版を受け付けない (署名が合わない) ので、**全台で手作業の入れ直し** が要る
   - Linux: 各 Worker で新しい版の `install.sh` を取り (README の「リリースの署名を自分で確かめる」で新しい公開鍵で確かめてから) `sudo bash install.sh`。設定 (`.env`) はそのまま残る
   - Windows / macOS: 新しい版の exe / アプリをダウンロードし直して置き換える。設定はそのまま残る
3. 利用者に連絡する (自動更新が止まっている旨と手順)

秘密鍵が **漏れた** 疑いがあるときも同じ手順で鍵を替えます。漏れた鍵で署名された版を Worker が受け付けてしまうのは、その鍵が埋め込まれた版が動いている間です。入れ替えが済むまでは管理者の一時停止 (kill switch) で自動更新を止めておきます。

## 推奨: GitHub の設定

リポジトリの設定 (このリポジトリのコードでは変えない) として次を推奨します。

- タグの保護 (ruleset): `v*` タグを作成・削除・更新できるのを管理者だけにする
- master の保護: force push の禁止
- Actions: 「Allow GitHub Actions to create and approve pull requests」は OFF、既定の `GITHUB_TOKEN` の権限は read
- Releases: (使えるなら) immutable releases を有効にする

## 依存のロック

CI と Docker は `requirements.lock`・`requirements-dev.lock` (ハッシュ付き) から `pip install --require-hashes` で入れます。`requirements*.txt` を変えたら `scripts/lock-deps.sh` (uv が要る) で作り直してコミットします。制約の範囲で最新に上げるときは `scripts/lock-deps.sh --upgrade` (上げる前にリリースノートを確認)。

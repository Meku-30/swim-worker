# install.sh の試運転 (使い捨てのコンテナ)

Rust 版の Worker を、本番と同じ `install.sh`・systemd の unit (制限つき) で入れて動かし、更新と巻き戻しまで確かめる道具。
SWIM には繋がない (試運転用のビルド `--features trial` は SWIM の代わりに偽物を使う)。

## 何を確かめるか (`inner.sh`)

| 場面 | 確かめること |
|---|---|
| A 新しく入れる | `install.sh` が通り、120 秒以内に `data/.startup_ok`、unit の制限 (`MemoryDenyWriteExecute` など) で止められず、偽の Coordinator に結果が届く |
| B Redis が遅れて起きる | Redis を止めて Worker を再起動し、20 秒後に Redis を起こしても 120 秒以内に起動の印が出る |
| C 更新して成功 | `install.sh --auto` で次の版になり、`.failed-version` がない |
| D 更新して失敗 | すぐ終わる版に更新すると、前の版・`.version`・unit に戻り、`.failed-version` に記録される |
| E Redis が 60 秒止まる | 止まったあとも Worker が動いたまま、結果が再び届く |
| F 権限の一部が欠ける | 承認の一覧を読めない利用者で、Worker がどう振る舞うか (ログを見る) |

## 仕組み

- `install.sh`・更新スクリプトの**写し**の 3 か所 (埋め込みの公開鍵・最新版の URL・ダウンロード元) だけを `patch_install.sh` で差し替え、
  コンテナの中の HTTPS の置き場 (`serve.py`、使い捨ての CA をコンテナに信頼させる) から取らせる。署名・ハッシュ・https の確かめはそのまま。配布する `install.sh` には手を入れない
- 置き場は `make_release.sh` がリリースと同じ形 (`SHA256SUMS` の先頭行・Ed25519 の署名) で作る。鍵は使い捨て
- Redis (TLS・ACL) は別のコンテナで、systemd のコンテナとネットワークを共有し 127.0.0.1 だけで話す
- systemd をコンテナの中で動かすため `--privileged` を使う。外に口は開けない。終わるとコンテナ・作ったイメージを消す

## 動かし方

docker を使える Linux の機で:

```bash
mkdir work && cp tools/install_trial/* work/ && cp -r scripts work/scripts.orig && cp tools/fake_coordinator.py work/
# 試運転用のビルド (amd64): cargo zigbuild --release -p swim-worker --bin swim-worker --features trial --target x86_64-unknown-linux-gnu.2.31
bash work/run.sh work ./swim-worker-trial     # sudo が要る機は DOCKER="sudo docker"
```

コンテナの中で root が書いたファイルが作業フォルダに残るので、終わったら作業フォルダごと消す。

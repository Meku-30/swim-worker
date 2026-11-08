# GUI の試作 (使い捨て、M1)

今の GUI (tkinter、`swim_worker/gui.py`) と同じ部品だけを並べた試作 2 つ。ボタンは何もしない。ログ欄は 1 秒に 1 行足し、1,000 行を超えたら 800 行に減らす (今の GUI と同じ)。Windows の runner で待機中のメモリを測り、軽いほうを選ぶ (`.github/workflows/rust-m1-windows.yml`)。M2 で本物を作ったら消す。

- `gui-native/`: Windows の標準の部品 (native-windows-gui)
- `gui-slint/`: Slint (ソフトウェア描画) + tray-icon

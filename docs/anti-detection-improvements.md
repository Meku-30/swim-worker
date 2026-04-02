# bot検知回避 改善調査レポート (2026-04-02)

## 1. SPA初期化POSTのリクエストbody問題

### 現状

`auth.py:327` で全てのSPA初期化POSTが `json={}` で送信されている。

```python
if method == "POST":
    await self._session.post(url, json={}, headers=ref_header)
```

### 検証方法

2026-04-02に Playwright (Chromium) で実際のSWIMポータルにログインし、各サービスページを開いた際のネットワークリクエスト（POST body含む）をキャプチャした。

キャプチャは全3回:

| 回 | 日時 | ファイル | 備考 |
|----|------|---------|------|
| 1 | 2026-03-04 11:20 | `swim-api-legacy/.../71_all_data_api_calls.json` | bodyキャプチャなし |
| 2 | 2026-03-04 11:27 | `swim-api-legacy/.../94_all_api_calls.json` | bodyキャプチャなし |
| 3 | **2026-04-02** | `/tmp/swim_capture_20260402/` | **body含む全データ** |

### f2aspr SPA初期化の順序比較 (3回のキャプチャ)

Workerの `_SPA_INIT_REQUESTS` は94_ (2回目) の順序に基づいているが、3回のキャプチャで順序が異なる。

```
=== Worker現在の実装 (94_ベース) ===
 1. POST LuciadRIALicense
 2. GET  ATCMAP.settings
 3. GET  auto_filter.json
 4. POST FLV901/LGV300        ← ここの位置が可変
 5. POST FLV811/LGV231        ← ここの位置が可変
 6. GET  map_disp.json
 7. GET  resource/message
 8. GET  resource/webfw
 9. GET  resource/user
    [browse GETs ×3]
10. POST FLV802/LGV205
11. POST FLV934/LGV387
12-23. GET settings/*.json (velocity〜groupLayer)

=== 2026-04-02 キャプチャ (実測) ===
 1. POST LuciadRIALicense
 2. GET  ATCMAP.settings
 3. GET  auto_filter.json
 4. GET  map_disp.json          ← FLV901/FLV811 はここにない
 5. GET  resource/message
 6. GET  resource/webfw
 7. GET  resource/user
 8. POST FLV901/LGV300          ← resource/userの後に移動
 9. POST FLV811/LGV231          ← resource/userの後に移動
10-21. GET settings/*.json (velocity〜groupLayer)
    [browse GETs: velocity後に1回、default_color後に2回 — 分散]
22. POST FLV802/LGV205
23. POST FLV934/LGV387
24-35. POST FLV921/909/913×2/914/915/916/918/919/920/921/807  ← 追加マップレイヤーAPI
36-39. GET UTM0〜UTM3.json

=== 71_ (2026-03-04 1回目) ===
 1. GET  ATCMAP.settings         ← LuciadRIAとATCMAPの順序が逆
 2. POST LuciadRIALicense
 3. GET  auto_filter.json
 4. GET  map_disp.json
 5-7. GET resource/*
    [browse GETs: resource/user後に3回]
 8-19. GET settings/*.json
20. POST FLV901/LGV300           ← groupLayerの後
```

**安定している部分:**
- LuciadRIALicense と ATCMAP.settings は常に最初（前後関係は可変）
- auto_filter.json は常にその直後
- resource/message → webfw → user の順序は固定
- settings/*.json (velocity〜groupLayer) の順序は固定
- UTM*.json は常に最後

**可変な部分:**
- LuciadRIA と ATCMAP の前後関係
- FLV901/FLV811 の出現位置（auto_filter後 or resource/user後 or groupLayer後）
- browse GETs の分散位置（resource/user後に集中 or settings内に分散）
- FLV802/FLV934 の出現位置（resource/user後 or groupLayer後）
- 追加マップレイヤーAPI POST の有無と数

### f2dnrq SPA初期化の順序比較

```
=== Worker現在の実装 (94_ベース) ===
 1. POST LuciadRIALicense
 2. GET  ATCMAP.settings
 3. GET  auto_filter.json
 4. POST FUV201/USV005           ← 1回目
 5. GET  map_disp.json
 6-8. GET resource/*
 9-20. GET settings/*.json
21. POST FUV201/USV005           ← 2回目
22-25. GET UTM*.json

=== 2026-04-02 キャプチャ (実測) ===
 1. POST LuciadRIALicense
 2. GET  ATCMAP.settings
 3. GET  auto_filter.json
 4. GET  map_disp.json            ← FUV201/USV005 はここにない
 5-7. GET resource/*
 8. POST FUV201/USV005            ← resource/userの後 (1回のみ)
 9-20. GET settings/*.json
21-24. GET UTM*.json              ← 2回目のUSV005なし
```

**変化点:** FUV201/USV005 の位置が auto_filter後 → resource/user後に移動。2回目のUSV005がなくなった。

### SPA初期化POST — 実際のリクエストbody (2026-04-02キャプチャ)

| URL | 実body | Worker現状 | 差異 |
|-----|--------|-----------|------|
| `LuciadRIALicense` | body なし (Playwrightで空) | `json={}` | **差異あり** (bodyを送らないべき) |
| `web/FLV901/LGV300` | `_BASIC_BODY` | `json={}` | **差異あり** |
| `web/FLV811/LGV231` | `_BASIC_BODY` + `"profileType": 0, "lang": "ja"` | `json={}` | **差異あり** |
| `web/FLV802/LGV205` | `_BASIC_BODY` + `"profileType": 0` | `json={}` | **差異あり** |
| `web/FLV934/LGV387` | `_BASIC_BODY` | `json={}` | **差異あり** |
| `web/FUV201/USV005` | `_BASIC_BODY` | `json={}` | **差異あり** |

`_BASIC_BODY`:
```json
{"msgHeader":{"jnlInfo":{"jnlRegistFlag":0},"tsusuInfo":{}},"ctrlInfo":{},"ctrlHeader":{}}
```

### 追加マップレイヤーAPI (f2aspr、groupLayer後に自動発生)

今回のキャプチャで初めてbody含みで確認できた、SPA初期化後に自動発生するAPIリクエスト群。Workerの `_SPA_INIT_REQUESTS` には含まれていない。

| URL | body |
|-----|------|
| `web/FLV921/LGV359` | `_BASIC_BODY` |
| `web/FLV909/LGV330` | `_BASIC_BODY` |
| `web/FLV913/LGV350` | `_BASIC_BODY` |
| `web/FLV913/LGV351` | `_BASIC_BODY` |
| `web/FLV914/LGV352` | `_BASIC_BODY` + `"layerName": "Aerodome_Weather_Status"` |
| `web/FLV915/LGV353` | `_BASIC_BODY` |
| `web/FLV916/LGV354` | `_BASIC_BODY` |
| `web/FLV918/LGV356` | `_BASIC_BODY` + `"layerName": "SIGMET"` |
| `web/FLV919/LGV357` | `_BASIC_BODY` + `"layerNameList": []` |
| `web/FLV920/LGV358` | `_BASIC_BODY` + `"layerNameList": ["PIREP_SMTH","PIREP_LGTM","PIREP_LGT","PIREP_LGTP","PIREP_MOD","PIREP_MODP","PIREP_SEV","PIREP_EXT","PIREP_ARS","Volcanic_Ash","WS","CLOUD","ICE","TS"]` |
| `web/FLV921/LGV359` | `_BASIC_BODY` (2回目) |
| `web/FLV807/LGV226` | `_BASIC_BODY` + `"callerKind": 0, "location": "", "notamExtractPreiodFrom": "", "notamExtractPreiodTo": "YYYYMMDDHHmm", "notamCd23": [50種のNOTAMコード]` |

これらはマップ上のレイヤーデータ取得API。Workerでは再現していないが、実ブラウザでは毎回発生する。再現するかどうかは検討事項。

### データ収集APIのbody比較

今回のキャプチャでNOTAM検索 (USV001) のbodyも確認できた。

| API | Coordinator body | キャプチャ上の実body | 一致 |
|-----|-----------------|-------------------|------|
| 空港一覧 (FUV201/USV005) | `_BASIC_BODY` | `_BASIC_BODY` | **OK** |
| NOTAM検索 (FUV201/USV001) | `_BASIC_BODY` + notam検索パラメータ | 同じ構造 | **OK** |
| PIREP (FLV920/LGV358) | `_BASIC_BODY` + layerNameList 14種 | 同じ14種 | **OK** |
| 空域気象状態 (FLV914/LGV352) | `_BASIC_BODY` + layerName | 同じ | **OK** |
| SIGMET (FLV918/LGV356) | `_BASIC_BODY` + layerName | 同じ | **OK** |

PKG気象 (FLV904)、フライト一覧/詳細 (FLV803/FLV911)、空港プロファイル (FLV806) は今回のキャプチャでもUI操作に至らなかったため未確認。ただし `_BASIC_BODY` の構造は全API共通であり、これらも問題ない可能性が高い。

### 修正方針

| URL | 送信すべきbody |
|-----|---------------|
| `LuciadRIALicense` | body なし (`data=b""` or 空POST) |
| `web/FLV901/LGV300` | `_BASIC_BODY` |
| `web/FLV811/LGV231` | `_BASIC_BODY` + `"profileType": 0, "lang": "ja"` |
| `web/FLV802/LGV205` | `_BASIC_BODY` + `"profileType": 0` |
| `web/FLV934/LGV387` | `_BASIC_BODY` |
| `web/FUV201/USV005` | `_BASIC_BODY` |

---

## 2. リクエスト間隔の分布問題

### 現状

```python
# consumer.py:61 — タスク実行前の遅延
delay = random.uniform(self._request_delay_min, self._request_delay_max)
# デフォルト: uniform(2.0, 8.0)

# auth.py:400 — レスポンス後の遅延（DOM処理シミュレーション）
await asyncio.sleep(random.expovariate(3.0) + 0.05)
```

### 均一分布の統計的問題

| 統計量 | uniform(2, 8) の値 |
|--------|-------------------|
| 平均 / 中央値 | 5.00秒 / 5.00秒 |
| 標準偏差 | 1.73秒 |
| 歪度 | 0.0 (完全対称) |
| P1-P99 | 2.06秒 - 7.94秒 |

**問題点:**

1. **歪度ゼロ**: 人間のブラウジングは正の歪度を持つ（短い間隔が多く、稀に長い間隔）。均一分布の完全対称性は不自然
2. **テールなし**: 8秒を絶対に超えない。人間は時々10-20秒考え込んだり離席したりする
3. **全区間が等確率**: 2秒待つ確率 = 7秒待つ確率。人間は「典型的な間隔」の周辺に集中する
4. **Bot検知の文献で均一分布は明示的に検知対象とされている** (Gianvecchio et al., 2008)

### 学術的根拠

- **Blenn & Van Mieghem (2016)** "[Are human interactivity times lognormal?](https://arxiv.org/abs/1607.02952)": 人間のインタラクション時間は**対数正規分布**が最もよくフィットする
- **Downey (2005)** "[Lognormal and Pareto distributions in the Internet](https://allendowney.com/research/longtail/downey04lognormal.pdf)": HTTPリクエストのinter-arrival time は P99以下で対数正規分布が最良
- **Gianvecchio et al. (2008)** "[Measurement and Classification of Humans and Bots in Internet Chat](https://www.usenix.org/legacy/event/sec08/tech/full_papers/gianvecchio/gianvecchio_html/)" USENIX Security: 均一分布でランダム化したBotも条件付きエントロピー (CCE) 分析で検知可能

### 分布の比較

| 分布 | 人間との一致度 | 特徴 |
|------|------------|------|
| **対数正規** | **最高** | 右裾が重い、正の歪度。人間のインタラクション時間の実証研究で最も支持 |
| ガンマ | 高い | 対数正規ほどテールが重くない |
| ワイブル | 中程度 | Webブラウジングへの実証根拠が薄い |
| パレート | 中程度 | テール部分は良いがバルクのフィットが悪い |
| 指数 | 低い | メモリレス性。人間の行動はメモリレスではない |
| **均一 (現状)** | **最低** | 完全対称、テールなし。Bot検知で明示的な検知対象 |

### 推薦: 対数正規分布

#### パラメータ

| パラメータ | 値 | 根拠 |
|-----------|-----|------|
| mu | ln(4.0) = 1.386 | 中央値4秒。業務系Webアプリの典型的操作間隔 |
| sigma | 0.568 | P99 = 15秒になるよう逆算: `(ln(15) - ln(4)) / 2.326` |
| clip_min | 1.5秒 | ネットワーク遅延+最低限の認知時間 |
| clip_max | 25.0秒 | 異常に長い待機を防止 |

#### 推薦パラメータの統計的特性

| 統計量 | 値 |
|--------|-----|
| 平均 | 4.70秒 |
| **中央値** | **4.00秒** |
| 最頻値 (mode) | 2.90秒 |
| 標準偏差 | 2.90秒 |
| 歪度 | +2.03 (人間的) |
| P5 | 1.57秒 |
| P25 | 2.73秒 |
| P75 | 5.86秒 |
| P95 | 10.17秒 |
| **P99** | **14.98秒** |

#### 実装コード

```python
import math
import random

def human_like_delay(median: float = 4.0, p99: float = 15.0,
                     clip_min: float = 1.5, clip_max: float = 25.0) -> float:
    """対数正規分布に基づく人間的なリクエスト間遅延"""
    mu = math.log(median)
    sigma = (math.log(p99) - mu) / 2.326  # 2.326 = norm.ppf(0.99)
    delay = random.lognormvariate(mu, sigma)
    return max(clip_min, min(clip_max, delay))
```

追加依存なし（Python標準ライブラリの `random.lognormvariate` を使用）。

#### auth.py のレスポンス後遅延

`expovariate(3.0) + 0.05` はDOMレンダリング時間のシミュレーションとして妥当。**変更不要**。

---

## 3. 優先順位

| 項目 | 影響 | コスト | 優先度 |
|------|------|--------|--------|
| SPA初期化POSTに正しいbodyを設定 | 高 | 低（auth.pyのPOST body定義追加） | **1** |
| リクエスト間隔を対数正規分布に変更 | 中-高 | 低（consumer.pyの数行） | **2** |
| SPA初期化の順序を最新キャプチャに合わせる | 中 | 中（順序が毎回変わるため完全一致は不可能） | **3** |
| 追加マップレイヤーAPIの再現 | 低 | 高（12件のAPI追加、パラメータ管理） | 4 |
| SPA初期化リクエスト間にフェーズブレイク追加 | 低 | 低 | 5 |

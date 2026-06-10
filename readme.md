# JobStreamExpress

An automated job-hunting pipeline that crawls job listings, scores them against your profile using a local LLM, and generates tailored cover letters and interview advice.

## How it works

1. **Crawl** — `auto_crawler.py` scrapes Seek search results, cleaning and deduplicating job listings.
2. **Analyse** — Each job is sent to a local Ollama LLM which extracts skills, scores the match against your profile, and grades the report (`strong_match / possible / no_match`).
3. **Advise** — `advisor.py` reads a saved report and produces job-specific interview tips and (optionally) a full cover letter.

---

## Setup

1. Install [Ollama](https://ollama.com) and pull a model (default: `gemma4:12b`).
2. Install Python dependencies:
   ```
   pip install playwright requests
   playwright install chromium
   ```
3. Edit `config/profile.json` with your skills, interests, and projects.
4. Edit `config/llm_config.txt` to set your model name and Ollama URL if different from defaults.

---

## Usage

### auto_crawler.py

Crawls a Seek search URL and runs every listing through the LLM pipeline.

```
python auto_crawler.py <url> [options]

  url              Seek search URL to crawl (required)
  --pages N        Max pages to crawl (default: 10)
  --delay N        Base delay multiplier between requests (default: 2.0)
  --headless       Run browser headless (no window)
```

Example:
```
python auto_crawler.py "https://au.seek.com/jobs?keywords=IT+Support&location=Melbourne" --pages 3 --delay 4
```

Reports are saved automatically to:
- `reports/strong_match/` — score ≥ 75%
- `reports/possible/` — score ≥ 50%
- `reports/no_match/` — everything else (skipped on re-crawl)

---

### advisor.py

Reads a saved report and prints job-specific interview advice. Pass `--cover` to also generate a cover letter saved to `output/`.

```
python advisor.py <report(s)> [options]

  reports          One or more .txt report file paths (required)
                   Supports globs: reports/strong_match/*.txt
  --cover          Also generate a cover letter for each report
  --platform NAME  Platform name used in cover letter opening (default: Seek)
```

Examples:
```
python advisor.py reports/strong_match/91995152.txt
python advisor.py reports/strong_match/91995152.txt --cover
python advisor.py reports/strong_match/91995152.txt --cover --platform LinkedIn
python advisor.py reports/strong_match/*.txt --cover
```

---

## Config files

| File | Purpose |
|------|---------|
| `config/profile.json` | Your skills, interests, and projects |
| `config/llm_config.txt` | Ollama model name, URL, token limits |
| `config/skill_aliases.json` | Canonical skill names and aliases for normalisation |
| `prompts/analyse.txt` | LLM prompt for English job analysis |
| `prompts/analyse_ja.txt` | LLM prompt for Japanese job analysis |
| `prompts/cover_p1–p4.txt` | Cover letter generation prompts (multi-part) |
| `avoid_keywords.txt` | Keywords that mark a listing as irrelevant |

---

## What's New

### 10 Jun 2026
- **Japanese job parsing** — jobs posted in Japanese are now detected and routed through a dedicated `analyse_ja.txt` prompt; extracted fields and skill normalisation work the same as English listings.
- **Hidden info parsing fix** — job details that were previously missed (obfuscated or lazily-loaded fields) are now captured correctly by `job_detector.py` and `job_cleaner.py`.
- **Expanded skill aliases** — `skill_aliases.json` grew from ~36 to ~500+ entries, covering more tech stacks, frameworks, and role titles for better match scoring.
- **Library fixes** — minor stability improvements to `job_analyser.py`.

### 8 Jun 2026
- Auto-navigation between pages during crawl.
- LLM-based cover letter writer and interview suggestion generator (`advisor.py`).

### 6 Jun 2026
- Skill normalisation via alias map.
- Score-based decision making (strong match / possible / no match grading).
- Report storage with grade separation.
- Input queue with LLM concurrency control.

### 5 Jun 2026
- LLM summary and user profile matching pipeline.

### 4 Jun 2026
- Initial port from old Job Stream repo.

---

---

# JobStreamExpress（日本語）

Seekの求人情報を自動収集し、ローカルLLMを使ってあなたのプロフィールとマッチングを行い、カバーレターと面接アドバイスを生成する自動求職パイプラインです。

## 仕組み

1. **クロール** — `auto_crawler.py` がSeekの検索結果をスクレイピングし、求人情報を収集・重複排除します。
2. **分析** — 各求人をローカルのOllama LLMに送信し、スキルを抽出してプロフィールとのマッチスコアを算出、レポートを評価します（`strong_match / possible / no_match`）。
3. **アドバイス** — `advisor.py` が保存済みレポートを読み込み、求人ごとの面接対策と（オプションで）カバーレターを生成します。

---

## セットアップ

1. [Ollama](https://ollama.com) をインストールし、モデルをダウンロードします（デフォルト: `gemma3:12b`）。
2. Python依存パッケージをインストールします:
   ```
   pip install playwright requests
   playwright install chromium
   ```
3. `config/profile.json` に自分のスキル・興味・プロジェクトを記入します。
4. デフォルト以外のモデル名やOllama URLを使う場合は `config/llm_config.txt` を編集します。

---

## 使い方

### auto_crawler.py

ネットでURL検索結果をクロールし、すべての求人をLLMパイプラインで処理します。

```
python auto_crawler.py <url> [オプション]

  url              クロールするSeekの検索URL（必須）
  --pages N        最大クロールページ数（デフォルト: 10）
  --delay N        リクエスト間の基本待機時間の倍率（デフォルト: 2.0）
  --headless       ブラウザをヘッドレスモードで実行（ウィンドウ非表示）
```

実行例:
```
python auto_crawler.py "https://au.seek.com/jobs?keywords=IT+Support&location=Melbourne" --pages 3 --delay 4
```

レポートは自動的に以下のディレクトリに保存されます:
- `reports/strong_match/` — スコア ≥ 75%
- `reports/possible/` — スコア ≥ 50%
- `reports/no_match/` — それ以外（再クロール時はスキップ）

---

### advisor.py

保存済みレポートを読み込み、求人ごとの面接アドバイスを表示します。`--cover` を指定するとカバーレターも生成し `output/` に保存します。

```
python advisor.py <レポートファイル> [オプション]

  reports          1つ以上の .txt レポートファイルパス（必須）
                   グロブ対応: reports/strong_match/*.txt
  --cover          各レポートのカバーレターも生成する
  --platform NAME  カバーレターの冒頭で使うプラットフォーム名（デフォルト: Seek）
```

実行例:
```
python advisor.py reports/strong_match/91995152.txt
python advisor.py reports/strong_match/91995152.txt --cover
python advisor.py reports/strong_match/91995152.txt --cover --platform LinkedIn
python advisor.py reports/strong_match/*.txt --cover
```

---

## 設定ファイル

| ファイル | 用途 |
|----------|------|
| `config/profile.json` | スキル・興味・プロジェクト情報 |
| `config/llm_config.txt` | Ollamaモデル名、URL、トークン制限 |
| `config/skill_aliases.json` | スキル正規化のための正式名称とエイリアス |
| `prompts/analyse.txt` | 英語求人分析用LLMプロンプト |
| `prompts/analyse_ja.txt` | 日本語求人分析用LLMプロンプト |
| `prompts/cover_p1–p4.txt` | カバーレター生成プロンプト（分割形式） |
| `avoid_keywords.txt` | 不要な求人を除外するキーワード |

---

## 新着情報

### 2026年6月10日
- **日本語求人パース対応** — 日本語で掲載された求人を自動検出し、専用の `analyse_ja.txt` プロンプトで処理。スキル抽出・正規化は英語と同様に機能します。
- **隠しフィールドのパース修正** — これまで取得できていなかった求人詳細（遅延ロードや難読化されたフィールド）を `job_detector.py` と `job_cleaner.py` で正しく取得できるようになりました。
- **スキルエイリアスの大幅拡充** — `skill_aliases.json` のエントリ数が約36件から500件以上に増加。より多くの技術スタック・フレームワーク・職種名に対応し、マッチスコアの精度が向上しました。
- **ライブラリの細かな修正** — `job_analyser.py` の安定性を改善しました。

### 2026年6月8日
- クロール中のページ自動ナビゲーション対応。
- LLMを使ったカバーレター生成と面接提案機能（`advisor.py`）を追加。

### 2026年6月6日
- エイリアスマップによるスキル正規化。
- スコアベースの判定（strong match / possible / no match の評価）。
- 評価別のレポート保存ディレクトリ分け。
- LLM処理の同時実行制御付き入力キュー。

### 2026年6月5日
- LLMサマリーとユーザープロフィールマッチングパイプラインを実装。

### 2026年6月4日
- 旧Job Streamリポジトリから移植。
